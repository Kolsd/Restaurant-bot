/* ── Menu Admin page ─────────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Tab switching
  function switchTab(tab) {
    document.querySelectorAll('[data-tab]').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    ['disp', 'inv', 'esc'].forEach(function (t) {
      const el = document.getElementById('tab-' + t);
      if (el) el.style.display = t === tab ? '' : 'none';
    });
  }

  document.querySelectorAll('[data-tab]').forEach(function (btn) {
    btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
  });

  // Category collapse
  document.querySelectorAll('.cat-head').forEach(function (head) {
    head.addEventListener('click', function () {
      const card = head.closest('.card');
      const grid = card ? card.querySelector('.dish-grid') : null;
      const arrow = head.querySelector('.cat-arrow');
      if (grid) {
        const hidden = grid.style.display === 'none';
        grid.style.display = hidden ? '' : 'none';
        if (arrow) arrow.classList.toggle('open', hidden);
      }
    });
  });

  // Toggle dish availability
  document.querySelectorAll('.toggle input').forEach(function (input) {
    input.addEventListener('change', function () {
      const dish = input.closest('.dish');
      if (dish) dish.classList.toggle('off', !input.checked);
      saveDishAvailability(input);
    });
  });

  async function saveDishAvailability(input) {
    const dish = input.closest('.dish');
    if (!dish) return;
    const name = dish.querySelector('.dish-name');
    if (!name) return;
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      // TODO: replace with actual dish ID when backend exposes it
      // await fetch('/api/menu/availability', { method: 'PATCH', headers, body: JSON.stringify({ dish: name.textContent, available: input.checked }) });
    } catch (e) {
      // Revert optimistic update on error
      input.checked = !input.checked;
      const dishEl = input.closest('.dish');
      if (dishEl) dishEl.classList.toggle('off', !input.checked);
    }
  }

  // Sync branches button
  const syncBtn = document.querySelector('.btn.primary');
  if (syncBtn && syncBtn.textContent.includes('Sincronizar')) {
    syncBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Sincronización de sucursales en proceso…', 'info');
      }
    });
  }

  // Add inventory item
  const addInvBtn = document.querySelector('#tab-inv .btn.primary');
  if (addInvBtn) {
    addInvBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Formulario de nuevo producto próximamente', 'info');
      }
    });
  }

  // Repone / Editar buttons in inventory
  document.querySelectorAll('.inv-row .btn.sm.ghost').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      const row = btn.closest('.inv-row');
      const name = row ? row.querySelector('[style*="font-weight"]') : null;
      if (typeof mesioToast === 'function') {
        mesioToast(btn.textContent.trim() + ': ' + (name ? name.textContent : ''), 'info', 2000);
      }
    });
  });

  async function loadMenu() {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/menu', { headers });
      if (res.ok) {
        // Menu data loaded — dish rendering would go here
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  async function loadInventory() {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/inventory', { headers });
      if (res.ok) {
        // Inventory data loaded
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  loadMenu();
  loadInventory();
})();
