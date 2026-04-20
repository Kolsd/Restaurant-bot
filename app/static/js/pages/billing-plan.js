/* ══ Mesio — Billing Plan Page (redesign 2026) ═══════════════════════
   Admin-only. Requires Bearer token (rb_token).
   Endpoints used:
     GET /api/restaurant          → restaurant info incl. subscription_plan
     GET /api/billing/log?limit=10 → recent invoices (if exists)
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  var token      = localStorage.getItem('rb_token');
  var restaurant = null;
  try { restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || 'null'); } catch (e) {}

  if (!token) {
    window.location.href = '/login';
    return;
  }

  var hdr = { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' };

  // ── PLAN DATA (hardcoded per business decision) ─────────────────────
  var PLANS = {
    starter: {
      name: 'Starter',
      price: '$120.000',
      priceLabel: '/mes · por sucursal',
      features: [
        { text: 'POS + KDS básico', active: true },
        { text: 'Hasta 1.000 pedidos/mes', active: true },
        { text: 'WhatsApp AI hasta 500 msg', active: true },
        { text: '1 sucursal incluida', active: true },
        { text: 'Biometría FIDO2', active: false },
        { text: 'API completa', active: false },
      ],
      actionLabel: 'Bajar a Starter',
      actionClass: 'btn',
    },
    pro: {
      name: 'Pro',
      price: '$160.000',
      priceLabel: '/mes · por sucursal',
      features: [
        { text: 'Todo de Starter +', active: true },
        { text: 'Pedidos ilimitados', active: true },
        { text: 'WhatsApp AI 10k msg · multi-número', active: true },
        { text: 'Biometría + nómina DIAN', active: true },
        { text: 'KDS + inventario (escandallos)', active: true },
        { text: 'API + webhooks', active: true },
      ],
      actionLabel: 'Tu plan actual',
      actionClass: 'btn brand',
    },
    enterprise: {
      name: 'Enterprise',
      price: 'Desde $380k',
      priceLabel: '/mes',
      features: [
        { text: 'Todo de Pro +', active: true },
        { text: 'SSO SAML + auditoría', active: true },
        { text: 'WhatsApp AI ilimitado', active: true },
        { text: 'Soporte 24/7 con SLA', active: true },
        { text: 'Onboarding dedicado', active: true },
        { text: 'Contrato anual personalizado', active: true },
      ],
      actionLabel: 'Hablar con ventas →',
      actionClass: 'btn primary',
    },
  };

  // ── Bootstrap ────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    populateNav();
    renderPlanCards(currentPlanKey());
    loadRestaurant();
    loadInvoices();
    wireButtons();
  });

  function currentPlanKey() {
    if (!restaurant) return 'starter';
    var plan = (restaurant.subscription_plan || '').toLowerCase();
    if (plan.includes('enterprise')) return 'enterprise';
    if (plan.includes('pro'))        return 'pro';
    return 'starter';
  }

  // ── Nav / user info ──────────────────────────────────────────────────
  function populateNav() {
    if (!restaurant) return;
    var nameEls = document.querySelectorAll('[data-rest-name]');
    nameEls.forEach(function (el) { el.textContent = restaurant.name || ''; });
    var planEl = document.getElementById('nav-plan-label');
    if (planEl) planEl.textContent = restaurant.subscription_plan || 'Starter';
  }

  // ── Load restaurant data ─────────────────────────────────────────────
  function loadRestaurant() {
    fetch('/api/restaurant', { headers: hdr })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        restaurant = data;
        // Update usage strip
        updateUsage(data);
        // Refresh plan current marker
        renderPlanCards(currentPlanKey());
      })
      .catch(function (e) { console.warn('billing-plan.js: /api/restaurant failed', e); });
  }

  function updateUsage(data) {
    // Populate placeholders if elements exist
    var features = data.features || {};
    safe('bp-plan-name', data.subscription_plan || 'Starter');

    // WhatsApp usage bar (if we have conversation count)
    var aiLimit = 10000;
    var aiUsed  = data.conversations_this_month || 0;
    var aiPct   = Math.min(100, Math.round((aiUsed / aiLimit) * 100));
    var aiFill  = document.getElementById('bp-ai-bar');
    if (aiFill) {
      aiFill.style.width = aiPct + '%';
      if (aiPct >= 80) aiFill.classList.add('warn');
    }
    safeNum('bp-ai-val', aiUsed >= 1000 ? (aiUsed / 1000).toFixed(1) + 'k' : String(aiUsed));

    // Orders
    safeNum('bp-orders-val', data.orders_this_month || '—');
  }

  function safe(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }
  function safeNum(id, val) { safe(id, val); }

  // ── Invoices table ────────────────────────────────────────────────────
  function loadInvoices() {
    var tbody = document.getElementById('bp-invoice-tbody');
    if (!tbody) return;

    fetch('/api/billing/log?limit=10', { headers: hdr })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.log || data.log.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-3);padding:20px;font-size:13px;">Sin facturas aún.</td></tr>';
          return;
        }
        tbody.innerHTML = data.log.map(function (l) {
          var date = (l.created_at || '').substring(0, 10);
          var statusBadge = l.status === 'success'
            ? '<span class="badge success">Pagada</span>'
            : '<span class="badge warn">Pendiente</span>';
          return '<tr>' +
            '<td>' + _escHtml(date) + '</td>' +
            '<td><span class="invoice-num">' + _escHtml(l.external_id || '—') + '</span></td>' +
            '<td>' + _escHtml(l.order_id || '—') + '</td>' +
            '<td class="num mono">' + _escHtml(l.amount ? '$' + Number(l.amount).toLocaleString('es-CO') : '—') + '</td>' +
            '<td>' + statusBadge + '</td>' +
            '</tr>';
        }).join('');
      })
      .catch(function () {
        if (tbody) tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-3);padding:20px;font-size:13px;">(datos próximamente)</td></tr>';
      });
  }

  // ── Plan cards ────────────────────────────────────────────────────────
  function renderPlanCards(activePlan) {
    var container = document.getElementById('bp-plans-grid');
    if (!container) return;
    container.innerHTML = '';

    Object.keys(PLANS).forEach(function (key) {
      var plan    = PLANS[key];
      var isCurrent = key === activePlan;
      var card    = document.createElement('div');
      card.className = 'plan-card' + (isCurrent ? ' current' : '');

      var nameEl = document.createElement('div');
      nameEl.className = 'pc-name';
      nameEl.textContent = plan.name;

      var priceEl = document.createElement('div');
      priceEl.className = 'pc-price';
      var priceNum = document.createElement('span');
      priceNum.textContent = plan.price;
      var priceUnit = document.createElement('span');
      priceUnit.className = 'u';
      priceUnit.textContent = ' ' + plan.priceLabel;
      priceEl.appendChild(priceNum);
      priceEl.appendChild(priceUnit);

      var list = document.createElement('ul');
      list.className = 'pc-list';
      plan.features.forEach(function (f) {
        var li = document.createElement('li');
        if (!f.active) li.className = 'off';
        li.textContent = f.text;
        list.appendChild(li);
      });

      var actionBtn = document.createElement('button');
      actionBtn.className = plan.actionClass + ' pc-action';
      actionBtn.type = 'button';
      actionBtn.textContent = isCurrent ? 'Tu plan actual' : plan.actionLabel;
      if (isCurrent) actionBtn.disabled = true;
      else {
        actionBtn.addEventListener('click', function () {
          handlePlanAction(key, plan);
        });
      }

      card.appendChild(nameEl);
      card.appendChild(priceEl);
      card.appendChild(list);
      card.appendChild(actionBtn);
      container.appendChild(card);
    });
  }

  function handlePlanAction(planKey, plan) {
    if (planKey === 'enterprise') {
      window.open('mailto:sales@mesio.co?subject=Enterprise%20plan', '_blank');
      return;
    }
    if (typeof mesioToast === 'function') {
      mesioToast('Para cambiar de plan contacta a soporte@mesio.co', 'info', 5000);
    }
  }

  // ── Wire buttons ──────────────────────────────────────────────────────
  function wireButtons() {
    var logoutBtn = document.getElementById('bp-logout');
    if (logoutBtn) {
      logoutBtn.addEventListener('click', function () {
        if (typeof mesioLogout === 'function') mesioLogout();
        else window.location.href = '/login';
      });
    }

    var addPmBtn = document.getElementById('bp-add-pm');
    if (addPmBtn) {
      addPmBtn.addEventListener('click', function () {
        if (typeof mesioToast === 'function') mesioToast('Gestión de métodos de pago próximamente', 'info');
      });
    }

    var editBillingBtn = document.getElementById('bp-edit-billing');
    if (editBillingBtn) {
      editBillingBtn.addEventListener('click', function () {
        window.location.href = '/billing';
      });
    }

    var portalBtn = document.getElementById('bp-portal-btn');
    if (portalBtn) {
      portalBtn.addEventListener('click', function () {
        if (typeof mesioToast === 'function') mesioToast('Portal de cliente próximamente', 'info');
      });
    }
  }

  // ── XSS guard (local, mesio-utils also provides _escHtml) ─────────────
  function _escHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

}());
