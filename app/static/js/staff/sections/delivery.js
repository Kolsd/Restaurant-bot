/* ═══════════════════════════════════════════════════
   Mesio — Staff App / Domicilios section (cashier's delivery+pickup queue)
   New section for the web delivery/pickup wave (docs/claude/delivery-web.md,
   chunk 7 — "the staff-side screens"). Talks to app/routes/staff_delivery.py
   (chunk 4), which sede-scopes every call to the cashier's own `location_id`
   (or an admin's chosen X-Location-ID). Follows the mount()/unmount()
   contract of the other sections (see courier.js / cashier.js) — lazy-
   loaded by app/static/js/staff/staff-shell.js on first visit.
   ═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var TEMPLATE = `
<style>
.mesio-sec-delivery { padding: 20px 24px 40px; max-width: 1180px; margin: 0 auto; }
.deliv-topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; gap: 12px; }
.deliv-title { font-size: 20px; font-weight: 700; color: var(--text); }

.deliv-group { margin-bottom: 26px; }
.deliv-group-title {
  font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--text-3); display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
}
.deliv-group--pending .deliv-group-title { color: var(--brand-dark); font-size: 14px; }
.deliv-count {
  background: var(--surface-hover); border: 1px solid var(--border); border-radius: 20px;
  padding: 1px 9px; font-size: 11.5px; font-weight: 700; color: var(--text-2);
}
.deliv-group--pending .deliv-count { background: var(--brand-light); color: var(--brand-dark); border-color: transparent; }
.deliv-collapsible { cursor: pointer; user-select: none; }
.deliv-chevron { margin-left: auto; transition: transform 0.15s; color: var(--text-3); }
.deliv-chevron.open { transform: rotate(90deg); }

.deliv-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.deliv-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 16px; display: flex; flex-direction: column; gap: 10px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.03);
}
.deliv-card--pending { border-color: var(--brand); border-width: 2px; box-shadow: 0 2px 10px rgba(29,158,117,0.12); }
.deliv-card--closed { opacity: 0.75; }

.deliv-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.deliv-code { font-family: var(--font-mono); font-weight: 700; font-size: 13px; color: var(--text); }
.deliv-type-badge { font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; padding: 2px 8px; border-radius: 20px; }
.deliv-type-badge.domicilio { background: var(--info-light, #DBEAFE); color: var(--info-text, #1E40AF); }
.deliv-type-badge.recoger { background: var(--purple-light, #EDE9FE); color: var(--purple-text, #5B21B6); }
.deliv-elapsed { font-size: 11.5px; color: var(--text-3); margin-left: auto; }
.deliv-status-pill { font-size: 10.5px; font-weight: 600; padding: 2px 8px; border-radius: 20px; background: var(--surface-hover); color: var(--text-2); }
.deliv-status-pill.rechazado, .deliv-status-pill.cancelado { background: var(--danger-light); color: var(--danger-text); }
.deliv-status-pill.entregado { background: var(--success-light); color: var(--success); }

.deliv-customer { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 13.5px; color: var(--text); font-weight: 600; }
.deliv-customer a { color: var(--brand-dark); text-decoration: none; font-weight: 600; font-size: 12.5px; }
.deliv-address { font-size: 12.5px; color: var(--text-2); line-height: 1.4; }
.deliv-address a { color: var(--brand-dark); font-weight: 600; margin-left: 6px; text-decoration: none; }
.deliv-schedule { font-size: 11.5px; color: var(--warning-text); background: var(--warning-light); border-radius: 6px; padding: 3px 8px; display: inline-block; width: fit-content; }

.deliv-items { border-top: 1px dashed var(--border); border-bottom: 1px dashed var(--border); padding: 8px 0; display: flex; flex-direction: column; gap: 4px; }
.deliv-item-row { display: flex; justify-content: space-between; font-size: 12.5px; color: var(--text); gap: 8px; }
.deliv-item-note { font-size: 11px; color: var(--text-3); font-style: italic; margin-left: 14px; }

.deliv-totals { font-size: 12px; color: var(--text-2); display: flex; flex-direction: column; gap: 2px; }
.deliv-totals .grand { font-size: 14px; font-weight: 700; color: var(--text); display: flex; justify-content: space-between; }
.deliv-row { display: flex; justify-content: space-between; }
.deliv-cash-note { font-size: 12px; font-weight: 600; color: var(--warning-text); }
.deliv-proof-thumb { width: 56px; height: 56px; object-fit: cover; border-radius: 8px; border: 1px solid var(--border); cursor: pointer; }

.deliv-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
.deliv-actions button { flex: 1 1 auto; min-width: 100px; }
.deliv-assign-row { display: flex; gap: 6px; align-items: center; }
.deliv-assign-row select { flex: 1; padding: 7px 8px; border-radius: 8px; border: 1px solid var(--border); font-family: inherit; font-size: 12.5px; background: var(--surface); color: var(--text); }
.deliv-pay-row { display: flex; gap: 6px; align-items: center; margin-top: 6px; }
.deliv-pay-row select { flex: 1; padding: 7px 8px; border-radius: 8px; border: 1px solid var(--border); font-family: inherit; font-size: 12.5px; background: var(--surface); color: var(--text); }
.deliv-paid-yes { color: var(--success-text); font-weight: 600; }
.deliv-paid-no { color: var(--warning-text); font-weight: 600; }
.deliv-rejection-reason { font-size: 12px; color: var(--danger-text); background: var(--danger-light); border-radius: 6px; padding: 6px 8px; }

.deliv-empty { text-align: center; padding: 40px 20px; color: var(--text-3); font-size: 13px; }

.deliv-eta-quick, .deliv-reject-quick { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.deliv-modal-input { width: 100%; padding: 9px 10px; border-radius: 8px; border: 1px solid var(--border); font-family: inherit; font-size: 13px; margin-bottom: 4px; background: var(--surface); color: var(--text); }
textarea.deliv-modal-input { min-height: 70px; resize: vertical; }
</style>

<div class="mesio-sec-delivery">
  <div class="deliv-topbar">
    <div class="deliv-title">Domicilios</div>
    <button type="button" class="m-btn m-btn--ghost m-btn--sm" id="deliv-sound-btn" aria-pressed="false">🔈 Activar sonido</button>
  </div>

  <section class="deliv-group deliv-group--pending">
    <h2 class="deliv-group-title">Por aceptar <span class="deliv-count" id="deliv-count-pending">0</span></h2>
    <div class="deliv-cards" id="deliv-list-pending"></div>
  </section>

  <section class="deliv-group">
    <h2 class="deliv-group-title">En curso <span class="deliv-count" id="deliv-count-progress">0</span></h2>
    <div class="deliv-cards" id="deliv-list-progress"></div>
  </section>

  <section class="deliv-group">
    <h2 class="deliv-group-title deliv-collapsible" id="deliv-closed-toggle">
      Cerrados hoy <span class="deliv-count" id="deliv-count-closed">0</span>
      <span class="deliv-chevron" id="deliv-closed-chevron">▸</span>
    </h2>
    <div class="deliv-cards" id="deliv-list-closed" style="display:none;"></div>
  </section>

  <div class="deliv-empty" id="deliv-empty" style="display:none;">No hay pedidos de domicilio o recoger todavía.</div>
</div>

<!-- ETA modal (Aceptar) -->
<div class="m-modal-overlay" id="deliv-eta-modal">
  <div class="m-modal-box">
    <h3>¿En cuántos minutos estará listo?</h3>
    <div class="deliv-eta-quick">
      <button type="button" class="m-btn m-btn--secondary m-btn--sm" data-eta="15">15 min</button>
      <button type="button" class="m-btn m-btn--secondary m-btn--sm" data-eta="25">25 min</button>
      <button type="button" class="m-btn m-btn--secondary m-btn--sm" data-eta="35">35 min</button>
      <button type="button" class="m-btn m-btn--secondary m-btn--sm" data-eta="45">45 min</button>
    </div>
    <input type="number" class="deliv-modal-input" id="deliv-eta-custom" placeholder="Otro número de minutos" min="1" max="240">
    <div class="m-modal-actions" style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px;">
      <button type="button" class="m-btn m-btn--ghost" id="deliv-eta-cancel">Cancelar</button>
      <button type="button" class="m-btn m-btn--primary" id="deliv-eta-custom-confirm">Aceptar con este tiempo</button>
    </div>
  </div>
</div>

<!-- Reject modal -->
<div class="m-modal-overlay" id="deliv-reject-modal">
  <div class="m-modal-box">
    <h3>Motivo del rechazo</h3>
    <div class="deliv-reject-quick">
      <button type="button" class="m-btn m-btn--ghost m-btn--sm" data-reason="Sin repartidores disponibles">Sin repartidores</button>
      <button type="button" class="m-btn m-btn--ghost m-btn--sm" data-reason="Estamos muy ocupados en este momento">Muy ocupados</button>
      <button type="button" class="m-btn m-btn--ghost m-btn--sm" data-reason="Fuera de nuestro horario de atención">Fuera de horario</button>
      <button type="button" class="m-btn m-btn--ghost m-btn--sm" data-reason="Ese plato ya no está disponible">Plato no disponible</button>
    </div>
    <textarea class="deliv-modal-input" id="deliv-reject-text" placeholder="Escribe el motivo…" maxlength="300"></textarea>
    <div class="m-modal-actions" style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px;">
      <button type="button" class="m-btn m-btn--ghost" id="deliv-reject-cancel">Cancelar</button>
      <button type="button" class="m-btn m-btn--danger" id="deliv-reject-custom-confirm">Rechazar con este motivo</button>
    </div>
  </div>
</div>

<!-- Payment proof lightbox -->
<div class="m-modal-overlay" id="deliv-proof-modal">
  <div class="m-modal-box" style="max-width:460px;">
    <h3>Comprobante de pago</h3>
    <img id="deliv-proof-img" src="" alt="Comprobante de pago" style="width:100%;border-radius:8px;margin-top:8px;">
    <div class="m-modal-actions" style="display:flex;justify-content:flex-end;margin-top:10px;">
      <button type="button" class="m-btn m-btn--ghost" id="deliv-proof-close">Cerrar</button>
    </div>
  </div>
</div>
`;

  // ── Cleanup tracking ─────────────────────────────────
  var _intervalHandles = [];
  var _rtUnsubs = [];
  function _trackInterval(id) { _intervalHandles.push(id); return id; }

  // ── XSS helper ──────────────────────────────────────
  function _esc(s) {
    var el = document.createElement('div');
    el.textContent = String(s == null ? '' : s);
    return el.innerHTML;
  }

  // ── State ─────────────────────────────────────────────
  var _orders = [];
  var _couriers = [];
  var _inFlight = Object.create(null);   // order id -> true while an action is in flight
  var _seenPendingIds = null;            // Set — null on first load (suppress the initial ding)
  var _closedExpanded = false;
  var _modalOrderId = null;              // order the open ETA/reject modal targets

  var _PENDING_STATUS = 'pendiente_aceptacion';
  var _IN_PROGRESS_STATUSES = ['en_preparacion', 'listo', 'en_camino', 'en_puerta'];
  var _CLOSED_STATUSES = ['entregado', 'rechazado', 'cancelado'];

  // ── Sound preference (per device, opt-in — same pattern as
  // kitchen.js/bar.js: browsers block autoplay until a user gesture). ─────
  function _soundEnabled() {
    try { return localStorage.getItem('rb_delivery_sound') === '1'; } catch (e) { return false; }
  }
  function _setSoundEnabled(v) {
    try { localStorage.setItem('rb_delivery_sound', v ? '1' : '0'); } catch (e) { /* private mode etc. */ }
  }
  function _updateSoundBtn(btn) {
    var on = _soundEnabled();
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.textContent = on ? '🔊 Sonido activado' : '🔈 Activar sonido';
  }

  // ── Date helpers — created_at/etc. come back as a naive ISO string
  // (orders.created_at is TIMESTAMP WITHOUT TIME ZONE, stored in UTC —
  // see docs/claude/testing.md gotcha #5); treat it as UTC explicitly,
  // same fix already applied in courier.js's _renderActive(). ─────────────
  function _asUtcDate(iso) {
    // Handles both naive and "+00:00" timestamps — see mesio-utils.js.
    return mesioParseServerDate(iso);
  }
  function _elapsedLabel(iso) {
    var d = _asUtcDate(iso);
    if (!d) return '';
    var mins = Math.max(0, Math.floor((Date.now() - d.getTime()) / 60000));
    if (mins < 1) return 'justo ahora';
    if (mins < 60) return 'hace ' + mins + ' min';
    var hrs = Math.floor(mins / 60);
    return 'hace ' + hrs + 'h ' + (mins % 60) + 'm';
  }
  function _isToday(iso) {
    var d = _asUtcDate(iso);
    if (!d) return false;
    var now = new Date();
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  }
  function _closedTimestamp(o) {
    return o.delivered_at || o.rejected_at || o.cancelled_at || o.created_at;
  }
  function _fmtTime(iso) {
    var d = _asUtcDate(iso);
    if (!d) return '';
    return d.toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
  }

  function _mapsLink(o) {
    if (o.delivery_lat != null && o.delivery_lon != null) {
      return 'https://www.google.com/maps?q=' + o.delivery_lat + ',' + o.delivery_lon;
    }
    return 'https://www.google.com/maps?q=' + encodeURIComponent(o.address || '');
  }
  function _telLink(phone) {
    var digits = (phone || '').replace(/\D/g, '');
    return 'tel:+' + digits;
  }

  // ── Fetch ─────────────────────────────────────────────
  async function fetchOrders() {
    try {
      var res = await fetch('/api/staff/delivery/orders', { headers: mesioHeaders() });
      mesioTrackFetch(res.ok);
      if (res.status === 401) { window.location.href = '/login'; return; }
      if (!res.ok) return;
      var data = await res.json();
      _orders = data.orders || [];
      _detectNewPending(_orders);
      _renderAll();
    } catch (e) { mesioTrackFetch(false); }
  }

  async function fetchCouriers() {
    try {
      var res = await fetch('/api/staff/delivery/couriers', { headers: mesioHeaders() });
      if (!res.ok) return;
      var data = await res.json();
      _couriers = data.couriers || [];
      _renderAll(); // re-render so any already-visible assign selects get options
    } catch (e) { /* non-critical — assign selects just show empty until retried */ }
  }

  // ── New-pending detection (sound + desktop notification + badge) ────────
  function _detectNewPending(orders) {
    var pending = orders.filter(function (o) { return o.status === _PENDING_STATUS; });
    var currentIds = {};
    pending.forEach(function (o) { currentIds[o.id] = true; });

    if (_seenPendingIds === null) {
      _seenPendingIds = currentIds;
      return;
    }
    var newOnes = pending.filter(function (o) { return !_seenPendingIds[o.id]; });
    _seenPendingIds = currentIds;
    if (!newOnes.length) return;

    if (!document.hidden && _soundEnabled()) mesioDing();
    if (newOnes.length === 1) {
      mesioNotify('Nuevo pedido — Domicilios', (newOnes[0].customer_name || 'Cliente') + ' · ' + mesioFmt(newOnes[0].total || 0));
    } else {
      mesioNotify('Nuevos pedidos — Domicilios', newOnes.length + ' pedidos nuevos por aceptar');
    }
  }

  // ── Card rendering ────────────────────────────────────
  function _itemsHtml(items) {
    if (!Array.isArray(items) || !items.length) return '';
    return '<div class="deliv-items">' + items.map(function (it) {
      var qty = it.quantity != null ? it.quantity : (it.qty || 1);
      var lineTotal = it.subtotal != null ? it.subtotal : (it.price != null ? it.price * qty : null);
      var priceHtml = lineTotal != null ? mesioFmt(lineTotal) : '';
      var row = '<div class="deliv-item-row"><span>' + _esc(qty) + '× ' + _esc(it.name || '') + '</span><span>' + priceHtml + '</span></div>';
      if (it.note || it.notes) row += '<div class="deliv-item-note">"' + _esc(it.note || it.notes) + '"</div>';
      return row;
    }).join('') + '</div>';
  }

  function _totalsHtml(o) {
    var lines = '';
    lines += '<div class="deliv-row"><span>Subtotal</span><span>' + mesioFmt(o.subtotal || 0) + '</span></div>';
    if (o.order_type === 'domicilio') {
      lines += '<div class="deliv-row"><span>Domicilio</span><span>' + mesioFmt(o.delivery_fee || 0) + '</span></div>';
    }
    if (o.tip_amount) {
      lines += '<div class="deliv-row"><span>Propina</span><span>' + mesioFmt(o.tip_amount) + '</span></div>';
    }
    var grand = '<div class="grand"><span>Total</span><span>' + mesioFmt(o.total || 0) + '</span></div>';
    var cash = '';
    if (o.payment_method === 'efectivo' && o.cash_change_for != null) {
      cash += '<div class="deliv-cash-note">Paga con ' + mesioFmt(o.cash_change_for) + '</div>';
      var change = Number(o.cash_change_for) - Number(o.total || 0);
      if (change > 0) cash += '<div class="deliv-cash-note">Cambio: ' + mesioFmt(change) + '</div>';
    }
    var methodLabel = _METHOD_LABELS[o.payment_method] || o.payment_method || '';
    // Whether the money is actually IN is not the same question as which
    // method the customer picked at checkout — a transfer with a receipt
    // still pending review reads 'Nequi · Pendiente' until a cashier
    // checks the bank and registers it.
    var paidCls = o.paid ? 'deliv-paid-yes' : 'deliv-paid-no';
    var paidLabel = o.paid ? 'Cobrado' : 'Pendiente';
    return '<div class="deliv-totals">' + lines + grand
      + '<div class="deliv-row"><span>Pago</span><span>' + _esc(methodLabel)
        + ' · <span class="' + paidCls + '">' + paidLabel + '</span></span></div>' + cash + '</div>';
  }

  // Keys must match app/services/delivery.ALLOWED_PAYMENT_METHODS — the
  // server rejects anything else with a 400.
  var _METHOD_LABELS = {
    efectivo: 'Efectivo', tarjeta: 'Tarjeta', nequi: 'Nequi', bancolombia: 'Bancolombia',
  };

  // Registering the payment is what closes the loop on a transfer: the
  // customer uploads a receipt, the cashier checks the bank and confirms it
  // here. Until this existed no web order could ever be paid, so delivery
  // sales never reached the owner's totals at all.
  function _payRowHtml(o) {
    var options = Object.keys(_METHOD_LABELS).map(function (key) {
      var selected = key === o.payment_method ? ' selected' : '';
      return '<option value="' + _esc(key) + '"' + selected + '>' + _esc(_METHOD_LABELS[key]) + '</option>';
    }).join('');
    return '<div class="deliv-pay-row">'
      + '<select data-order="' + _esc(o.id) + '" class="deliv-pay-select" aria-label="Cómo pagó el cliente">' + options + '</select>'
      + '<button type="button" class="m-btn m-btn--secondary m-btn--sm" data-action="mark-paid" data-order="' + _esc(o.id) + '"' + (_inFlight[o.id] ? ' disabled' : '') + '>Registrar pago</button>'
      + '</div>';
  }

  function _courierSelectHtml(o) {
    var options = '<option value="">Elegir domiciliario…</option>' + _couriers.map(function (c) {
      var selected = c.id === o.courier_staff_id ? ' selected' : '';
      return '<option value="' + _esc(c.id) + '"' + selected + '>' + _esc(c.name) + '</option>';
    }).join('');
    return '<div class="deliv-assign-row">'
      + '<select data-order="' + _esc(o.id) + '" class="deliv-courier-select">' + options + '</select>'
      + '<button type="button" class="m-btn m-btn--secondary m-btn--sm" data-action="assign" data-order="' + _esc(o.id) + '"' + (_inFlight[o.id] ? ' disabled' : '') + '>Asignar</button>'
      + '</div>';
  }

  function _actionsHtml(o) {
    var busy = !!_inFlight[o.id];
    var dis = busy ? ' disabled' : '';
    var btns = [];

    if (o.status === _PENDING_STATUS) {
      btns.push('<button type="button" class="m-btn m-btn--primary" data-action="accept" data-order="' + _esc(o.id) + '"' + dis + '>Aceptar</button>');
      btns.push('<button type="button" class="m-btn m-btn--danger" data-action="reject" data-order="' + _esc(o.id) + '"' + dis + '>Rechazar</button>');
      return '<div class="deliv-actions">' + btns.join('') + '</div>';
    }

    var html = '';
    if (o.order_type === 'domicilio' && (o.status === 'en_preparacion' || o.status === 'listo' || o.status === 'en_camino' || o.status === 'en_puerta')) {
      html += _courierSelectHtml(o);
    }
    if (o.order_type === 'domicilio' && (o.status === 'en_preparacion' || o.status === 'listo')) {
      btns.push('<button type="button" class="m-btn m-btn--secondary" data-action="en-route" data-order="' + _esc(o.id) + '"' + dis + '>En camino</button>');
    }
    if (o.order_type === 'recoger' && (o.status === 'en_preparacion' || o.status === 'listo')) {
      btns.push('<button type="button" class="m-btn m-btn--primary" data-action="delivered" data-order="' + _esc(o.id) + '"' + dis + '>Entregado al cliente</button>');
    }
    if (o.order_type === 'domicilio' && (o.status === 'en_camino' || o.status === 'en_puerta')) {
      btns.push('<button type="button" class="m-btn m-btn--primary" data-action="delivered" data-order="' + _esc(o.id) + '"' + dis + '>Entregado</button>');
    }
    if (btns.length) html += '<div class="deliv-actions">' + btns.join('') + '</div>';
    // Cancelled/rejected orders are not revenue — no payment to register.
    if (!o.paid && o.status !== 'cancelado' && o.status !== 'rechazado') {
      html += _payRowHtml(o);
    }
    return html;
  }

  function _cardHtml(o, variant) {
    var typeLabel = o.order_type === 'recoger' ? 'Recoger' : 'Domicilio';
    var typeClass = o.order_type === 'recoger' ? 'recoger' : 'domicilio';
    var code = o.public_code || String(o.id).slice(-6).toUpperCase();

    var scheduleHtml = '';
    if (o.scheduled_pickup_at) {
      scheduleHtml = '<div class="deliv-schedule">Programado: ' + _esc(_fmtTime(o.scheduled_pickup_at)) + '</div>';
    }

    var proofHtml = '';
    if (o.proof_url) {
      var thumb = (typeof mesioImageUrl === 'function') ? mesioImageUrl(o.proof_url, 'thumb') : o.proof_url;
      proofHtml = '<img class="deliv-proof-thumb" src="' + _esc(thumb) + '" data-action="proof" data-url="' + _esc(o.proof_url) + '" alt="Comprobante de pago" title="Ver comprobante completo">';
    }

    var rejectionHtml = '';
    if (o.status === 'rechazado' && o.rejection_reason) {
      rejectionHtml = '<div class="deliv-rejection-reason">Rechazado: ' + _esc(o.rejection_reason) + '</div>';
    }

    var statusLabels = {
      en_preparacion: 'En preparación', listo: 'Listo', en_camino: 'En camino', en_puerta: 'En la puerta',
      entregado: 'Entregado', rechazado: 'Rechazado', cancelado: 'Cancelado',
    };

    return '<div class="deliv-card ' + (variant || '') + '" data-card-order="' + _esc(o.id) + '">'
      + '<div class="deliv-card-head">'
        + '<span class="deliv-code">#' + _esc(code) + '</span>'
        + '<span class="deliv-type-badge ' + typeClass + '">' + typeLabel + '</span>'
        + (variant !== 'deliv-card--pending' ? '<span class="deliv-status-pill ' + _esc(o.status) + '">' + _esc(statusLabels[o.status] || o.status) + '</span>' : '')
        + '<span class="deliv-elapsed">' + _esc(_elapsedLabel(o.created_at)) + '</span>'
      + '</div>'
      + '<div class="deliv-customer"><span>' + _esc(o.customer_name || 'Cliente') + '</span>'
        + (o.customer_phone ? '<a href="' + _telLink(o.customer_phone) + '">📞 ' + _esc(o.customer_phone) + '</a>' : '')
      + '</div>'
      + '<div class="deliv-address">' + _esc(o.address || (o.order_type === 'recoger' ? 'Recoge en el local' : 'Sin dirección'))
        + (o.order_type === 'domicilio' ? '<a href="' + _esc(_mapsLink(o)) + '" target="_blank" rel="noopener">Ver mapa</a>' : '')
      + '</div>'
      + scheduleHtml
      + _itemsHtml(o.items)
      + (o.notes ? '<div class="deliv-item-note">Nota del pedido: "' + _esc(o.notes) + '"</div>' : '')
      + _totalsHtml(o)
      + (proofHtml ? '<div>' + proofHtml + '</div>' : '')
      + rejectionHtml
      + _actionsHtml(o)
      + '</div>';
  }

  // ── Group + render ────────────────────────────────────
  function _renderAll() {
    var pending = _orders.filter(function (o) { return o.status === _PENDING_STATUS; })
      .sort(function (a, b) { return new Date(a.created_at) - new Date(b.created_at); });
    var inProgress = _orders.filter(function (o) { return _IN_PROGRESS_STATUSES.indexOf(o.status) !== -1; })
      .sort(function (a, b) { return new Date(a.created_at) - new Date(b.created_at); });
    var closedToday = _orders.filter(function (o) { return _CLOSED_STATUSES.indexOf(o.status) !== -1 && _isToday(_closedTimestamp(o)); })
      .sort(function (a, b) { return new Date(_closedTimestamp(b)) - new Date(_closedTimestamp(a)); });

    var pendingEl = document.getElementById('deliv-list-pending');
    var progressEl = document.getElementById('deliv-list-progress');
    var closedEl = document.getElementById('deliv-list-closed');
    if (!pendingEl) return; // section unmounted mid-fetch

    pendingEl.innerHTML = pending.length
      ? pending.map(function (o) { return _cardHtml(o, 'deliv-card--pending'); }).join('')
      : '<div class="deliv-empty">Sin pedidos por aceptar.</div>';
    progressEl.innerHTML = inProgress.length
      ? inProgress.map(function (o) { return _cardHtml(o, ''); }).join('')
      : '<div class="deliv-empty">Sin pedidos en curso.</div>';
    closedEl.innerHTML = closedToday.length
      ? closedToday.map(function (o) { return _cardHtml(o, 'deliv-card--closed'); }).join('')
      : '<div class="deliv-empty">Sin pedidos cerrados hoy.</div>';

    var setCount = function (id, n) { var el = document.getElementById(id); if (el) el.textContent = String(n); };
    setCount('deliv-count-pending', pending.length);
    setCount('deliv-count-progress', inProgress.length);
    setCount('deliv-count-closed', closedToday.length);

    var empty = document.getElementById('deliv-empty');
    if (empty) empty.style.display = _orders.length ? 'none' : '';

    if (window.MesioStaffShell) MesioStaffShell.setBadge('delivery', pending.length);
  }

  // ── Server action helper ─────────────────────────────
  async function _postDelivery(orderId, path, body) {
    try {
      var res = await fetch('/api/staff/delivery/orders/' + encodeURIComponent(orderId) + path, {
        method: 'POST', headers: mesioHeaders(),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      if (res.ok) { await fetchOrders(); return true; }

      var detail = 'No se pudo actualizar el pedido';
      try {
        var data = await res.json();
        if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      } catch (e) { /* non-JSON error body */ }

      if (res.status === 409) {
        mesioToast('Este pedido ya cambió de estado — actualizando la lista', 'warning');
        await fetchOrders();
      } else if (res.status === 404) {
        mesioToast('Este pedido ya no está en tu sede — actualizando la lista', 'warning');
        await fetchOrders();
      } else {
        mesioToast(detail, 'error');
      }
      return false;
    } catch (e) {
      mesioToast('Error de conexión al actualizar el pedido', 'error');
      return false;
    }
  }

  async function _runAction(orderId, fn) {
    if (_inFlight[orderId]) return;
    _inFlight[orderId] = true;
    _renderAll();
    try { await fn(); }
    finally { delete _inFlight[orderId]; _renderAll(); }
  }

  // ── ETA modal (Aceptar) ───────────────────────────────
  function openEtaModal(orderId) {
    _modalOrderId = orderId;
    var custom = document.getElementById('deliv-eta-custom');
    if (custom) custom.value = '';
    var modal = document.getElementById('deliv-eta-modal');
    if (modal) modal.classList.add('open');
  }
  function closeEtaModal() {
    var modal = document.getElementById('deliv-eta-modal');
    if (modal) modal.classList.remove('open');
    _modalOrderId = null;
  }
  function confirmAccept(etaMinutes) {
    var orderId = _modalOrderId;
    var minutes = parseInt(etaMinutes, 10);
    if (!orderId || !minutes || minutes < 1) { mesioToast('Ingresa un número de minutos válido', 'warning'); return; }
    closeEtaModal();
    _runAction(orderId, function () { return _postDelivery(orderId, '/accept', { eta_minutes: minutes }); });
  }

  // ── Reject modal ──────────────────────────────────────
  function openRejectModal(orderId) {
    _modalOrderId = orderId;
    var text = document.getElementById('deliv-reject-text');
    if (text) text.value = '';
    var modal = document.getElementById('deliv-reject-modal');
    if (modal) modal.classList.add('open');
  }
  function closeRejectModal() {
    var modal = document.getElementById('deliv-reject-modal');
    if (modal) modal.classList.remove('open');
    _modalOrderId = null;
  }
  function confirmReject(reason) {
    var orderId = _modalOrderId;
    var trimmed = (reason || '').trim();
    if (!orderId || !trimmed) { mesioToast('El motivo de rechazo es obligatorio', 'warning'); return; }
    closeRejectModal();
    _runAction(orderId, function () { return _postDelivery(orderId, '/reject', { reason: trimmed }); });
  }

  // ── Proof lightbox ────────────────────────────────────
  function openProofModal(url) {
    var img = document.getElementById('deliv-proof-img');
    if (img) img.src = url;
    var modal = document.getElementById('deliv-proof-modal');
    if (modal) modal.classList.add('open');
  }
  function closeProofModal() {
    var modal = document.getElementById('deliv-proof-modal');
    if (modal) modal.classList.remove('open');
    var img = document.getElementById('deliv-proof-img');
    if (img) img.src = '';
  }

  // ── Delegated click handling (cards are re-rendered wholesale on every
  // fetch, so listeners are attached once on the stable containers). ──────
  function _onContainerClick(e) {
    var target = e.target.closest('[data-action]');
    if (!target) return;
    var action = target.dataset.action;
    var orderId = target.dataset.order;

    if (action === 'accept') { openEtaModal(orderId); return; }
    if (action === 'reject') { openRejectModal(orderId); return; }
    if (action === 'proof') { openProofModal(target.dataset.url); return; }
    if (action === 'en-route') { _runAction(orderId, function () { return _postDelivery(orderId, '/en-route'); }); return; }
    if (action === 'delivered') { _runAction(orderId, function () { return _postDelivery(orderId, '/delivered'); }); return; }
    if (action === 'mark-paid') {
      var paySelect = document.querySelector('select.deliv-pay-select[data-order="' + orderId + '"]');
      var method = paySelect ? paySelect.value : '';
      if (!method) { mesioToast('Elige cómo pagó el cliente', 'warning'); return; }
      _runAction(orderId, function () { return _postDelivery(orderId, '/mark-paid', { payment_method: method }); });
      return;
    }
    if (action === 'assign') {
      var select = document.querySelector('select.deliv-courier-select[data-order="' + orderId + '"]');
      var courierId = select ? select.value : '';
      if (!courierId) { mesioToast('Elige un domiciliario primero', 'warning'); return; }
      _runAction(orderId, function () { return _postDelivery(orderId, '/assign-courier', { courier_staff_id: courierId }); });
    }
  }

  // ── mount / unmount ───────────────────────────────────
  function mount(container) {
    container.innerHTML = TEMPLATE;
    _orders = [];
    _seenPendingIds = null;
    _closedExpanded = false;
    _modalOrderId = null;

    container.addEventListener('click', _onContainerClick);

    // Sound toggle (same unlock-on-click pattern as kitchen.js/bar.js).
    var soundBtn = document.getElementById('deliv-sound-btn');
    if (soundBtn) {
      _updateSoundBtn(soundBtn);
      soundBtn.addEventListener('click', function () {
        var next = !_soundEnabled();
        _setSoundEnabled(next);
        _updateSoundBtn(soundBtn);
        if (next) mesioDing();
      });
    }
    if (window.mesioRequestNotificationPermission) mesioRequestNotificationPermission();

    // "Cerrados hoy" collapse toggle.
    var closedToggle = document.getElementById('deliv-closed-toggle');
    if (closedToggle) {
      closedToggle.addEventListener('click', function () {
        _closedExpanded = !_closedExpanded;
        var list = document.getElementById('deliv-list-closed');
        var chevron = document.getElementById('deliv-closed-chevron');
        if (list) list.style.display = _closedExpanded ? '' : 'none';
        if (chevron) chevron.classList.toggle('open', _closedExpanded);
      });
    }

    // ETA modal wiring.
    document.querySelectorAll('#deliv-eta-modal [data-eta]').forEach(function (btn) {
      btn.addEventListener('click', function () { confirmAccept(btn.dataset.eta); });
    });
    var etaCustomConfirm = document.getElementById('deliv-eta-custom-confirm');
    if (etaCustomConfirm) etaCustomConfirm.addEventListener('click', function () {
      var input = document.getElementById('deliv-eta-custom');
      confirmAccept(input ? input.value : '');
    });
    var etaCancel = document.getElementById('deliv-eta-cancel');
    if (etaCancel) etaCancel.addEventListener('click', closeEtaModal);

    // Reject modal wiring.
    document.querySelectorAll('#deliv-reject-modal [data-reason]').forEach(function (btn) {
      btn.addEventListener('click', function () { confirmReject(btn.dataset.reason); });
    });
    var rejectCustomConfirm = document.getElementById('deliv-reject-custom-confirm');
    if (rejectCustomConfirm) rejectCustomConfirm.addEventListener('click', function () {
      var text = document.getElementById('deliv-reject-text');
      confirmReject(text ? text.value : '');
    });
    var rejectCancel = document.getElementById('deliv-reject-cancel');
    if (rejectCancel) rejectCancel.addEventListener('click', closeRejectModal);

    // Proof lightbox wiring.
    var proofClose = document.getElementById('deliv-proof-close');
    if (proofClose) proofClose.addEventListener('click', closeProofModal);

    // Close any open modal on overlay click / Escape (matches floorplan.js's
    // existing convention for .m-modal-overlay).
    document.querySelectorAll('.m-modal-overlay').forEach(function (overlay) {
      overlay.addEventListener('click', function (e) { if (e.target === overlay) overlay.classList.remove('open'); });
    });

    fetchOrders();
    fetchCouriers();
    _trackInterval(mesioLiveInterval(fetchOrders, 10000));

    // Real-time invalidation — SSE events (shared connection opened once by
    // staff-shell.js's boot()) call fetchOrders() immediately; the interval
    // above is just the 60s safety net once MesioRealtime is connected.
    if (window.MesioRealtime) {
      ['order.created', 'order.updated', 'resync'].forEach(function (topic) {
        _rtUnsubs.push(MesioRealtime.on(topic, fetchOrders));
      });
    }
  }

  function unmount(container) {
    _intervalHandles.forEach(function (id) { clearInterval(id); });
    _intervalHandles = [];
    _rtUnsubs.forEach(function (off) { off(); });
    _rtUnsubs = [];
    _inFlight = Object.create(null);
    if (container) {
      container.removeEventListener('click', _onContainerClick);
      container.innerHTML = '';
    }
  }

  window.MesioStaffSections = window.MesioStaffSections || {};
  window.MesioStaffSections.delivery = { mount: mount, unmount: unmount };
})();
