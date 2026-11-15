# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Transferts FTP vers les HyperDeck, et vérification de la liste.

RÈGLE CARDINALE : à tout instant, le HyperDeck doit voir LE MÊME NOMBRE de clips, dans le
MÊME ORDRE. L'exploitant rappelle un clip par son rang ; un clip en trop ou en moins
décale tous ceux qui suivent.

Échange d'un fichier (dépôt, remplacement V2, bouche-trou) :
  1. envoi sous un nom TEMPORAIRE qui se classe après tout le reste (`zzz_envoi_…`) : le
     clip en plus, s'il apparaît, est en fin de liste, il ne décale personne ;
  2. contrôle « à l'antenne » : si le HyperDeck lit le fichier qu'on va retirer, on attend ;
  3. suppression de l'ancien, puis RENOMMAGE du temporaire sous le nom définitif. Seul
     trou dans la liste : les quelques millisecondes entre les deux commandes FTP.
  4. si le renommage est refusé (non vérifié sur machine réelle), repli : on envoie le
     fichier directement sous son nom définitif, puis on supprime le temporaire.

Un seul transfert à la fois PAR HYPERDECK (une file et un fil par lecteur) : deux envois
sur la même machine se partageraient son lien et son disque sans rien gagner.
"""
import ftplib
import os
import queue
import secrets
import threading
import time
import traceback

import deck
import media
import store

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "60") or 0)
FTP_TIMEOUT = float(os.environ.get("FTP_TIMEOUT", "20") or 20)
AIR_WAIT_MAX = 600          # au-delà, l'envoi est mis en échec : on ne garde pas un échange en suspens 1 h

PRIORITY = {"deliver": 0, "delete": 1, "placeholder": 2}

_jobs = {}                  # id → job (dict)
_queues = {}                # pid → queue.Queue
_threads = {}
_scans = {}                 # pid → dernier résultat de vérification
_listing = {}               # pid → (time, [noms]) dernier listing FTP connu
_pending = {}               # fid → fichier refusé mais corrigeable, en attente de décision
_glock = threading.RLock()


# ── FTP ─────────────────────────────────────────────────────────────────────

def ftp_open(p, cwd=True):
    if not p.get("host"):
        raise RuntimeError("adresse du HyperDeck non renseignée")
    ftp = ftplib.FTP(timeout=FTP_TIMEOUT, encoding="utf-8")
    try:
        ftp.connect(p["host"], int(p.get("ftp_port") or 21))
        ftp.login(p.get("ftp_user") or "anonymous", p.get("ftp_password") or "")
        try:
            ftp.sendcmd("OPTS UTF8 ON")
        except ftplib.all_errors:
            pass
        ftp.voidcmd("TYPE I")
        if cwd and p.get("ftp_dir"):
            ftp.cwd("/" + p["ftp_dir"])
    except ftplib.all_errors as e:
        _close(ftp)
        raise RuntimeError("FTP %s : %s" % (p["host"], e))
    return ftp


def _close(ftp):
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def ftp_names(ftp):
    """Noms des FICHIERS du dossier courant (MLSD si gérée, sinon NLST)."""
    try:
        return sorted(n for n, f in ftp.mlsd(facts=["type"])
                      if f.get("type", "file") == "file")
    except ftplib.all_errors:
        out = []
        for n in ftp.nlst():
            n = n.rsplit("/", 1)[-1]
            if n not in (".", ".."):
                out.append(n)
        return sorted(out)


def ftp_dirs(p, path):
    """Sous-dossiers d'un chemin (aide au choix du disque : le HyperDeck présente un
    dossier par support à la racine de son FTP)."""
    ftp = ftp_open(p, cwd=False)
    try:
        ftp.cwd("/" + (path or "").strip("/"))
        dirs, files = [], 0
        try:
            for n, f in ftp.mlsd(facts=["type"]):
                if f.get("type") == "dir":
                    dirs.append(n)
                elif f.get("type", "file") == "file":
                    files += 1
        except ftplib.all_errors:
            for n in ftp.nlst():
                n = n.rsplit("/", 1)[-1]
                try:
                    ftp.cwd(n)
                    dirs.append(n)
                    ftp.cwd("..")
                except ftplib.all_errors:
                    files += 1
        return {"path": (path or "").strip("/"), "dirs": sorted(dirs), "files": files}
    finally:
        _close(ftp)


# ── File de transferts ──────────────────────────────────────────────────────

def _q(pid):
    with _glock:
        if pid not in _queues:
            _queues[pid] = queue.PriorityQueue()
            t = threading.Thread(target=_worker, args=(pid,), daemon=True, name="ftp-" + pid)
            _threads[pid] = t
            t.start()
        return _queues[pid]


def enqueue(pid, kind, num=None, local=None, original=None, remote=None, by="", label=None,
            summary=None, size=None):
    job = {"id": secrets.token_hex(5), "player": pid, "kind": kind, "num": num,
           "local": local, "original": original, "remote": remote, "by": by,
           "label": label, "summary": summary, "size": size or (os.path.getsize(local) if local and os.path.exists(local) else None),
           "sent": 0, "state": "queued", "detail": "", "error": None, "log": [],
           "created": time.time(), "started": None, "ended": None,
           "cancel": False, "force": False}
    with _glock:
        _jobs[job["id"]] = job
        _prune()
    # Un vrai clip passe AVANT les bouche-trous en attente : combler 91 emplacements ne doit
    # pas faire patienter la livraison qu'on attend pour l'antenne. FIFO à priorité égale.
    _q(pid).put((PRIORITY.get(kind, 5), job["created"], job["id"]))
    return public_job(job)


def _prune():
    done = [j for j in _jobs.values() if j["state"] in ("done", "error", "cancelled")]
    done.sort(key=lambda j: j["ended"] or 0)
    for j in done[:-150]:
        _jobs.pop(j["id"], None)


def public_job(j):
    return {k: v for k, v in j.items() if k not in ("local", "cancel", "force")}


def jobs(pid=None):
    with _glock:
        js = [public_job(j) for j in _jobs.values() if pid is None or j["player"] == pid]
    return sorted(js, key=lambda j: j["created"])


def busy(pid):
    with _glock:
        return any(j["player"] == pid and j["state"] not in ("done", "error", "cancelled", "queued")
                   for j in _jobs.values())


def cancel(jid):
    with _glock:
        j = _jobs.get(jid)
        if not j:
            return None
        if j["state"] == "queued":
            j["state"], j["ended"] = "cancelled", time.time()
            _discard_local(j)
        elif j["state"] in ("rendering", "sending", "waiting_air", "blocked"):
            j["cancel"] = True          # le fil s'arrête au prochain point de contrôle
        return public_job(j)


def force(jid):
    with _glock:
        j = _jobs.get(jid)
        if j and j["state"] in ("waiting_air", "blocked"):
            j["force"] = True
        return public_job(j) if j else None


def _log(j, msg):
    j["log"].append("%s %s" % (time.strftime("%H:%M:%S"), msg))
    j["detail"] = msg


def _discard_local(j):
    if j.get("local") and j["kind"] != "delete":
        try:
            os.remove(j["local"])
        except OSError:
            pass


class Cancelled(Exception):
    pass


def _worker(pid):
    q = _queues[pid]
    while True:
        _, _, jid = q.get()
        with _glock:
            j = _jobs.get(jid)
        if not j or j["state"] != "queued":
            continue
        j["state"], j["started"] = "sending", time.time()
        try:
            p = store.get(pid)
            if not p:
                raise RuntimeError("lecteur supprimé")
            if j["kind"] == "placeholder":
                _render(j, p)
            if j["kind"] == "delete":
                _delete_remote(j, p)
            else:
                _swap(j, p)
            j["state"] = "done"
            _log(j, "terminé")
        except Cancelled:
            j["state"] = "cancelled"
            _log(j, "annulé")
        except Exception as e:
            j["state"], j["error"] = "error", str(e)
            _log(j, "échec : %s" % e)
            print("[job %s] %s" % (jid, traceback.format_exc()), flush=True)
        finally:
            j["ended"] = time.time()
            _discard_local(j)
            threading.Thread(target=_safe_scan, args=(pid,), daemon=True).start()


def _render(j, p):
    slot = next((s for s in p["slots"] if s["num"] == j["num"]), None)
    if not slot:
        raise RuntimeError("emplacement %s absent de la liste" % j["num"])
    j["state"] = "rendering"
    _log(j, "fabrication du bouche-trou")
    os.makedirs(store.WORK, exist_ok=True)
    dst = os.path.join(store.WORK, "ph_%s.mov" % j["id"])
    media.render_placeholder(slot, p["format"], p["placeholder_duration"],
                             p["audio_channels"], p["name"], dst)
    j["local"] = dst
    j["size"] = os.path.getsize(dst)
    j["remote"] = store.placeholder_name(slot)
    j["label"] = media.format_label(p["format"])
    j["state"] = "sending"


def _check_cancel(j):
    if j["cancel"]:
        raise Cancelled()


def _wait_air(j, p, names):
    """Attend que le HyperDeck ne lise plus aucun des fichiers qu'on va retirer."""
    if not names or not p.get("protect_on_air", True):
        return
    t0 = time.time()
    while True:
        _check_cancel(j)
        if j["force"]:
            _log(j, "contrôle d'antenne levé par l'exploitant")
            return
        try:
            st = deck.read(p["host"], p.get("tcp_port") or deck.PORT)
            playing = deck.on_air(st, names)
            if not playing:
                return
            j["state"] = "waiting_air"
            _log(j, "en attente : « %s » est en cours de lecture" % playing)
        except deck.DeckError as e:
            j["state"] = "blocked"
            _log(j, "en attente : impossible de vérifier ce que lit le HyperDeck (%s)" % e)
        if time.time() - t0 > AIR_WAIT_MAX:
            raise RuntimeError("abandon : clip toujours à l'antenne (ou HyperDeck muet) après %d min"
                               % (AIR_WAIT_MAX // 60))
        time.sleep(2)


def _swap(j, p):
    num, final, local = j["num"], j["remote"], j["local"]
    ext = os.path.splitext(final)[1] or ".mov"
    temp = "%s%s%s" % (store.TEMP_PREFIX, j["id"], ext)

    ftp = ftp_open(p)
    try:
        _check_cancel(j)
        j["state"] = "sending"
        _log(j, "envoi vers %s sous %s" % (p["host"], temp))

        def cb(chunk):
            j["sent"] += len(chunk)
            if j["cancel"]:
                raise Cancelled()
        try:
            with open(local, "rb") as fh:
                ftp.storbinary("STOR " + temp, fh, blocksize=1 << 20, callback=cb)
        except Cancelled:
            _close(ftp)
            _cleanup_temp(p, temp)
            raise
        names = ftp_names(ftp)
        if temp not in names:
            raise RuntimeError("le fichier envoyé n'apparaît pas sur le HyperDeck")
    finally:
        _close(ftp)

    olds = [n for n in names if store.num_of(n) == num and n != temp and store.is_media(n)]
    j["state"] = "swapping"
    try:
        _wait_air(j, p, olds)
    except Cancelled:
        _cleanup_temp(p, temp)
        raise
    _log(j, "échange : %s → %s" % (" + ".join(olds) or "(rien)", final))
    ftp = ftp_open(p)
    try:
        for o in olds:
            ftp.delete(o)
        try:
            ftp.rename(temp, final)
        except ftplib.all_errors as e:
            _log(j, "renommage refusé (%s) : envoi direct sous le nom définitif" % e)
            j["sent"] = 0
            with open(local, "rb") as fh:
                ftp.storbinary("STOR " + final, fh, blocksize=1 << 20,
                               callback=lambda c: j.__setitem__("sent", j["sent"] + len(c)))
            try:
                ftp.delete(temp)
            except ftplib.all_errors:
                pass
        names = ftp_names(ftp)
    finally:
        _close(ftp)
    _listing[p["id"]] = (time.time(), names)
    mine = [n for n in names if store.num_of(n) == num and store.is_media(n)]
    if mine != [final]:
        raise RuntimeError("après échange, l'emplacement %s porte : %s" % (num, ", ".join(mine) or "rien"))
    _record(j, p, olds)


def _cleanup_temp(p, temp):
    try:
        ftp = ftp_open(p)
        try:
            ftp.delete(temp)
        finally:
            _close(ftp)
    except Exception:
        pass


def _record(j, p, olds):
    num = j["num"]

    def fn(pl):
        hist = pl["history"].setdefault(num, [])
        prev = pl["files"].get(num)
        entry = {"at": store.now(), "by": j["by"], "remote": j["remote"], "size": j["size"],
                 "label": j["label"], "replaced": olds}
        if j["kind"] == "placeholder":
            entry.update(action="retiré" if prev and prev.get("kind") == "clip" else "bouche-trou")
            pl["files"][num] = {"kind": "placeholder", "remote": j["remote"], "at": entry["at"],
                                "by": j["by"], "size": j["size"], "label": j["label"]}
        else:
            v = max([h.get("v") or 0 for h in hist] + [0]) + 1
            entry.update(action="remplacé" if v > 1 else "livré", v=v, original=j["original"],
                         summary=j.get("summary"))
            pl["files"][num] = {"kind": "clip", "remote": j["remote"], "original": j["original"],
                                "v": v, "at": entry["at"], "by": j["by"], "size": j["size"],
                                "label": j["label"], "summary": j.get("summary")}
        hist.append(entry)
    store.mutate(p["id"], fn)


def _delete_remote(j, p):
    name = j["remote"]
    _wait_air(j, p, [name])
    ftp = ftp_open(p)
    try:
        _log(j, "suppression de %s" % name)
        ftp.delete(name)
        _listing[p["id"]] = (time.time(), ftp_names(ftp))
    finally:
        _close(ftp)


# ── Vérification ────────────────────────────────────────────────────────────

def _safe_scan(pid):
    try:
        scan(pid)
    except Exception:
        traceback.print_exc()


def scan(pid):
    """Relit le HyperDeck : fichiers (FTP) et timeline (TCP 9993)."""
    p = store.get(pid)
    if not p:
        raise KeyError(pid)
    res = {"at": store.now(), "ftp": {"ok": False}, "deck": {"ok": False}}
    if busy(pid) and pid in _listing:
        # Un transfert occupe la machine : on ne lui ouvre pas une seconde session FTP,
        # on reprend le dernier listing connu (signalé comme tel).
        res["ftp"] = {"ok": True, "files": _listing[pid][1], "cached": True}
    else:
        try:
            ftp = ftp_open(p)
            try:
                names = ftp_names(ftp)
            finally:
                _close(ftp)
            _listing[pid] = (time.time(), names)
            res["ftp"] = {"ok": True, "files": names}
        except Exception as e:
            res["ftp"] = {"ok": False, "error": str(e)}
    try:
        st = deck.read(p["host"], p.get("tcp_port") or deck.PORT)
        res["deck"] = {"ok": True, **st}
    except Exception as e:
        res["deck"] = {"ok": False, "error": str(e)}
    _scans[pid] = res
    return res


def last_scan(pid):
    return _scans.get(pid)


def evaluate(p, sc):
    """État de chaque emplacement + anomalies globales, à partir d'une vérification."""
    slots = p["slots"]
    out = {"slots": {}, "foreign": [], "temps": [], "others": [], "order": None}
    if not sc or not sc["ftp"].get("ok"):
        for s in slots:
            out["slots"][s["num"]] = {"status": "unknown", "files": []}
    else:
        files = sc["ftp"]["files"]
        nums = {s["num"] for s in slots}
        by_num = {}
        for n in files:
            if not store.is_media(n):
                if not n.startswith("."):
                    out["others"].append(n)
                continue
            if n.startswith(store.TEMP_PREFIX):
                out["temps"].append(n)
                continue
            k = store.num_of(n)
            if k in nums:
                by_num.setdefault(k, []).append(n)
            else:
                out["foreign"].append(n)
        for s in slots:
            names = by_num.get(s["num"], [])
            rec = p["files"].get(s["num"])
            if not names:
                st = "missing"
            elif len(names) > 1:
                st = "duplicate"
            else:
                n = names[0]
                if n == store.placeholder_name(s):
                    st = "empty"
                elif store.is_placeholder(n, s["num"]):
                    st = "stale_placeholder"
                elif rec and rec.get("kind") == "clip" and n == rec.get("remote"):
                    st = "delivered"
                else:
                    st = "foreign_file"
            out["slots"][s["num"]] = {"status": st, "files": names}

    d = sc.get("deck") if sc else None
    if d and d.get("ok"):
        clips = d.get("clips") or []
        rank = {}
        for c in clips:
            rank.setdefault(deck.strip_ext(c["name"]), c["id"])
        bad = 0
        for i, s in enumerate(slots, start=1):
            e = out["slots"][s["num"]]
            e["rank_expected"] = i
            if len(e["files"]) == 1:
                e["rank"] = rank.get(deck.strip_ext(e["files"][0]))
                if e["rank"] != i:
                    bad += 1
            else:
                e["rank"] = None
                bad += 1
        known = {deck.strip_ext(f) for e in out["slots"].values() for f in e["files"]}
        extra = [c["name"] for c in clips if deck.strip_ext(c["name"]) not in known]
        count = d.get("clips_count") if d.get("clips_count") is not None else len(clips)
        tr = d.get("transport") or {}
        out["order"] = {"ok": bad == 0 and count == len(slots) and not extra,
                        "expected": len(slots), "count": count, "misplaced": bad,
                        "extra": extra, "status": tr.get("status"), "clip_id": tr.get("clip id"),
                        "slot_id": tr.get("slot id")}
    return out


def fill_plan(p, sc):
    """Emplacements à combler (absents ou bouche-trou périmé) d'après une vérification."""
    ev = evaluate(p, sc)
    return [s["num"] for s in p["slots"]
            if ev["slots"][s["num"]]["status"] in ("missing", "stale_placeholder")]


# ── Fichiers refusés mais corrigeables ──────────────────────────────────────

def pending_add(pid, num, path, original, by, summary, result):
    fid = secrets.token_hex(5)
    with _glock:
        _pending[fid] = {"id": fid, "player": pid, "num": num, "path": path,
                         "original": original, "by": by, "summary": summary,
                         "check": result, "at": time.time()}
    return fid


def pending_take(fid):
    with _glock:
        return _pending.pop(fid, None)


def pending_list(pid):
    with _glock:
        return [{k: v for k, v in x.items() if k != "path"} for x in _pending.values()
                if x["player"] == pid]


def _janitor():
    """Ménage : fichiers refusés oubliés (30 min), restes d'envois interrompus."""
    while True:
        time.sleep(60)
        now = time.time()
        with _glock:
            old = [f for f, x in _pending.items() if now - x["at"] > 1800]
            gone = [_pending.pop(f) for f in old]
        for x in gone:
            try:
                os.remove(x["path"])
            except OSError:
                pass


def _scanner():
    while SCAN_INTERVAL > 0:
        time.sleep(SCAN_INTERVAL)
        for p in store.players():
            if p.get("host") and not busy(p["id"]):
                _safe_scan(p["id"])


def start():
    for d in (store.INCOMING, store.WORK):
        os.makedirs(d, exist_ok=True)
        for n in os.listdir(d):         # restes d'avant redémarrage : aucune file ne les porte
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
    threading.Thread(target=_janitor, daemon=True).start()
    threading.Thread(target=_scanner, daemon=True).start()
    for p in store.players():
        if p.get("host"):
            threading.Thread(target=_safe_scan, args=(p["id"],), daemon=True).start()
