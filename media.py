# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Média : contrôle de format (ffprobe), bouche-trous (ffmpeg), correction de trame.

Le HyperDeck ne lit PAS un clip dont le format diffère de celui de sa timeline : un fichier
mal encodé ne fait pas « un peu moins bien », il ne part pas à l'antenne. On refuse donc
avant d'envoyer, sur des critères mesurés par ffprobe — jamais sur l'extension.

Ordre des trames — on le lit dans les IMAGES, pas dans l'étiquette du conteneur. Chaque
image ProRes porte dans son en-tête (« icpf ») son mode : progressif, trame haute en premier
ou trame basse en premier. C'est la vérité ; l'atome `fiel` du conteneur n'est qu'une étiquette.
FAIT TERRAIN 2026-11-22 (clip enregistré par un HyperDeck Studio en 1080i50) : le HyperDeck
étiquette lui-même la trame haute `fiel 02 09`, que ffprobe NOMME « tb » — comme Apple ProRes
(After Effects, Avid). Ce « tb » n'est PAS un défaut : jusqu'en 0.9.11 l'outil le prenait pour
un « bogue d'en-tête de ffmpeg », refusait des fichiers justes et aurait refusé un clip
enregistré par le HyperDeck. Règle depuis 0.9.12 :
  - images dans l'ordre attendu + étiquette concordante (`tt` ou `tb` pour la trame haute) → accepté ;
  - images dans l'ordre attendu + étiquette discordante → correction de l'ÉTIQUETTE proposée
    (`-c copy`, sans réencoder), réécrite comme l'écrit le HyperDeck (`tb`, soit `fiel 02 09`) ;
  - images dans l'AUTRE ordre → refus, jamais de correction (l'image tremblerait à l'antenne).
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile

FONT = "/usr/share/fonts/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"

# Étiquette QuickTime → profil ProRes. L'étiquette est ce que lit le HyperDeck ; le libellé
# `profile` de ffprobe n'en est qu'une traduction.
PRORES = {
    "apco": {"label": "ProRes 422 Proxy", "profile": 0, "pix": "yuv422p10le"},
    "apcs": {"label": "ProRes 422 LT", "profile": 1, "pix": "yuv422p10le"},
    "apcn": {"label": "ProRes 422", "profile": 2, "pix": "yuv422p10le"},
    "apch": {"label": "ProRes 422 HQ", "profile": 3, "pix": "yuv422p10le"},
    "ap4h": {"label": "ProRes 4444", "profile": 4, "pix": "yuva444p10le"},
}

FIELD_LABEL = {
    "tt": "entrelacé, trame haute en premier",
    "bb": "entrelacé, trame basse en premier",
    # « tb »/« bt » : noms que ffprobe donne aux étiquettes `fiel 02 09` / `02 0e`, celles
    # qu'écrivent le HyperDeck et Apple ProRes pour la trame haute / basse en premier.
    "tb": "entrelacé, trame haute en premier",
    "bt": "entrelacé, trame basse en premier",
    "progressive": "progressif", "unknown": "balayage inconnu", None: "balayage non déclaré",
}

DEFAULT_FORMAT = {"codec_tag": "apch", "width": 1920, "height": 1080,
                  "fps": "25/1", "field_order": "tt"}


def format_label(fmt):
    f = dict(DEFAULT_FORMAT, **(fmt or {}))
    num, _, den = str(f["fps"]).partition("/")
    rate = float(num) / float(den or 1)
    rate_s = ("%g" % rate)
    scan = "i" if f["field_order"] in ("tt", "bb") else "p"
    # 1080i50 : en entrelacé, on nomme la cadence en TRAMES (convention broadcast).
    cad = ("%g" % (rate * 2)) if scan == "i" else rate_s
    return "%s, %d × %d, %s i/s %s (%s%s)" % (
        PRORES.get(f["codec_tag"], {}).get("label", f["codec_tag"]), f["width"], f["height"],
        rate_s, FIELD_LABEL.get(f["field_order"], f["field_order"]), f["height"], scan + cad)


