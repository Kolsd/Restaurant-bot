/* ── Nómina page ─────────────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Period selector
  let currentPeriod = 'Q2 abril';
  document.querySelectorAll('.page-head .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.page-head .seg-btn').forEach(function (b) { b.classList.remove('active'); });
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
      filterByRole(btn.textContent.trim());
    });
  });

  function filterByRole(role) {
    const rows = document.querySelectorAll('.pay-row:not(.head)');
    rows.forEach(function (row) {
      if (role === 'Todos') { row.style.display = ''; return; }
      const sub = row.querySelector('[style*="color: var(--text-3)"]');
      const roleText = sub ? sub.textContent.toLowerCase() : '';
      const visible = role === 'Meseros' ? roleText.includes('mesero')
        : role === 'Cocina' ? (roleText.includes('cocinero') || roleText.includes('chef'))
        : role === 'Bar' ? roleText.includes('bartender')
        : role === 'Admin' ? (roleText.includes('admin') || roleText.includes('cajera'))
        : true;
      row.style.display = visible ? '' : 'none';
    });
  }

  // Process payroll button
  const processBtn = document.querySelector('.page-head .btn.primary');
  if (processBtn && processBtn.textContent.includes('Procesar')) {
    processBtn.addEventListener('click', processPayroll);
  }

  // Export PDF button
  const exportBtn = document.querySelector('.page-head .btn:not(.primary)');
  if (exportBtn && exportBtn.textContent.includes('Exportar')) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Exportación PDF próximamente disponible', 'info');
      }
    });
  }

  // Configure tips button
  const configTipsBtn = document.querySelector('.card .btn.sm.ghost');
  if (configTipsBtn && configTipsBtn.textContent.includes('Configurar')) {
    configTipsBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Configuración de propinas en Equipo → Nómina', 'info', 2500);
      }
    });
  }

  // Row click — show detail
  document.querySelectorAll('.pay-row:not(.head)').forEach(function (row) {
    row.addEventListener('click', function () {
      const name = row.querySelector('[style*="font-weight: 500"]');
      if (name && typeof mesioToast === 'function') {
        mesioToast('Desglose de ' + name.textContent + ' — próximamente disponible', 'info', 2000);
      }
    });
  });

  async function loadPayroll(period) {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      // Map period label to date range
      const today = new Date();
      let periodStart, periodEnd;
      if (period.includes('Q1')) {
        periodStart = new Date(today.getFullYear(), today.getMonth(), 1).toISOString().split('T')[0];
        periodEnd = new Date(today.getFullYear(), today.getMonth(), 15).toISOString().split('T')[0];
      } else {
        periodStart = new Date(today.getFullYear(), today.getMonth(), 16).toISOString().split('T')[0];
        periodEnd = new Date(today.getFullYear(), today.getMonth() + 1, 0).toISOString().split('T')[0];
      }
      const params = new URLSearchParams({ period_start: periodStart, period_end: periodEnd });
      const res = await fetch('/api/staff/payroll/calculate?' + params.toString(), { headers });
      if (res.ok) {
        // Payroll data loaded — table rendering would go here
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  async function processPayroll() {
    const confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Procesar pagos de nómina Q2 abril?', { confirmText: 'Procesar', danger: false })
      : window.confirm('¿Procesar pagos?');
    if (!confirmed) return;
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const today = new Date();
      const body = {
        period_start: new Date(today.getFullYear(), today.getMonth(), 16).toISOString().split('T')[0],
        period_end: new Date(today.getFullYear(), today.getMonth() + 1, 0).toISOString().split('T')[0],
        snapshot: {}
      };
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
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const today = new Date();
      const params = new URLSearchParams({
        period_start: new Date(today.getFullYear(), today.getMonth(), 16).toISOString().split('T')[0],
        period_end: new Date(today.getFullYear(), today.getMonth() + 1, 0).toISOString().split('T')[0]
      });
      const res = await fetch('/api/staff/tips/auto?' + params.toString(), { headers });
      if (res.ok) {
        // Tips data loaded
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  loadPayroll(currentPeriod);
  loadTipsAuto();
})();
