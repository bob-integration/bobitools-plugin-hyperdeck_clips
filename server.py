# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Serveur de l'outil « Clips HyperDeck » (runtime=docker).

HTTP minimal (ThreadingHTTPServer, stdlib), proxifié par l'app sous
/api/tools/hyperdeck_clips/*. Les ENVOIS de fichiers arrivent en
`application/octet-stream` (paramètres en query string) : le cœur les relaie par morceaux,
et on les écrit ici sur disque par morceaux — un clip ProRes HQ pèse ses gigaoctets.

Un fichier n'est jamais envoyé au HyperDeck sans avoir passé ffprobe sur le fichier
COMPLET reçu. Le pré-contrôle (`precheck`, en-tête seul lu par le navigateur) n'est qu'un
confort : il évite d'envoyer 2 Go pour se les voir refuser.
"""
import json
import os
import re
import shutil
import secrets
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

import csvlist
import deck
import engine
import media
import store

PORT = int(os.environ.get("PORT", "8080"))
SPACE_MARGIN = 256 * 1024 * 1024        # on garde toujours ce volume libre sur /data
PRECHECK_MAX = 64 * 1024 * 1024          # un en-tête moov ne pèse jamais autant


class HTTPError(Exception):
    def __init__(self, code, data):
        super().__init__(data)
        self.code, self.data = code, data


def _err(code, msg, **extra):
    return HTTPError(code, dict({"error": msg}, **extra))


def _player(pid):
    p = store.get(pid)
    if not p:
        raise _err(404, "lecteur inconnu")
    return p


def _slot(p, num):
    s = next((s for s in p["slots"] if s["num"] == num), None)
    if not s:
        raise _err(404, "emplacement %s absent de la liste de ce lecteur" % num)
    return s


def _view(p):
    """Tout ce que la page affiche pour un lecteur, en une réponse."""
    sc = engine.last_scan(p["id"])
    ev = engine.evaluate(p, sc)
    js = engine.jobs(p["id"])
    active = {}
    for j in js:
        if j["num"] and j["state"] not in ("done", "cancelled"):
            if j["state"] != "error" or j["num"] not in active:
                active[j["num"]] = j
    slots = []
    for s in p["slots"]:
        e = ev["slots"][s["num"]]
        slots.append(dict(s, **e, file=p["files"].get(s["num"]),
                          history=p["history"].get(s["num"], []),
                          placeholder_name=engine.ph_name(p, s),
                          job=active.get(s["num"])))
    scan = None
    if sc:
        scan = {"at": sc["at"],
                "ftp": {k: v for k, v in sc["ftp"].items() if k != "files"},
                "deck": {k: v for k, v in sc["deck"].items() if k not in ("clips",)},
                "files_count": len(sc["ftp"].get("files") or [])}
    return {"player": store.public(p), "slots": slots, "storage": _storage(p, sc),
            "no_rename": engine._no_rename.get(p["id"]),
            "categories": csvlist.categories(p["slots"]),
            "format_label": media.format_label(p["format"]),
            "scan": scan, "anomalies": {k: ev[k] for k in ("foreign", "temps", "others")},
            "order": ev["order"], "jobs": [j for j in js if j["state"] != "done"][-30:],
            "done": [j for j in js if j["state"] == "done"][-10:],
            "pending": engine.pending_list(p["id"]), "queue": engine.queue_state(p["id"]),
            "sides": engine.SIDES}


def _dir_bytes(path):
    total = 0
    try:
        for e in os.scandir(path):
            if e.is_file(follow_symlinks=False):
                total += e.stat(follow_symlinks=False).st_size
            elif e.is_dir(follow_symlinks=False):
                total += _dir_bytes(e.path)
    except OSError:
        pass
    return total


def _storage(p, sc):
    """Espace : celui du SERVEUR (chaque clip y transite en entier avant le FTP) et ce
    qu'on sait du HyperDeck (taille des clips relevée par FTP, temps d'enregistrement
    restant annoncé par le protocole — il ne donne pas d'octets libres)."""
    du = shutil.disk_usage(store.DATA_DIR)
    out = {"server": {"total": du.total, "used": du.used, "free": du.free,
                      "in_transit": _dir_bytes(store.INCOMING) + _dir_bytes(store.WORK),
                      "cache": _dir_bytes(store.CACHE),
                      "margin": SPACE_MARGIN}}
    if sc and sc["ftp"].get("ok"):
        sizes = sc["ftp"].get("sizes") or {}
        media_ = [n for n in sc["ftp"]["files"] if store.is_media(n)]
        known = [sizes[n] for n in media_ if n in sizes]
        out["deck"] = {"files": len(media_), "bytes": sum(known) if known else None,
                       "complete": len(known) == len(media_)}
    if sc and sc["deck"].get("ok") and sc["deck"].get("slot"):
        d = out.setdefault("deck", {})
        d["slot"] = sc["deck"]["slot"]
        # Espace libre : le protocole ne donne que le TEMPS d'enregistrement restant. Converti
        # en octets au débit du format d'enregistrement courant → estimation (« ≈ »).
        import deck as _deck
        cfg = sc["deck"].get("config") or {}
        rt = d["slot"].get("recording_time")
        bps = _deck.bitrate(cfg.get("file format"), d["slot"].get("video_format"),
                            p.get("audio_channels") or 2)
        d["record_format"] = cfg.get("file format")
        d["margin"] = engine.DECK_MARGIN
        d["queued"] = engine.pending_bytes(p["id"])
        if d["slot"].get("remaining_size") is not None:
            # Octets EXACTS annoncés par la machine : plus d'estimation.
            d["free"] = d["slot"]["remaining_size"]
            d["total"] = d["slot"].get("total_size")
        elif rt is not None and bps:
            d["free_est"] = int(rt * bps)
            if d.get("bytes") is not None:
                d["total_est"] = d["free_est"] + d["bytes"]
    return out


def _summary(p):
    sc = engine.last_scan(p["id"])
    ev = engine.evaluate(p, sc)
    counts = {}
    for e in ev["slots"].values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {"id": p["id"], "name": p["name"], "host": p["host"], "slots": len(p["slots"]),
            "counts": counts, "order": ev["order"],
            "list": p.get("list"), "busy": engine.busy(p["id"]),
            "scan_at": sc["at"] if sc else None,
            "ftp_ok": bool(sc and sc["ftp"].get("ok")), "deck_ok": bool(sc and sc["deck"].get("ok"))}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # -- outils --------------------------------------------------------------

    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _user(self):
        return unquote(self.headers.get("X-BT-User") or "") or "?"

    def _length(self):
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return 0

    def _json(self):
        n = self._length()
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            raise _err(400, "JSON invalide")

    def _drain(self):
        """Corps non lu (refus avant lecture) : on ferme plutôt que de tout avaler."""
        self.close_connection = True

    def _require_stream(self):
        """Un envoi DOIT arriver en application/octet-stream. Un cœur Bobi.Tools sans le
        relais par morceaux (`_proxy_stream`, commit 134f30d) transforme le fichier en
        corps JSON « {} » : ffprobe lisait 2 octets et l'outil annonçait « fichier
        illisible » pour un clip parfaitement conforme (relevé le 2026-11-15)."""
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct != "application/octet-stream":
            self._drain()
            raise _err(426, "le fichier n'est pas parvenu à l'outil : le cœur de Bobi.Tools est trop "
                            "ancien pour relayer un envoi de fichier. Mettez à jour Bobi.Tools "
                            "(relais des envois, app/routes.py:_proxy_stream), puis redémarrez-le.",
                       reason="core_too_old")

    def _receive(self, dst, limit=None):
        """Écrit le corps sur disque, par blocs. Renvoie le nombre d'octets reçus."""
        n = self._length()
        if limit and n > limit:
            self._drain()
            raise _err(413, "trop volumineux")
        got = 0
        with open(dst, "wb") as fh:
            while got < n:
                chunk = self.rfile.read(min(1 << 20, n - got))
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
        if got != n:
            try:
                os.remove(dst)
            except OSError:
                pass
            raise _err(400, "envoi incomplet (%d / %d octets)" % (got, n))
        return got

    def _route(self, method):
        u = urlsplit(self.path)
        parts = [unquote(x) for x in u.path.strip("/").split("/") if x]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            return self._dispatch(method, parts, q)
        except HTTPError as e:
            return self._send(e.code, e.data)
        except KeyError:
            return self._send(404, {"error": "introuvable"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    # -- routes --------------------------------------------------------------

    def _dispatch(self, m, parts, q):
        P = parts + [None] * 6
        if P[0] == "health":
            return self._send(200, {"ok": True})
        if P[0] == "players" and P[1] is None:
            if m == "GET":
                return self._send(200, {"players": [_summary(p) for p in store.players()]})
            if m == "POST":
                body = self._json()
                p = store.create(body)
                return self._send(200, {"ok": True, "player": store.public(p)})
        if P[0] == "players" and P[1]:
            pid = P[1]
            if P[2] is None:
                if m == "GET":
                    return self._send(200, _view(_player(pid)))
                if m == "PUT":
                    body = self._json()
                    p, _ = store.mutate(pid, lambda pl: store.apply_fields(pl, body))
                    return self._send(200, {"ok": True, "player": store.public(p)})
                if m == "DELETE":
                    if engine.busy(pid):
                        raise _err(409, "un transfert est en cours sur ce lecteur")
                    store.delete(pid)
                    return self._send(200, {"ok": True})
            if m == "POST" and P[2] == "list" and P[3] is None:
                return self._list(pid, self._json())
            if m == "POST" and P[2] == "scan":
                engine.scan(pid)
                return self._send(200, _view(_player(pid)))
            if m == "POST" and P[2] == "fill":
                return self._fill(pid, self._json())
            if m == "POST" and P[2] == "queue" and P[3] in ("resume", "cancel", "clear-failed"):
                _player(pid)
                if P[3] == "resume":
                    engine.resume(pid)
                    return self._send(200, {"ok": True})
                if P[3] == "cancel":
                    return self._send(200, {"ok": True, "cancelled": engine.cancel_queued(pid)})
                engine.clear_failed(pid)
                return self._send(200, {"ok": True})
            if m == "GET" and P[2] == "ph-preview":
                # GET : lecture pure, non journalisée (l'aperçu suit chaque retouche).
                try:
                    body = json.loads(q.get("q") or "{}")
                except ValueError:
                    raise _err(400, "paramètres d'aperçu invalides")
                return self._ph_preview(pid, body)
            if m == "POST" and P[2] == "reset":
                return self._reset(pid, self._json())
            if m == "POST" and P[2] == "test":
                return self._test(pid, self._json())
            if m == "POST" and P[2] == "ftp" and P[3] == "ls":
                body = self._json()
                p = _player(pid)
                tmp = dict(p, **{k: body[k] for k in ("host", "ftp_port", "ftp_user")
                                 if body.get(k) not in (None, "")})
                if body.get("ftp_password") not in (None, "", "__unchanged__"):
                    tmp["ftp_password"] = body["ftp_password"]
                try:
                    return self._send(200, engine.ftp_dirs(tmp, body.get("path") or ""))
                except Exception as e:
                    raise _err(502, str(e))
            if m == "POST" and P[2] == "precheck":
                return self._precheck(pid)
            if m == "POST" and P[2] == "slots" and P[3] and P[4] == "upload":
                return self._upload(pid, P[3], q)
            if m == "POST" and P[2] == "slots" and P[3] and P[4] == "move":
                return self._move(pid, P[3], self._json())
            if m == "POST" and P[2] == "slots" and P[3] and P[4] == "clear":
                p = _player(pid)
                _slot(p, P[3])
                job = engine.enqueue(pid, "placeholder", num=P[3], by=self._user())
                return self._send(200, {"ok": True, "job": job})
            if P[2] == "pending" and P[3]:
                if m == "POST" and P[4] == "fix":
                    return self._fix(pid, P[3])
                if m == "DELETE":
                    x = engine.pending_take(P[3])
                    if x:
                        try:
                            os.remove(x["path"])
                        except OSError:
                            pass
                    return self._send(200, {"ok": True})
            if m == "POST" and P[2] == "remote" and P[3] == "delete":
                return self._delete_remote(pid, self._json())
        if P[0] == "cache" and P[1] == "clear" and m == "POST":
            n = 0
            for f in os.listdir(store.CACHE) if os.path.isdir(store.CACHE) else []:
                try:
                    os.remove(os.path.join(store.CACHE, f))
                    n += 1
                except OSError:
                    pass
            return self._send(200, {"ok": True, "removed": n})
        if P[0] == "jobs" and P[1] and m == "POST":
            fn = {"cancel": engine.cancel, "force": engine.force}.get(P[2])
            if fn:
                j = fn(P[1])
                if not j:
                    raise _err(404, "transfert inconnu")
                return self._send(200, {"ok": True, "job": j})
        raise _err(404, "route inconnue")

    # -- liste attendue (CSV) --------------------------------------------------

    def _list(self, pid, body):
        p = _player(pid)
        try:
            parsed = csvlist.parse(body.get("csv") or "")
            chosen = body.get("player") or None
            if len(parsed["players"]) > 1 and not chosen:
                return self._send(200, {"ok": False, "need_player": True,
                                        "players": parsed["players"],
                                        "counts": {x: sum(1 for r in parsed["rows"] if r["player"] == x)
                                                   for x in parsed["players"]}})
            rows = csvlist.select(parsed, chosen)
        except csvlist.ListError as e:
            raise _err(422, str(e))
        d = csvlist.diff(p["slots"], rows)
        # Fichiers présents sur la machine pour des numéros qui disparaissent : ils
        # resteront sur le HyperDeck (et compteront dans sa liste) jusqu'à suppression.
        sc = engine.last_scan(pid)
        files = (sc or {}).get("ftp", {}).get("files") or []
        orphans = [n for n in files if store.num_of(n) in {r["num"] for r in d["removed"]}]
        preview = {"filename": body.get("filename"), "player": chosen, "count": len(rows),
                   "categories": [{"name": c, "count": sum(1 for r in rows if r["cat"] == c)}
                                  for c in csvlist.categories(rows)],
                   "diff": d, "orphans": orphans, "first": p["slots"] == []}
        if not body.get("apply"):
            return self._send(200, {"ok": True, "preview": preview})

        def fn(pl):
            pl["slots"] = rows
            pl["list"] = {"filename": body.get("filename"), "player": chosen,
                          "imported_at": store.now(), "imported_by": self._user(),
                          "count": len(rows)}
            keep = {r["num"] for r in rows}
            pl["files"] = {k: v for k, v in pl["files"].items() if k in keep}
        store.mutate(pid, fn)
        return self._send(200, {"ok": True, "applied": True, "preview": preview})

    # -- contrôle de format ------------------------------------------------------

    def _space_for(self, n):
        free = shutil.disk_usage(store.DATA_DIR).free
        if n + SPACE_MARGIN > free:
            self._drain()
            raise _err(507, "espace disque insuffisant sur le serveur : %d Mo libres, %d Mo nécessaires"
                       % (free // 2**20, (n + SPACE_MARGIN) // 2**20), reason="no_space")

    def _precheck(self, pid):
        p = _player(pid)
        self._require_stream()
        os.makedirs(store.WORK, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=store.WORK, suffix=".mov")
        os.close(fd)
        try:
            self._receive(path, limit=PRECHECK_MAX)
            try:
                info = media.probe(path)
            except Exception as e:
                return self._send(200, {"ok": False, "readable": False, "error": str(e)})
            s = media.summarize(info)   # une erreur ici = l'outil, pas le fichier (→ 500)
            r = media.check(s, p["format"], partial=True)
            return self._send(200, dict(r, readable=True, summary=s,
                                        expected=media.format_label(p["format"])))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _upload(self, pid, num, q):
        p = _player(pid)
        slot = _slot(p, num)
        original = os.path.basename(q.get("name") or "clip.mov")
        self._require_stream()
        n = self._length()
        if not n:
            raise _err(400, "fichier vide")
        self._space_for(n)
        # Carte du HyperDeck : relue à l'instant, AVANT de recevoir le fichier.
        full = engine.deck_room(p, n)
        if full:
            self._drain()
            raise _err(507, full, reason="deck_full")
        os.makedirs(store.INCOMING, exist_ok=True)
        path = os.path.join(store.INCOMING, "%s.mov" % secrets.token_hex(6))
        self._receive(path)
        # Deux côtés distincts : ffprobe refuse le FICHIER (RuntimeError de media.probe), ou
        # l'OUTIL plante en lisant le résultat — à ne jamais présenter comme un fichier
        # illisible (0.9.0 : un bogue de l'outil refusait tous les dépôts sous ce libellé).
        try:
            info = media.probe(path)
        except Exception as e:
            os.remove(path)
            raise _err(422, "fichier illisible : %s" % e,
                       problems=["fichier illisible par ffprobe (%d octets reçus) : %s" % (n, e)])
        try:
            s = media.summarize(info)
            r = media.check(s, p["format"])
        except Exception as e:
            os.remove(path)
            traceback.print_exc()
            raise _err(500, "erreur de l'outil pendant le contrôle du format : %s" % e,
                       problems=["erreur de l'OUTIL (pas du fichier) pendant le contrôle du format : %s — "
                                 "le fichier n'est pas en cause, signalez-le" % e])
        if not r["ok"]:
            if r["fixable_field_order"]:
                fid = engine.pending_add(pid, num, path, original, self._user(), s, r)
                raise _err(422, "format refusé", problems=r["problems"], fixable=True,
                           pending=fid, summary=s, expected=media.format_label(p["format"]))
            os.remove(path)
            raise _err(422, "format refusé", problems=r["problems"], summary=s,
                       expected=media.format_label(p["format"]))
        job = engine.enqueue(pid, "deliver", num=num, local=path, original=original,
                             remote=store.delivered_name(num, original), by=self._user(),
                             label=s.get("label"), summary=s)
        return self._send(200, {"ok": True, "job": job, "summary": s, "slot": slot["num"]})

    def _fix(self, pid, fid):
        x = engine.pending_take(fid)
        if not x or x["player"] != pid:
            raise _err(404, "fichier en attente introuvable (expiré ?)")
        p = _player(pid)
        size = os.path.getsize(x["path"])
        try:
            self._space_for(size)
        except HTTPError:
            os.remove(x["path"])
            raise
        dst = x["path"] + ".tt.mov"
        try:
            media.fix_field_order(x["path"], dst, "tt")
        finally:
            try:
                os.remove(x["path"])
            except OSError:
                pass
        s = media.summarize(media.probe(dst))
        r = media.check(s, p["format"])
        if not r["ok"]:
            os.remove(dst)
            raise _err(422, "toujours refusé après correction", problems=r["problems"])
        job = engine.enqueue(pid, "deliver", num=x["num"], local=dst, original=x["original"],
                             remote=store.delivered_name(x["num"], x["original"]),
                             by=self._user(), label=s.get("label"),
                             summary=dict(s, header_fixed=True))
        return self._send(200, {"ok": True, "job": job})

    # -- bouche-trous, suppression ----------------------------------------------

    def _fill(self, pid, body=None):
        body = body or {}
        p = _player(pid)
        sc = engine.scan(pid)
        if not sc["ftp"].get("ok"):
            raise _err(502, "FTP injoignable : %s" % sc["ftp"].get("error"))
        queued = {j["num"] for j in engine.jobs(pid) if j["state"] not in ("done", "error", "cancelled")}
        plan = [x for x in engine.fill_plan(p, sc, redo=bool(body.get("redo"))) if x[0] not in queued]
        jobs = [engine.enqueue(pid, kind, num=n, original=old, by=self._user()) for n, kind, old in plan]
        return self._send(200, {"ok": True, "queued": len(jobs),
                                "renamed": sum(1 for x in plan if x[1] == "rename")})

    def _reset(self, pid, body):
        """FIN DE TOURNAGE : chaque emplacement repasse au bouche-trou.

        Toujours en deux temps : sans `confirm`, on renvoie le PLAN (ce qui serait retiré,
        relu sur la machine à l'instant) ; avec `confirm` = nom exact du lecteur, on
        l'exécute. Le même mécanisme d'échange que pour une livraison est utilisé, emplacement
        par emplacement : la liste garde tous ses rangs pendant toute l'opération, et un clip
        en cours de lecture n'est pas retiré sous les yeux de l'antenne.
        L'historique est conservé (une entrée « retiré » par clip).
        """
        p = _player(pid)
        sc = engine.scan(pid)
        if not sc["ftp"].get("ok"):
            raise _err(502, "FTP injoignable : %s" % sc["ftp"].get("error"))
        ev = engine.evaluate(p, sc)
        nums = [s["num"] for s in p["slots"] if ev["slots"][s["num"]]["status"] != "empty"]
        by_status = {}
        for n in nums:
            st = ev["slots"][n]["status"]
            by_status[st] = by_status.get(st, 0) + 1
        clips = [{"num": s["num"], "nom": s["nom"], "files": ev["slots"][s["num"]]["files"],
                  "status": ev["slots"][s["num"]]["status"]} for s in p["slots"] if s["num"] in nums]
        extra = ev["foreign"] + ev["temps"]
        active = [j for j in engine.jobs(pid) if j["state"] not in ("done", "error", "cancelled")]
        plan = {"slots": clips, "by_status": by_status, "foreign": ev["foreign"],
                "temps": ev["temps"], "busy": len(active), "total": len(p["slots"])}
        if body.get("confirm") is None:
            return self._send(200, {"ok": True, "plan": plan})
        if body.get("confirm") != p["name"]:
            raise _err(400, "confirmation incorrecte : tapez exactement le nom du lecteur")
        if active:
            raise _err(409, "%d transfert(s) en cours ou en attente : attendez-les ou annulez-les "
                            "avant la remise à zéro" % len(active))
        jobs = [engine.enqueue(pid, "placeholder", num=n, by=self._user()) for n in nums]
        deleted = 0
        if body.get("foreign"):
            for name in extra:
                engine.enqueue(pid, "delete", remote=name, by=self._user())
                deleted += 1
        return self._send(200, {"ok": True, "queued": len(jobs), "deleted": deleted})

    def _move(self, pid, a, body):
        """Déplacer le clip de l'emplacement `a` vers `body.to` (mode move|replace|swap)."""
        p = _player(pid)
        _slot(p, a)
        b = str(body.get("to") or "")
        _slot(p, b)
        if a == b:
            raise _err(400, "même emplacement")
        mode = body.get("mode") or "move"
        if mode not in ("move", "replace", "swap"):
            raise _err(400, "mode inconnu")
        ra, rb = p["files"].get(a) or {}, p["files"].get(b) or {}
        if ra.get("kind") != "clip":
            raise _err(409, "seul un clip livré par l'outil peut être déplacé")
        if rb.get("kind") == "clip" and mode == "move":
            raise _err(409, "l'emplacement %s porte déjà un clip : choisissez « échanger » ou « remplacer »" % b,
                       reason="target_busy")
        if mode == "swap" and rb.get("kind") != "clip":
            mode = "move"
        busy = [j for j in engine.jobs(pid) if j["num"] in (a, b) and j["state"] not in ("done", "error", "cancelled")]
        if busy:
            raise _err(409, "un transfert est en cours ou en attente sur l'emplacement %s" % busy[0]["num"])
        job = engine.enqueue(pid, "move", num=a, target=b, mode="swap" if mode == "swap" else "move",
                             original=ra.get("original"), by=self._user())
        return self._send(200, {"ok": True, "job": job})

    def _ph_preview(self, pid, body):
        """PNG du bouche-trou d'un emplacement, avec le style ENVOYÉ (non enregistré) :
        l'aperçu suit les réglages pendant qu'on les modifie."""
        p = _player(pid)
        slot = next((s for s in p["slots"] if s["num"] == str(body.get("num") or "")), None) \
            or (p["slots"][0] if p["slots"] else {"num": "104", "nom": "Off 4", "cat": "Thème 1"})
        fmt = dict(p["format"], **(body.get("format") or {}))
        os.makedirs(store.WORK, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=store.WORK, suffix=".png")
        os.close(fd)
        try:
            media.render_preview(slot, fmt, body.get("lecteur") or p["name"],
                                 media.norm_style(body.get("style")), path)
            with open(path, "rb") as fh:
                data = fh.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _delete_remote(self, pid, body):
        name = body.get("name") or ""
        sc = engine.last_scan(pid)
        files = (sc or {}).get("ftp", {}).get("files") or []
        p = _player(pid)
        if name not in files:
            ghosts = ((engine.evaluate(p, sc) if sc else {}).get("order") or {}).get("ghosts") or []
            if name in ghosts:
                if body.get("mode") == "timeline":
                    return self._unlist(p, name)
                job = engine.enqueue(pid, "ghost", num=None, remote=name, by=self._user())
                return self._send(200, {"ok": True, "job": job})
            raise _err(404, "fichier absent du dernier relevé : relancez une vérification")
        num = store.num_of(name)
        if num and any(s["num"] == num for s in p["slots"]):
            ev = engine.evaluate(p, sc)
            if ev["slots"][num]["status"] != "duplicate":
                raise _err(409, "ce fichier occupe l'emplacement %s : utilisez « Retirer » "
                                "(il sera remplacé par un bouche-trou)" % num)
        job = engine.enqueue(pid, "delete", num=None, remote=name, by=self._user())
        return self._send(200, {"ok": True, "job": job})

    def _unlist(self, p, name):
        """Retrait PROVISOIRE de la liste de lecture (TCP 9993), sans toucher au disque :
        remet les rangs d'aplomb tout de suite, jusqu'à ce que le HyperDeck relise sa carte."""
        port = p.get("tcp_port") or deck.PORT
        try:
            ids = [cid for cid, b in deck.raw_clip_names(p["host"], port).items()
                   if engine._nfc(deck.strip_ext(b.decode("utf-8", "replace"))) == engine._nfc(deck.strip_ext(name))]
            if len(ids) != 1:
                raise _err(409, "le HyperDeck ne liste plus ce clip (ou plusieurs fois) : relancez une vérification")
            code, text = deck.remove_from_timeline(p["host"], port, ids[0])
        except deck.DeckError as e:
            raise _err(502, "HyperDeck (TCP 9993) : %s" % e)
        if code >= 300:
            raise _err(502, "le HyperDeck refuse de retirer le clip de sa liste : %d %s" % (code, text))
        engine._changed(p["id"])
        return self._send(200, {"ok": True, "unlisted": ids[0]})

    def _test(self, pid, body):
        import deck
        p = _player(pid)
        out = {}
        try:
            ftp = engine.ftp_open(p)
            try:
                out["ftp"] = {"ok": True, "files": len(engine.ftp_names(ftp)), "cwd": ftp.pwd()}
            finally:
                engine._close(ftp)
        except Exception as e:
            out["ftp"] = {"ok": False, "error": str(e)}
        if out["ftp"].get("ok"):
            out["rename"] = self._test_rename(p)
        try:
            st = deck.read(p["host"], p.get("tcp_port") or deck.PORT)
            out["deck"] = {"ok": True, "model": st.get("model"), "clips": st.get("clips_count"),
                           "protocol": st.get("protocol")}
        except Exception as e:
            out["deck"] = {"ok": False, "error": str(e)}
        return self._send(200, out)

    @staticmethod
    def _test_rename(p):
        """Renommage d'essai sur un petit fichier TEXTE (le HyperDeck ne liste que les clips :
        la timeline n'est pas touchée). Rend le journal brut de l'échange FTP : c'est lui qui
        dit si la machine répond dans l'ordre."""
        import io
        a, b = store.TEMP_PREFIX + "test_renommage.txt", store.TEMP_PREFIX + "test_renommage_ok.txt"
        res = {"ok": False}
        ftp = None
        try:
            ftp = engine.ftp_open(p)
            ftp.storbinary("STOR " + a, io.BytesIO(b"test Bobi.Tools\n"))
            try:
                engine.ftp_rename(ftp, a, b)
            except engine.RenameUnsupported as e:
                engine._no_rename[p["id"]] = e.reply
                res["unsupported"] = True
                raise
            names = engine.ftp_names(ftp)
            res["ok"] = b in names and a not in names
            if res["ok"]:
                engine._no_rename.pop(p["id"], None)     # mise à jour du HyperDeck : on réessaiera
            if not res["ok"]:
                res["error"] = "renommage annoncé réussi mais non constaté sur le disque"
        except Exception as e:
            res["error"] = str(e)
        finally:
            if ftp is not None:
                for n in (a, b):
                    try:
                        ftp.sendcmd("DELE " + n)
                    except Exception:
                        pass
                res["journal"] = list(ftp.journal)
                res["stray"] = len(ftp.stray)
                engine._close(ftp)
        return res


def main():
    engine.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("hyperdeck_clips à l'écoute sur :%d" % PORT, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