# Étiquettes de conteneur qui concordent avec un ordre d'images, et celle que le HyperDeck
# écrit lui-même (cible de la correction d'étiquette).
FIELD_TAGS = {"tff": ("tt", "tb"), "bff": ("bb", "bt")}
FIELD_NATIVE = {"tff": "tb", "bff": "bt"}
SCAN_OF = {"tt": "tff", "bb": "bff"}
SCAN_LABEL = {"tff": "trame haute (supérieure) en premier", "bff": "trame basse (inférieure) en premier",
              "progressive": "progressives"}


def prores_scan(data):
    """Ordre des trames lu dans l'en-tête de la première image ProRes trouvée dans `data`
    (octets bruts du fichier) : 'tff' | 'bff' | 'progressive' | None (pas trouvé).
    En-tête d'image : taille(4) « icpf » taille_en-tête(2) version(2) créateur(4)
    largeur(2) hauteur(2) drapeaux(1) — bits 3-2 des drapeaux : 0 progressif, 1 trame
    haute, 2 trame basse (relevé : 0x84 / 0x88 sur Apple, Avid et HyperDeck)."""
    i = data.find(b"icpf")
    while i >= 0:
        h = data[i + 4:i + 17]
        if len(h) == 13:
            hdr = int.from_bytes(h[0:2], "big")
            w, hh = int.from_bytes(h[8:10], "big"), int.from_bytes(h[10:12], "big")
            if 20 <= hdr <= 1024 and 16 <= w <= 16384 and 16 <= hh <= 16384:
                return {0: "progressive", 1: "tff", 2: "bff"}.get((h[12] >> 2) & 3)
        i = data.find(b"icpf", i + 4)
    return None


def scan_file(path, limit=32 << 20, timeout=60):
    """Ordre des trames des IMAGES d'un fichier : en-tête ProRes dans les premiers Mo, sinon
    ce que ffprobe dit de la première image décodée. None si rien de sûr."""
    try:
        with open(path, "rb") as fh:
            got = prores_scan(fh.read(limit))
        if got:
            return got
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                            "-show_entries", "frame=interlaced_frame,top_field_first", "-of", "json", path],
                           capture_output=True, text=True, timeout=timeout)
        fr = (json.loads(p.stdout or "{}").get("frames") or [None])[0]
        if fr:
            if not fr.get("interlaced_frame"):
                return "progressive"
            return "tff" if fr.get("top_field_first") else "bff"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def probe(path, timeout=60):
    """Sortie JSON de ffprobe. Lève RuntimeError avec le message de ffprobe en cas d'échec."""
    p = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", path], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "ffprobe a échoué").strip().splitlines()[-1][:300])
    return json.loads(p.stdout or "{}")


def _rate(s):
    try:
        n, _, d = str(s).partition("/")
        return float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        return None


def summarize(info):
    """Résumé lisible et compact d'un ffprobe (ce que l'UI affiche et ce qu'on archive)."""
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = info.get("format") or {}
    out = {"container": fmt.get("format_name"), "duration": _float(fmt.get("duration")),
           "size": _int(fmt.get("size")), "encoder": (fmt.get("tags") or {}).get("encoder"),
           "audio": [{"codec": a.get("codec_name"), "channels": a.get("channels"),
                      "rate": _int(a.get("sample_rate"))} for a in audio]}
    if v:
        out.update({"codec": v.get("codec_name"), "codec_tag": v.get("codec_tag_string"),
                    "profile": v.get("profile"), "width": v.get("width"),
                    "height": v.get("height"), "pix_fmt": v.get("pix_fmt"),
                    "fps": v.get("r_frame_rate"), "avg_fps": v.get("avg_frame_rate"),
                    "field_order": v.get("field_order")})
        out["label"] = "%s, %s × %s, %s i/s, %s" % (
            PRORES.get(v.get("codec_tag_string"), {}).get("label")
            or ("%s %s" % (v.get("codec_name") or "?", v.get("profile") or "")).strip(),
            v.get("width"), v.get("height"), "%g" % (_rate(v.get("r_frame_rate")) or 0),
            FIELD_LABEL.get(v.get("field_order"), v.get("field_order")))
    return out


