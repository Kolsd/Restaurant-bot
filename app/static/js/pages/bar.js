/* ═══════════════════════════════════════════════════
   Mesio — Bar KDS v2
   Auto-refresh 15s · keyboard 1-7 select, Enter=listo, T=+2min
   ═══════════════════════════════════════════════════ */

// ── Auth guard ──────────────────────────────────────
const _token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token');
if (!_token) { window.location.href = '/login'; }

// ── State ───────────────────────────────────────────
let _tickets = [];
let _selectedIdx = -1;
let _localPlusMins = {};
let _seenOrderIds = null;   // Set of order IDs seen on previous polls; null = first load (suppress alert)

// ── Clock ───────────────────────────────────────────
(function _initClock() {
  function _tc() {
    const el = document.getElementById('bar-clock');
    if (el) el.textContent = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
  }
  _tc(); mesioInterval(_tc, 10000);
})();

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

  // Item toggle (local visual)
  grid.querySelectorAll('.dr-item').forEach(el => {
    el.addEventListener('click', () => el.classList.toggle('done'));
  });

  // +2 min (local only)
  grid.querySelectorAll('.dr-plus2').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      _localPlusMins[btn.dataset.id] = (_localPlusMins[btn.dataset.id] || 0) + 2;
      mesioToast('+2 min (local)', 'warning', 1500);
    });
  });

  // Listo
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
    // Initial load — populate seen set silently, no alert.
    _seenOrderIds = currentIds;
    return;
  }

  const newOrders = orders.filter(o => _ACTIVE_STATUSES.has(o.status) && !_seenOrderIds.has(String(o.id)));
  _seenOrderIds = currentIds;

  if (!newOrders.length) return;

  // Fire audio only when page is visible; notification fires regardless (OS-level).
  if (!document.hidden) mesioDing();

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
// Reads /api/stats/inventory-critical — response shape is
// {alerts:[{ingredient,unit,current_stock,min_stock,severity,affects_dishes}],
//  ok:[...]}
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
    // critical: <50% of min → red; warning: <100% → amber
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

// ── Top beverages this shift (client-side aggregation from loaded orders) ──
// No dedicated endpoint yet; we count line items from the orders currently
// displayed. Good-enough approximation: bar is a narrow station, so "this
// shift" ≈ "what's in the pending queue plus recently-ready".
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
document.addEventListener('keydown', e => {
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
  }
});

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
        mesioDing(); // confirm sound works
      }
    }, { once: true });
  }
}

// ── Boot ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  _initNotifyOptin();
  loadOrders();
  mesioInterval(loadOrders, 15000);
});
