/* ── Reservaciones page ──────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  let currentDate = new Date();
  let _tables = []; // cached from floor-plan

  function isoDate(d) {
    return d.toISOString().split('T')[0];
  }

  function formatDate(d) {
    const days = ['Domingo', 'Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado'];
    const months = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];
    return days[d.getDay()] + ' ' + d.getDate() + ' ' + months[d.getMonth()];
  }

  function updateDayLabel() {
    const lbl = document.querySelector('.day-label');
    if (lbl) lbl.textContent = formatDate(currentDate);
  }

  // Day nav buttons
  const prevBtn = document.querySelector('.day-nav button:first-child');
  const nextBtn = document.querySelector('.day-nav button:last-child');
  if (prevBtn) {
    prevBtn.addEventListener('click', function () {
      currentDate.setDate(currentDate.getDate() - 1);
      updateDayLabel();
      loadReservations();
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener('click', function () {
      currentDate.setDate(currentDate.getDate() + 1);
      updateDayLabel();
      loadReservations();
    });
  }

  // Today button
  const todayBtn = document.querySelector('.page-head .btn:not(.primary)');
  if (todayBtn && todayBtn.textContent.includes('Hoy')) {
    todayBtn.addEventListener('click', function () {
      currentDate = new Date();
      updateDayLabel();
      loadReservations();
    });
  }

  // Nueva reserva button
  const newResBtn = document.querySelector('.btn.primary');
  if (newResBtn) {
    newResBtn.addEventListener('click', openNewReservationModal);
  }

  // Seg buttons (Lista / Timeline / Por mesa)
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
    });
  });

  // ── Render ────────────────────────────────────────────────────────

  function statusBadgeHtml(status) {
    const map = {
      confirmed: '<span class="badge success">Confirmada</span>',
      pending: '<span class="badge warn">Pendiente</span>',
      cancelled: '<span class="badge danger">Cancelada</span>',
      no_show: '<span class="badge danger">No-show</span>',
      completed: '<span class="badge">Completada</span>',
    };
    return map[status] || ('<span class="badge">' + _escHtml(status || '') + '</span>');
  }

  function actionsForStatus(res) {
    const id = res.id;
    const status = res.status || 'pending';
    const depositAmount = Number(res.deposit_amount || 0);
    const depositPaid   = !!res.deposit_paid;
    const parts = [];
    // Deposit awaiting manual confirmation (Wompi skipped / cliente paid via
    // transferencia or screenshot). Priority button — shown above Confirmar
    // because without deposit the reservation stays pending.
    if (depositAmount > 0 && !depositPaid && status !== 'cancelled' && status !== 'completed') {
      parts.push('<button class="btn sm primary" data-action="confirm-deposit" data-id="' + id + '">Validar depósito</button>');
    }
    if (status === 'pending' && !(depositAmount > 0 && !depositPaid)) {
      parts.push('<button class="btn sm primary" data-action="confirm" data-id="' + id + '">Confirmar</button>');
    }
    // 'Cliente llegó' button: confirmed reservation that has not yet been
    // seated. POSTs /api/reservations/{id}/seat which opens a table_session
    // automatically (DISCONNECT #9). The customer's bot WhatsApp picks up
    // immediately on the right mesa without needing to scan a QR.
    if (status === 'confirmed') {
      parts.push('<button class="btn sm primary" data-action="seat" data-id="' + id + '">🪑 Cliente llegó</button>');
    }
    if (status !== 'cancelled' && status !== 'completed' && status !== 'seated') {
      parts.push('<button class="btn sm ghost" data-action="cancel" data-id="' + id + '">Cancelar</button>');
      parts.push('<button class="btn sm ghost" data-action="assign-table" data-id="' + id + '">Asignar mesa</button>');
    }
    return parts.join(' ');
  }

  function groupByTime(reservations) {
    const groups = {};
    reservations.forEach(function (r) {
      const time = (r.time || r.reservation_time || '').substring(0, 5);
      if (!groups[time]) groups[time] = [];
      groups[time].push(r);
    });
    return groups;
  }

  function renderReservations(reservations) {
    const timeline = document.querySelector('.timeline');
    if (!timeline) return;

    if (!reservations || !reservations.length) {
      timeline.innerHTML = '<div style="padding:24px;color:var(--text-3);font-size:13px;">(sin reservas para este día)</div>';
      timeline.setAttribute('data-loaded', 'true');
      return;
    }

    const groups = groupByTime(reservations);
    const times = Object.keys(groups).sort();

    timeline.innerHTML = times.map(function (time) {
      const cards = groups[time].map(function (r) {
        const name = _escHtml(r.customer_name || r.guest_name || 'Sin nombre');
        const pax = r.guests || r.party_size || 1;
        const phone = r.phone || r.customer_phone || '';
        const table = r.table_name || (r.table_id ? 'Mesa ' + r.table_id : 'sin mesa');
        const cls = r.status === 'confirmed' ? 'confirmed' : r.status === 'cancelled' ? 'cancelled' : 'pending';
        return '<div class="res-card ' + cls + '" data-res-id="' + r.id + '">' +
          '<div class="res-name">' + name + ' · ' + pax + ' pax</div>' +
          '<div class="res-sub">' + _escHtml(phone) + ' · ' + _escHtml(table) + '</div>' +
          '<div class="res-meta">' + statusBadgeHtml(r.status) + '</div>' +
          '<div class="res-actions" style="margin-top:8px;display:flex;gap:6px;">' + actionsForStatus(r) + '</div>' +
          '</div>';
      }).join('');
      return '<div class="tl-time">' + _escHtml(time) + '</div>' +
        '<div class="tl-slot">' + cards + '</div>';
    }).join('');

    timeline.setAttribute('data-loaded', 'true');

    // Bind action buttons
    timeline.querySelectorAll('[data-action]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        const action = btn.dataset.action;
        const id = parseInt(btn.dataset.id, 10);
        if (action === 'confirm') confirmReservation(id);
        else if (action === 'cancel') cancelReservation(id);
        else if (action === 'assign-table') assignTable(id);
        else if (action === 'seat') seatReservation(id);
        else if (action === 'confirm-deposit') confirmDepositManual(id);
      });
    });
  }

  function updateStats(reservations) {
    const strip = document.querySelector('.stat-strip');
    if (!strip || !reservations) return;
    const vals = strip.querySelectorAll('.val');
    if (!vals.length) return;
    const total = reservations.length;
    const confirmed = reservations.filter(function (r) { return r.status === 'confirmed'; }).length;
    const pending = reservations.filter(function (r) { return r.status === 'pending'; }).length;
    const cancelled = reservations.filter(function (r) { return r.status === 'cancelled'; }).length;
    const pax = reservations.reduce(function (s, r) { return s + (r.guests || r.party_size || 1); }, 0);
    if (vals[0]) vals[0].textContent = total;
    if (vals[0] && vals[0].nextElementSibling) vals[0].nextElementSibling.textContent = pax + ' personas';
    if (vals[1]) vals[1].textContent = confirmed;
    if (vals[2]) vals[2].textContent = pending;
    if (vals[3]) vals[3].textContent = cancelled;
  }

  // ── API calls ──────────────────────────────────────────────────

  async function loadReservations() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const d = isoDate(currentDate);
      const res = await fetch('/api/reservations?date_from=' + d + '&date_to=' + d, { headers });
      if (!res.ok) {
        console.error('reservaciones: fetch failed', res.status);
        return;
      }
      const data = await res.json();
      const reservations = data.reservations || data;
      // Cache for action handlers (seatReservation needs reservation.table_id)
      window._lastReservations = Array.isArray(reservations) ? reservations : [];
      renderReservations(window._lastReservations);
      updateStats(window._lastReservations);
    } catch (e) {
      console.error('reservaciones: error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al cargar reservas', 'warning');
    }
  }

  async function confirmReservation(id) {
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/reservations/' + id + '/status', {
        method: 'PUT', headers,
        body: JSON.stringify({ status: 'confirmed' })
      });
      if (!res.ok) throw new Error('status ' + res.status);
      if (typeof mesioToast === 'function') mesioToast('Reserva confirmada', 'success');
      loadReservations();
    } catch (e) {
      console.error('reservaciones: confirm error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al confirmar reserva', 'error');
    }
  }

  async function confirmDepositManual(id) {
    // Caja confirms the deposit was received via non-Wompi method (transferencia,
    // nequi screenshot). Fires the same DB path the Wompi webhook uses — atomic
    // deposit + reservation transition to confirmed.
    const ok = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Confirmar que recibiste el depósito? Esto marca la reserva como confirmada.',
          { confirmText: 'Confirmar depósito' })
      : window.confirm('¿Confirmar que recibiste el depósito?');
    if (!ok) return;
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/reservations/' + id + '/confirm-deposit-manual', {
        method: 'POST', headers, body: JSON.stringify({})
      });
      const json = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(json.detail || 'status ' + res.status);
      const msg = json.already_confirmed ? 'Depósito ya estaba confirmado' : 'Depósito validado, reserva confirmada';
      if (typeof mesioToast === 'function') mesioToast(msg, 'success');
      loadReservations();
    } catch (e) {
      console.error('reservaciones: confirm-deposit error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al validar depósito: ' + e.message, 'error');
    }
  }

  async function cancelReservation(id) {
    const ok = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Cancelar esta reserva?', { confirmText: 'Cancelar reserva', danger: true })
      : window.confirm('¿Cancelar esta reserva?');
    if (!ok) return;
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/reservations/' + id + '/status', {
        method: 'PUT', headers,
        body: JSON.stringify({ status: 'cancelled', reason: 'manual' })
      });
      if (!res.ok) throw new Error('status ' + res.status);
      if (typeof mesioToast === 'function') mesioToast('Reserva cancelada', 'success');
      loadReservations();
    } catch (e) {
      console.error('reservaciones: cancel error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al cancelar reserva', 'error');
    }
  }

  async function assignTable(id) {
    // Load tables if not cached
    if (!_tables.length) {
      try {
        const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
        const res = await fetch('/api/tables/floor-plan', { headers });
        if (res.ok) {
          const data = await res.json();
          _tables = (data.tables || data || []).filter(function (t) { return t.id; });
        }
      } catch (e) {
        console.error('reservaciones: floor-plan error', e);
      }
    }

    if (!_tables.length) {
      if (typeof mesioToast === 'function') mesioToast('No hay mesas disponibles', 'warning');
      return;
    }

    // Build simple prompt with table list
    const options = _tables.map(function (t) {
      return (t.table_number || t.name || 'Mesa ' + t.id) + ' (cap. ' + (t.capacity || '?') + ')';
    }).join('\n');
    const choice = window.prompt('Seleccionar mesa:\n' + options + '\n\nIngresa el ID de la mesa:');
    if (!choice) return;
    const tableId = parseInt(choice, 10);
    if (!tableId) {
      if (typeof mesioToast === 'function') mesioToast('ID de mesa inválido', 'warning');
      return;
    }
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/reservations/' + id + '/assign-table', {
        method: 'PUT', headers,
        body: JSON.stringify({ table_id: tableId })
      });
      if (!res.ok) throw new Error('status ' + res.status);
      if (typeof mesioToast === 'function') mesioToast('Mesa asignada', 'success');
      loadReservations();
    } catch (e) {
      console.error('reservaciones: assign-table error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al asignar mesa', 'error');
    }
  }

  // ── 'Cliente llegó' → POST /seat → opens a table_session (DISCONNECT #9) ──
  async function seatReservation(id) {
    const reservation = (window._lastReservations || []).find(function (r) { return r.id === id; });

    // Resolve table_id: prefer the table_id already set on the reservation (assigned
    // when the host taps 'Asignar mesa' before arrival). If none, prompt the host
    // to pick one now from the floor plan list.
    let tableId = (reservation && reservation.table_id) ? String(reservation.table_id) : '';
    if (!tableId) {
      if (!_tables.length) {
        try {
          const h = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
          const r = await fetch('/api/tables/floor-plan', { headers: h });
          if (r.ok) {
            const d = await r.json();
            _tables = (d.tables || d || []).filter(function (t) { return t.id; });
          }
        } catch (e) { console.error('reservaciones: floor-plan error', e); }
      }
      if (!_tables.length) {
        if (typeof mesioToast === 'function') mesioToast('No hay mesas — primero asigna una', 'warning');
        return;
      }
      const opts = _tables.map(function (t) {
        return (t.table_number || t.name || 'Mesa') + ' [' + t.id + ']' + ' (cap. ' + (t.capacity || '?') + ')';
      }).join('\n');
      const choice = window.prompt('Seleccionar mesa para sentar al cliente:\n' + opts + '\n\nIngresa el ID de la mesa:');
      if (!choice) return;
      tableId = String(choice).trim();
    }

    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/reservations/' + id + '/seat', {
        method: 'POST', headers,
        body: JSON.stringify({ table_id: tableId })
      });
      if (!res.ok) {
        let detail = '';
        try { const j = await res.json(); detail = j.detail || ''; } catch (_) { /* ignore */ }
        throw new Error(detail || ('status ' + res.status));
      }
      const data = await res.json();
      if (data.already_seated) {
        if (typeof mesioToast === 'function') mesioToast('Ya estaba sentada', 'info', 1800);
      } else {
        if (typeof mesioToast === 'function') mesioToast('Cliente sentado · sesión abierta', 'success', 2200);
      }
      loadReservations();
    } catch (e) {
      console.error('reservaciones: seat error', e);
      const msg = (e && e.message) ? e.message : 'Error al sentar al cliente';
      if (typeof mesioToast === 'function') mesioToast(msg, 'error');
    }
  }

  // ── Nueva reserva modal ───────────────────────────────────────────

  function _buildModalHtml() {
    var tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    var minDate = isoDate(tomorrow);
    return '<div id="newResModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="newResTitle">' +
      '<div class="modal-card" style="max-width:420px;">' +
        '<div class="modal-header"><h3 id="newResTitle">Nueva reserva</h3>' +
          '<button id="newResClose" class="btn icon ghost" aria-label="Cerrar">&times;</button></div>' +
        '<div class="modal-body" style="display:flex;flex-direction:column;gap:12px;">' +
          '<label>Nombre del cliente *<input id="nrName" class="inp" type="text" autocomplete="off"></label>' +
          '<label>Teléfono<input id="nrPhone" class="inp" type="tel" autocomplete="off"></label>' +
          '<label>Personas (1-20) *<input id="nrPax" class="inp" type="number" min="1" max="20" value="2"></label>' +
          '<label>Fecha *<input id="nrDate" class="inp" type="date" min="' + minDate + '"></label>' +
          '<label>Hora *<input id="nrTime" class="inp" type="time" value="19:00"></label>' +
          '<label>Notas<textarea id="nrNotes" class="inp" rows="2"></textarea></label>' +
        '</div>' +
        '<div class="modal-footer" style="display:flex;gap:8px;justify-content:flex-end;">' +
          '<button id="newResCancel" class="btn ghost">Cancelar</button>' +
          '<button id="newResSubmit" class="btn primary">Crear reserva</button>' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  function openNewReservationModal() {
    var existing = document.getElementById('newResModal');
    if (existing) existing.remove();

    document.body.insertAdjacentHTML('beforeend', _buildModalHtml());

    var modal = document.getElementById('newResModal');
    var closeModal = function () { modal.remove(); };

    document.getElementById('newResClose').addEventListener('click', closeModal);
    document.getElementById('newResCancel').addEventListener('click', closeModal);

    modal.addEventListener('click', function (e) {
      if (e.target === modal) closeModal();
    });

    document.getElementById('newResSubmit').addEventListener('click', submitNewReservation);
  }

  async function submitNewReservation() {
    var nameEl    = document.getElementById('nrName');
    var phoneEl   = document.getElementById('nrPhone');
    var paxEl     = document.getElementById('nrPax');
    var dateEl    = document.getElementById('nrDate');
    var timeEl    = document.getElementById('nrTime');
    var notesEl   = document.getElementById('nrNotes');
    var submitBtn = document.getElementById('newResSubmit');

    var name = nameEl ? nameEl.value.trim() : '';
    var phone = phoneEl ? phoneEl.value.trim() : '';
    var pax = paxEl ? parseInt(paxEl.value, 10) : 0;
    var date = dateEl ? dateEl.value : '';
    var time = timeEl ? timeEl.value : '';
    var notes = notesEl ? notesEl.value.trim() : '';

    if (!name) {
      if (typeof mesioToast === 'function') mesioToast('El nombre es obligatorio', 'warning');
      if (nameEl) nameEl.focus();
      return;
    }
    if (!pax || pax < 1 || pax > 20) {
      if (typeof mesioToast === 'function') mesioToast('Número de personas debe ser entre 1 y 20', 'warning');
      if (paxEl) paxEl.focus();
      return;
    }
    if (!date) {
      if (typeof mesioToast === 'function') mesioToast('La fecha es obligatoria', 'warning');
      if (dateEl) dateEl.focus();
      return;
    }
    if (!time) {
      if (typeof mesioToast === 'function') mesioToast('La hora es obligatoria', 'warning');
      if (timeEl) timeEl.focus();
      return;
    }

    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Creando…'; }

    try {
      var headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      var res = await fetch('/api/reservations', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
          customer_name: name,
          customer_phone: phone || null,
          party_size: pax,
          date: date,
          time: time,
          notes: notes || null,
          source: 'manual'
        })
      });

      if (res.status === 400 || res.status === 422) {
        var err = await res.json().catch(function () { return {}; });
        if (typeof mesioToast === 'function') mesioToast(err.detail || 'Datos inválidos', 'warning');
        return;
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);

      if (typeof mesioToast === 'function') mesioToast('Reserva creada', 'success');
      var modal = document.getElementById('newResModal');
      if (modal) modal.remove();
      loadReservations();
    } catch (e) {
      console.error('reservaciones: create error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al crear la reserva', 'error');
    } finally {
      if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Crear reserva'; }
    }
  }

  updateDayLabel();
  loadReservations();
})();
