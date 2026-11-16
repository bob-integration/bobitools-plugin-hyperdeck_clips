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
import unicodedata

DATA_DIR = os.environ.get("DATA_DIR", "/data")
FILE = os.path.join(DATA_DIR, "players.json")
INCOMING = os.path.join(DATA_DIR, "incoming")
WORK = os.path.join(DATA_DIR, "work")
CACHE = os.path.join(DATA_DIR, "cache")     # bouche-trous gardés (option par lecteur)

LOCK = threading.RLock()
_data = None

PLAYER_FIELDS = {
    "name": str, "host": str, "ftp_port": int, "ftp_user": str, "ftp_password": str,
    "ftp_dir": str, "tcp_port": int, "audio_channels": int, "protect_on_air": bool,
    "placeholder_duration": int, "placeholder_template": str, "ph_cache": bool,
}
FORMAT_FIELDS = {"codec_tag": str, "width": int, "height": int, "fps": str, "field_order": str}

DEFAULT_PH_TEMPLATE = "{num} - VIDE - {nom}"   # cf. validate_template

DEFAULT_PLAYER = {
    "name": "", "host": "", "ftp_port": 21, "ftp_user": "", "ftp_password": "",
    "ftp_dir": "", "tcp_port": 9993, "audio_channels": 2, "protect_on_air": True,
    "placeholder_duration": 10, "placeholder_template": DEFAULT_PH_TEMPLATE,
    "ph_cache": False, "ph_style": None,
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
    if "ph_style" in body:
        import media
        p["ph_style"] = media.norm_style(body["ph_style"])
    p["placeholder_template"] = validate_template(p.get("placeholder_template") or DEFAULT_PH_TEMPLATE)
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


_NUM_LEAD = re.compile(r"^(\d+)(?=\D)")


def num_of(filename):
    """Numéro en tête du nom (« 104 - … », « 104_… »). Suivi d'au moins un non-chiffre."""
    m = _NUM_LEAD.match(filename or "")
    return m.group(1) if m else None


def ascii_name(name):
    """Nom SANS accent ni caractère non ASCII. FAIT TERRAIN (2026-11-16, HyperDeck Studio HD
    Plus) : un nom venu d'un Mac (accent DÉCOMPOSÉ, « a » + accent grave) est accepté au STOR,
    lu par le HyperDeck… mais absent de la liste FTP et impossible à supprimer par FTP (DELE →
    451/550). Le clip bloque alors la liste jusqu'au passage de la carte sur un ordinateur.
    Le nom d'un clip livré ne sert à personne (l'exploitant rappelle par RANG) : on ne prend
    aucun risque, tout ce qui n'est pas ASCII imprimable est translittéré ou remplacé."""
    n = unicodedata.normalize("NFKD", str(name or ""))
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.replace("œ", "oe").replace("Œ", "OE").replace("æ", "ae").replace("Æ", "AE").replace("ß", "ss")
    return "".join(c if 32 <= ord(c) < 127 else "_" for c in n)


def delivered_name(num, original):
    """« 104 - Interview Dupont v2.mov ». Un nom qui porte DÉJÀ ce numéro en tête (fichier
    relu sur le HyperDeck, clip de test de generer_clips.py) n'est pas préfixé deux fois.
    Toujours en ASCII, cf. `ascii_name`."""
    o = clean(ascii_name(os.path.basename(original or "clip.mov")))
    m = _NUM_PREFIX.match(o)
    if m and m.group(1) == num:
        o = o[m.end():]
    return clean("%s - %s" % (num, o or "clip.mov"))


# Modèle de nom des bouche-trous, réglable par lecteur. Il DOIT commencer par {num} suivi
# d'un caractère qui n'est pas un chiffre : c'est le numéro en tête qui classe le fichier
# au bon rang (ordre alphabétique du HyperDeck) et qui le rattache à son emplacement.
PH_FIELDS = ("num", "nom", "cat", "id", "lecteur")
_FIELD = re.compile(r"\{([^{}]*)\}")


def validate_template(t):
    t = (t or "").strip()
    if t.lower().endswith(".mov"):
        t = t[:-4].rstrip()
    if not t.startswith("{num}"):
        raise ValueError("le nom des bouche-trous doit commencer par {num} : c'est le numéro "
                         "en tête qui place le fichier au bon rang sur le HyperDeck")
    rest = t[len("{num}"):]
    if not rest or rest[0].isdigit() or rest[0] == "{":
        raise ValueError("après {num}, mettez un séparateur (ex. « {num} - VIDE - {nom} ») : "
                         "sinon le numéro se confondrait avec la suite du nom")
    for f in _FIELD.findall(t):
        if f not in PH_FIELDS:
            raise ValueError("variable inconnue {%s} (disponibles : %s)"
                             % (f, ", ".join("{%s}" % x for x in PH_FIELDS)))
    if _BAD.search(_FIELD.sub("", t)):
        raise ValueError('caractères interdits dans un nom de fichier : / \\ : * ? " < > |')
    return t


def _values(slot, player_name):
    return {"num": slot["num"], "nom": slot.get("nom") or "clip", "cat": slot.get("cat") or "",
            "id": slot.get("id") or "", "lecteur": player_name or ""}


def placeholder_name(slot, template=None, player_name=""):
    v = _values(slot, player_name)
    base = _FIELD.sub(lambda m: str(v.get(m.group(1), "")), template or DEFAULT_PH_TEMPLATE)
    return clean(re.sub(r"\s{2,}", " ", base).strip() + ".mov")


def _ph_regex(slot, template):
    out, pos = "", 0
    for m in _FIELD.finditer(template):
        out += re.escape(template[pos:m.start()])
        out += re.escape(slot["num"]) if m.group(1) == "num" else ".*"
        pos = m.end()
    out += re.escape(template[pos:])
    return re.compile(out + r"\.mov", re.IGNORECASE | re.DOTALL)


def is_placeholder(filename, slot, template=None):
    """Le fichier a-t-il la FORME d'un bouche-trou de cet emplacement (modèle actuel, ou
    modèle par défaut — pour reconnaître ceux posés avant un changement de réglage) ?"""
    n = filename or ""
    return any(_ph_regex(slot, t).fullmatch(n) for t in {template or DEFAULT_PH_TEMPLATE, DEFAULT_PH_TEMPLATE})


def is_media(filename):
    return (not filename.startswith(".")) and filename.lower().endswith(VIDEO_EXT)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
