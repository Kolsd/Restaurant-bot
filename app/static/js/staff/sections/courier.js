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
  var _rtUnsubs = [];  // MesioRealtime.on() unsubscribe fns, cleared on unmount
  function _trackInterval(id) { _intervalHandles.push(id); return id; }

  // ── State ───────────────────────────────────────────
  let _activeTab = 'hoy';
  let _lastWebSignature = null;
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

  function _wazeLink(address, lat, lng) {
    // The GPS pin is what the rider navigates by (docs/claude/delivery-web.md:
    // the typed address is only guidance) — use it whenever the order has one.
    if (lat != null && lng != null) return `https://waze.com/ul?ll=${lat},${lng}&navigate=yes`;
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
      const fullName = (localStorage.getItem('rb_staff_name') || '').trim();
      const firstName = fullName ? fullName.split(/\s+/)[0] : '';
      nameEl.textContent = firstName ? `Hola, ${firstName} 👋` : 'Hola 👋';
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
          Listo para recoger · #${_esc(_orderLabel(readyOrder))}
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
    const wazeUrl = _wazeLink(inRoute.address, inRoute.lat, inRoute.lng);

    const isPickup = inRoute.status === 'en_camino';
    const isAtDoor = inRoute.status === 'en_puerta';
    // The new web delivery lifecycle (chunk 7, app/routes/staff_delivery.py)
    // has no en_puerta step of its own — only en-route and delivered exist,
    // and the backend already accepts "delivered" straight from en_camino
    // for these orders — so a web order skips the legacy "Llegué" middle
    // step and goes straight to the Entregado action.
    const isWebOrder = inRoute._source === 'web';
    const showDelivered = isAtDoor || isWebOrder;

    let etaText = '';
    if (inRoute.dispatched_at) {
      const mins = Math.floor((Date.now() - mesioParseServerDate(inRoute.dispatched_at).getTime()) / 60000);
      etaText = `${mins} min en ruta`;
    }

    const addrEl = document.createElement('div');
    addrEl.textContent = inRoute.address || 'Sin dirección';
    const safeAddr = addrEl.innerHTML;

    const notesEl = document.createElement('div');
    notesEl.textContent = inRoute.notes || '';
    const safeNotes = notesEl.innerHTML;

    activeEl.innerHTML = `
      <div class="active-badge"><span class="dot"></span>Entrega en curso · #${_esc(_orderLabel(inRoute))}</div>
      <div class="active-name">${_esc(inRoute.customer_name || inRoute.phone || 'Cliente')}</div>
      <div class="active-addr">${safeAddr}</div>
      ${safeNotes ? `<div class="active-note">"${safeNotes}"</div>` : ''}

      <div class="progress">
        <div class="step-list">
          <div class="step done">
            <div class="step-dot"></div>
            <div class="step-text"><strong>Pickup en restaurante</strong></div>
          </div>
          <div class="step ${isPickup || showDelivered ? 'cur' : ''}">
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
        ${isWebOrder
          ? `<a class="cta-btn outline dom-maps-btn" href="${_esc(mapsUrl)}" target="_blank" rel="noopener" style="flex:0 0 50px;text-align:center;font-size:18px;">🗺️</a>
             <a class="cta-btn outline" href="${_esc(wazeUrl)}" target="_blank" rel="noopener" style="flex:0 0 60px;font-size:13px;">Waze</a>
             <button class="cta-btn dom-action-btn" data-action="entregado" data-id="${_esc(String(inRoute.id))}">✅ Entregado</button>`
          : isAtDoor
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

    // A web delivery/pickup line's own total lives in `subtotal` (cart-line
    // shape — see app/routes/diner_delivery.py's `_public_order_items_view`
    // docstring), not `price` — the legacy WhatsApp shape uses `price`.
    // Falling back to `(i.price || 0) * qty` alone would silently show $0
    // for every web order's items.
    itemsEl.innerHTML = items.map(i => {
      const qty = i.quantity || i.qty || 1;
      const lineTotal = i.subtotal != null ? i.subtotal : (i.price != null ? i.price * qty : 0);
      const note = i.note || i.notes;
      return `<div class="item-row">
      <div><span class="item-qty">${_esc(String(qty))}×</span>${_esc(i.name || '')}${note ? ` <em>"${_esc(note)}"</em>` : ''}</div>
      <div class="item-price">${mesioFmt(lineTotal)}</div>
    </div>`;
    }).join('');

    if (totalEl) {
      const total = inRoute.total || items.reduce((s,i)=>s+(i.price||0)*(i.quantity||i.qty||1),0);
      const paid = inRoute.paid ? '(pagado)' : '(cobrar)';
      const method = inRoute.payment_method || '';
      // The rider is the one holding the money on a cash or card-at-the-door
      // order, so they are who registers it — not the cashier back at the
      // restaurant. Only these two methods: a Nequi/Bancolombia transfer is
      // confirmed against the bank by whoever can see the account, which is
      // the cashier's Domicilios queue, not a phone at someone's door.
      const payButtons = inRoute.paid ? '' : `
      <div class="dom-pay-row">
        <button class="cta-btn outline dom-pay-btn" data-method="efectivo" data-id="${_esc(String(inRoute.id))}">💵 Cobré efectivo</button>
        <button class="cta-btn outline dom-pay-btn" data-method="tarjeta" data-id="${_esc(String(inRoute.id))}">💳 Cobré con tarjeta</button>
      </div>`;
      totalEl.innerHTML = `<div class="total-row">
        <div>Total ${_esc(paid)} ${_esc(method)}</div>
        <div class="total-val">${mesioFmt(total)}</div>
      </div>${payButtons}`;

      // Listener per button (not a single querySelector like the cta-row
      // above, which only ever renders ONE action button): both must work.
      totalEl.querySelectorAll('.dom-pay-btn').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
          const el = e.currentTarget;
          el.disabled = true;
          await registerPayment(el.dataset.id, el.dataset.method);
        });
      });
    }
  }

  // Records the money as received on a web delivery/pickup order. The server
  // (app/routes/staff_delivery.py::mark_order_paid) re-checks that this rider
  // is the order's assigned courier and refuses a second registration with a
  // 409 — never trust the button being hidden.
  async function registerPayment(orderId, method) {
    try {
      const res = await fetch('/api/staff/delivery/orders/' + orderId + '/mark-paid', {
        method: 'POST',
        headers: mesioHeaders(),
        body: JSON.stringify({ payment_method: method }),
      });
      if (res.ok) { mesioToast('Pago registrado', 'success'); await fetchOrders(); return; }
      let detail = 'No se pudo registrar el pago';
      try {
        const data = await res.json();
        if (data && typeof data.detail === 'string') detail = data.detail;
      } catch (_) { /* non-JSON error body */ }
      mesioToast(detail, 'error');
      await fetchOrders();
    } catch (_) { mesioToast('Error de conexión', 'error'); }
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
        <div class="hist-id">#${_esc(_orderLabel(o))} · ${_esc(o.customer_name || o.phone || '')}</div>
        <div class="hist-addr">${_esc(o.address || '')}</div>
      </div>
      <div>
        <div class="hist-total">${mesioFmt(o.total||0)}</div>
        <div class="hist-time">${o.delivered_at ? mesioDate(o.delivered_at) : '—'}</div>
      </div>
    </div>`).join('');
  }

  // ── Update order status ───────────────────────────────
  // Every order in _allOrders comes exclusively from fetchWebOrders() (see
  // fetchOrders() below) since chunk 9 (docs/claude/delivery-web.md) retired
  // WhatsApp delivery/pickup entirely, so this always dispatches to the
  // sede+ownership-enforced web endpoint (app/routes/staff_delivery.py::
  // _require_can_transition) — the legacy PATCH .../status branch that used
  // to run for WhatsApp-era orders was removed along with that flow.
  async function updateStatus(orderId, status) {
    try {
      const path = status === 'en_camino' ? '/en-route' : '/delivered';
      // Plain string concat (not a template literal) so the two dynamic
      // segments don't collapse into one unmatchable {PARAM}{PARAM} token
      // for scripts/lint_frontend.py's FETCH check (see its own
      // '/api/x/' + id example) — normalizes to the same
      // /api/staff/delivery/orders/{PARAM}/... prefix as delivery.js's
      // _postDelivery.
      const res = await fetch('/api/staff/delivery/orders/' + orderId + path, {
        method: 'POST', headers: mesioHeaders(),
      });
      if (res.ok) { await fetchOrders(); return; }
      let detail = 'Error al actualizar';
      try {
        const data = await res.json();
        if (data && typeof data.detail === 'string') detail = data.detail;
      } catch (_) { /* non-JSON error body */ }
      mesioToast(detail, 'error');
    } catch (_) { mesioToast('Error de conexión', 'error'); }
  }

  // ── Normalize a web delivery/pickup order (app/routes/staff_delivery.py's
  // _cashier_order_view shape) into the SAME field names this file's
  // render functions already expected from the old WhatsApp-era delivery
  // list shape (deleted in chunk 9, docs/claude/delivery-web.md), so
  // _renderHero/_renderActive/etc. need no branching per source.
  // dispatched_at has no exact web-order equivalent (the web model tracks
  // WHO/WHEN a courier was assigned, not a separate "left for delivery"
  // timestamp) — courier_assigned_at is the closest available proxy.
  // The code the customer sees on /pedido/{code} and says on the phone —
  // not a slice of the internal id ("#WEB-49"), which nobody can match.
  function _orderLabel(o) {
    return o.public_code ? String(o.public_code) : String(o.id).slice(0, 6);
  }

  function _normalizeWebOrder(o) {
    return {
      id: o.id,
      public_code: o.public_code,
      status: o.status,
      order_type: o.order_type,
      customer_name: o.customer_name,
      phone: o.customer_phone,
      address: o.address,
      lat: o.delivery_lat,
      lng: o.delivery_lon,
      notes: o.notes,
      items: o.items,
      total: o.total,
      tip_amount: o.tip_amount,
      paid: !!o.paid,
      payment_method: o.payment_method,
      dispatched_at: o.courier_assigned_at,
      delivered_at: o.delivered_at,
      created_at: o.created_at,
      _source: 'web',
    };
  }

  // GET /orders/mine returns the courier's FULL history (no date filter —
  // the endpoint's job is ownership scoping, not a time window), unlike the
  // old WhatsApp-era delivery list. Without this filter, "entregadas" in the
  // hero stats and the "Completados hoy" history tab would grow across every
  // day this rider has ever worked. Uses the BROWSER's local date, not the
  // sede's own timezone (docs/claude/status.md already flags per-timezone
  // day-bucketing as an open item elsewhere) — good enough for "today" on a
  // rider's own phone, not sede-timezone-exact.
  function _isTodayIso(iso) {
    const d = mesioParseServerDate(iso);
    if (!d) return false;
    const now = new Date();
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  }

  // ── Fetch the courier's OWN web delivery/pickup orders (chunk 7) — the
  // smallest sede-scoped endpoint added for this: GET
  // /api/staff/delivery/orders/mine, filtered server-side to
  // courier_staff_id = the caller (never another rider's queue, and never
  // client-filtered — see app/routes/staff_delivery.py::list_my_delivery_orders).
  async function fetchWebOrders() {
    try {
      const res = await fetch('/api/staff/delivery/orders/mine', { headers: mesioHeaders() });
      mesioTrackFetch(res.ok);
      if (res.status === 401) { window.location.href = '/login'; return []; }
      if (!res.ok) return [];
      const data = await res.json();
      return (data.orders || [])
        .filter(o => o.status !== 'entregado' || _isTodayIso(o.delivered_at))
        .map(_normalizeWebOrder);
    } catch (_) { mesioTrackFetch(false); return []; }
  }

  // ── Fetch orders ──────────────────────────────────────
  async function fetchOrders() {
    // ONLY the courier's own orders, scoped on the server (courier + sede).
    // This used to also merge the WhatsApp-era org-wide delivery list
    // endpoint (deleted in chunk 9), which returned EVERY
    // delivery order of the org — any sede, any courier,
    // with customer name/phone/address — so a rider saw everyone's orders
    // and every web order twice. WhatsApp delivery is switched off in this
    // wave (docs/claude/delivery-web.md), so that list has nothing of this
    // rider's that /orders/mine does not already return.
    _allOrders = await fetchWebOrders();
    _render();
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
  // The legacy hash endpoint only covers WhatsApp-era orders, so a
  // web-order-only change (e.g. the cashier assigns/reassigns a courier)
  // would never flip `data.hash` and this rider would miss it. Web orders
  // have no server-side hash of their own (the smallest addition for this
  // chunk was the /orders/mine list endpoint, not a second hash endpoint —
  // its own payload is tiny, so a cheap client-side signature is enough).
  function _webOrdersSignature(orders) {
    return orders.map(o => o.id + ':' + o.status + ':' + (o.courier_staff_id || '')).sort().join('|');
  }

  async function checkUpdates() {
    let webChanged = false;
    try {
      const res = await fetch('/api/staff/delivery/orders/mine', { headers: mesioHeaders() });
      if (res.ok) {
        const data = await res.json();
        const sig = _webOrdersSignature(data.orders || []);
        webChanged = sig !== _lastWebSignature;
        _lastWebSignature = sig;
      }
    } catch (_) { /* silent: mobile network */ }

    if (webChanged) fetchOrders();
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
    _lastWebSignature = null;

    document.querySelectorAll('.dom-tab').forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    fetchOrders();
    _trackInterval(mesioLiveInterval(checkUpdates, 10000));

    // Real-time invalidation — SSE events call checkUpdates() immediately;
    // mesioLiveInterval above is just the 60s safety net while connected.
    if (window.MesioRealtime) {
      ['order.created', 'order.updated', 'resync'].forEach(function (topic) {
        _rtUnsubs.push(MesioRealtime.on(topic, checkUpdates));
      });
    }

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
    _rtUnsubs.forEach(function (off) { off(); });
    _rtUnsubs = [];
    _observers.forEach(function (o) { o.disconnect(); });
    _observers = [];
    if (container) container.innerHTML = '';
  }

  window.MesioStaffSections = window.MesioStaffSections || {};
  window.MesioStaffSections.courier = { mount: mount, unmount: unmount };
})();
