/* ═══════════════════════════════════════════════════
   Mesio — Domiciliario v2 (mobile-first, dark)
   Auto-refresh via hash check every 10s
   ═══════════════════════════════════════════════════ */

// ── Auth guard ──────────────────────────────────────
const _token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token');
if (!_token) { window.location.href = '/login'; }

// ── State ───────────────────────────────────────────
let _activeTab = 'hoy';
let _currentHash = null;
let _allOrders = [];

// ── XSS helper ──────────────────────────────────────
function _esc(s) {
  const el = document.createElement('div');
  el.textContent = String(s == null ? '' : s);
  return el.innerHTML;
}

// ── Navigation link builders ─────────────────────────
function _mapsLink(lat, lng, address) {
  const dest = (lat != null && lng != null) ? `${lat},${lng}` : encodeURIComponent(address || '');
  return `https://www.google.com/maps/dir/?api=1&destination=${dest}`;
}

function _wazeLink(address) {
  if (!address) return 'https://waze.com/ul?q=';
  const mapsMatch = address.match(/[?&]q=([-\d.]+),([-\d.]+)/);
  if (mapsMatch) return `https://waze.com/ul?ll=${mapsMatch[1]},${mapsMatch[2]}&navigate=yes`;
  const latMatch = address.match(/lat:([-\d.]+)/);
  const lonMatch = address.match(/lon:([-\d.]+)/);
  if (latMatch && lonMatch) return `https://waze.com/ul?ll=${latMatch[1]},${lonMatch[1]}&navigate=yes`;
  return `https://waze.com/ul?q=${encodeURIComponent(address)}`;
}

// ── Tab switching ────────────────────────────────────
function switchTab(tab) {
  _activeTab = tab;
  document.querySelectorAll('.dom-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  document.querySelectorAll('.tab-pane').forEach(el => {
    el.classList.toggle('active', el.dataset.pane === tab);
  });
}

// ── Render hero stats ─────────────────────────────────
function _renderHero(orders) {
  const done = orders.filter(o => o.status === 'entregado');
  const pending = orders.filter(o => o.status !== 'entregado');

  const nameEl = document.getElementById('dom-staff-name');
  if (nameEl) {
    const staffName = localStorage.getItem('rb_staff_name') || 'Tú';
    nameEl.textContent = `Hola, ${staffName} 👋`;
  }
  const setEl = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  setEl('dom-stat-delivered', done.length);
  setEl('dom-stat-pending', pending.length);
}

