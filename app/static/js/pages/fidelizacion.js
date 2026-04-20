/* ── Fidelización page ───────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Campaign status filter
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
    });
  });

  // New campaign button
  const newCampBtn = document.getElementById('btn-new-campaign');
  if (newCampBtn) {
    newCampBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Creador de campañas próximamente disponible', 'info');
      }
    });
  }

  // Configure program
  const configBtn = document.getElementById('btn-configure');
  if (configBtn) {
    configBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Configuración del programa próximamente disponible', 'info');
      }
    });
  }

  // Segment cards
  document.querySelectorAll('.seg-card').forEach(function (card) {
    card.addEventListener('click', function () {
      const title = card.querySelector('.seg-card-title, [style*="font-weight: 600"]');
      if (title && typeof mesioToast === 'function') {
        mesioToast('Segmento: ' + title.textContent, 'info', 1500);
      }
    });
  });

  // Campaign toggle switches
  document.querySelectorAll('.camp-row .toggle input').forEach(function (input) {
    input.addEventListener('change', function () {
      const row = input.closest('.camp-row');
      const name = row ? row.querySelector('[style*="font-weight: 500"]') : null;
      if (typeof mesioToast === 'function') {
        mesioToast((input.checked ? 'Activada: ' : 'Pausada: ') + (name ? name.textContent : ''), 'info', 2000);
      }
    });
  });

  // ── Render loyalty stats ──────────────────────────────────────────

  function renderLoyaltyStats(data) {
    if (!data) return;
    const tier = document.querySelectorAll('.tier-card');
    if (!tier.length) return;

    const tiers = data.tiers || data;
    if (!Array.isArray(tiers)) return;

    tiers.forEach(function (t, i) {
      if (!tier[i]) return;
      const valEl = tier[i].querySelector('.tier-val');
      const subEl = tier[i].querySelector('.tier-sub');
      if (valEl && t.count !== undefined) valEl.textContent = t.count.toLocaleString();
      if (subEl && t.label) subEl.textContent = t.label;
    });

    // Update global metrics if present
    const metrics = document.querySelectorAll('.metrics-row .metric-value');
    if (data.total_customers !== undefined && metrics[0]) {
      metrics[0].textContent = data.total_customers.toLocaleString();
    }
    if (data.active_30d !== undefined && metrics[1]) {
      metrics[1].textContent = data.active_30d.toLocaleString();
    }
    if (data.redemptions_month !== undefined && metrics[2]) {
      metrics[2].textContent = data.redemptions_month.toLocaleString();
    }
  }

  // ── Render loyalty customers ──────────────────────────────────────

  function renderLoyaltyCustomers(customers) {
    let container = document.getElementById('loyalty-customers-body');
    if (!container) {
      // Find the customers table/card and inject the container
      const cards = document.querySelectorAll('.card.flush');
      let targetCard = null;
      cards.forEach(function (c) {
        if (c.textContent.includes('Clientes') || c.textContent.includes('ranking')) {
          targetCard = c;
        }
      });
      if (!targetCard) return; // No customers card found — skip gracefully
      container = document.createElement('div');
      container.id = 'loyalty-customers-body';
      targetCard.appendChild(container);
    }

    if (!customers || !customers.length) {
      container.innerHTML = '<div style="padding:18px;color:var(--text-3);font-size:13px;">(sin clientes de fidelización)</div>';
      container.setAttribute('data-loaded', 'true');
      return;
    }

    const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };
    container.innerHTML = customers.slice(0, 20).map(function (c) {
      const name = c.customer_name || c.name || c.phone || 'Cliente';
      const points = c.points_balance || c.current_points || 0;
      const total = c.total_spent || c.lifetime_value || 0;
      const tier = c.tier || c.level || 'Bronce';
      const initials = name.split(' ').map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase();
      return '<div style="display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:0.5px solid var(--border);">' +
        '<div style="width:32px;height:32px;border-radius:50%;background:var(--brand-light);color:var(--brand);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;flex-shrink:0;">' + _escHtml(initials) + '</div>' +
        '<div style="flex:1;">' +
        '<div style="font-weight:500;font-size:13px;">' + _escHtml(name) + '</div>' +
        '<div style="font-size:11.5px;color:var(--text-3);">' + _escHtml(tier) + ' · ' + points + ' pts</div>' +
        '</div>' +
        '<div style="font-size:12px;color:var(--text-2);">' + fmt(total) + '</div>' +
        '</div>';
    }).join('');

    container.setAttribute('data-loaded', 'true');
  }

  async function loadLoyaltyStats() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/loyalty/stats', { headers });
      if (res.ok) {
        const data = await res.json();
        renderLoyaltyStats(data);
      }
    } catch (e) {
      console.error('fidelizacion: stats error', e);
    }
  }

  async function loadLoyaltyCustomers() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/loyalty/customers', { headers });
      if (res.status === 404) {
        // Endpoint pending — render placeholder
        renderLoyaltyCustomers(null);
        return;
      }
      if (!res.ok) { return; }
      const data = await res.json();
      const customers = data.customers || data;
      renderLoyaltyCustomers(Array.isArray(customers) ? customers : []);
    } catch (e) {
      console.error('fidelizacion: customers error', e);
      renderLoyaltyCustomers(null); // Graceful degradation
    }
  }

  loadLoyaltyStats();
  loadLoyaltyCustomers();
})();
