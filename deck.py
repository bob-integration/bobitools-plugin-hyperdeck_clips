# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Contrôle d'un HyperDeck par son protocole Ethernet (texte, TCP 9993).

Volontairement MINIMAL et à connexion COURTE : l'outil `hyperdeck` tient déjà, lui, une
connexion permanente par machine. Ici on ouvre, on lit `transport info`, `clips count`,
`clips get`, on referme. On ne s'abonne à rien : aucune notification ne vient se glisser
entre les réponses, et la machine ne garde pas un client de plus pour rien (elle n'en
accepte que quelques-uns : `120 connection failed`).

Format de `clips get` (positionnel, PAS des paires clé/valeur) :
    {id}: {nom} {timecode début} {durée}
Le nom peut contenir des espaces : on le lit en partant de la FIN de la ligne.
"""
import re
import socket
import time

PORT = 9993
_HEADER = re.compile(r"^(\d{3})\s*(.*)$")
_CLIP = re.compile(r"^(\d+):\s+(.*?)\s+(\d{2}:\d{2}:\d{2}[:;.]\d{2})\s+(\d{2}:\d{2}:\d{2}[:;.]\d{2})\s*$")


class DeckError(Exception):
    pass


class _Conn:
    def __init__(self, host, port, timeout):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise DeckError("connexion %s:%s impossible (%s)" % (host, port, e))
        self.sock.settimeout(timeout)
        self.buf = b""

    def _line(self, deadline):
        while b"\n" not in self.buf:
            if time.time() > deadline:
                raise DeckError("réponse incomplète (délai dépassé)")
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise DeckError("pas de réponse du HyperDeck")
            if not chunk:
                raise DeckError("connexion fermée par le HyperDeck")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def block(self, timeout=8):
        """Un bloc de réponse : (code, texte, lignes). Ignore les blocs 5xx asynchrones."""
        deadline = time.time() + timeout
        while True:
            head = self._line(deadline)
            if not head.strip():
                continue
            m = _HEADER.match(head)
            if not m:
                continue
            code, text = int(m.group(1)), m.group(2)
            lines = []
            if text.endswith(":"):
                while True:
                    l = self._line(deadline)
                    if l == "":
                        break
                    lines.append(l)
            if 500 <= code < 600 and code != 500:
                continue            # notification non sollicitée : pas notre réponse
            return code, text.rstrip(":"), lines

    def command(self, cmd, timeout=8):
        self.sock.sendall((cmd + "\r\n").encode("utf-8"))
        return self.block(timeout)

    def close(self):
        try:
            self.sock.sendall(b"quit\r\n")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _params(lines):
    out = {}
    for l in lines:
        k, sep, v = l.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


def _int(v):
    v = str(v or "").strip()
    return int(v) if v.isdigit() else None


def slot_space(host, port=PORT, timeout=4):
    """(octets libres, octets totaux) du disque ACTIF, relus à l'instant ; None si la
    machine ne les annonce pas (ancien logiciel : seul le temps restant est donné)."""
    c = _Conn(host, port or PORT, timeout)
    try:
        c.block(timeout)
        code, text, lines = c.command("transport info", timeout)
        sid = str(_params(lines).get("slot id") or "") if code < 300 else ""
        if not sid.isdigit():
            return None
        code, text, lines = c.command("slot info: slot id: %s" % sid, timeout)
        if code >= 300:
            return None
        si = _params(lines)
        free = _int(si.get("remaining size"))
        return (free, _int(si.get("total size"))) if free is not None else None
    finally:
        c.close()


def parse_clips(lines):
    clips = []
    for l in lines:
        m = _CLIP.match(l)
        if m:
            clips.append({"id": int(m.group(1)), "name": m.group(2),
                          "start": m.group(3), "duration": m.group(4)})
        else:
            k, sep, v = l.partition(":")
            if sep and k.strip().isdigit():     # ligne sans timecodes reconnus
                clips.append({"id": int(k), "name": v.strip(), "start": None, "duration": None})
    return clips


def read(host, port=PORT, timeout=5):
    """État utile à l'outil : transport + liste des clips de la timeline."""
    c = _Conn(host, port or PORT, timeout)
    try:
        code, text, lines = c.block(timeout)        # préambule « 500 connection info: »
        if code == 120:
            raise DeckError("le HyperDeck refuse la connexion : trop de clients (120)")
        pre = _params(lines)
        out = {"model": pre.get("model"), "protocol": pre.get("protocol version")}
        code, text, lines = c.command("transport info", timeout)
        out["transport"] = _params(lines) if code < 300 else {"error": "%d %s" % (code, text)}
        # Disque actif : nom du volume et temps d'enregistrement restant (dans le format
        # d'enregistrement courant). Le protocole ne donne PAS d'octets libres.
        sid = str(out["transport"].get("slot id") or "")
        if sid.isdigit():
            code, text, lines = c.command("slot info: slot id: %s" % sid, timeout)
            if code < 300:
                si = _params(lines)
                out["slot"] = {"id": sid, "volume": si.get("volume name"), "status": si.get("status"),
                               "recording_time": int(si["recording time"]) if str(si.get("recording time", "")).isdigit() else None,
                               "video_format": si.get("video format"),
                               # Octets EXACTS (protocole ≥ 1.1x, ex. 1.19 relevé sur Studio HD
                               # Plus le 2026-11-16 : « remaining size », « total size »).
                               "remaining_size": _int(si.get("remaining size")),
                               "total_size": _int(si.get("total size"))}
        # Format d'enregistrement (« file format: QuickTimeProResHQ ») : sert à convertir le
        # temps restant en octets (estimation), le protocole ne donnant pas d'octets libres.
        code, text, lines = c.command("configuration", timeout)
        if 200 <= code < 300:
            out["config"] = _params(lines)
        code, text, lines = c.command("clips count", timeout)
        p = _params(lines)
        out["clips_count"] = int(p["clip count"]) if code < 300 and p.get("clip count", "").isdigit() else None
        code, text, lines = c.command("clips get", timeout=max(timeout, 15))
        if code >= 300:
            raise DeckError("clips get refusé : %d %s" % (code, text))
        out["clips"] = parse_clips(lines)
        return out
    finally:
        c.close()


