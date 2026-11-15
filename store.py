# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Persistance (volume `/data`) : lecteurs, listes attendues, fichiers posés, historique.

Un seul fichier JSON, écrit atomiquement sous verrou. Ce qui est stocké ici est ce que
l'OUTIL a fait (quel fichier il a posé sur quel emplacement, en quelle version) ; ce qui
est RÉELLEMENT sur le HyperDeck se relit à chaque vérification (FTP + TCP 9993) et n'est
jamais pris pour acquis : quelqu'un a pu passer par un client FTP entre-temps.
"""
import copy
import json
import os
import re
import secrets
import threading
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
FILE = os.path.join(DATA_DIR, "players.json")
INCOMING = os.path.join(DATA_DIR, "incoming")
WORK = os.path.join(DATA_DIR, "work")

LOCK = threading.RLock()
_data = None

PLAYER_FIELDS = {
    "name": str, "host": str, "ftp_port": int, "ftp_user": str, "ftp_password": str,
    "ftp_dir": str, "tcp_port": int, "audio_channels": int, "protect_on_air": bool,
    "placeholder_duration": int,
}
FORMAT_FIELDS = {"codec_tag": str, "width": int, "height": int, "fps": str, "field_order": str}

DEFAULT_PLAYER = {
    "name": "", "host": "", "ftp_port": 21, "ftp_user": "", "ftp_password": "",
    "ftp_dir": "", "tcp_port": 9993, "audio_channels": 2, "protect_on_air": True,
    "placeholder_duration": 10,
    "format": {"codec_tag": "apch", "width": 1920, "height": 1080, "fps": "25/1",
               "field_order": "tt"},
    "list": None,        # {filename, player, imported_at, imported_by, categories}
    "slots": [],         # [{num, nom, cat, id, player}] triés par numéro
    "files": {},         # num → {kind: clip|placeholder, remote, original, v, size, at, by, label}
    "history": {},       # num → [{v, action, original, remote, size, at, by, label}]
}


def _load():
    global _data
    if _data is None:
        try:
            with open(FILE, encoding="utf-8") as fh:
                _data = json.load(fh)
        except (OSError, ValueError):
            _data = {"players": []}
        for p in _data["players"]:
            for k, v in DEFAULT_PLAYER.items():
                p.setdefault(k, copy.deepcopy(v))
    return _data


def _save():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def players():
    with LOCK:
        return copy.deepcopy(_load()["players"])


def get(pid):
    with LOCK:
        p = next((p for p in _load()["players"] if p["id"] == pid), None)
        return copy.deepcopy(p) if p else None


def mutate(pid, fn):
    """Applique fn(player) sous verrou et persiste. Renvoie (copie du lecteur, retour de fn)."""
    with LOCK:
        p = next((p for p in _load()["players"] if p["id"] == pid), None)
        if p is None:
            raise KeyError(pid)
        r = fn(p)
        _save()
        return copy.deepcopy(p), r


def apply_fields(p, body):
    for k, typ in PLAYER_FIELDS.items():
        if k not in body:
            continue
        v = body[k]
        if k == "ftp_password" and (v is None or v == "__unchanged__"):
            continue
        try:
            p[k] = typ(v) if typ is not bool else bool(v)
        except (TypeError, ValueError):
            raise ValueError("valeur invalide pour %s" % k)
    if isinstance(body.get("format"), dict):
        for k, typ in FORMAT_FIELDS.items():
            if k in body["format"]:
                try:
                    p["format"][k] = typ(body["format"][k])
                except (TypeError, ValueError):
                    raise ValueError("valeur invalide pour format.%s" % k)
    p["name"] = (p["name"] or "").strip() or p["host"] or "Lecteur"
    p["ftp_dir"] = (p["ftp_dir"] or "").strip().strip("/")
    p["placeholder_duration"] = max(1, min(600, int(p["placeholder_duration"] or 10)))
    p["audio_channels"] = max(0, min(16, int(p["audio_channels"] or 0)))


def create(body):
    with LOCK:
        d = _load()
        p = copy.deepcopy(DEFAULT_PLAYER)
        p["id"] = secrets.token_hex(4)
        apply_fields(p, body)
        d["players"].append(p)
        _save()
        return copy.deepcopy(p)


def delete(pid):
    with LOCK:
        d = _load()
        n = len(d["players"])
        d["players"] = [p for p in d["players"] if p["id"] != pid]
        _save()
        return len(d["players"]) != n


def public(p):
    """Vue sans secret (le mot de passe FTP ne ressort jamais)."""
    q = {k: v for k, v in p.items() if k != "ftp_password"}
    q["has_password"] = bool(p.get("ftp_password"))
    return q


# ── Noms de fichiers sur le HyperDeck ───────────────────────────────────────

_BAD = re.compile(r'[\x00-\x1f/\\:*?"<>|]')
_NUM_PREFIX = re.compile(r"^(\d+)\s*-\s*")
TEMP_PREFIX = "zzz_envoi_"
VIDEO_EXT = (".mov", ".mp4", ".mxf", ".mkv")


def clean(name, limit=200):
    n = _BAD.sub("_", str(name or "")).strip().lstrip(".")
    if len(n) > limit:
        base, dot, ext = n.rpartition(".")
        n = (base[:limit - len(ext) - 1] + "." + ext) if dot else n[:limit]
    return n


def num_of(filename):
    m = _NUM_PREFIX.match(filename or "")
    return m.group(1) if m else None


def delivered_name(num, original):
    """« 104 - Interview Dupont v2.mov ». Un nom qui porte DÉJÀ ce numéro en tête (fichier
    relu sur le HyperDeck, clip de test de generer_clips.py) n'est pas préfixé deux fois."""
    o = clean(os.path.basename(original or "clip.mov"))
    m = _NUM_PREFIX.match(o)
    if m and m.group(1) == num:
        o = o[m.end():]
    return clean("%s - %s" % (num, o or "clip.mov"))


def placeholder_name(slot):
    return clean("%s - VIDE - %s.mov" % (slot["num"], slot.get("nom") or "clip"))


def is_placeholder(filename, num):
    return (filename or "").startswith("%s - VIDE - " % num)


def is_media(filename):
    return (not filename.startswith(".")) and filename.lower().endswith(VIDEO_EXT)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
