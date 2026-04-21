/* ── Nómina page ─────────────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Period selector
  let currentPeriod = 'Q2 abril';
  document.querySelectorAll('.page-head .seg-btn, .period-card .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.period-card .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      currentPeriod = btn.textContent.trim();
      loadPayroll(currentPeriod);
    });
  });

  // Role filter
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      filterByRole(btn.dataset.roleFilter || btn.textContent.trim());
    });
  });

  function filterByRole(role) {
    const body = document.getElementById('payroll-body');
    if (!body) return;
    body.querySelectorAll('.pay-row').forEach(function (row) {
      if (role === 'Todos') { row.style.display = ''; return; }
      const sub = row.querySelector('.pay-role');
      const roleText = sub ? sub.textContent.toLowerCase() : '';
      const visible = role === 'Meseros' ? roleText.includes('mesero')
        : role === 'Cocina' ? (roleText.includes('cocinero') || roleText.includes('chef') || roleText.includes('cocina'))
        : role === 'Bar' ? roleText.includes('bartender')
        : role === 'Admin' ? (roleText.includes('admin') || roleText.includes('cajera') || roleText.includes('gerente'))
        : true;
      row.style.display = visible ? '' : 'none';
    });
  }

  // Process payroll button
  const processBtn = document.getElementById('btn-process-payroll');
  if (processBtn) {
    processBtn.addEventListener('click', processPayroll);
  }

  // Export PDF button
  const exportBtn = document.getElementById('btn-export-pdf');
  if (exportBtn) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Disponible en la próxima versión', 'info');
      }
    });
  }

  // Configure tips button
  const configTipsBtn = document.getElementById('btn-config-tips');
  if (configTipsBtn) {
    configTipsBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Configuración de propinas en Equipo → Nómina', 'info', 2500);
      }
    });
  }

  // ── Period → date range ───────────────────────────────────────────

  function periodDates(period) {
    const today = new Date();
    let periodStart, periodEnd;
    if (period.includes('Q1')) {
      periodStart = new Date(today.getFullYear(), today.getMonth(), 1).toISOString().split('T')[0];
      periodEnd = new Date(today.getFullYear(), today.getMonth(), 15).toISOString().split('T')[0];
    } else {
      periodStart = new Date(today.getFullYear(), today.getMonth(), 16).toISOString().split('T')[0];
      periodEnd = new Date(today.getFullYear(), today.getMonth() + 1, 0).toISOString().split('T')[0];
    }
    return { periodStart, periodEnd };
  }

  // ── Render ────────────────────────────────────────────────────────

  function getOrCreatePayrollBody() {
    let body = document.getElementById('payroll-body');
    if (!body) {
      const head = document.querySelector('.pay-row.head');
      if (!head) return null;
      // Remove static rows
      const parent = head.parentNode;
      parent.querySelectorAll('.pay-row:not(.head)').forEach(function (r) { r.remove(); });
      body = document.createElement('div');
      body.id = 'payroll-body';
      parent.insertBefore(body, head.nextSibling);
    }
    return body;
  }

  function avatarInitials(name) {
    return (name || '?').split(' ').map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase();
  }

  function renderPayroll(employees) {
    const body = getOrCreatePayrollBody();
    if (!body) return;

    if (!employees || !employees.length) {
      body.innerHTML = '<div style="padding:18px;color:var(--text-3);">(sin datos de nómina para este período)</div>';
      body.setAttribute('data-loaded', 'true');
      return;
    }

    const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };

    body.innerHTML = employees.map(function (emp) {
      const name = emp.name || emp.full_name || 'Empleado';
      const role = emp.role || emp.position || '';
      const hours = emp.hours_worked || emp.billable_hours || 0;
      const base = emp.base_salary || emp.gross_salary || 0;
      const tips = emp.tips || emp.tips_total || 0;
      const deductions = emp.deductions || emp.total_deductions || 0;
      const overtime = emp.overtime_pay || emp.extra || 0;
      const net = emp.net_salary ?? emp.total ?? null;
      const initials = avatarInitials(name);

      return '<div class="pay-row" style="cursor:pointer;">' +
        '<div class="avatar-sm" style="background:var(--brand-light);color:var(--brand);">' + _escHtml(initials) + '</div>' +
        '<div>' +
        '<div style="font-weight:500;">' + _escHtml(name) + '</div>' +
        '<div class="pay-role" style="font-size:11.5px;color:var(--text-3);">' + _escHtml(role) + '</div>' +
        '</div>' +
        '<div class="num">' + Math.round(hours) + 'h</div>' +
        '<div class="num">' + fmt(base) + '</div>' +
        '<div class="num brand">+' + fmt(tips) + '</div>' +
        '<div class="num danger">' + (deductions ? '-' + fmt(deductions) : '$0') + '</div>' +
        '<div class="num">' + (overtime ? fmt(overtime) : '$0') + '</div>' +
        '<div class="num" style="font-weight:600;">' + (net !== null ? fmt(net) : '—') + '</div>' +
        '<div><span class="badge success">OK</span></div>' +
        '</div>';
    }).join('');

    body.setAttribute('data-loaded', 'true');

    // Row click detail
    body.querySelectorAll('.pay-row').forEach(function (row) {
      row.addEventListener('click', function () {
        const nameEl = row.querySelector('[style*="font-weight:500"]') || row.querySelector('[style*="font-weight: 500"]');
        const name = nameEl ? nameEl.textContent : 'empleado';
        if (typeof mesioToast === 'function') {
          mesioToast('Desglose de ' + name + ' — próximamente disponible', 'info', 2000);
        }
      });
    });
  }

  function renderTipsSummary(data) {
    const container = document.getElementById('tips-bars-container');
    const totalLabel = document.getElementById('tips-total-label');
    if (!container) return;

    const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };

    if (!data || !data.entries || !data.entries.length) {
      container.innerHTML = '<div style="padding:6px 0;color:var(--text-3);font-size:13px;">Sin propinas en este período</div>';
      if (totalLabel) totalLabel.textContent = fmt(0);
      return;
    }

    if (totalLabel) totalLabel.textContent = fmt(data.total_tips || 0);

    const byRole = {};
    data.entries.forEach(function (e) {
      if (!e.role) return;
      byRole[e.role] = (byRole[e.role] || 0) + (e.total_tips || 0);
    });

    const totalTips = data.total_tips || 1;
    const roleColors = { mesero: 'var(--brand)', cocina: 'var(--warning)', bar: '#378ADD' };
    const roleLabels = { mesero: 'Meseros', cocina: 'Cocina', bar: 'Bar' };

    const bars = Object.keys(byRole).map(function (role) {
      const amount = byRole[role];
      const pct = Math.round((amount / totalTips) * 100);
      const color = roleColors[role] || 'var(--brand)';
      const label = roleLabels[role] || _escHtml(role.charAt(0).toUpperCase() + role.slice(1));
      return '<div style="flex:1;min-width:120px;">' +
        '<div style="font-size:11.5px;color:var(--text-3);margin-bottom:4px;">' + label + '</div>' +
        '<div class="tip-bar"><div class="tip-fill" style="width:' + pct + '%;background:' + color + ';" title="' + pct + '%"></div></div>' +
        '<div style="font-size:12px;font-weight:600;margin-top:4px;">' + pct + '% &middot; ' + fmt(amount) + '</div>' +
        '</div>';
    });

    container.innerHTML = bars.length ? bars.join('') : '<div style="padding:6px 0;color:var(--text-3);font-size:13px;">Sin propinas en este período</div>';
  }

  function renderPayrollSummary(employees, periodStart, periodEnd) {
    const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };
    const list = Array.isArray(employees) ? employees : [];

    // Aggregate totals across all employees
    let totalBase = 0, totalTips = 0, totalDeductions = 0, totalOvertime = 0, totalNet = 0;
    list.forEach(function (emp) {
      totalBase       += +(emp.base_salary        || emp.gross_salary      || 0);
      totalTips       += +(emp.tips               || emp.tips_total        || emp.tip_earnings || 0);
      totalDeductions += +(emp.deductions         || emp.total_deductions  || 0);
      totalOvertime   += +(emp.overtime_pay       || emp.extra             || 0);
      const net = emp.net_salary !== undefined ? emp.net_salary : (emp.total !== undefined ? emp.total : null);
      if (net !== null && net !== undefined) totalNet += +net;
    });

    // Period card
    const titleEl = document.getElementById('period-title');
    const datesEl = document.getElementById('period-dates');
    const totalEl = document.getElementById('period-total');
    const subEl   = document.getElementById('period-sub');

    if (titleEl) {
      // Derive a human label from periodStart month
      try {
        var d = new Date(periodStart + 'T12:00:00');
        var monthName = d.toLocaleString('es-CO', { month: 'long' });
        // Is it first or second half?
        var day = parseInt(periodStart.split('-')[2], 10);
        titleEl.textContent = (day <= 15 ? 'Primera quincena · ' : 'Segunda quincena · ') +
          monthName.charAt(0).toUpperCase() + monthName.slice(1);
      } catch (e2) {
        titleEl.textContent = 'Período';
      }
    }

    if (datesEl && periodStart && periodEnd) {
      try {
        var ds = new Date(periodStart + 'T12:00:00');
        var de = new Date(periodEnd   + 'T12:00:00');
        var fmtDate = function (d) {
          return d.getDate() + ' ' + d.toLocaleString('es-CO', { month: 'short' });
        };
        datesEl.textContent = fmtDate(ds) + ' — ' + fmtDate(de);
      } catch (e3) {
        datesEl.textContent = periodStart + ' — ' + periodEnd;
      }
    }

    if (totalEl) totalEl.textContent = fmt(totalNet);
    if (subEl)   subEl.textContent   = list.length + ' empleado' + (list.length === 1 ? '' : 's') + ' · borrador';

    // Summary metrics
    var setMetric = function (id, val) {
      var el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    setMetric('summary-base',        fmt(totalBase));
    setMetric('summary-tips',        fmt(totalTips));
    setMetric('summary-deductions',  list.length ? '-' + fmt(totalDeductions) : fmt(0));
    setMetric('summary-overtime',    fmt(totalOvertime));
  }

  async function loadPayroll(period) {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const { periodStart, periodEnd } = periodDates(period);
      const params = new URLSearchParams({ period_start: periodStart, period_end: periodEnd });
      const res = await fetch('/api/staff/payroll/calculate?' + params.toString(), { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const employees = data.employees || data.payroll || data;
      const empList = Array.isArray(employees) ? employees : [];
      renderPayroll(empList);
      renderPayrollSummary(empList, periodStart, periodEnd);
    } catch (e) {
      console.error('nomina: payroll error', e);
    }
  }

  async function processPayroll() {
    const confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Procesar pagos de nómina ' + currentPeriod + '?', { confirmText: 'Procesar', danger: false })
      : await mesioConfirm('¿Procesar pagos?');
    if (!confirmed) return;
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const { periodStart, periodEnd } = periodDates(currentPeriod);
      const payrollBody = document.getElementById('payroll-body');
      const employeeRows = payrollBody ? Array.from(payrollBody.querySelectorAll('.pay-row')).map(function(row) {
        const nameEl = row.querySelector('[style*="font-weight:500"]') || row.querySelector('[style*="font-weight: 500"]');
        const roleEl = row.querySelector('.pay-role');
        return { name: nameEl ? nameEl.textContent : '', role: roleEl ? roleEl.textContent : '' };
      }) : [];
      const body = { period_start: periodStart, period_end: periodEnd, snapshot: { employees: employeeRows } };
      const res = await fetch('/api/staff/payroll/runs', { method: 'POST', headers, body: JSON.stringify(body) });
      if (res.ok) {
        if (typeof mesioToast === 'function') mesioToast('Nómina procesada exitosamente', 'success');
      } else {
        if (typeof mesioToast === 'function') mesioToast('Error al procesar nómina', 'error');
      }
    } catch (e) {
      if (typeof mesioToast === 'function') mesioToast('Error de conexión', 'error');
    }
  }

  async function loadTipsAuto() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const { periodStart, periodEnd } = periodDates(currentPeriod);
      const params = new URLSearchParams({ period_start: periodStart, period_end: periodEnd });
      const res = await fetch('/api/staff/tips/auto?' + params.toString(), { headers });
      if (res.ok) {
        const data = await res.json();
        renderTipsSummary(data);
      }
    } catch (e) {
      // Tips summary is secondary — silent fail
    }
  }

  loadPayroll(currentPeriod);
  loadTipsAuto();
})();
