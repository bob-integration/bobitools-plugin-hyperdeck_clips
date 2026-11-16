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
import collections
import ftplib
import os
import queue
import re
import shutil
import secrets
import select
import threading
import time
import unicodedata
import traceback

import deck
import media
import store

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "60") or 0)
FTP_TIMEOUT = float(os.environ.get("FTP_TIMEOUT", "20") or 20)
CACHE_MIN_FREE = 2 * 1024 ** 3   # en dessous, on n'ajoute plus rien au cache des bouche-trous
AIR_WAIT_MAX = 600          # au-delà, l'envoi est mis en échec : on ne garde pas un échange en suspens 1 h

PRIORITY = {"deliver": 0, "delete": 1, "move": 1, "rename": 2, "placeholder": 3}

_jobs = {}                  # id → job (dict)
_queues = {}                # pid → queue.Queue
_threads = {}
_scans = {}                 # pid → dernier résultat de vérification
_listing = {}               # pid → (time, [noms]) dernier listing FTP connu
_pending = {}               # fid → fichier refusé mais corrigeable, en attente de décision
_glock = threading.RLock()


# ── FTP ─────────────────────────────────────────────────────────────────────

class HDFtp(ftplib.FTP):
    """Client FTP qui SE RECALE sur les réponses du HyperDeck et garde un JOURNAL brut.

    Relevé le 2026-11-15 sur machine réelle : un renommage reçoit « 200 » au RNFR puis
    « 350 Requested file action pending » au RNTO. 350 est la réponse NORMALE au RNFR :
    les réponses arrivent décalées d'un cran (une réponse en trop, venue plus tôt dans la
    session, est lue à la place de la bonne). ftplib lit une réponse par commande et ne
    s'en remet jamais. Ici, avant chaque commande, toute réponse déjà arrivée et non
    réclamée est lue, notée au journal comme « en trop » et écartée.
    Le journal (commandes envoyées, réponses lues, mot de passe masqué) accompagne les
    erreurs : il dit ce que la machine a réellement répondu."""

    DRAIN_WAIT = 0.08      # s : délai laissé à une réponse en trop pour arriver

    def __init__(self, *a, **kw):
        self._rbuf = b""
        self.journal = collections.deque(maxlen=60)
        self.stray = []
        super().__init__(*a, **kw)

    def getline(self):
        while b"\n" not in self._rbuf:
            if len(self._rbuf) > self.maxline:
                raise ftplib.Error("réponse FTP trop longue")
            chunk = self.sock.recv(8192)
            if not chunk:
                raise EOFError
            self._rbuf += chunk
        raw, _, self._rbuf = self._rbuf.partition(b"\n")
        line = raw.decode(self.encoding or "utf-8", "replace").rstrip("\r")
        self.journal.append("< " + line)
        return line

    def _pending(self, wait):
        if b"\n" in self._rbuf:
            return True
        if self.sock is None:
            return False
        try:
            return bool(select.select([self.sock], [], [], wait)[0])
        except (OSError, ValueError):
            return False

    def drain(self, wait=None, expected=False):
        """Lit et écarte les réponses arrivées sans avoir été demandées. Renvoie leur liste.
        `expected` : réponses supplémentaires ATTENDUES (2ᵉ réponse du RNFR) — pas « en trop »."""
        got = []
        wait = self.DRAIN_WAIT if wait is None else wait
        while self._pending(wait):
            try:
                resp = self.getmultiline()
            except (OSError, EOFError):
                break
            got.append(resp)
            if not expected:
                self.journal.append("  (réponse en trop : ignorée)")
            wait = self.DRAIN_WAIT
        if not expected:
            self.stray.extend(got)
        return got

    def putcmd(self, line):
        if self.sock is not None:
            self.drain()
        self.journal.append("> " + ("PASS ****" if line[:5].upper() == "PASS " else line))
        super().putcmd(line)


def ftp_journal(ftp, n=16):
    """Dernières lignes échangées avec le HyperDeck (pour le journal d'une opération)."""
    return list(getattr(ftp, "journal", []))[-n:]


def ftp_open(p, cwd=True):
    if not p.get("host"):
        raise RuntimeError("adresse du HyperDeck non renseignée")
    ftp = HDFtp(timeout=FTP_TIMEOUT, encoding="utf-8")
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


def ftp_names(ftp, sizes=None):
    """Noms des FICHIERS du dossier courant (MLSD si gérée, sinon NLST). Si `sizes` est un
    dict, il reçoit {nom: octets} quand le serveur donne la taille (MLSD seulement)."""
    try:
        out = []
        for n, f in ftp.mlsd(facts=["type", "size"]):
            if f.get("type", "file") == "file":
                out.append(n)
                if sizes is not None and str(f.get("size", "")).isdigit():
                    sizes[n] = int(f["size"])
        return sorted(out)
    except ftplib.all_errors:
        out = []
        for n in ftp.nlst():
            n = n.rsplit("/", 1)[-1]
            if n not in (".", ".."):
                out.append(n)
        if sizes is not None:
            _list_sizes(ftp, out, sizes)
        return sorted(out)


_LIST_UNIX = re.compile(r"^[-dl][rwxsStT-]{9}\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\w{3}\s+\d{1,2}\s+[\d:]{4,5}\s(.+)$")
_LIST_DOS = re.compile(r"^\d{2}-\d{2}-\d{2,4}\s+\d{1,2}:\d{2}[AP]M\s+(\d+)\s+(.+)$", re.IGNORECASE)


def _list_sizes(ftp, names, sizes):
    """Tailles des fichiers par LIST (format « ls -l » ou DOS), pour un serveur sans MLSD
    (le HyperDeck). Les NOMS restent ceux de NLST : LIST ne sert qu'à lire les tailles, et
    une ligne qu'on ne sait pas lire est ignorée plutôt que devinée."""
    wanted = set(names)
    lines = []
    try:
        ftp.retrlines("LIST", lines.append)
    except ftplib.all_errors:
        return
    for l in lines:
        m = _LIST_UNIX.match(l) or _LIST_DOS.match(l)
        if m and m.group(2) in wanted:
            sizes[m.group(2)] = int(m.group(1))