def _float(x):
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def check(summary, fmt, partial=False):
    """Compare un résumé au format attendu.

    Renvoie {ok, problems: [str], fixable_field_order: bool, fix_to?}. `fixable_field_order`
    n'est vrai que si la SEULE différence est l'ÉTIQUETTE d'ordre des trames, les images
    (`summary["scan"]`, lu par scan_file) étant dans l'ordre attendu ; `fix_to` = étiquette
    à écrire (celle du HyperDeck).
    `partial` : contrôle sur l'en-tête seul (pré-contrôle navigateur) — la durée n'a pas
    de sens et l'absence de données média est normale.
    """
    f = dict(DEFAULT_FORMAT, **(fmt or {}))
    problems, field_only = [], True
    if not summary.get("codec"):
        return {"ok": False, "problems": ["aucune piste vidéo lisible"],
                "fixable_field_order": False}
    if summary.get("codec") != "prores" or summary.get("codec_tag") != f["codec_tag"]:
        want = PRORES.get(f["codec_tag"], {}).get("label", f["codec_tag"])
        problems.append("codec : %s attendu, reçu %s" % (
            want, PRORES.get(summary.get("codec_tag"), {}).get("label")
            or "%s (%s)" % (summary.get("codec"), summary.get("codec_tag"))))
        field_only = False
    if summary.get("width") != f["width"] or summary.get("height") != f["height"]:
        problems.append("image : %d × %d attendu, reçu %s × %s" % (
            f["width"], f["height"], summary.get("width"), summary.get("height")))
        field_only = False
    want_rate, got_rate = _rate(f["fps"]), _rate(summary.get("fps"))
    if want_rate is None or got_rate is None or abs(want_rate - got_rate) > 0.001:
        problems.append("cadence : %g i/s attendu, reçu %s" % (
            want_rate or 0, ("%g" % got_rate) if got_rate is not None else "inconnue"))
        field_only = False
    got_fo, scan = summary.get("field_order"), summary.get("scan")
    want_scan, label_only = SCAN_OF.get(f["field_order"]), False
    if want_scan:                                   # entrelacé attendu
        if scan == want_scan:
            if got_fo not in FIELD_TAGS[want_scan]:
                label_only = True
                problems.append("étiquette du fichier : les images sont bien en %s, mais le fichier déclare « %s » "
                                "— l'étiquette peut être corrigée sans réencoder" % (
                                    SCAN_LABEL[scan], FIELD_LABEL.get(got_fo, got_fo)))
        elif scan in ("tff", "bff"):
            problems.append("ordre des trames : le HyperDeck attend des images en %s, celles de ce fichier sont en %s "
                            "— refaites l'export en %s" % (SCAN_LABEL[want_scan], SCAN_LABEL[scan], SCAN_LABEL[want_scan]))
        elif scan == "progressive":
            problems.append("balayage : le HyperDeck attend des images entrelacées (%s), celles de ce fichier sont "
                            "progressives" % SCAN_LABEL[want_scan])
        elif got_fo in FIELD_TAGS[want_scan]:
            pass                                    # images non lues : l'étiquette concorde
        elif partial and got_fo in ("tt", "tb", "bb", "bt"):
            pass                                    # pré-contrôle sans image : le fichier complet tranchera
        else:
            problems.append("balayage : %s attendu, le fichier déclare %s" % (
                FIELD_LABEL.get(f["field_order"], f["field_order"]), FIELD_LABEL.get(got_fo, got_fo)))
    elif got_fo != f["field_order"] or scan in ("tff", "bff"):
        problems.append("balayage : %s attendu, reçu %s" % (
            FIELD_LABEL.get(f["field_order"], f["field_order"]),
            ("entrelacé, " + SCAN_LABEL[scan]) if scan in ("tff", "bff") else FIELD_LABEL.get(got_fo, got_fo)))
    if not label_only:
        field_only = False
    if not partial and not (summary.get("duration") or 0) > 0:
        problems.append("durée nulle ou illisible")
        field_only = False
    # Corrigeable : SEULE l'étiquette est en cause, les images étant lues dans le bon ordre.
    fixable = bool(problems) and field_only
    out = {"ok": not problems, "problems": problems, "fixable_field_order": fixable}
    if fixable:
        out["fix_to"] = FIELD_NATIVE[want_scan]
    return out


