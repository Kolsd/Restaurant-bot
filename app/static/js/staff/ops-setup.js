/* ═══════════════════════════════════════════════════
   Mesio — Staff App / "Configurar operación"
   Which screens this sede uses (app/services/ops_config.py, migration 0105):
   bar (and which carta categories go there), Domicilios, Mis entregas,
   Mesero. Caja and Cocina are always on. Opened by staff-shell.js the first
   time an owner/admin/gerente enters Operación, and from the sidebar after.
   GET/PUT /api/staff/ops-config.
   ═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var overlay = null;
  var trap = null;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function close() {
    if (trap) { trap.deactivate(); trap = null; }
    if (overlay) { overlay.remove(); overlay = null; }
  }

  /* A yes/no question as two pill buttons. Returns {node, get, onChange}. */
  function yesNo(question, hint, initial) {
    var value = !!initial;
    var listeners = [];
    var row = el('div', 'ops-q');
    var text = el('div', 'ops-q-text');
    text.appendChild(el('div', 'ops-q-title', question));
    if (hint) text.appendChild(el('div', 'ops-q-hint', hint));
    row.appendChild(text);
    var pills = el('div', 'ops-pills');
    pills.setAttribute('role', 'group');
    pills.setAttribute('aria-label', question);
    var yes = el('button', 'ops-pill', 'Sí');
    var no = el('button', 'ops-pill', 'No');
    yes.type = 'button';
    no.type = 'button';
    function paint() {
      yes.classList.toggle('on', value);
      no.classList.toggle('on', !value);
      yes.setAttribute('aria-pressed', String(value));
      no.setAttribute('aria-pressed', String(!value));
    }
    function set(v) {
      value = v;
      paint();
      listeners.forEach(function (fn) { fn(value); });
    }
    yes.addEventListener('click', function () { set(true); });
    no.addEventListener('click', function () { set(false); });
    pills.appendChild(yes);
    pills.appendChild(no);
    row.appendChild(pills);
    paint();
    return {
      node: row,
      get: function () { return value; },
      onChange: function (fn) { listeners.push(fn); },
    };
  }

  function render(data, opts) {
    var cfg = data.config || {};
    var first = !cfg.configured;
    var deliveryInPlan = !!data.delivery_in_plan;

    overlay = el('div', 'ops-overlay');
    var box = el('div', 'ops-box');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'ops-title');

    var title = el('h2', 'ops-title', first ? 'Configura tu operación' : 'Configurar operación');
    title.id = 'ops-title';
    box.appendChild(title);
    var sede = data.location_name ? (' en ' + data.location_name) : '';
    box.appendChild(el('p', 'ops-lead',
      'Elige qué pantallas usas' + sede + '. Caja y Cocina siempre están; lo que apagues no aparece en el menú. Puedes cambiarlo cuando quieras.'));

    // Unconfigured sedes start from the common case, not "everything on".
    var bar = yesNo('¿Tienes bar con su propia pantalla?',
      'Las bebidas y cócteles salen en la pantalla del bar y no en la de cocina.',
      first ? false : cfg.bar);
    box.appendChild(bar.node);

    var catsWrap = el('div', 'ops-cats');
    catsWrap.appendChild(el('div', 'ops-q-hint', '¿Qué categorías de la carta se preparan en el bar?'));
    var catRow = el('div', 'ops-cat-row');
    var chosen = {};
    (cfg.bar_categories || []).forEach(function (c) { chosen[c] = true; });
    var categories = Array.isArray(data.categories) ? data.categories : [];
    if (!categories.length) {
      catRow.appendChild(el('div', 'ops-q-hint', 'Tu carta todavía no tiene categorías. Crea la carta primero.'));
    }
    categories.forEach(function (c) {
      if (first && /bebida|bar|c[oó]ctel|cerveza|vino|licor|trago/i.test(c)) chosen[c] = true;
      var chip = el('button', 'ops-cat', c);
      chip.type = 'button';
      function paint() {
        chip.classList.toggle('on', !!chosen[c]);
        chip.setAttribute('aria-pressed', String(!!chosen[c]));
      }
      chip.addEventListener('click', function () { chosen[c] = !chosen[c]; paint(); });
      paint();
      catRow.appendChild(chip);
    });
    catsWrap.appendChild(catRow);
    box.appendChild(catsWrap);
    function syncCats() { catsWrap.hidden = !bar.get(); }
    bar.onChange(syncCats);
    syncCats();

    var delivery = null;
    var courier = null;
    if (deliveryInPlan) {
      delivery = yesNo('¿Recibes pedidos a domicilio o para recoger?',
        'Domicilios: la caja acepta los pedidos que llegan por tu link y los asigna.',
        first ? true : cfg.delivery);
      box.appendChild(delivery.node);
      courier = yesNo('¿Tienes domiciliarios propios?',
        'Mis entregas: el domiciliario ve en su celular los pedidos que le asignan.',
        first ? false : cfg.courier);
      box.appendChild(courier.node);
      var syncCourier = function () { courier.node.hidden = !delivery.get(); };
      delivery.onChange(syncCourier);
      syncCourier();
    }

    var waiter = yesNo('¿Tus meseros usan la app en su celular?',
      'Mesero: ven las mesas, los llamados y cuándo un pedido está listo.',
      first ? true : cfg.waiter);
    box.appendChild(waiter.node);

    var actions = el('div', 'ops-actions');
    var later = el('button', 'btn', first ? 'Ahora no' : 'Cancelar');
    later.type = 'button';
    later.addEventListener('click', close);
    var save = el('button', 'btn brand', 'Guardar');
    save.type = 'button';
    save.addEventListener('click', async function () {
      var cats = categories.filter(function (c) { return chosen[c]; });
      if (bar.get() && !cats.length) {
        mesioToast('Elige al menos una categoría para el bar.', 'warning', 3500);
        return;
      }
      save.disabled = true;
      try {
        var res = await fetch('/api/staff/ops-config', {
          method: 'PUT',
          headers: mesioHeaders(),
          body: JSON.stringify({
            bar: bar.get(),
            bar_categories: bar.get() ? cats : [],
            delivery: delivery ? delivery.get() : false,
            courier: delivery && courier ? (delivery.get() && courier.get()) : false,
            waiter: waiter.get(),
          }),
        });
        var body = null;
        try { body = await res.json(); } catch (e) { body = null; }
        if (!res.ok) throw new Error((body && body.detail) || 'No pudimos guardar la configuración.');
        close();
        mesioToast('Operación configurada', 'success', 2500);
        if (opts && typeof opts.onSaved === 'function') opts.onSaved(body && body.config);
      } catch (e) {
        save.disabled = false;
        mesioToast((e && e.message) || 'No pudimos guardar la configuración.', 'error', 4000);
      }
    });
    actions.appendChild(later);
    actions.appendChild(save);
    box.appendChild(actions);

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'ops-title' });
  }

  async function open(opts) {
    if (overlay) return;
    try {
      var res = await fetch('/api/staff/ops-config', { headers: mesioHeaders() });
      var data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      if (!res.ok) throw new Error((data && data.detail) || 'No pudimos cargar la configuración.');
      render(data, opts || {});
    } catch (e) {
      mesioToast((e && e.message) || 'No pudimos cargar la configuración.', 'error', 4000);
    }
  }

  window.MesioOpsSetup = { open: open };
})();
