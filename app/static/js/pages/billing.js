(function () {
  'use strict';

  const token      = localStorage.getItem('rb_token');
  const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
  if (!token) { window.location.href = '/login'; return; }

  const hdr = { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' };

  // Restaurant name shown in sidebar (injected by sidebar.js)

  let PROVIDERS = {};
  let currentProvider = null;
  let existingConfig  = null;

  function toast(msg, type) {
    if (typeof mesioToast === 'function') {
      mesioToast(msg, type === 'err' ? 'error' : 'success');
    } else {
      const el = document.getElementById('toast');
      if (!el) return;
      el.textContent = (type === 'err' ? '❌ ' : '✅ ') + msg;
      el.className = 'toast show ' + (type || 'ok');
      setTimeout(function () { el.classList.remove('show'); }, 4000);
    }
  }

  function switchTab(id, btn) {
    document.querySelectorAll('.tab-section').forEach(function (s) { s.classList.remove('active'); });
    document.querySelectorAll('.seg-btn').forEach(function (b) { b.classList.remove('active'); });
    document.getElementById('tab-' + id).classList.add('active');
    btn.classList.add('active');
    if (id === 'log') loadLog();
  }

  async function init() {
    await Promise.all([loadProviders(), loadCurrentConfig(), loadPlanUsage()]);
  }

  async function loadPlanUsage() {
    try {
      const r = await fetch('/api/subscription/usage', { headers: hdr });
      if (!r.ok) return;
      const d = await r.json();
      const card = document.getElementById('plan-usage-card');
      if (!card) return;
      const planLabel = document.getElementById('plan-usage-name');
      if (planLabel) planLabel.textContent = d.plan ? '· Plan ' + d.plan : '';
      const limits = d.limits || {};
      const today  = (d.usage && d.usage.today)  || {};
      const month  = (d.usage && d.usage.month)  || {};
      _renderUsageRow(card, 'tokens',   today.tokens_used   || 0, limits.daily_tokens);
      _renderUsageRow(card, 'invoices', today.invoices_used || 0, limits.daily_invoices);
      _renderUsageRow(card, 'orders',   month.orders_count  || 0, limits.monthly_orders);
    } catch (e) { console.error('plan usage load failed', e); }
  }

  function _renderUsageRow(card, rowKey, used, limit) {
    const row = card.querySelector('[data-row="' + rowKey + '"]');
    if (!row) return;
    const valEl = row.querySelector('[data-val]');
    const barEl = row.querySelector('[data-bar]');
    if (limit === -1 || limit === undefined || limit === null) {
      valEl.textContent = used.toLocaleString() + ' · Ilimitado';
      barEl.style.width = '0%';
      return;
    }
    valEl.textContent = used.toLocaleString() + ' / ' + Number(limit).toLocaleString();
    const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
    barEl.style.width = pct.toFixed(1) + '%';
    if (pct >= 90)      barEl.style.background = 'var(--m-danger, #d33)';
    else if (pct >= 70) barEl.style.background = 'var(--m-warning, #e67e22)';
    else                barEl.style.background = 'var(--m-brand, #1D9E75)';
  }

  async function loadProviders() {
    try {
      const r = await fetch('/api/billing/providers', { headers: hdr });
      const d = await r.json();
      d.providers.forEach(function (p) { PROVIDERS[p.id] = p; });
    } catch (e) { console.error(e); }
  }

  async function loadCurrentConfig() {
    try {
      const r = await fetch('/api/billing/config', { headers: hdr });
      if (!r.ok) return;
      const d = await r.json();
      if (d.configured && d.config) {
        existingConfig = d.config;
        const prov = d.config.provider;
        currentProvider = prov;
        selectProvider(prov, true);
        showStatusCard(prov, d.config.auto_emit);
        loadMetrics();
      }
    } catch (e) { console.error(e); }
  }

  function showStatusCard(prov, autoEmit) {
    const card = document.getElementById('status-card');
    const pill = document.getElementById('status-pill');
    const text = document.getElementById('status-provider-text');
    card.style.display = 'block';
    pill.innerHTML = '<span class="status-pill pill-ok"><span class="pill-dot"></span> Conectado</span>';
    const names = { siigo: 'Siigo', alegra: 'Alegra', loggro: 'Loggro' };
    const provName = document.createElement('span');
    provName.innerHTML = 'Sistema: <strong>' + _escHtml(names[prov] || prov) + '</strong> \u00b7 Auto-emisión: <strong>' + (autoEmit ? 'Activada' : 'Desactivada') + '</strong>';
    text.textContent = '';
    text.appendChild(provName);
    document.getElementById('metrics-mini').style.display = 'grid';
  }

  async function loadMetrics() {
    try {
      const r = await fetch('/api/billing/log?limit=200', { headers: hdr });
      if (!r.ok) return;
      const d = await r.json();
      const log = d.log || [];
      const ok  = log.filter(function (l) { return l.status === 'success'; }).length;
      const err = log.filter(function (l) { return l.status === 'error'; }).length;
      document.getElementById('m-ok').textContent  = ok;
      document.getElementById('m-err').textContent = err;
      document.getElementById('m-auto').textContent = existingConfig && existingConfig.auto_emit ? 'Activa' : 'Inactiva';
    } catch (e) {}
  }

  function selectProvider(provId, skipHighlight) {
    currentProvider = provId;
    document.querySelectorAll('.provider-card').forEach(function (c) { c.classList.remove('selected'); });
    const el = document.getElementById('card-' + provId);
    if (el) el.classList.add('selected');
    renderFields(provId);
  }

  function renderFields(provId) {
    const prov = PROVIDERS[provId];
    if (!prov) return;

    const card  = document.getElementById('fields-card');
    const title = document.getElementById('fields-card-title');
    const cont  = document.getElementById('fields-container');
    title.textContent = '2 \u00b7 Credenciales ' + prov.name;
    card.style.display = 'block';

    let html = '';
    prov.fields.forEach(function (f) {
      const val    = (existingConfig && existingConfig[f.key] !== undefined) ? existingConfig[f.key] : '';
      const isFull = (f.key.includes('description') || f.key.includes('notes')) ? 'full' : '';
      const isSec  = f.type === 'password' ? 'input-secret' : '';
      const req    = f.required ? '<span class="req">*</span>' : '';

      if (f.type === 'select' && f.options) {
        html += '<div class="form-group ' + isFull + '">' +
          '<label class="form-label">' + _escHtml(f.label) + ' ' + req + '</label>' +
          '<select class="form-select" id="field-' + _escHtml(f.key) + '">' +
          f.options.map(function (o) {
            return '<option value="' + _escHtml(o) + '"' + (val === o ? ' selected' : '') + '>' + _escHtml(o) + '</option>';
          }).join('') +
          '</select></div>';
      } else {
        const dispVal = (f.type === 'password' && val === '***') ? '' : _escHtml(String(val));
        const typeAttr = f.type === 'password' ? 'password' : f.type === 'email' ? 'email' : f.type === 'number' ? 'number' : 'text';
        const stepAttr = f.type === 'number' ? ' step="0.01"' : '';
        html += '<div class="form-group ' + isFull + '">' +
          '<label class="form-label">' + _escHtml(f.label) + ' ' + req + '</label>' +
          '<input class="form-input ' + isSec + '" id="field-' + _escHtml(f.key) + '" type="' + typeAttr + '"' + stepAttr +
          ' value="' + dispVal + '" placeholder="' + (f.type === 'password' ? '••••••••' : '') + '">' +
          '</div>';
      }
    });
    cont.innerHTML = html;

    if (existingConfig && existingConfig.auto_emit !== undefined) {
      document.getElementById('toggle-auto-emit').checked = existingConfig.auto_emit;
    }
  }

  async function saveConfig() {
    if (!currentProvider) { toast('Selecciona un proveedor primero', 'err'); return; }
    const prov   = PROVIDERS[currentProvider];
    const payload = { provider: currentProvider };

    let valid = true;
    prov.fields.forEach(function (f) {
      const el = document.getElementById('field-' + f.key);
      if (!el) return;
      const val = el.value.trim();
      if (f.required && !val) { el.style.borderColor = 'var(--danger)'; valid = false; }
      else { el.style.borderColor = ''; if (val) payload[f.key] = f.type === 'number' ? parseFloat(val) : val; }
    });
    if (!valid) { toast('Completa los campos obligatorios', 'err'); return; }

    payload.auto_emit = document.getElementById('toggle-auto-emit').checked;

    const btn = document.getElementById('btn-save');
    btn.classList.add('btn-loading'); btn.textContent = 'Guardando...';

    try {
      const r = await fetch('/api/billing/config', { method: 'POST', headers: hdr, body: JSON.stringify(payload) });
      if (!r.ok) { const e = await r.json(); throw new Error(e.detail || 'Error'); }
      existingConfig = payload;
      showStatusCard(currentProvider, payload.auto_emit);
      toast('Configuración guardada correctamente');
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      btn.classList.remove('btn-loading'); btn.textContent = '💾 Guardar configuración';
    }
  }

  async function testConnection() {
    const btn1 = document.getElementById('btn-test');
    const btn2 = document.getElementById('btn-test2');
    [btn1, btn2].forEach(function (b) { if (b) { b.classList.add('btn-loading'); b.textContent = 'Probando...'; } });

    try {
      const r = await fetch('/api/billing/test-connection', { method: 'POST', headers: hdr });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'Error de conexión');
      toast('Conexión exitosa con ' + _escHtml(d.provider || 'el proveedor'));
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      if (btn1) { btn1.classList.remove('btn-loading'); btn1.textContent = '🔌 Probar conexión'; }
      if (btn2) { btn2.classList.remove('btn-loading'); btn2.textContent = '🔌 Probar conexión'; }
    }
  }

  async function clearConfig() {
    const confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Desconectar el sistema contable? Se perderá la configuración actual.')
      : confirm('¿Desconectar el sistema contable? Se perderá la configuración actual.');
    if (!confirmed) return;
    try {
      await fetch('/api/billing/config', {
        method: 'POST', headers: hdr,
        body: JSON.stringify({ provider: 'siigo', auto_emit: false, _clear: true })
      });
      existingConfig = null;
      currentProvider = null;
      document.getElementById('status-card').style.display = 'none';
      document.getElementById('metrics-mini').style.display = 'none';
      document.getElementById('fields-card').style.display = 'none';
      document.querySelectorAll('.provider-card').forEach(function (c) { c.classList.remove('selected'); });
      toast('Sistema contable desconectado');
    } catch (e) { toast('Error al desconectar', 'err'); }
  }

  async function emitInvoice() {
    const orderId = document.getElementById('emit-order-id').value.trim();
    if (!orderId) { toast('Ingresa el ID del pedido', 'err'); return; }

    const customer = {};
    const nit   = document.getElementById('emit-customer-nit').value.trim();
    const name  = document.getElementById('emit-customer-name').value.trim();
    const email = document.getElementById('emit-customer-email').value.trim();
    if (nit)   customer.nit   = nit;
    if (name)  customer.name  = name;
    if (email) customer.email = email;

    const btn = document.getElementById('btn-emit');
    btn.classList.add('btn-loading'); btn.textContent = 'Emitiendo...';
    const resultDiv = document.getElementById('emit-result');
    resultDiv.style.display = 'none';

    try {
      const body = { order_id: orderId };
      if (Object.keys(customer).length) body.customer = customer;
      const r = await fetch('/api/billing/emit', { method: 'POST', headers: hdr, body: JSON.stringify(body) });
      const d = await r.json();

      if (!r.ok) throw new Error(d.detail || 'Error al emitir');

      resultDiv.style.display = 'block';
      const wrap = document.createElement('div');
      wrap.style.cssText = 'background:#F0FDF4;border:1px solid #BBF7D0;border-radius:12px;padding:1rem;font-size:.84rem;';
      const heading = document.createElement('div');
      heading.style.cssText = 'font-weight:700;color:#166534;margin-bottom:8px;';
      heading.textContent = '✅ Factura emitida exitosamente';
      const provLine = document.createElement('div');
      const provStrong = document.createElement('strong');
      provStrong.textContent = 'Proveedor:';
      provLine.appendChild(provStrong);
      provLine.appendChild(document.createTextNode(' ' + (d.provider || '')));
      const idLine = document.createElement('div');
      const idStrong = document.createElement('strong');
      idStrong.textContent = 'ID Externo:';
      const idMono = document.createElement('span');
      idMono.className = 'mono';
      idMono.textContent = d.external_id || '—';
      idLine.appendChild(idStrong);
      idLine.appendChild(document.createTextNode(' '));
      idLine.appendChild(idMono);
      const pre = document.createElement('pre');
      pre.style.cssText = 'margin-top:8px;font-size:.72rem;background:#E7F5EA;border-radius:8px;padding:.75rem;overflow:auto;max-height:200px;';
      pre.textContent = JSON.stringify(d.data, null, 2);
      wrap.appendChild(heading);
      wrap.appendChild(provLine);
      wrap.appendChild(idLine);
      wrap.appendChild(pre);
      resultDiv.textContent = '';
      resultDiv.appendChild(wrap);

      toast('Factura emitida: ' + (d.external_id || ''));
      document.getElementById('emit-order-id').value = '';
    } catch (e) {
      resultDiv.style.display = 'block';
      const errDiv = document.createElement('div');
      errDiv.style.cssText = 'background:#FEF2F2;border:1px solid #FECACA;border-radius:12px;padding:1rem;font-size:.84rem;color:#991B1B;';
      const errStrong = document.createElement('strong');
      errStrong.textContent = '❌ Error:';
      errDiv.appendChild(errStrong);
      errDiv.appendChild(document.createTextNode(' ' + e.message));
      resultDiv.textContent = '';
      resultDiv.appendChild(errDiv);
      toast(e.message, 'err');
    } finally {
      btn.classList.remove('btn-loading'); btn.textContent = '📤 Emitir Factura';
    }
  }

  async function loadLog() {
    const tbody = document.getElementById('log-tbody');
    tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state">Cargando...</div></td></tr>';
    try {
      const r = await fetch('/api/billing/log?limit=100', { headers: hdr });
      if (!r.ok) throw new Error('Error');
      const d = await r.json();
      const log = d.log || [];
      if (!log.length) {
        tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state">Sin facturas emitidas aún. Configura tu sistema contable y emite la primera.</div></td></tr>';
        return;
      }
      const provClass = { siigo: 'prov-siigo', alegra: 'prov-alegra', loggro: 'prov-loggro' };
      const provLabel = { siigo: 'Siigo', alegra: 'Alegra', loggro: 'Loggro' };
      const fragment = document.createDocumentFragment();
      log.forEach(function (l) {
        const tr = document.createElement('tr');

        const tdDate = document.createElement('td');
        tdDate.style.cssText = 'color:var(--text-3);font-size:.78rem;white-space:nowrap;';
        tdDate.textContent = (l.created_at || '').substring(0, 16).replace('T', ' ');

        const tdOrder = document.createElement('td');
        tdOrder.className = 'mono';
        tdOrder.textContent = l.order_id || '—';

        const tdProv = document.createElement('td');
        const provSpan = document.createElement('span');
        provSpan.className = 'prov-badge ' + (provClass[l.provider] || '');
        provSpan.textContent = provLabel[l.provider] || l.provider;
        tdProv.appendChild(provSpan);

        const tdStatus = document.createElement('td');
        const statusSpan = document.createElement('span');
        statusSpan.className = l.status === 'success' ? 'badge-success' : l.status === 'error' ? 'badge-error' : 'badge-pending';
        statusSpan.textContent = l.status === 'success' ? 'Exitosa' : l.status === 'error' ? 'Error' : 'Pendiente';
        tdStatus.appendChild(statusSpan);

        const tdExt = document.createElement('td');
        tdExt.className = 'mono';
        tdExt.textContent = l.external_id || '—';

        const tdDetail = document.createElement('td');
        tdDetail.style.cssText = 'max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.75rem;color:var(--text-3);';
        tdDetail.title = l.error_message || '';
        tdDetail.textContent = l.error_message || '—';

        tr.appendChild(tdDate);
        tr.appendChild(tdOrder);
        tr.appendChild(tdProv);
        tr.appendChild(tdStatus);
        tr.appendChild(tdExt);
        tr.appendChild(tdDetail);
        fragment.appendChild(tr);
      });
      tbody.textContent = '';
      tbody.appendChild(fragment);
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state">Error cargando historial</div></td></tr>';
    }
  }

  // Event delegation for data-action buttons
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === 'saveConfig') saveConfig();
    else if (action === 'testConnection') testConnection();
    else if (action === 'clearConfig') clearConfig();
    else if (action === 'emitInvoice') emitInvoice();
    else if (action === 'loadLog') loadLog();
  });

  document.addEventListener('click', function (e) {
    const tabBtn = e.target.closest('.seg-btn[data-tab-target]');
    if (!tabBtn) return;
    const id = tabBtn.dataset.tabTarget;
    if (id) switchTab(id, tabBtn);
  });

  document.querySelectorAll('.provider-card[data-provider]').forEach(function (card) {
    card.addEventListener('click', function () {
      selectProvider(card.dataset.provider);
    });
  });

  init();
})();