def ffmpeg_error(stderr, what="fabrication"):
    """Message d'échec de ffmpeg LISIBLE : la cause connue en clair, le texte brut de ffmpeg
    (jargon : « lavfi », « Lavf »…) seulement en complément, entre parenthèses."""
    raw = (stderr or "").strip()
    if "No space left" in raw:
        return "%s impossible : disque du serveur plein. Libérez de la place (cache des bouche-trous, transferts en cours), puis recommencez." % what
    return "%s impossible : erreur de l'outil de traitement vidéo du serveur (détail technique : %s)" % (what, raw[-300:] or "aucun")


def _run(cmd, timeout):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(ffmpeg_error(p.stderr, "fabrication du bouche-trou"))


def fix_field_order(src, dst, order="tt", timeout=1800):
    """Réécrit l'ordre de trame déclaré, SANS réencoder (copie des flux)."""
    p = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", src,
                        "-map", "0", "-c", "copy", "-field_order", order, "-f", "mov", dst],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(ffmpeg_error(p.stderr, "correction de l'en-tête"))


# Apparence des bouche-trous, réglable par lecteur. Défaut = le visuel d'origine : fond
# noir, infos en gris, discret s'il part à l'antenne par erreur.
#   anim        : trois points dont l'allumé fait l'aller-retour, et un compteur des secondes,
#                 pour voir que le HyperDeck LIT. Tout change ANIM_STEPS fois par seconde :
#                 on n'encode que ces images-là, répétées par copie de flux (coût ≈ fixe).
#   transparent : ProRes 4444 seulement (fill & key) : le fond devient un cartouche autour
#                 des infos, le reste de l'image est transparent.
DEFAULT_STYLE = {
    "bg": "#000000", "fg": "#6E6E6E", "bg_by_cat": {},
    "show": {"num": True, "nom": True, "cat": True, "lecteur": True, "mention": True},
    "mention": "clip non livré", "anim": False, "transparent": False,
}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_NEW_KEYS = ("anim", "transparent")   # absents des empreintes tant qu'ils sont éteints
ANIM_STEPS = 5   # images différentes par seconde en mode animé (aller-retour en 0,8 s)


def norm_style(style):
    st = style or {}
    out = {"bg": st.get("bg") if _HEX.match(str(st.get("bg") or "")) else DEFAULT_STYLE["bg"],
           "fg": st.get("fg") if _HEX.match(str(st.get("fg") or "")) else DEFAULT_STYLE["fg"],
           "bg_by_cat": {str(k): v for k, v in (st.get("bg_by_cat") or {}).items()
                         if _HEX.match(str(v or ""))},
           "show": {k: bool((st.get("show") or {}).get(k, d)) for k, d in DEFAULT_STYLE["show"].items()},
           "mention": str(st.get("mention") if st.get("mention") is not None else DEFAULT_STYLE["mention"])[:120],
           "anim": bool(st.get("anim")), "transparent": bool(st.get("transparent"))}
    return out


