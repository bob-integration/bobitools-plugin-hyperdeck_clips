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
    let canDeliver = true, canManage = true;
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
            }
        } catch (e) { /* le serveur tranchera */ }
        $("#hc-add").hidden = !canManage;
        $("#hc-list-btn").hidden = !canManage;
        $("#hc-fill").hidden = !canDeliver;
    }

    function schedule() {
        clearTimeout(timer);
        if (!alive) return;
        const busy = uploading || (view && (view.jobs || []).some((j) => !["error", "cancelled"].includes(j.state)));
        timer = setTimeout(tick, busy ? 1500 : 8000);
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
        if (bad || (p.order && !p.order.ok)) return { cls: "bad", text: "liste décalée" };
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
                        + (counts.delivered || 0) + " livrés, " + (counts.empty || 0) + " bouche-trous.";
                    cls = "ok";
                } else {
                    const why = [];
                    if (o.count !== o.expected) why.push("le HyperDeck compte <strong>" + o.count + "</strong> clips pour " + o.expected + " attendus");
                    if (o.misplaced) why.push(o.misplaced + " emplacement(s) au mauvais rang");
                    if (o.extra && o.extra.length) why.push(o.extra.length + " clip(s) hors liste");
                    txt = "<strong>⚠ Liste décalée :</strong> " + why.join(", ") + ". Les rangs rappelés par l'exploitant ne correspondent plus.";
                    cls = "bad";
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
                + a.temps.map((n) => '<li><span class="hc-mono">' + esc(n) + "</span>" + (running ? "" : del(n)) + "</li>").join("") + "</ul></div>");
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
        const jobs = (view.jobs || []).filter((j) => j.state !== "cancelled");
        const placeholders = jobs.filter((j) => j.kind === "placeholder" && j.state === "queued");
        jobs.filter((j) => !(j.kind === "placeholder" && j.state === "queued")).forEach((j) => {
            const pct = j.size ? Math.min(100, 100 * j.sent / j.size) : 0;
            const act = [];
            if (["queued", "rendering", "sending", "waiting_air", "blocked"].includes(j.state) && canDeliver)
                act.push('<button class="btn" type="button" data-cancel-job="' + esc(j.id) + '">Annuler</button>');
            if (["waiting_air", "blocked"].includes(j.state) && canDeliver)
                act.push('<button class="btn btn-orange" type="button" data-force-job="' + esc(j.id) + '" title="Échange immédiat, même si le clip est à l\'antenne">Forcer</button>');
            const label = j.kind === "delete" ? "suppression de " + j.remote : j.kind === "placeholder" ? "bouche-trou" : (j.original || j.remote);
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
            rank = s.rank === s.rank_expected ? '<span class="hc-slot-rank" title="Rang dans la liste du HyperDeck">rang ' + s.rank + "</span>"
                : '<span class="hc-slot-rank bad" title="Rang attendu ' + s.rank_expected + '">rang ' + (s.rank || "—") + " ≠ " + s.rank_expected + "</span>";
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
            prog = '<span class="hc-slot-job" style="color:var(--status-stopped-fg)" title="' + esc(j.error) + '">Échec : ' + esc(j.error) + "</span>";
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
                if (s.status === "delivered" || s.status === "foreign_file")
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
        } catch (e) { /* pré-contrôle indisponible : le contrôle complet tranchera */ }
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
    function bulkDrop(files) {
        const rows = files.map((f) => {
            const n = numPrefix(f.name);
            const s = n && view.slots.find((x) => x.num === n);
            return { f, n, s };
        });
        const ok = rows.filter((r) => r.s);
        const html = '<p>Chaque fichier va sur l\'emplacement dont il porte le numéro en tête de nom.</p><div class="hc-scroll"><table class="hc-table"><thead><tr><th>Fichier</th><th>Emplacement</th></tr></thead><tbody>'
            + rows.map((r) => "<tr><td>" + esc(r.f.name) + "</td><td>" + (r.s ? "<strong>" + esc(r.s.num) + "</strong> " + esc(r.s.nom)
                + (r.s.status === "delivered" ? ' <span class="hc-badge warn">remplace la V' + esc((r.s.file || {}).v) + "</span>" : "")
                : '<span class="hc-badge bad">' + (r.n ? "numéro " + esc(r.n) + " absent de la liste" : "pas de numéro en tête") + "</span>") + "</td></tr>").join("")
            + "</tbody></table></div>";
        msg("Dépôt de " + files.length + " fichier(s)", html, ok.length ? [{
            label: "Déposer " + ok.length + " fichier(s)", cls: "btn-green",
            fn: () => ok.forEach((r) => queueFile(r.s.num, r.f, { quiet: true })),
        }] : []);
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
                await ctx.api("players/" + pid, { method: "PUT", body });
                $("#hc-set-dlg").close();
                await loadView();
                if (before !== JSON.stringify(body.format))
                    toast("Format modifié : les bouche-trous déjà posés restent dans l'ancien format. « Combler les vides » ne refait que les absents ; retirez-les pour les refaire.", "warning");
                else toast("Réglages enregistrés", "info");
            }
        } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
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
        $("#hc-csv").value = "";
        $("#hc-list-dlg").showModal();
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

    async function fill() {
        try {
            const r = await ctx.api("players/" + pid + "/fill", { method: "POST", body: {} });
            toast(r.queued ? r.queued + " bouche-trou(s) en file" : "Rien à combler : tous les emplacements ont leur fichier", "info");
            await loadView(); schedule();
        } catch (e) { if (!e.rightsShown) toast(e.message, "error"); }
    }

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
        $("#hc-fill").onclick = fill;
        $("#hc-list-btn").onclick = openList;
        $("#hc-list-apply").onclick = applyList;
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
        app.addEventListener("dragover", (e) => {
            if (!hasFiles(e) || !view || !pid || e.target.closest("dialog")) return;
            e.preventDefault();
            const s = e.target.closest(".hc-slot");
            root.querySelectorAll(".hc-slot.drag").forEach((x) => { if (x !== s) x.classList.remove("drag"); });
            if (s && canDeliver) s.classList.add("drag");
            e.dataTransfer.dropEffect = canDeliver ? "copy" : "none";
        });
        app.addEventListener("dragleave", (e) => {
            const s = e.target.closest && e.target.closest(".hc-slot");
            if (s && !s.contains(e.relatedTarget)) s.classList.remove("drag");
        });
        app.addEventListener("drop", (e) => {
            if (!hasFiles(e) || !view || !pid || e.target.closest("dialog")) return;
            e.preventDefault();
            root.querySelectorAll(".hc-slot.drag").forEach((x) => x.classList.remove("drag"));
            if (!canDeliver) return toast("Vous n'avez pas le droit de livrer des clips", "error");
            const files = Array.from(e.dataTransfer.files || []);
            if (!files.length) return;
            const s = e.target.closest(".hc-slot");
            if (s && files.length === 1) return queueFile(s.dataset.num, files[0]);
            bulkDrop(files);
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
        return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
    }
    function fmtDate(s) { if (!s) return ""; const d = new Date(s); return isNaN(d) ? s : d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }); }
    function fmtTime(s) { if (!s) return ""; const d = new Date(s); return isNaN(d) ? s : d.toLocaleTimeString("fr-FR"); }
    function fmtDur(sec) { const m = Math.floor(sec / 60), s = Math.round(sec % 60); return m + " min " + String(s).padStart(2, "0") + " s"; }

    return { mount, unmount };
})();