class FtpStepError(ftplib.error_perm):
    """Refus d'une commande FTP, QUALIFIÉ : quelle commande, sur quel fichier, réponse
    exacte du HyperDeck, et de quel côté vient le problème (`side`) :
      - "missing"  : le fichier n'existe pas (ou plus) sous ce nom sur le disque ;
      - "hyperdeck": le fichier est là, mais la machine refuse l'opération.
    Hérite de error_perm : les replis existants (`except ftplib.all_errors`) s'appliquent."""

    def __init__(self, cmd, name, reply, side, text):
        super().__init__(text)
        self.cmd, self.name, self.reply, self.side, self.text = cmd, name, reply, side, text


RETRY_DELAYS = (1, 3, 6)    # refus d'un fichier PRÉSENT : peut-être passager (relecture du disque)


def _ftp_step(ftp, cmd, name, fn):
    last = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except (ftplib.error_perm, ftplib.error_temp, ftplib.error_reply) as e:
            last = str(e).strip()
            try:
                present = name in ftp_names(ftp)
            except ftplib.all_errors:
                present = None
            if present is False:
                raise FtpStepError(cmd, name, last, "missing",
                                   "%s « %s » : le fichier n'existe pas sous ce nom sur le HyperDeck "
                                   "(réponse : %s)" % (cmd, name, last))
    raise FtpStepError(cmd, name, last, "hyperdeck",
                       "%s « %s » refusé par le HyperDeck, %d essais sur %d s (réponse : %s). Le "
                       "fichier est bien sur le disque : il est peut-être chargé ou en lecture, "
                       "ou le disque est en cours de relecture." % (cmd, name, len(RETRY_DELAYS) + 1,
                                                                     sum(RETRY_DELAYS), last))


class RenameUnsupported(FtpStepError):
    """Le HyperDeck refuse le RNFR lui-même : il ne renomme pas par FTP."""


# Lecteurs dont le FTP ne renomme pas (relevé en marche, oublié au redémarrage du conteneur
# pour qu'une mise à jour du HyperDeck soit reprise en compte).
_no_rename = {}


def ftp_rename(ftp, src, dst, pid=None):
    """RNFR/RNTO, en lisant TOUTES les réponses du RNFR.

    Relevé le 2026-11-15 sur HyperDeck Studio HD Pro (« Tester la connexion ») : le RNFR
    reçoit DEUX réponses, « 350 Requested file action pending » puis aussitôt « 550
    Requested action not taken. File unavailable », pour un fichier pourtant présent (le
    DELE qui suit réussit). ftplib ne lisait que la première : le RNTO récoltait alors le
    550 ou un « 503 Bad sequence » — d'où les « 200 au RNFR », « 350 au RNTO » vus avant.
    Ici : après le RNFR, on laisse arriver la suite (1 s). Un refus (4xx/5xx) → pas de
    RNTO ; un essai avec le chemin ABSOLU (certains serveurs embarqués le veulent) ; s'il
    est refusé aussi → RenameUnsupported, sans répétition (le refus est systématique)."""
    if pid and _no_rename.get(pid):
        raise RenameUnsupported("Renommage", src, _no_rename[pid], "hyperdeck",
                                "le HyperDeck ne renomme pas par FTP (réponse : %s)" % _no_rename[pid])

    def rnfr(name):
        replies = [ftp.sendcmd("RNFR " + name)]       # lève sur un 4xx/5xx en 1re réponse
        if hasattr(ftp, "drain"):
            replies += ftp.drain(1.0, expected=True)
        bad = next((r for r in replies if r[:1] in "45"), None)
        return bad

    bad = None
    try:
        bad = rnfr(src)
    except (ftplib.error_perm, ftplib.error_temp) as e:
        bad = str(e).strip()
    if bad:
        try:
            cwd = ftp.pwd()
        except ftplib.all_errors:
            cwd = None
        if cwd and not src.startswith("/"):
            absolute = cwd.rstrip("/") + "/" + src
            try:
                bad2 = rnfr(absolute)
            except (ftplib.error_perm, ftplib.error_temp) as e:
                bad2 = str(e).strip()
            if not bad2:
                src, bad = absolute, None
                dst = cwd.rstrip("/") + "/" + dst if not dst.startswith("/") else dst
    if bad:
        if pid:
            _no_rename[pid] = bad
        raise RenameUnsupported("Renommage", src, bad, "hyperdeck",
                                "le HyperDeck refuse de renommer « %s » (réponse au RNFR : %s)" % (src, bad))
    r2 = ftp.sendcmd("RNTO " + dst)
    if not r2.startswith("2"):
        raise FtpStepError("Renommage", src, r2, "hyperdeck",
                           "renommage « %s » → « %s » refusé (réponse au RNTO : %s)" % (src, dst, r2))
    return r2


def ftp_delete(ftp, name):
    """DELE tolérant : `ftplib.delete` n'accepte que 250/200, et le FTP du HyperDeck a déjà
    montré des codes hors norme (200 au RNFR). Tout 2xx/3xx passe ; la VRAIE preuve est la
    relecture du dossier, faite après chaque échange ou suppression."""
    return _ftp_step(ftp, "Suppression", name, lambda: ftp.sendcmd("DELE " + name))


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
            summary=None, size=None, **extra):
    job = {"id": secrets.token_hex(5), "player": pid, "kind": kind, "num": num,
           "local": local, "original": original, "remote": remote, "by": by,
           "label": label, "summary": summary, "size": size or (os.path.getsize(local) if local and os.path.exists(local) else None),
           "sent": 0, "state": "queued", "detail": "", "error": None, "log": [],
           "created": time.time(), "started": None, "ended": None,
           "cancel": False, "force": False}
    job.update(extra)
    with _glock:
        if kind == "deliver":
            # Un bouche-trou ENCORE EN FILE pour cet emplacement passerait après la livraison
            # (priorités) et écraserait le clip qu'on vient de livrer. La dernière intention
            # gagne : on l'annule. (Un bouche-trou déjà en cours d'envoi, lui, finit d'abord,
            # puis la livraison le remplace — l'ordre final reste le bon.)
            for o in _jobs.values():
                if o["player"] == pid and o["num"] == num and o["kind"] in ("placeholder", "rename") and o["state"] == "queued":
                    o["state"], o["ended"] = "cancelled", time.time()
                    _log(o, "annulé : un clip a été livré sur cet emplacement")
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
    if j.get("local") and j["kind"] != "delete" and not j.get("keep_local"):
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
        while _paused.get(pid):
            time.sleep(0.5)             # file suspendue : on garde le travail, on n'insiste pas
        with _glock:
            j = _jobs.get(jid)
        if not j or j["state"] != "queued":
            continue
        j["state"], j["started"] = "sending", time.time()
        try:
            p = store.get(pid)
            if not p:
                raise RuntimeError("lecteur supprimé")
            if j["kind"] == "placeholder" or (j["kind"] == "move" and j.get("mode") != "swap"):
                _render(j, p)
            if j["kind"] == "delete":
                _delete_remote(j, p)
            elif j["kind"] == "ghost":
                _delete_ghost(j, p)
            elif j["kind"] == "rename":
                _rename_placeholder(j, p)
            elif j["kind"] == "move":
                _move(j, p)
            else:
                _swap(j, p)
            j["state"] = "done"
            _log(j, "terminé")
            _fail_streak.pop(pid, None)
        except Cancelled:
            j["state"] = "cancelled"
            _log(j, "annulé")
        except Exception as e:
            side, sig = classify(e)
            j["state"], j["error"], j["error_side"] = "error", str(e), side
            _log(j, "échec : %s" % e)
            print("[job %s] %s" % (jid, traceback.format_exc()), flush=True)
            _streak(pid, sig, j)
        finally:
            j["ended"] = time.time()
            _discard_local(j)
            _changed(pid)


