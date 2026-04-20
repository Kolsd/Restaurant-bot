/* ═══════════════════════════════════════════════════
   Mesio — Kitchen KDS v2
   Auto-refresh 15s · keyboard 1-7 select, Enter=listo, T=+2min, /=search
   ═══════════════════════════════════════════════════ */

// ── Auth guard ──────────────────────────────────────
const _token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token');
if (!_token) { window.location.href = '/login'; }

const _hdr = { 'Authorization': 'Bearer ' + _token, 'Content-Type': 'application/json' };

// ── State ───────────────────────────────────────────
let _tickets = [];          // current displayed tickets
let _selectedIdx = -1;      // keyboard-selected ticket index (0-based)
let _station = 'all';       // station filter
let _localPlusMins = {};    // ticketId → extra minutes added locally

// ── Clock ───────────────────────────────────────────
(function _initClock() {
  function _tc() {
    const el = document.getElementById('kds-clock');
    if (el) el.textContent = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
  }
  _tc(); setInterval(_tc, 10000);
})();

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
  if (mins >= 12) return 'd';   // delayed — red blink
  if (mins >= 7)  return 'w';   // warning — amber
  return '';
}

// ── Source badge ─────────────────────────────────────
function _srcBadge(order) {
  if (order.is_delivery) {
    const type = order.order_type === 'recoger' ? '🛍️ Para Recoger' : '🛵 Domicilio';
    return `<span class="tag" style="background:#1a2033;color:#60A5FA;">${type}</span>`;
  }
  if (order.channel === 'whatsapp') return `<span class="tag src">📱 WhatsApp</span>`;
  return '';
}

// ── Render ticket wall ────────────────────────────────
function _renderWall(orders) {
  _tickets = orders;
  const wall = document.getElementById('kds-wall');
  if (!wall) return;

  if (!orders.length) {
    wall.innerHTML = '<div style="padding:60px;text-align:center;color:#7C8393;font-size:14px;">Sin órdenes activas</div>';
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
    const itemsHtml = items.map(item => {
      const el = document.createElement('div');
      el.textContent = item.name || '';
      const safeName = el.innerHTML;
      const qEl = document.createElement('div');
      qEl.textContent = String(item.quantity || item.qty || 1);
      const safeQty = qEl.innerHTML;
      const modHtml = item.notes ? `<div class="tkt-mods">${_escHtmlSafe(item.notes)}</div>` : '';
      return `<div class="tkt-item" data-done="0"><div class="tkt-qty">${safeQty}</div><div><div class="tkt-dish">${safeName}</div>${modHtml}</div><div class="tkt-check"></div></div>`;
    }).join('');

    const tableName = (() => { const e = document.createElement('div'); e.textContent = o.table_name || o.table_id || '#'; return e.innerHTML; })();
    const tblCls = o.is_delivery ? 'DOM' : tableName;
    const metaPax = o.guests ? `<span class="tag">Mesa · ${o.guests}p</span>` : `<span class="tag">Mesa</span>`;
    const srcBadge = _srcBadge(o);
    const hotBadge = mins >= 12 ? `<span class="tag hot">🔥 Retrasado</span>` : '';

    const doneStyle = isDone ? 'opacity:0.55;' : '';
    const selectedStyle = isSelected ? 'box-shadow:0 0 0 2px var(--brand);' : '';

    return `<article class="tkt ${cls} ${isDone ? 'done' : ''}" data-id="${_esc(o.id)}" data-idx="${idx}" style="${doneStyle}${selectedStyle}">
      <div class="tkt-head">
        <div class="tkt-table"><span class="n">${tblCls}</span><span class="sub">#${_esc(String(o.id).slice(0,6))}</span></div>
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

  // Bind item toggle (local visual state)
  wall.querySelectorAll('.tkt-item').forEach(el => {
    el.addEventListener('click', () => {
      el.classList.toggle('done');
    });
  });

  // Bind +2min
  wall.querySelectorAll('.tkt-plus2').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const id = btn.dataset.id;
      _localPlusMins[id] = (_localPlusMins[id] || 0) + 2;
      mesioToast('+2 minutos (local)', 'warning', 1500);
      // TODO: backend endpoint for server-side timer extension
    });
  });

  // Bind listo
  wall.querySelectorAll('.tkt-listo').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      markListo(btn.dataset.id);
    });
  });

  // Update tab counts
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
  const active   = orders.filter(o => o.status === 'recibido' || o.status === 'en_preparacion');
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
      method: 'POST', headers: _hdr, body: JSON.stringify({ status: 'listo' })
    });
    mesioTrackFetch(res.ok);
    if (!res.ok) throw new Error('status ' + res.status);
    mesioToast('✅ Listo para servir', 'success', 2000);
    loadOrders();
  } catch (err) {
    mesioToast('Error al marcar listo', 'error');
  }
}

// ── Load orders ───────────────────────────────────────
async function loadOrders() {
  try {
    const res = await fetch('/api/table-orders', { headers: _hdr });
    mesioTrackFetch(res.ok);
    if (!res.ok) { if (res.status === 401) { window.location.href = '/login'; return; } throw new Error('status'); }
    const data = await res.json();
    let orders = data.orders || data || [];

    // Filter by active station
    if (_station === 'delayed') {
      orders = orders.filter(o => _elapsedMins(o.created_at, _localPlusMins[o.id]) >= 12);
    } else if (_station === 'done') {
      orders = orders.filter(o => o.status === 'listo');
    } else {
      orders = orders.filter(o => o.status !== 'entregado');
    }

    _updateStats(orders);
    _renderWall(orders);
  } catch (err) {
    mesioTrackFetch(false);
  }
}

// ── Keyboard shortcuts ────────────────────────────────
document.addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;

  if (e.key === '/') {
    e.preventDefault();
    const si = document.getElementById('kds-search');
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
    markListo(_tickets[_selectedIdx].id);
    return;
  }
  if ((e.key === 't' || e.key === 'T') && _selectedIdx >= 0 && _tickets[_selectedIdx]) {
    const id = _tickets[_selectedIdx].id;
    _localPlusMins[id] = (_localPlusMins[id] || 0) + 2;
    mesioToast('+2 minutos (local)', 'warning', 1500);
    // TODO: server-side timer extension
    return;
  }
});

// ── Search filter ─────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
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
      if (e.key === 'Escape') { si.value = ''; si.blur(); loadOrders(); }
    });
  }

  // Station tab buttons
  document.querySelectorAll('.k-tabs button').forEach(btn => {
    btn.addEventListener('click', () => setStation(btn.dataset.station || 'all'));
  });

  loadOrders();
  mesioInterval(loadOrders, 15000);
});
