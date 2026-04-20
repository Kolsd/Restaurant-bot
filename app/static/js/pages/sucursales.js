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
      loadLocations(btn.dataset.period || btn.textContent.trim());
    });
  });

  // Add branch button
  const addBtn = document.getElementById('btn-add-branch');
  if (addBtn) {
    addBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Alta de nueva sucursal próximamente disponible', 'info');
      }
    });
  }

  // Export comparison table
  const exportBtn = document.getElementById('btn-export-cmp');
  if (exportBtn) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Exportación próximamente disponible', 'info');
      }
    });
  }

  // Search input
  const searchInput = document.getElementById('search-input');
  if (searchInput) {
    searchInput.addEventListener('input', function () {
      const q = searchInput.value.toLowerCase().trim();
      const container = document.getElementById('branches-grid');
      if (!container) return;
      container.querySelectorAll('.branch-card').forEach(function (card) {
        const text = card.textContent.toLowerCase();
        card.style.display = (!q || text.includes(q)) ? '' : 'none';
      });
    });
  }

  // ── Render ────────────────────────────────────────────────────────

  function renderBranches(branches) {
    let container = document.getElementById('branches-grid');
    if (!container) {
      // Find the branch cards wrapper (the grid div)
      const existingCards = document.querySelectorAll('.branch-card');
      if (existingCards.length) {
        const parent = existingCards[0].parentNode;
        container = document.createElement('div');
        container.id = 'branches-grid';
        container.style.cssText = 'display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px;';
        parent.parentNode.insertBefore(container, parent);
        parent.remove(); // Remove old static grid
      } else {
        return;
      }
    }

    if (!branches || !branches.length) {
      container.innerHTML = '<div style="padding:24px;color:var(--text-3);">(sin sucursales)</div>';
      container.setAttribute('data-loaded', 'true');
      return;
    }

    const colors = [
      'linear-gradient(135deg,#1D9E75,#0F6E56)',
      'linear-gradient(135deg,#F59E0B,#B45309)',
      'linear-gradient(135deg,#3B82F6,#1D4ED8)',
      'linear-gradient(135deg,#EC4899,#BE185D)',
      'linear-gradient(135deg,#8B5CF6,#7C3AED)',
    ];

    container.innerHTML = branches.map(function (b, idx) {
      const name = b.name || b.branch_name || b.location_name || ('Sucursal ' + (b.id || idx + 1));
      const addr = b.address || b.location || '';
      const tables = b.table_count || b.tables || '';
      const nps = b.nps_score || b.nps || '';
      const color = colors[idx % colors.length];
      const initials = name.split(/\s+/).map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase();

      return '<div class="branch-card" data-branch-id="' + (b.id || '') + '">' +
        '<div class="branch-head">' +
        '<div class="branch-logo" style="background:' + color + ';">' + _escHtml(initials) + '</div>' +
        '<div style="flex:1;">' +
        '<div class="branch-name">' + _escHtml(name) + '</div>' +
        '<div class="branch-addr">' + _escHtml(addr) + (tables ? ' · ' + tables + ' mesas' : '') + '</div>' +
        '</div>' +
        '</div>' +
        '<div class="branch-kpis">' +
        '<div><div class="branch-kpi-lbl">NPS</div><div class="branch-kpi-val">' + (nps || '—') + '</div></div>' +
        '<div><div class="branch-kpi-lbl">Mesas</div><div class="branch-kpi-val">' + (tables || '—') + '</div></div>' +
        '</div>' +
        '<div style="margin-top:14px;display:flex;gap:6px;">' +
        '<button class="btn sm" style="flex:1;" data-branch-action="dashboard" data-branch-id="' + (b.id || '') + '">Dashboard</button>' +
        '<button class="btn sm ghost" data-branch-action="settings" data-branch-id="' + (b.id || '') + '" aria-label="Configuración">⚙</button>' +
        '</div>' +
        '</div>';
    }).join('');

    container.setAttribute('data-loaded', 'true');

    // Bind branch action buttons
    container.querySelectorAll('[data-branch-action]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        const action = btn.dataset.branchAction;
        const card = btn.closest('.branch-card');
        const nameEl = card ? card.querySelector('.branch-name') : null;
        const name = nameEl ? nameEl.textContent.trim() : 'sucursal';
        if (action === 'dashboard') {
          if (typeof mesioToast === 'function') mesioToast('Dashboard de ' + name, 'info', 1500);
        } else if (action === 'settings') {
          if (typeof mesioToast === 'function') mesioToast('Configuración de ' + name + ' — próximamente', 'info', 1500);
        }
      });
    });
  }

  async function loadLocations(period) {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/team/branches', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const branches = data.branches || data;
      renderBranches(Array.isArray(branches) ? branches : []);
    } catch (e) {
      console.error('sucursales: error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al cargar sucursales', 'warning');
    }
  }

  loadLocations('7 días');
})();