PLAYING = ("play", "forward", "rewind", "jog", "shuttle", "record")


def on_air(state, names):
    """Le HyperDeck est-il EN TRAIN de lire l'un de ces fichiers ?

    Renvoie le nom concerné, ou None. On compare le clip courant (`clip id`) au rang que la
    timeline donne à ces fichiers ; à l'arrêt ou en preview, rien n'est à l'antenne."""
    tr = state.get("transport") or {}
    if tr.get("status") not in PLAYING:
        return None
    try:
        cur = int(tr.get("clip id"))
    except (TypeError, ValueError):
        return None
    wanted = {strip_ext(n) for n in names}
    for c in state.get("clips") or []:
        if c["id"] == cur and strip_ext(c["name"]) in wanted:
            return c["name"]
    return None


def strip_ext(name):
    n = str(name or "")
    base, dot, ext = n.rpartition(".")
    return base if dot and 1 <= len(ext) <= 4 and base else n


# Débits ProRes d'Apple (Mb/s) en 1920×1080 à 25 images/s (1080i50 / 1080p25), audio en sus.
_RATES = (("proreshq", 184), ("prores4444xq", 415), ("prores4444", 275), ("proreslt", 85),
          ("proresproxy", 38), ("prores", 122), ("dnxhd220", 185), ("dnxhd145", 121),
          ("dnxhd45", 36), ("dnxhrhqx", 185), ("dnxhrhq", 185), ("dnxhrsq", 122), ("dnxhrlb", 38))
_VF = re.compile(r"^(\d{3,4})(i|p|psf)(\d+(?:\.\d+)?)$", re.IGNORECASE)
_WIDTH = {"720": 1280, "1080": 1920, "2160": 3840, "4320": 7680, "486": 720, "576": 720}


def bitrate(file_format, video_format, audio_channels=2):
    """Débit (octets/s) estimé d'un enregistrement, ou None si le codec n'est pas connu
    (H.264/H.265 : débit variable, pas d'estimation honnête possible)."""
    ff = re.sub(r"[^a-z0-9]", "", str(file_format or "").lower()).replace("quicktime", "")
    mbps = next((r for k, r in _RATES if ff.startswith(k)), None)
    m = _VF.match(str(video_format or "").strip())
    if mbps is None or not m or m.group(1) not in _WIDTH:
        return None
    h, kind, rate = int(m.group(1)), m.group(2).lower(), float(m.group(3))
    fps = rate / 2 if kind == "i" else rate
    pixels = _WIDTH[m.group(1)] * h / (1920 * 1080)
    video = mbps * pixels * fps / 25 * 1e6 / 8
    return video + max(0, audio_channels) * 48000 * 3      # PCM 24 bits 48 kHz


_CLIP_RAW = re.compile(rb"^(\d+):\s+(.*?)\s+\d{2}:\d{2}:\d{2}[:;.]\d{2}\s+\d{2}:\d{2}:\d{2}[:;.]\d{2}\s*$")


def raw_clip_names(host, port=PORT, timeout=5):
    """{id: nom en OCTETS BRUTS} de `clips get` : pour viser un fichier dont le nom est mal
    formé exactement comme le HyperDeck l'écrit, sans passer par un décodage."""
    c = _Conn(host, port or PORT, timeout)
    try:
        c.block(timeout)
        c.sock.sendall(b"clips get\r\n")
        deadline, out, started = time.time() + 15, {}, False
        while True:
            while b"\n" not in c.buf:
                if time.time() > deadline:
                    raise DeckError("clips get : réponse incomplète")
                chunk = c.sock.recv(65536)
                if not chunk:
                    raise DeckError("connexion fermée par le HyperDeck")
                c.buf += chunk
            line, _, c.buf = c.buf.partition(b"\n")
            line = line.rstrip(b"\r")
            if not started:
                started = line.startswith(b"205")
                continue
            if not line:
                return out
            m = _CLIP_RAW.match(line)
            if m:
                out[int(m.group(1))] = m.group(2)
    finally:
        c.close()


def remove_from_timeline(host, port, clip_id, timeout=5):
    """`clips remove` : retire un clip de la LISTE DE LECTURE (timeline), pas du disque.
    Provisoire : la machine reconstruit sa liste en relisant le disque."""
    c = _Conn(host, port or PORT, timeout)
    try:
        c.block(timeout)
        code, text, _ = c.command("clips remove: clip id: %d" % int(clip_id), timeout)
        return code, text
    finally:
        c.close()
