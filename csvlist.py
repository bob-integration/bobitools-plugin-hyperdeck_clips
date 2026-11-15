# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Lecture de la liste de clips attendue (CSV `ID;Player;Catégorie;Numéro;Nom`).

Même tolérance que `generer_clips.py` : séparateur `;`, `,` ou tabulation détecté, en-têtes
sans casse ni accents, colonnes en plus ignorées. En revanche on est INTRANSIGEANT sur ce
qui garantit l'ordre au HyperDeck, qui classe les clips par ordre ALPHABÉTIQUE de nom de
fichier :
  - un numéro par emplacement, sans doublon ;
  - tous les numéros de la même largeur (sinon « 1000 » passerait avant « 200 ») —
    l'ordre alphabétique est alors l'ordre numérique.
"""
import csv
import io
import re
import unicodedata


def _norm(s):
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode()
    return s.strip().lower().replace(" ", "").replace("°", "")


ALIAS = {
    "id": ["id", "idclip", "id du clip", "clipid", "identifiant"],
    "player": ["player", "lecteur", "playeur"],
    "num": ["numero", "num", "no", "n", "numéro"],
    "cat": ["categorie", "cat", "theme"],
    "nom": ["nom", "name", "titre"],
}
REQUIRED = ("num", "nom")


class ListError(ValueError):
    pass


def parse(text):
    """Texte CSV → {"rows": [...], "players": [valeurs distinctes de Player]}.

    Lève ListError (message lisible) si la liste ne peut pas garantir l'ordre."""
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise ListError("fichier vide")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    lines = [l for l in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in l)]
    if len(lines) < 2:
        raise ListError("aucune ligne de clip sous l'en-tête")
    heads = [_norm(c) for c in lines[0]]
    idx = {}
    for key, names in ALIAS.items():
        names_n = {_norm(a) for a in names}
        for i, h in enumerate(heads):
            if h in names_n:
                idx[key] = i
                break
    missing = [k for k in REQUIRED if k not in idx]
    if missing:
        raise ListError("colonnes introuvables : %s (en-têtes lus : %s)" % (
            ", ".join({"num": "Numéro", "nom": "Nom"}[k] for k in missing),
            " ; ".join(lines[0])))

    rows = []
    for n, l in enumerate(lines[1:], start=2):
        def val(k):
            i = idx.get(k)
            return l[i].strip() if i is not None and i < len(l) else ""
        num = val("num")
        if num.endswith(".0"):          # Excel transforme 104 en 104.0
            num = num[:-2]
        if not num:
            continue
        if not re.fullmatch(r"\d+", num):
            raise ListError("ligne %d : numéro « %s » non numérique" % (n, num))
        rows.append({"num": num, "nom": val("nom"), "cat": val("cat") or "Sans catégorie",
                     "id": val("id"), "player": val("player"), "line": n})
    if not rows:
        raise ListError("aucun clip numéroté")
    players = []
    for r in rows:
        if r["player"] and r["player"] not in players:
            players.append(r["player"])
    return {"rows": rows, "players": players}


def select(parsed, player=None):
    """Garde les lignes d'un Player (si le CSV en mélange plusieurs) et contrôle l'ordre."""
    rows = parsed["rows"]
    if player:
        rows = [r for r in rows if r["player"] == player]
        if not rows:
            raise ListError("aucun clip pour le player « %s »" % player)
    # Doublons contrôlés PAR player : un CSV commun à deux lecteurs réutilise ses numéros.
    seen = {}
    for r in rows:
        if r["num"] in seen:
            raise ListError("numéro %s en double (lignes %d et %d)" % (r["num"], seen[r["num"]], r["line"]))
        seen[r["num"]] = r["line"]
    rows = [{k: v for k, v in r.items() if k != "line"} for r in rows]
    widths = {len(r["num"]) for r in rows}
    if len(widths) > 1:
        raise ListError(
            "numéros de largeurs différentes (%s chiffres) : l'ordre alphabétique du "
            "HyperDeck ne serait plus l'ordre numérique. Complétez par des zéros (ex. 042)."
            % " et ".join(str(w) for w in sorted(widths)))
    return sorted(rows, key=lambda r: r["num"])


def categories(rows):
    """Catégories dans leur ordre d'apparition (l'ordre du CSV, pas l'alphabet)."""
    out = []
    for r in rows:
        if r["cat"] not in out:
            out.append(r["cat"])
    return out


def diff(old_rows, new_rows):
    """Écarts entre l'ancienne liste et la nouvelle, par numéro."""
    o = {r["num"]: r for r in old_rows or []}
    n = {r["num"]: r for r in new_rows}
    added = [n[k] for k in sorted(n) if k not in o]
    removed = [o[k] for k in sorted(o) if k not in n]
    changed = [{"num": k, "before": o[k], "after": n[k]} for k in sorted(n)
               if k in o and (o[k]["nom"], o[k]["cat"]) != (n[k]["nom"], n[k]["cat"])]
    return {"added": added, "removed": removed, "changed": changed}
