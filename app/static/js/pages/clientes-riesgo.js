/* ── Clientes en Riesgo page ─────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Sort filter buttons
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
    });
  });

  // Action buttons per customer row
  document.querySelectorAll('.risk-row .btn.sm.primary').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      const row = btn.closest('.risk-row');
      const nameEl = row ? row.querySelector('[style*="font-weight: 500"]') : null;
      const name = nameEl ? nameEl.textContent.trim() : 'cliente';
      const action = btn.textContent.trim();

      if (action.includes('Llamar')) {
        if (typeof mesioToast === 'function') {
          mesioToast('Iniciando contacto con ' + name + '…', 'info', 2000);
        }
      } else if (action.includes('Oferta IA')) {
        sendAIOferta(row, name);
      }
    });
  });

  async function sendAIOferta(row, name) {
    const phoneEl = row ? row.querySelector('.risk-sub, [style*="color: var(--text-3)"]') : null;
    const phone = phoneEl ? phoneEl.textContent.trim() : '';
    if (typeof mesioToast === 'function') {
      mesioToast('Generando oferta personalizada para ' + name + '…', 'info', 2000);
    }
    // TODO: wire to campaign endpoint when available
  }

  // Bulk campaign button
  const bulkBtn = document.querySelector('.page-head .btn.primary');
  if (bulkBtn) {
    bulkBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Campaña masiva IA — próximamente disponible', 'info');
      }
    });
  }

  // Export CSV
  const exportBtn = document.querySelector('.page-head .btn:not(.primary)');
  if (exportBtn && exportBtn.textContent.includes('Exportar')) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Exportación próximamente disponible', 'info');
      }
    });
  }

  // Send bulk offer (medium risk section)
  const bulkOfferBtn = document.querySelector('.card.flush:last-of-type .btn.sm');
  if (bulkOfferBtn) {
    bulkOfferBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Enviando oferta a 24 clientes de riesgo medio…', 'info', 2500);
      }
    });
  }

  async function loadAtRiskCustomers() {
    try {
      const headers = mesioHeaders ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/stats/customers-at-risk?limit=50', { headers });
      if (res.ok) {
        // At-risk customers loaded — rendering would replace static rows
      }
    } catch (e) {
      // Static placeholder remains
    }
  }

  loadAtRiskCustomers();
})();