SIDES = {
    "hyperdeck": "refusé par le HyperDeck",
    "missing": "fichier introuvable sur le HyperDeck",
    "network": "HyperDeck injoignable (réseau, FTP ou identifiants)",
    "deck": "protocole du HyperDeck (TCP 9993)",
    "tool": "erreur de l'outil",
}
PAUSE_AFTER = 3
_fail_streak = {}           # pid → (signature, nombre d'échecs identiques d'affilée)
_paused = {}                # pid → {reason, side, count, since}


def classify(e):
    """(côté, signature) d'une erreur. La signature regroupe les échecs « identiques »
    (même côté, même commande, même code de réponse), quel que soit le fichier."""
    if isinstance(e, FtpStepError):
        return e.side, (e.side, e.cmd, (e.reply or "")[:3])
    if isinstance(e, deck.DeckError):
        return "deck", ("deck", str(e)[:40])
    msg = str(e)
    if isinstance(e, (OSError, EOFError)) or msg.startswith("FTP "):
        return "network", ("network", msg.split(":")[0][:60])
    if isinstance(e, ftplib.all_errors):
        return "hyperdeck", ("hyperdeck", msg[:3])
    return "tool", ("tool", type(e).__name__, msg[:60])


def _streak(pid, sig, j):
    prev = _fail_streak.get(pid)
    n = prev[1] + 1 if prev and prev[0] == sig else 1
    _fail_streak[pid] = (sig, n)
    if n >= PAUSE_AFTER and not _paused.get(pid):
        _paused[pid] = {"reason": j["error"], "side": j.get("error_side"), "count": n,
                        "since": store.now()}


def queue_state(pid):
    with _glock:
        queued = sum(1 for j in _jobs.values() if j["player"] == pid and j["state"] == "queued")
    return {"paused": _paused.get(pid), "queued": queued}


def resume(pid):
    _paused.pop(pid, None)
    _fail_streak.pop(pid, None)


def cancel_queued(pid):
    n = 0
    with _glock:
        for j in _jobs.values():
            if j["player"] == pid and j["state"] == "queued":
                j["state"], j["ended"] = "cancelled", time.time()
                _discard_local(j)
                n += 1
    resume(pid)
    return n


def clear_failed(pid):
    with _glock:
        for k in [k for k, j in _jobs.items() if j["player"] == pid and j["state"] == "error"]:
            _jobs.pop(k, None)


def _render(j, p):
    slot = next((s for s in p["slots"] if s["num"] == j["num"]), None)
    if not slot:
        raise RuntimeError("emplacement %s absent de la liste" % j["num"])
    j["state"] = "rendering"
    key = ph_key(p, slot)
    cached = os.path.join(store.CACHE, "ph_%s.mov" % key)
    if p.get("ph_cache") and os.path.exists(cached):
        _log(j, "bouche-trou repris du cache du serveur")
        dst = cached
        j["keep_local"] = True
    else:
        _log(j, "fabrication du bouche-trou")
        os.makedirs(store.WORK, exist_ok=True)
        dst = os.path.join(store.WORK, "ph_%s.mov" % j["id"])
        media.render_placeholder(slot, p["format"], p["placeholder_duration"],
                                 p["audio_channels"], p["name"], dst, style=p.get("ph_style"))
        if p.get("ph_cache"):
            # Gardé seulement s'il reste de la place : le cache ne doit jamais empêcher un
            # dépôt (chaque clip transite en entier par ce disque).
            if shutil.disk_usage(store.DATA_DIR).free - os.path.getsize(dst) > CACHE_MIN_FREE:
                os.makedirs(store.CACHE, exist_ok=True)
                os.replace(dst, cached)
                dst = cached
                j["keep_local"] = True
            else:
                _log(j, "cache non alimenté : place insuffisante sur le serveur")
    j["local"] = dst
    j["size"] = os.path.getsize(dst)
    j["remote"] = ph_name(p, slot)
    j["key"] = ph_key(p, slot)
    j["label"] = media.format_label(p["format"])
    j["state"] = "sending"


def ph_name(p, slot):
    return store.placeholder_name(slot, p.get("placeholder_template"), p["name"])


def ph_key(p, slot):
    """Empreinte du CONTENU d'un bouche-trou (ce qui est dessiné et son format). Le nom du
    fichier n'y entre pas : changer le modèle de nom n'oblige qu'à renommer."""
    return media.placeholder_key(slot, p["format"], p["placeholder_duration"],
                                 p["audio_channels"], p["name"], p.get("ph_style"))


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


def _stor(ftp, j, local, name):
    """STOR avec avancement et annulation ; un envoi coupé ne laisse pas de fichier partiel."""
    j["sent"] = 0

    def cb(chunk):
        j["sent"] += len(chunk)
        if j["cancel"]:
            raise Cancelled()
    with open(local, "rb") as fh:
        ftp.storbinary("STOR " + name, fh, blocksize=1 << 20, callback=cb)


