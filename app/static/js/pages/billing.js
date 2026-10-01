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
    await Promise.all([loadProviders(), loadCurrentConfig()]);
  }

  async function loadProviders() {
    try {
      const r = await fetch('/api/billing/providers', { headers: hdr });
      const d = await r.json();
      d.providers.forEach(function (p) { PROVIDERS[p.id] = p; });
    } catch (e) { console.error(e); mesioToast('No se pudo cargar la información de plan', 'error'); }
  }

  async function loadCurrentConfig() {
    try {
      const r = await fetch('/api/billing/config', { headers: hdr });
      if (!r.ok) return;
      const d = await r.json();
      if (d.dian_in_plan === false) {
        // Electronic invoicing is a Pro feature: say so instead of a setup
        // wizard whose every save would be refused.
        document.querySelectorAll('.billing-tabs, .tab-section').forEach(function (el) { el.style.display = 'none'; });
        const title = [...document.querySelectorAll('.page-head .page-title')]
          .find(function (h) { return h.textContent.trim() === 'Facturación Contable'; });
        const head = title && title.closest('.page-head');
        if (head && !document.getElementById('dian-locked-note')) {
          const note = document.createElement('div');
          note.id = 'dian-locked-note';
          note.className = 'card';
          note.style.cssText = 'padding:16px 18px;margin-top:12px;font-size:13px;color:var(--text-2);';
          note.textContent = 'La factura electrónica DIAN está en el plan Pro. Escríbenos por soporte si quieres activarla.';
          head.insertAdjacentElement('afterend', note);
        }
        return;
      }
      if (d.configured && d.config) {
        existingConfig = d.config;
        const prov = d.config.provider;
        currentProvider = prov;
        selectProvider(prov, true);
        showStatusCard(prov, d.config.auto_emit);
        loadMetrics();
      }
    } catch (e) { console.error(e); mesioToast('No se pudo cargar la información de plan', 'error'); }
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
    } catch (e) { console.error(e); mesioToast('No se pudo cargar la información de plan', 'error'); }
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
     GET  /api/billing/plan            (current plan + addons)
     GET  /api/billing/usage           (per-dimension cap status)

   Fallback: if these endpoints 404 (not yet in router), section hides
   gracefully. All fetches are lint-allow'd below because the backend
   router is wired separately (billing_subscription.py).
   ══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ── Dimension display metadata ─────────────────────────────────
  var DIMENSION_META = {
    conversations: { label: 'Conversaciones del bot', unit: 'conv.' },
    audio:         { label: 'Minutos de audio (voz)',  unit: 'min'   },
    storage:       { label: 'Almacenamiento',           unit: 'MB'    },
    staff:         { label: 'Empleados activos',        unit: ''      },
    sku:           { label: 'SKUs en menú',             unit: ''      },
    marketing:     { label: 'Mensajes marketing/mes',   unit: 'msgs'  },
  };

  // Plan catalog used for the "Cambiar plan" modal — mirrors
  // app/services/plans.py and the landing's #precios. Prices per sede.
  var PLAN_CATALOG = [
    { id: 'esencial',    name: 'Esencial',    price: 119000, desc: 'Carta QR y pedidos desde la mesa, cocina, caja y panel de ventas. Hasta 5 usuarios.' },
    { id: 'restaurante', name: 'Restaurante', price: 249000, desc: 'Todo lo de Esencial + asistente con IA, domicilios y recogida por tu link, usuarios ilimitados.' },
    { id: 'pro',         name: 'Pro',         price: 349000, desc: 'Todo lo de Restaurante + reservas, inventario por pedido y facturación DIAN (folios aparte).' },
    { id: 'cadena',      name: 'Cadena',      price: 299000, desc: 'Desde 3 sedes: todo lo de Pro + panel de todas tus sedes y traslados de inventario.' },
  ];
  var PLAN_NAMES = { esencial: 'Esencial', restaurante: 'Restaurante', pro: 'Pro', cadena: 'Cadena' };

  var _currentPlan   = null; // plan id string from backend
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

  var MONTHS = ['enero','febrero','marzo','abril','mayo','junio',
                'julio','agosto','septiembre','octubre','noviembre','diciembre'];

  function _longDate(d) {
    return d.getDate() + ' de ' + MONTHS[d.getMonth()] + ' de ' + d.getFullYear();
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
      sub.textContent = 'Superaste lo previsto en tu plan. El bot sigue atendiendo con normalidad.';
      el.appendChild(sub);
    } else if (status === 'warn90' || status === 'warn80') {
      var remaining = cap - used;
      var sub2 = document.createElement('div');
      sub2.className = 'm-gauge-subtext';
      sub2.textContent = 'Te quedan ' + Number(remaining).toLocaleString() + (meta.unit ? ' ' + meta.unit : '') + ' este período.';
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
    _currentPlan = planData.plan_code || null;
    _setText('plan-name-display', planData.plan_name || PLAN_NAMES[_currentPlan] || 'Plan activo');

    // Price is per sede; the total multiplies by the active sedes.
    var perSede = planData.monthly_price_cop;
    var sedes = planData.sedes || 1;
    var priceTxt = perSede != null ? _fmt(perSede) + ' por sede al mes' : '';
    if (sedes > 1 && planData.monthly_total_cop != null) {
      priceTxt += ' · ' + sedes + ' sedes = ' + _fmt(planData.monthly_total_cop) + ' al mes';
    }
    _setText('plan-price-display', priceTxt);
    _setText('plan-founder-display', planData.founder
      ? 'Precio fundador: congelado de por vida mientras mantengas tu suscripción (lista: ' + _fmt(planData.list_price_cop) + ').'
      : '');

    // Subscription state (app/services/plans.billing_status): free days,
    // paid period, overdue in its grace days, or paused.
    var status = planData.billing_status;
    var renewal = '';
    if (status === 'trial' && planData.comp_until) {
      renewal = 'Prueba gratis hasta el ' + _longDate(new Date(planData.comp_until)) + '.';
      if (planData.effective_plan && planData.effective_plan !== _currentPlan) {
        renewal += ' Mientras tanto tienes todo el plan ' + (PLAN_NAMES[planData.effective_plan] || '') + '.';
      }
    } else if (status === 'suspendido') {
      renewal = 'Tu cuenta está pausada: tus clientes no pueden pedir por QR ni por tu link. ' +
        'Escríbenos a soporte para activar tu plan.';
    } else if (status === 'vencido' && planData.pauses_on) {
      renewal = 'Tu pago está pendiente. Si no lo recibimos, la cuenta se pausa el ' +
        _longDate(new Date(planData.pauses_on)) + '.';
    } else if (planData.paid_until) {
      renewal = 'Pagado hasta el ' + _longDate(new Date(planData.paid_until)) + '.';
    }
    _setText('plan-renewal-display', renewal);

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
  }

  // ── Render usage gauges ────────────────────────────────────────

  function renderGauges(usageData) {
    var container = document.getElementById('mi-plan-gauges');
    if (!container) return;
    container.textContent = '';

    var dims = usageData.dimensions || {};
    // Only staff users are a limit the owner has (Esencial: 5). The
    // conversation allowance is an internal ceiling that alerts Mesio.
    var order = ['staff'];

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
        priceEl2.textContent = _fmt(plan.price) + ' por sede al mes';

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

  // Bootstrap
  loadMyPlan();
})();