// ── Render active delivery ────────────────────────────
function _renderActive(orders) {
  const inRoute = orders.find(o => o.status === 'en_camino' || o.status === 'en_puerta');
  const activeEl = document.getElementById('dom-active-card');
  if (!activeEl) return;

  if (!inRoute) {
    activeEl.innerHTML = '<div class="dom-no-active">Sin entregas en curso. Revisa la cola de pedidos.</div>';
    return;
  }

  const nameParts = (inRoute.customer_name || inRoute.phone || 'C').split(' ');
  const initials = nameParts.map(p => p.charAt(0).toUpperCase()).slice(0, 2).join('');
  const cleanPhone = (inRoute.phone || '').replace(/\D/g, '');
  const mapsUrl = _mapsLink(inRoute.lat, inRoute.lng, inRoute.address);
  const wazeUrl = _wazeLink(inRoute.address);

  const isPickup = inRoute.status === 'en_camino';
  const isAtDoor = inRoute.status === 'en_puerta';

  let etaText = '';
  if (inRoute.dispatched_at) {
    const iso = inRoute.dispatched_at.endsWith('Z') ? inRoute.dispatched_at : inRoute.dispatched_at + 'Z';
    const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    etaText = `${mins} min en ruta`;
  }

  const addrEl = document.createElement('div');
  addrEl.textContent = inRoute.address || 'Sin dirección';
  const safeAddr = addrEl.innerHTML;

  const notesEl = document.createElement('div');
  notesEl.textContent = inRoute.notes || '';
  const safeNotes = notesEl.innerHTML;

  activeEl.innerHTML = `
    <div class="active-badge"><span class="dot"></span>Entrega en curso · #${_esc(String(inRoute.id).slice(0,6))}</div>
    <div class="active-name">${_esc(inRoute.customer_name || inRoute.phone || 'Cliente')}</div>
    <div class="active-addr">${safeAddr}</div>
    ${safeNotes ? `<div class="active-note">"${safeNotes}"</div>` : ''}

    <div class="progress">
      <div class="step-list">
        <div class="step done">
          <div class="step-dot"></div>
          <div class="step-text"><strong>Pickup en restaurante</strong></div>
        </div>
        <div class="step ${isPickup || isAtDoor ? 'cur' : ''}">
          <div class="step-dot"></div>
          <div class="step-text"><strong>En camino</strong><div class="step-sub">${etaText}</div></div>
        </div>
        <div class="step ${isAtDoor ? 'cur' : ''}">
          <div class="step-dot"></div>
          <div class="step-text">Entrega al cliente</div>
        </div>
      </div>
    </div>

    <div class="cta-row">
      <a class="cta-btn outline" href="tel:+${_esc(cleanPhone)}" aria-label="Llamar">📞</a>
      ${isAtDoor
        ? `<button class="cta-btn dom-action-btn" data-action="entregado" data-id="${_esc(String(inRoute.id))}">✅ Marcar Entregado</button>`
        : `<a class="cta-btn dom-maps-btn" href="${_esc(mapsUrl)}" target="_blank" rel="noopener">🗺️ Maps</a>
           <a class="cta-btn outline dom-waze-btn" href="${_esc(wazeUrl)}" target="_blank" rel="noopener" data-id="${_esc(String(inRoute.id))}">Waze</a>`
      }
    </div>`;

  activeEl.querySelector('.dom-action-btn')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const action = btn.dataset.action;
    const orderId = btn.dataset.id;
    if (action === 'entregado') {
      const ok = await mesioConfirm('¿Confirmar entrega al cliente?', { confirmText: 'Sí, entregado', danger: false });
      if (ok) await updateStatus(orderId, 'entregado');
    }
  });

  activeEl.querySelector('.dom-waze-btn')?.addEventListener('click', async (e) => {
    const orderId = e.currentTarget.dataset.id;
    if (orderId) await updateStatus(orderId, 'en_puerta');
  });
}

// ── Render order items ────────────────────────────────
function _renderOrderItems(order) {
  const itemsEl = document.getElementById('dom-order-items');
  const totalEl = document.getElementById('dom-order-total');
  if (!itemsEl) return;

  const inRoute = order;
  if (!inRoute) {
    itemsEl.innerHTML = '';
    if (totalEl) totalEl.innerHTML = '';
    return;
  }

  let items = inRoute.items || [];
  if (typeof items === 'string') { try { items = JSON.parse(items); } catch(_){items=[]; } }

  itemsEl.innerHTML = items.map(i => `<div class="item-row">
    <div><span class="item-qty">${_esc(String(i.quantity || i.qty || 1))}×</span>${_esc(i.name || '')}</div>
    <div class="item-price">${mesioFmt((i.price || 0) * (i.quantity || i.qty || 1))}</div>
  </div>`).join('');

  if (totalEl) {
    const total = inRoute.total || items.reduce((s,i)=>s+(i.price||0)*(i.quantity||i.qty||1),0);
    const paid = inRoute.paid ? '(pagado)' : '(cobrar)';
    const method = inRoute.payment_method || '';
    totalEl.innerHTML = `<div class="total-row">
      <div>Total ${_esc(paid)} ${_esc(method)}</div>
      <div class="total-val">${mesioFmt(total)}</div>
    </div>`;
  }
}

// ── Render customer section ───────────────────────────
function _renderCustomer(order) {
  const el = document.getElementById('dom-customer-card');
  if (!el || !order) return;
  const nameParts = (order.customer_name || order.phone || 'C').split(' ');
  const initials = nameParts.map(p=>p.charAt(0).toUpperCase()).slice(0,2).join('');
  const cleanPhone = (order.phone||'').replace(/\D/g,'');

  el.innerHTML = `<div class="tel-card">
    <div class="tel-av">${_esc(initials)}</div>
    <div class="tel-info">
      <div class="tel-nm">${_esc(order.customer_name || 'Cliente')}</div>
      <div class="tel-ph">${_esc(order.phone || '')}</div>
    </div>
    <div class="tel-actions">
      <a class="tel-btn wa" href="https://wa.me/${_esc(cleanPhone)}" target="_blank" rel="noopener" aria-label="WhatsApp">💬</a>
      <a class="tel-btn" href="tel:+${_esc(cleanPhone)}" aria-label="Llamar">📞</a>
    </div>
  </div>`;
}