def _swap_direct(j, p):
    """Livraison SANS renommage (HyperDeck dont le FTP refuse RNFR) : envoi direct sous le
    nom définitif, PUIS suppression de l'ancien fichier de l'emplacement.
    Cet ordre garde l'ancien clip intact si l'envoi échoue. Contrepartie, inévitable sans
    renommage : pendant l'envoi, l'emplacement porte deux fichiers (les rangs suivants
    peuvent être décalés d'un cran le temps du transfert, si la machine liste un fichier
    en cours d'écriture). Un nom définitif identique à l'ancien est écrasé en place."""
    num, final, local = j["num"], j["remote"], j["local"]
    ftp = ftp_open(p)
    try:
        olds = [n for n in ftp_names(ftp) if store.num_of(n) == num and store.is_media(n) and n != final]
        _check_cancel(j)
        j["state"] = "sending"
        _log(j, "envoi direct sous %s (le HyperDeck ne renomme pas)" % final)
        try:
            _stor(ftp, j, local, final)
        except Exception:
            _close(ftp)
            if final not in olds:
                _cleanup_temp(p, final)
            raise
        # Le fichier reçu doit APPARAÎTRE au FTP avant qu'on touche à l'ancien : sinon on
        # retirerait le clip en place pour un fichier qu'on ne peut ni voir ni retirer.
        if final not in ftp_names(ftp):
            gone = False
            try:
                ftp_delete(ftp, final)
                gone = final not in ftp_names(ftp)
            except ftplib.all_errors:
                pass
            raise RuntimeError(
                "le HyperDeck a reçu « %s » mais son FTP ne le liste pas. L'ancien fichier de "
                "l'emplacement %s est conservé. %s" % (final, num,
                    "Le fichier reçu a été retiré." if gone else
                    "Le fichier reçu N'A PAS PU être retiré : s'il apparaît dans la liste du "
                    "HyperDeck, il faut le supprimer en passant la carte sur un ordinateur."))
    finally:
        _close(ftp)
    if olds:
        j["state"] = "swapping"
        _wait_air(j, p, olds)
        _log(j, "suppression de l'ancien : %s" % " + ".join(olds))
    ftp = ftp_open(p)
    try:
        for o in olds:
            ftp_delete(ftp, o)
        names = ftp_names(ftp)
    finally:
        _close(ftp)
    _fresh_listing(p["id"], names)
    mine = [n for n in names if store.num_of(n) == num and store.is_media(n)]
    if mine != [final]:
        raise RuntimeError("après envoi, l'emplacement %s porte : %s" % (num, ", ".join(mine) or "rien"))
    _record(j, p, olds)


def _swap(j, p):
    if _no_rename.get(p["id"]):
        return _swap_direct(j, p)
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
        except Exception:               # annulé OU coupé en route : pas de temporaire partiel
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
    renamed = False
    try:
        ftp = ftp_open(p)
        try:
            for o in olds:
                ftp_delete(ftp, o)
            try:
                ftp_rename(ftp, temp, final, pid=p["id"])
                renamed = True
            except ftplib.all_errors as e:
                _log(j, "renommage refusé (%s). Échange FTP :\n  %s"
                     % (e, "\n  ".join(ftp_journal(ftp))))
        finally:
            _close(ftp)
        # Session NEUVE pour la suite : un serveur laissé en attente du RNTO répond « 503
        # Bad sequence of commands » à tout ce qu'on lui envoie ensuite (relevé sur HyperDeck).
        ftp = ftp_open(p)
        try:
            if not renamed:
                cur = set(ftp_names(ftp))
                if final in cur and temp not in cur:
                    # Réponse trompeuse : le renommage a bien eu lieu, rien à renvoyer.
                    _log(j, "le HyperDeck a bien renommé malgré sa réponse")
                    renamed = True
            if not renamed:
                _log(j, "envoi direct sous le nom définitif")
                j["sent"] = 0
                with open(local, "rb") as fh:
                    ftp.storbinary("STOR " + final, fh, blocksize=1 << 20,
                                   callback=lambda c: j.__setitem__("sent", j["sent"] + len(c)))
                try:
                    ftp_delete(ftp, temp)
                except ftplib.all_errors:
                    pass
            names = ftp_names(ftp)
        finally:
            _close(ftp)
    except Exception:
        # Jamais de temporaire abandonné : il resterait en fin de liste du HyperDeck.
        if not renamed:
            _cleanup_temp(p, temp)
        raise
    _fresh_listing(p["id"], names)
    mine = [n for n in names if store.num_of(n) == num and store.is_media(n)]
    if mine != [final]:
        raise RuntimeError("après échange, l'emplacement %s porte : %s" % (num, ", ".join(mine) or "rien"))
    _record(j, p, olds)


def _cleanup_temp(p, temp):
    try:
        ftp = ftp_open(p)
        try:
            ftp_delete(ftp, temp)
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
                                "by": j["by"], "size": j["size"], "label": j["label"],
                                "key": j.get("key")}
        else:
            v = max([h.get("v") or 0 for h in hist] + [0]) + 1
            entry.update(action="remplacé" if v > 1 else "livré", v=v, original=j["original"],
                         summary=j.get("summary"))
            pl["files"][num] = {"kind": "clip", "remote": j["remote"], "original": j["original"],
                                "v": v, "at": entry["at"], "by": j["by"], "size": j["size"],
                                "label": j["label"], "summary": j.get("summary")}
        hist.append(entry)
    store.mutate(p["id"], fn)


