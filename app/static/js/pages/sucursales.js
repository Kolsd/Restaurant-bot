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

  // Add branch modal
  function openAddBranchModal() {
    var modal = document.getElementById('addBranchModal');
    if (modal) { modal.classList.add('open'); }
  }

  function closeAddBranchModal() {
    var modal = document.getElementById('addBranchModal');
    if (modal) {
      modal.classList.remove('open');
      var nameEl = document.getElementById('branchName');
      var addrEl = document.getElementById('branchAddress');
      var waEl = document.getElementById('branchWhatsapp');
      if (nameEl) nameEl.value = '';
      if (addrEl) addrEl.value = '';
      if (waEl) waEl.value = '';
    }
  }

  async function submitAddBranch() {
    var nameEl = document.getElementById('branchName');
    var addrEl = document.getElementById('branchAddress');
    var waEl = document.getElementById('branchWhatsapp');
    if (!nameEl || !nameEl.value.trim()) { mesioToast('Nombre requerido', 'warn'); return; }
    if (!addrEl || !addrEl.value.trim()) { mesioToast('Dirección requerida', 'warn'); return; }

    var payload = {
      name: nameEl.value.trim(),
      address: addrEl.value.trim(),
      whatsapp_number: waEl ? waEl.value.trim() : ''
    };

    try {
      var res = await fetch('/api/team/branches', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        var err = await res.json().catch(function () { return {}; });
        throw new Error(err.detail || 'HTTP ' + res.status);
      }
      mesioToast('Sucursal creada correctamente', 'success');
      closeAddBranchModal();
      loadLocations();
    } catch (e) {
      mesioToast('Error: ' + e.message, 'error');
    }
  }

  var addBtn = document.getElementById('btn-add-branch');
  if (addBtn) { addBtn.addEventListener('click', openAddBranchModal); }

  var cancelBtn = document.getElementById('addBranchModalCancel');
  if (cancelBtn) { cancelBtn.addEventListener('click', closeAddBranchModal); }

  var closeBtn = document.getElementById('addBranchModalClose');
  if (closeBtn) { closeBtn.addEventListener('click', closeAddBranchModal); }

  var submitBtn = document.getElementById('addBranchModalSubmit');
  if (submitBtn) { submitBtn.addEventListener('click', submitAddBranch); }

  var overlay = document.getElementById('addBranchModal');
  if (overlay) {
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeAddBranchModal();
    });
  }

  // Export comparison table
  const exportBtn = document.getElementById('btn-export-cmp');
  if (exportBtn) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Disponible en la próxima versión', 'info');
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
    if (period) { console.info('sucursales: period filter is a v2 hook — backend does not support it yet', period); }
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

  loadLocations();

  // ── Consolidated KPIs ────────────────────────────────────────────

  async function loadConsolidated(days) {
    days = days || 7;
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/stats/branches-consolidated?days=' + days, { headers });
      if (!res.ok) return;
      const data = await res.json();

      const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };
      const setEl = function (id, val) {
        const el = document.getElementById(id);
        if (el) el.textContent = val != null ? val : '—';
      };

      setEl('consolidated-sales',   data.total_sales   != null ? fmt(data.total_sales) : '—');
      setEl('consolidated-tickets', data.total_tickets != null ? data.total_tickets    : '—');
      setEl('consolidated-nps',     data.avg_nps       != null ? data.avg_nps          : '—');
      setEl('consolidated-staff',   data.total_staff   != null ? data.total_staff      : '—');
      setEl('consolidated-growth',  data.growth_yoy    != null ? data.growth_yoy + '%' : '—');
      setEl('consolidated-label',   data.period        || ('Últimos ' + days + 'd'));
    } catch (e) {
      console.error('sucursales: consolidated error', e);
    }
  }

  // ── Comparison table ─────────────────────────────────────────────

  async function loadComparison(days) {
    days = days || 30;
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/stats/branches-comparison?days=' + days, { headers });
      if (!res.ok) return;
      const data = await res.json();

      const locations = data.locations || [];
      const rows = data.rows || [];
      if (!locations.length || !rows.length) return;

      const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };

      // Render header row with location names
      const thead = document.getElementById('cmp-head');
      if (thead) {
        let headerHtml = '<tr><th style="text-align:left;">Métrica</th>';
        locations.forEach(function (loc) {
          headerHtml += '<th>' + _escHtml(loc.name || ('Sede ' + loc.id)) + '</th>';
        });
        headerHtml += '<th>Promedio</th></tr>';
        thead.innerHTML = headerHtml;
      }

      // Render body rows
      const tbody = document.getElementById('cmp-body');
      if (!tbody) return;

      const isMoney = function (metric) {
        return metric.toLowerCase().includes('ventas') || metric.toLowerCase().includes('ticket') || metric.toLowerCase().includes('nómina');
      };
      const isPct = function (metric) {
        return metric.toLowerCase().includes('%') || metric.toLowerCase().includes('rate') || metric.toLowerCase().includes('tasa') || metric.toLowerCase().includes('crecimiento') || metric.toLowerCase().includes('rotación');
      };

      const formatVal = function (val, metric) {
        if (val == null) return '<span style="color:var(--text-4);">—</span>';
        if (isMoney(metric)) return fmt(Math.round(val));
        if (isPct(metric)) return val + '%';
        return String(Math.round(val * 10) / 10);
      };

      tbody.innerHTML = rows.map(function (row) {
        const isTop = row.per_location && row.top_location_id != null;
        let cells = '<td style="font-weight:500;">' + _escHtml(row.metric) + '</td>';

        (row.per_location || []).forEach(function (val, i) {
          const locId = locations[i] && locations[i].id;
          const highlight = isTop && locId === row.top_location_id && val != null;
          cells += '<td style="' + (highlight ? 'color:var(--brand);font-weight:600;' : '') + '">' +
            formatVal(val, row.metric) + '</td>';
        });

        cells += '<td style="color:var(--text-2);">' + formatVal(row.avg, row.metric) + '</td>';
        return '<tr>' + cells + '</tr>';
      }).join('');
    } catch (e) {
      console.error('sucursales: comparison error', e);
    }
  }

  // ── AI Benchmark ─────────────────────────────────────────────────

  async function loadBenchmark() {
    const aiEl = document.getElementById('ai-benchmark');
    if (!aiEl) return;
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };

      // Gather the consolidated numbers first for the AI prompt
      const consRes = await fetch('/api/stats/branches-consolidated?days=30', { headers });
      if (!consRes.ok) return;
      const consData = await consRes.json();

      const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };
      const salesTxt = consData.total_sales   != null ? fmt(consData.total_sales) : 'N/A';
      const npsTxt   = consData.avg_nps       != null ? String(consData.avg_nps)  : 'N/A';
      const growthTxt = consData.growth_yoy   != null ? consData.growth_yoy + '%' : 'N/A';

      const aiRes = await fetch('/api/ai/proxy', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
        body: JSON.stringify({
          prompt: 'Análisis ejecutivo conciso (2 frases) para el dueño de un restaurante multi-sede. ' +
            'Ventas totales 30 días: ' + salesTxt + '. NPS promedio: ' + npsTxt + '. ' +
            'Crecimiento YoY: ' + growthTxt + '. ' +
            consData.total_staff + ' empleados activos. Sé directo y accionable.',
          max_tokens: 130
        })
      });
      if (aiRes.ok) {
        const aiData = await aiRes.json();
        const text = (aiData.content || aiData.response || aiData.text || '').trim();
        if (text) aiEl.textContent = text; // textContent — untrusted LLM output
      }
    } catch (_) { /* AI benchmark is best-effort */ }
  }

  loadConsolidated(7);
  loadComparison(30);
  loadBenchmark();
})();