/* ══════════════════════════════════════════════════════════════════
   Mi Plan — Subscription dashboard module
   Endpoints consumed:
     GET  /api/billing/plans           (public plan catalog)
     GET  /api/billing/plan            (current plan + addons + auto-recharge)
     GET  /api/billing/usage           (per-dimension cap status)
     POST /api/billing/auto-recharge   (body: {enabled, max_packs})
     POST /api/billing/buy-pack        (manual pack purchase)

   Fallback: if these endpoints 404 (not yet in router), section hides
   gracefully. All fetches are lint-allow'd below because the backend
   router is wired separately (billing_subscription.py).
   ══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ── Dimension display metadata ─────────────────────────────────
  var DIMENSION_META = {
    conversations: { label: 'Conversaciones WhatsApp', unit: 'conv.' },
    audio:         { label: 'Minutos de audio (voz)',  unit: 'min'   },
    storage:       { label: 'Almacenamiento',           unit: 'MB'    },
    staff:         { label: 'Empleados activos',        unit: ''      },
    sku:           { label: 'SKUs en menú',             unit: ''      },
    marketing:     { label: 'Mensajes marketing/mes',   unit: 'msgs'  },
  };

  // Plan catalog used for the "Cambiar plan" modal.
  // Prices in COP, formatted with mesioFmt when available.
  var PLAN_CATALOG = [
    { id: 'pulso',      name: 'Pulso',      price: 149000,  desc: 'Para restaurantes que arrancan. 300 conversaciones/mes, 1 sede.' },
    { id: 'restaurante',name: 'Restaurante',price: 299000,  desc: 'El plan más popular. 1.000 conversaciones, staff y nómina.' },
    { id: 'pro',        name: 'Pro',        price: 599000,  desc: 'Multi-sede, analytics avanzados, catálogo visual, 3.000 conversaciones.' },
    { id: 'cadena',     name: 'Cadena',     price: null,    desc: 'Para grupos con 5+ sedes. Precio a medida. Habla con nosotros.' },
  ];

  var _currentPlan   = null; // plan id string from backend
  var _autoRecharge  = false;
  var _maxPacks      = 5;
  var _planModalTrap = null; // focus trap reference

  // ── Helpers ────────────────────────────────────────────────────

  function _fmt(n) {
    if (typeof mesioFmt === 'function') return mesioFmt(n);
    return '$' + Number(n).toLocaleString('es-CO');
  }

  function _showEl(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = '';
  }

  function _hideEl(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }

  function _setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function _formatNextRenewal(periodStartIso) {
    if (!periodStartIso) return '';
    try {
      var start = new Date(periodStartIso);
      var next  = new Date(start);
      next.setDate(next.getDate() + 30);
      var months = ['enero','febrero','marzo','abril','mayo','junio',
                    'julio','agosto','septiembre','octubre','noviembre','diciembre'];
      return 'Proximo cobro: ' + next.getDate() + ' de ' + months[next.getMonth()] + ' de ' + next.getFullYear();
    } catch (e) { return ''; }
  }

  // ── Gauge renderer ─────────────────────────────────────────────

  function renderGauge(dimension, used, cap, status) {
    // status: 'ok' | 'warn50' | 'warn80' | 'warn90' | 'exceeded' | 'unlimited'
    var meta  = DIMENSION_META[dimension] || { label: _escHtml(dimension), unit: '' };
    var pct   = 0;
    var numTxt = '';
    var colorClass = 'is-ok';

    if (status === 'unlimited' || cap === -1 || cap === null || cap === undefined) {
      numTxt     = Number(used).toLocaleString() + (meta.unit ? ' ' + meta.unit : '') + ' · Ilimitado';
      colorClass = 'is-ok';
    } else {
      pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
      numTxt = Number(used).toLocaleString() + ' / ' + Number(cap).toLocaleString();
      if (meta.unit) numTxt += ' ' + meta.unit;
      if      (status === 'exceeded') colorClass = 'is-exceeded';
      else if (status === 'warn90')   colorClass = 'is-warn90';
      else if (status === 'warn80')   colorClass = 'is-warn80';
      else if (status === 'warn50')   colorClass = 'is-warn50';
      else                            colorClass = 'is-ok';
    }

    var el = document.createElement('div');
    el.className = 'm-gauge';
    el.setAttribute('role', 'meter');
    el.setAttribute('aria-valuenow', String(Math.round(pct)));
    el.setAttribute('aria-valuemin', '0');
    el.setAttribute('aria-valuemax', '100');
    el.setAttribute('aria-label', _escHtml(meta.label));

    var header = document.createElement('div');
    header.className = 'm-gauge-header';

    var labelEl = document.createElement('span');
    labelEl.className = 'm-gauge-label';
    labelEl.textContent = meta.label;

    var numsEl = document.createElement('span');
    numsEl.className = 'm-gauge-numbers';
    numsEl.textContent = numTxt;

    header.appendChild(labelEl);
    header.appendChild(numsEl);
    el.appendChild(header);

    var barEl = document.createElement('div');
    barEl.className = 'm-gauge-bar';
    var fillEl = document.createElement('div');
    fillEl.className = 'm-gauge-fill ' + colorClass;

    // Animate width after paint
    fillEl.style.width = '0%';
    barEl.appendChild(fillEl);
    el.appendChild(barEl);

    // Contextual sub-text
    if (status === 'exceeded') {
      var sub = document.createElement('div');
      sub.className = 'm-gauge-subtext exceeded';
      sub.textContent = 'Excedido — el bot esta redirigiendo a un humano.';
      el.appendChild(sub);
    } else if (status === 'warn90' || status === 'warn80') {
      var remaining = cap - used;
      var sub2 = document.createElement('div');
      sub2.className = 'm-gauge-subtext';
      sub2.textContent = 'Te quedan ' + Number(remaining).toLocaleString() + (meta.unit ? ' ' + meta.unit : '') + '. Activa la auto-recarga para evitar interrupciones.';
      el.appendChild(sub2);
    } else if (status === 'unlimited') {
      var unlimEl = document.createElement('div');
      unlimEl.className = 'm-gauge-unlimited';
      unlimEl.textContent = 'Sin limite en este plan';
      el.appendChild(unlimEl);
    }

    // Trigger CSS transition after element is in DOM
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        fillEl.style.width = (status === 'unlimited' ? '0' : pct.toFixed(1)) + '%';
      });
    });

    return el;
  }

  // ── Render plan card ───────────────────────────────────────────

  function renderPlanCard(planData) {
    _currentPlan = planData.plan_id || planData.plan || null;

    // Plan name
    var nameMap = { pulso: 'Pulso', restaurante: 'Restaurante', pro: 'Pro', cadena: 'Cadena' };
    var displayName = nameMap[_currentPlan] || (_currentPlan ? _currentPlan.charAt(0).toUpperCase() + _currentPlan.slice(1) : 'Plan activo');
    _setText('plan-name-display', displayName);

    // Price
    var price = planData.price_monthly;
    var priceEl = document.getElementById('plan-price-display');
    if (priceEl) {
      priceEl.textContent = (price != null && price !== 0) ? _fmt(price) + '/mes' : 'Precio a medida';
    }

    // Renewal date
    _setText('plan-renewal-display', _formatNextRenewal(planData.current_period_start));

    // Add-ons
    var addonsEl = document.getElementById('plan-addons-display');
    if (addonsEl) {
      addonsEl.textContent = '';
      var addons = planData.active_addons || [];
      if (addons.length === 0) {
        addonsEl.textContent = '';
      } else {
        addons.forEach(function (name) {
          var chip = document.createElement('span');
          chip.className = 'plan-addon-chip';
          chip.textContent = _escHtml(name);
          addonsEl.appendChild(chip);
        });
      }
    }

    // Auto-recharge state
    var ar = planData.auto_recharge || {};
    _autoRecharge = !!ar.enabled;
    _maxPacks     = ar.max_packs || 5;
    _syncAutoRechargeUI();
  }

  // ── Auto-recharge UI sync ──────────────────────────────────────

  function _syncAutoRechargeUI() {
    var toggle = document.getElementById('toggle-autorecharge');
    if (toggle) {
      toggle.checked = _autoRecharge;
      toggle.setAttribute('aria-checked', _autoRecharge ? 'true' : 'false');
    }
    var maxInput = document.getElementById('input-max-packs');
    if (maxInput) maxInput.value = _maxPacks;

    var maxRow   = document.getElementById('autorecharge-maxpacks-row');
    var warning  = document.getElementById('autorecharge-off-warning');
    if (maxRow)  maxRow.style.display   = _autoRecharge ? '' : 'none';
    if (warning) warning.style.display  = _autoRecharge ? 'none' : '';
  }

  // ── Render usage gauges ────────────────────────────────────────

  function renderGauges(usageData) {
    var container = document.getElementById('mi-plan-gauges');
    if (!container) return;
    container.textContent = '';

    var dims = usageData.dimensions || {};
    var order = ['conversations', 'audio', 'storage', 'staff', 'sku', 'marketing'];

    order.forEach(function (dim) {
      var d = dims[dim];
      if (!d) return;
      var gauge = renderGauge(dim, d.used || 0, d.cap, d.status || 'ok');
      container.appendChild(gauge);
    });

    if (!container.children.length) {
      var empty = document.createElement('div');
      empty.style.cssText = 'color:var(--text-3);font-size:13px;padding:8px 0;';
      empty.textContent = 'Sin datos de uso disponibles para este periodo.';
      container.appendChild(empty);
    }
  }

  // ── Plan modal ─────────────────────────────────────────────────

  function openPlanModal() {
    var grid = document.getElementById('modal-plans-grid');
    if (grid) {
      grid.textContent = '';
      PLAN_CATALOG.forEach(function (plan) {
        var card = document.createElement('div');
        card.className = 'plan-option-card' + (plan.id === _currentPlan ? ' is-current' : '');
        card.setAttribute('role', plan.id === _currentPlan ? 'presentation' : 'button');
        card.setAttribute('tabindex', plan.id === _currentPlan ? '-1' : '0');

        var nameEl = document.createElement('div');
        nameEl.className = 'plan-option-name';
        nameEl.textContent = plan.name;

        var priceEl2 = document.createElement('div');
        priceEl2.className = 'plan-option-price';
        priceEl2.textContent = plan.price ? _fmt(plan.price) + '/mes +IVA' : 'Precio a medida';

        var descEl = document.createElement('div');
        descEl.className = 'plan-option-desc';
        descEl.textContent = plan.desc;

        card.appendChild(nameEl);
        card.appendChild(priceEl2);
        card.appendChild(descEl);

        if (plan.id === _currentPlan) {
          var badge = document.createElement('div');
          badge.className = 'plan-option-current-badge';
          badge.textContent = 'Plan actual';
          card.appendChild(badge);
        }

        grid.appendChild(card);
      });
    }

    var overlay = document.getElementById('modal-change-plan');
    if (overlay) {
      overlay.classList.add('open');
      if (typeof mesioFocusTrap === 'function') {
        var box = overlay.querySelector('.m-modal-box');
        _planModalTrap = mesioFocusTrap(box, {
          onEscape: closePlanModal,
          labelledBy: 'modal-plan-title',
        });
      }
    }
  }

  function closePlanModal() {
    var overlay = document.getElementById('modal-change-plan');
    if (overlay) overlay.classList.remove('open');
    if (_planModalTrap && typeof _planModalTrap.deactivate === 'function') {
      _planModalTrap.deactivate();
      _planModalTrap = null;
    }
    var btn = document.getElementById('btn-change-plan');
    if (btn) btn.focus();
  }

  // ── API calls ──────────────────────────────────────────────────

  async function saveAutoRecharge() {
    var toggle = document.getElementById('toggle-autorecharge');
    var maxInput = document.getElementById('input-max-packs');
    var enabled  = toggle ? toggle.checked : false;
    var maxPacks = maxInput ? Math.max(1, Math.min(5, parseInt(maxInput.value, 10) || 5)) : 5;

    var btn = document.getElementById('btn-save-autorecharge');
    if (btn) { btn.disabled = true; btn.textContent = 'Guardando...'; }

    try {
      var r = await fetch('/api/billing/auto-recharge', { // lint-allow: subscription billing endpoint — wired in billing_subscription.py
        method: 'POST',
        headers: mesioHeaders(),
        body: JSON.stringify({ enabled: enabled, max_packs: maxPacks }),
      });
      mesioTrackFetch(r.ok);
      if (!r.ok) {
        var err = await r.json().catch(function () { return {}; });
        throw new Error(err.detail || 'Error al guardar');
      }
      _autoRecharge = enabled;
      _maxPacks     = maxPacks;
      _syncAutoRechargeUI();
      mesioToast('Auto-recarga actualizada', 'success');
    } catch (e) {
      mesioToast(e.message || 'Error al guardar', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Guardar'; }
    }
  }

  async function buyPack() {
    var confirmed = await mesioConfirm(
      'Comprar 1 pack de 100 conversaciones extra por $50.000 COP (+IVA). Se cobrará a tu método de pago configurado.',
      { confirmText: 'Comprar', danger: false }
    );
    if (!confirmed) return;

    var btn = document.getElementById('btn-buy-pack');
    if (btn) { btn.disabled = true; btn.textContent = 'Procesando...'; }

    try {
      // Pago real via Wompi pendiente — backend stub crea pack sin cobrar // lint-allow: subscription billing endpoint — wired in billing_subscription.py
      var r = await fetch('/api/billing/buy-pack', { // lint-allow: subscription billing endpoint — wired in billing_subscription.py
        method: 'POST',
        headers: mesioHeaders(),
        body: JSON.stringify({}),
      });
      mesioTrackFetch(r.ok);
      if (!r.ok) {
        var err2 = await r.json().catch(function () { return {}; });
        throw new Error(err2.detail || 'Error al comprar pack');
      }
      var d = await r.json();
      mesioToast('Pack comprado: +100 conversaciones. Total packs este mes: ' + (d.packs_this_period || '—'), 'success');
      // Reload usage to reflect new cap
      await loadUsage();
      _updatePacksDisplay(d.packs_this_period);
    } catch (e) {
      mesioToast(e.message || 'Error al comprar pack', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Comprar 100 conversaciones extra ($50.000 +IVA)'; }
    }
  }

  function _updatePacksDisplay(count) {
    var el = document.getElementById('packs-count-display');
    if (!el) return;
    if (count != null && count > 0) {
      el.textContent = 'Packs comprados este periodo: ' + count;
    } else {
      el.textContent = '';
    }
  }

  // ── Data fetchers ──────────────────────────────────────────────

  async function loadPlan() {
    try {
      var r = await fetch('/api/billing/plan', { headers: mesioHeaders() }); // lint-allow: subscription billing endpoint — wired in billing_subscription.py
      mesioTrackFetch(r.ok);
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  async function loadUsage() {
    try {
      var r = await fetch('/api/billing/usage', { headers: mesioHeaders() }); // lint-allow: subscription billing endpoint — wired in billing_subscription.py
      mesioTrackFetch(r.ok);
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  // ── loadMyPlan — main entry point ──────────────────────────────

  async function loadMyPlan() {
    var skeleton = document.getElementById('mi-plan-loading');
    if (skeleton) skeleton.classList.add('m-skeleton');

    try {
      var results = await Promise.all([loadPlan(), loadUsage()]);
      var planData  = results[0];
      var usageData = results[1];

      if (skeleton) { skeleton.classList.remove('m-skeleton'); skeleton.style.display = 'none'; }

      if (planData) {
        renderPlanCard(planData);
        document.getElementById('plan-current-card').style.display = '';
        document.getElementById('plan-autorecharge-card').style.display = '';
        document.getElementById('plan-buypack-card').style.display = '';
        _updatePacksDisplay(planData.packs_this_period);
      }

      if (usageData) {
        renderGauges(usageData);
        document.getElementById('plan-usage-section-card').style.display = '';
      }
    } catch (e) {
      if (skeleton) { skeleton.classList.remove('m-skeleton'); skeleton.style.display = 'none'; }
      // Fail gracefully — section stays hidden, DIAN content unaffected
    }
  }

  // ── Event wiring ───────────────────────────────────────────────

  var btnChange = document.getElementById('btn-change-plan');
  if (btnChange) btnChange.addEventListener('click', openPlanModal);

  var btnCloseModal = document.getElementById('btn-close-plan-modal');
  if (btnCloseModal) btnCloseModal.addEventListener('click', closePlanModal);

  var overlay = document.getElementById('modal-change-plan');
  if (overlay) {
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closePlanModal();
    });
  }

  var toggleAR = document.getElementById('toggle-autorecharge');
  if (toggleAR) {
    toggleAR.addEventListener('change', function () {
      _autoRecharge = toggleAR.checked;
      toggleAR.setAttribute('aria-checked', _autoRecharge ? 'true' : 'false');
      _syncAutoRechargeUI();
    });
  }

  var btnSaveAR = document.getElementById('btn-save-autorecharge');
  if (btnSaveAR) btnSaveAR.addEventListener('click', saveAutoRecharge);

  var btnBuyPack = document.getElementById('btn-buy-pack');
  if (btnBuyPack) btnBuyPack.addEventListener('click', buyPack);

  // Bootstrap
  loadMyPlan();
})();
