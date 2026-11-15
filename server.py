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
                          placeholder_name=store.placeholder_name(s),
                          job=active.get(s["num"])))
    scan = None
    if sc:
        scan = {"at": sc["at"],
                "ftp": {k: v for k, v in sc["ftp"].items() if k != "files"},
                "deck": {k: v for k, v in sc["deck"].items() if k not in ("clips",)},
                "files_count": len(sc["ftp"].get("files") or [])}
    return {"player": store.public(p), "slots": slots,
            "categories": csvlist.categories(p["slots"]),
            "format_label": media.format_label(p["format"]),
            "scan": scan, "anomalies": {k: ev[k] for k in ("foreign", "temps", "others")},
            "order": ev["order"], "jobs": [j for j in js if j["state"] != "done"][-30:],
            "done": [j for j in js if j["state"] == "done"][-10:],
            "pending": engine.pending_list(p["id"])}


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
                return self._fill(pid)
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
                       % (free // 2**20, (n + SPACE_MARGIN) // 2**20), code="no_space")

    def _precheck(self, pid):
        p = _player(pid)
        os.makedirs(store.WORK, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=store.WORK, suffix=".mov")
        os.close(fd)
        try:
            self._receive(path, limit=PRECHECK_MAX)
            try:
                s = media.summarize(media.probe(path))
            except Exception as e:
                return self._send(200, {"ok": False, "readable": False, "error": str(e)})
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
        n = self._length()
        if not n:
            raise _err(400, "fichier vide")
        self._space_for(n)
        os.makedirs(store.INCOMING, exist_ok=True)
        path = os.path.join(store.INCOMING, "%s.mov" % secrets.token_hex(6))
        self._receive(path)
        try:
            s = media.summarize(media.probe(path))
        except Exception as e:
            os.remove(path)
            raise _err(422, "fichier illisible : %s" % e, problems=["fichier illisible"])
        r = media.check(s, p["format"])
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

    def _fill(self, pid):
        p = _player(pid)
        sc = engine.scan(pid)
        if not sc["ftp"].get("ok"):
            raise _err(502, "FTP injoignable : %s" % sc["ftp"].get("error"))
        queued = {j["num"] for j in engine.jobs(pid) if j["state"] not in ("done", "error", "cancelled")}
        nums = [n for n in engine.fill_plan(p, sc) if n not in queued]
        jobs = [engine.enqueue(pid, "placeholder", num=n, by=self._user()) for n in nums]
        return self._send(200, {"ok": True, "queued": len(jobs)})

    def _delete_remote(self, pid, body):
        name = body.get("name") or ""
        sc = engine.last_scan(pid)
        files = (sc or {}).get("ftp", {}).get("files") or []
        if name not in files:
            raise _err(404, "fichier absent du dernier relevé : relancez une vérification")
        p = _player(pid)
        num = store.num_of(name)
        if num and any(s["num"] == num for s in p["slots"]):
            ev = engine.evaluate(p, sc)
            if ev["slots"][num]["status"] != "duplicate":
                raise _err(409, "ce fichier occupe l'emplacement %s : utilisez « Retirer » "
                                "(il sera remplacé par un bouche-trou)" % num)
        job = engine.enqueue(pid, "delete", num=None, remote=name, by=self._user())
        return self._send(200, {"ok": True, "job": job})

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
        try:
            st = deck.read(p["host"], p.get("tcp_port") or deck.PORT)
            out["deck"] = {"ok": True, "model": st.get("model"), "clips": st.get("clips_count"),
                           "protocol": st.get("protocol")}
        except Exception as e:
            out["deck"] = {"ok": False, "error": str(e)}
        return self._send(200, out)


def main():
    engine.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("hyperdeck_clips à l'écoute sur :%d" % PORT, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
