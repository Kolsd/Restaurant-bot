/* ── Pedidos page ────────────────────────────────────────────────── */
(function () {
  'use strict';

  // Auth guard
  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Tab switching (no inline onclick)
  function switchTab(tab) {
    document.querySelectorAll('[data-tab]').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    const rt = document.getElementById('tab-rt');
    const hist = document.getElementById('tab-hist');
    if (rt) rt.style.display = tab === 'rt' ? '' : 'none';
    if (hist) hist.style.display = tab === 'hist' ? '' : 'none';
  }

  // Bind tab buttons
  document.querySelectorAll('[data-tab]').forEach(function (btn) {
    btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
  });

  // Filter chips (channel filter for history)
  document.querySelectorAll('.filter-chip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      document.querySelectorAll('.filter-chip').forEach(function (c) { c.classList.remove('active'); });
      chip.classList.add('active');
      const filter = chip.textContent.trim().toLowerCase();
      filterHistoryRows(filter);
    });
  });

  function filterHistoryRows(filter) {
    const rows = document.querySelectorAll('.hist-row:not(.hist-head)');
    rows.forEach(function (row) {
      if (filter === 'todos' || filter === 'all') {
        row.style.display = '';
        return;
      }
      const pill = row.querySelector('.channel-pill');
      const text = pill ? pill.textContent.trim().toLowerCase() : '';
      row.style.display = text.includes(filter) ? '' : 'none';
    });
  }

  // Period filter (seg buttons in history)
  document.querySelectorAll('#tab-hist .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('#tab-hist .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
    });
  });

  // Reload button (live section)
  const reloadBtn = document.querySelector('#tab-rt .btn.sm.ghost');
  if (reloadBtn) {
    reloadBtn.addEventListener('click', function () {
      loadLiveOrders();
    });
  }

  // Export CSV button
  const exportBtn = document.querySelector('.page-head .btn:not(.primary)');
  if (exportBtn && exportBtn.textContent.includes('Exportar')) {
    exportBtn.addEventListener('click', exportCSV);
  }

  async function loadLiveOrders() {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/stats/live-orders', { headers });
      if (res.ok) {
        // Live orders data loaded — UI update would go here
        // For now, the static design serves as placeholder
      }
    } catch (e) {
      // Network error — static placeholder remains
    }
  }

  async function loadHistoryOrders(period) {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const params = new URLSearchParams({ period: period || '7d' });
      const res = await fetch('/api/orders?' + params.toString(), { headers });
      if (res.ok) {
        // History data loaded
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  function exportCSV() {
    // TODO: wire to actual export endpoint when available
    if (typeof mesioToast === 'function') {
      mesioToast('Exportación próximamente disponible', 'info');
    }
  }

  // Initial load
  loadLiveOrders();
})();