def placeholder_key(slot, fmt, duration, audio_channels, player_name, style=None):
    parts = [slot.get("num"), slot.get("nom"), slot.get("cat"), player_name,
             dict(DEFAULT_FORMAT, **(fmt or {})), duration, audio_channels, 2]
    st = norm_style(style)
    # Le style n'entre dans l'empreinte que s'il diffère du défaut : les bouche-trous posés
    # avant l'arrivée du réglage gardent leur empreinte (et ne passent pas « périmés »).
    # Idem pour les options venues après (anim, transparent) : absentes tant qu'éteintes.
    if st != norm_style(None):
        part = {**st, "bg_cat": st["bg_by_cat"].get(slot.get("cat") or ""), "bg_by_cat": None}
        for k in _NEW_KEYS:
            if not part[k]:
                del part[k]
        if part.get("anim"):
            part["anim"] = "points"   # 0.9.1 : la roue de 0.9.0 devient trois points → à refaire
        parts.append(part)
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _alpha(st, fmt):
    return bool(st["transparent"]) and (fmt or {}).get("codec_tag") == "ap4h"


def _mix(fg, bg, a):
    """Couleur `fg` posée à l'opacité `a` sur `bg`, rendue OPAQUE (« 0xRRGGBB@1.00 »)."""
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "0x%02X%02X%02X@1.00" % tuple(round(x * a + y * (1 - a)) for x, y in zip(f, b))


def _layout(slot, player_name, st, w, h, tmp, fg=None, counter=None, opaque_on=None):
    """Calques empilés et centrés verticalement : infos choisies, puis (anim) points + compteur.
    Rend (filtres de la chaîne principale, position du point allumé ou None).
    `opaque_on` (mode transparent) : les gris semi-transparents sont pré-mélangés à ce fond,
    sinon ils perceraient la découpe (le programme passerait à travers les lettres)."""
    sh, k = st["show"], h / 1080.0
    fg = fg or st["fg"]

    def col(a):
        return _mix(fg, opaque_on, a) if opaque_on else "%s@%.2f" % (fg.replace("#", "0x"), a)
    sub = " · ".join(x for x in ((slot.get("cat") if sh["cat"] else ""),
                                  (player_name if sh["lecteur"] else "")) if x)
    lines = [(str(slot.get("num") or ""), FONT_BOLD, 150, 1.0) if sh["num"] else None,
             (str(slot.get("nom") or ""), FONT_BOLD, 84, 0.85) if sh["nom"] else None,
             (sub, FONT, 48, 0.7) if sub else None,
             (st["mention"], FONT, 44, 0.7) if sh["mention"] and st["mention"].strip() else None]
    lines = [l for l in lines if l and l[0].strip()]
    heights = [size * 1.55 for _, _, size, _ in lines]
    wheel_h, cnt_h = 90, 80
    total = sum(heights) + (wheel_h + cnt_h if st["anim"] else 0)
    y = -total / 2
    out = []
    for i, ((txt, font, size, alpha), hh) in enumerate(zip(lines, heights)):
        tf = os.path.join(tmp, "l%d.txt" % i)
        with open(tf, "w", encoding="utf-8") as fh:
            fh.write(txt)
        center = y + hh / 2
        y += hh
        out.append("drawtext=fontfile=%s:textfile=%s:fontsize=%d:fontcolor=%s:"
                   "x=(w-text_w)/2:y=h/2-text_h/2+%d" % (font, tf, max(10, int(size * k)), col(alpha),
                                                       int(center * k)))
    dot = None
    if st["anim"]:
        # Trois points ; l'allumé fait l'aller-retour gauche → milieu → droite → milieu
        # (un cran par image encodée, ANIM_STEPS par seconde : visible même sur 2 s).
        cx, cy, g, d = w / 2, h / 2 + (y + wheel_h / 2) * k, 60 * k, max(4, int(22 * k))
        for i in (-1, 0, 1):
            out.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=%s:t=fill"
                       % (cx + i * g - d / 2, cy - d / 2, d, d, col(0.35)))
        # Compteur des secondes : ANIM_STEPS images encodées par seconde.
        txt = counter if counter is not None else r"%{eif\:floor(n/" + str(ANIM_STEPS) + r")\:d\:2}"
        out.append("drawtext=fontfile=%s:fontsize=%d:fontcolor=%s:x=(w-text_w)/2:"
                   "y=h/2-text_h/2+%d:text='%s'" % (FONT_BOLD, max(10, int(56 * k)), col(0.9),
                                                    int((y + wheel_h + cnt_h / 2) * k), txt))
        dd = max(6, int(30 * k))
        dot = {"cx": cx, "cy": cy, "g": g, "d": dd}
    return out, dot


