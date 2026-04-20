/* ═══════════════════════════════════════════════════
   Mesio — Bar KDS v2
   Auto-refresh 15s · keyboard 1-7 select, Enter=listo, T=+2min
   ═══════════════════════════════════════════════════ */

// ── Auth guard ──────────────────────────────────────
const _token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token');
if (!_token) { window.location.href = '/login'; }

const _hdr = { 'Authorization': 'Bearer ' + _token, 'Content-Type': 'application/json' };

// ── State ───────────────────────────────────────────
let _tickets = [];
let _selectedIdx = -1;
let _localPlusMins = {};

// ── Clock ───────────────────────────────────────────
(function _initClock() {
  function _tc() {
    const el = document.getElementById('bar-clock');
    if (el) el.textContent = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
  }
  _tc(); setInterval(_tc, 10000);
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
      // TODO: server-side timer extension
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
    // Bar orders come from table_orders — use same KDS endpoint
    const res = await fetch(`/api/table-orders/${orderId}/status`, {
      method: 'POST', headers: _hdr, body: JSON.stringify({ status: 'listo' })
    });
    mesioTrackFetch(res.ok);
    if (!res.ok) throw new Error('status ' + res.status);
    mesioToast('✅ Bebidas listas', 'success', 2000);
    loadOrders();
  } catch (err) {
    mesioToast('Error al marcar listo', 'error');
  }
}

// ── Load orders (bar filter) ─────────────────────────
async function loadOrders() {
  try {
    const res = await fetch('/api/table-orders', { headers: _hdr });
    mesioTrackFetch(res.ok);
    if (!res.ok) { if (res.status === 401) { window.location.href = '/login'; return; } throw new Error('status'); }
    const data = await res.json();
    let orders = data.orders || data || [];

    // Keep only bar-relevant orders: filter by category 'bebidas' or channel.
    // The backend doesn't yet expose per-station category filtering, so we show
    // all active non-delivered orders — staff can visually identify drinks.
    // TODO: add `?station=bar` query param when backend supports it.
    orders = orders.filter(o => o.status !== 'entregado');

    _updateStats(orders);
    _renderQueue(orders);
    loadInventory();
  } catch (err) {
    mesioTrackFetch(false);
  }
}

// ── Load inventory sidebar ────────────────────────────
async function loadInventory() {
  try {
    const res = await fetch('/api/stats/inventory-critical', { headers: _hdr });
    if (!res.ok) return;
    const data = await res.json();
    const items = data.items || data || [];
    renderInventory(items);
  } catch (_) { /* non-critical */ }
}

function renderInventory(items) {
  const el = document.getElementById('bar-inv-list');
  if (!el) return;
  if (!items.length) { el.innerHTML = '<div style="font-size:12px;color:var(--b-text-3);">Stock OK</div>'; return; }
  el.innerHTML = items.map(item => {
    const pct = item.capacity > 0 ? Math.min(100, Math.round(item.stock / item.capacity * 100)) : 50;
    const lvlCls = pct < 20 ? 'crit' : pct < 50 ? 'lo' : '';
    return `<div class="inv-row">
      <div>
        <div class="inv-name">${_esc(item.name || item.sku || '')}</div>
        <div class="inv-bar"><div class="inv-fill ${lvlCls}" style="width:${pct}%"></div></div>
      </div>
      <div class="inv-qty">${_esc(String(item.stock ?? '—'))}</div>
    </div>`;
  }).join('');
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

// ── Boot ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadOrders();
  mesioInterval(loadOrders, 15000);
});
