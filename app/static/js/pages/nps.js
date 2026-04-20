/* ── NPS & Reseñas page ──────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Period filter
  document.querySelectorAll('.page-head .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.page-head .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      loadNPS(btn.textContent.trim());
    });
  });

  // Feed filter
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      filterFeed(btn.textContent.trim());
    });
  });

  function filterFeed(filter) {
    const reviews = document.querySelectorAll('.review');
    reviews.forEach(function (rev) {
      if (filter === 'Todas') { rev.style.display = ''; return; }
      const badge = rev.querySelector('.badge');
      const text = badge ? badge.textContent.trim().toLowerCase() : '';
      const stars = rev.querySelector('.stars') ? rev.querySelector('.stars').textContent.trim() : '';
      const visible = filter === 'Pendientes' ? text.includes('pendiente')
        : filter === 'Urgentes' ? text.includes('urgente')
        : filter === '5★' ? stars.split('★').length - 1 === 5 && !rev.querySelector('.off')
        : filter === '1-3★' ? (stars.split('★').length - 1 <= 3)
        : true;
      rev.style.display = visible ? '' : 'none';
    });
  }

  // Reply buttons
  document.querySelectorAll('.review .btn.sm.primary').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (typeof mesioToast === 'function') {
        mesioToast('Generando respuesta con IA…', 'info', 2000);
      }
    });
  });

  // Manual reply buttons
  document.querySelectorAll('.review .btn.sm.ghost').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (typeof mesioToast === 'function') {
        mesioToast(btn.textContent.trim() + ' — próximamente disponible', 'info', 2000);
      }
    });
  });

  // Send survey button
  const surveyBtn = document.querySelector('.page-head .btn:not(.primary)');
  if (surveyBtn && surveyBtn.textContent.includes('encuesta')) {
    surveyBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Envío de encuesta próximamente disponible', 'info');
      }
    });
  }

  async function loadNPS(period) {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/nps/stats', { headers });
      if (res.ok) {
        // NPS data loaded
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  async function loadReviews() {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/reviews', { headers });
      if (res.ok) {
        // Reviews data loaded
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  loadNPS('30d');
  loadReviews();
})();
