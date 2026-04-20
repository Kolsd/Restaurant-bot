/* ── Fidelización page ───────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Campaign status filter
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
    });
  });

  // New campaign button
  const newCampBtn = document.querySelector('.page-head .btn.primary');
  if (newCampBtn) {
    newCampBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Creador de campañas próximamente disponible', 'info');
      }
    });
  }

  // Configure program
  const configBtn = document.querySelector('.page-head .btn:not(.primary)');
  if (configBtn) {
    configBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Configuración del programa próximamente disponible', 'info');
      }
    });
  }

  // Segment cards
  document.querySelectorAll('.seg-card').forEach(function (card) {
    card.addEventListener('click', function () {
      const title = card.querySelector('[style*="font-weight: 600"]');
      if (title && typeof mesioToast === 'function') {
        mesioToast('Segmento: ' + title.textContent, 'info', 1500);
      }
    });
  });

  // Campaign toggle switches
  document.querySelectorAll('.camp-row .toggle input').forEach(function (input) {
    input.addEventListener('change', function () {
      const row = input.closest('.camp-row');
      const name = row ? row.querySelector('[style*="font-weight: 500"]') : null;
      if (typeof mesioToast === 'function') {
        mesioToast((input.checked ? 'Activada: ' : 'Pausada: ') + (name ? name.textContent : ''), 'info', 2000);
      }
    });
  });

  async function loadLoyaltyCustomers() {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/loyalty/stats', { headers });
      if (res.ok) {
        // Loyalty stats loaded
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  loadLoyaltyCustomers();
})();
