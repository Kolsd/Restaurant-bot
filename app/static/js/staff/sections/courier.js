/* ═══════════════════════════════════════════════════
   Mesio — Staff App / Courier section (domiciliario)
   Ported from the old /courier page (app/static/html/courier.html +
   app/static/js/pages/courier.js) into a mount()/unmount() module for the
   unified Staff App shell (app/static/js/staff/staff-shell.js).

   Business logic is UNCHANGED from the original courier.js — only the
   bootstrap (DOMContentLoaded → mount) and cleanup (unmount clears the
   poll interval and removes the DOM the section injected) are new.
   ═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var TEMPLATE = `
<div class="mesio-sec-courier">
<div class="phone-wrap">
  <div class="phone-frame">

    <!-- iOS-style notch -->
    <div class="notch" aria-hidden="true"></div>

    <!-- Status bar (desktop only) -->
    <div class="phone-status" aria-hidden="true">
      <span id="dom-time">9:41</span>
      <span>● ● ●</span>
    </div>

    <!-- ── Light inner screen ── -->
    <div class="phone-screen">

      <!-- Hero gradient -->
      <div class="dom-hero">
        <div class="hero-label" id="dom-turno-label">Turno activo</div>
        <div class="hero-title" id="dom-staff-name">Hola 👋</div>
        <div class="hero-stats">
          <div><strong id="dom-stat-delivered">—</strong>entregadas</div>
          <div><strong id="dom-stat-pending">—</strong>pendientes</div>
          <div><strong id="dom-stat-tips">—</strong>propinas</div>
        </div>
      </div>

      <!-- ── Hoy tab content ── -->
      <div class="tab-pane active" data-pane="hoy">

        <!-- Active delivery card -->
        <div id="dom-active-card">
          <div class="dom-no-active">Cargando entregas…</div>
        </div>

        <!-- Order items -->
        <div class="dom-section" id="dom-order-section" style="display:none;">
          <h4>Pedido</h4>
          <div id="dom-order-items"></div>
          <div id="dom-order-total"></div>
        </div>

        <!-- Customer -->
        <div class="dom-section" id="dom-customer-section" style="display:none;">
          <h4>Cliente</h4>
          <div id="dom-customer-card"></div>
        </div>

        <!-- Up-next -->
        <div id="dom-upnext"></div>

      </div><!-- /.tab-pane hoy -->

      <!-- ── Mapa tab (placeholder) ── -->
      <div class="tab-pane" data-pane="mapa">
        <div style="padding:40px 20px;text-align:center;color:var(--text-3);">
          <div style="font-size:32px;margin-bottom:12px;">🗺️</div>
          <div style="font-size:14px;font-weight:600;color:var(--text);">Vista de mapa</div>
          <div style="font-size:12px;margin-top:6px;">Usa Waze desde la tarjeta de entrega activa.</div>
        </div>
      </div>

      <!-- ── Historial tab ── -->
      <div class="tab-pane" data-pane="historial">
        <div style="padding:14px 14px 6px;">
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-3);">Completados hoy</div>
        </div>
        <div id="dom-hist-list">
          <div class="dom-list-empty">Cargando…</div>
        </div>
      </div>

      <!-- ── Perfil tab ── -->
      <div class="tab-pane" data-pane="perfil">
        <div style="padding:24px 20px;">
          <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;">
            <div style="width:54px;height:54px;border-radius:50%;background:linear-gradient(135deg,#1D9E75,#0F6E56);display:flex;align-items:center;justify-content:center;color:#fff;font-size:20px;font-weight:700;" id="perfil-av">?</div>
            <div>
              <div style="font-weight:700;font-size:16px;color:var(--text);" id="perfil-name">Cargando…</div>
              <div style="font-size:12px;color:var(--text-3);">Domiciliario</div>
            </div>
          </div>
          <button class="m-btn m-btn--ghost" style="width:100%;justify-content:center;" id="perfil-logout">
            Cerrar sesión
          </button>
        </div>
      </div>

      <!-- ── Bottom tab bar ── -->
      <nav class="dom-tabs" role="tablist" aria-label="Navegación principal">
        <button class="dom-tab active" data-tab="hoy" role="tab" aria-selected="true" aria-label="Hoy">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M5 11l7-7 7 7v9a2 2 0 01-2 2h-3v-6h-4v6H7a2 2 0 01-2-2z"/>
          </svg>
          Hoy
        </button>
        <button class="dom-tab" data-tab="mapa" role="tab" aria-selected="false" aria-label="Mapa">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3V6z"/><path d="M9 3v15M15 6v15"/>
          </svg>
          Mapa
        </button>
        <button class="dom-tab" data-tab="historial" role="tab" aria-selected="false" aria-label="Historial">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 10h8M8 14h5"/>
          </svg>
          Historial
        </button>
        <button class="dom-tab" data-tab="perfil" role="tab" aria-selected="false" aria-label="Perfil">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/>
          </svg>
          Perfil
        </button>
      </nav>

    </div><!-- /.phone-screen -->

  </div><!-- /.phone-frame -->
</div><!-- /.phone-wrap -->
</div><!-- /.mesio-sec-courier -->
`;

  // ── Cleanup tracking ─────────────────────────────────
  var _intervalHandles = [];
  var _observers = [];
  function _trackInterval(id) { _intervalHandles.push(id); return id; }

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

    const tipsToday = done.reduce((sum, o) => sum + Number(o.tip_amount || 0), 0);
    const tipsEl = document.getElementById('dom-stat-tips');
    if (tipsEl) tipsEl.textContent = tipsToday > 0 ? mesioFmt(tipsToday) : '—';
  }

  // ── Render active delivery ────────────────────────────
  function _renderActive(orders) {
    const inRoute = orders.find(o => o.status === 'en_camino' || o.status === 'en_puerta');
    const activeEl = document.getElementById('dom-active-card');
    if (!activeEl) return;

    if (!inRoute) {
      const readyOrder = orders.find(o => o.status === 'listo');
      if (!readyOrder) {
        activeEl.innerHTML = '<div class="dom-no-active">Sin entregas en curso. Revisa la cola de pedidos.</div>';
        return;
      }
      const addrEl = document.createElement('div');
      addrEl.textContent = readyOrder.address || 'Sin dirección';
      const safeAddr = addrEl.innerHTML;
      const cleanPhone = (readyOrder.phone || '').replace(/\D/g, '');
      const mapsUrl = _mapsLink(readyOrder.lat, readyOrder.lng, readyOrder.address);

      activeEl.innerHTML = `
        <div class="active-badge" style="color:#F59E0B;">
          <span class="dot" style="background:#F59E0B;box-shadow:0 0 0 4px rgba(245,158,11,0.15);"></span>
          Listo para recoger · #${_esc(String(readyOrder.id).slice(0,6))}
        </div>
        <div class="active-name">${_esc(readyOrder.customer_name || readyOrder.phone || 'Cliente')}</div>
        <div class="active-addr">${safeAddr}</div>
        <div class="cta-row">
          <a class="cta-btn outline" href="tel:+${_esc(cleanPhone)}" aria-label="Llamar">📞</a>
          <a class="cta-btn outline" href="${_esc(mapsUrl)}" target="_blank" rel="noopener" style="flex:0 0 54px;text-align:center;">🗺️</a>
          <button class="cta-btn dom-action-btn" data-action="en_camino" data-id="${_esc(String(readyOrder.id))}">🛵 Salir a entregar</button>
        </div>`;

      activeEl.querySelector('.dom-action-btn')?.addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true; btn.textContent = 'Confirmando…';
        const ok = await mesioConfirm('¿Salir a entregar este pedido?', { confirmText: 'Sí, salir' });
        if (ok) {
          await updateStatus(btn.dataset.id, 'en_camino');
        } else {
          btn.disabled = false; btn.textContent = '🛵 Salir a entregar';
        }
      });
      return;
    }

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
        <a class="cta-btn outline" href="tel:+${_esc(cleanPhone)}" aria-label="Llamar" style="flex:0 0 50px;font-size:18px;">📞</a>
        ${isAtDoor
          ? `<button class="cta-btn dom-action-btn" data-action="entregado" data-id="${_esc(String(inRoute.id))}">✅ Entregado</button>`
          : `<a class="cta-btn outline dom-maps-btn" href="${_esc(mapsUrl)}" target="_blank" rel="noopener" style="flex:0 0 50px;text-align:center;font-size:18px;">🗺️</a>
             <a class="cta-btn outline" href="${_esc(wazeUrl)}" target="_blank" rel="noopener" style="flex:0 0 60px;font-size:13px;">Waze</a>
             <button class="cta-btn dom-action-btn" data-action="en_puerta" data-id="${_esc(String(inRoute.id))}">📍 Llegué</button>`
        }
      </div>`;

    activeEl.querySelector('.dom-action-btn')?.addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const action = btn.dataset.action;
      const orderId = btn.dataset.id;
      if (action === 'en_puerta') {
        const ok = await mesioConfirm('¿Confirmás que llegaste al destino?', { confirmText: 'Sí, llegué' });
        if (ok) await updateStatus(orderId, 'en_puerta');
      } else if (action === 'entregado') {
        const ok = await mesioConfirm('¿Confirmar entrega al cliente?', { confirmText: 'Sí, entregado', danger: false });
        if (ok) await updateStatus(orderId, 'entregado');
      }
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
    const cleanPhone = (order.phone||'').replace(/\D/g,'');
    const nameParts = (order.customer_name || order.phone || 'C').split(' ');
    const initials = nameParts.map(p=>p.charAt(0).toUpperCase()).slice(0,2).join('');

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

    const inRoute = orders.find(o => o.status === 'en_camino' || o.status === 'en_puerta');
    const listoOrders = orders.filter(o => o.status === 'listo');
    const queueListo = inRoute ? listoOrders : listoOrders.slice(1);
    const preparing = orders.filter(o => o.status === 'confirmado' || o.status === 'en_preparacion');
    const queue = [...queueListo, ...preparing];

    if (!queue.length) { el.innerHTML = ''; return; }

    el.innerHTML = `<div class="dom-upnext">
      <h4>Siguientes entregas</h4>
      ${queue.slice(0, 4).map((o, i) => {
        const addrEl = document.createElement('span');
        addrEl.textContent = o.address || 'Sin dirección';
        const isListo = o.status === 'listo';
        return `<div class="up-card" style="align-items:center;">
          <div class="up-n">${i + 2}</div>
          <div class="up-body">
            <div class="up-name">${_esc(o.customer_name || o.phone || 'Cliente')}</div>
            <div class="up-where">${addrEl.innerHTML}</div>
          </div>
          <div class="up-meta" style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
            <div class="up-total">${mesioFmt(o.total||0)}</div>
            ${isListo
              ? `<button class="dom-queue-btn cta-btn" data-id="${_esc(String(o.id))}" style="font-size:11px;padding:6px 10px;border-radius:8px;">🛵 Tomar</button>`
              : `<div class="up-time">En cocina</div>`
            }
          </div>
        </div>`;
      }).join('')}
    </div>`;

    el.querySelectorAll('.dom-queue-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        const ok = await mesioConfirm('¿Tomar este pedido para entrega?', { confirmText: 'Sí, tomar' });
        if (ok) { await updateStatus(btn.dataset.id, 'en_camino'); }
        else { btn.disabled = false; }
      });
    });
  }

  // ── Render history ──────────────────────────────────
  function _renderHistory(orders) {
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
    const readyOrder = !inRoute ? _allOrders.find(o => o.status === 'listo') : null;
    _renderActive(_allOrders);
    _renderOrderItems(inRoute || readyOrder || null);
    _renderCustomer(inRoute || readyOrder || null);
    _renderUpnext(_allOrders);
    _renderHistory(_allOrders);
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

  // ── Status bar clock (was courier.html's inline <script>) ──────────
  function _stClock() {
    const el = document.getElementById('dom-time');
    if (el) el.textContent = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
  }

  // ── mount / unmount ───────────────────────────────────
  function mount(container) {
    container.innerHTML = TEMPLATE;
    _activeTab = 'hoy';
    _currentHash = null;

    document.querySelectorAll('.dom-tab').forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    fetchOrders();
    _trackInterval(mesioInterval(checkUpdates, 10000));

    // Status bar clock
    _stClock();
    _trackInterval(setInterval(_stClock, 30000));

    // Profile tab populate
    const pn = document.getElementById('perfil-name');
    const pav = document.getElementById('perfil-av');
    const name = localStorage.getItem('rb_staff_name') || localStorage.getItem('rb_name') || 'Domiciliario';
    if (pn) pn.textContent = name;
    if (pav) pav.textContent = name.split(' ').map(p => p[0]).join('').slice(0,2).toUpperCase();

    // Shift label with elapsed time
    const shiftStart = localStorage.getItem('rb_shift_start');
    if (shiftStart) {
      const mins = Math.floor((Date.now() - new Date(shiftStart).getTime()) / 60000);
      const hrs = Math.floor(mins / 60);
      const lbl = document.getElementById('dom-turno-label');
      if (lbl) lbl.textContent = `Turno activo · ${hrs}h ${mins % 60}m`;
    }

    // Show order + customer sections once active card renders
    const obs = new MutationObserver(() => {
      const hasActive = document.querySelector('.active-badge');
      const orderSec = document.getElementById('dom-order-section');
      const custSec  = document.getElementById('dom-customer-section');
      if (orderSec) orderSec.style.display = hasActive ? '' : 'none';
      if (custSec)  custSec.style.display  = hasActive ? '' : 'none';
    });
    const activeCard = document.getElementById('dom-active-card');
    if (activeCard) { obs.observe(activeCard, { childList: true, subtree: true }); _observers.push(obs); }

    // Logout
    document.getElementById('perfil-logout')?.addEventListener('click', () => mesioLogout());
  }

  function unmount(container) {
    _intervalHandles.forEach(function (id) { clearInterval(id); });
    _intervalHandles = [];
    _observers.forEach(function (o) { o.disconnect(); });
    _observers = [];
    if (container) container.innerHTML = '';
  }

  window.MesioStaffSections = window.MesioStaffSections || {};
  window.MesioStaffSections.courier = { mount: mount, unmount: unmount };
})();
