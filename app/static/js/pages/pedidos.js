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
    const rows = document.querySelectorAll('#hist-body .hist-row');
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
      const periodMap = { 'Hoy': '1d', '7 días': '7d', 'Mes': '30d' };
      const period = periodMap[btn.textContent.trim()] || '7d';
      loadHistoryOrders(period);
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

  // ── Live orders render ──────────────────────────────────────────

  function statusBadge(status) {
    const map = {
      'pendiente': '<span class="badge warn">Cocina</span>',
      'en_cocina': '<span class="badge warn">Cocina</span>',
      'en_ruta': '<span class="badge info">En ruta</span>',
      'listo': '<span class="badge success">Listo</span>',
      'entregado': '<span class="badge">Entregado</span>',
    };
    return map[status] || ('<span class="badge">' + _escHtml(status || '') + '</span>');
  }

  function renderLiveOrders(orders) {
    const grid = document.querySelector('.kds-grid');
    if (!grid) return;
    if (!orders || !orders.length) {
      grid.innerHTML = '<div style="padding:24px;color:var(--text-3);font-size:13px;">(sin pedidos activos)</div>';
      grid.setAttribute('data-loaded', 'true');
      return;
    }
    const cssClass = { 'en_cocina': 'cocina', 'pendiente': 'cocina', 'en_ruta': 'ruta', 'listo': 'listo' };
    grid.innerHTML = orders.map(function (o) {
      const cls = cssClass[o.status] || '';
      const items = Array.isArray(o.items) ? o.items : [];
      const itemsHtml = items.slice(0, 3).map(function (it) {
        return '<div class="kds-item"><span>' + _escHtml(it.name || '') + '</span><span class="mono">×' + (it.qty || 1) + '</span></div>';
      }).join('');
      const total = typeof mesioFmt === 'function' ? mesioFmt(o.total || 0) : '$' + (o.total || 0);
      const sub = o.address ? _escHtml(o.address) : (o.table_name ? _escHtml(o.table_name) : '');
      return '<div class="kds-card ' + cls + '">' +
        '<div class="kds-top"><div>' +
        '<div class="kds-id">#' + _escHtml(String(o.id || '')) + '</div>' +
        '<div class="kds-name">' + _escHtml(o.customer_name || o.phone || '') + '</div>' +
        '<div class="kds-sub">' + sub + '</div>' +
        '</div>' + statusBadge(o.status) + '</div>' +
        '<div class="kds-items">' + itemsHtml + '</div>' +
        '<div class="kds-foot"><span class="kds-time">⏱</span><span class="kds-price">' + total + '</span></div>' +
        '</div>';
    }).join('');
    grid.setAttribute('data-loaded', 'true');
  }

  function renderMetrics(data) {
    const metricsRow = document.querySelector('#tab-rt .metrics-row');
    if (!metricsRow || !data) return;
    const vals = metricsRow.querySelectorAll('.metric-value');
    if (vals[0]) vals[0].textContent = data.total_today || '0';
    if (vals[1]) vals[1].textContent = data.in_kitchen || '0';
    if (vals[2]) vals[2].textContent = data.in_delivery || '0';
    if (vals[3]) vals[3].textContent = data.delivered || '0';
  }

  async function loadLiveOrders() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/stats/live-orders', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const orders = data.orders || data;
      renderLiveOrders(Array.isArray(orders) ? orders : []);
      renderMetrics(data);
    } catch (e) {
      console.error('pedidos: live-orders error', e);
    }
  }

  // ── History orders render ───────────────────────────────────────

  function channelPill(channel) {
    const map = { 'whatsapp': 'wa', 'pos': 'pos', 'delivery': 'dom', 'domicilio': 'dom', 'qr': 'qr' };
    const cls = map[(channel || '').toLowerCase()] || '';
    return '<span class="channel-pill ' + cls + '">' + _escHtml(channel || '-') + '</span>';
  }

  function histStatusBadge(status) {
    if (!status) return '';
    const map = {
      'pagado': 'success', 'entregado': 'success',
      'pendiente': 'warn', 'en_ruta': 'info', 'cancelado': 'danger'
    };
    const cls = map[status.toLowerCase()] || '';
    return '<span class="badge ' + cls + '">' + _escHtml(status) + '</span>';
  }

  function renderHistoryOrders(orders) {
    let body = document.getElementById('hist-body');
    if (!body) {
      // Create the body container inside the table after the head row
      const head = document.querySelector('.hist-row.hist-head');
      if (!head) return;
      body = document.createElement('div');
      body.id = 'hist-body';
      head.parentNode.insertBefore(body, head.nextSibling);
      // Remove old static rows
      const staticRows = document.querySelectorAll('.card.flush .hist-row:not(.hist-head)');
      staticRows.forEach(function (r) { if (r !== body) r.remove(); });
    }
    if (!orders || !orders.length) {
      body.innerHTML = '<div style="padding:18px;color:var(--text-3);font-size:13px;">(sin pedidos en este período)</div>';
      body.setAttribute('data-loaded', 'true');
      return;
    }
    body.innerHTML = orders.map(function (o) {
      const total = typeof mesioFmt === 'function' ? mesioFmt(o.total || 0) : '$' + (o.total || 0);
      const date = typeof mesioDate === 'function' ? mesioDate(o.created_at || '') : (o.created_at || '');
      const channel = o.channel || o.source || 'WhatsApp';
      const customer = o.customer_name || o.phone || o.table_name || '-';
      const items = o.items_summary || (Array.isArray(o.items) ? o.items.map(function(i){return i.name;}).join(', ') : '');
      return '<div class="hist-row">' +
        '<div class="mono" style="color:var(--text-3);">' + _escHtml(String(o.id || '')) + '</div>' +
        '<div>' + channelPill(channel) + '</div>' +
        '<div><div style="font-weight:500;">' + _escHtml(customer) + '</div></div>' +
        '<div style="font-size:12px;color:var(--text-2);">' + _escHtml(items.slice(0, 60)) + '</div>' +
        '<div style="font-size:12px;color:var(--text-3);">' + _escHtml(date) + '</div>' +
        '<div class="mono" style="text-align:right;font-weight:600;">' + total + '</div>' +
        '<div>' + histStatusBadge(o.status) + '</div>' +
        '</div>';
    }).join('');
    body.setAttribute('data-loaded', 'true');
  }

  async function loadHistoryOrders(period) {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const params = new URLSearchParams({ period: period || '7d' });
      const res = await fetch('/api/orders?' + params.toString(), { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const orders = data.orders || data;
      renderHistoryOrders(Array.isArray(orders) ? orders : []);
    } catch (e) {
      console.error('pedidos: history error', e);
    }
  }

  function exportCSV() {
    if (typeof mesioToast === 'function') {
      mesioToast('Exportación próximamente disponible', 'info');
    }
  }

  // Initial load
  loadLiveOrders();
  loadHistoryOrders('7d');

  // Auto-refresh live section every 30s
  if (typeof mesioInterval === 'function') {
    mesioInterval(loadLiveOrders, 30000);
  }
})();
