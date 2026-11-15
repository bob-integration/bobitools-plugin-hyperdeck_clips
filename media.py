# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Média : contrôle de format (ffprobe), bouche-trous (ffmpeg), correction de trame.

Le HyperDeck ne lit PAS un clip dont le format diffère de celui de sa timeline : un fichier
mal encodé ne fait pas « un peu moins bien », il ne part pas à l'antenne. On refuse donc
avant d'envoyer, sur des critères mesurés par ffprobe — jamais sur l'extension.

Piège des .mov ProRes entrelacés produits par ffmpeg : l'encodeur inscrit « trame basse en
premier » (`bb`, ou `tb` selon la version) dans l'atome `fiel` alors que les images sont bien trame haute en premier.
On ne peut pas trancher en lisant l'en-tête (un vrai fichier `bb` existe aussi) : le
refus reste la règle, et la correction d'en-tête (`-c copy -field_order tt`, sans
réencodage) est un geste PROPOSÉ à l'exploitant, qui sait d'où vient son fichier.
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
    "tb": "entrelacé (tb)", "bt": "entrelacé (bt)",
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

    Renvoie {ok, problems: [str], fixable_field_order: bool}. `fixable_field_order` n'est
    vrai que si la SEULE différence est l'ordre de trame déclaré `bb`/`tb` alors qu'on
    attend `tt` — le cas du bogue d'en-tête ffmpeg, et rien d'autre.
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
    got_fo = summary.get("field_order")
    if got_fo != f["field_order"]:
        problems.append("balayage : %s attendu, reçu %s" % (
            FIELD_LABEL.get(f["field_order"], f["field_order"]),
            FIELD_LABEL.get(got_fo, got_fo)))
    else:
        field_only = False
    if not partial and not (summary.get("duration") or 0) > 0:
        problems.append("durée nulle ou illisible")
        field_only = False
    # Le bogue s'écrit `bb` ou `tb` selon la version de ffmpeg (relevé : Lavf 62 → `tb`).
    fixable = bool(problems) and field_only and got_fo in ("bb", "tb") and f["field_order"] == "tt"
    return {"ok": not problems, "problems": problems, "fixable_field_order": fixable}


def _run(cmd, timeout):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg : " + (p.stderr or "échec").strip()[-300:])


def fix_field_order(src, dst, order="tt", timeout=1800):
    """Réécrit l'ordre de trame déclaré, SANS réencoder (copie des flux)."""
    p = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", src,
                        "-map", "0", "-c", "copy", "-field_order", order, "-f", "mov", dst],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "ffmpeg a échoué").strip()[-300:])


# Apparence des bouche-trous, réglable par lecteur. Défaut = le visuel d'origine : fond
# noir, infos en gris, discret s'il part à l'antenne par erreur.
DEFAULT_STYLE = {
    "bg": "#000000", "fg": "#6E6E6E", "bg_by_cat": {},
    "show": {"num": True, "nom": True, "cat": True, "lecteur": True, "mention": True},
    "mention": "clip non livré",
}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def norm_style(style):
    st = style or {}
    out = {"bg": st.get("bg") if _HEX.match(str(st.get("bg") or "")) else DEFAULT_STYLE["bg"],
           "fg": st.get("fg") if _HEX.match(str(st.get("fg") or "")) else DEFAULT_STYLE["fg"],
           "bg_by_cat": {str(k): v for k, v in (st.get("bg_by_cat") or {}).items()
                         if _HEX.match(str(v or ""))},
           "show": {k: bool((st.get("show") or {}).get(k, d)) for k, d in DEFAULT_STYLE["show"].items()},
           "mention": str(st.get("mention") if st.get("mention") is not None else DEFAULT_STYLE["mention"])[:120]}
    return out


