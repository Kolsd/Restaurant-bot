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

  // ── support actions ──────────────────────────────────────────────
  function api(method, path, body) {
    return fetch(path, {
      method: method,
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) {
          var d = j && j.detail;
          throw new Error(typeof d === 'string' ? d : (Array.isArray(d) && d[0] && d[0].msg) || ('HTTP ' + r.status));
        }
        return j;
      });
    });
  }

  // One dialog for every action: says what will happen, asks the reason,
  // runs `run(reason)`; errors stay in the dialog, success reloads the ficha.
  function act(title, desc, run, okText) {
    var dlg = $('org-action');
    $('org-action-title').textContent = title;
    $('org-action-desc').textContent = desc;
    $('org-action-reason').value = '';
    $('org-action-error').textContent = '';
    $('org-action-ok').textContent = okText || 'Confirmar';
    $('org-action-ok').disabled = false;
    $('org-action-form').onsubmit = function (e) {
      e.preventDefault();
      var reason = $('org-action-reason').value.trim();
      if (reason.length < 8) { $('org-action-error').textContent = 'Escribe el motivo (mínimo 8 caracteres).'; return; }
      $('org-action-ok').disabled = true;
      run(reason).then(function (msg) {
        dlg.close();
        if (msg) toast(msg);
        load();
      }).catch(function (err) {
        $('org-action-ok').disabled = false;
        $('org-action-error').textContent = err.message;
      });
    };
    $('org-action-cancel').onclick = function () { dlg.close(); };
    dlg.showModal();
    $('org-action-reason').focus();
  }
  function toast(msg) {
    if (typeof mesioToast === 'function') { mesioToast(msg, 'success'); return; }
    $('org-updated').textContent = msg;
  }
  function supportUrl(action) { return '/api/internal/hq/support/' + orgId + '/' + action; }
  function smallBtn(text, onClick, danger) {
    var b = el('button', 'org-btn org-btn-sm' + (danger ? ' org-btn-danger' : ''), text);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }

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
      if (f.code === 'paused') {
        var row = el('div');
        row.appendChild(smallBtn('Despausar restaurante', function () {
          act('Despausar ' + data.org.name, 'Los comensales vuelven a poder pedir en todas las sedes. Hazlo solo si el dueño lo confirmó.',
            function (reason) { return api('POST', supportUrl('unpause'), { reason: reason }).then(function () { return 'Restaurante reactivado'; }); },
            'Despausar');
        }));
        body.appendChild(row);
      }
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
    loadSupport(currentSede);
  }

  function loadSupport(sedeId) {
    var box = clear($('org-support'));
    box.appendChild(el('div', 'org-hint', 'Cargando…'));
    api('GET', '/api/internal/hq/orgs/' + orgId + '/sedes/' + sedeId + '/support')
      .then(function (sup) { if (sedeId === currentSede) renderSupport(sedeId, sup); })
      .catch(function (e) { clear(box).appendChild(el('div', 'org-error', 'No se pudo cargar: ' + e.message)); });
  }

  function block(title, headBtn) {
    var b = el('div', 'org-support-block');
    var h = el('div', 'org-support-head');
    h.appendChild(el('div', 'org-support-title', title));
    if (headBtn) h.appendChild(headBtn);
    b.appendChild(h);
    return b;
  }
  function supRow(text, btn) {
    var r = el('div', 'org-support-row');
    r.appendChild(el('span', null, text));
    if (btn) r.appendChild(btn);
    return r;
  }
  function tableLabel(name, id) {
    var n = name || id || '';
    return /^\d+$/.test(n) ? 'Mesa ' + n : n;
  }

  function renderSupport(sedeId, sup) {
    var box = clear($('org-support'));
    var sede = data.sedes.filter(function (s) { return s.id === sedeId; })[0];
    var sedeName = sede ? sede.name : '#' + sedeId;

    var b1 = block('Mesas abiertas (' + sup.sittings.length + ')');
    if (!sup.sittings.length) b1.appendChild(supRow('Ninguna.'));
    sup.sittings.forEach(function (s) {
      var label = tableLabel(s.table_name, s.table_id);
      b1.appendChild(supRow(label + ' · abierta ' + ago(s.started_at) + ' · ' + money(s.rounds_total) + ' en rondas',
        smallBtn('Cerrar mesa', function () {
          act('Cerrar ' + label, 'Cierra la sesión de la mesa en ' + sedeName + '. Lo ya cobrado no cambia; los comensales tendrán que escanear de nuevo.',
            function (reason) { return api('POST', supportUrl('close-sitting'), { session_id: s.id, reason: reason }).then(function () { return 'Mesa cerrada'; }); },
            'Cerrar mesa');
        }, true)));
    });
    box.appendChild(b1);

    var b2 = block('Rondas de mesa atascadas (+45 min sin "listo")');
    if (!sup.stuck_rounds.length) b2.appendChild(supRow('Ninguna.'));
    sup.stuck_rounds.forEach(function (o) {
      b2.appendChild(supRow(tableLabel(o.table_name) + ' · ' + o.status + ' · ' + money(o.total) + ' · ' + ago(o.created_at),
        smallBtn('Cancelar ronda', function () {
          act('Cancelar ronda', 'La ronda sale de cocina y de la cuenta de la mesa. Confírmalo con el restaurante antes.',
            function (reason) { return api('POST', supportUrl('cancel-round'), { order_id: o.id, reason: reason }).then(function () { return 'Ronda cancelada'; }); },
            'Cancelar ronda');
        }, true)));
    });
    box.appendChild(b2);

    var b3 = block('Domicilios / recoger atascados (+90 min)');
    if (!sup.stuck_web_orders.length) b3.appendChild(supRow('Ninguno.'));
    sup.stuck_web_orders.forEach(function (o) {
      b3.appendChild(supRow((o.public_code || o.id) + ' · ' + o.order_type + ' · ' + o.status + ' · ' + money(o.total) + ' · ' + ago(o.created_at),
        smallBtn('Cancelar pedido', function () {
          act('Cancelar pedido ' + (o.public_code || ''), 'El pedido queda cancelado; el cliente lo verá en su seguimiento. Si ya pagó, el reembolso lo hace el restaurante.',
            function (reason) { return api('POST', supportUrl('cancel-web-order'), { order_id: o.id, reason: reason }).then(function () { return 'Pedido cancelado'; }); },
            'Cancelar pedido');
        }, true)));
    });
    box.appendChild(b3);

    var wa = sup.waiter_alerts || {};
    var b4 = block('Alertas al mesero sin atender: ' + num(wa.open) + (wa.oldest ? ' · la más vieja ' + ago(wa.oldest) : ''),
      wa.open ? smallBtn('Descartar las de más de 1 h', function () {
        act('Descartar alertas viejas', 'Marca como atendidas las alertas al mesero de ' + sedeName + ' con más de una hora.',
          function (reason) {
            return api('POST', supportUrl('dismiss-alerts'), { location_id: sedeId, older_than_minutes: 60, reason: reason })
              .then(function (r) { return r.dismissed + ' alertas descartadas'; });
          }, 'Descartar');
      }) : null);
    box.appendChild(b4);

    var b5 = block('Platos agotados (' + sup.sold_out.length + ')',
      sup.sold_out.length > 1 ? smallBtn('Quitar todos', function () {
        act('Quitar todos los agotados', 'Todos los platos agotados de ' + sedeName + ' vuelven a estar disponibles para pedir.',
          function (reason) {
            return api('POST', supportUrl('clear-sold-out'), { location_id: sedeId, dish_name: null, reason: reason })
              .then(function (r) { return r.cleared + ' platos disponibles de nuevo'; });
          }, 'Quitar agotados');
      }) : null);
    if (!sup.sold_out.length) b5.appendChild(supRow('Ninguno.'));
    sup.sold_out.forEach(function (d) {
      b5.appendChild(supRow(d.dish_name + ' · agotado ' + ago(d.updated_at), smallBtn('Disponible', function () {
        act('Marcar disponible: ' + d.dish_name, 'Los comensales de ' + sedeName + ' podrán volver a pedirlo.',
          function (reason) {
            return api('POST', supportUrl('clear-sold-out'), { location_id: sedeId, dish_name: d.dish_name, reason: reason })
              .then(function () { return d.dish_name + ' disponible'; });
          }, 'Marcar disponible');
      })));
    });
    box.appendChild(b5);

    var b6 = block('Facturas DIAN sin aceptar (' + sup.dian_pending.length + ')');
    if (!sup.dian_pending.length) b6.appendChild(supRow('Ninguna.'));
    sup.dian_pending.forEach(function (f) {
      b6.appendChild(supRow((f.prefix || '') + (f.invoice_number || '') + ' · pedido ' + f.order_id + ' · ' + f.dian_status + ' · ' + ago(f.created_at)));
    });
    if (sup.dian_pending.length) {
      b6.appendChild(el('div', 'org-hint', 'El reintento desde el HQ llega cuando probemos DIAN en el sandbox de MATIAS (para no duplicar folios). Mientras tanto, revisa la respuesta del proveedor en billing_log.'));
    }
    box.appendChild(b6);

    var b7 = block('Configurar operación', smallBtn('Volver a preguntar', function () {
      act('Volver a preguntar "Configurar operación"', 'El dueño o admin verá otra vez el asistente de cocina, bar, domicilios y mesero la próxima vez que entre a la Staff App de ' + sedeName + '. Sus respuestas anteriores quedan como punto de partida.',
        function (reason) { return api('POST', supportUrl('reask-ops'), { location_id: sedeId, reason: reason }).then(function () { return 'Se le volverá a preguntar'; }); },
        'Volver a preguntar');
    }));
    b7.appendChild(supRow(sede && sede.adoption.ops_configured ? 'Configurada.' : 'Aún sin configurar.'));
    box.appendChild(b7);
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
    ['Nombre', 'Usuario', 'Roles', 'Estado', 'Último inicio de sesión', ''].forEach(function (h) { hr.appendChild(el('th', null, h)); });
    t.appendChild(hr);
    s.staff.forEach(function (p) {
      var tr = el('tr');
      tr.appendChild(el('td', null, p.name));
      tr.appendChild(el('td', 'org-muted', p.username || '—'));
      tr.appendChild(el('td', null, String(p.roles || '').split(',').map(function (r) { return ROLE[r.trim()] || r.trim(); }).join(', ')));
      var tdS = el('td'); tdS.appendChild(badge(p.active ? 'Activo' : 'Inactivo', p.active ? 'ok' : '')); tr.appendChild(tdS);
      tr.appendChild(el('td', null, ago(p.last_login)));
      var tdA = el('td');
      tdA.appendChild(smallBtn('Cerrar sesiones', function () {
        act('Cerrar sesiones de ' + p.name, 'Saca a ' + p.name + ' de la Staff App en todos sus dispositivos. Podrá volver a entrar con su PIN.',
          function (reason) { return api('POST', supportUrl('close-sessions'), { staff_id: p.id, reason: reason }).then(function (r) { return r.sessions_closed + ' sesiones cerradas'; }); },
          'Cerrar sesiones');
      }));
      tr.appendChild(tdA);
      t.appendChild(tr);
    });
    wrap.appendChild(t); box.appendChild(wrap);
  }

  function renderUsers() {
    var t = clear($('org-users'));
    var hr = el('tr');
    ['Usuario', 'Nombre', 'Rol', 'Sede', 'Último inicio de sesión', ''].forEach(function (h) { hr.appendChild(el('th', null, h)); });
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
      var tdA = el('td');
      tdA.style.whiteSpace = 'nowrap';
      if (u.username.indexOf('@') > 0) {
        tdA.appendChild(smallBtn('Enviar código', function () {
          act('Enviar código a ' + u.username, 'Le llega a su email el mismo código de "¿Olvidaste tu contraseña?". Mesio nunca pone ni ve la contraseña.',
            function (reason) { return api('POST', supportUrl('password-reset'), { username: u.username, reason: reason }).then(function () { return 'Código enviado a ' + u.username; }); },
            'Enviar código');
        }));
        tdA.appendChild(document.createTextNode(' '));
      }
      tdA.appendChild(smallBtn('Cerrar sesiones', function () {
        act('Cerrar sesiones de ' + u.username, 'Saca a este usuario del panel en todos sus dispositivos.',
          function (reason) { return api('POST', supportUrl('close-sessions'), { username: u.username, reason: reason }).then(function (r) { return r.sessions_closed + ' sesiones cerradas'; }); },
          'Cerrar sesiones');
      }));
      tr.appendChild(tdA);
      t.appendChild(tr);
    });
  }

  $('org-refresh').addEventListener('click', load);
  load();
  setInterval(function () { if (!document.hidden) load(); }, 60000);
})();
