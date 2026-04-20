/* ── Reservaciones page ──────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  let currentDate = new Date();

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
    newResBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Formulario de nueva reserva próximamente', 'info');
      }
    });
  }

  // Reservation card clicks — show actions
  document.querySelectorAll('.res-card').forEach(function (card) {
    card.addEventListener('click', function () {
      const name = card.querySelector('.res-name');
      if (name && typeof mesioToast === 'function') {
        mesioToast('Detalle de reserva: ' + name.textContent, 'info', 2000);
      }
    });
  });

  async function loadReservations() {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const iso = currentDate.toISOString().split('T')[0];
      const res = await fetch('/api/reservations?date=' + iso, { headers });
      if (res.ok) {
        // Reservations data loaded — timeline rendering would go here
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  // Seg buttons (Lista / Timeline / Por mesa)
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
    });
  });

  updateDayLabel();
  loadReservations();
})();