def _bbox(slot, player_name, st, w, h, tmp, timeout):
    """Cadre exact des infos (textes, points, compteur au plus large) : mesuré par ffmpeg
    lui-même (filtre bbox) sur un rendu blanc sur noir — aucune estimation de police."""
    filters, dot = _layout(slot, player_name, st, w, h, tmp, fg="#FFFFFF", counter="88")
    if dot:   # les trois positions du point allumé
        filters.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=white:t=fill"
                       % (dot["cx"] - dot["g"] - dot["d"] / 2, dot["cy"] - dot["d"] / 2,
                          2 * dot["g"] + dot["d"], dot["d"]))
    if not filters:
        return None
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-f", "lavfi",
                        "-i", "color=c=black:s=%dx%d" % (w, h), "-frames:v", "1",
                        "-vf", ",".join(filters + ["bbox=min_val=40"]), "-f", "null", "-"],
                       capture_output=True, text=True, timeout=timeout)
    m = re.findall(r"x1:(\d+) x2:(\d+) y1:(\d+) y2:(\d+)", p.stderr or "")
    if not m:
        return None
    x1, x2, y1, y2 = map(int, m[-1])
    pad = int(56 * h / 1080.0)
    x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
    x2, y2 = min(w - 1, x2 + pad), min(h - 1, y2 + pad)
    return x1 - x1 % 2, y1 - y1 % 2, (x2 - x1 + 2) // 2 * 2, (y2 - y1 + 2) // 2 * 2


def _graph(slot, player_name, st, fmt, w, h, tmp, rate, timeout):
    """Entrées lavfi + graphe complet → [v]. `rate` = cadence de la source."""
    alpha = _alpha(st, fmt)
    bgh = st["bg_by_cat"].get(slot.get("cat") or "", st["bg"])
    bg = bgh.replace("#", "0x")
    filters, dot = _layout(slot, player_name, st, w, h, tmp, opaque_on=bgh if alpha else None)
    if alpha:
        inputs = ["color=c=black@0:s=%dx%d:r=%s,format=yuva444p10le" % (w, h, rate)]
        box = _bbox(slot, player_name, st, w, h, tmp, timeout)
        if box:   # cartouche autour des infos ; sans info du tout, image entièrement transparente
            filters.insert(0, "drawbox=x=%d:y=%d:w=%d:h=%d:color=%s@1:t=fill:replace=1" % (box + (bg,)))
    else:
        inputs = ["color=c=%s:s=%dx%d:r=%s" % (bg, w, h, rate)]
    graph = "[0]" + (",".join(filters) if filters else "null")
    if dot:
        inputs.append("color=c=%s:s=%dx%d:r=%s" % (st["fg"].replace("#", "0x"), dot["d"], dot["d"], rate))
        # position 0,1,2,1,0… = 1 - |(n mod 4) - 2| autour du centre
        graph += "[b];[b][1]overlay=eval=frame:x=%.1f+(1-abs(mod(n\\,4)-2))*%.1f-%d:y=%d" % (
            dot["cx"], dot["g"], dot["d"] // 2, dot["cy"] - dot["d"] // 2)
    return inputs, graph


def _lavfi(inputs):
    out = []
    for i in inputs:
        out += ["-f", "lavfi", "-i", i]
    return out


def render_preview(slot, fmt, player_name, style, dst, timeout=60):
    """Une image PNG du bouche-trou (aperçu des réglages), sans encodage ProRes. En mode
    transparent, PNG avec couche alpha (la page la montre sur un damier)."""
    f = dict(DEFAULT_FORMAT, **(fmt or {}))
    st = norm_style(style)
    w, h = int(f["width"]), int(f["height"])
    with tempfile.TemporaryDirectory(dir=os.path.dirname(dst) or None) as tmp:
        inputs, graph = _graph(slot, player_name, st, f, w, h, tmp, "25", timeout)
        graph += ",scale=960:-2" + (",format=rgba" if _alpha(st, f) else "") + "[v]"
        _run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error"] + _lavfi(inputs)
             + ["-filter_complex", graph, "-map", "[v]", "-frames:v", "1", dst], timeout)
    return dst


