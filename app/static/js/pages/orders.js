/* ── Orders page ────────────────────────────────────────────────── */
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
      _histChannel = chip.textContent.trim().toLowerCase();
      renderHistoryOrders(currentHistoryRows());
    });
  });

  let _allOrders = [];
  let _histChannel = 'todos';     // todos | pos | domicilio | qr
  let _histDays = 7;

  // Rows on screen = the loaded period, narrowed by the channel chip.
  function currentHistoryRows() {
    if (_histChannel === 'todos') return _allOrders;
    return _allOrders.filter(function (o) { return o.channel === _histChannel; });
  }

  // Period filter (seg buttons in history)
  document.querySelectorAll('#tab-hist .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('#tab-hist .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      const daysMap = { 'Hoy': 1, '7 días': 7, 'Mes': 30 };
      _histDays = daysMap[btn.textContent.trim()] || 7;
      loadHistoryOrders();
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
  const exportBtn = document.getElementById('btn-export-csv');
  if (exportBtn) {
    exportBtn.addEventListener('click', exportCSV);
  }

  // ── Live orders render ──────────────────────────────────────────

  // The web delivery/pickup statuses (app/repositories/delivery_repo.py).
  function statusBadge(status) {
    const map = {
      'pendiente_aceptacion': '<span class="badge warn">Por aceptar</span>',
      'en_preparacion': '<span class="badge warn">En cocina</span>',
      'listo': '<span class="badge success">Listo</span>',
      'en_camino': '<span class="badge info">En camino</span>',
      'en_puerta': '<span class="badge info">En la puerta</span>',
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
    const cssClass = {
      'pendiente_aceptacion': 'cocina', 'en_preparacion': 'cocina',
      'en_camino': 'ruta', 'en_puerta': 'ruta', 'listo': 'listo',
    };
    grid.innerHTML = orders.map(function (o) {
      const cls = cssClass[o.status] || '';
      const itemsHtml = o.items_summary
        ? '<div class="kds-item"><span>' + _escHtml(o.items_summary) + '</span></div>'
        : '';
      const total = typeof mesioFmt === 'function' ? mesioFmt(o.total || 0) : '$' + (o.total || 0);
      const sub = o.source === 'pickup' ? 'Recoger en tienda' : _escHtml(o.address_short || 'Domicilio');
      const age = Number(o.age_min || 0);
      const ageTxt = age < 60 ? (age + ' min') : (Math.floor(age / 60) + ' h ' + (age % 60) + ' min');
      return '<div class="kds-card ' + cls + '">' +
        '<div class="kds-top"><div>' +
        '<div class="kds-id">#' + _escHtml(String(o.id || '')) + '</div>' +
        '<div class="kds-name">' + _escHtml(o.customer || '') + '</div>' +
        '<div class="kds-sub">' + sub + '</div>' +
        '</div>' + statusBadge(o.status) + '</div>' +
        '<div class="kds-items">' + itemsHtml + '</div>' +
        '<div class="kds-foot"><span class="kds-time">⏱ ' + ageTxt + '</span><span class="kds-price">' + total + '</span></div>' +
        '</div>';
    }).join('');
    grid.setAttribute('data-loaded', 'true');
  }

  function renderMetrics(data) {
    const metricsRow = document.querySelector('#tab-rt .metrics-row');
    if (!metricsRow || !data) return;
    const vals = metricsRow.querySelectorAll('.metric-value');
    const t = data.delivery_today || {};
    if (vals[0]) vals[0].textContent = t.total || '0';
    if (vals[1]) vals[1].textContent = t.in_kitchen || '0';
    if (vals[2]) vals[2].textContent = t.in_delivery || '0';
    if (vals[3]) vals[3].textContent = t.delivered || '0';
  }

  async function loadLiveOrders() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/stats/live-orders', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const orders = Array.isArray(data.orders) ? data.orders : [];
      // This monitor is domicilios only — table rounds have the Salón monitor below.
      renderLiveOrders(orders.filter(function (o) { return o.source === 'delivery' || o.source === 'pickup'; }));
      renderMetrics(data);
    } catch (e) {
      console.error('pedidos: live-orders error', e);
    }
  }

  // ── History orders render ───────────────────────────────────────

  const CHANNEL_LABELS = { pos: 'POS', domicilio: 'Domicilio', qr: 'QR' };
  function channelPill(channel) {
    const cls = { pos: 'pos', domicilio: 'dom', qr: 'qr' }[channel] || '';
    return '<span class="channel-pill ' + cls + '">' + _escHtml(CHANNEL_LABELS[channel] || channel || '-') + '</span>';
  }

  // Table rounds and web orders use different status words; a person reads one set.
  const STATUS_LABELS = {
    recibido: ['Recibido', 'warn'], en_preparacion: ['En cocina', 'warn'], listo: ['Listo', 'success'],
    entregado: ['Entregado', 'success'], generar_factura: ['Cobrando', 'info'],
    factura_entregada: ['Pagado', 'success'], pendiente_aceptacion: ['Por aceptar', 'warn'],
    en_camino: ['En camino', 'info'], en_puerta: ['En la puerta', 'info'],
    cancelado: ['Cancelado', 'danger'], cancelled: ['Cancelado', 'danger'], rechazado: ['Rechazado', 'danger'],
  };
  function statusLabel(status) {
    return (STATUS_LABELS[status] || [status || ''])[0];
  }
  function histStatusBadge(status) {
    if (!status) return '';
    const hit = STATUS_LABELS[status] || [status, ''];
    return '<span class="badge ' + hit[1] + '">' + _escHtml(hit[0]) + '</span>';
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
    const summaryEl = document.getElementById('hist-summary');
    if (summaryEl) {
      const sum = (orders || []).reduce(function (acc, o) {
        return acc + (/^(cancelado|cancelled|rechazado)$/.test(o.status) ? 0 : Number(o.total || 0));
      }, 0);
      summaryEl.textContent = (orders || []).length + ' pedidos · ' +
        (typeof mesioFmt === 'function' ? mesioFmt(sum) : '$' + sum);
    }
    if (!orders || !orders.length) {
      body.innerHTML = '<div style="padding:18px;color:var(--text-3);font-size:13px;">(sin pedidos en este período)</div>';
      body.setAttribute('data-loaded', 'true');
      return;
    }
    body.innerHTML = orders.map(function (o) {
      const total = typeof mesioFmt === 'function' ? mesioFmt(o.total || 0) : '$' + (o.total || 0);
      const date = typeof mesioDate === 'function' ? mesioDate(o.created_at || '') : (o.created_at || '');
      const channel = o.channel;
      const customer = o.source === 'table'
        ? (/^\d+$/.test(o.who || '') ? 'Mesa ' + o.who : (o.who || 'Mesa'))
        : ((o.who || 'Cliente') + (o.source === 'pickup' ? ' · recoger' : ''));
      const items = o.items_summary || '';
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


  // Table rounds + web orders of this sede, restaurant-local days
  // (GET /api/stats/order-history). It read /api/orders before, which only
  // ever had web orders — the history never showed a single table.
  async function loadHistoryOrders() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/stats/order-history?days=' + _histDays, { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      _allOrders = Array.isArray(data.orders) ? data.orders : [];
      renderHistoryOrders(currentHistoryRows());
    } catch (e) {
      console.error('pedidos: history error', e);
    }
  }

  function exportCSV() {
    const rows = currentHistoryRows();
    if (!rows.length) {
      if (typeof mesioToast === 'function') mesioToast('No hay pedidos para exportar en este período', 'info');
      return;
    }
    const cell = function (v) { return '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"'; };
    const lines = [['Pedido', 'Canal', 'Cliente / mesa', 'Productos', 'Fecha', 'Total', 'Estado'].map(cell).join(',')];
    rows.forEach(function (o) {
      lines.push([o.id, CHANNEL_LABELS[o.channel] || o.channel, o.who, o.items_summary,
        o.created_at, o.total, statusLabel(o.status)].map(cell).join(','));
    });
    // BOM so Excel opens the accents right.
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'pedidos-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }

  // ── Dining-room monitor (active tables) ──────────────────────────────

  function renderSalonMonitor(tables) {
    var grids = document.querySelectorAll('.kds-grid');
    var salonGrid = grids[1]; // second kds-grid is the dining-room monitor
    if (!salonGrid) return;

    // Populate metrics row for salon section (second metrics-row inside #tab-rt)
    var metricsRows = document.querySelectorAll('#tab-rt .metrics-row');
    var salonMetrics = metricsRows[1];

    var activeTables = (tables || []).filter(function (t) {
      return t.session_active || t.bot_active || (t.pending_orders && t.pending_orders.length);
    });

    if (salonMetrics) {
      var vals = salonMetrics.querySelectorAll('.metric-value');
      var openTickets = activeTables.filter(function (t) { return t.has_open_check; }).length;
      var totalTicket = 0;
      var ticketCount = 0;
      activeTables.forEach(function (t) {
        if (t.current_total) { totalTicket += +t.current_total; ticketCount++; }
      });
      var avgTicket = ticketCount > 0 ? totalTicket / ticketCount : 0;
      var fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };
      if (vals[0]) vals[0].textContent = activeTables.length;
      if (vals[1]) vals[1].textContent = openTickets;
      if (vals[2]) vals[2].textContent = fmt(avgTicket);
      if (vals[3]) vals[3].textContent = '—';
    }

    if (!activeTables.length) {
      salonGrid.innerHTML = '<div style="padding:24px;color:var(--text-3);font-size:13px;">(sin mesas activas)</div>';
      salonGrid.setAttribute('data-loaded', 'true');
      return;
    }

    var fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };

    salonGrid.innerHTML = activeTables.map(function (t) {
      var tableNum = t.table_number || t.name || t.id;
      var pax = t.party_size || t.capacity || '';
      var staffName = t.assigned_staff_name || '';
      var startedAt = t.session_started_at;
      var minutesOpen = '—';
      if (startedAt) {
        var mins = Math.floor((Date.now() - new Date(startedAt).getTime()) / 60000);
        minutesOpen = mins < 60 ? mins + ' min' : Math.floor(mins / 60) + 'h ' + (mins % 60) + 'min';
      }
      var items = Array.isArray(t.items) ? t.items : [];
      var itemsHtml = items.slice(0, 3).map(function (it) {
        return '<div class="kds-item"><span>' + _escHtml(it.name || '') + '</span><span class="mono">×' + (it.qty || 1) + '</span></div>';
      }).join('');
      if (items.length > 3) {
        itemsHtml += '<div class="kds-item" style="color:var(--text-3);">+' + (items.length - 3) + ' más</div>';
      }
      var total = t.current_total ? fmt(t.current_total) : '—';
      return '<div class="kds-card">' +
        '<div class="kds-top"><div>' +
        '<div class="kds-id">Mesa ' + _escHtml(String(tableNum)) + (pax ? ' · ' + pax + ' pax' : '') + '</div>' +
        (staffName ? '<div class="kds-name">Mesero: ' + _escHtml(staffName) + '</div>' : '') +
        '<div class="kds-sub">Abierto hace ' + minutesOpen + '</div>' +
        '</div></div>' +
        (itemsHtml ? '<div class="kds-items">' + itemsHtml + '</div>' : '') +
        '<div class="kds-foot"><span></span><span class="kds-price">' + total + '</span></div>' +
        '</div>';
    }).join('');

    salonGrid.setAttribute('data-loaded', 'true');
  }

  async function loadSalonMonitor() {
    try {
      var headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      var res = await fetch('/api/pos/tables-status', { headers: headers });
      if (!res.ok) { return; }
      var data = await res.json();
      var tables = data.tables || data;
      renderSalonMonitor(Array.isArray(tables) ? tables : []);
    } catch (e) {
      console.error('pedidos: salon monitor error', e);
    }
  }

  // Initial load
  loadLiveOrders();
  loadSalonMonitor();
  loadHistoryOrders();

  // Auto-refresh live section every 30s
  if (typeof mesioInterval === 'function') {
    mesioInterval(function () { loadLiveOrders(); loadSalonMonitor(); }, 30000);
  }
})();