/* ══════════════════════════════════════════════════════════════════
   Plan Downgrade module
   Endpoints consumed:
     GET  /api/billing/plan-status       (current plan + pending downgrade + sucursales)
     GET  /api/billing/plan-options      (list of smaller plans for dropdown)
     POST /api/billing/request-downgrade (body: {new_plan_code, kept_location_id})
     POST /api/billing/cancel-downgrade  (no body)
   ══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var _planStatus     = null;   // loaded from GET /api/billing/plan-status
  var _planOptions    = null;   // loaded from GET /api/billing/plan-options
  var _selectedLocId  = null;   // currently selected kept_location_id

  // ── Plan order for "is this a downgrade?" check ────────────────
  var PLAN_ORDER = ['esencial', 'restaurante', 'pro', 'cadena'];

  function _planRank(code) {
    var idx = PLAN_ORDER.indexOf((code || '').toLowerCase());
    return idx === -1 ? 999 : idx;
  }

  // ── Date formatter ─────────────────────────────────────────────
  function _fmtDate(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso);
      var months = ['enero','febrero','marzo','abril','mayo','junio',
                    'julio','agosto','septiembre','octubre','noviembre','diciembre'];
      return d.getDate() + ' de ' + months[d.getMonth()] + ' de ' + d.getFullYear();
    } catch (e) { return iso.substring(0, 10); }
  }

  // ── Render pending downgrade banner ───────────────────────────
  function _renderBanner(status) {
    var banner = document.getElementById('plan-downgrade-banner');
    if (!banner) return;
    if (!status || !status.pending_plan) {
      banner.style.display = 'none';
      return;
    }
    var nameMap = { esencial: 'Esencial', restaurante: 'Restaurante', pro: 'Pro', cadena: 'Cadena' };
    var planName = nameMap[status.pending_plan] || status.pending_plan;
    var keptName = '';
    if (status.kept_location_id && status.current_sucursales) {
      var found = status.current_sucursales.find(function (s) { return s.id === status.kept_location_id; });
      if (found) keptName = found.name;
    }
    var dateStr = _fmtDate(status.effective_at);
    var txt = 'Tu plan bajara a ' + planName + ' el ' + dateStr + '.';
    if (keptName) txt += ' Solo quedara activa la sucursal "' + keptName + '".';
    var textEl = document.getElementById('downgrade-banner-text');
    if (textEl) textEl.textContent = txt;
    banner.style.display = '';
  }

  // ── Render downgrade form ──────────────────────────────────────
  function _renderDowngradeForm(status, options) {
    var card = document.getElementById('plan-downgrade-card');
    if (!card) return;

    // Only show if there's no pending downgrade already
    if (status && status.pending_plan) {
      card.style.display = 'none';
      return;
    }

    var select = document.getElementById('select-downgrade-plan');
    if (!select) return;

    var currentPlan = status ? (status.current_plan || '') : '';
    var currentRank = _planRank(currentPlan);

    // Populate only plans that are strictly smaller than current
    select.textContent = '';
    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '— Selecciona un plan —';
    select.appendChild(placeholder);

    var hasOptions = false;
    if (options && options.plans) {
      options.plans.forEach(function (p) {
        if (_planRank(p.plan_code) < currentRank) {
          var opt = document.createElement('option');
          opt.value = p.plan_code;
          var priceStr = p.monthly_price_cop ? ' — $' + Number(p.monthly_price_cop).toLocaleString('es-CO') + '/mes' : '';
          opt.textContent = (p.display_name || p.plan_code) + priceStr;
          select.appendChild(opt);
          hasOptions = true;
        }
      });
    }

    // Show the card only when there are downgradeable plans
    card.style.display = hasOptions ? '' : 'none';
    _onPlanSelectChange(status);
  }

  // ── Handle plan select change: show/hide location picker ──────
  function _onPlanSelectChange(status) {
    var select = document.getElementById('select-downgrade-plan');
    var picker  = document.getElementById('downgrade-location-picker');
    var list    = document.getElementById('downgrade-location-list');
    var btn     = document.getElementById('btn-submit-downgrade');
    if (!select || !picker || !list || !btn) return;

    var newPlan = select.value;
    btn.disabled = !newPlan;
    _selectedLocId = null;

    if (!newPlan || !status) {
      picker.style.display = 'none';
      return;
    }

    // Every plan is priced per sede, so no plan limits how many sedes stay.
    var newLimit = null;
    var branches = status.current_sucursales || [];

    if (newLimit !== null && branches.length > newLimit) {
      // Need to pick which one to keep
      list.textContent = '';
      branches.forEach(function (s) {
        var label = document.createElement('label');
        label.style.cssText = 'display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;';
        var radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'kept-location';
        radio.value = String(s.id);
        radio.addEventListener('change', function () {
          _selectedLocId = s.id;
          btn.disabled = false;
        });
        var span = document.createElement('span');
        span.textContent = s.name;
        label.appendChild(radio);
        label.appendChild(span);
        list.appendChild(label);
      });
      picker.style.display = '';
      btn.disabled = true; // require location selection
    } else {
      // Single location or plan allows all current locations — auto-pick first
      picker.style.display = 'none';
      _selectedLocId = branches.length > 0 ? branches[0].id : null;
      btn.disabled = !newPlan;
    }
  }

  // ── Submit downgrade ──────────────────────────────────────────
  async function _submitDowngrade() {
    var select = document.getElementById('select-downgrade-plan');
    var btn    = document.getElementById('btn-submit-downgrade');
    if (!select || !btn) return;

    var newPlan = select.value;
    if (!newPlan) { mesioToast('Selecciona un plan', 'error'); return; }

    var keptId = _selectedLocId;
    if (!keptId && _planStatus && _planStatus.current_sucursales && _planStatus.current_sucursales.length > 0) {
      keptId = _planStatus.current_sucursales[0].id;
    }
    if (!keptId) { mesioToast('Selecciona la sucursal que deseas conservar', 'error'); return; }

    var confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('Confirmar bajada al plan ' + newPlan + '. Entrara en vigor en 7 dias. Podras cancelarla antes.')
      : confirm('Confirmar bajada al plan ' + newPlan + '. Entrara en vigor en 7 dias.');
    if (!confirmed) return;

    btn.disabled = true;
    btn.textContent = 'Programando...';

    try {
      var r = await fetch('/api/billing/request-downgrade', {
        method: 'POST',
        headers: mesioHeaders(),
        body: JSON.stringify({ new_plan_code: newPlan, kept_location_id: keptId }),
      });
      mesioTrackFetch(r.ok);
      var d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'Error al programar bajada');
      mesioToast('Bajada de plan programada correctamente', 'success');
      // Reload status
      await _loadDowngradeStatus();
    } catch (e) {
      mesioToast(e.message || 'Error', 'error');
      btn.disabled = false;
      btn.textContent = 'Programar bajada de plan';
    }
  }

  // ── Cancel downgrade ──────────────────────────────────────────
  async function _cancelDowngrade() {
    var confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('Cancelar la bajada de plan programada?')
      : confirm('Cancelar la bajada de plan programada?');
    if (!confirmed) return;

    var btn = document.getElementById('btn-cancel-downgrade');
    if (btn) { btn.disabled = true; btn.textContent = 'Cancelando...'; }

    try {
      var r = await fetch('/api/billing/cancel-downgrade', {
        method: 'POST',
        headers: mesioHeaders(),
        body: JSON.stringify({}),
      });
      mesioTrackFetch(r.ok);
      var d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'Error al cancelar');
      mesioToast('Bajada de plan cancelada', 'success');
      await _loadDowngradeStatus();
    } catch (e) {
      mesioToast(e.message || 'Error', 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Cancelar bajada'; }
    }
  }

  // ── Load plan status + options, then render ───────────────────
  async function _loadDowngradeStatus() {
    try {
      var results = await Promise.all([
        fetch('/api/billing/plan-status',  { headers: mesioHeaders() }),
        fetch('/api/billing/plan-options', { headers: mesioHeaders() }),
      ]);
      mesioTrackFetch(results[0].ok);
      mesioTrackFetch(results[1].ok);

      if (!results[0].ok || !results[1].ok) return;

      _planStatus  = await results[0].json();
      _planOptions = await results[1].json();

      _renderBanner(_planStatus);
      _renderDowngradeForm(_planStatus, _planOptions);
    } catch (e) {
      // Fail gracefully — downgrade section stays hidden
    }
  }

  // ── Event wiring ───────────────────────────────────────────────
  var btnCancel = document.getElementById('btn-cancel-downgrade');
  if (btnCancel) btnCancel.addEventListener('click', _cancelDowngrade);

  var btnSubmit = document.getElementById('btn-submit-downgrade');
  if (btnSubmit) btnSubmit.addEventListener('click', _submitDowngrade);

  var selectPlan = document.getElementById('select-downgrade-plan');
  if (selectPlan) {
    selectPlan.addEventListener('change', function () {
      _onPlanSelectChange(_planStatus);
    });
  }

  // Bootstrap — load after a short defer so the Mi Plan module runs first
  setTimeout(_loadDowngradeStatus, 100);
})();
