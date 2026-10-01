/* ── Carta de esta sede ──────────────────────────────────────────────
   The organization's carta plus this sede's changes (migration 0093):
   its own price for a dish, a dish it does not sell, dishes of its own.
   The base carta is not touched from here.

   A gerente works their own sede; owner/admin work the sede picked in the
   sidebar (the backend resolves which, see deps.resolve_sede_filter).
   All user data goes through textContent. */
(function () {
  'use strict';

  var body = document.getElementById('sede-carta-body');
  if (!body) return;

  var _loaded = false;
  var _state = null; // last GET /api/menu/sede

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'style') node.style.cssText = attrs[k];
      else if (k === 'className') node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    if (text != null) node.textContent = text;
    return node;
  }

  function message(text) {
    body.textContent = '';
    body.appendChild(el('div', { style: 'padding:24px;color:var(--text-3);font-size:13px;' }, text));
  }

  async function api(method, path, payload) {
    var opts = { method: method, headers: mesioHeaders() };
    if (payload !== undefined) opts.body = JSON.stringify(payload);
    var res = await fetch(path, opts);
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      var detail = data && data.detail;
      var err = new Error(typeof detail === 'string' ? detail : 'Error ' + res.status);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function load() {
    try {
      _state = await api('GET', '/api/menu/sede');
    } catch (e) {
      if (e.status === 400) message('Elegí una sede en el selector de la barra lateral para ver y cambiar su carta.');
      else if (e.status === 403) message('Solo el dueño, un admin o el gerente de la sede pueden cambiar la carta.');
      else message('No se pudo cargar la carta de la sede: ' + e.message);
      return;
    }
    render();
  }

  function overrideFor(name) {
    var key = (name || '').trim().toLowerCase();
    var list = (_state && _state.overrides) || [];
    for (var i = 0; i < list.length; i++) {
      if ((list[i].dish_name || '').trim().toLowerCase() === key) return list[i];
    }
    return null;
  }

  async function saveOverride(name, price, hidden) {
    try {
      await api('PUT', '/api/menu/sede/override', { dish_name: name, price: price, hidden: hidden });
      mesioToast('Guardado para esta sede', 'success', 1500);
    } catch (e) {
      mesioToast(e.message, 'error');
    }
    await load();
  }

  function baseDishRow(dish) {
    var ov = overrideFor(dish.name);
    var hidden = !!(ov && ov.hidden);
    var ownPrice = ov && ov.price != null ? ov.price : null;

    var row = el('div', { className: 'dish' + (hidden ? ' off' : ''), style: 'display:flex;align-items:center;gap:10px;flex-wrap:wrap;' });
    var info = el('div', { style: 'flex:1;min-width:140px;' });
    info.appendChild(el('div', { className: 'dish-name' }, dish.name || ''));
    var meta = el('div', { className: 'dish-meta' }, 'General: ' + mesioFmt(dish.price || 0));
    if (ownPrice != null) {
      meta.appendChild(el('span', { style: 'margin-left:8px;color:var(--brand);font-weight:600;' }, '· Precio propio'));
    }
    info.appendChild(meta);
    row.appendChild(info);

    var priceInput = el('input', {
      type: 'number', min: '0', step: 'any', inputmode: 'decimal',
      placeholder: 'Precio general', 'aria-label': 'Precio en esta sede',
      style: 'width:130px;',
    });
    if (ownPrice != null) priceInput.value = String(ownPrice);
    priceInput.addEventListener('change', function () {
      var raw = priceInput.value.trim();
      saveOverride(dish.name, raw === '' ? null : raw, hidden);
    });
    row.appendChild(priceInput);

    var label = el('label', { className: 'toggle', title: 'Se vende en esta sede' });
    var box = el('input', { type: 'checkbox' });
    box.checked = !hidden;
    box.addEventListener('change', function () {
      saveOverride(dish.name, ownPrice, !box.checked);
    });
    label.appendChild(box);
    label.appendChild(el('span', { className: 'toggle-slider' }));
    row.appendChild(label);
    return row;
  }

  function section(title, sub) {
    var card = el('div', { className: 'card', style: 'margin-bottom:14px;padding:14px 16px;' });
    card.appendChild(el('div', { style: 'font-weight:600;' }, title));
    if (sub) card.appendChild(el('div', { style: 'font-size:12px;color:var(--text-3);margin:2px 0 10px;' }, sub));
    return card;
  }

  function renderBase(container) {
    var base = (_state && _state.base) || {};
    var cats = Object.keys(base).filter(function (c) { return Array.isArray(base[c]) && base[c].length; });
    if (!cats.length) {
      container.appendChild(el('div', { style: 'color:var(--text-3);font-size:13px;' }, 'La carta general está vacía.'));
      return;
    }
    cats.forEach(function (cat) {
      container.appendChild(el('div', { style: 'font-size:12px;font-weight:600;color:var(--text-3);margin:12px 0 6px;text-transform:uppercase;' }, cat));
      base[cat].forEach(function (dish) { container.appendChild(baseDishRow(dish)); });
    });
  }

  function categoryOptions() {
    var names = {};
    Object.keys((_state && _state.menu) || {}).forEach(function (c) { names[c] = true; });
    Object.keys((_state && _state.base) || {}).forEach(function (c) { names[c] = true; });
    return Object.keys(names);
  }

  function ownDishForm(existing) {
    var form = el('form', { style: 'display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-top:10px;' });
    var name = el('input', { type: 'text', placeholder: 'Nombre del plato', required: 'required', maxlength: '200' });
    var cat = el('input', { type: 'text', placeholder: 'Categoría', required: 'required', maxlength: '100', list: 'sede-carta-cats' });
    var price = el('input', { type: 'number', min: '0', step: 'any', placeholder: 'Precio', required: 'required' });
    var desc = el('input', { type: 'text', placeholder: 'Descripción (opcional)', maxlength: '500' });
    var list = el('datalist', { id: 'sede-carta-cats' });
    categoryOptions().forEach(function (c) { list.appendChild(el('option', { value: c })); });
    if (existing) {
      name.value = existing.dish.name || '';
      cat.value = existing.category || '';
      price.value = existing.dish.price != null ? String(existing.dish.price) : '';
      desc.value = existing.dish.description || '';
    }
    var submit = el('button', { type: 'submit', className: 'btn primary' }, existing ? 'Guardar cambios' : 'Agregar plato');
    [name, cat, price, desc, list, submit].forEach(function (n) { form.appendChild(n); });

    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      submit.disabled = true;
      var dish = Object.assign({}, existing ? existing.dish : {}, {
        name: name.value.trim(), price: price.value.trim(), description: desc.value.trim(),
      });
      try {
        await api('PUT', '/api/menu/sede/dish', {
          category: cat.value.trim(), dish: dish,
          previous_name: existing ? existing.dish.name : null,
        });
        mesioToast(existing ? 'Plato actualizado' : 'Plato agregado a esta sede', 'success', 1500);
        await load();
      } catch (e) {
        mesioToast(e.message, 'error');
        submit.disabled = false;
      }
    });
    return form;
  }

  function renderOwn(container) {
    var own = (_state && _state.own_dishes) || [];
    if (!own.length) {
      container.appendChild(el('div', { style: 'color:var(--text-3);font-size:13px;' }, 'Esta sede todavía no tiene platos propios.'));
    }
    own.forEach(function (item) {
      var row = el('div', { className: 'dish', style: 'display:flex;align-items:center;gap:10px;flex-wrap:wrap;' });
      var info = el('div', { style: 'flex:1;min-width:140px;' });
      info.appendChild(el('div', { className: 'dish-name' }, item.dish.name || ''));
      info.appendChild(el('div', { className: 'dish-meta' }, (item.category || '') + ' · ' + mesioFmt(item.dish.price || 0)));
      row.appendChild(info);

      var edit = el('button', { type: 'button', className: 'btn' }, 'Editar');
      var del = el('button', { type: 'button', className: 'btn' }, 'Quitar');
      edit.addEventListener('click', function () {
        if (row.nextSibling && row.nextSibling.tagName === 'FORM') { row.nextSibling.remove(); return; }
        row.parentNode.insertBefore(ownDishForm(item), row.nextSibling);
      });
      del.addEventListener('click', async function () {
        var ok = await mesioConfirm('Quitar "' + (item.dish.name || '') + '" de la carta de esta sede.', { confirmText: 'Quitar', danger: true });
        if (!ok) return;
        try {
          await api('DELETE', '/api/menu/sede/dish?dish_name=' + encodeURIComponent(item.dish.name || ''));
          mesioToast('Plato quitado', 'success', 1500);
        } catch (e) {
          mesioToast(e.message, 'error');
        }
        await load();
      });
      row.appendChild(edit);
      row.appendChild(del);
      container.appendChild(row);
    });
    container.appendChild(ownDishForm(null));
  }

  function renderOrphans(container) {
    var orphans = ((_state && _state.overrides) || []).filter(function (o) { return o.orphaned; });
    if (!orphans.length) return false;
    orphans.forEach(function (o) {
      var row = el('div', { className: 'dish', style: 'display:flex;align-items:center;gap:10px;' });
      row.appendChild(el('div', { className: 'dish-name', style: 'flex:1;' }, o.dish_name));
      var clear = el('button', { type: 'button', className: 'btn' }, 'Quitar cambio');
      clear.addEventListener('click', function () { saveOverride(o.dish_name, null, false); });
      row.appendChild(clear);
      container.appendChild(row);
    });
    return true;
  }

  function render() {
    body.textContent = '';

    var base = section('Carta general en esta sede',
      'Poné un precio solo para esta sede (vacío = el precio general) o apagá lo que esta sede no vende. ' +
      'Un precio propio se mantiene aunque cambie el precio general.');
    renderBase(base);
    body.appendChild(base);

    var own = section('Platos solo de esta sede', 'Las demás sedes no los ven.');
    renderOwn(own);
    body.appendChild(own);

    var orphans = section('Cambios de platos que ya no están en la carta general',
      'El plato se renombró o se quitó de la carta general; este cambio ya no aplica.');
    if (renderOrphans(orphans)) body.appendChild(orphans);
  }

  // Loaded the first time its tab opens — most visits never need it.
  document.querySelectorAll('[data-tab="sede"]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (_loaded) return;
      _loaded = true;
      load();
    });
  });
})();
