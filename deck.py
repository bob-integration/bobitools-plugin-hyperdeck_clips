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
