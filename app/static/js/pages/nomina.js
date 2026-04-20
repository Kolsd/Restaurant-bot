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
        mesioToast('Exportación PDF próximamente disponible', 'info');
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
      const net = emp.net_salary || emp.total || (base + tips - deductions + overtime);
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
        '<div class="num" style="font-weight:600;">' + fmt(net) + '</div>' +
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
    // Update the tip bar values if data present
    if (!data || !data.by_role) return;
    const byRole = data.by_role;
    // Visual update: just update the total label
    const totalEl = document.querySelector('.period-total');
    if (totalEl && data.total_net) {
      const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };
      // Don't override — the period total is salaries not just tips
    }
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
      renderPayroll(Array.isArray(employees) ? employees : []);
    } catch (e) {
      console.error('nomina: payroll error', e);
    }
  }

  async function processPayroll() {
    const confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Procesar pagos de nómina ' + currentPeriod + '?', { confirmText: 'Procesar', danger: false })
      : window.confirm('¿Procesar pagos?');
    if (!confirmed) return;
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const { periodStart, periodEnd } = periodDates(currentPeriod);
      const body = { period_start: periodStart, period_end: periodEnd, snapshot: {} };
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