def placeholder_key(slot, fmt, duration, audio_channels, player_name, style=None):
    parts = [slot.get("num"), slot.get("nom"), slot.get("cat"), player_name,
             dict(DEFAULT_FORMAT, **(fmt or {})), duration, audio_channels, 2]
    st = norm_style(style)
    # Le style n'entre dans l'empreinte que s'il diffère du défaut : les bouche-trous posés
    # avant l'arrivée du réglage gardent leur empreinte (et ne passent pas « périmés »).
    if st != norm_style(None):
        parts.append({**st, "bg_cat": st["bg_by_cat"].get(slot.get("cat") or ""), "bg_by_cat": None})
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _filters(slot, player_name, style, w, h, tmp):
    """Calques drawtext empilés et centrés verticalement selon les infos choisies."""
    st = norm_style(style)
    sh, fg, k = st["show"], st["fg"].replace("#", "0x"), h / 1080.0
    sub = " · ".join(x for x in ((slot.get("cat") if sh["cat"] else ""),
                                  (player_name if sh["lecteur"] else "")) if x)
    lines = [(str(slot.get("num") or ""), FONT_BOLD, 150, 1.0) if sh["num"] else None,
             (str(slot.get("nom") or ""), FONT_BOLD, 84, 0.85) if sh["nom"] else None,
             (sub, FONT, 48, 0.7) if sub else None,
             (st["mention"], FONT, 44, 0.7) if sh["mention"] and st["mention"].strip() else None]
    lines = [l for l in lines if l and l[0].strip()]
    heights = [size * 1.55 for _, _, size, _ in lines]
    y = -sum(heights) / 2
    out = []
    for i, ((txt, font, size, alpha), hh) in enumerate(zip(lines, heights)):
        tf = os.path.join(tmp, "l%d.txt" % i)
        with open(tf, "w", encoding="utf-8") as fh:
            fh.write(txt)
        center = y + hh / 2
        y += hh
        out.append("drawtext=fontfile=%s:textfile=%s:fontsize=%d:fontcolor=%s@%.2f:"
                   "x=(w-text_w)/2:y=h/2-text_h/2+%d" % (font, tf, max(10, int(size * k)), fg, alpha,
                                                       int(center * k)))
    bg = st["bg_by_cat"].get(slot.get("cat") or "", st["bg"])
    return out, bg.replace("#", "0x")


def render_preview(slot, fmt, player_name, style, dst, timeout=60):
    """Une image PNG du bouche-trou (aperçu des réglages), sans encodage ProRes."""
    f = dict(DEFAULT_FORMAT, **(fmt or {}))
    w, h = int(f["width"]), int(f["height"])
    with tempfile.TemporaryDirectory(dir=os.path.dirname(dst) or None) as tmp:
        filters, bg = _filters(slot, player_name, style, w, h, tmp)
        _run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
              "color=c=%s:s=%dx%d" % (bg, w, h), "-frames:v", "1",
              "-vf", ",".join(filters + ["scale=960:-2"]) if filters else "scale=960:-2", dst], timeout)
    return dst


def render_placeholder(slot, fmt, duration, audio_channels, player_name, dst, timeout=600, style=None):
    """Bouche-trou au format technique exact des vrais clips. Apparence : `style`
    (défaut : fond noir, infos en gris — discret s'il est rappelé par erreur)."""
    f = dict(DEFAULT_FORMAT, **(fmt or {}))
    pr = PRORES.get(f["codec_tag"])
    if not pr:
        raise RuntimeError("bouche-trou : codec %s non géré" % f["codec_tag"])
    w, h = int(f["width"]), int(f["height"])
    interlaced = f["field_order"] in ("tt", "bb")
    with tempfile.TemporaryDirectory(dir=os.path.dirname(dst) or None) as tmp:
        filters, bg = _filters(slot, player_name, style, w, h, tmp)
        if interlaced:
            filters.append("setfield=%s" % ("tff" if f["field_order"] == "tt" else "bff"))
        filters.append("format=%s" % pr["pix"])
        # ProRes est INTRA : chaque image se décode seule. On n'encode donc qu'UNE image
        # (≈ 0,2 s au lieu de ~5 s pour 10 s de clip), puis on la répète par copie de flux.
        one = os.path.join(tmp, "one.mov")
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=%s:s=%dx%d:r=%s" % (bg, w, h, f["fps"]),
               "-frames:v", "1", "-vf", ",".join(filters),
               "-c:v", "prores_ks", "-profile:v", str(pr["profile"]), "-vendor", "apl0",
               "-pix_fmt", pr["pix"]]
        if interlaced:
            cmd += ["-flags", "+ildct+ilme", "-field_order", f["field_order"]]
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                "-f", "mov", one]
        _run(cmd, timeout)
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
               "-stream_loop", "-1", "-i", one]
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