def _move(j, p):
    """Déplacer un clip livré de l'emplacement A vers B, par RENOMMAGE (le clip ne repasse
    pas par le réseau). Deux modes :
      - "move"    : B perd ce qu'il portait (bouche-trou, ou clip en mode « remplacer »),
                    A reçoit un bouche-trou (déjà fabriqué et envoyé sous un nom temporaire) ;
      - "swap"    : A et B échangent leurs clips, rien n'est envoyé ni supprimé.
    Ordre choisi pour ne JAMAIS perdre un clip si la machine refuse une commande en route :
    le clip déplacé part d'abord sous un nom temporaire (fin de liste) ; ce n'est qu'une
    fois à l'abri qu'on supprime quoi que ce soit. En cas d'échec, retour en arrière.
    Comme pour un échange de fichier, la liste n'a un trou que quelques millisecondes."""
    if _no_rename.get(p["id"]):
        return _move_copy(j, p)
    a, b, mode = j["num"], j["target"], j.get("mode") or "move"
    slot_a = next((s for s in p["slots"] if s["num"] == a), None)
    slot_b = next((s for s in p["slots"] if s["num"] == b), None)
    rec_a, rec_b = p["files"].get(a) or {}, p["files"].get(b) or {}
    if not slot_a or not slot_b:
        raise RuntimeError("emplacement absent de la liste")
    if rec_a.get("kind") != "clip":
        raise RuntimeError("l'emplacement %s ne porte plus de clip livré" % a)
    if mode == "swap" and rec_b.get("kind") != "clip":
        raise RuntimeError("l'emplacement %s ne porte plus de clip livré : échange impossible" % b)
    x = rec_a["remote"]
    new_b = store.delivered_name(b, rec_a.get("original") or x)
    new_a = store.delivered_name(a, rec_b.get("original") or rec_b.get("remote")) if mode == "swap" \
        else ph_name(p, slot_a)
    park = "%smove_%s%s" % (store.TEMP_PREFIX, j["id"], os.path.splitext(x)[1] or ".mov")
    temp_ph = None

    ftp = ftp_open(p)
    try:
        names = ftp_names(ftp)
        if x not in names:
            raise RuntimeError("« %s » n'est plus sur le HyperDeck : relancez une vérification" % x)
        olds_b = [n for n in names if store.num_of(n) == b and store.is_media(n)]
        if mode == "swap" and rec_b.get("remote") not in olds_b:
            raise RuntimeError("le clip de l'emplacement %s n'est plus sur le HyperDeck" % b)
        if mode != "swap":
            # Bouche-trou de A, envoyé AVANT de toucher à quoi que ce soit (fin de liste).
            temp_ph = "%s%s.mov" % (store.TEMP_PREFIX, j["id"])
            j["state"] = "sending"
            _log(j, "envoi du bouche-trou de %s sous %s" % (a, temp_ph))
            with open(j["local"], "rb") as fh:
                ftp.storbinary("STOR " + temp_ph, fh, blocksize=1 << 20,
                               callback=lambda c: j.__setitem__("sent", j["sent"] + len(c)))
    except Exception:
        _close(ftp)
        if temp_ph:
            _cleanup_temp(p, temp_ph)
        raise
    _close(ftp)

    j["state"] = "swapping"
    try:
        _wait_air(j, p, [x] + olds_b)
    except Exception:
        if temp_ph:
            _cleanup_temp(p, temp_ph)
        raise

    done = []                      # renommages faits, pour revenir en arrière
    cur = [None]                   # renommage en cours au moment d'une erreur

    def ren(src, dst):
        cur[0] = (src, dst)
        ftp_rename(ftp, src, dst, pid=p["id"])
        done.append((src, dst))
        cur[0] = None

    ftp = ftp_open(p)
    try:
        _log(j, "%s : %s (%s) → %s" % ("échange" if mode == "swap" else "déplacement", a, x, b))
        try:
            ren(x, park)
        except RenameUnsupported as e:
            # Découvert à l'instant : rien n'a bougé ; on retire le bouche-trou temporaire et
            # on passe au déplacement par copie.
            _log(j, "%s → déplacement par copie" % e)
            _close(ftp)
            if temp_ph:
                _cleanup_temp(p, temp_ph)
            return _move_copy(j, p)
        if mode == "swap":
            ren(rec_b["remote"], new_a)
            ren(park, new_b)
        else:
            for o in olds_b:
                ftp_delete(ftp, o)
            ren(park, new_b)
            ren(temp_ph, new_a)
        names = ftp_names(ftp)
        _close(ftp)
    except ftplib.all_errors as e:
        _log(j, "refus du HyperDeck (%s). Échange FTP :\n  %s" % (e, "\n  ".join(ftp_journal(ftp))))
        _close(ftp)
        _rollback_move(j, p, done, cur[0], temp_ph, new_a)
        raise RuntimeError("déplacement annulé (tout est revenu en place) : %s" % e)
    _fresh_listing(p["id"], names)
    for n, want in ((a, new_a), (b, new_b)):
        mine = [f for f in names if store.num_of(f) == n and store.is_media(f)]
        if mine != [want]:
            raise RuntimeError("après déplacement, l'emplacement %s porte : %s" % (n, ", ".join(mine) or "rien"))

    _record_move(j, p, a, b, mode, new_a, new_b, x, olds_b if mode != "swap" else [rec_b.get("remote")])


def _record_move(j, p, a, b, mode, new_a, new_b, x, replaced_b):
    def fn(pl):
        ra, rb = dict(pl["files"].get(a) or {}), dict(pl["files"].get(b) or {})
        ha, hb = pl["history"].setdefault(a, []), pl["history"].setdefault(b, [])
        now = store.now()
        vb = max([h.get("v") or 0 for h in hb] + [0]) + 1
        hb.append({"at": now, "by": j["by"], "action": "reçu de %s" % a, "v": vb,
                   "original": ra.get("original"), "remote": new_b, "replaced": replaced_b})
        pl["files"][b] = dict(ra, remote=new_b, v=vb, at=now, by=j["by"])
        if mode == "swap":
            va = max([h.get("v") or 0 for h in ha] + [0]) + 1
            ha.append({"at": now, "by": j["by"], "action": "échangé avec %s" % b, "v": va,
                       "original": rb.get("original"), "remote": new_a, "replaced": [x]})
            pl["files"][a] = dict(rb, remote=new_a, v=va, at=now, by=j["by"])
        else:
            ha.append({"at": now, "by": j["by"], "action": "déplacé vers %s" % b, "remote": new_a,
                       "replaced": [x]})
            pl["files"][a] = {"kind": "placeholder", "remote": new_a, "at": now, "by": j["by"],
                              "size": j.get("size"), "label": j.get("label"), "key": j.get("key")}
    store.mutate(p["id"], fn)


