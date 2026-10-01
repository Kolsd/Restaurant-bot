/* ═══════════════════════════════════════════════════
   Mesio — Staff App / Kitchen section (KDS)
   Ported from the old /kitchen page (app/static/html/kitchen.html +
   app/static/js/pages/kitchen.js) into a mount()/unmount() module for the
   unified Staff App shell. Business logic is UNCHANGED from the original
   kitchen.js — only bootstrap/cleanup are new (auto-refresh 15s, keyboard
   1-7 select / Enter=listo / T=+2min / "/"=search).
   ═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var TEMPLATE = `
<style>
/* v2 overrides / extensions (scoped to this section via .staff-kds-scope) */
.staff-kds-scope {
  /* Light, like the rest of Operación (2026-10-01): the KDS keeps its own
     variable names, pointed at the shared tokens. */
  --k-surface: var(--surface); --k-surface-2: var(--surface-hover); --k-border: var(--border);
  --k-border-strong: var(--border-strong); --k-text: var(--text); --k-text-2: var(--text-2); --k-text-3: var(--text-3);
}
.staff-kds-scope .kds { display: grid; grid-template-rows: 52px 1fr 40px; height: 100%; min-height: 640px; background: var(--bg); color: var(--k-text); border-radius: 12px; overflow: hidden; }

.staff-kds-scope .k-top { background: var(--bg); border-bottom: 1px solid var(--k-border); display: flex; align-items: center; padding: 0 20px; gap: 14px; }
.staff-kds-scope .k-brand { display: flex; align-items: center; gap: 8px; }
.staff-kds-scope .k-mark { width: 28px; height: 28px; background: var(--brand); border-radius: 7px; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:800; font-size:13px; }
.staff-kds-scope .k-brand-name { font-weight:700; font-size:14px; }
.staff-kds-scope .k-sep { width:1px; height:18px; background:var(--k-border); }
.staff-kds-scope .k-station { font-size:13px; color:var(--k-text-2); }
.staff-kds-scope .k-station strong { color:var(--k-text); }
.staff-kds-scope .k-pill { font-size:11px; font-weight:600; padding:3px 8px; border-radius:999px; background:rgba(29,158,117,0.14); color:var(--brand-dark); border:1px solid rgba(29,158,117,0.25); }

.staff-kds-scope .k-tabs { display:flex; gap:2px; background:var(--surface); border-radius:7px; padding:2px; margin-left:8px; }
.staff-kds-scope .k-tabs button { padding:5px 12px; border-radius:5px; border:none; background:transparent; color:var(--k-text-3); font-size:12px; font-weight:500; font-family:inherit; cursor:pointer; }
.staff-kds-scope .k-tabs button.active { background:var(--surface-hover); color:var(--k-text); }
.staff-kds-scope .k-tabs button .count { color:var(--k-text-3); margin-left:5px; font-size:11px; }
.staff-kds-scope .k-tabs button.active .count { color:var(--brand-dark); }

