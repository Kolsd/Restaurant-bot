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
        mesioToast('Disponible en la próxima versión', 'info');
      }
    });
  }

  // Configure program
  const configBtn = document.getElementById('btn-configure');
  if (configBtn) {
    configBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Disponible en la próxima versión', 'info');
      }
    });
  }

  // Segment cards
  document.querySelectorAll('.seg-card').forEach(function (card) {
    card.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Disponible en la próxima versión', 'info');
      }
    });
  });

  // Ver todos segments
  const viewAllBtn = document.getElementById('btn-view-all-segments');
  if (viewAllBtn) {
    viewAllBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Disponible en la próxima versión', 'info');
      }
    });
  }

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

  // ── Tier thresholds ───────────────────────────────────────────────

  function getTierThresholds() {
    var stored = null;
    try {
      var rest = localStorage.getItem('rb_restaurant');
      if (rest) {
        var r = JSON.parse(rest);
        stored = r && r.features && r.features.loyalty_tiers ? r.features.loyalty_tiers : null;
      }
    } catch (e) { }
    return stored || { bronce: [0, 20], plata: [21, 80], oro: [81, 200], platino: [201, Infinity] };
  }

  function computeTierCounts(customers) {
    var t = getTierThresholds();
    var counts = { bronce: 0, plata: 0, oro: 0, platino: 0 };
    customers.forEach(function (c) {
      var pts = c.points_balance || 0;
      if (pts >= t.platino[0]) counts.platino++;
      else if (pts >= t.oro[0]) counts.oro++;
      else if (pts >= t.plata[0]) counts.plata++;
      else counts.bronce++;
    });
    return counts;
  }

  function renderTierCounts(counts) {
    var bronce = document.getElementById('tier-bronce-count');
    var plata  = document.getElementById('tier-plata-count');
    var oro    = document.getElementById('tier-oro-count');
    var platino = document.getElementById('tier-platino-count');
    if (bronce)  bronce.textContent  = counts.bronce.toLocaleString();
    if (plata)   plata.textContent   = counts.plata.toLocaleString();
    if (oro)     oro.textContent     = counts.oro.toLocaleString();
    if (platino) platino.textContent = counts.platino.toLocaleString();
  }

  // ── Render loyalty stats ──────────────────────────────────────────

  function renderLoyaltyStats(data) {
    if (!data) return;
    var metrics = document.querySelectorAll('.metrics-row .metric-value');
    if (data.total_customers !== undefined && metrics[0]) {
      metrics[0].textContent = data.total_customers.toLocaleString();
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
      const list = Array.isArray(customers) ? customers : [];
      renderLoyaltyCustomers(list);
      renderTierCounts(computeTierCounts(list));
    } catch (e) {
      console.error('fidelizacion: customers error', e);
      renderLoyaltyCustomers(null); // Graceful degradation
    }
  }

  loadLoyaltyStats();
  loadLoyaltyCustomers();
})();
