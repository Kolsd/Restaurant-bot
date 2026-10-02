/* Mesio HQ — organization ficha. Data: GET /api/internal/hq/orgs/{id}.
   Everything is built with textContent: names, comments and usernames are
   the restaurants' own data. */
(function () {
  'use strict';

  var SESSION_KEY = 'hq_session';
  var token = sessionStorage.getItem(SESSION_KEY) || '';
  var orgId = (window.location.pathname.match(/\/internal\/org\/(\d+)/) || [])[1];
  var data = null;
  var currentSede = null;

  if (!token) { window.location.href = '/internal/superadmin'; return; }

  // ── tiny DOM helpers ─────────────────────────────────────────────
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  function $(id) { return document.getElementById(id); }

  var cop = new Intl.NumberFormat('es-CO', { style: 'currency', currency: 'COP', maximumFractionDigits: 0 });
  function money(v) { return v === null || v === undefined ? '—' : cop.format(v); }
  function num(v) { return v === null || v === undefined ? '—' : Number(v).toLocaleString('es-CO'); }
  function ago(iso) {
    if (!iso) return 'nunca';
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return 'hace un momento';
    if (s < 3600) return 'hace ' + Math.round(s / 60) + ' min';
    if (s < 86400) return 'hace ' + Math.round(s / 3600) + ' h';
    return 'hace ' + Math.round(s / 86400) + ' d';
  }
  function day(iso) {
    return iso ? new Date(iso).toLocaleDateString('es-CO', { day: 'numeric', month: 'short', year: 'numeric' }) : '—';
  }

  var STATUS = {
    trial: ['Trial', 'info'], activo: ['Activo', 'ok'],
    vencido: ['Pago vencido', 'warn'], suspendido: ['Suspendido', 'bad']
  };
  var SEVERITY = { critical: ['Crítico', 'bad'], warning: ['Atención', 'warn'], info: ['Info', 'info'] };
  var ROLE = { owner: 'Dueño', admin: 'Admin', gerente: 'Gerente', mesero: 'Mesero', cocina: 'Cocina',
    caja: 'Caja', bar: 'Bar', domiciliario: 'Domiciliario' };

  function badge(text, kind) { return el('span', 'org-badge' + (kind ? ' ' + kind : ''), text); }
  function kpi(label, value, note) {
    var k = el('div', 'org-kpi');
    k.appendChild(el('div', 'org-kpi-label', label));
    k.appendChild(el('div', 'org-kpi-value', value));
    if (note) k.appendChild(el('div', 'org-kpi-note', note));
    return k;
  }

  // ── fetch ────────────────────────────────────────────────────────
  function load() {
    if (!orgId) { showError('Falta el id de la organización en la URL.'); return; }
    fetch('/api/internal/hq/orgs/' + orgId, { headers: { 'Authorization': 'Bearer ' + token } })
      .then(function (r) {
        if (r.status === 401 || r.status === 403) {
          sessionStorage.removeItem(SESSION_KEY);
          window.location.href = '/internal/superadmin';
          return null;
        }
        if (r.status === 404) { showError('Esa organización no existe.'); return null; }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        if (!d) return;
        data = d;
        $('org-error').hidden = true;
        render();
        $('org-updated').textContent = 'Actualizado ' + new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
      })
      .catch(function (e) { showError('No se pudo cargar la ficha: ' + e.message); });
  }
  function showError(msg) { var b = $('org-error'); b.textContent = msg; b.hidden = false; }

  // ── render ───────────────────────────────────────────────────────
  function render() {
    var o = data.org, b = data.business;
    document.title = 'Mesio HQ — ' + o.name;
    $('org-name').textContent = o.name;
    $('org-crumb').textContent = o.name;
    var sub = clear($('org-sub'));
    var st = STATUS[b.billing_status] || [b.billing_status, ''];
    sub.appendChild(badge(st[0], st[1]));
    sub.appendChild(badge(b.plan_name));
    if (b.paused_by_owner) sub.appendChild(badge('Pausado por el dueño', 'bad'));
    sub.appendChild(el('span', null, 'Org #' + o.id + ' · cliente desde ' + day(o.created_at)));
    if (o.order_link) {
      var a = el('a', null, o.order_link);
      a.href = o.order_link; a.target = '_blank'; a.rel = 'noopener';
      sub.appendChild(a);
    }
    $('org-costs').href = '/internal/costs?org=' + o.id;
    $('org-superadmin').href = '/internal/superadmin?org=' + o.id;

    renderFlags();
    renderBusiness();
    renderSedeTabs();
    renderErrors();
    renderUsers();
  }

  function renderErrors() {
    var t = clear($('org-errors'));
    var errs = data.errors || [];
    if (!errs.length) {
      var tr0 = el('tr'); var td0 = el('td', 'org-ok', '✓ Sin errores en los últimos 7 días.');
      tr0.appendChild(td0); t.appendChild(tr0); return;
    }
    var sedeName = {};
    data.sedes.forEach(function (s) { sedeName[s.id] = s.name; });
    var hr = el('tr');
    ['Último', 'Veces', 'Fuente', 'Sede', 'Dónde', 'Error', 'Request id'].forEach(function (h) { hr.appendChild(el('th', null, h)); });
    t.appendChild(hr);
    errs.forEach(function (e) {
      var tr = el('tr');
      tr.appendChild(el('td', null, ago(e.last_at)));
      tr.appendChild(el('td', null, num(e.count)));
      var src = el('td'); src.appendChild(badge(e.source === 'bot' ? 'Bot' : 'Servidor', e.source === 'bot' ? 'bad' : 'warn')); tr.appendChild(src);
      tr.appendChild(el('td', 'org-muted', e.location_id ? (sedeName[e.location_id] || '#' + e.location_id) : '—'));
      tr.appendChild(el('td', 'org-muted', [e.method, e.route].filter(Boolean).join(' ') || '—'));
      tr.appendChild(el('td', null, e.error_type + (e.message ? ': ' + e.message : '')));
      tr.appendChild(el('td', 'org-muted', e.request_id || '—'));
      t.appendChild(tr);
    });
  }

  function renderFlags() {
    var box = clear($('org-flags'));
    if (!data.flags.length) { box.appendChild(el('div', 'org-ok', '✓ Nada pendiente. Todas las sedes operan sin alertas.')); return; }
    data.flags.forEach(function (f) {
      var d = el('details', 'org-flag ' + f.severity);
      var s = el('summary');
      var sev = SEVERITY[f.severity] || [f.severity, ''];
      s.appendChild(badge(sev[0], sev[1]));
      if (f.sede) s.appendChild(badge(f.sede));
      s.appendChild(el('span', null, f.title + (f.count ? ' (' + f.count + ')' : '')));
      d.appendChild(s);
      var body = el('div', 'org-flag-body');
      var w = el('div'); w.appendChild(el('b', null, 'Dónde mirar: ')); w.appendChild(document.createTextNode(f.where));
      var fx = el('div'); fx.appendChild(el('b', null, 'Cómo resolverlo: ')); fx.appendChild(document.createTextNode(f.fix));
      body.appendChild(w); body.appendChild(fx);
      d.appendChild(body);
      box.appendChild(d);
    });
  }

  function renderBusiness() {
    var b = data.business, box = clear($('org-business'));
    var st = STATUS[b.billing_status] || [b.billing_status];
    box.appendChild(kpi('Estado', st[0],
      b.billing_status === 'trial' ? 'Trial hasta ' + day(b.comp_until)
        : b.paid_until ? 'Pagado hasta ' + day(b.paid_until) : 'Gestionado a mano'));
    if (b.billing_status === 'vencido') box.appendChild(kpi('Se pausa el', day(b.pauses_on)));
    box.appendChild(kpi('Plan', b.plan_name, b.founder_price_cop ? 'Precio fundador' : null));
    box.appendChild(kpi('Precio por sede', money(b.price_per_sede_cop), 'al mes'));
    box.appendChild(kpi('Sedes activas', num(b.active_sedes)));
    box.appendChild(kpi('MRR', money(b.mrr_cop), b.mrr_cop ? null : 'No factura (trial, suspendido o a mano)'));
    box.appendChild(kpi('Costo IA 30 días', money(b.llm_cost_30d_cop), num(b.llm_tokens_30d) + ' tokens'));
    box.appendChild(kpi('Margen 30 días', b.margin_30d_cop === null ? '—' : money(b.margin_30d_cop), 'MRR − costo IA'));
    box.appendChild(kpi('Último login del panel', ago(data.people.last_login)));
  }

  function renderSedeTabs() {
    var tabs = clear($('org-sede-tabs'));
    if (!data.sedes.length) { clear($('org-sede')).appendChild(el('div', 'org-hint', 'Esta organización no tiene sedes.')); return; }
    if (!currentSede || !data.sedes.some(function (s) { return s.id === currentSede; })) currentSede = data.sedes[0].id;
    data.sedes.forEach(function (s) {
      var t = el('button', 'org-tab', s.name + (s.active ? '' : ' (inactiva)'));
      t.type = 'button';
      t.setAttribute('role', 'tab');
      t.setAttribute('aria-selected', String(s.id === currentSede));
      var urgent = s.flags.filter(function (f) { return f.severity !== 'info'; }).length;
      if (urgent) t.appendChild(el('span', 'org-tab-count', urgent));
      t.addEventListener('click', function () { currentSede = s.id; renderSedeTabs(); });
      tabs.appendChild(t);
    });
    renderSede(data.sedes.filter(function (s) { return s.id === currentSede; })[0]);
  }

  function renderSede(s) {
    var box = clear($('org-sede'));
    var meta = el('div', 'org-hint', [s.address || 'Sin dirección', s.phone || null, 'Zona horaria ' + s.timezone, 'Sede #' + s.id]
      .filter(Boolean).join(' · '));
    box.appendChild(meta);

    var op = s.operation;
    box.appendChild(el('h3', 'org-h3', 'Operación'));
    var g = el('div', 'org-kpis');
    g.appendChild(kpi('Pedidos hoy', num(op.orders_today), 'mesas + web, día local'));
    g.appendChild(kpi('Ventas hoy', money(op.sales_today)));
    g.appendChild(kpi('Ventas 7 días', money(op.sales_7d)));
    g.appendChild(kpi('Ventas 30 días', money(op.sales_30d)));
    g.appendChild(kpi('Ticket promedio', money(op.avg_ticket_30d), '30 días'));
    g.appendChild(kpi('Mesas abiertas', num(op.tables_open) + ' / ' + num(op.tables)));
    g.appendChild(kpi('Rondas de mesa', num(op.table_rounds_30d), '30 días · ' + num(op.table_rounds_7d) + ' en 7'));
    g.appendChild(kpi('Domicilios / recoger', num(op.delivery_30d) + ' / ' + num(op.pickup_30d), '30 días'));
    g.appendChild(kpi('Tiempo de cocina', op.kitchen_p50_min === null ? '—' : op.kitchen_p50_min + ' min',
      op.kitchen_p50_min === null ? 'Sin rondas marcadas "listo" en 7 días' : 'mediana · p90 ' + op.kitchen_p90_min + ' min (' + op.kitchen_samples_7d + ')'));
    g.appendChild(kpi('Alertas al mesero', num(op.open_waiter_alerts), 'sin atender, 24 h'));
    g.appendChild(kpi('Último pedido', ago(op.last_order_at)));
    box.appendChild(g);

    var ad = s.adoption;
    box.appendChild(el('h3', 'org-h3', 'Uso y adopción'));
    var a = el('div', 'org-kpis');
    a.appendChild(kpi('Comensales 7 días', num(ad.diner_sessions_7d), num(ad.table_diner_sessions_7d) + ' en mesa'));
    a.appendChild(kpi('Recordados', num(ad.remembered_diners_7d), 'aceptaron "te recordamos"'));
    a.appendChild(kpi('Chats con la IA', num(ad.chat_conversations_7d), '7 días'));
    a.appendChild(kpi('Último comensal', ago(ad.last_diner_at)));
    a.appendChild(kpi('Reservas 30 días', num(ad.reservations_30d)));
    a.appendChild(kpi('Inventario', num(ad.inventory_items) + ' insumos'));
    a.appendChild(kpi('Operación configurada', ad.ops_configured ? 'Sí' : 'No',
      ad.ops_configured ? ['bar', 'delivery', 'courier', 'waiter'].filter(function (k) { return ad.ops[k]; })
        .map(function (k) { return { bar: 'Bar', delivery: 'Domicilios', courier: 'Mis entregas', waiter: 'Mesero' }[k]; }).join(', ') || 'Solo cocina'
        : 'El dueño no ha respondido el asistente'));
    a.appendChild(kpi('Domicilios web', ad.delivery_enabled ? 'Activos' : 'Apagados',
      (ad.pickup_enabled ? 'Recoger activo' : 'Recoger apagado') + (ad.payment_methods.length ? ' · ' + ad.payment_methods.join(', ') : '')));
    box.appendChild(a);

    var n = s.nps;
    box.appendChild(el('h3', 'org-h3', 'NPS (30 días)'));
    var ng = el('div', 'org-kpis');
    ng.appendChild(kpi('NPS', n.score_30d === null ? '—' : n.score_30d, num(n.responses_30d) + ' respuestas'));
    ng.appendChild(kpi('Quejas con comentario', num(n.detractor_comments_30d)));
    box.appendChild(ng);

    box.appendChild(el('h3', 'org-h3', 'Personal (' + s.staff.length + ')'));
    if (!s.staff.length) { box.appendChild(el('div', 'org-hint', 'Sin personal creado en esta sede.')); return; }
    var wrap = el('div', 'org-table-wrap'), t = el('table', 'org-table');
    var hr = el('tr');
    ['Nombre', 'Usuario', 'Roles', 'Estado', 'Último inicio de sesión'].forEach(function (h) { hr.appendChild(el('th', null, h)); });
    t.appendChild(hr);
    s.staff.forEach(function (p) {
      var tr = el('tr');
      tr.appendChild(el('td', null, p.name));
      tr.appendChild(el('td', 'org-muted', p.username || '—'));
      tr.appendChild(el('td', null, String(p.roles || '').split(',').map(function (r) { return ROLE[r.trim()] || r.trim(); }).join(', ')));
      var tdS = el('td'); tdS.appendChild(badge(p.active ? 'Activo' : 'Inactivo', p.active ? 'ok' : '')); tr.appendChild(tdS);
      tr.appendChild(el('td', null, ago(p.last_login)));
      t.appendChild(tr);
    });
    wrap.appendChild(t); box.appendChild(wrap);
  }

  function renderUsers() {
    var t = clear($('org-users'));
    var hr = el('tr');
    ['Usuario', 'Nombre', 'Rol', 'Sede', 'Último inicio de sesión'].forEach(function (h) { hr.appendChild(el('th', null, h)); });
    t.appendChild(hr);
    var sedeName = {};
    data.sedes.forEach(function (s) { sedeName[s.id] = s.name; });
    data.people.users.forEach(function (u) {
      var tr = el('tr');
      tr.appendChild(el('td', null, u.username));
      tr.appendChild(el('td', null, u.name || '—'));
      tr.appendChild(el('td', null, String(u.role || '').split(',').map(function (r) { return ROLE[r.trim()] || r.trim(); }).join(', ')));
      tr.appendChild(el('td', 'org-muted', u.location_id ? (sedeName[u.location_id] || '#' + u.location_id) : 'Todas'));
      tr.appendChild(el('td', null, ago(u.last_login)));
      t.appendChild(tr);
    });
  }

  $('org-refresh').addEventListener('click', load);
  load();
  setInterval(function () { if (!document.hidden) load(); }, 60000);
})();