def _move_copy(j, p):
    """Déplacer / échanger SANS renommage : le clip est RELU depuis le HyperDeck (RETR) vers
    le serveur, renvoyé sous son nouveau nom, et l'ancien n'est supprimé qu'ensuite. Plus long
    qu'un renommage (aller-retour du fichier), mais rien n'est perdu si un envoi échoue."""
    a, b, mode = j["num"], j["target"], j.get("mode") or "move"
    rec_a, rec_b = p["files"].get(a) or {}, p["files"].get(b) or {}
    slot_a = next((s for s in p["slots"] if s["num"] == a), None)
    if rec_a.get("kind") != "clip" or not slot_a:
        raise RuntimeError("l'emplacement %s ne porte plus de clip livré" % a)
    if mode == "swap" and rec_b.get("kind") != "clip":
        raise RuntimeError("l'emplacement %s ne porte plus de clip livré : échange impossible" % b)
    x = rec_a["remote"]
    y = rec_b.get("remote") if mode == "swap" else None
    new_b = store.delivered_name(b, rec_a.get("original") or x)
    new_a = store.delivered_name(a, rec_b.get("original") or y) if mode == "swap" else ph_name(p, slot_a)
    os.makedirs(store.WORK, exist_ok=True)
    copies = {}
    try:
        ftp = ftp_open(p)
        try:
            sizes = {}
            names = ftp_names(ftp, sizes)
            if x not in names:
                raise RuntimeError("« %s » n'est plus sur le HyperDeck : relancez une vérification" % x)
            if y and y not in names:
                raise RuntimeError("le clip de l'emplacement %s n'est plus sur le HyperDeck" % b)
            olds_b = [n for n in names if store.num_of(n) == b and store.is_media(n) and n not in (new_b, y)]
            need = sum(sizes.get(n, rec.get("size") or 0) for n, rec in ((x, rec_a), (y, rec_b)) if n)
            if shutil.disk_usage(store.DATA_DIR).free < need + 256 * 1024 ** 2:
                raise RuntimeError("place insuffisante sur le serveur pour relire le clip (%d Mo)" % (need >> 20))
            ph_size = j.get("size")
            for n in [x] + ([y] if y else []):
                dst = os.path.join(store.WORK, "mv_%s_%s" % (j["id"], len(copies)))
                j["state"], j["sent"], j["size"] = "sending", 0, sizes.get(n)
                _log(j, "relecture de %s depuis le HyperDeck" % n)
                with open(dst, "wb") as fh:
                    def w(chunk, fh=fh):
                        fh.write(chunk)
                        j["sent"] += len(chunk)
                        if j["cancel"]:
                            raise Cancelled()
                    ftp.retrbinary("RETR " + n, w, blocksize=1 << 20)
                if n in sizes and os.path.getsize(dst) != sizes[n]:
                    raise RuntimeError("relecture incomplète de %s" % n)
                copies[n] = dst
        finally:
            _close(ftp)
        # Envois d'abord (rien n'est supprimé tant que tout n'est pas arrivé).
        plan = [(copies[x], new_b)]
        plan.append((copies[y], new_a) if y else (j["local"], new_a))
        ftp = ftp_open(p)
        try:
            for src, name in plan:
                _log(j, "envoi de %s" % name)
                j["state"], j["size"] = "sending", os.path.getsize(src)
                _stor(ftp, j, src, name)
        finally:
            _close(ftp)
        j["size"] = ph_size
        gone = [n for n in [x] + ([y] if y else []) + olds_b if n not in (new_a, new_b)]
        j["state"] = "swapping"
        _wait_air(j, p, gone)
        ftp = ftp_open(p)
        try:
            for n in gone:
                ftp_delete(ftp, n)
            names = ftp_names(ftp)
        finally:
            _close(ftp)
    finally:
        for f in copies.values():
            try:
                os.remove(f)
            except OSError:
                pass
    _fresh_listing(p["id"], names)
    for n, want in ((a, new_a), (b, new_b)):
        mine = [f for f in names if store.num_of(f) == n and store.is_media(f)]
        if mine != [want]:
            raise RuntimeError("après déplacement, l'emplacement %s porte : %s" % (n, ", ".join(mine) or "rien"))
    _record_move(j, p, a, b, mode, new_a, new_b, x, olds_b if mode != "swap" else [y])


def _rollback_move(j, p, done, pending, temp_ph, new_a):
    """Remet les fichiers en place après un déplacement refusé. Sur une session NEUVE (la
    précédente peut être désynchronisée), et d'après le DISQUE : une réponse décalée peut
    annoncer un échec alors que le renommage a bien eu lieu."""
    try:
        ftp = ftp_open(p)
    except Exception as e:
        _log(j, "retour en arrière impossible : %s" % e)
        return
    try:
        names = set(ftp_names(ftp))
        if pending and pending[0] not in names and pending[1] in names:
            _log(j, "le HyperDeck a bien renommé %s → %s malgré sa réponse" % pending)
            done = done + [pending]
        for src, dst in reversed(done):
            try:
                ftp_rename(ftp, dst, src, pid=p["id"])
            except ftplib.all_errors as e2:
                _log(j, "retour en arrière impossible pour %s (%s)" % (dst, e2))
        if temp_ph and (temp_ph, new_a) not in done:
            try:
                ftp_delete(ftp, temp_ph)
            except ftplib.all_errors:
                pass
    except ftplib.all_errors as e:
        _log(j, "retour en arrière interrompu : %s" % e)
    finally:
        _close(ftp)


def _rename_placeholder(j, p):
    """Bouche-trou au bon contenu mais à l'ancien nom : RNFR/RNTO, rien d'autre."""
    slot = next((s for s in p["slots"] if s["num"] == j["num"]), None)
    if not slot:
        raise RuntimeError("emplacement %s absent de la liste" % j["num"])
    new = ph_name(p, slot)
    j["remote"] = new
    if store.num_of(new) != j["num"]:
        raise RuntimeError("nom calculé sans le numéro en tête : %s" % new)
    # Le nom à renommer est relu SUR LE DISQUE maintenant, jamais pris dans le plan : un
    # relevé fait pendant un transfert (ou deux changements de modèle de suite) donnait un
    # nom périmé → « 550 File unavailable » en série (relevé le 2026-11-15).
    ftp = ftp_open(p)
    try:
        cur = [n for n in ftp_names(ftp) if store.num_of(n) == j["num"] and store.is_media(n)]
    finally:
        _close(ftp)
    if cur == [new]:
        _log(j, "déjà au bon nom : rien à faire")
        return
    if len(cur) != 1:
        raise RuntimeError("l'emplacement %s porte %s sur le disque : renommage impossible, "
                           "relancez « Combler les vides »" % (j["num"], ", ".join(cur) or "aucun fichier"))
    old = cur[0]
    j["original"] = old
    _wait_air(j, p, [old])
    j["state"] = "swapping"
    _log(j, "renommage : %s → %s" % (old, new))
    ftp = ftp_open(p)
    try:
        try:
            ftp_rename(ftp, old, new, pid=p["id"])
        except RenameUnsupported:
            # Pas de renommage possible : le bouche-trou est refabriqué et renvoyé.
            _close(ftp)
            ftp = None
            _log(j, "le HyperDeck ne renomme pas : bouche-trou refabriqué et renvoyé")
            j["kind"] = "placeholder"
            _render(j, p)
            _swap(j, p)
            return
        except ftplib.all_errors as e:
            _log(j, "refus (%s). Échange FTP :\n  %s" % (e, "\n  ".join(ftp_journal(ftp))))
            _close(ftp)
            ftp = ftp_open(p)           # session neuve : l'ancienne peut être désynchronisée
            names = ftp_names(ftp)
            if new not in names or old in names:
                raise
            _log(j, "le HyperDeck a bien renommé malgré sa réponse")
        names = ftp_names(ftp)
    finally:
        _close(ftp)
    _fresh_listing(p["id"], names)
    if new not in names or old in names:
        raise RuntimeError("renommage non constaté sur le HyperDeck")

    def fn(pl):
        rec = pl["files"].get(j["num"]) or {}
        rec["remote"] = new
        pl["files"][j["num"]] = rec
        pl["history"].setdefault(j["num"], []).append(
            {"at": store.now(), "by": j["by"], "action": "bouche-trou renommé", "remote": new,
             "replaced": [old]})
    store.mutate(p["id"], fn)


