(function () {
  'use strict';

  const token      = localStorage.getItem('rb_token');
  const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
  if (!token) { window.location.href = '/login'; return; }

  const hdr = { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' };

  const navName = document.getElementById('nav-rest-name');
  if (navName && restaurant.name) {
    const strong = document.createElement('strong');
    strong.textContent = restaurant.name;
    navName.textContent = '';
    navName.appendChild(strong);
    navName.appendChild(document.createTextNode(' \u00b7 Facturación'));
  }

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
    document.querySelectorAll('.tab-btn').forEach(function (b) { b.classList.remove('active'); });
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
      if (f.required && !val) { el.style.borderColor = 'var(--red)'; valid = false; }
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
        tdDate.style.cssText = 'color:var(--muted);font-size:.78rem;white-space:nowrap;';
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
        tdDetail.style.cssText = 'max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.75rem;color:var(--muted);';
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
    const tabBtn = e.target.closest('.tab-btn');
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
