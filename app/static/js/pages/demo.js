/* /demo — live demo page (app/routes/live_demo.py, app/services/live_demo.py).
 *
 * Gets a table of the demo restaurant, shows its QR, and polls that table's
 * kitchen tickets so the visitor watches their own phone order arrive and
 * can move it along like a kitchen would. Public page: no auth headers. */
(function () {
  'use strict';

  var STORE_KEY = 'mesio_demo_table';
  var TABLE_TTL_MS = 60 * 60 * 1000;   // keep the same table across reloads for an hour
  var POLL_MS = 3000;
  var NEXT = {
    recibido: { status: 'en_preparacion', label: 'Empezar a preparar' },
    en_preparacion: { status: 'listo', label: 'Marcar listo' },
    listo: { status: 'entregado', label: 'Entregado' },
  };
  var STATUS_LABELS = {
    recibido: 'Nuevo', en_preparacion: 'En preparación', listo: 'Listo', entregado: 'Entregado',
  };

  var tableId = null;
  var seen = {};
  var lastSignature = '';
  var timer = null;

  function $(id) { return document.getElementById(id); }

  function loadStored() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (!raw) return null;
      var data = JSON.parse(raw);
      return data && data.at && Date.now() - data.at < TABLE_TTL_MS ? data : null;
    } catch (e) { return null; }
  }

  function store(table) {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        table_id: table.table_id, table_name: table.table_name, chat_url: table.chat_url, at: Date.now(),
      }));
    } catch (e) { /* private mode: the table just won't survive a reload */ }
  }

  function showError(el, msg) {
    el.textContent = msg;
    el.hidden = !msg;
  }

  function renderTable(table) {
    tableId = table.table_id;
    var url = window.location.origin + table.chat_url;
    var qr = $('qr');
    qr.textContent = '';
    if (typeof QRCode === 'function') {
      new QRCode(qr, { text: url, width: 200, height: 200, colorDark: '#0D1412', colorLight: '#ffffff',
                       correctLevel: QRCode.CorrectLevel.M });
    }
    $('table-name').textContent = table.table_name;
    var link = $('open-here');
    link.href = table.chat_url;
    link.hidden = false;
  }

  async function getTable(forceNew) {
    showError($('table-error'), '');
    var stored = forceNew ? null : loadStored();
    if (stored) { renderTable(stored); return; }
    try {
      var res = await fetch('/api/demo/table', { method: 'POST' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var table = await res.json();
      store(table);
      seen = {};
      renderTable(table);
    } catch (e) {
      $('table-name').textContent = 'Sin mesa';
      showError($('table-error'), 'No pudimos asignarte una mesa. Recarga la página en unos segundos.');
    }
  }

  function minutesAgo(iso) {
    var secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    return secs < 60 ? 'hace ' + secs + ' s' : 'hace ' + Math.round(secs / 60) + ' min';
  }

  function ticketEl(order) {
    var el = document.createElement('article');
    el.className = 'ticket' + (seen[order.id] ? '' : ' is-new');
    seen[order.id] = true;

    var top = document.createElement('div');
    top.className = 'ticket-top';
    var b = document.createElement('b');
    b.textContent = $('table-name').textContent;
    var when = document.createElement('span');
    when.className = 'ticket-age';
    when.dataset.createdAt = order.created_at || '';
    when.textContent = order.created_at ? minutesAgo(order.created_at) : '';
    top.appendChild(b);
    top.appendChild(when);
    el.appendChild(top);

    var chip = document.createElement('span');
    chip.className = 'chip ' + order.status;
    chip.textContent = STATUS_LABELS[order.status] || order.status;
    el.appendChild(chip);

    var ul = document.createElement('ul');
    order.items.forEach(function (item) {
      var li = document.createElement('li');
      li.textContent = item.qty + ' × ' + item.name;
      if (item.notes) {
        var note = document.createElement('small');
        note.textContent = item.notes;
        li.appendChild(note);
      }
      ul.appendChild(li);
    });
    el.appendChild(ul);

    var next = NEXT[order.status];
    if (next) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn ' + (next.status === 'listo' ? 'btn-primary' : 'btn-ghost') + ' btn-sm';
      btn.textContent = next.label;
      btn.addEventListener('click', function () { advance(order.id, next.status, btn); });
      el.appendChild(btn);
    }
    if (order.status === 'listo') {
      var hint = document.createElement('div');
      hint.className = 'hint';
      hint.textContent = 'Mira tu celular: el cliente ya sabe que su pedido está listo.';
      el.appendChild(hint);
    }
    return el;
  }

  function refreshAges() {
    document.querySelectorAll('.ticket-age').forEach(function (el) {
      if (el.dataset.createdAt) el.textContent = minutesAgo(el.dataset.createdAt);
    });
  }

  async function advance(orderId, status, btn) {
    btn.disabled = true;
    try {
      var res = await fetch('/api/demo/kitchen/orders/' + encodeURIComponent(orderId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: status }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      await poll();
    } catch (e) {
      btn.disabled = false;
    }
  }

  async function poll() {
    if (!tableId || document.hidden) return;
    try {
      var res = await fetch('/api/demo/kitchen/' + encodeURIComponent(tableId));
      if (!res.ok) return;
      var data = await res.json();
      var orders = (data.orders || []).slice().reverse();
      // Re-render only on change, so a button never vanishes under a click.
      var signature = JSON.stringify(orders.map(function (o) { return [o.id, o.status]; }));
      if (signature === lastSignature) { refreshAges(); return; }
      lastSignature = signature;
      var box = $('tickets');
      box.textContent = '';
      orders.forEach(function (o) { box.appendChild(ticketEl(o)); });
      $('empty').hidden = orders.length > 0;
    } catch (e) { /* next tick retries */ }
  }

  $('new-table').addEventListener('click', function () {
    lastSignature = '';
    getTable(true).then(poll);
  });

  getTable(false).then(function () {
    poll();
    timer = setInterval(poll, POLL_MS);
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