def _delete_remote(j, p):
    name = j["remote"]
    _wait_air(j, p, [name])
    ftp = ftp_open(p)
    try:
        _log(j, "suppression de %s" % name)
        resp = ftp_delete(ftp, name)
        names = ftp_names(ftp)
        _fresh_listing(p["id"], names)
        if name in names:
            raise RuntimeError("le HyperDeck a répondu « %s » mais le fichier est toujours là" % resp)
    finally:
        _close(ftp)


def _ghost_candidates(name, raw):
    """Toutes les écritures plausibles du nom sur la carte, en octets : celle que donne le
    HyperDeck (brute), puis accents composés / décomposés en UTF-8, puis Latin-1."""
    out = []
    forms = [raw] if raw else []
    for form in ("NFC", "NFD"):
        forms.append(unicodedata.normalize(form, name).encode("utf-8"))
    try:
        forms.append(unicodedata.normalize("NFC", name).encode("latin-1"))
    except UnicodeEncodeError:
        pass
    for b in forms:
        if b not in out:
            out.append(b)
    return out


def _delete_ghost(j, p):
    """Clip lu par le HyperDeck mais INVISIBLE à son FTP (FAIT TERRAIN 2026-11-16 : accent
    décomposé d'un Mac). On tente la suppression sous chaque écriture possible du nom, dans
    une session FTP neuve à chaque fois (un refus peut laisser le serveur dans un état
    bizarre), octets envoyés tels quels. Échec = message qui dit quoi faire, sans détour."""
    name = j["remote"]
    _wait_air(j, p, [name])
    raw = None
    try:
        for cid, b in deck.raw_clip_names(p["host"], p.get("tcp_port") or deck.PORT).items():
            if _nfc(deck.strip_ext(b.decode("utf-8", "replace"))) == _nfc(deck.strip_ext(name)):
                raw = b
                break
    except Exception as e:
        _log(j, "lecture du nom brut impossible (%s) : on essaie les écritures usuelles" % e)
    replies = []
    for b in _ghost_candidates(name, raw):
        ftp = ftp_open(p)
        try:
            ftp.encoding = "latin-1"            # octets passés TELS QUELS
            arg = b.decode("latin-1")
            try:
                r = ftp.sendcmd("DELE " + arg)
            except ftplib.all_errors as e:
                r = str(e).strip()
            _log(j, "DELE %r → %s" % (b, r))
            replies.append(r)
            if r[:1] == "2":
                ftp.encoding = "utf-8"
                _fresh_listing(p["id"], ftp_names(ftp))
                _log(j, "fichier supprimé ; le HyperDeck relit son disque, la liste se met à jour")
                return
        finally:
            _close(ftp)
    raise FtpStepError("Suppression", name, replies[-1] if replies else "", "hyperdeck",
                       "le FTP du HyperDeck refuse de supprimer « %s » sous toutes ses écritures "
                       "(%s). Il faut retirer ce fichier hors de l'outil : carte sur un ordinateur "
                       "(supprimer ce seul fichier), ou formatage de la carte depuis le HyperDeck "
                       "puis « Combler les vides »." % (name, " / ".join(r for r in replies if r) or "sans réponse"))


# ── Vérification ────────────────────────────────────────────────────────────

# ── Le HyperDeck relit son disque EN DIFFÉRÉ ────────────────────────────────
# Relevé sur machine réelle le 2026-11-15 : juste après un échange FTP, le fichier est
# bien en place (FTP) mais `clips get` annonce encore l'ancien pendant quelques secondes.
# Vérifier immédiatement affichait donc « liste décalée » en rouge pour un état qui se
# rétablissait seul. On revérifie à intervalles rapprochés après chaque modification, et
# pendant cette fenêtre un écart est présenté comme « mise à jour en cours », pas comme
# une panne. Passé la fenêtre, un écart restant est un VRAI problème (rouge).
SETTLE_DELAYS = (2, 3, 3, 4, 5, 8, 10, 15)    # revérifs à 2, 5, 8, 12, 17, 25, 35, 50 s
SETTLE_WINDOW = max(90, sum(SETTLE_DELAYS) + 15)
_last_change = {}           # pid → instant de la dernière modification FTP terminée
_settle_gen = {}


def _fresh_listing(pid, names):
    """Relevé du disque fait PAR le transfert qui vient de finir : appliqué tout de suite au
    dernier état affiché. Sans cela, la case gardait l'ancien fichier jusqu'à la
    vérification suivante (« le clip n'apparaît pas, il faut cliquer Vérifier »). Seule la
    partie FTP est mise à jour : ce que liste le HyperDeck (TCP) reste le dernier relevé,
    et l'écart éventuel s'affiche comme tel (« ⟳ liste l'ancien »)."""
    _listing[pid] = (time.time(), names)
    sc = _scans.get(pid)
    if sc and sc.get("ftp", {}).get("ok"):
        old_sizes = sc["ftp"].get("sizes") or {}
        sc["ftp"] = dict(sc["ftp"], files=names, sizes={n: v for n, v in old_sizes.items() if n in names},
                         cached=False)