def _fps_frac(fps):
    try:
        n, d = (str(fps).split("/") + ["1"])[:2]
        return int(n), int(d)
    except ValueError:
        return 25, 1


def render_placeholder(slot, fmt, duration, audio_channels, player_name, dst, timeout=600, style=None):
    """Bouche-trou au format technique exact des vrais clips. Apparence : `style`
    (défaut : fond noir, infos en gris — discret s'il est rappelé par erreur)."""
    f = dict(DEFAULT_FORMAT, **(fmt or {}))
    pr = PRORES.get(f["codec_tag"])
    if not pr:
        raise RuntimeError("bouche-trou : codec %s non géré" % f["codec_tag"])
    st = norm_style(style)
    w, h = int(f["width"]), int(f["height"])
    interlaced = f["field_order"] in ("tt", "bb")
    duration = max(1, int(duration))
    # ProRes est INTRA : chaque image se décode seule. On n'encode donc que les images
    # DIFFÉRENTES (une seule, ou ANIM_STEPS par seconde en mode animé), puis on les répète par
    # copie de flux (≈ 0,5 s au lieu de ~6 s pour 10 s de clip).
    frames = duration * ANIM_STEPS if st["anim"] else 1
    with tempfile.TemporaryDirectory(dir=os.path.dirname(dst) or None) as tmp:
        inputs, graph = _graph(slot, player_name, st, f, w, h, tmp, f["fps"], timeout)
        if interlaced:
            graph += ",setfield=%s" % ("tff" if f["field_order"] == "tt" else "bff")
        graph += ",format=%s[v]" % pr["pix"]
        enc = ["-c:v", "prores_ks", "-profile:v", str(pr["profile"]), "-vendor", "apl0",
               "-pix_fmt", pr["pix"]]
        if pr["pix"].startswith("yuva"):
            enc += ["-alpha_bits", "16"]
        if interlaced:
            enc += ["-flags", "+ildct+ilme", "-field_order", f["field_order"]]
        enc += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
        base = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"] + _lavfi(inputs) \
            + ["-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames)] + enc
        if frames == 1:
            src = os.path.join(tmp, "one.mov")
            _run(base + ["-f", "mov", src], timeout)
            vin = ["-stream_loop", "-1", "-i", src]
        else:
            # Une image par fichier, puis liste de concaténation : chaque seconde répétée
            # autant de fois qu'elle a d'images (cadences fractionnaires comprises).
            _run(base + ["-f", "segment", "-segment_format", "mov", "-reset_timestamps", "1",
                         "-segment_frames", ",".join(str(i) for i in range(1, frames)),
                         os.path.join(tmp, "s%05d.mov")], timeout)
            num, den = _fps_frac(f["fps"])
            total = duration * num // den
            lst = os.path.join(tmp, "list.txt")
            with open(lst, "w") as fh:
                for i in range(total):
                    fh.write("file 's%05d.mov'\n" % min(frames - 1, i * den * ANIM_STEPS // num))
            vin = ["-f", "concat", "-safe", "0", "-i", lst]
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"] + vin
        if audio_channels:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono"]
        cmd += ["-t", str(duration), "-map", "0:v"]
        if audio_channels:
            cmd += ["-map", "1:a", "-c:a", "pcm_s24le", "-ac", str(int(audio_channels))]
        # `-field_order` à la copie : c'est ce qui réécrit l'atome `fiel` (bogue d'en-tête).
        cmd += ["-c:v", "copy", "-field_order", f["field_order"] if interlaced else "progressive",
                "-f", "mov", dst + ".part"]
        _run(cmd, timeout)
    os.replace(dst + ".part", dst)
    return dst
