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

  // Bulk campaign button
  const bulkBtn = document.getElementById('btn-bulk-campaign');
  if (bulkBtn) {
    bulkBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Disponible en la próxima versión', 'info');
      }
    });
  }

  // Export CSV
  const exportBtn = document.getElementById('btn-export');
  if (exportBtn) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Disponible en la próxima versión', 'info');
      }
    });
  }

  // Send bulk offer (medium risk section)
  const bulkOfferBtn = document.getElementById('btn-mass-offer');
  if (bulkOfferBtn) {
    bulkOfferBtn.addEventListener('click', function () {
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
      const body = document.getElementById('risk-body');
      if (!body) return;
      body.querySelectorAll('.risk-row').forEach(function (row) {
        const text = row.textContent.toLowerCase();
        row.style.display = (!q || text.includes(q)) ? '' : 'none';
      });
    });
  }

  // ── Render ────────────────────────────────────────────────────────

  function scoreClass(score) {
    if (score >= 80) return 'hi';
    if (score >= 50) return 'md';
    return '';
  }

  function renderAtRiskCustomers(customers) {
    let body = document.getElementById('risk-body');
    if (!body) {
      const head = document.querySelector('.risk-row.head');
      if (!head) return;
      const parent = head.parentNode;
      // Remove old static data rows (keep head)
      parent.querySelectorAll('.risk-row:not(.head)').forEach(function (r) { r.remove(); });
      body = document.createElement('div');
      body.id = 'risk-body';
      parent.insertBefore(body, head.nextSibling);
    }

    if (!customers || !customers.length) {
      body.innerHTML = '<div style="padding:18px;color:var(--text-3);">(sin clientes en riesgo)</div>';
      body.setAttribute('data-loaded', 'true');
      return;
    }

    const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };

    body.innerHTML = customers.map(function (c, idx) {
      const name = c.customer_name || c.name || c.phone || 'Cliente';
      const phone = c.phone || '';
      const score = Math.round((c.churn_score || c.risk_score || 0) * 100);
      const last = c.days_since_last_visit !== undefined ? 'hace ' + c.days_since_last_visit + 'd' : (c.last_visit || '-');
      const ltv = c.lifetime_value || c.ltv || 0;
      const reason = c.churn_reason || c.reason || '';
      const initials = name.split(' ').map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase();
      const bg = ['#FDE8CE', '#DBEAFE', '#EDE9FE', '#DCFCE7', '#FEE2E2'][idx % 5];
      const fg = ['#BA7517', '#1E40AF', '#5B21B6', '#166534', '#991B1B'][idx % 5];
      const isLast = idx === customers.length - 1;

      return '<div class="risk-row" style="' + (isLast ? 'border-bottom:none;' : '') + '">' +
        '<div class="risk-avatar" style="background:' + bg + ';color:' + fg + ';">' + _escHtml(initials) + '</div>' +
        '<div>' +
        '<div style="font-weight:500;">' + _escHtml(name) + '</div>' +
        '<div style="font-size:11.5px;color:var(--text-3);">' + _escHtml(phone) + '</div>' +
        '</div>' +
        '<div class="num"><span class="score-pill ' + scoreClass(score) + '">' + score + '%</span></div>' +
        '<div class="num muted">' + _escHtml(last) + '</div>' +
        '<div class="num"><div class="spark"></div></div>' +
        '<div class="num mono" style="font-weight:600;">' + fmt(ltv) + '</div>' +
        '<div style="font-size:12px;color:var(--text-2);">' + _escHtml(reason.slice(0, 80)) + '</div>' +
        '<div>' +
        '<button class="btn sm primary" data-action="reengage" data-name="' + _escHtml(name) + '" data-phone="' + _escHtml(phone) + '">Contactar</button>' +
        '</div>' +
        '</div>';
    }).join('');

    body.setAttribute('data-loaded', 'true');

    // Bind per-row action buttons
    body.querySelectorAll('[data-action="reengage"]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        const name = btn.dataset.name;
        const phone = btn.dataset.phone;
        if (!phone) {
          if (typeof mesioToast === 'function') mesioToast('Sin número disponible para ' + name, 'warn');
          return;
        }
        const digits = phone.replace(/\D/g, '');
        const restaurantRaw = localStorage.getItem('rb_restaurant');
        let restaurantName = 'nuestro restaurante';
        try {
          const r = JSON.parse(restaurantRaw);
          restaurantName = (r && r.name) ? r.name : restaurantName;
        } catch (_) {}
        const msg = 'Hola ' + name + ', te extrañamos en ' + restaurantName + '! Tenemos novedades en el menú que seguro te van a gustar. ¿Quieres que te enviemos la carta?';
        const url = 'https://wa.me/' + digits + '?text=' + encodeURIComponent(msg);
        window.open(url, '_blank');
        if (typeof mesioToast === 'function') {
          mesioToast('Abriendo WhatsApp con ' + name + '…', 'success', 2500);
        }
      });
    });
  }

  async function loadAtRiskCustomers() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/stats/customers-at-risk?limit=50', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const customers = data.customers || data;
      renderAtRiskCustomers(Array.isArray(customers) ? customers : []);
    } catch (e) {
      console.error('clientes-riesgo: error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al cargar clientes en riesgo', 'warning');
    }
  }

  loadAtRiskCustomers();

  // Auto-refresh every 30s
  if (typeof mesioInterval === 'function') {
    mesioInterval(loadAtRiskCustomers, 30000);
  }
})();