.staff-kds-scope .k-top-right { margin-left:auto; display:flex; align-items:center; gap:14px; font-size:12px; color:var(--k-text-2); }
.staff-kds-scope .k-stat { display:flex; flex-direction:column; line-height:1.1; text-align:right; }
.staff-kds-scope .k-stat-label { font-size:10px; color:var(--k-text-3); text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }
.staff-kds-scope .k-stat-val { font-family:var(--font-display); font-weight:600; font-size:15px; font-variant-numeric:tabular-nums; margin-top:1px; }
.staff-kds-scope .k-stat-val.ok { color:var(--brand-dark); } .staff-kds-scope .k-stat-val.warn { color:#b45309; }
.staff-kds-scope .k-clock { font-family:var(--font-mono); font-size:13px; background:var(--surface); padding:5px 10px; border-radius:7px; }

.staff-kds-scope .k-search-row { position:absolute; top:52px; left:0; right:0; z-index:10; padding:8px 16px; background:var(--bg); border-bottom:1px solid var(--k-border); display:none; }
.staff-kds-scope .k-search-row.open { display:flex; }
.staff-kds-scope .k-search-input { flex:1; background:var(--surface); border:1px solid var(--k-border); border-radius:7px; padding:8px 14px; color:var(--k-text); font-family:inherit; font-size:13px; outline:none; }
.staff-kds-scope .k-search-input:focus { border-color:var(--border-strong); }

.staff-kds-scope .k-wall { overflow-x:auto; overflow-y:hidden; padding:14px; display:grid; grid-auto-flow:column; grid-auto-columns:310px; gap:12px; background:radial-gradient(ellipse at top,var(--bg),var(--bg) 70%); align-items:start; }
.staff-kds-scope .k-wall::-webkit-scrollbar { height:8px; }
.staff-kds-scope .k-wall::-webkit-scrollbar-thumb { background:var(--surface-hover); border-radius:4px; }

.staff-kds-scope .tkt { background:var(--k-surface); border:1px solid var(--k-border); border-radius:12px; display:flex; flex-direction:column; overflow:hidden; max-height:calc(100vh - 200px); position:relative; }
.staff-kds-scope .tkt::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:var(--brand); }
.staff-kds-scope .tkt.w::before { background:#FBBF24; }
.staff-kds-scope .tkt.d::before { background:#F87171; animation:blink 1s ease-in-out infinite; }
.staff-kds-scope .tkt.done::before { background:#7C8393; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.4} }

.staff-kds-scope .tkt-head { padding:12px 16px 10px; display:flex; align-items:baseline; justify-content:space-between; border-bottom:1px solid var(--k-border); }
.staff-kds-scope .tkt-table { font-family:var(--font-display); font-weight:700; font-size:24px; letter-spacing:-1px; line-height:1; }
.staff-kds-scope .tkt-table .n { color:var(--k-text); }
.staff-kds-scope .tkt-table .sub { font-family:var(--font-body); font-size:11px; font-weight:500; color:var(--k-text-3); letter-spacing:0; margin-left:8px; }
.staff-kds-scope .tkt-time { font-family:var(--font-mono); font-size:20px; font-weight:500; font-variant-numeric:tabular-nums; }
.staff-kds-scope .tkt.w .tkt-time { color:#b45309; } .staff-kds-scope .tkt.d .tkt-time { color:#dc2626; }

.staff-kds-scope .tkt-meta { padding:7px 16px; display:flex; align-items:center; gap:8px; font-size:11px; color:var(--k-text-3); border-bottom:1px solid var(--k-border); flex-wrap:wrap; }
.staff-kds-scope .tag { padding:2px 7px; border-radius:4px; background:var(--surface); color:var(--k-text-2); font-size:11px; font-weight:500; }
.staff-kds-scope .tag.hot { background:#FFEDD5; color:#C2410C; }
.staff-kds-scope .tag.src { background:rgba(37,211,102,0.12); color:var(--brand-dark); }

.staff-kds-scope .tkt-body { padding:6px 0; overflow-y:auto; flex:1; }
.staff-kds-scope .tkt-body::-webkit-scrollbar { width:4px; }
.staff-kds-scope .tkt-body::-webkit-scrollbar-thumb { background:var(--surface-hover); border-radius:2px; }
.staff-kds-scope .tkt-item { padding:8px 16px; border-bottom:1px dashed var(--border); display:flex; gap:10px; cursor:pointer; transition:background 0.1s; }
.staff-kds-scope .tkt-item:hover { background:var(--surface); }
.staff-kds-scope .tkt-item:last-child { border-bottom:none; }
.staff-kds-scope .tkt-qty { font-family:var(--font-display); font-weight:700; font-size:17px; color:var(--brand-dark); min-width:24px; line-height:1.15; }
.staff-kds-scope .tkt-item.done .tkt-qty { color:var(--k-text-3); }
.staff-kds-scope .tkt-dish { flex:1; font-size:13.5px; font-weight:500; line-height:1.3; }
.staff-kds-scope .tkt-item.done .tkt-dish { text-decoration:line-through; color:var(--k-text-3); }
.staff-kds-scope .tkt-mods { font-size:11px; color:var(--k-text-3); margin-top:2px; }
.staff-kds-scope .tkt-check { width:20px; height:20px; border-radius:5px; border:1.5px solid var(--k-border-strong); flex-shrink:0; display:flex; align-items:center; justify-content:center; margin-top:1px; }
.staff-kds-scope .tkt-item.done .tkt-check { background:var(--brand); border-color:var(--brand); }
.staff-kds-scope .tkt-item.done .tkt-check::after { content:'✓'; color:#fff; font-size:12px; font-weight:700; }

.staff-kds-scope .tkt-foot { padding:9px 12px; border-top:1px solid var(--k-border); display:flex; gap:8px; background:var(--bg); }
.staff-kds-scope .tkt-btn { flex:1; padding:9px; border-radius:7px; border:1px solid var(--k-border-strong); background:var(--k-surface-2); color:var(--k-text); font-size:12px; font-weight:600; font-family:inherit; cursor:pointer; }
.staff-kds-scope .tkt-btn:hover { background:var(--surface-hover); }
.staff-kds-scope .tkt-btn.ready { background:var(--brand); border-color:var(--brand); color:#fff; }
.staff-kds-scope .tkt-btn.ready:hover { background:var(--brand-dark); }

.staff-kds-scope .k-bar { background:var(--bg); border-top:1px solid var(--k-border); display:flex; align-items:center; padding:0 20px; gap:14px; font-size:11px; color:var(--k-text-2); }
.staff-kds-scope .k-bar .hint { font-family:var(--font-mono); color:var(--k-text-3); font-size:10.5px; }
.staff-kds-scope .k-bar .hint kbd { background:var(--surface); border:1px solid var(--k-border); border-radius:4px; padding:1px 5px; margin:0 2px; color:var(--k-text-2); font-family:var(--font-mono); font-size:10px; }
.staff-kds-scope .dot.live { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--brand); animation:pulse-live 2s infinite; margin-right:4px; }
@keyframes pulse-live { 0%,100%{opacity:1} 50%{opacity:0.3} }

/* ── Phone: KDS is tablet-only — _applyKdsScale() in mount() scales .kds
   to fit narrow viewports via CSS transform (which doesn't reduce layout
   width on its own — transform-origin here keeps the scaled box anchored
   top-left instead of centering into overflow). ── */
@media (max-width: 768px) {
  .staff-kds-scope { overflow: hidden; max-width: 100%; }
  .staff-kds-scope .kds { transform-origin: top left; }
}
</style>
<div class="staff-kds-scope">
<div class="kds">
  <!-- Topbar -->
  <header class="k-top">
    <div class="k-brand">
      <div class="k-mark">M</div>
      <div class="k-brand-name">Cocina</div>
    </div>
    <div class="k-sep"></div>
    <div class="k-station">Estación <strong id="kds-station-name">Todas</strong></div>
    <div class="k-pill"><span class="dot live"></span>En línea</div>

    <div class="k-tabs">
      <button class="active" data-station="all">Activos <span class="count" id="kds-count-active">0</span></button>
      <button data-station="delayed">Retrasados <span class="count" id="kds-count-delayed">0</span></button>
      <button data-station="done">Listos <span class="count" id="kds-count-done">0</span></button>
    </div>

    <div class="k-top-right">
      <div class="k-stat">
        <div class="k-stat-label">T. promedio</div>
        <div class="k-stat-val ok" id="kds-avg">—:—</div>
      </div>
      <div class="k-stat">
        <div class="k-stat-label">En cola</div>
        <div class="k-stat-val" id="kds-queue">0</div>
      </div>
      <div class="k-stat">
        <div class="k-stat-label">Retrasados</div>
        <div class="k-stat-val ok" id="kds-delayed">0</div>
      </div>
      <div class="k-clock" id="kds-clock">--:--</div>
      <button id="kds-sound-toggle" class="tkt-btn" style="padding:5px 10px;font-size:11px;width:auto;flex:none;" type="button" aria-pressed="false">
        🔈 Activar sonido
      </button>
      <button id="kds-notify-optin" class="tkt-btn" style="padding:5px 10px;font-size:11px;width:auto;flex:none;" hidden>
        Activar alertas
      </button>
    </div>
  </header>

  <!-- Search bar (hidden, revealed by /) -->
  <div class="k-search-row" id="kds-search-row">
    <input class="k-search-input" id="kds-search" placeholder="Buscar mesa, plato…" autocomplete="off">
  </div>

  <!-- Ticket wall -->
  <section class="k-wall" id="kds-wall">
    <div style="padding:60px;text-align:center;color:var(--text-3);font-size:14px;">Cargando…</div>
  </section>

  <!-- Bottom hint bar -->
  <footer class="k-bar">
    <div class="hint">
      <kbd>1</kbd>…<kbd>7</kbd> seleccionar ·
      <kbd>↵</kbd> marcar listo ·
      <kbd>T</kbd> +2 min ·
      <kbd>/</kbd> buscar
    </div>
    <div style="margin-left:auto;" class="m-live-status online">
      <div class="dot"></div><span class="label">En vivo</span>
    </div>
  </footer>
</div>
</div>
`;

  // ── Cleanup tracking ─────────────────────────────────
  var _intervalHandles = [];
  function _trackInterval(id) { _intervalHandles.push(id); return id; }
  var _boundKeydown = null;
  var _boundKdsScale = null;

  // ── Phone scale — KDS is tablet-first; on small viewports scale the whole
  // .kds board down to fit instead of letting it overflow horizontally.
  // Ported from the original kitchen.html's trailing <script> (dropped by
  // mistake during the Staff App port — restored per mobile review round 2,
  // 2026-09-14). transform doesn't shrink layout width on its own, so the
  // matching CSS in TEMPLATE above sets transform-origin + fixed 768px width.
  function _applyKdsScale() {
    var kds = document.querySelector('.kds');
    if (!kds) return;
    if (window.innerWidth >= 768) { kds.style.transform = ''; kds.style.width = ''; return; }
    var s = window.innerWidth / 768;
    kds.style.transform = 'scale(' + s + ')';
    kds.style.width = '768px';
  }

  // ── State ───────────────────────────────────────────
  let _tickets = [];          // current displayed tickets
  let _selectedIdx = -1;      // keyboard-selected ticket index (0-based)
  let _station = 'all';       // station filter
  let _localPlusMins = {};    // ticketId → extra minutes added locally
  let _seenOrderIds = null;   // Set of order IDs seen on previous polls; null = first load (suppress alert)
  var _rtUnsubs = [];         // MesioRealtime.on() unsubscribe fns, cleared on unmount
  var _lastBeepAt = 0;        // throttle: never beep more than once every 2s

  // ── Sound preference (per device, opt-in — browsers block autoplay
  // until a user gesture, so this is unlocked by the "Activar sonido"
  // click itself, which also plays the initial confirmation beep). ──────
  function _soundEnabled() {
    try { return localStorage.getItem('rb_kds_sound') === '1'; } catch (e) { return false; }
  }
  function _setSoundEnabled(v) {
    try { localStorage.setItem('rb_kds_sound', v ? '1' : '0'); } catch (e) { /* private mode etc. */ }
  }
  function _playBeep() {
    var now = Date.now();
    if (now - _lastBeepAt < 2000) return;
    _lastBeepAt = now;
    mesioDing();
  }
  function _updateSoundBtn(btn) {
    var on = _soundEnabled();
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.textContent = on ? '🔊 Sonido activado' : '🔈 Activar sonido';
  }

  function _tc() {
    const el = document.getElementById('kds-clock');
    if (el) el.textContent = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
  }

  // ── Stats display ────────────────────────────────────
  function _updateStats(orders) {
    const now = Date.now();
    const pending = orders.filter(o => o.status !== 'listo' && o.status !== 'entregado');
    let totalMs = 0, count = 0, delayed = 0;
    pending.forEach(o => {
      const ms = now - new Date(o.created_at.endsWith('Z') ? o.created_at : o.created_at + 'Z').getTime();
      const mins = ms / 60000;
      totalMs += ms; count++;
      if (mins > 12) delayed++;
    });
    const avgMins = count > 0 ? Math.floor(totalMs / count / 60000) : 0;
    const avgSecs = count > 0 ? Math.floor((totalMs / count / 1000) % 60) : 0;

    const avgEl = document.getElementById('kds-avg');
    const queueEl = document.getElementById('kds-queue');
    const delayEl = document.getElementById('kds-delayed');
    if (avgEl) {
      avgEl.textContent = `${String(avgMins).padStart(2,'0')}:${String(avgSecs).padStart(2,'0')}`;
      avgEl.className = 'k-stat-val ' + (avgMins < 10 ? 'ok' : 'warn');
    }
    if (queueEl) queueEl.textContent = pending.length;
    if (delayEl) {
      delayEl.textContent = delayed;
      delayEl.className = 'k-stat-val ' + (delayed > 0 ? 'warn' : 'ok');
    }
  }

  // ── Elapsed time ─────────────────────────────────────
  function _elapsedMins(createdAt, localExtra) {
    const iso = createdAt.endsWith('Z') ? createdAt : createdAt + 'Z';
    const base = Math.floor((Date.now() - new Date(iso).getTime()) / 1000 / 60);
    return base + (localExtra || 0);
  }
  function _fmtTime(mins, secs) {
    return `${String(mins).padStart(2,'0')}:${String(secs || 0).padStart(2,'0')}`;
  }
  function _ticketClass(mins) {
    if (mins >= 12) return 'd';
    if (mins >= 7)  return 'w';
    return '';
  }

  // ── Source badge ─────────────────────────────────────
  function _srcBadge(order) {
    if (order._is_delivery) return '';
    if (order.channel === 'whatsapp_bot') return `<span class="tag src">📱 WhatsApp</span>`;
    if (order.channel === 'whatsapp')     return `<span class="tag src">📱 WhatsApp</span>`;
    if (order.channel === 'web_chat')     return `<span class="tag src">💬 Chat Mesio</span>`;
    return '';
  }

  // ── Render ticket wall ────────────────────────────────
  function _renderWall(orders) {
    _tickets = orders;
    const wall = document.getElementById('kds-wall');
    if (!wall) return;

    if (!orders.length) {
      wall.innerHTML = '<div style="padding:60px;text-align:center;color:var(--text-3);font-size:14px;">Sin órdenes activas</div>';
      return;
    }

    wall.innerHTML = orders.map((o, idx) => {
      const extra = _localPlusMins[o.id] || 0;
      const iso = o.created_at.endsWith('Z') ? o.created_at : o.created_at + 'Z';
      const totalSecs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000) + extra * 60;
      const mins = Math.floor(totalSecs / 60);
      const secs = totalSecs % 60;
      const cls = _ticketClass(mins);
      const isDone = o.status === 'listo';
      const isSelected = idx === _selectedIdx;

      const items = Array.isArray(o.items) ? o.items : [];

      const _COURSE_ORDER = ['bebida', 'entrada', 'principal', 'postre', 'sin_curso'];
      const _COURSE_LABEL = {
        bebida:    '🥤 Bebidas',
        entrada:   '🥗 Entradas',
        principal: '🍽 Plato fuerte',
        postre:    '🍰 Postres',
        sin_curso: 'Otros',
      };
      const renderItem = (item) => {
        const el = document.createElement('div');
        el.textContent = item.name || '';
        const safeName = el.innerHTML;
        const qEl = document.createElement('div');
        qEl.textContent = String(item.quantity || item.qty || 1);
        const safeQty = qEl.innerHTML;
        const modHtml = item.notes ? `<div class="tkt-mods">${_escHtmlSafe(item.notes)}</div>` : '';
        return `<div class="tkt-item" data-done="0"><div class="tkt-qty">${safeQty}</div><div><div class="tkt-dish">${safeName}</div>${modHtml}</div><div class="tkt-check"></div></div>`;
      };

      const anyCourse = items.some(it => (it.course || '').trim());
      let itemsHtml;
      if (anyCourse) {
        const grouped = {};
        items.forEach(it => {
          const c = ((it.course || '').trim().toLowerCase()) || 'sin_curso';
          if (!grouped[c]) grouped[c] = [];
          grouped[c].push(it);
        });
        const orderedKeys = _COURSE_ORDER.filter(k => grouped[k] && grouped[k].length);
        Object.keys(grouped).forEach(k => { if (!orderedKeys.includes(k)) orderedKeys.push(k); });
        itemsHtml = orderedKeys.map(k => {
          const label = _COURSE_LABEL[k] || (k.charAt(0).toUpperCase() + k.slice(1));
          const safeLabel = (() => { const e = document.createElement('div'); e.textContent = label; return e.innerHTML; })();
          return `<div class="tkt-course-hdr" style="padding:6px 16px;font-size:11px;font-weight:700;color:var(--k-text-3,var(--text-3));text-transform:uppercase;letter-spacing:0.04em;background:rgba(17,24,39,0.02);">${safeLabel}</div>`
               + grouped[k].map(renderItem).join('');
        }).join('');
      } else {
        itemsHtml = items.map(renderItem).join('');
      }

      const tableName = (() => { const e = document.createElement('div'); e.textContent = o.table_name || o.table_id || '#'; return e.innerHTML; })();
      const tblCls = tableName;
      let metaPax;
      if (o._is_delivery) {
        const addrEl = document.createElement('div');
        addrEl.textContent = o.address || '';
        const safeAddr = addrEl.innerHTML;
        const typeLabel = o.order_type === 'recoger' ? '🛍️ Recoger' : '🛵 Domicilio';
        metaPax = `<span class="tag">${typeLabel}</span>` + (o.address ? `<span class="tag" style="font-size:11px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${safeAddr}</span>` : '');
      } else {
        metaPax = o.guests ? `<span class="tag">Mesa · ${o.guests}p</span>` : `<span class="tag">Mesa</span>`;
      }
      const srcBadge = _srcBadge(o);
      const hotBadge = mins >= 12 ? `<span class="tag hot">🔥 Retrasado</span>` : '';

      const doneStyle = isDone ? 'opacity:0.55;' : '';
      const selectedStyle = isSelected ? 'box-shadow:0 0 0 2px var(--brand);' : '';

      return `<article class="tkt ${cls} ${isDone ? 'done' : ''}" data-id="${_esc(o.id)}" data-idx="${idx}" data-delivery="${o._is_delivery ? '1' : '0'}" style="${doneStyle}${selectedStyle}">
        <div class="tkt-head">
          <div class="tkt-table"><span class="n">${tblCls}</span><span class="sub">#${_esc(o.public_code || String(o.id).slice(0,6))}</span></div>
          <div class="tkt-time">${_fmtTime(mins, secs)}</div>
        </div>
        <div class="tkt-meta">${metaPax}${hotBadge}${srcBadge}</div>
        <div class="tkt-body">${itemsHtml}</div>
        <div class="tkt-foot">
          <button class="tkt-btn tkt-plus2" data-id="${_esc(o.id)}">+ 2 min</button>
          <button class="tkt-btn ready tkt-listo" data-id="${_esc(o.id)}">Listo ${isSelected ? '↵' : ''}</button>
        </div>
      </article>`;
    }).join('');

    wall.querySelectorAll('.tkt-item').forEach(el => {
      el.addEventListener('click', () => {
        el.classList.toggle('done');
      });
    });

    wall.querySelectorAll('.tkt-plus2').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const id = btn.dataset.id;
        _localPlusMins[id] = (_localPlusMins[id] || 0) + 2;
        mesioToast('+2 minutos (local)', 'warning', 1500);
        _renderWall(_tickets);
      });
    });

    wall.querySelectorAll('.tkt-listo').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const article = btn.closest('article');
        if (article && article.dataset.delivery === '1') {
          markListoDelivery(btn.dataset.id);
        } else {
          markListo(btn.dataset.id);
        }
      });
    });

    _updateTabCounts(orders);
  }

  function _escHtmlSafe(s) {
    const el = document.createElement('div');
    el.textContent = String(s == null ? '' : s);
    return el.innerHTML;
  }
  function _esc(s) { return _escHtmlSafe(s); }

  // ── Tab counts ───────────────────────────────────────
  function _updateTabCounts(orders) {
    const active   = orders.filter(o => o.status === 'recibido' || o.status === 'en_preparacion' || o.status === 'confirmado');
    const delayed  = active.filter(o => _elapsedMins(o.created_at, _localPlusMins[o.id]) >= 12);
    const done     = orders.filter(o => o.status === 'listo');

    const setCount = (id, n) => { const el = document.getElementById(id); if (el) el.textContent = n; };
    setCount('kds-count-active',  active.length);
    setCount('kds-count-delayed', delayed.length);
    setCount('kds-count-done',    done.length);
  }

  // ── Station filter ────────────────────────────────────
  function setStation(station) {
    _station = station;
    document.querySelectorAll('.k-tabs button').forEach(b => {
      b.classList.toggle('active', b.dataset.station === station);
    });
    loadOrders();
  }

  // ── Mark listo ────────────────────────────────────────
  async function markListo(orderId) {
    try {
      const res = await fetch(`/api/table-orders/${orderId}/status`, {
        method: 'POST', headers: mesioHeaders(), body: JSON.stringify({ status: 'listo' })
      });
      mesioTrackFetch(res.ok);
      if (!res.ok) throw new Error('status ' + res.status);
      mesioToast('✅ Listo para servir', 'success', 2000);
      loadOrders();
    } catch (err) {
      mesioToast('Error al marcar listo', 'error');
    }
  }

  async function markListoDelivery(orderId) {
    try {
      const res = await fetch(`/api/kitchen/delivery-orders/${encodeURIComponent(orderId)}/status`, {
        method: 'PATCH', headers: mesioHeaders(), body: JSON.stringify({ status: 'listo' })
      });
      mesioTrackFetch(res.ok);
      if (!res.ok) throw new Error('status ' + res.status);
      mesioToast('✅ Listo para entrega', 'success', 2000);
      loadOrders();
    } catch (err) {
      mesioToast('Error al marcar listo', 'error');
    }
  }

  // ── Load orders ───────────────────────────────────────
  async function loadOrders() {
    _selectedIdx = -1;
    try {
      const [tableRes, deliveryRes] = await Promise.all([
        fetch('/api/table-orders?station=kitchen', { headers: mesioHeaders() }),
        fetch('/api/kitchen/delivery-orders', { headers: mesioHeaders() }),
      ]);
      mesioTrackFetch(tableRes.ok);
      if (!tableRes.ok) { if (tableRes.status === 401) { window.location.href = '/login'; return; } throw new Error('status'); }
      const tableData = await tableRes.json();
      let orders = (tableData.orders || tableData || []);

      orders = orders.filter(o => !o.pending_table_validation);

      if (deliveryRes.ok) {
        const deliveryData = await deliveryRes.json();
        const deliveryOrders = (deliveryData.orders || []).map(o => ({
          ...o,
          _is_delivery: true,
          table_name: o.order_type === 'recoger' ? '🛍️ Recoger' : '🛵 Domicilio',
          guests: null,
        }));
        orders = [...orders, ...deliveryOrders];
      }

      orders.sort((a, b) => new Date(a.created_at) - new Date(b.created_at));

      if (_station === 'delayed') {
        orders = orders.filter(o => o.status !== 'listo' && o.status !== 'entregado' && _elapsedMins(o.created_at, _localPlusMins[o.id] || 0) >= 12);
      } else if (_station === 'done') {
        orders = orders.filter(o => o.status === 'listo');
      } else {
        orders = orders.filter(o => o.status !== 'entregado');
      }

      _updateStats(orders);
      _renderWall(orders);
      _detectNewOrders(orders);
    } catch (err) {
      mesioTrackFetch(false);
    }
  }

  // ── New-order detection ────────────────────────────
  const _ACTIVE_STATUSES = new Set(['pendiente', 'confirmado', 'recibido', 'en_preparacion']);

  function _detectNewOrders(orders) {
    const currentIds = new Set(orders.filter(o => _ACTIVE_STATUSES.has(o.status)).map(o => String(o.id)));

    if (_seenOrderIds === null) {
      _seenOrderIds = currentIds;
      return;
    }

    const newOrders = orders.filter(o => _ACTIVE_STATUSES.has(o.status) && !_seenOrderIds.has(String(o.id)));
    _seenOrderIds = currentIds;

    if (!newOrders.length) return;

    if (!document.hidden && _soundEnabled()) _playBeep();

    const count = newOrders.length;
    if (count === 1) {
      const o = newOrders[0];
      const items = Array.isArray(o.items) ? o.items : [];
      const label = o._is_delivery
        ? (o.order_type === 'recoger' ? 'Recoger' : 'Domicilio')
        : (o.table_name || o.table_id || 'Mesa');
      const body = `${label} · ${items.length} plato${items.length !== 1 ? 's' : ''}`;
      mesioNotify('Nueva orden — Cocina', body);
    } else {
      mesioNotify('Nuevas órdenes — Cocina', `${count} nuevas órdenes llegaron`);
    }
  }

  // ── Keyboard shortcuts ────────────────────────────────
  function _onKeydown(e) {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') return;

    if (e.key === '/') {
      e.preventDefault();
      const si = document.getElementById('kds-search');
      const row = document.getElementById('kds-search-row');
      if (row) row.classList.add('open');
      if (si) si.focus();
      return;
    }
    const num = parseInt(e.key, 10);
    if (num >= 1 && num <= 7) {
      _selectedIdx = Math.min(num - 1, _tickets.length - 1);
      _renderWall(_tickets);
      return;
    }
    if (e.key === 'Enter' && _selectedIdx >= 0 && _tickets[_selectedIdx]) {
      const t = _tickets[_selectedIdx];
      if (t._is_delivery) { markListoDelivery(t.id); } else { markListo(t.id); }
      return;
    }
    if ((e.key === 't' || e.key === 'T') && _selectedIdx >= 0 && _tickets[_selectedIdx]) {
      const id = _tickets[_selectedIdx].id;
      _localPlusMins[id] = (_localPlusMins[id] || 0) + 2;
      mesioToast('+2 minutos (local)', 'warning', 1500);
      _renderWall(_tickets);
      return;
    }
  }

  // ── Notification opt-in ───────────────────────────────
  async function _initNotifyOptin() {
    const btn = document.getElementById('kds-notify-optin');
    if (!btn) return;
    if ('Notification' in window && Notification.permission === 'default') {
      btn.hidden = false;
      btn.addEventListener('click', async () => {
        const result = await mesioRequestNotificationPermission();
        btn.hidden = true;
        if (result === 'granted') {
          mesioToast('Alertas activadas', 'success', 2500);
          mesioDing();
        }
      }, { once: true });
    }
  }

  // ── mount / unmount ───────────────────────────────────
  function mount(container) {
    container.innerHTML = TEMPLATE;
    _tickets = [];
    _selectedIdx = -1;
    _station = 'all';
    _localPlusMins = {};
    _seenOrderIds = null;

    _tc();
    _trackInterval(mesioInterval(_tc, 10000));

    const si = document.getElementById('kds-search');
    if (si) {
      si.addEventListener('input', () => {
        const q = si.value.toLowerCase();
        document.querySelectorAll('.tkt').forEach(tkt => {
          const text = tkt.textContent.toLowerCase();
          tkt.style.display = (!q || text.includes(q)) ? '' : 'none';
        });
      });
      si.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
          e.preventDefault();
          si.value = '';
          si.blur();
          const row = document.getElementById('kds-search-row');
          if (row) row.classList.remove('open');
          loadOrders();
        }
      });
    }

    document.querySelectorAll('.k-tabs button').forEach(btn => {
      btn.addEventListener('click', () => setStation(btn.dataset.station || 'all'));
    });

    _boundKeydown = _onKeydown;
    document.addEventListener('keydown', _boundKeydown);

    _boundKdsScale = _applyKdsScale;
    _applyKdsScale();
    window.addEventListener('resize', _boundKdsScale);

    const soundBtn = document.getElementById('kds-sound-toggle');
    if (soundBtn) {
      _updateSoundBtn(soundBtn);
      soundBtn.addEventListener('click', () => {
        _setSoundEnabled(!_soundEnabled());
        _updateSoundBtn(soundBtn);
        if (_soundEnabled()) mesioDing(); // audible confirmation + unlocks WebAudio via this click gesture
      });
    }

    _initNotifyOptin();
    loadOrders();
    _trackInterval(mesioLiveInterval(loadOrders, 15000));

    // Real-time invalidation — SSE events call loadOrders() immediately;
    // mesioLiveInterval above is just the 60s safety net while connected.
    if (window.MesioRealtime) {
      ['table_order.created', 'table_order.updated', 'order.created', 'order.updated', 'resync'].forEach((topic) => {
        _rtUnsubs.push(MesioRealtime.on(topic, loadOrders));
      });
    }
  }

  function unmount(container) {
    _intervalHandles.forEach(function (id) { clearInterval(id); });
    _intervalHandles = [];
    _rtUnsubs.forEach(function (off) { off(); });
    _rtUnsubs = [];
    if (_boundKeydown) { document.removeEventListener('keydown', _boundKeydown); _boundKeydown = null; }
    if (_boundKdsScale) { window.removeEventListener('resize', _boundKdsScale); _boundKdsScale = null; }
    if (container) container.innerHTML = '';
  }

  window.MesioStaffSections = window.MesioStaffSections || {};
  window.MesioStaffSections.kitchen = { mount: mount, unmount: unmount };
})();
