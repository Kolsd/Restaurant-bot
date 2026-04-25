/* ═══════════════════════════════════════════════════
   Mesio — Mesero v2
   Auto-refresh 20s · zone filter · waiter alert banner
   ═══════════════════════════════════════════════════ */

// ── Auth guard ──────────────────────────────────────
const _token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token');
if (!_token) { window.location.href = '/login'; }

const _hdr = mesioHeaders;
const _staffId = localStorage.getItem('rb_staff_id') || null;

// ── State ───────────────────────────────────────────
let _currentZone = 'all';
let _allTables = [];

// ── XSS helper ──────────────────────────────────────
function _esc(s) {
  const el = document.createElement('div');
  el.textContent = String(s == null ? '' : s);
  return el.innerHTML;
}

// ── Elapsed time helper ──────────────────────────────
function _elapsed(isoStr) {
  if (!isoStr) return '';
  const iso = isoStr.endsWith('Z') ? isoStr : isoStr + 'Z';
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${mins} min`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

// ── Zone filter ──────────────────────────────────────
function setZone(zone) {
  _currentZone = zone;
  document.querySelectorAll('.m-zone-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.zone === zone);
    btn.setAttribute('aria-selected', btn.dataset.zone === zone ? 'true' : 'false');
  });
  _applyZoneFilter();
}

function _applyZoneFilter() {
  document.querySelectorAll('.m-tbl').forEach(tile => {
    const z = tile.dataset.zone || '';
    const show = _currentZone === 'all' || z === _currentZone;
    tile.classList.toggle('zone-hidden', !show);
  });
}

// ── Channel badge: origen del pedido (bot WhatsApp vs POS vs QR) ──
function _channelBadge(channel) {
  if (!channel) return '';
  const map = {
    whatsapp_bot: { icon: '💬', title: 'WhatsApp bot' },
    qr_pickup:    { icon: '📱', title: 'QR / pickup' },
    web:          { icon: '🌐', title: 'Web' },
    pos:          { icon: '🧾', title: 'POS' },
    manual:       { icon: '✋', title: 'Manual' },
  };
  const m = map[channel];
  if (!m) return '';
  return `<div class="m-tbl-channel" title="${m.title}" aria-label="${m.title}">${m.icon}</div>`;
}

// ── Status → CSS class + label ───────────────────────
function _tableState(t) {
  if (t.has_waiter_alert ?? false) return { cls: 'alert', label: '🙋 Llamó al mesero' };
  if (!(t.session_active ?? false)) return { cls: '', label: 'Libre' };
  const orders = t.pending_orders || [];
  const anyListo = orders.some(s => s === 'listo');
  const anyBill  = t.has_open_check ?? false;
  if (anyBill)   return { cls: 'fac',  label: 'Facturando' };
  if (anyListo)  return { cls: 'occ',  label: 'Comiendo' };
  if (orders.length) return { cls: 'sent', label: 'Esperando' };
  return { cls: 'sent', label: 'Sentados' };
}

// ── Render tables ─────────────────────────────────────
function _renderTables(tables) {
  _allTables = tables;
  const floor = document.getElementById('mesero-floor');
  if (!floor) return;

  if (!tables.length) {
    floor.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-3);">No hay mesas configuradas.</div>';
    return;
  }

  floor.innerHTML = tables.map(t => {
    const { cls, label } = _tableState(t);
    const name  = _esc(t.name || t.table_name || String(t.id));
    const cap   = _esc(String(t.capacity || ''));
    const zone  = t.zone || '';
    const total = t.current_total != null ? mesioFmt(t.current_total) : '';
    const sinceStr = t.session_started_at ? _elapsed(t.session_started_at) : '';
    const alertLabel = (t.has_waiter_alert ?? false) ? '<div class="m-tbl-alert">Atenci\u00f3n</div>' : '';
    const checkBadge = (t.has_open_check ?? false) ? '<div class="m-tbl-check-badge">Cobrando</div>' : '';
    const capBadge = cap ? `<div class="m-tbl-cap ${cls === 'alert' ? 'alert' : ''}">${cls === 'alert' ? '🙋' : cap + 'p'}</div>` : '';

    // Waiter attribution + channel: show WHO is serving (if known) and HOW the session started.
    const waiterLine = t.waiter_name
      ? `<div class="m-tbl-waiter" title="Atiende">Att: ${_esc(t.waiter_name)}</div>`
      : '';
    const channelBadge = _channelBadge(t.channel);

    const tileAriaLabel = `Mesa ${name}, ${label}`;
    return `<div class="m-tbl ${cls}" role="button" tabindex="0" aria-label="${_esc(tileAriaLabel)}" data-id="${_esc(String(t.id))}" data-zone="${_esc(zone)}">
      ${capBadge}
      ${channelBadge}
      <div class="m-tbl-num">${name}</div>
      <div class="m-tbl-state">${_esc(label)}</div>
      <div class="m-tbl-body">
        ${total ? `<div class="m-tbl-total">${total}</div>` : ''}
        ${sinceStr ? `<div class="m-tbl-time">${sinceStr}</div>` : ''}
        ${waiterLine}
        ${alertLabel}
        ${checkBadge}
      </div>
    </div>`;
  }).join('');

  // Click + keyboard → open POS
  floor.querySelectorAll('.m-tbl').forEach(tile => {
    tile.addEventListener('click', () => {
      const id = tile.dataset.id;
      const t = tables.find(x => String(x.id) === id);
      if (t) openTable(t);
    });
    tile.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        tile.click();
      }
    });
  });

  _applyZoneFilter();
  _updateActionBar();
}

// ── Open table → navigate to caja with table pre-loaded ──
function openTable(t) {
  sessionStorage.setItem('caja_open_table', JSON.stringify({ id: t.id, name: t.name || t.table_name }));
  window.location.href = `/caja?tableId=${encodeURIComponent(t.id)}`;
}

// ── Waiter alerts banner ─────────────────────────────
async function _loadAlerts() {
  try {
    const res = await fetch('/api/waiter-alerts?resolved=false', { headers: _hdr() });
    if (!res.ok) return;
    const data = await res.json();
    const alerts = data.alerts || data || [];
    const banner = document.getElementById('mesero-alert-banner');
    if (!banner) return;
    if (!alerts.length) { banner.classList.add('hidden'); return; }
    banner.classList.remove('hidden');
    const first = alerts[0];
    const tableEl = document.createElement('strong');
    tableEl.textContent = first.table_name || `Mesa ${first.table_id}`;
    banner.innerHTML = '';
    const icon = document.createTextNode('🔔 ');
    banner.appendChild(icon);
    banner.appendChild(tableEl);
    const msg = document.createTextNode(` llamó al mesero · ${alerts.length} alerta${alerts.length > 1 ? 's' : ''}`);
    banner.appendChild(msg);
    const btn = document.createElement('button');
    btn.className = 'm-btn m-btn--sm m-btn--ghost';
    btn.style.cssText = 'margin-left:auto;font-size:11px;min-height:28px;';
    btn.textContent = 'Ver todas';
    banner.appendChild(btn);
    btn.addEventListener('click', e => { e.stopPropagation(); _showAllAlerts(alerts); });
  } catch (_) { /* non-critical */ }
}

function _showAllAlerts(alerts) {
  const existing = document.getElementById('mesero-alerts-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'mesero-alerts-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-label', 'Alertas de mesero');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:9999;';

  const card = document.createElement('div');
  card.style.cssText = 'background:var(--surface-2,#1e2535);border-radius:12px;padding:24px;min-width:280px;max-width:380px;width:90%;';

  const title = document.createElement('h3');
  title.style.cssText = 'margin:0 0 16px;font-size:16px;color:var(--text-1,#fff);';
  title.textContent = `${alerts.length} alerta${alerts.length > 1 ? 's' : ''} activa${alerts.length > 1 ? 's' : ''}`;
  card.appendChild(title);

  const list = document.createElement('ul');
  list.style.cssText = 'list-style:none;margin:0 0 16px;padding:0;display:flex;flex-direction:column;gap:8px;';
  alerts.forEach(a => {
    const li = document.createElement('li');
    li.style.cssText = 'background:var(--surface-3,#252d40);border-radius:8px;padding:10px 12px;display:flex;gap:8px;align-items:center;';
    const badge = document.createElement('span');
    badge.style.cssText = 'font-size:11px;font-weight:600;color:var(--warn,#f59e0b);white-space:nowrap;';
    badge.textContent = a.table_name || `Mesa ${a.table_id}`;
    const type = document.createElement('span');
    type.style.cssText = 'font-size:12px;color:var(--text-2,#94a3b8);';
    type.textContent = a.alert_type || '';
    li.appendChild(badge);
    li.appendChild(type);
    list.appendChild(li);
  });
  card.appendChild(list);

  const closeBtn = document.createElement('button');
  closeBtn.className = 'm-btn m-btn--sm';
  closeBtn.style.cssText = 'width:100%;';
  closeBtn.textContent = 'Cerrar';
  closeBtn.addEventListener('click', () => modal.remove());
  card.appendChild(closeBtn);

  modal.appendChild(card);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
  closeBtn.focus();
}

// ── Request help alert for a table ───────────────────
async function _callAlert(tableId, tableName) {
  try {
    const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    const botNumber = restaurant.whatsapp_number || '';
    const res = await fetch('/api/waiter-alerts/admin-call', {
      method: 'POST',
      headers: _hdr(),
      body: JSON.stringify({ table_name: tableName, bot_number: botNumber, alert_type: 'help' })
    });
    if (res.ok) {
      mesioToast('Alerta enviada', 'success', 2000);
    } else {
      mesioToast('No se pudo enviar la alerta', 'error');
    }
  } catch (_) {
    mesioToast('Error de conexi\u00f3n', 'error');
  }
}

// ── Bottom action bar ─────────────────────────────────
async function _updateActionBar() {
  if (!_staffId) return;
  try {
    // Staff performance for this week
    const res = await fetch(`/api/stats/staff-performance?staff_id=${encodeURIComponent(_staffId)}&weeks=1`, { headers: _hdr() });
    if (!res.ok) return;
    const data = await res.json();

    const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setVal('m-stat-ventas', data.total_sales != null ? mesioFmt(data.total_sales) : '—');
    setVal('m-stat-mesas', data.tables_served != null ? String(data.tables_served) : '—');
    setVal('m-stat-ticket', data.avg_ticket != null ? mesioFmt(data.avg_ticket) : '—');
  } catch (_) { /* non-critical */ }

  try {
    const res2 = await fetch('/api/stats/tips-pool', { headers: _hdr() });
    if (!res2.ok) return;
    const data2 = await res2.json();
    const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setVal('m-stat-propinas', data2.my_pool != null ? mesioFmt(data2.my_pool) : '—');
  } catch (_) { /* non-critical */ }
}

// ── Load tables ───────────────────────────────────────
async function loadTables() {
  try {
    const res = await fetch('/api/pos/tables-status', { headers: _hdr() });
    mesioTrackFetch(res.ok);
    if (!res.ok) { if (res.status === 401) { window.location.href = '/login'; return; } throw new Error(); }
    const data = await res.json();
    const tables = data.tables || data || [];
    _renderTables(tables);
    const subEl = document.getElementById('mesero-sub');
    if (subEl) {
      const n = tables.length;
      const active = tables.filter(t => t.session_active || t.has_open_check).length;
      subEl.textContent = `${n} mesa${n === 1 ? '' : 's'} · ${active} activa${active === 1 ? '' : 's'}`;
    }
    _loadAlerts();
  } catch (_) {
    mesioTrackFetch(false);
  }
}

// ── Zone tabs setup ───────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.m-zone-btn').forEach(btn => {
    btn.addEventListener('click', () => setZone(btn.dataset.zone || 'all'));
  });

  loadTables();
  mesioInterval(loadTables, 20000);
});
