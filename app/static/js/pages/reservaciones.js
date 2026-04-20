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
    const parts = [];
    if (status === 'pending') {
      parts.push('<button class="btn sm primary" data-action="confirm" data-id="' + id + '">Confirmar</button>');
    }
    if (status !== 'cancelled' && status !== 'completed') {
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
      const res = await fetch('/api/reservations?date=' + isoDate(currentDate), { headers });
      if (!res.ok) {
        console.error('reservaciones: fetch failed', res.status);
        return;
      }
      const data = await res.json();
      const reservations = data.reservations || data;
      renderReservations(Array.isArray(reservations) ? reservations : []);
      updateStats(Array.isArray(reservations) ? reservations : []);
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

  function openNewReservationModal() {
    // TODO: POST /api/reservations does not exist yet — flagged as pending
    if (typeof mesioToast === 'function') {
      mesioToast('Creación de reservas via formulario — endpoint pendiente', 'info');
    }
  }

  updateDayLabel();
  loadReservations();
})();
