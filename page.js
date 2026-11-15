// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// UI de « Clips HyperDeck ». Toute la logique (contrôle de format, FTP, vérification)
// tourne dans le conteneur ; cette page dépose les fichiers et montre l'état.
//
// Envoi d'un fichier, en trois temps visibles :
//   1. PRÉ-CONTRÔLE : on lit dans le fichier local ses seuls atomes `ftyp` + `moov`
//      (quelques Ko, où qu'ils soient dans le .mov) et on les fait passer par ffprobe. Un
//      fichier au mauvais format est refusé en une seconde, sans avoir envoyé 2 Go.
//   2. ENVOI navigateur → serveur (barre bleue), un fichier à la fois : le serveur stocke
//      le clip entier avant de l'envoyer, et son disque n'est pas extensible.
//   3. TRANSFERT serveur → HyperDeck (barre verte), suivi par la file du lecteur.
//
// Rafraîchissement : chaque carte d'emplacement est redessinée SEULEMENT si son contenu a
// changé (signature) — un survol de glisser-déposer ne clignote pas. Les dialogues ne sont
// jamais réécrits par le rafraîchissement.
window.BTTools = window.BTTools || {};
window.BTTools.hyperdeck_clips = (function () {
    "use strict";
    let ctx = null, root = null, timer = null, alive = false;
    let players = [], pid = null, view = null;
    let uploads = [];          // file locale : {key, num, file, state, pct, error, fix}
    let uploading = false;
    let canDeliver = true, canManage = true, canReset = true;
    let listParsed = null;     // dernier aperçu d'import CSV
    let slotDlgNum = null;

    const esc = (s) => (window.BT && BT.esc ? BT.esc(s) : String(s == null ? "" : s)
        .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
    const tr = (key, fb) => { const v = ctx && ctx.t ? ctx.t(key) : null; return (v && v !== key) ? v : fb; };
    const $ = (sel) => root.querySelector(sel);
    const toast = (m, k) => ctx.toast(m, k);
    const T = (k, fb) => tr("plugin.hyperdeck_clips." + k, fb);

    // État d'un emplacement → libellé + classe. Le libellé porte l'information.
    const STATUS = {
        delivered: { label: "Livré", cls: "ok" },
        empty: { label: "Vide · bouche-trou", cls: "" },
        missing: { label: "MANQUANT", cls: "bad", tip: "Aucun fichier pour ce numéro : la liste du HyperDeck a un rang de moins." },
        duplicate: { label: "DOUBLON", cls: "bad", tip: "Plusieurs fichiers portent ce numéro : tous les rangs suivants sont décalés." },
        stale_placeholder: { label: "Bouche-trou périmé", cls: "warn", tip: "Le nom du bouche-trou ne correspond plus à la liste (nom modifié)." },
        foreign_file: { label: "Fichier non suivi", cls: "warn", tip: "Un fichier porte ce numéro mais n'a pas été posé par l'outil (client FTP ?)." },
        unknown: { label: "Non vérifié", cls: "" },
    };
    const JOB = {
        queued: "En attente", rendering: "Fabrication du bouche-trou", sending: "Envoi au HyperDeck",
        swapping: "Échange", waiting_air: "Attente : clip à l'antenne", blocked: "Attente : HyperDeck muet",
        done: "Terminé", error: "Échec", cancelled: "Annulé",
    };

    // ── Cycle de vie ──────────────────────────────────────────
    function mount(el, context) {
        ctx = context; root = el; alive = true;
        bind();
        rights();
        loadPlayers(true);
    }

    function unmount() {
        alive = false;
        clearTimeout(timer);
        root.querySelectorAll("dialog[open]").forEach((d) => d.close());
    }

    async function rights() {
        try {
            const r = await ctx.rights();
            if (!r.admin) {
                canDeliver = (r.granted || []).includes("clips.deliver");
                canManage = (r.granted || []).includes("clips.manage");
                canReset = (r.granted || []).includes("clips.reset");
            }
        } catch (e) { /* le serveur tranchera */ }
        $("#hc-add").hidden = !canManage;
        $("#hc-list-btn").hidden = !canManage;
        $("#hc-fill").hidden = !canDeliver;
        $("#hc-reset").hidden = !canReset;
    }

    // Rafraîchissement rapide pendant un transfert, ET pendant la minute qui suit (le
    // serveur revérifie le HyperDeck, qui relit son disque en différé) : sans cela, la case
    // gardait l'ancien fichier jusqu'à 8 s après la fin du transfert.
    let lastActive = 0;
    function schedule() {
        clearTimeout(timer);
        if (!alive) return;
        const active = uploading || (view && (view.jobs || []).some((j) => !["error", "cancelled"].includes(j.state)));
        if (active) lastActive = Date.now();
        const fast = active || Date.now() - lastActive < 60000 || (view && view.order && view.order.settling);
        timer = setTimeout(tick, fast ? 1500 : 8000);
    }

    async function tick() {
        if (!alive) return;
        try {
            if (pid) await loadView();
            await loadPlayers(false);
        } catch (e) { /* conteneur arrêté : la barre d'état de l'outil le dit déjà */ }
        schedule();
    }

    // ── Données ───────────────────────────────────────────────
    async function loadPlayers(first) {
        const d = await ctx.api("players");
        players = d.players || [];
        if (!pid || !players.find((p) => p.id === pid)) {
            let saved = null;
            try { saved = localStorage.getItem("hc.player"); } catch (e) { /* stockage indisponible */ }
            pid = (players.find((p) => p.id === saved) || players[0] || {}).id || null;
            if (pid) await loadView();
        }
        renderPlayers();
        if (first) schedule();
    }

    async function loadView() {
        if (!pid) { view = null; render(); return; }
        view = await ctx.api("players/" + pid);
        render();
    }

    function selectPlayer(id) {
        pid = id;
        try { localStorage.setItem("hc.player", id); } catch (e) { /* idem */ }
        view = null;
        renderPlayers();
        loadView().then(schedule);
    }

    // ── Rendu ─────────────────────────────────────────────────
    function verdictOf(p) {
        if (!p.slots) return { cls: "", text: "aucune liste" };
        if (!p.scan_at) return { cls: "", text: "non vérifié" };
        if (!p.ftp_ok && !p.deck_ok) return { cls: "bad", text: "injoignable" };
        const c = p.counts || {};
        const bad = (c.missing || 0) + (c.duplicate || 0);
        if (p.order && !p.order.ok && p.order.settling && !bad) return { cls: "warn", text: "relecture du HyperDeck…" };
        if (bad || (p.order && !p.order.ok)) return { cls: "bad", text: "écart avec le HyperDeck" };
        if (c.stale_placeholder || c.foreign_file || !p.deck_ok) return { cls: "warn", text: "à surveiller" };
        return { cls: "ok", text: (c.delivered || 0) + "/" + p.slots + " livrés" };
    }

    function renderPlayers() {
        $("#hc-players").innerHTML = players.map((p) => {
            const v = verdictOf(p);
            return '<button type="button" class="hc-ptab' + (p.id === pid ? " on" : "") + '" data-pid="' + esc(p.id) + '">'
                + '<span class="hc-dot ' + v.cls + '" aria-hidden="true"></span>'
                + '<span>' + esc(p.name) + '</span><small>' + esc(v.text) + (p.busy ? " · transfert" : "") + '</small></button>';
        }).join("");
        $("#hc-none").hidden = players.length > 0;
        $("#hc-player").hidden = !pid;
    }

    function render() {
        if (!view || !pid) { $("#hc-player").hidden = !pid; return; }
        const p = view.player;
        $("#hc-player").hidden = false;
        $("#hc-name").textContent = p.name;
        const l = p.list;
        $("#hc-sub").innerHTML = [
            '<span class="hc-mono">' + esc(p.host || "adresse non renseignée") + (p.ftp_dir ? " /" + esc(p.ftp_dir) : "") + '</span>',
            '<span>' + esc(view.format_label) + '</span>',
            l ? '<span>Liste : ' + esc(l.filename || "CSV") + (l.player ? " (player " + esc(l.player) + ")" : "")
                + " — " + esc(l.count) + " clips, importée le " + esc(fmtDate(l.imported_at)) + '</span>'
              : '<span>Pas encore de liste</span>',
        ].join("");
        renderStorage();
        renderVerdict();
        renderAlerts();
        renderTransfers();
        renderSlots();
        if (slotDlgNum && $("#hc-slot-dlg").open) renderSlotDialog(slotDlgNum, true);
        $("#hc-hint").hidden = !view.slots.length;
    }

    function renderVerdict() {
        const el = $("#hc-verdict");
        const sc = view.scan, o = view.order, n = view.slots.length;
        let cls = "", txt;
        if (!n) {
            txt = "<strong>Aucune liste.</strong> Importez le CSV des clips attendus pour ce lecteur.";
        } else if (!sc) {
            txt = "<strong>Pas encore vérifié.</strong> Cliquez sur « Vérifier ».";
        } else {
            const counts = {};
            view.slots.forEach((s) => { counts[s.status] = (counts[s.status] || 0) + 1; });
            const parts = [];
            if (!sc.ftp.ok) parts.push("FTP injoignable : " + esc(sc.ftp.error));
            if (!sc.deck.ok) parts.push("protocole (TCP " + esc(p_tcp()) + ") injoignable : " + esc(sc.deck.error));
            if (o) {
                if (o.ok) {
                    txt = "<strong>✓ " + o.count + " / " + o.expected + " clips, dans l'ordre.</strong> "
                        + (counts.delivered || 0) + " livrés, " + ((counts.empty || 0) + (counts.stale_placeholder || 0)) + " bouche-trous"
                        + (counts.stale_placeholder ? " (dont " + counts.stale_placeholder + " à renommer ou refaire : « Combler les vides »)" : "")
                        + (counts.foreign_file ? ", " + counts.foreign_file + " fichier(s) non suivi(s)" : "") + ".";
                    cls = "ok";
                } else {
                    txt = gapsText(o);
                    cls = o.settling ? "warn" : "bad";
                }
                if (o.status) txt += ' <span class="hc-badge">' + esc(o.status) + (o.clip_id ? " · clip " + esc(o.clip_id) : "") + "</span>";
            } else {
                const bad = (counts.missing || 0) + (counts.duplicate || 0);
                txt = bad ? "<strong>⚠ " + bad + " emplacement(s) manquant(s) ou en double</strong> (relevé FTP)."
                          : "<strong>Fichiers en place (relevé FTP).</strong> Ordre non contrôlé : protocole injoignable.";
                cls = bad ? "bad" : "warn";
            }
            if (parts.length) { txt = (txt ? txt + "<br>" : "") + parts.join("<br>"); if (cls === "ok" || !cls) cls = "warn"; }
            if (!sc.ftp.ok && !sc.deck.ok) cls = "bad";
        }
        el.className = "hc-verdict " + cls;
        el.innerHTML = '<span>' + txt + '</span>' + (sc ? '<span class="hc-when">vérifié à ' + esc(fmtTime(sc.at))
            + (sc.ftp.cached ? " (listing pendant un transfert)" : "") + '</span>' : "");
    }

    // Espace : le serveur garde chaque clip ENTIER avant de l'envoyer au HyperDeck.
    function renderStorage() {
        const st = view.storage || {}, sv = st.server, dk = st.deck, parts = [];
        if (sv) {
            const ratio = sv.used / sv.total;
            const cls = sv.free < sv.margin + 1024 ** 3 ? "bad" : sv.free < sv.margin + 4 * 1024 ** 3 ? "warn" : "";
            parts.push('<span class="hc-store ' + cls + '" title="Chaque clip déposé transite en entier par ce disque avant son envoi au HyperDeck. Marge gardée libre : '
                + fmtSize(sv.margin) + '.">Serveur : <strong>' + fmtSize(sv.free) + " libres</strong> sur " + fmtSize(sv.total)
                + ' <span class="hc-bar-p ' + cls + '"><i style="--p:' + ratio.toFixed(3) + '"></i></span>'
                + (sv.in_transit ? " · en transit " + fmtSize(sv.in_transit) : "") + " · clip max ≈ " + fmtSize(Math.max(0, sv.free - sv.margin))
                + (sv.cache ? ' · cache bouche-trous ' + fmtSize(sv.cache) + (canManage ? ' <button class="btn" type="button" data-clear-cache>vider</button>' : "") : "") + "</span>");
        }
        if (dk) {
            const bits = [];
            if (dk.files != null) bits.push(dk.files + " clips" + (dk.bytes != null ? ", " + (dk.complete ? "" : "≥ ") + fmtSize(dk.bytes) : ""));
            if (dk.slot) {
                if (dk.slot.volume) bits.push("volume « " + esc(dk.slot.volume) + " »");
                if (dk.slot.recording_time != null) bits.push("reste ≈ " + fmtHours(dk.slot.recording_time) + " d'enregistrement");
            }
            parts.push('<span class="hc-store" title="Le protocole du HyperDeck donne le temps d\'enregistrement restant (dans le format d\'enregistrement courant), pas les octets libres.">HyperDeck : <strong>'
                + (bits[0] || "?") + "</strong>" + bits.slice(1).map((b) => " · " + b).join("") + "</span>");
        }
        $("#hc-storage").innerHTML = parts.join("");
    }

    // L'écart, DÉCRIT : ce qu'il y a sur le disque et ce que le HyperDeck liste, avec la
    // conséquence pour l'exploitant (« rappeler le rang 11 lancerait … »). Orange tant que
    // le HyperDeck peut encore être en train de relire son disque, rouge s'il persiste.
    function gapsText(o) {
        const gaps = o.gaps || [], q = (n) => "« " + esc(n) + " »";
        const lines = gaps.slice(0, 6).map((g) => {
            const disk = g.disk[0];
            if (g.kind === "stale") return "<li><strong>" + esc(g.num) + "</strong> : sur le disque " + q(disk) + ", le HyperDeck liste encore " + q(g.deck[0])
                + (g.deck_rank ? " — rappeler le rang " + g.deck_rank + " lancerait encore l'ancien" : "") + "</li>";
            if (g.kind === "unseen") return "<li><strong>" + esc(g.num) + "</strong> : " + q(disk) + " est sur le disque mais absent de la liste du HyperDeck</li>";
            if (g.kind === "disk") return "<li><strong>" + esc(g.num) + "</strong> : " + (g.disk.length ? g.disk.length + " fichiers portent ce numéro sur le disque" : "aucun fichier sur le disque") + "</li>";
            return "<li><strong>" + esc(g.num) + "</strong> : " + q(disk) + " est au rang " + esc(g.rank) + " au lieu de " + esc(g.expected) + "</li>";
        });
        if (gaps.length > 6) lines.push("<li>… et " + (gaps.length - 6) + " autre(s)</li>");
        const staleNames = new Set(gaps.flatMap((g) => g.kind === "stale" ? g.deck : []));
        const extra = (o.extra || []).filter((n) => !staleNames.has(n));
        if (extra.length) lines.push("<li>hors liste, vu par le HyperDeck : " + extra.slice(0, 4).map(q).join(", ") + (extra.length > 4 ? "…" : "") + "</li>");
        if (o.count !== o.expected) lines.push("<li>le HyperDeck compte <strong>" + o.count + "</strong> clips pour " + o.expected + " attendus</li>");
        const head = o.settling
            ? "<strong>⟳ Le HyperDeck n'a pas encore pris en compte le dernier changement.</strong> Il relit son disque après chaque transfert ; nouvelle vérification automatique dans quelques secondes."
            : "<strong>⚠ Le HyperDeck ne voit pas la liste attendue</strong>" + (o.since_change != null && o.since_change < 3600 ? ", " + o.since_change + " s après le dernier transfert" : "")
              + ". Les rangs rappelés par l'exploitant ne correspondent pas à la liste.";
        return head + '<ul class="hc-gaps">' + lines.join("") + "</ul>";
    }

    function p_tcp() { return (view && view.player.tcp_port) || 9993; }

    function renderAlerts() {
        const a = view.anomalies || {}, out = [];
        const del = (n) => canManage ? ' <button class="btn" type="button" data-del-remote="' + esc(n) + '">Supprimer du HyperDeck</button>' : "";
        if (a.foreign && a.foreign.length) {
            out.push('<div class="hc-alert bad"><strong>' + a.foreign.length + " fichier(s) hors liste sur le HyperDeck</strong> — ils comptent dans sa liste de clips"
                + " et décalent les rangs s'ils se classent avant un emplacement :<ul>"
                + a.foreign.map((n) => '<li><span class="hc-mono">' + esc(n) + "</span>" + del(n) + "</li>").join("") + "</ul></div>");
        }
        const dups = view.slots.filter((s) => s.status === "duplicate");
        if (dups.length) {
            out.push('<div class="hc-alert bad"><strong>Numéros en double</strong> — gardez un seul fichier par numéro :<ul>'
                + dups.map((s) => "<li><strong>" + esc(s.num) + "</strong> : " + s.files.map((n) => '<span class="hc-mono">' + esc(n) + "</span>" + del(n)).join(" · ") + "</li>").join("")
                + "</ul></div>");
        }
        if (a.temps && a.temps.length) {
            const running = (view.jobs || []).some((j) => ["sending", "swapping", "waiting_air", "blocked"].includes(j.state));
            out.push('<div class="hc-alert"><strong>Fichier(s) temporaire(s) d\'envoi</strong> en fin de liste'
                + (running ? " (un transfert est en cours)" : " — reste d'un envoi interrompu") + " :<ul>"
                + a.temps.map((n) => '<li><span class="hc-mono">' + esc(n) + "</span>" + (running ? "" : del(n)) + "</li>").join("") + "</ul>"
                + (!running && canManage && a.temps.length > 1 ? '<button class="btn" type="button" data-del-temps>Supprimer les ' + a.temps.length + " temporaires</button>" : "")
                + "</div>");
        }
        (view.pending || []).forEach((x) => {
            out.push('<div class="hc-alert"><strong>' + esc(x.num) + " — « " + esc(x.original) + " » refusé :</strong> "
                + esc((x.check.problems || []).join(" ; "))
                + ". Si ce fichier sort de ffmpeg, son en-tête annonce à tort la trame basse en premier : on peut le corriger sans réencoder."
                + ' <button class="btn" type="button" data-fix="' + esc(x.id) + '">Corriger l\'en-tête et envoyer</button>'
                + ' <button class="btn" type="button" data-drop-pending="' + esc(x.id) + '">Abandonner</button></div>');
        });
        $("#hc-alerts").innerHTML = out.join("");
    }

    function renderTransfers() {
        const rows = [];
        uploads.filter((u) => u.state !== "done").forEach((u) => {
            const d = u.state === "precheck" ? "contrôle du format…" : u.state === "uploading" ? "envoi au serveur " + Math.round(u.pct) + " %"
                : u.state === "checking" ? "contrôle du fichier complet…" : u.state === "waiting" ? "en attente d'envoi" : (u.error || u.state);
            rows.push('<div class="hc-xfer"><span class="hc-num-l">' + esc(u.num) + '</span><span class="hc-xfer-t" title="' + esc(u.file.name) + '">'
                + esc(u.file.name) + '</span><span class="hc-bar-p"><i style="--p:' + (u.pct / 100) + '"></i></span><span class="hc-xfer-d">' + esc(d)
                + (u.state === "waiting" || u.state === "uploading" ? ' <button class="btn" type="button" data-cancel-up="' + esc(u.key) + '">Annuler</button>' : "")
                + "</span></div>");
        });
        const jobs = (view.jobs || []).filter((j) => j.state !== "cancelled" && j.state !== "error");
        const qs = view.queue || {}, sides = view.sides || {};
        if (qs.paused) {
            rows.unshift('<div class="hc-alert bad"><strong>File suspendue</strong> après ' + esc(qs.paused.count) + " échecs identiques d'affilée — "
                + "<strong>" + esc(sides[qs.paused.side] || qs.paused.side || "") + "</strong> :<br>" + esc(qs.paused.reason)
                + "<br>" + esc(qs.queued) + " transfert(s) restent en attente, rien n'a été perdu."
                + (canDeliver ? ' <button class="btn" type="button" data-queue="resume">Reprendre la file</button> <button class="btn btn-red" type="button" data-queue="cancel">Annuler les ' + esc(qs.queued) + " en attente</button>" : "") + "</div>");
        }
        // Échecs regroupés par cause : même côté + même message (noms de fichiers mis de côté).
        const groups = new Map();
        (view.jobs || []).filter((j) => j.state === "error").forEach((j) => {
            const key = (j.error_side || "?") + "|" + String(j.error || "").replace(/« [^»]*»/g, "«…»").replace(/\d+ s\b/g, "");
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(j);
        });
        groups.forEach((list) => {
            const j0 = list[0];
            rows.push('<div class="hc-alert bad"><strong>' + list.length + " transfert(s) en échec</strong> — côté : <strong>"
                + esc(sides[j0.error_side] || j0.error_side || "inconnu") + "</strong><br>" + esc(j0.error)
                + '<br><span class="hc-note">Emplacements : ' + list.map((j) => esc(j.num || j.remote || "?")).join(", ") + "</span>"
                + (canDeliver ? ' <button class="btn" type="button" data-queue="retry">Réessayer</button> <button class="btn" type="button" data-queue="clear-failed">Effacer ces échecs</button>' : "")
                + (list.some((j) => j.kind === "deliver") ? '<br><span class="hc-note">Un clip livré en échec doit être redéposé : son fichier n\'est pas gardé sur le serveur.</span>' : "")
                + "</div>");
        });
        const isBulk = (j) => (j.kind === "placeholder" || j.kind === "rename") && j.state === "queued";
        const placeholders = jobs.filter(isBulk);
        jobs.filter((j) => !isBulk(j)).forEach((j) => {
            const pct = j.size ? Math.min(100, 100 * j.sent / j.size) : 0;
            const act = [];
            if (["queued", "rendering", "sending", "waiting_air", "blocked"].includes(j.state) && canDeliver)
                act.push('<button class="btn" type="button" data-cancel-job="' + esc(j.id) + '">Annuler</button>');
            if (["waiting_air", "blocked"].includes(j.state) && canDeliver)
                act.push('<button class="btn btn-orange" type="button" data-force-job="' + esc(j.id) + '" title="Échange immédiat, même si le clip est à l\'antenne">Forcer</button>');
            const label = j.kind === "delete" ? "suppression de " + j.remote : j.kind === "placeholder" ? "bouche-trou"
                : j.kind === "rename" ? "renommage du bouche-trou"
                : j.kind === "move" ? (j.mode === "swap" ? "échange " + j.num + " ↔ " + j.target : "déplacement " + j.num + " → " + j.target + " (" + (j.original || "") + ")")
                : (j.original || j.remote);
            rows.push('<div class="hc-xfer"><span class="hc-num-l">' + esc(j.num || "—") + '</span><span class="hc-xfer-t" title="' + esc(j.remote || "") + '">'
                + esc(label) + '</span><span class="hc-bar-p ftp"><i style="--p:' + (j.state === "done" ? 1 : pct / 100) + '"></i></span><span class="hc-xfer-d">'
                + '<span class="hc-badge ' + (j.state === "error" ? "bad" : ["waiting_air", "blocked"].includes(j.state) ? "warn" : "info") + '">' + esc(JOB[j.state] || j.state) + "</span> "
                + esc(j.state === "sending" && j.size ? Math.round(pct) + " % · " + fmtSize(j.sent) + " / " + fmtSize(j.size) : (j.error || j.detail || ""))
                + " " + act.join(" ") + "</span></div>");
        });
        if (placeholders.length) rows.push('<div class="hc-xfer"><span class="hc-num-l">…</span><span class="hc-xfer-t">'
            + placeholders.length + " bouche-trou(s) en attente</span><span></span><span class=\"hc-xfer-d\">passent après les clips livrés</span></div>");
        $("#hc-transfers").innerHTML = rows.join("");
    }

    function uploadOf(num) { return uploads.find((u) => u.num === num && !["done", "failed"].includes(u.state)); }

    function slotHTML(s) {
        const st = STATUS[s.status] || STATUS.unknown;
        const f = s.file;
        let badge = '<span class="hc-badge ' + st.cls + '"' + (st.tip ? ' title="' + esc(st.tip) + '"' : "") + ">" + esc(st.label) + "</span>";
        if (s.status === "delivered" && f && f.v) badge = '<span class="hc-v">V' + f.v + "</span>" + badge;
        let rank = "";
        if (s.rank_expected) {
            const settling = view.order && view.order.settling;
            const seen = (s.deck_sees || [])[0];
            const tip = s.gap === "stale" ? "Le HyperDeck liste encore « " + seen + " » à la place de ce fichier"
                : s.gap === "unseen" ? "Fichier présent sur le disque mais pas encore dans la liste du HyperDeck"
                : "Rang attendu " + s.rank_expected;
            const lbl = s.gap === "stale" ? "liste l'ancien" : s.gap === "unseen" ? "pas encore vu" : "rang " + (s.rank || "—") + " ≠ " + s.rank_expected;
            rank = s.rank === s.rank_expected ? '<span class="hc-slot-rank" title="Rang dans la liste du HyperDeck">rang ' + s.rank + "</span>"
                : '<span class="hc-slot-rank ' + (settling && s.gap !== "disk" ? "pend" : "bad") + '" title="' + esc(tip) + '">' + (settling && s.gap !== "disk" ? "⟳ " : "") + esc(lbl) + "</span>";
        }
        const file = s.files && s.files.length ? s.files.join(" · ") : (s.status === "unknown" ? "" : "aucun fichier");
        let prog = "";
        const u = uploadOf(s.num);
        const j = s.job;
        if (u) {
            prog = '<span class="hc-bar-p"><i style="--p:' + (u.pct / 100) + '"></i></span><span class="hc-slot-job">'
                + esc(u.state === "uploading" ? "envoi au serveur " + Math.round(u.pct) + " %" : u.state === "precheck" ? "contrôle du format…" : u.state === "checking" ? "contrôle du fichier…" : "en attente d'envoi") + "</span>";
        } else if (j && j.state !== "error") {
            const pct = j.size ? 100 * j.sent / j.size : 0;
            prog = '<span class="hc-bar-p ftp"><i style="--p:' + (pct / 100) + '"></i></span><span class="hc-slot-job">'
                + esc((JOB[j.state] || j.state) + (j.state === "sending" ? " " + Math.round(pct) + " %" : "")) + "</span>";
        } else if (j && j.state === "error") {
            prog = '<span class="hc-slot-job" style="color:var(--status-stopped-fg)" title="' + esc(j.error) + '">Échec : '
                + esc((view.sides || {})[j.error_side] || j.error) + "</span>";
        }
        return '<span class="hc-slot-h"><span class="hc-slot-n">' + esc(s.num) + "</span>" + badge + rank + "</span>"
            + '<span class="hc-slot-nom" title="' + esc(s.nom) + '">' + esc(s.nom || "—") + "</span>"
            + '<span class="hc-slot-file" title="' + esc(file) + '">' + esc(file) + "</span>" + prog;
    }

    function renderSlots() {
        const host = $("#hc-cats");
        const cats = view.categories || [];
        // Structure (catégories) : reconstruite seulement si elle change.
        const sig = JSON.stringify(cats) + "|" + view.slots.map((s) => s.num).join(",");
        if (host.dataset.sig !== sig) {
            host.dataset.sig = sig;
            host.innerHTML = cats.map((c) => {
                const items = view.slots.filter((s) => s.cat === c);
                return '<section class="hc-cat"><h3>' + esc(c) + ' <small data-cat-count="' + esc(c) + '"></small></h3><div class="hc-slots">'
                    + items.map((s) => '<div class="hc-slot" tabindex="0" role="button" data-num="' + esc(s.num) + '" aria-label="Emplacement ' + esc(s.num) + '"></div>').join("")
                    + "</div></section>";
            }).join("");
        }
        view.slots.forEach((s) => {
            const el = host.querySelector('.hc-slot[data-num="' + CSS.escape(s.num) + '"]');
            if (!el) return;
            const html = slotHTML(s);
            if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
            el.className = "hc-slot st-" + s.status + (el.classList.contains("drag") ? " drag" : "");
            const movable = canDeliver && s.status === "delivered" && !s.job && !uploadOf(s.num);
            if (movable) el.setAttribute("draggable", "true"); else el.removeAttribute("draggable");
            el.title = movable ? "Glissez sur une autre case pour déplacer ce clip" : "";
        });
        cats.forEach((c) => {
            const items = view.slots.filter((s) => s.cat === c);
            const ok = items.filter((s) => s.status === "delivered").length;
            const el = host.querySelector('[data-cat-count="' + CSS.escape(c) + '"]');
            if (el) el.textContent = ok + " / " + items.length + " livrés · " + items[0].num + "–" + items[items.length - 1].num;
        });
    }

    // ── Détail d'un emplacement ──────────────────────────────
    function openSlot(num) {
        slotDlgNum = num;
        renderSlotDialog(num, false);
        const d = $("#hc-slot-dlg");
        if (!d.open) d.showModal();
    }

    function renderSlotDialog(num, refresh) {
        const s = view.slots.find((x) => x.num === num);
        if (!s) return;
        const st = STATUS[s.status] || STATUS.unknown;
        $("#hc-slot-title").textContent = s.num + " — " + (s.nom || "");
        const f = s.file || {};
        const sum = f.summary || null;
        const rows = [
            ["Catégorie", esc(s.cat)],
            ["État", '<span class="hc-badge ' + st.cls + '">' + esc(st.label) + "</span>" + (st.tip ? " " + esc(st.tip) : "")],
            ["Sur le HyperDeck", s.files && s.files.length ? s.files.map((n) => '<span class="hc-mono">' + esc(n) + "</span>").join("<br>") : "—"],
            ["Rang", s.rank_expected ? (s.rank === s.rank_expected ? s.rank + " ✓" : "vu " + (s.rank || "—") + ", attendu " + s.rank_expected) : "non contrôlé"],
        ];
        if (f.kind === "clip") {
            rows.push(["Version", "V" + f.v + " — « " + esc(f.original) + " »"]);
            rows.push(["Livré", esc(fmtDate(f.at)) + " par " + esc(f.by) + " · " + esc(fmtSize(f.size))]);
            if (sum) rows.push(["Format", esc(sum.label || "") + (sum.audio && sum.audio.length ? " · audio " + sum.audio.map((a) => a.channels + " voies " + (a.codec || "")).join(", ") : " · sans audio")
                + (sum.duration ? " · " + fmtDur(sum.duration) : "") + (sum.header_fixed ? " · en-tête de trame corrigé" : "")]);
        }
        rows.push(["Bouche-trou", '<span class="hc-mono">' + esc(s.placeholder_name) + "</span>"]);
        if (s.job) rows.push([s.job.state === "error" ? "Dernier échec" : "Transfert",
            '<span class="hc-badge ' + (s.job.state === "error" ? "bad" : "info") + '">' + esc(JOB[s.job.state] || s.job.state) + "</span> "
            + esc(s.job.error || s.job.detail || "") + (s.job.log && s.job.log.length ? '<details><summary>Journal</summary><pre class="hc-mono">' + esc(s.job.log.join("\n")) + "</pre></details>" : "")]);
        const hist = (s.history || []).slice().reverse();
        const body = '<dl class="hc-kv">' + rows.map(([k, v]) => "<dt>" + k + "</dt><dd>" + v + "</dd>").join("") + "</dl>"
            + "<h4>Historique</h4>" + (hist.length ? '<div class="hc-scroll"><table class="hc-table"><thead><tr><th>Quand</th><th>Geste</th><th>Fichier</th><th>Par</th></tr></thead><tbody>'
                + hist.map((h) => "<tr><td>" + esc(fmtDate(h.at)) + "</td><td>" + (h.v ? "<strong>V" + h.v + "</strong> " : "") + esc(h.action) + "</td><td>"
                    + (h.original ? "« " + esc(h.original) + " »<br>" : "") + '<span class="hc-mono">' + esc(h.remote) + "</span></td><td>" + esc(h.by) + "</td></tr>").join("")
                + "</tbody></table></div>" : '<p class="hc-note">Aucun geste enregistré.</p>');
        $("#hc-slot-body").innerHTML = body;
        if (!refresh || !$("#hc-slot-actions").dataset.num || $("#hc-slot-actions").dataset.num !== num) {
            const acts = [];
            if (canDeliver) {
                acts.push('<button class="btn btn-green" type="button" data-pick="' + esc(num) + '">' + (s.status === "delivered" ? "Remplacer (V" + ((f.v || 0) + 1) + ")…" : "Choisir un fichier…") + "</button>");
                if (s.status === "delivered" && !s.job) {
                    acts.push('<span class="hc-row"><select id="hc-move-to" aria-label="Emplacement de destination"><option value="">Déplacer vers…</option>'
                        + view.slots.filter((x) => x.num !== num).map((x) => '<option value="' + esc(x.num) + '">' + esc(x.num + " — " + x.nom)
                            + (x.status === "delivered" ? " (clip V" + ((x.file || {}).v || "?") + ")" : "") + "</option>").join("")
                        + '</select><button class="btn" type="button" data-move-from="' + esc(num) + '">Déplacer</button></span>');
                }
                if (s.status === "delivered" || s.status === "foreign_file" || s.status === "duplicate")
                    acts.push('<button class="btn btn-red" type="button" data-clear="' + esc(num) + '">Retirer (remettre le bouche-trou)</button>');
                if (["missing", "stale_placeholder"].includes(s.status))
                    acts.push('<button class="btn" type="button" data-clear="' + esc(num) + '">Poser le bouche-trou</button>');
            }
            $("#hc-slot-actions").innerHTML = acts.join("");
            $("#hc-slot-actions").dataset.num = num;
        }
    }

    // ── Dépôt de fichiers ─────────────────────────────────────
    function numPrefix(name) { const m = /^(\d+)\s*-\s*/.exec(name || ""); return m ? m[1] : null; }

    async function queueFile(num, file, opts) {
        opts = opts || {};
        const s = view.slots.find((x) => x.num === num);
        if (!s) return;
        if (!/\.mov$/i.test(file.name) && !opts.quiet) {
            if (!(await confirmDlg("Extension inhabituelle", "« " + esc(file.name) + " » n'est pas un .mov. Le format sera contrôlé de toute façon. Continuer ?", "Continuer"))) return;
        }
        const pre = numPrefix(file.name);
        if (pre && pre !== num && !opts.quiet) {
            if (!(await confirmDlg("Numéro différent", "Le fichier « " + esc(file.name) + " » commence par <strong>" + esc(pre)
                + "</strong>, mais vous le déposez sur l'emplacement <strong>" + esc(num) + " — " + esc(s.nom) + "</strong>.<br>Il sera nommé « "
                + esc(num) + " - " + esc(file.name) + " » sur le HyperDeck.", "Déposer sur " + num))) return;
        }
        if (s.status === "delivered" && !opts.quiet) {
            const f = s.file || {};
            if (!(await confirmDlg("Remplacer la V" + f.v, "L'emplacement <strong>" + esc(num) + "</strong> contient « " + esc(f.original)
                + " » (V" + f.v + "). Le nouveau fichier le remplacera sur le HyperDeck, sans changer le rang des autres clips.", "Remplacer par la V" + ((f.v || 0) + 1)))) return;
        }
        const u = { key: Math.random().toString(36).slice(2), num, file, state: "precheck", pct: 0, error: null, fix: false };
        uploads.push(u);
        render();
        try {
            const r = await precheck(file);
            if (r && r.readable && !r.ok) {
                if (r.fixable_field_order) {
                    const ok = await confirmDlg("En-tête de trame à corriger",
                        "« " + esc(file.name) + " » annonce : " + esc(r.problems.join(" ; ")) + ".<br><br>Les fichiers ProRes entrelacés produits par <strong>ffmpeg</strong> "
                        + "annoncent à tort la trame basse en premier alors que les images sont bien trame haute en premier. Si c'est le cas de ce fichier, "
                        + "l'outil peut corriger l'en-tête après l'envoi, sans réencoder.<br><br>Si le fichier est réellement en trame basse, il ne faut PAS le corriger : refaites l'export.",
                        "Corriger l'en-tête et envoyer");
                    if (!ok) { finish(u, "failed", "abandonné"); return; }
                    u.fix = true;
                } else {
                    finish(u, "failed", "refusé");
                    refusal(file.name, r.problems, r.summary, r.expected);
                    return;
                }
            }
        } catch (e) {
            if (e.coreTooOld) { finish(u, "failed", "cœur trop ancien"); msg("Envoi impossible", "<p>" + esc(e.message) + "</p>", []); return; }
            /* pré-contrôle indisponible : le contrôle complet tranchera */
        }
        u.state = "waiting";
        render();
        pump();
    }

    // Lit les atomes ftyp + moov du fichier local, où qu'ils soient (moov en tête ou en
    // fin de fichier selon l'outil d'export) : quelques Ko, jamais le média.
    async function headerBlob(file) {
        const parts = [];
        let off = 0, guard = 0, moov = false;
        while (off + 8 <= file.size && guard++ < 64) {
            const h = new DataView(await file.slice(off, off + 16).arrayBuffer());
            let size = h.getUint32(0);
            const type = String.fromCharCode(h.getUint8(4), h.getUint8(5), h.getUint8(6), h.getUint8(7));
            if (size === 1) size = Number(h.getBigUint64(8));
            else if (size === 0) size = file.size - off;
            if (size < 8) return null;
            if (type === "ftyp" || type === "moov") {
                if (size > 48 * 1024 * 1024) return null;
                parts.push(file.slice(off, off + size));
                if (type === "moov") moov = true;
            }
            off += size;
        }
        return moov ? new Blob(parts) : null;     // pas de moov (MXF, fichier tronqué) : contrôle complet seul
    }

    async function precheck(file) {
        const blob = await headerBlob(file);
        if (!blob) return null;
        const r = await fetch("/api/tools/" + encodeURIComponent(ctx.type) + "/players/" + encodeURIComponent(pid) + "/precheck",
            { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: blob });
        if (r.status === 426) {          // cœur trop ancien : inutile d'envoyer le fichier entier
            const d = await r.json().catch(() => ({}));
            throw Object.assign(new Error(d.error || "cœur trop ancien"), { coreTooOld: true });
        }
        if (!r.ok) return null;
        return r.json();
    }

    function finish(u, state, error) {
        u.state = state; u.error = error || null;
        uploads = uploads.filter((x) => x !== u);
        render();
    }

    // Un envoi à la fois vers le serveur : il stocke le clip entier avant transfert.
    function pump() {
        if (uploading) return;
        const u = uploads.find((x) => x.state === "waiting");
        if (!u) return;
        const sv = view && view.storage && view.storage.server;
        if (sv && u.file.size + sv.margin > sv.free) {
            finish(u, "failed", "place insuffisante");
            msg("Place insuffisante sur le serveur", "<p>« " + esc(u.file.name) + " » pèse " + fmtSize(u.file.size) + " ; le serveur n'a que "
                + fmtSize(sv.free) + " libres (dont " + fmtSize(sv.margin) + " gardés en réserve). Le clip y transite en entier avant d'être envoyé au HyperDeck.</p>"
                + "<p>Attendez la fin des transferts en cours, ou libérez de la place sur le serveur.</p>", []);
            return pump();
        }
        uploading = true;
        u.state = "uploading";
        render();
        schedule();
        const xhr = new XMLHttpRequest();
        u.xhr = xhr;
        const pidAt = pid;
        xhr.open("POST", "/api/tools/" + encodeURIComponent(ctx.type) + "/players/" + encodeURIComponent(pidAt) + "/slots/"
            + encodeURIComponent(u.num) + "/upload?name=" + encodeURIComponent(u.file.name));
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) { u.pct = 100 * e.loaded / e.total; if (u.pct >= 100) u.state = "checking"; renderTransfers(); renderSlots(); }
        };
        xhr.onload = async () => {
            let d = null;
            try { d = JSON.parse(xhr.responseText); } catch (e) { /* non-JSON */ }
            uploading = false;
            if (xhr.status === 200) {
                finish(u, "done");
                toast(u.num + " : « " + u.file.name + " » accepté, transfert vers le HyperDeck en file", "info");
            } else if (xhr.status === 422 && d && d.fixable && u.fix) {
                try {
                    await ctx.api("players/" + pidAt + "/pending/" + d.pending + "/fix", { method: "POST", body: {} });
                    finish(u, "done");
                    toast(u.num + " : en-tête corrigé, transfert en file", "info");
                } catch (e) { finish(u, "failed", e.message); refusal(u.file.name, (e.data && e.data.problems) || [e.message]); }
            } else if (xhr.status === 422 && d) {
                finish(u, "failed", "refusé");
                if (!d.fixable) refusal(u.file.name, d.problems || [d.error], d.summary, d.expected);
            } else {
                finish(u, "failed", (d && d.error) || ("HTTP " + xhr.status));
                toast(u.num + " : " + ((d && d.error) || ("échec de l'envoi (HTTP " + xhr.status + ")")), "error");
            }
            loadView().catch(() => {});
            pump();
        };
        xhr.onerror = xhr.onabort = () => {
            uploading = false;
            finish(u, "failed", "interrompu");
            if (!u.cancelled) toast(u.num + " : envoi interrompu", "error");
            pump();
        };
        xhr.send(u.file);
    }

    function refusal(name, problems, summary, expected) {
        msg("Fichier refusé", "<p>« " + esc(name) + " » ne peut pas être lu par le HyperDeck :</p><ul class=\"hc-problems\">"
            + (problems || []).map((p) => "<li>" + esc(p) + "</li>").join("") + "</ul>"
            + (expected ? '<p class="hc-note">Attendu : ' + esc(expected) + "</p>" : "")
            + (summary && summary.label ? '<p class="hc-note">Reçu : ' + esc(summary.label) + "</p>" : ""), []);
    }

    // Dépôt groupé : fichiers nommés « 104 - … » lâchés hors d'un emplacement.
    // Cases que N fichiers rempliraient à partir de `startNum`, dans l'ordre de la liste.
    // `skipFull` : on saute les cases qui portent déjà un clip livré (défaut), pour ne
    // jamais remplacer un clip sans l'avoir voulu.
    function seqTargets(startNum, n, skipFull) {
        const i0 = view.slots.findIndex((x) => x.num === startNum);
        if (i0 < 0) return { slots: [], missing: n };
        const out = [];
        for (let i = i0; i < view.slots.length && out.length < n; i++) {
            const x = view.slots[i];
            if (skipFull && (x.status === "delivered" || x.job || uploadOf(x.num))) continue;
            out.push(x);
        }
        return { slots: out, missing: n - out.length };
    }

    const natural = (a, b) => a.name.localeCompare(b.name, "fr", { numeric: true, sensitivity: "base" });

    // Dépôt de plusieurs fichiers : aperçu fichier → case, deux façons de répartir.
    function bulkDrop(files, startNum) {
        files = files.slice().sort(natural);
        const byNum = files.every((f) => { const n = numPrefix(f.name); return n && view.slots.some((x) => x.num === n); });
        const st = { mode: byNum ? "prefix" : "seq", start: startNum || (view.slots.find((x) => x.status !== "delivered") || view.slots[0] || {}).num, skip: true };
        const plan = () => {
            if (st.mode === "prefix") return files.map((f) => { const n = numPrefix(f.name); return { f, s: n && view.slots.find((x) => x.num === n), why: n ? "numéro " + n + " absent de la liste" : "pas de numéro en tête" }; });
            const t = seqTargets(st.start, files.length, st.skip);
            return files.map((f, i) => ({ f, s: t.slots[i], why: "plus de case libre après " + st.start }));
        };
        const draw = () => {
            const rows = plan(), ok = rows.filter((r) => r.s);
            const repl = ok.filter((r) => r.s.status === "delivered").length;
            const body = $("#hc-msg-body");
            body.innerHTML = '<div class="hc-bulk-opts">'
                + '<label><input type="radio" name="hc-bm" value="seq"' + (st.mode === "seq" ? " checked" : "") + "> À la suite, à partir de "
                + '<select id="hc-bm-start">' + view.slots.map((x) => '<option value="' + esc(x.num) + '"' + (x.num === st.start ? " selected" : "") + ">" + esc(x.num + " — " + x.nom) + "</option>").join("") + "</select>"
                + ' <label class="hc-inline"><input type="checkbox" id="hc-bm-skip"' + (st.skip ? " checked" : "") + "> sauter les cases déjà livrées</label></label>"
                + '<label><input type="radio" name="hc-bm" value="prefix"' + (st.mode === "prefix" ? " checked" : "") + "> Selon le numéro en tête du nom de fichier (« 104 - … »)</label></div>"
                + "<p><strong>" + ok.length + "</strong> case(s) remplie(s) sur " + files.length + " fichier(s)"
                + (repl ? ' · <span class="hc-badge warn">' + repl + " clip(s) remplacé(s)</span>" : "")
                + (ok.length < files.length ? ' · <span class="hc-badge bad">' + (files.length - ok.length) + " fichier(s) sans case</span>" : "") + "</p>"
                + '<div class="hc-scroll"><table class="hc-table"><thead><tr><th>Fichier</th><th>Case</th><th>Aujourd\'hui</th></tr></thead><tbody>'
                + rows.map((r) => "<tr><td>" + esc(r.f.name) + " <span class=\"hc-note\">" + fmtSize(r.f.size) + "</span></td><td>"
                    + (r.s ? "<strong>" + esc(r.s.num) + "</strong> " + esc(r.s.nom) : '<span class="hc-badge bad">' + esc(r.why) + "</span>") + "</td><td>"
                    + (r.s ? (r.s.status === "delivered" ? '<span class="hc-badge warn">V' + esc((r.s.file || {}).v) + " remplacée</span>"
                        : '<span class="hc-badge">' + esc((STATUS[r.s.status] || STATUS.unknown).label) + "</span>") : "") + "</td></tr>").join("")
                + "</tbody></table></div>";
            const go = $("#hc-msg-actions .btn-green");
            if (go) { go.textContent = "Déposer " + ok.length + " fichier(s)"; go.disabled = !ok.length; }
            body.querySelectorAll("[name=hc-bm]").forEach((x) => { x.onchange = () => { st.mode = x.value; draw(); }; });
            $("#hc-bm-start").onchange = (e) => { st.start = e.target.value; st.mode = "seq"; draw(); };
            $("#hc-bm-skip").onchange = (e) => { st.skip = e.target.checked; draw(); };
        };
        msg("Dépôt de " + files.length + " fichier(s)", "", [{
            label: "Déposer", cls: "btn-green",
            fn: () => plan().filter((r) => r.s).forEach((r) => queueFile(r.s.num, r.f, { quiet: true })),
        }]);
        draw();
    }

    // Surlignage pendant le survol : le navigateur donne le NOMBRE de fichiers (pas leurs
    // noms) ; on allume les cases qu'ils rempliraient à partir de la case survolée.
    function dragCount(e) {
        const it = e.dataTransfer && e.dataTransfer.items;
        const n = it ? Array.from(it).filter((x) => x.kind === "file").length : 0;
        return n || 1;
    }
    function paintFill(startNum, n) {
        // Un seul fichier remplace la case visée, quelle qu'elle soit ; plusieurs sautent les cases livrées.
        const t = !startNum ? { slots: [], missing: 0 }
            : n === 1 ? { slots: view.slots.filter((x) => x.num === startNum), missing: 0 } : seqTargets(startNum, n, true);
        const on = new Set(t.slots.map((x) => x.num));
        root.querySelectorAll(".hc-slot").forEach((el) => {
            el.classList.toggle("drag", el.dataset.num === (t.slots[0] || {}).num);
            el.classList.toggle("fill", on.has(el.dataset.num) && el.dataset.num !== (t.slots[0] || {}).num);
        });
        const h = $("#hc-drop-hint");
        if (!startNum) { h.hidden = false; h.textContent = n > 1 ? n + " fichiers : lâchez sur une case pour remplir à partir d'elle, ou ici pour répartir par numéro" : "Lâchez sur une case"; return; }
        h.hidden = false;
        h.textContent = n === 1 ? "1 fichier → " + startNum
            : n + " fichiers → " + t.slots.length + " case(s)" + (t.slots.length ? " : " + t.slots[0].num + " à " + t.slots[t.slots.length - 1].num : "")
              + (t.missing ? " · " + t.missing + " sans case (fin de liste)" : "") + " · cases déjà livrées sautées";
    }
    function clearFill() {
        root.querySelectorAll(".hc-slot.drag, .hc-slot.fill").forEach((x) => x.classList.remove("drag", "fill"));
        const h = $("#hc-drop-hint"); if (h) h.hidden = true;
    }

    // ── Dialogues génériques ──────────────────────────────────
    function msg(title, html, actions) {
        const d = $("#hc-msg-dlg");
        $("#hc-msg-title").textContent = title;
        $("#hc-msg-body").innerHTML = html;
        const f = $("#hc-msg-actions");
        f.innerHTML = '<span class="hc-spacer"></span>';
        (actions || []).forEach((a) => {
            const b = document.createElement("button");
            b.type = "button"; b.className = "btn " + (a.cls || ""); b.textContent = a.label;
            b.onclick = () => { d.close(); a.fn(); };
            f.appendChild(b);
        });
        if (d.open) d.close();
        d.showModal();
        return d;
    }

    function confirmDlg(title, html, label) {
        return new Promise((resolve) => {
            let done = false;
            const d = msg(title, html, [{ label, cls: "btn-green", fn: () => { done = true; resolve(true); } }]);
            d.addEventListener("close", () => { if (!done) resolve(false); }, { once: true });
        });
    }

    // ── Réglages d'un lecteur ─────────────────────────────────
    function openSettings(isNew) {
        const f = $("#hc-set-form");
        const p = isNew ? { ftp_port: 21, tcp_port: 9993, placeholder_duration: 10, audio_channels: 2, protect_on_air: true,
            format: { codec_tag: "apch", width: 1920, height: 1080, fps: "25/1", field_order: "tt" } } : view.player;
        f.dataset.mode = isNew ? "new" : "edit";
        $("#hc-set-title").textContent = isNew ? "Nouveau lecteur" : "Réglages — " + p.name;
        ["name", "host", "ftp_port", "ftp_user", "ftp_dir", "tcp_port", "placeholder_duration", "audio_channels"].forEach((k) => { f.elements[k].value = p[k] == null ? "" : p[k]; });
        f.elements.placeholder_template.value = p.placeholder_template || "{num} - VIDE - {nom}";
        fillStyle(p, isNew);
        f.dataset.tpl = f.elements.placeholder_template.value;
        tplPreview();
        f.elements.ftp_password.value = "";
        f.elements.ftp_password.placeholder = p.has_password ? "(inchangé)" : "(aucun)";
        ["codec_tag", "width", "height", "fps", "field_order"].forEach((k) => { f.elements[k].value = p.format[k]; });
        f.elements.protect_on_air.checked = p.protect_on_air !== false;
        $("#hc-del").hidden = isNew || !canManage;
        $("#hc-test").hidden = isNew;
        $("#hc-test-out").innerHTML = "";
        $("#hc-browse-out").hidden = true;
        $("#hc-set-dlg").showModal();
    }

    function formBody() {
        const f = $("#hc-set-form").elements;
        const b = {
            name: f.name.value.trim(), host: f.host.value.trim(), ftp_port: +f.ftp_port.value || 21, ftp_user: f.ftp_user.value.trim(),
            ftp_dir: f.ftp_dir.value.trim(), tcp_port: +f.tcp_port.value || 9993, placeholder_duration: +f.placeholder_duration.value || 10,
            audio_channels: +f.audio_channels.value || 0, protect_on_air: f.protect_on_air.checked,
            placeholder_template: f.placeholder_template.value.trim(),
            ph_style: styleFromForm(), ph_cache: f.ph_cache.checked,
            format: { codec_tag: f.codec_tag.value, width: +f.width.value, height: +f.height.value, fps: f.fps.value, field_order: f.field_order.value },
        };
        if (f.ftp_password.value) b.ftp_password = f.ftp_password.value;
        return b;
    }

    async function saveSettings(ev) {
        ev.preventDefault();
        const f = $("#hc-set-form");
        const body = formBody();
        try {
            if (f.dataset.mode === "new") {
                const r = await ctx.api("players", { method: "POST", body });
                $("#hc-set-dlg").close();
                await loadPlayers(false);
                selectPlayer(r.player.id);
                toast("Lecteur ajouté. Importez maintenant sa liste de clips.", "info");
            } else {
                const before = JSON.stringify(view.player.format);
                const tplChanged = (view.player.placeholder_template || "") !== body.placeholder_template;
                const styleChanged = JSON.stringify(normStyle(view.player.ph_style)) !== JSON.stringify(body.ph_style)
                    || JSON.stringify(view.player.format) !== JSON.stringify(body.format)
                    || view.player.placeholder_duration !== body.placeholder_duration || view.player.audio_channels !== body.audio_channels;
                await ctx.api("players/" + pid, { method: "PUT", body });
                $("#hc-set-dlg").close();
                await loadView();
                if (styleChanged && view.slots.length && canDeliver) {
                    await ctx.api("players/" + pid + "/scan", { method: "POST", body: {} }).catch(() => {});
                    await loadView();
                    const stale = view.slots.filter((s) => s.status === "stale_placeholder").length;
                    const empty = view.slots.filter((s) => s.status === "empty" || s.status === "stale_placeholder").length;
                    msg("Apparence des bouche-trous modifiée", "<p>Les bouche-trous déjà sur le HyperDeck gardent leur ancienne image tant qu'on ne les refait pas. "
                        + (stale ? stale + " sont repérés comme périmés. " : "") + "Les refaire maintenant ? (envoi FTP de chaque bouche-trou, un par un ; la liste garde ses rangs)</p>",
                        [{ label: "Refaire les " + empty + " bouche-trous", cls: "btn-green", fn: () => fill(true) }]);
                } else if (tplChanged && view.slots.length) {
                    await ctx.api("players/" + pid + "/scan", { method: "POST", body: {} }).catch(() => {});
                    await loadView();
                    const stale = view.slots.filter((s) => s.status === "stale_placeholder").length;
                    if (stale && canDeliver) msg("Nom des bouche-trous modifié", "<p>" + stale + " bouche-trou(s) portent encore l'ancien nom. Les renommer maintenant ? "
                        + "Ceux dont le contenu n'a pas changé sont simplement renommés sur le HyperDeck (aucun envoi, aucun rang déplacé).</p>",
                        [{ label: "Renommer " + stale + " bouche-trou(s)", cls: "btn-green", fn: fill }]);
                    else toast("Réglages enregistrés", "info");
                } else if (before !== JSON.stringify(body.format))
                    toast("Format modifié : les bouche-trous déjà posés restent dans l'ancien format. « Combler les vides » ne refait que les absents ; retirez-les pour les refaire.", "warning");
                else toast("Réglages enregistrés", "info");
            }
        } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
    }

    // ── Apparence des bouche-trous ──────────────────────────
    const DEF_STYLE = { bg: "#000000", fg: "#6e6e6e", bg_by_cat: {}, show: { num: true, nom: true, cat: true, lecteur: true, mention: true }, mention: "clip non livré" };
    function normStyle(st) {
        st = st || {};
        return { bg: (st.bg || DEF_STYLE.bg).toLowerCase(), fg: (st.fg || DEF_STYLE.fg).toLowerCase(),
            bg_by_cat: Object.fromEntries(Object.entries(st.bg_by_cat || {}).map(([k, v]) => [k, String(v).toLowerCase()])),
            show: Object.assign({}, DEF_STYLE.show, st.show || {}), mention: st.mention == null ? DEF_STYLE.mention : st.mention };
    }
    function fillStyle(p, isNew) {
        const f = $("#hc-set-form").elements, st = normStyle(p.ph_style);
        f.ph_bg.value = st.bg; f.ph_fg.value = st.fg; f.ph_mention.value = st.mention;
        ["num", "nom", "cat", "lecteur", "mention"].forEach((k) => { f["show_" + k].checked = !!st.show[k]; });
        f.ph_cache.checked = !!p.ph_cache;
        const cats = isNew || !view ? [] : (view.categories || []);
        $("#hc-cat-colors").innerHTML = cats.length ? cats.map((c) => {
            const on = c in st.bg_by_cat;
            return '<label><input type="checkbox" data-cat-on="' + esc(c) + '"' + (on ? " checked" : "") + '> ' + esc(c)
                + ' <input type="color" data-cat-color="' + esc(c) + '" value="' + esc(on ? st.bg_by_cat[c] : st.bg) + '"' + (on ? "" : " disabled")
                + ' aria-label="Fond de ' + esc(c) + '"></label>';
        }).join("") : '<span class="hc-note">Importez d\'abord la liste : les catégories apparaîtront ici.</span>';
        const sel = $("#hc-ph-num");
        sel.innerHTML = isNew || !view ? "" : cats.map((c) => { const x = view.slots.find((s) => s.cat === c); return x ? '<option value="' + esc(x.num) + '">' + esc(x.num + " — " + c) + "</option>" : ""; }).join("");
        const est = isNew || !view ? 0 : view.slots.length * (+f.placeholder_duration.value || 10) * 2.6e6;
        $("#hc-cache-est").textContent = est ? fmtSize(est) : "?";
        $("#hc-ph-img").removeAttribute("src");
        $("#hc-ph-msg").textContent = isNew ? "aperçu disponible une fois le lecteur enregistré" : "";
        if (!isNew) previewSoon();
    }
    function styleFromForm() {
        const f = $("#hc-set-form").elements, by = {};
        root.querySelectorAll("[data-cat-on]").forEach((c) => {
            if (c.checked) by[c.dataset.catOn] = root.querySelector('[data-cat-color="' + CSS.escape(c.dataset.catOn) + '"]').value.toLowerCase();
        });
        return { bg: f.ph_bg.value.toLowerCase(), fg: f.ph_fg.value.toLowerCase(), bg_by_cat: by, mention: f.ph_mention.value,
            show: { num: f.show_num.checked, nom: f.show_nom.checked, cat: f.show_cat.checked, lecteur: f.show_lecteur.checked, mention: f.show_mention.checked } };
    }
    let previewT = null, previewUrl = null, previewSeq = 0;
    function previewSoon() { clearTimeout(previewT); previewT = setTimeout(preview, 350); }
    async function preview() {
        if (!pid || $("#hc-set-form").dataset.mode === "new") return;
        const seq = ++previewSeq, f = $("#hc-set-form").elements;
        $("#hc-ph-msg").textContent = "…";
        try {
            const q = JSON.stringify({ num: $("#hc-ph-num").value, style: styleFromForm(), lecteur: f.name.value,
                format: { width: +f.width.value, height: +f.height.value } });
            const r = await fetch("/api/tools/" + encodeURIComponent(ctx.type) + "/players/" + encodeURIComponent(pid) + "/ph-preview?q=" + encodeURIComponent(q));
            if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("HTTP " + r.status));
            const blob = await r.blob();
            if (seq !== previewSeq) return;
            if (previewUrl) URL.revokeObjectURL(previewUrl);
            previewUrl = URL.createObjectURL(blob);
            $("#hc-ph-img").src = previewUrl;
            $("#hc-ph-msg").textContent = "";
        } catch (e) { if (seq === previewSeq) $("#hc-ph-msg").textContent = "aperçu indisponible : " + e.message; }
    }

    // Aperçu du nom des bouche-trous, calculé comme le serveur (variables remplacées).
    function tplPreview() {
        const f = $("#hc-set-form").elements, out = $("#hc-tpl-preview");
        const s = (view && view.slots && view.slots[0]) || { num: "104", nom: "Off 4", cat: "Thème 1", id: "4" };
        const v = { num: s.num, nom: s.nom || "clip", cat: s.cat || "", id: s.id || "", lecteur: f.name.value || (view ? view.player.name : "") };
        const t = f.placeholder_template.value.trim().replace(/\.mov$/i, "");
        let err = "";
        if (!t.startsWith("{num}")) err = "doit commencer par {num} (c'est lui qui place le fichier au bon rang)";
        else if (!t.slice(5) || /^[\d{]/.test(t.slice(5))) err = "mettez un séparateur après {num}";
        else { const bad = (t.match(/\{([^{}]*)\}/g) || []).map((x) => x.slice(1, -1)).filter((x) => !(x in v)); if (bad.length) err = "variable inconnue : {" + bad[0] + "}"; }
        out.innerHTML = err ? '<span class="hc-badge bad">' + esc(err) + "</span>"
            : 'Exemple : <span class="hc-mono">' + esc(t.replace(/\{([^{}]*)\}/g, (m, k) => v[k]).replace(/\s{2,}/g, " ") + ".mov") + "</span>"
              + (/vide|bouche|non livr|empty|spare/i.test(t) ? "" : ' <span class="hc-badge warn">conseil : gardez un mot comme « VIDE » pour distinguer un bouche-trou d\'un vrai clip</span>');
    }

    async function browse(path) {
        const out = $("#hc-browse-out");
        out.hidden = false;
        out.textContent = "lecture…";
        const b = formBody();
        if (!pid || $("#hc-set-form").dataset.mode === "new") { out.textContent = "Enregistrez d'abord le lecteur."; return; }
        try {
            const r = await ctx.api("players/" + pid + "/ftp/ls", { method: "POST", body: { path, host: b.host, ftp_port: b.ftp_port, ftp_user: b.ftp_user, ftp_password: $("#hc-set-form").elements.ftp_password.value } });
            out.innerHTML = '<span class="hc-mono">/' + esc(r.path) + "</span> · " + r.files + " fichier(s) "
                + (r.path ? '<button class="btn" type="button" data-ls="' + esc(r.path.split("/").slice(0, -1).join("/")) + '">↑</button>' : "")
                + '<button class="btn btn-green" type="button" data-use-dir="' + esc(r.path) + '">Utiliser ce dossier</button>'
                + r.dirs.map((d) => '<button class="btn" type="button" data-ls="' + esc((r.path ? r.path + "/" : "") + d) + '">' + esc(d) + "/</button>").join("");
        } catch (e) { out.textContent = e.message; }
    }

    async function testConn() {
        const out = $("#hc-test-out");
        out.textContent = "test en cours…";
        try {
            const r = await ctx.api("players/" + pid + "/test", { method: "POST", body: {} });
            out.innerHTML = (r.ftp.ok ? '<span class="hc-badge ok">FTP OK</span> ' + esc(r.ftp.cwd) + " · " + r.ftp.files + " fichier(s)"
                : '<span class="hc-badge bad">FTP</span> ' + esc(r.ftp.error)) + "<br>"
                + (r.deck.ok ? '<span class="hc-badge ok">Protocole OK</span> ' + esc(r.deck.model || "") + " · " + esc(r.deck.clips) + " clip(s) dans la timeline"
                : '<span class="hc-badge bad">Protocole</span> ' + esc(r.deck.error));
        } catch (e) { out.textContent = e.message; }
    }

    // ── Import de la liste ────────────────────────────────────
    function openList() {
        listParsed = null;
        $("#hc-list-out").innerHTML = view && view.slots.length
            ? '<p class="hc-note">Liste actuelle : ' + view.slots.length + " clips. Un nouvel import montre les écarts avant de les appliquer ; les fichiers déjà livrés restent liés à leur numéro.</p>" : "";
        $("#hc-list-apply").hidden = true;
        $("#hc-csv-export").hidden = !(view && view.slots.length);
        $("#hc-csv").value = "";
        $("#hc-list-dlg").showModal();
    }

    // CSV : UTF-8 avec BOM (Excel l'ouvre alors sans casser les accents), séparateur « ; ».
    function downloadCsv(name, rows) {
        const q = (v) => { v = String(v == null ? "" : v); return /[;"\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
        const text = "\ufeff" + rows.map((r) => r.map(q).join(";")).join("\r\n") + "\r\n";
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
        a.download = name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    }

    function exampleCsv() {
        downloadCsv("Liste_Clips_exemple.csv", [
            ["ID", "Player", "Catégorie", "Numéro", "Nom"],
            [1, "P1", "Thème 1", "101", "Off 1"],
            [2, "P1", "Thème 1", "102", "Off 2"],
            [3, "P1", "Thème 1", "103", "Interview Dupont"],
            [4, "P1", "Thème 2", "201", "Off 1"],
            [5, "P1", "Thème 2", "202", "Reportage"],
            [6, "P1", "Liners", "501", "Liner 1"],
            [7, "P1", "Début", "900", "Générique"],
            [8, "P2", "Habillage", "101", "Virgule 1"],
            [9, "P2", "Habillage", "102", "Virgule 2"],
            [10, "P2", "Jingles", "201", "Jingle entrée"],
        ]);
    }

    function exportCsv() {
        if (!view || !view.slots.length) return toast("Ce lecteur n'a pas encore de liste", "warning");
        const l = view.player.list || {};
        const rows = [["ID", "Player", "Catégorie", "Numéro", "Nom"]];
        (view.categories || []).forEach((c) => view.slots.filter((x) => x.cat === c)
            .forEach((x) => rows.push([x.id || "", x.player || l.player || "", x.cat, x.num, x.nom])));
        downloadCsv("Liste_Clips_" + view.player.name.replace(/[^\w.-]+/g, "_") + ".csv", rows);
    }

    async function readCsv(file) {
        const buf = await file.arrayBuffer();
        let text;
        try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); }
        catch (e) { text = new TextDecoder("windows-1252").decode(buf); }   // CSV exporté par Excel
        listParsed = { csv: text, filename: file.name, player: null };
        await previewList();
    }

    async function previewList() {
        const out = $("#hc-list-out");
        try {
            const r = await ctx.api("players/" + pid + "/list", { method: "POST", body: listParsed });
            if (r.need_player) {
                out.innerHTML = "<p>Ce CSV contient plusieurs players. Lequel correspond à <strong>" + esc(view.player.name) + "</strong> ?</p><p>"
                    + r.players.map((x) => '<button class="btn" type="button" data-choose-player="' + esc(x) + '">' + esc(x) + " (" + r.counts[x] + " clips)</button>").join(" ") + "</p>";
                $("#hc-list-apply").hidden = true;
                return;
            }
            const p = r.preview, d = p.diff;
            let html = "<p><strong>" + p.count + " clips</strong>" + (p.player ? " (player " + esc(p.player) + ")" : "") + " · "
                + p.categories.map((c) => esc(c.name) + " " + c.count).join(", ") + "</p>";
            if (!p.first) {
                if (!d.added.length && !d.removed.length && !d.changed.length) html += '<p class="hc-note">Identique à la liste actuelle.</p>';
                if (d.added.length) html += "<p><strong>" + d.added.length + " ajouté(s)</strong> : " + d.added.map((r) => esc(r.num + " " + r.nom)).join(", ") + "</p>";
                if (d.changed.length) html += "<p><strong>" + d.changed.length + " renommé(s)</strong> : " + d.changed.map((c) => esc(c.num + " " + c.before.nom + " → " + c.after.nom)).join(", ")
                    + '<br><span class="hc-note">Leurs bouche-trous deviendront « périmés » : « Combler les vides » les refera.</span></p>';
                if (d.removed.length) html += '<p><strong>' + d.removed.length + " retiré(s)</strong> : " + d.removed.map((r) => esc(r.num + " " + r.nom)).join(", ") + "</p>";
                if (p.orphans.length) html += '<div class="hc-alert bad">Ces fichiers resteront sur le HyperDeck et <strong>décaleront la liste</strong> tant qu\'ils n\'auront pas été supprimés : '
                    + p.orphans.map((n) => '<span class="hc-mono">' + esc(n) + "</span>").join(", ") + "</div>";
            }
            out.innerHTML = html;
            $("#hc-list-apply").hidden = false;
        } catch (e) {
            out.innerHTML = '<div class="hc-alert bad">' + esc(e.message) + "</div>";
            $("#hc-list-apply").hidden = true;
        }
    }

    async function applyList() {
        try {
            await ctx.api("players/" + pid + "/list", { method: "POST", body: Object.assign({}, listParsed, { apply: true }) });
            $("#hc-list-dlg").close();
            await ctx.api("players/" + pid + "/scan", { method: "POST", body: {} }).catch(() => {});
            await loadView();
            const holes = view.slots.filter((s) => ["missing", "stale_placeholder"].includes(s.status)).length;
            if (holes && canDeliver) {
                msg("Liste appliquée", "<p>" + holes + " emplacement(s) n'ont pas encore de fichier sur le HyperDeck (ou un bouche-trou périmé). "
                    + "Les combler maintenant avec des bouche-trous ?</p>", [{ label: "Combler " + holes + " emplacement(s)", cls: "btn-green", fn: fill }]);
            } else toast("Liste appliquée", "info");
        } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
    }

    async function fill(redo) {
        try {
            const r = await ctx.api("players/" + pid + "/fill", { method: "POST", body: { redo: redo === true } });
            toast(r.queued ? r.queued + " bouche-trou(s) en file" + (r.renamed ? " (dont " + r.renamed + " simple(s) renommage(s))" : "") : "Rien à combler : tous les emplacements ont leur fichier", "info");
            await loadView(); schedule();
        } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
    }

    // ── Fin de tournage ───────────────────────────────────────
    // Tous les emplacements repassent au bouche-trou. Plan relu sur la machine, puis
    // confirmation en tapant le nom du lecteur : un geste qui vide la machine ne doit pas
    // pouvoir partir d'un clic malheureux.
    async function resetFlow() {
        let plan;
        try { plan = (await ctx.api("players/" + pid + "/reset", { method: "POST", body: {} })).plan; }
        catch (e) { if (!e.rightsShown) toast(e.message, "error"); return; }
        const name = view.player.name;
        const n = plan.slots.length;
        const bs = plan.by_status;
        const lab = { delivered: "clip(s) livré(s)", foreign_file: "fichier(s) non suivi(s)", stale_placeholder: "bouche-trou(s) périmé(s)",
            missing: "emplacement(s) sans fichier", duplicate: "emplacement(s) en double" };
        let html = "";
        if (!n && !plan.foreign.length && !plan.temps.length) {
            msg("Fin de tournage — " + name, "<p>Rien à faire : les " + plan.total + " emplacements portent déjà leur bouche-trou, et aucun fichier hors liste.</p>", []);
            return;
        }
        html += "<p>Sur <strong>" + esc(name) + "</strong>, <strong>" + n + "</strong> emplacement(s) sur " + plan.total + " vont repasser au bouche-trou :</p><ul>"
            + Object.keys(bs).map((k) => "<li>" + bs[k] + " " + esc(lab[k] || k) + "</li>").join("") + "</ul>";
        if (n) html += '<div class="hc-scroll"><table class="hc-table"><thead><tr><th>N°</th><th>Nom</th><th>Fichier retiré du HyperDeck</th></tr></thead><tbody>'
            + plan.slots.map((s) => "<tr><td><strong>" + esc(s.num) + "</strong></td><td>" + esc(s.nom) + '</td><td class="hc-mono">'
                + (s.files.length ? s.files.map(esc).join("<br>") : "—") + "</td></tr>").join("") + "</tbody></table></div>";
        const extra = plan.foreign.concat(plan.temps);
        if (extra.length) html += '<label class="hc-toggle"><input type="checkbox" class="ios-toggle" id="hc-reset-foreign" checked><span>Supprimer aussi '
            + extra.length + " fichier(s) hors liste : " + extra.map((x) => '<span class="hc-mono">' + esc(x) + "</span>").join(", ") + "</span></label>";
        if (plan.busy) html += '<div class="hc-alert bad">' + plan.busy + " transfert(s) en cours ou en attente : attendez-les ou annulez-les d'abord.</div>";
        html += '<p class="hc-note">Chaque clip est échangé un par un, comme une livraison : la liste garde tous ses rangs pendant l\'opération, '
            + "et un clip en cours de lecture n'est pas retiré avant la fin. Les fichiers effacés du HyperDeck ne sont pas récupérables ; l'historique est conservé.</p>"
            + '<label class="hc-grid"><span>Tapez <strong>' + esc(name) + '</strong> pour confirmer</span><input id="hc-reset-confirm" autocomplete="off"></label>';
        const d = msg("Fin de tournage — " + name, html, [{
            label: "Tout remettre en bouche-trous", cls: "btn-red",
            fn: async () => {
                const foreign = !!(document.getElementById("hc-reset-foreign") || {}).checked;
                try {
                    const r = await ctx.api("players/" + pid + "/reset", { method: "POST", body: { confirm: typed, foreign } });
                    toast(r.queued + " bouche-trou(s) en file" + (r.deleted ? ", " + r.deleted + " suppression(s)" : ""), "info");
                    await loadView(); schedule();
                } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
            },
        }]);
        let typed = "";
        const go = d.querySelector("#hc-msg-actions .btn-red");
        go.disabled = true;
        const inp = d.querySelector("#hc-reset-confirm");
        inp.addEventListener("input", () => { typed = inp.value; go.disabled = inp.value !== name || plan.busy > 0; });
        inp.focus();
    }

    // ── Déplacer un clip d'une case à l'autre ─────────────────
    // Ce n'est qu'un RENOMMAGE sur le HyperDeck (le clip ne repasse pas par le réseau),
    // plus un bouche-trou pour la case quittée. Sur une case occupée : échanger ou remplacer.
    async function moveFlow(a, b) {
        const A = view.slots.find((x) => x.num === a), B = view.slots.find((x) => x.num === b);
        if (!A || !B || a === b) return;
        const fa = A.file || {}, fb = B.file || {};
        const clipA = "« " + esc(fa.original || "") + " » (V" + esc(fa.v) + ")";
        const run = async (mode) => {
            try {
                await ctx.api("players/" + pid + "/slots/" + encodeURIComponent(a) + "/move", { method: "POST", body: { to: b, mode } });
                toast(mode === "swap" ? "Échange " + a + " ↔ " + b + " en file" : "Déplacement " + a + " → " + b + " en file", "info");
                if ($("#hc-slot-dlg").open) $("#hc-slot-dlg").close();
                await loadView(); schedule();
            } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
        };
        const after = '<p class="hc-note">Sur le HyperDeck, c\'est un simple renommage : le clip n\'est pas renvoyé. La liste garde tous ses rangs '
            + "(un clip en cours de lecture n'est pas touché avant la fin).</p>";
        if (B.status === "delivered") {
            msg("Déplacer " + a + " sur " + b, "<p>L'emplacement <strong>" + esc(b) + " — " + esc(B.nom) + "</strong> contient déjà « " + esc(fb.original || "")
                + " » (V" + esc(fb.v) + ").</p><ul><li><strong>Échanger</strong> : " + clipA + " passe en " + esc(b) + ", et « " + esc(fb.original || "")
                + " » passe en " + esc(a) + ". Rien n'est supprimé.</li><li><strong>Remplacer</strong> : " + clipA + " passe en " + esc(b) + ", « "
                + esc(fb.original || "") + " » est <strong>supprimé du HyperDeck</strong>, et " + esc(a) + " reçoit son bouche-trou.</li></ul>" + after,
                [{ label: "Remplacer", cls: "btn-red", fn: () => run("replace") }, { label: "Échanger " + a + " ↔ " + b, cls: "btn-green", fn: () => run("swap") }]);
        } else {
            msg("Déplacer " + a + " → " + b, "<p>" + clipA + " quitte <strong>" + esc(a) + " — " + esc(A.nom) + "</strong> pour <strong>" + esc(b) + " — "
                + esc(B.nom) + "</strong>.</p><ul><li>sur le HyperDeck, il sera nommé « " + esc(b) + " - " + esc(stripNum(fa.original || "", a)) + " » ;</li>"
                + "<li>" + esc(a) + " reçoit son bouche-trou ;</li>"
                + (B.files && B.files.length ? "<li>ce que portait " + esc(b) + " (" + B.files.map((n) => '<span class="hc-mono">' + esc(n) + "</span>").join(", ") + ") est supprimé.</li>" : "")
                + "</ul>" + after, [{ label: "Déplacer vers " + b, cls: "btn-green", fn: () => run("move") }]);
        }
    }
    function stripNum(name, num) { const m = /^(\d+)\s*-\s*/.exec(name); return m && m[1] === num ? name.slice(m[0].length) : name; }

    // ── Événements ────────────────────────────────────────────
    function bind() {
        root.addEventListener("click", async (ev) => {
            const t = ev.target.closest("button, .hc-slot, [data-pid]");
            if (!t) return;
            const ds = t.dataset;
            try {
                if (ds.pid) return selectPlayer(ds.pid);
                if (t.classList.contains("hc-slot")) return openSlot(ds.num);
                if (ds.close !== undefined) return t.closest("dialog").close();
                if (ds.pick) { $("#hc-file").dataset.num = ds.pick; $("#hc-file").value = ""; return $("#hc-file").click(); }
                if (ds.clear) {
                    const s = view.slots.find((x) => x.num === ds.clear);
                    const ok = s.status !== "delivered" || await confirmDlg("Retirer le clip " + s.num,
                        "« " + esc((s.file || {}).original) + " » sera remplacé sur le HyperDeck par le bouche-trou « " + esc(s.placeholder_name) + " ». Le rang ne change pas.", "Retirer");
                    if (!ok) return;
                    await ctx.api("players/" + pid + "/slots/" + ds.clear + "/clear", { method: "POST", body: {} });
                    $("#hc-slot-dlg").open && $("#hc-slot-dlg").close();
                    toast(ds.clear + " : bouche-trou en file", "info");
                    return loadView().then(schedule);
                }
                if (ds.delRemote) {
                    if (!(await confirmDlg("Supprimer du HyperDeck", "Supprimer définitivement « <span class=\"hc-mono\">" + esc(ds.delRemote) + "</span> » du disque du HyperDeck ?", "Supprimer"))) return;
                    await ctx.api("players/" + pid + "/remote/delete", { method: "POST", body: { name: ds.delRemote } });
                    return loadView().then(schedule);
                }
                if (ds.queue) {
                    if (ds.queue === "retry") {
                        await ctx.api("players/" + pid + "/queue/clear-failed", { method: "POST", body: {} });
                        await ctx.api("players/" + pid + "/queue/resume", { method: "POST", body: {} });
                        await fill(false);
                        return;
                    }
                    if (ds.queue === "cancel" && !(await confirmDlg("Annuler la file", "Les transferts en attente seront annulés (les clips déjà envoyés ne bougent pas).", "Annuler la file"))) return;
                    await ctx.api("players/" + pid + "/queue/" + ds.queue, { method: "POST", body: {} });
                    return loadView().then(schedule);
                }
                if (ds.clearCache !== undefined) {
                    if (!(await confirmDlg("Vider le cache", "Les bouche-trous gardés sur le serveur seront supprimés (pas ceux du HyperDeck). Ils seront refabriqués au besoin.", "Vider"))) return;
                    const r = await ctx.api("cache/clear", { method: "POST", body: {} });
                    toast(r.removed + " fichier(s) supprimé(s) du cache", "info");
                    return loadView();
                }
                if (ds.moveFrom) {
                    const to = $("#hc-move-to").value;
                    if (!to) return toast("Choisissez l'emplacement de destination", "warning");
                    $("#hc-slot-dlg").close();
                    return moveFlow(ds.moveFrom, to);
                }
                if (ds.delTemps !== undefined) {
                    const names = (view.anomalies.temps || []).slice();
                    if (!(await confirmDlg("Supprimer les temporaires", names.length + " fichier(s) <span class=\"hc-mono\">zzz_envoi_…</span> seront supprimés du HyperDeck. Ce sont des restes d'envois interrompus : aucun n'occupe un emplacement.", "Supprimer " + names.length + " fichier(s)"))) return;
                    let n = 0;
                    for (const name of names) {
                        try { await ctx.api("players/" + pid + "/remote/delete", { method: "POST", body: { name } }); n++; }
                        catch (e) { toast(name + " : " + e.message, "error"); }
                    }
                    toast(n + " suppression(s) en file", "info");
                    return loadView().then(schedule);
                }
                if (ds.fix) { await ctx.api("players/" + pid + "/pending/" + ds.fix + "/fix", { method: "POST", body: {} }); toast("En-tête corrigé, transfert en file", "info"); return loadView().then(schedule); }
                if (ds.dropPending) { await ctx.api("players/" + pid + "/pending/" + ds.dropPending, { method: "DELETE" }); return loadView(); }
                if (ds.cancelJob) { await ctx.api("jobs/" + ds.cancelJob + "/cancel", { method: "POST", body: {} }); return loadView(); }
                if (ds.forceJob) {
                    if (!(await confirmDlg("Forcer l'échange", "L'échange va se faire <strong>même si le clip est en cours de lecture</strong> : l'antenne peut être coupée.", "Forcer"))) return;
                    await ctx.api("jobs/" + ds.forceJob + "/force", { method: "POST", body: {} }); return loadView();
                }
                if (ds.cancelUp) {
                    const u = uploads.find((x) => x.key === ds.cancelUp);
                    if (u) { u.cancelled = true; if (u.xhr && u.state === "uploading") u.xhr.abort(); else finish(u, "failed", "annulé"); }
                    return;
                }
                if (ds.choosePlayer) { listParsed.player = ds.choosePlayer; return previewList(); }
                if (ds.ls !== undefined) return browse(ds.ls);
                if (ds.useDir !== undefined) { $("#hc-set-form").elements.ftp_dir.value = ds.useDir; $("#hc-browse-out").hidden = true; return; }
            } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
        });
        $("#hc-add").onclick = () => openSettings(true);
        $("#hc-settings-btn").onclick = () => openSettings(false);
        $("#hc-set-form").onsubmit = saveSettings;
        $("#hc-browse").onclick = () => browse($("#hc-set-form").elements.ftp_dir.value);
        $("#hc-set-form").elements.placeholder_template.addEventListener("input", tplPreview);
        $("#hc-set-form").addEventListener("input", (e) => {
            const t = e.target;
            if (t.dataset.catOn !== undefined) { root.querySelector('[data-cat-color="' + CSS.escape(t.dataset.catOn) + '"]').disabled = !t.checked; }
            if (t.matches("[name^=ph_], [name^=show_], [data-cat-on], [data-cat-color], [name=name], [name=width], [name=height]") && t.name !== "ph_cache") previewSoon();
        });
        $("#hc-set-form").addEventListener("change", (e) => { if (e.target.matches("[data-cat-on], [name^=show_]")) previewSoon(); });
        $("#hc-ph-num").addEventListener("change", previewSoon);
        $("#hc-set-form").elements.name.addEventListener("input", tplPreview);
        $("#hc-test").onclick = testConn;
        $("#hc-del").onclick = async () => {
            if (!(await confirmDlg("Supprimer le lecteur", "Supprimer « " + esc(view.player.name) + " », sa liste et son historique ? Rien n'est effacé sur le HyperDeck.", "Supprimer"))) return;
            try {
                await ctx.api("players/" + pid, { method: "DELETE" });
                $("#hc-set-dlg").close(); pid = null; view = null; await loadPlayers(false);
            } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
        };
        $("#hc-scan").onclick = async () => {
            const b = $("#hc-scan"); b.disabled = true;
            try { view = await ctx.api("players/" + pid + "/scan", { method: "POST", body: {} }); render(); await loadPlayers(false); }
            catch (e) { toast(e.message, "error"); }
            finally { b.disabled = false; }
        };
        $("#hc-fill").onclick = () => fill(false);
        $("#hc-reset").onclick = resetFlow;
        $("#hc-list-btn").onclick = openList;
        $("#hc-list-apply").onclick = applyList;
        $("#hc-csv-example").onclick = exampleCsv;
        $("#hc-csv-export").onclick = exportCsv;
        $("#hc-csv").onchange = (e) => e.target.files[0] && readCsv(e.target.files[0]);
        const drop = $("#hc-csv-drop");
        drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
        drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
        drop.addEventListener("drop", (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.remove("drag"); if (e.dataTransfer.files[0]) readCsv(e.dataTransfer.files[0]); });
        $("#hc-file").onchange = (e) => {
            const f = e.target.files[0], num = e.target.dataset.num;
            if (!f || !num) return;
            if ($("#hc-slot-dlg").open) $("#hc-slot-dlg").close();
            queueFile(num, f);
        };
        $("#hc-slot-dlg").addEventListener("close", () => { slotDlgNum = null; $("#hc-slot-actions").dataset.num = ""; });

        // Glisser-déposer sur les emplacements, ou n'importe où (dépôt groupé).
        const app = $("#hc-app");
        const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
        const MOVE_T = "application/x-hc-slot";
        const isMove = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes(MOVE_T);
        let dragFrom = null;
        app.addEventListener("dragstart", (e) => {
            const s = e.target.closest && e.target.closest(".hc-slot[draggable=true]");
            if (!s) return;
            dragFrom = s.dataset.num;
            e.dataTransfer.setData(MOVE_T, dragFrom);
            e.dataTransfer.setData("text/plain", dragFrom);
            e.dataTransfer.effectAllowed = "move";
            s.classList.add("moving");
        });
        app.addEventListener("dragend", () => {
            dragFrom = null;
            root.querySelectorAll(".hc-slot.moving, .hc-slot.drag").forEach((x) => x.classList.remove("moving", "drag"));
        });
        app.addEventListener("dragover", (e) => {
            if (!isMove(e) || !dragFrom) return;
            const s = e.target.closest(".hc-slot");
            root.querySelectorAll(".hc-slot.drag").forEach((x) => { if (x !== s) x.classList.remove("drag"); });
            if (!s || s.dataset.num === dragFrom) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            s.classList.add("drag");
        });
        app.addEventListener("drop", (e) => {
            if (!isMove(e) || !dragFrom) return;
            e.preventDefault();
            e.stopImmediatePropagation();
            const s = e.target.closest(".hc-slot"), from = dragFrom;
            root.querySelectorAll(".hc-slot.drag, .hc-slot.moving").forEach((x) => x.classList.remove("drag", "moving"));
            dragFrom = null;
            if (s && s.dataset.num !== from) moveFlow(from, s.dataset.num);
        });
        app.addEventListener("dragover", (e) => {
            if (!hasFiles(e) || !view || !pid || e.target.closest("dialog")) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = canDeliver ? "copy" : "none";
            if (!canDeliver) return;
            const s = e.target.closest(".hc-slot");
            paintFill(s ? s.dataset.num : null, dragCount(e));
        });
        app.addEventListener("dragleave", (e) => {
            if (!e.relatedTarget || !app.contains(e.relatedTarget)) clearFill();
        });
        app.addEventListener("drop", (e) => {
            if (!hasFiles(e) || !view || !pid || e.target.closest("dialog")) return;
            e.preventDefault();
            clearFill();
            if (!canDeliver) return toast("Vous n'avez pas le droit de livrer des clips", "error");
            const files = Array.from(e.dataTransfer.files || []);
            if (!files.length) return;
            const s = e.target.closest(".hc-slot");
            if (s && files.length === 1) return queueFile(s.dataset.num, files[0]);
            bulkDrop(files, s ? s.dataset.num : null);
        });
        root.addEventListener("keydown", (e) => {
            const s = e.target.closest && e.target.closest(".hc-slot");
            if (s && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openSlot(s.dataset.num); }
        });
    }

    // ── Formats ───────────────────────────────────────────────
    function fmtSize(n) {
        if (n == null) return "?";
        const u = ["o", "Ko", "Mo", "Go"]; let i = 0; n = +n;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return (i ? n.toFixed(n < 10 ? 1 : 0).replace(".", ",") : n) + " " + u[i];
    }
    function fmtDate(s) { if (!s) return ""; const d = new Date(s); return isNaN(d) ? s : d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }); }
    function fmtTime(s) { if (!s) return ""; const d = new Date(s); return isNaN(d) ? s : d.toLocaleTimeString("fr-FR"); }
    function fmtHours(sec) { const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60); return h ? h + " h " + String(m).padStart(2, "0") : m + " min"; }
    function fmtDur(sec) { const m = Math.floor(sec / 60), s = Math.round(sec % 60); return m + " min " + String(s).padStart(2, "0") + " s"; }

    return { mount, unmount };
})();
