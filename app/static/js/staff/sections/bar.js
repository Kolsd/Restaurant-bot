/* ═══════════════════════════════════════════════════
   Mesio — Staff App / Bar section (KDS)
   Ported from the old /bar page (app/static/html/bar.html +
   app/static/js/pages/bar.js) into a mount()/unmount() module for the
   unified Staff App shell. Business logic is UNCHANGED from the original
   bar.js — only bootstrap/cleanup are new (auto-refresh 15s, keyboard
   1-7 select / Enter=listo / T=+2min).
   ═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var TEMPLATE = `
<div class="mesio-sec-bar">
<div class="bar-layout">

  <!-- ── Topbar ── -->
  <header class="bar-top">
    <div class="bar-brand">
      <div class="bar-mark">M</div>
      <span>Bar</span>
    </div>
    <div class="bar-sep"></div>
    <div class="bar-station">Estación <strong id="bar-station-name">Barra principal</strong></div>
    <div class="bar-pill"><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--b-purple);margin-right:4px;animation:blink 2s infinite;"></span>En línea</div>
    <div class="bar-top-right">
      <div class="bar-kstat">
        <div class="bar-kstat-l">En cola</div>
        <div class="bar-kstat-v" id="bar-stat-queue">—</div>
      </div>
      <div class="bar-kstat">
        <div class="bar-kstat-l">Prom.</div>
        <div class="bar-kstat-v" id="bar-stat-avg">—:—</div>
      </div>
      <div class="bar-clock" id="bar-clock">--:--</div>
      <button id="kds-sound-toggle" type="button" aria-pressed="false" style="padding:5px 10px;font-size:11px;border:1px solid var(--b-border,var(--border));border-radius:6px;background:var(--b-surface-2,var(--surface));color:var(--b-text-2,#a09bc0);font-family:inherit;cursor:pointer;">
        🔈 Activar sonido
      </button>
      <button id="kds-notify-optin" style="display:none;padding:5px 10px;font-size:11px;border:1px solid var(--b-border,var(--border));border-radius:6px;background:var(--b-surface-2,var(--surface));color:var(--b-text-2,#a09bc0);font-family:inherit;cursor:pointer;" hidden>
        Activar alertas
      </button>
    </div>
  </header>

  <!-- ── Main queue ── -->
  <main class="bar-main">
    <div class="queue-head">
      <h2>Cola de preparación</h2>
      <div class="queue-sub" id="bar-queue-sub">Cargando…</div>
    </div>
    <div class="queue-grid" id="bar-queue">
      <div class="bar-empty">Cargando bebidas…</div>
    </div>
  </main>

  <!-- ── Right sidebar ── -->
  <aside class="bar-side">

    <!-- Live inventory -->
    <section>
      <div class="side-section-title">Inventario crítico</div>
      <div id="bar-inv-list">
        <div style="font-size:12px;color:var(--b-text-3);">Cargando…</div>
      </div>
    </section>

    <!-- Top of the shift -->
    <section>
      <div class="side-section-title">Top en la cola actual</div>
      <div id="bar-pop-list">
        <div style="font-size:12px;color:var(--b-text-3);">Cargando…</div>
      </div>
    </section>

  </aside>

  <!-- ── Footer hint bar ── -->
  <footer class="bar-foot" style="grid-column:1/-1;">
    <span class="hint">
      <kbd>1</kbd>…<kbd>7</kbd> seleccionar ·
      <kbd>↵</kbd> marcar listo ·
      <kbd>T</kbd> +2 min
    </span>
    <div style="margin-left:auto;" class="m-live-status online">
      <div class="dot"></div><span class="label">En vivo</span>
    </div>
  </footer>

</div><!-- /.bar-layout -->
</div><!-- /.mesio-sec-bar -->
`;

  // ── Cleanup tracking ─────────────────────────────────
  var _intervalHandles = [];
  function _trackInterval(id) { _intervalHandles.push(id); return id; }

  // ── State ───────────────────────────────────────────
  let _tickets = [];
  let _selectedIdx = -1;
  let _localPlusMins = {};
  let _seenOrderIds = null;   // Set of order IDs seen on previous polls; null = first load (suppress alert)
  var _rtUnsubs = [];         // MesioRealtime.on() unsubscribe fns, cleared on unmount
  var _lastBeepAt = 0;        // throttle: never beep more than once every 2s

  // ── Sound preference (per device, opt-in — see kitchen.js for the same
  // pattern; browsers block autoplay until a user gesture). ─────────────
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
    const el = document.getElementById('bar-clock');
    if (el) el.textContent = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
  }

  // ── XSS safe text ───────────────────────────────────
  function _esc(s) {
    const el = document.createElement('div');
    el.textContent = String(s == null ? '' : s);
    return el.innerHTML;
  }

  // ── Elapsed minutes ──────────────────────────────────
  function _elapsedMins(createdAt, extra) {
    const iso = createdAt.endsWith('Z') ? createdAt : createdAt + 'Z';
    return Math.floor((Date.now() - new Date(iso).getTime()) / 60000) + (extra || 0);
  }

  // ── Format mm:ss ─────────────────────────────────────
  function _fmtTime(createdAt, extra) {
    const iso = createdAt.endsWith('Z') ? createdAt : createdAt + 'Z';
    const totalSecs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000) + (extra || 0) * 60;
    const mins = Math.floor(totalSecs / 60);
    const secs = totalSecs % 60;
    return `${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}`;
  }

  // ── Stats ────────────────────────────────────────────
  function _updateStats(orders) {
    const active = orders.filter(o => o.status !== 'listo' && o.status !== 'entregado');
    let totalMs = 0;
    active.forEach(o => {
      const iso = o.created_at.endsWith('Z') ? o.created_at : o.created_at + 'Z';
      totalMs += Date.now() - new Date(iso).getTime();
    });
    const avg = active.length ? Math.floor(totalMs / active.length / 60000) : 0;
    const avgSecs = active.length ? Math.floor((totalMs / active.length / 1000) % 60) : 0;

    const qEl = document.getElementById('bar-stat-queue');
    const pEl = document.getElementById('bar-stat-avg');
    if (qEl) qEl.textContent = active.length;
    if (pEl) { pEl.textContent = `${String(avg).padStart(2,'0')}:${String(avgSecs).padStart(2,'0')}`; pEl.className = 'bar-kstat-v ' + (avg < 5 ? '' : 'text-warn'); }
    const subEl = document.getElementById('bar-queue-sub');
    if (subEl) {
      subEl.textContent = active.length === 0 ? 'Sin bebidas pendientes' : `${active.length} en cola`;
    }
  }

  // ── Render queue ─────────────────────────────────────
  function _renderQueue(orders) {
    _tickets = orders;
    const grid = document.getElementById('bar-queue');
    if (!grid) return;

    if (!orders.length) {
      grid.innerHTML = '<div class="bar-empty">Sin bebidas en cola ☕</div>';
      return;
    }

    grid.innerHTML = orders.map((o, idx) => {
      const extra = _localPlusMins[o.id] || 0;
      const mins = _elapsedMins(o.created_at, extra);
      const warnCls = mins >= 8 ? 'warn' : mins < 2 ? 'new' : '';
      const isDone = o.status === 'listo';
      const isSelected = idx === _selectedIdx;

      const items = Array.isArray(o.items) ? o.items : [];
      const itemsHtml = items.map(item => {
        const safeName = _esc(item.name || '');
        const safeQty = _esc(String(item.quantity || item.qty || 1));
        const specHtml = item.notes ? `<div class="dr-spec">${_esc(item.notes)}</div>` : '';
        return `<div class="dr-item" data-done="0">
          <div class="dr-qty">${safeQty}</div>
          <div style="flex:1;"><div class="dr-name">${safeName}</div>${specHtml}</div>
          <div class="dr-check"></div>
        </div>`;
      }).join('');

      const src = _esc(o.table_name || o.table_id || '#');
      const guests = o.guests ? `<span class="sub">${_esc(String(o.guests))}p</span>` : '';
      const doneStyle = isDone ? 'opacity:0.55;' : '';
      const selStyle = isSelected ? 'box-shadow:0 0 0 2px var(--b-purple);' : '';

      return `<article class="dr ${warnCls} ${isDone ? 'done' : ''} ${isSelected ? 'selected' : ''}" data-id="${_esc(o.id)}" data-idx="${idx}" style="${doneStyle}${selStyle}">
        <div class="dr-head">
          <div class="dr-src">${src}${guests}</div>
          <div class="dr-time">${_fmtTime(o.created_at, extra)}</div>
        </div>
        <div class="dr-items">${itemsHtml}</div>
        <div class="dr-foot">
          <button class="dr-btn dr-plus2" data-id="${_esc(o.id)}">+2 min</button>
          <button class="dr-btn ready dr-listo" data-id="${_esc(o.id)}">Listo${isSelected ? ' ↵' : ''}</button>
        </div>
      </article>`;
    }).join('');

    grid.querySelectorAll('.dr-item').forEach(el => {
      el.addEventListener('click', () => el.classList.toggle('done'));
    });

    grid.querySelectorAll('.dr-plus2').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        _localPlusMins[btn.dataset.id] = (_localPlusMins[btn.dataset.id] || 0) + 2;
        mesioToast('+2 min (local)', 'warning', 1500);
        _renderQueue(_tickets);
      });
    });

    grid.querySelectorAll('.dr-listo').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        markListo(btn.dataset.id);
      });
    });
  }

  // ── Mark listo ────────────────────────────────────────
  async function markListo(orderId) {
    try {
      const res = await fetch(`/api/table-orders/${orderId}/status`, {
        method: 'POST', headers: mesioHeaders(), body: JSON.stringify({ status: 'listo' })
      });
      mesioTrackFetch(res.ok);
      if (!res.ok) throw new Error('status ' + res.status);
      mesioToast('✅ Bebidas listas', 'success', 2000);
      _selectedIdx = -1;
      loadOrders();
    } catch (err) {
      mesioToast('Error al marcar listo', 'error');
    }
  }

  // ── Load orders (bar filter) ─────────────────────────
  async function loadOrders() {
    try {
      const res = await fetch('/api/table-orders?station=bar', { headers: mesioHeaders() });
      mesioTrackFetch(res.ok);
      if (!res.ok) { if (res.status === 401) { window.location.href = '/login'; return; } throw new Error('status'); }
      const data = await res.json();
      let orders = data.orders || data || [];

      orders = orders.filter(o => o.status !== 'entregado');

      _updateStats(orders);
      _renderQueue(orders);
      _detectNewOrders(orders);
      loadInventory();
      renderPopularBeverages(orders);
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
      const label = o.table_name || o.table_id || 'Mesa';
      const body = `${label} · ${items.length} bebida${items.length !== 1 ? 's' : ''}`;
      mesioNotify('Nueva orden — Bar', body);
    } else {
      mesioNotify('Nuevas órdenes — Bar', `${count} nuevas órdenes llegaron`);
    }
  }

  // ── Load inventory sidebar ────────────────────────────
  async function loadInventory() {
    try {
      const res = await fetch('/api/stats/inventory-critical', { headers: mesioHeaders() });
      if (res.status === 401) { window.location.href = '/login'; return; }
      if (!res.ok) return;
      const data = await res.json();
      renderInventory(data.alerts || []);
    } catch (_) { /* non-critical */ }
  }

  function renderInventory(alerts) {
    const el = document.getElementById('bar-inv-list');
    if (!el) return;
    if (!alerts.length) {
      el.innerHTML = '<div style="font-size:12px;color:var(--b-text-3);">Stock OK</div>';
      return;
    }
    el.innerHTML = alerts.map(item => {
      const cur = Number(item.current_stock ?? 0);
      const min = Number(item.min_stock ?? 0);
      const pct = min > 0 ? Math.min(100, Math.round((cur / min) * 100)) : 0;
      const lvlCls = item.severity === 'critical' ? 'crit' : 'lo';
      const qty = `${cur}${item.unit ? ' ' + item.unit : ''}`;
      return `<div class="inv-row">
        <div>
          <div class="inv-name">${_esc(item.ingredient || '')}</div>
          <div class="inv-bar"><div class="inv-fill ${lvlCls}" style="width:${pct}%"></div></div>
        </div>
        <div class="inv-qty">${_esc(qty)}</div>
      </div>`;
    }).join('');
  }

  // ── Top beverages this shift ──────────────────────────
  function renderPopularBeverages(orders) {
    const el = document.getElementById('bar-pop-list');
    if (!el) return;
    const counts = new Map();
    (orders || []).forEach(o => {
      (o.items || []).forEach(it => {
        const name = (it.name || it.dish || '').trim();
        if (!name) return;
        const qty = Number(it.qty || it.quantity || 1);
        counts.set(name, (counts.get(name) || 0) + qty);
      });
    });
    const top = [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
    if (!top.length) {
      el.innerHTML = '<div style="font-size:12px;color:var(--b-text-3);">Cola vacía.</div>';
      return;
    }
    el.innerHTML = top.map(([name, qty]) => `
      <div class="inv-row">
        <div class="inv-name">${_esc(name)}</div>
        <div class="inv-qty">${qty}×</div>
      </div>`).join('');
  }

  // ── Keyboard shortcuts ────────────────────────────────
  function _onKeydown(e) {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') return;

    const num = parseInt(e.key, 10);
    if (num >= 1 && num <= 7) {
      _selectedIdx = Math.min(num - 1, _tickets.length - 1);
      _renderQueue(_tickets);
      return;
    }
    if (e.key === 'Enter' && _selectedIdx >= 0 && _tickets[_selectedIdx]) {
      markListo(_tickets[_selectedIdx].id);
      return;
    }
    if ((e.key === 't' || e.key === 'T') && _selectedIdx >= 0 && _tickets[_selectedIdx]) {
      _localPlusMins[_tickets[_selectedIdx].id] = (_localPlusMins[_tickets[_selectedIdx].id] || 0) + 2;
      mesioToast('+2 min (local)', 'warning', 1500);
      _renderQueue(_tickets);
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
    _localPlusMins = {};
    _seenOrderIds = null;

    _tc();
    _trackInterval(mesioInterval(_tc, 10000));

    document.addEventListener('keydown', _onKeydown);

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
    document.removeEventListener('keydown', _onKeydown);
    if (container) container.innerHTML = '';
  }

  window.MesioStaffSections = window.MesioStaffSections || {};
  window.MesioStaffSections.bar = { mount: mount, unmount: unmount };
})();