// ── Render up-next queue ──────────────────────────────
function _renderUpnext(orders) {
  const el = document.getElementById('dom-upnext');
  if (!el) return;
  const upcoming = orders.filter(o => o.status === 'listo' || o.status === 'confirmado');
  if (!upcoming.length) { el.innerHTML = ''; return; }

  el.innerHTML = `<div class="dom-upnext">
    <h4>Siguientes entregas</h4>
    ${upcoming.slice(0, 3).map((o, i) => {
      const addrEl = document.createElement('span');
      addrEl.textContent = o.address || 'Sin dirección';
      return `<div class="up-card">
        <div class="up-n">${i + 2}</div>
        <div class="up-body">
          <div class="up-name">${_esc(o.customer_name || o.phone || 'Cliente')}</div>
          <div class="up-where">${addrEl.innerHTML}</div>
        </div>
        <div class="up-meta">
          <div class="up-total">${mesioFmt(o.total||0)}</div>
        </div>
      </div>`;
    }).join('')}
  </div>`;
}

// ── Render historial ──────────────────────────────────
function _renderHistorial(orders) {
  const el = document.getElementById('dom-hist-list');
  if (!el) return;
  const done = orders.filter(o => o.status === 'entregado');
  if (!done.length) { el.innerHTML = '<div class="dom-list-empty">Sin entregas completadas hoy</div>'; return; }
  el.innerHTML = done.map(o => `<div class="hist-card">
    <div>
      <div class="hist-id">#${_esc(String(o.id).slice(0,6))} · ${_esc(o.customer_name || o.phone || '')}</div>
      <div class="hist-addr">${_esc(o.address || '')}</div>
    </div>
    <div>
      <div class="hist-total">${mesioFmt(o.total||0)}</div>
      <div class="hist-time">${o.delivered_at ? mesioDate(o.delivered_at) : '—'}</div>
    </div>
  </div>`).join('');
}

// ── Update order status ───────────────────────────────
async function updateStatus(orderId, status) {
  try {
    const res = await fetch(`/api/delivery/orders/${orderId}/status`, {
      method: 'PATCH', headers: mesioHeaders(),
      body: JSON.stringify({ status })
    });
    if (res.ok) { await fetchOrders(); }
    else { mesioToast('Error al actualizar', 'error'); }
  } catch (_) { mesioToast('Error de conexión', 'error'); }
}

// ── Fetch orders ──────────────────────────────────────
async function fetchOrders() {
  try {
    const res = await fetch('/api/delivery/orders', { headers: mesioHeaders() });
    mesioTrackFetch(res.ok);
    if (!res.ok) { if (res.status === 401) { window.location.href = '/login'; } return; }
    const data = await res.json();
    _allOrders = data.orders || [];
    _render();
  } catch (_) { mesioTrackFetch(false); }
}

function _render() {
  _renderHero(_allOrders);
  const inRoute = _allOrders.find(o => o.status === 'en_camino' || o.status === 'en_puerta');
  _renderActive(_allOrders);
  _renderOrderItems(inRoute || null);
  _renderCustomer(inRoute || null);
  _renderUpnext(_allOrders);
  _renderHistorial(_allOrders);
}

// ── Hash check for efficient polling ─────────────────
async function checkUpdates() {
  try {
    const res = await fetch('/api/delivery/check-updates', { headers: mesioHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    if (data.hash !== _currentHash) {
      _currentHash = data.hash;
      fetchOrders();
    }
  } catch (_) { /* silent: mobile network */ }
}

// ── Boot ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.dom-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  fetchOrders();
  mesioInterval(checkUpdates, 10000);
});
