/* ── Sucursales page ─────────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Period filter
  document.querySelectorAll('.page-head .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.page-head .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      loadLocations(btn.textContent.trim());
    });
  });

  // Add branch button
  const addBtn = document.querySelector('.page-head .btn.primary');
  if (addBtn) {
    addBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Alta de nueva sucursal próximamente disponible', 'info');
      }
    });
  }

  // Dashboard per-branch buttons
  document.querySelectorAll('.branch-card .btn.sm:not(.ghost)').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      const card = btn.closest('.branch-card');
      const name = card ? card.querySelector('.branch-name') : null;
      if (name && typeof mesioToast === 'function') {
        mesioToast('Dashboard de ' + name.textContent.replace(/\s*Principal.*$/, '').trim(), 'info', 1500);
      }
    });
  });

  // Settings (gear) per-branch buttons
  document.querySelectorAll('.branch-card .btn.sm.ghost').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      const card = btn.closest('.branch-card');
      const name = card ? card.querySelector('.branch-name') : null;
      if (name && typeof mesioToast === 'function') {
        mesioToast('Configuración de ' + name.textContent.replace(/\s*Principal.*$/, '').trim() + ' próximamente', 'info', 1500);
      }
    });
  });

  // Export comparison table
  const exportBtn = document.querySelector('.card.flush .btn.sm.ghost');
  if (exportBtn) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Exportación próximamente disponible', 'info');
      }
    });
  }

  async function loadLocations(period) {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      // Try org/locations endpoint first, fall back to branches
      let res = await fetch('/api/org/locations', { headers });
      if (!res.ok) {
        res = await fetch('/api/restaurants/branches', { headers });
      }
      if (res.ok) {
        // Locations data loaded — card rendering would replace static cards
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  loadLocations('7 días');
})();