def _changed(pid):
    _last_change[pid] = time.time()
    gen = _settle_gen[pid] = _settle_gen.get(pid, 0) + 1
    threading.Thread(target=_settle, args=(pid, gen), daemon=True).start()


def _settle(pid, gen):
    for d in SETTLE_DELAYS:
        time.sleep(d)
        if _settle_gen.get(pid) != gen:
            return                  # une modification plus récente a pris le relais
        if busy(pid):
            continue                # un transfert tourne : sa fin relancera la séquence
        try:
            sc = scan(pid)
            p = store.get(pid)
            o = evaluate(p, sc)["order"] if p else None
            if o is None or o.get("ok") or not sc["deck"].get("ok"):
                return
        except Exception:
            traceback.print_exc()
            return


def settling(pid):
    """Vrai tant que le HyperDeck peut encore être en train de relire son disque."""
    t = _last_change.get(pid)
    return bool(t and time.time() - t < SETTLE_WINDOW) or busy(pid)


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
        prev = (_scans.get(pid) or {}).get("ftp") or {}
        res["ftp"] = {"ok": True, "files": _listing[pid][1], "cached": True,
                      "sizes": prev.get("sizes") or {}}
    else:
        try:
            ftp = ftp_open(p)
            sizes = {}
            try:
                names = ftp_names(ftp, sizes)
            finally:
                _close(ftp)
            _listing[pid] = (time.time(), names)
            res["ftp"] = {"ok": True, "files": names, "sizes": sizes}
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
                if rec and rec.get("kind") == "clip" and n == rec.get("remote"):
                    st = "delivered"
                elif n == ph_name(p, s):
                    # Bon nom ; contenu périmé si l'empreinte enregistrée diffère (style,
                    # format, durée…). Sans empreinte (pose ancienne) : on ne sait pas, on
                    # ne signale rien — « Refaire tous les bouche-trous » est là pour ça.
                    st = "stale_placeholder" if rec and rec.get("kind") == "placeholder" \
                        and rec.get("key") and rec.get("key") != ph_key(p, s) else "empty"
                elif (rec and rec.get("kind") == "placeholder" and n == rec.get("remote")) \
                        or store.is_placeholder(n, s, p.get("placeholder_template")):
                    st = "stale_placeholder"
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
        # Clips que le HyperDeck lit mais que son FTP ne liste PAS (nom mal formé : accent
        # décomposé d'un Mac…) : l'outil ne peut ni les voir ni les retirer. On le DIT, avec
        # le seul remède connu, plutôt que « hors liste » + un bouton qui échouerait.
        on_disk = {_nfc(deck.strip_ext(f)) for f in sc["ftp"].get("files") or []} \
            if sc and sc["ftp"].get("ok") else None
        ghosts = [n for n in extra if on_disk is not None and _nfc(deck.strip_ext(n)) not in on_disk]
        count = d.get("clips_count") if d.get("clips_count") is not None else len(clips)
        tr = d.get("transport") or {}
        ok = bad == 0 and count == len(slots) and not extra
        # Écarts DÉCRITS, emplacement par emplacement : ce qu'il y a sur le disque (FTP) et
        # ce que le HyperDeck liste pour ce numéro. « Le HyperDeck liste encore l'ancien
        # fichier » n'est pas « la liste est décalée » : l'exploitant doit pouvoir le lire.
        by_num_deck = {}
        for c in clips:
            by_num_deck.setdefault(store.num_of(c["name"]), []).append(c["name"])
        gaps = []
        for s in slots:
            e = out["slots"][s["num"]]
            if e.get("rank") == e["rank_expected"]:
                continue
            seen = by_num_deck.get(s["num"], [])
            e["deck_sees"] = seen
            if len(e["files"]) == 1 and e["rank"] is None:
                kind = "stale" if seen else "unseen"     # ancien nom encore listé / rien
            elif len(e["files"]) != 1:
                kind = "disk"                           # problème côté disque (absent, doublon)
            else:
                kind = "rank"                           # vu, mais pas au bon rang
            e["gap"] = kind
            gaps.append({"num": s["num"], "kind": kind, "disk": e["files"], "deck": seen,
                         "deck_rank": rank.get(deck.strip_ext(seen[0])) if seen else None,
                         "rank": e.get("rank"), "expected": e["rank_expected"]})
        out["order"] = {"ok": ok,
                        "expected": len(slots), "count": count, "misplaced": bad,
                        "extra": extra, "ghosts": ghosts, "gaps": gaps, "status": tr.get("status"),
                        "clip_id": tr.get("clip id"), "slot_id": tr.get("slot id"),
                        "settling": (not ok) and settling(p["id"]),
                        "since_change": round(time.time() - _last_change[p["id"]]) if p["id"] in _last_change else None}
    return out


def _nfc(s):
    return unicodedata.normalize("NFC", s or "")


def fill_plan(p, sc, redo=False):
    """Emplacements à combler d'après une vérification : [(num, "placeholder"|"rename", ancien)].

    Un bouche-trou périmé dont le CONTENU est toujours le bon (seul le modèle de nom a
    changé) est simplement RENOMMÉ : pas de fabrication ni de transfert, et le numéro en
    tête ne bouge pas, donc aucun rang non plus."""
    ev = evaluate(p, sc)
    out = []
    for s in p["slots"]:
        e = ev["slots"][s["num"]]
        if e["status"] == "missing":
            out.append((s["num"], "placeholder", None))
        elif redo and e["status"] == "empty":
            out.append((s["num"], "placeholder", e["files"][0]))
        elif e["status"] == "stale_placeholder":
            rec = p["files"].get(s["num"]) or {}
            old = e["files"][0]
            same = rec.get("kind") == "placeholder" and rec.get("remote") == old \
                and rec.get("key") and rec.get("key") == ph_key(p, s)
            # Bouche-trous posés avant la 0.4.0 (sans empreinte) : leur nom au modèle par
            # défaut dit le nom du CSV qu'ils portent ; s'il est inchangé, le contenu aussi.
            if rec.get("kind") == "placeholder" and not rec.get("key") \
                    and old == store.placeholder_name(s, store.DEFAULT_PH_TEMPLATE, p["name"]):
                same = True
            out.append((s["num"], "rename" if same else "placeholder", e["files"][0]))
    return out


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
