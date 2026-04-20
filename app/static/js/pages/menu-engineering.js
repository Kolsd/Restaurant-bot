/* ── Menu Engineering page ───────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Period filter buttons
  document.querySelectorAll('.page-head .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.page-head .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      const period = btn.textContent.trim();
      loadEngineering(period);
    });
  });

  // Table filter buttons
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      const filter = btn.textContent.trim().toLowerCase();
      filterTableRows(filter);
    });
  });

  function filterTableRows(filter) {
    const rows = document.querySelectorAll('.me-tbl tbody tr');
    rows.forEach(function (row) {
      if (filter === 'todos') { row.style.display = ''; return; }
      const badge = row.querySelector('.quad-badge');
      const cls = badge ? badge.className : '';
      const visible = filter.includes('star') ? cls.includes('stars')
        : filter.includes('puzzle') ? cls.includes('puzzles')
        : filter.includes('plowhorse') ? cls.includes('plowhorses')
        : filter.includes('dog') ? cls.includes('dogs')
        : true;
      row.style.display = visible ? '' : 'none';
    });
  }

  // Bubble tooltips
  document.querySelectorAll('.bubble').forEach(function (bubble) {
    bubble.addEventListener('click', function () {
      const name = bubble.textContent.trim().replace(/\n/g, ' ');
      if (typeof mesioToast === 'function') {
        mesioToast(name, 'info', 1500);
      }
    });
  });

  // Table column sort
  document.querySelectorAll('.me-tbl th').forEach(function (th) {
    th.addEventListener('click', function () {
      document.querySelectorAll('.me-tbl th').forEach(function (t) { t.classList.remove('active'); });
      th.classList.add('active');
    });
  });

  // Export button
  const exportBtn = document.querySelector('.card.flush .btn.sm.ghost');
  if (exportBtn) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Exportación próximamente disponible', 'info');
      }
    });
  }

  async function loadEngineering(period) {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const days = period === '7 días' ? 7 : period === '90 días' ? 90 : 30;
      // TODO: endpoint GET /api/menu-analytics/engineering?days=N (check menu_analytics_repo)
      // const res = await fetch('/api/menu-analytics/engineering?days=' + days, { headers });
    } catch (e) {
      // Static placeholder remains
    }
  }
})();
