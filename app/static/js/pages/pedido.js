/* ═══════════════════════════════════════════════════════════════════
   Mesio — Customer status page (docs/claude/delivery-web.md chunk 6)
   /pedido/{public_code}

   PUBLIC, unauthenticated page — the public_code in the URL is the only
   thing this page needs to read an order's status. Every value coming
   back from the API is untrusted display data, so every render helper
   below uses textContent (never innerHTML) for it.

   Realtime: a device that still holds the diner_sessions token that
   CREATED this order (DinerSession.load(), sessionStorage — usually the
   same tab, right after checkout) opens the SSE stream via MesioRealtime
   and gets pushed "delivery_order.updated"/"resync" events; a device that
   only has the link (no token — a different tab, a later visit, someone
   else's phone) has nothing to authenticate an SSE connection with, so it
   never calls MesioRealtime.connect() at all.

   Both cases share ONE polling loop, mesioLiveInterval(loadOrder, 10000)
   (app/static/js/mesio-utils.js): it polls every 10s UNLESS
   MesioRealtime.connected is true, in which case it backs off to a 60s
   safety net — exactly "SSE + 60s net" for the token-holding case, and a
   plain 10s poll for the link-only case, with no separate code path
   needed for either.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  function getPublicCodeFromPath() {
    var parts = window.location.pathname.split('/').filter(Boolean);
    // Path is always /pedido/{code} (see app/routes/dashboard.py::
    // diner_delivery_status_page) — the code segment is index 1.
    return parts.length >= 2 ? decodeURIComponent(parts[1]) : '';
  }

  var CODE = getPublicCodeFromPath();
  var liveStopped = false;
  var liveIntervalId = null;

  var TIMELINE_STEPS = [
    { key: 'pendiente_aceptacion', label: 'Esperando confirmación del restaurante' },
    { key: 'en_preparacion', label: 'En preparación' },
    { key: 'listo', label: 'Listo' },
    { key: 'en_camino', label: 'En camino' },
    { key: 'entregado', label: 'Entregado' },
  ];

  var STATUS_TITLES = {
    pendiente_aceptacion: 'Esperando confirmación del restaurante',
    en_preparacion: 'En preparación',
    listo: 'Listo',
    en_camino: 'En camino',
    en_puerta: 'El repartidor está muy cerca',
  };

  function statusIndex(status) {
    for (var i = 0; i < TIMELINE_STEPS.length; i++) {
      if (TIMELINE_STEPS[i].key === status) return i;
    }
    return -1;
  }

  function el(tag, cls) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }

  function textEl(tag, cls, text) {
    var e = el(tag, cls);
    e.textContent = text == null ? '' : String(text);
    return e;
  }

  function fmtMoney(n) {
    try { return mesioFmt(n || 0); } catch (e) { return String(n || 0); }
  }

  function getSessionToken() {
    try {
      var session = DinerSession.load();
      return (session && session.token) || null;
    } catch (e) {
      return null;
    }
  }

  // ── Data load ────────────────────────────────────────────────────────

  async function loadOrder() {
    var data;
    try {
      data = await DinerSession.fetch(`/api/diner/order/${encodeURIComponent(CODE)}`, 'GET', null, null);
    } catch (e) {
      renderError(e);
      return;
    }
    render(data);
    maybeStopLiveUpdates(data);
  }

  function renderError(e) {
    var loading = document.getElementById('pedido-loading');
    var content = document.getElementById('pedido-content');
    if (loading) loading.hidden = true;
    if (!content) return;
    content.hidden = false;
    content.innerHTML = '';
    var box = el('div', 'pedido-error-state');
    var isNotFound = e && e.status === 404;
    box.appendChild(textEl(
      'p', null,
      isNotFound
        ? 'No encontramos este pedido. Verifica el enlace.'
        : ((e && e.message) || 'No pudimos cargar tu pedido. Intenta de nuevo.'),
    ));
    content.appendChild(box);
    // A 404 is permanent for this code — stop hammering the endpoint.
    if (isNotFound) stopLiveUpdates();
  }

  // ── Render ───────────────────────────────────────────────────────────

  function buildTimeline(status) {
    var list = el('ul', 'pedido-timeline');
    if (status === 'rechazado' || status === 'cancelado') return list;
    var idx = statusIndex(status);
    TIMELINE_STEPS.forEach(function (step, i) {
      var cls = 'pedido-timeline-step';
      if (i < idx) cls += ' is-done';
      else if (i === idx) cls += ' is-current';
      var li = el('li', cls);
      li.appendChild(el('span', 'dot'));
      li.appendChild(textEl('span', null, step.label));
      list.appendChild(li);
    });
    return list;
  }

  function buildStatusCard(data) {
    var card = el('div', 'pedido-card');
    var status = data.status;

    if (status === 'rechazado') {
      card.appendChild(textEl('p', 'pedido-status-title', 'Tu pedido fue rechazado'));
      card.appendChild(textEl('p', 'pedido-status-sub is-error', data.rejection_reason || 'El restaurante no indicó un motivo.'));
    } else if (status === 'cancelado') {
      card.appendChild(textEl('p', 'pedido-status-title', 'Pedido cancelado'));
      card.appendChild(textEl('p', 'pedido-status-sub', 'Cancelaste este pedido.'));
    } else if (status === 'entregado') {
      card.appendChild(textEl('p', 'pedido-status-title', 'Pedido entregado'));
      card.appendChild(textEl('p', 'pedido-status-sub is-ok', '¡Buen provecho!'));
    } else {
      card.appendChild(textEl('p', 'pedido-status-title', STATUS_TITLES[status] || status || ''));
      if (status === 'pendiente_aceptacion') {
        card.appendChild(textEl('p', 'pedido-status-sub', 'Te avisaremos apenas el restaurante confirme tu pedido.'));
      } else if (data.eta && data.eta.local_label) {
        var label = data.order_type === 'recoger'
          ? ('Listo aprox. a las ' + data.eta.local_label)
          : ('Llega aprox. a las ' + data.eta.local_label);
        card.appendChild(textEl('p', 'pedido-status-sub', label));
      }
    }

    card.appendChild(buildTimeline(status));
    return card;
  }

  function buildItemsCard(data) {
    var card = el('div', 'pedido-card');
    var list = el('ul', 'pedido-items-list');
    (data.items || []).forEach(function (item) {
      var row = el('li', 'pedido-item-row');
      var left = el('div', null);
      left.appendChild(textEl('div', 'pedido-item-name', (item.quantity || 1) + 'x ' + (item.name || '')));
      if (item.note) left.appendChild(textEl('div', 'pedido-item-note', item.note));
      row.appendChild(left);
      row.appendChild(textEl('div', 'pedido-item-subtotal', fmtMoney(item.subtotal)));
      list.appendChild(row);
    });
    card.appendChild(list);

    var totals = el('div', null);
    function totalRow(label, value) {
      var row = el('div', 'pedido-totals-row');
      row.appendChild(textEl('span', null, label));
      row.appendChild(textEl('span', null, fmtMoney(value)));
      totals.appendChild(row);
    }
    totalRow('Subtotal', data.subtotal);
    if (data.order_type === 'domicilio') totalRow('Domicilio', data.delivery_fee);
    if (data.tip_amount) totalRow('Propina', data.tip_amount);
    var totalRowEl = el('div', 'pedido-totals-row is-total');
    totalRowEl.appendChild(textEl('span', null, 'Total'));
    totalRowEl.appendChild(textEl('span', null, fmtMoney(data.total)));
    totals.appendChild(totalRowEl);
    card.appendChild(totals);

    if (data.payment_method) {
      // A customer who paid by transfer uploads a receipt and then waits. Say
      // whether the restaurant took it as received, not just which method
      // they picked — "Pago: nequi" alone told them nothing.
      var payLine = 'Pago: ' + data.payment_method
        + (data.paid ? ' · confirmado' : ' · pendiente de confirmar');
      card.appendChild(textEl('p', 'pedido-status-sub', payLine));
    }
    if (data.order_type === 'domicilio' && data.address) {
      card.appendChild(textEl('p', 'pedido-status-sub', 'Dirección: ' + data.address));
    }
    return card;
  }

  function buildActions(data) {
    var wrap = el('div', 'pedido-actions');
    if (data.location_phone) {
      var link = document.createElement('a');
      link.href = 'tel:' + data.location_phone.replace(/[^0-9+]/g, '');
      link.className = 'm-btn m-btn--secondary';
      link.textContent = 'Llamar al restaurante';
      wrap.appendChild(link);
    }
    if (data.can_cancel) {
      var cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'm-btn m-btn--danger';
      cancelBtn.textContent = 'Cancelar pedido';
      cancelBtn.addEventListener('click', onCancelClick);
      wrap.appendChild(cancelBtn);
    }
    return wrap;
  }

  async function onCancelClick() {
    var ok = await mesioConfirm('¿Seguro que quieres cancelar este pedido?', { confirmText: 'Sí, cancelar', danger: true });
    if (!ok) return;
    var token = getSessionToken();
    try {
      await DinerSession.fetch(`/api/diner/order/${encodeURIComponent(CODE)}/cancel`, 'POST', {}, token);
      mesioToast('Pedido cancelado.', 'success');
      loadOrder();
    } catch (e) {
      // A 403 (wrong/missing token — "another device holding only the
      // link") still reloads so the page reflects reality instead of
      // staying stuck on a Cancel button that will never work here.
      mesioToast((e && e.message) || 'No pudimos cancelar tu pedido.', 'error', 5000);
      if (e && e.status === 403) loadOrder();
    }
  }

  function buildNpsCard(data) {
    if (!data.nps || !data.nps.eligible) return null;
    var card = el('div', 'pedido-card');
    if (data.nps.already_submitted) {
      card.appendChild(textEl('p', 'pedido-status-title', '¡Gracias por tu calificación!'));
      return card;
    }

    card.appendChild(textEl('p', 'pedido-status-title', '¿Cómo calificarías tu pedido?'));
    var scoresWrap = el('div', 'pedido-nps-scores');
    var commentBox = document.createElement('textarea');
    commentBox.className = 'pedido-nps-comment';
    commentBox.placeholder = 'Cuéntanos qué pasó (opcional)';
    commentBox.hidden = true;

    var submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.className = 'm-btn m-btn--primary';
    submitBtn.textContent = 'Enviar calificación';
    submitBtn.hidden = true;

    var selected = null;
    for (var s = 1; s <= 5; s++) {
      (function (score) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'pedido-nps-score-btn';
        btn.textContent = String(score);
        btn.addEventListener('click', function () {
          selected = score;
          Array.prototype.forEach.call(scoresWrap.children, function (c) { c.classList.remove('is-selected'); });
          btn.classList.add('is-selected');
          if (score <= 3) {
            // "a score of 3 or less asks for an optional comment"
            // (docs/claude/delivery-web.md chunk 6) — 4/5 submit right away.
            commentBox.hidden = false;
            submitBtn.hidden = false;
          } else {
            commentBox.hidden = true;
            submitBtn.hidden = true;
            submitNps(score, '');
          }
        });
        scoresWrap.appendChild(btn);
      })(s);
    }
    card.appendChild(scoresWrap);
    card.appendChild(commentBox);
    card.appendChild(submitBtn);

    submitBtn.addEventListener('click', function () {
      if (selected == null) return;
      submitNps(selected, commentBox.value || '');
    });

    var skipBtn = document.createElement('button');
    skipBtn.type = 'button';
    skipBtn.className = 'm-btn m-btn--ghost';
    skipBtn.textContent = 'No calificar';
    skipBtn.addEventListener('click', function () { submitNpsSkip(); });
    card.appendChild(skipBtn);

    return card;
  }

  async function submitNps(score, comment) {
    var token = getSessionToken();
    try {
      await DinerSession.fetch(
        `/api/diner/order/${encodeURIComponent(CODE)}/nps`, 'POST',
        { score: score, comment: comment, skip: false }, token,
      );
      mesioToast('¡Gracias por tu calificación!', 'success');
      loadOrder();
    } catch (e) {
      mesioToast((e && e.message) || 'No pudimos enviar tu calificación.', 'error', 5000);
    }
  }

  async function submitNpsSkip() {
    var token = getSessionToken();
    try {
      await DinerSession.fetch(`/api/diner/order/${encodeURIComponent(CODE)}/nps`, 'POST', { skip: true }, token);
      loadOrder();
    } catch (e) {
      mesioToast((e && e.message) || 'No pudimos guardar tu respuesta.', 'error', 4000);
    }
  }

  function render(data) {
    var loading = document.getElementById('pedido-loading');
    var content = document.getElementById('pedido-content');
    if (loading) loading.hidden = true;
    if (!content) return;
    content.hidden = false;
    content.innerHTML = '';

    var header = el('div', 'pedido-header');
    var info = el('div', 'pedido-header-info');
    info.appendChild(textEl('p', 'pedido-org-name', data.org_name));
    info.appendChild(textEl('p', 'pedido-location-name', data.location_name));
    header.appendChild(info);
    header.appendChild(textEl('span', 'pedido-code-chip', data.public_code));
    content.appendChild(header);

    content.appendChild(buildStatusCard(data));
    content.appendChild(buildItemsCard(data));
    content.appendChild(buildActions(data));

    var npsCard = buildNpsCard(data);
    if (npsCard) content.appendChild(npsCard);
  }

  // ── Live updates ─────────────────────────────────────────────────────

  function stopLiveUpdates() {
    if (liveStopped) return;
    liveStopped = true;
    if (liveIntervalId) { clearInterval(liveIntervalId); liveIntervalId = null; }
    try { MesioRealtime.disconnect(); } catch (e) { /* not connected — no-op */ }
  }

  function maybeStopLiveUpdates(data) {
    var terminalNoFollowUp = data.status === 'rechazado' || data.status === 'cancelado';
    var deliveredAndDone = data.status === 'entregado' && (!data.nps || !data.nps.eligible || data.nps.already_submitted);
    if (terminalNoFollowUp || deliveredAndDone) stopLiveUpdates();
  }

  function startLiveUpdates() {
    var token = getSessionToken();
    if (token) {
      try {
        MesioRealtime.connect('/api/diner/stream', function () { return token; });
        MesioRealtime.on('delivery_order.updated', loadOrder);
        MesioRealtime.on('resync', loadOrder);
      } catch (e) {
        // SSE unavailable for whatever reason — mesioLiveInterval below just
        // never backs off to the 60s net and keeps polling at 10s, which is
        // still correct behavior, not a broken page.
      }
    }
    // mesioLiveInterval backs off to a 60s safety net once MesioRealtime
    // reports connected — a plain 10s poll otherwise (link-only device, or
    // SSE still reconnecting). See this file's header comment.
    liveIntervalId = mesioLiveInterval(loadOrder, 10000);
  }

  loadOrder().then(startLiveUpdates);
})();
