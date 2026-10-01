/* ── Menu Admin page ─────────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // ── Tab switching ─────────────────────────────────────────────────
  function switchTab(tab) {
    document.querySelectorAll('[data-tab]').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    ['disp', 'inv', 'esc', 'sede'].forEach(function (t) {
      const el = document.getElementById('tab-' + t);
      if (el) el.style.display = t === tab ? '' : 'none';
    });
  }

  document.querySelectorAll('[data-tab]').forEach(function (btn) {
    btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
  });

  // ── Availability sub-filters ────────────────────────────────────
  // State: 'all' | 'available' | 'unavailable'
  var _menuFilter = 'all';
  var _rawCategories = null; // last full category dict from loadMenu

  var dispFilterBtns = document.querySelectorAll('#tab-disp .seg:first-of-type .seg-btn, #tab-disp .seg .seg-btn');
  // Scope only to the filter row seg (not the top tab seg)
  var dispFilterSeg = null;
  (function () {
    var rows = document.querySelectorAll('#tab-disp .row');
    if (rows[0]) dispFilterSeg = rows[0].querySelector('.seg');
  })();

  if (dispFilterSeg) {
    var filterBtns = dispFilterSeg.querySelectorAll('.seg-btn');
    filterBtns.forEach(function (btn, idx) {
      btn.addEventListener('click', function () {
        filterBtns.forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        _menuFilter = idx === 0 ? 'all' : idx === 1 ? 'available' : 'unavailable';
        if (_rawCategories) renderMenu(_rawCategories);
      });
    });
  }

  function _applyMenuFilter(categories) {
    if (_menuFilter === 'all') return categories;
    var out = {};
    Object.keys(categories).forEach(function (cat) {
      var dishes = (categories[cat] || []).filter(function (d) {
        var avail = d.available !== false;
        return _menuFilter === 'available' ? avail : !avail;
      });
      if (dishes.length) out[cat] = dishes;
    });
    return out;
  }

  // ── Render menu ───────────────────────────────────────────────────

  function initToggle(input) {
    input.addEventListener('change', function () {
      var dish = input.closest('.dish');
      if (dish) dish.classList.toggle('off', !input.checked);
      saveDishAvailability(input);
    });
  }

  async function saveDishAvailability(input) {
    var dish = input.closest('.dish');
    if (!dish) return;
    var name = dish.querySelector('.dish-name');
    if (!name) return;
    try {
      var res = await fetch('/api/menu/availability', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
        body: JSON.stringify({ dish_name: name.textContent.trim(), available: input.checked })
      });
      if (!res.ok) throw new Error('status ' + res.status);
    } catch (e) {
      input.checked = !input.checked;
      var dishEl = input.closest('.dish');
      if (dishEl) dishEl.classList.toggle('off', !input.checked);
      mesioToast('Error al guardar disponibilidad', 'error');
    }
  }

  function _updateMenuCounter(categories) {
    var el = document.getElementById('menu-counter');
    if (!el) return;
    if (!categories || !Object.keys(categories).length) {
      el.textContent = '0 platos';
      return;
    }
    var total = 0, avail = 0, paused = 0;
    Object.values(categories).forEach(function (dishes) {
      (dishes || []).forEach(function (d) {
        total += 1;
        if (d.active === false) { paused += 1; }
        else if (d.available !== false) { avail += 1; }
      });
    });
    el.textContent = avail + ' / ' + total + ' disponibles' + (paused ? ' · ' + paused + ' pausados' : '');
  }

  function renderMenu(categories) {
    // Always update counter with the full (unfiltered) categories
    _updateMenuCounter(categories);

    var filtered = _applyMenuFilter(categories);
    var container = document.getElementById('live-menu-container');
    if (!container) return;

    if (!filtered || !Object.keys(filtered).length) {
      container.innerHTML = '<div style="padding:24px;color:var(--text-3);">' +
        (_menuFilter === 'all' ? '(sin platos)' : 'No hay platos con ese filtro.') + '</div>';
      container.setAttribute('data-loaded', 'true');
      return;
    }

    container.innerHTML = Object.keys(filtered).map(function (cat) {
      var dishes = filtered[cat];
      var dishHtml = dishes.map(function (d) {
        var available = d.available !== false;
        var initials = (d.name || '?')[0].toUpperCase();
        var price = mesioFmt(d.price || 0);
        var thumbContent = d.image_url
          ? '<img src="' + _escHtml(mesioImageUrl(d.image_url, 'thumb')) + '" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:8px;">'
          : _escHtml(initials);
        return '<div class="dish ' + (available ? '' : 'off') + '" data-dish-name="' + _escHtml(d.name || '') + '">' +
          '<div class="dish-thumb" style="background:linear-gradient(135deg,var(--brand),#0F6E56);position:relative;overflow:hidden;">' +
          thumbContent +
          '<button class="dish-img-btn" data-img-dish="' + _escHtml(d.name || '') + '" aria-label="Imagen" title="Gestionar imagen" style="position:absolute;top:2px;right:2px;background:rgba(0,0,0,.45);border:none;border-radius:4px;padding:2px 4px;font-size:10px;cursor:pointer;color:#fff;line-height:1;">📷</button>' +
          '</div>' +
          '<div>' +
          '<div class="dish-name">' + _escHtml(d.name || '') + '</div>' +
          '<div class="dish-meta">' + price + '</div>' +
          '</div>' +
          '<label class="toggle" style="margin-left:auto;">' +
          '<input type="checkbox"' + (available ? ' checked' : '') + '>' +
          '<span class="toggle-slider"></span></label>' +
          '</div>';
      }).join('');

      return '<div class="card flush" style="margin-bottom:14px;">' +
        '<div class="cat-head" style="padding:12px 16px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:space-between;">' +
        '<span>' + _escHtml(cat) + '</span>' +
        '<span class="cat-arrow open">▾</span>' +
        '</div>' +
        '<div class="dish-grid">' + dishHtml + '</div>' +
        '</div>';
    }).join('');

    container.querySelectorAll('.cat-head').forEach(function (head) {
      head.addEventListener('click', function () {
        var grid = head.closest('.card').querySelector('.dish-grid');
        var arrow = head.querySelector('.cat-arrow');
        if (grid) {
          var hidden = grid.style.display === 'none';
          grid.style.display = hidden ? '' : 'none';
          if (arrow) arrow.classList.toggle('open', hidden);
        }
      });
    });

    container.querySelectorAll('.toggle input').forEach(initToggle);

    container.querySelectorAll('.dish-img-btn').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        e.preventDefault();
        openImageModal(btn.dataset.imgDish, categories);
      });
    });

    container.setAttribute('data-loaded', 'true');
  }

  function buildCategoriesFromMenu(menuData) {
    if (!menuData) return {};
    if (Array.isArray(menuData)) return { 'Menú': menuData };
    return menuData;
  }

  async function loadMenu() {
    try {
      var res = await fetch('/api/dashboard/menu', { headers: mesioHeaders() });
      if (!res.ok) return;
      var data = await res.json();
      var menuRaw = data.menu || data.categories || data;
      _rawCategories = buildCategoriesFromMenu(menuRaw);
      renderMenu(_rawCategories);
    } catch (e) {
      console.error('menu-admin: load menu error', e);
    }
  }

  // ── Visual onboarding: photo coverage widget ─────────────────────────
  // Renders a banner above the dish grid showing how many dishes still
  // lack a photo. Clicking a missing dish scrolls to its row so the admin
  // can hit the existing 📷 button. Refreshes after every upload/delete.
  async function loadPhotoCoverage() {
    var widget = document.getElementById('photo-coverage-widget');
    if (!widget) return;
    try {
      var res = await fetch('/api/menu/missing-photos', { headers: mesioHeaders() });
      if (!res.ok) {
        widget.style.display = 'none';
        return;
      }
      var data = await res.json();
      _renderPhotoCoverage(widget, data);
    } catch (e) {
      widget.style.display = 'none';
    }
  }

  function _renderPhotoCoverage(widget, data) {
    var total   = (data && data.total_dishes)  | 0;
    var withPh  = (data && data.with_photo)    | 0;
    var missing = (data && data.missing) || [];

    // Empty menu — nothing to nudge.
    if (total === 0) {
      widget.style.display = 'none';
      widget.setAttribute('data-loaded', 'true');
      return;
    }

    // 100% coverage — celebrate briefly, then hide on next render.
    if (missing.length === 0) {
      widget.style.display = '';
      widget.innerHTML = '';
      var done = document.createElement('div');
      done.className = 'card flush';
      done.style.cssText = 'padding:12px 16px;display:flex;align-items:center;gap:10px;border-left:3px solid var(--brand);';
      var doneIcon = document.createElement('span');
      doneIcon.textContent = '✅';
      var doneText = document.createElement('div');
      doneText.style.cssText = 'font-size:13px;color:var(--text-2);';
      doneText.textContent = 'Todos los platos del menú tienen foto (' + total + '/' + total + ').';
      done.appendChild(doneIcon);
      done.appendChild(doneText);
      widget.appendChild(done);
      widget.setAttribute('data-loaded', 'true');
      return;
    }

    widget.style.display = '';
    widget.innerHTML = '';

    var card = document.createElement('div');
    card.className = 'card flush';
    card.style.cssText = 'padding:12px 16px;border-left:3px solid #d97706;';

    // Header line
    var header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:10px;cursor:pointer;';

    var headerLeft = document.createElement('div');
    headerLeft.style.cssText = 'display:flex;align-items:center;gap:10px;font-size:13px;color:var(--text-2);';
    var icon = document.createElement('span');
    icon.textContent = '📷';
    var label = document.createElement('span');
    label.textContent = withPh + ' de ' + total + ' platos tienen foto. Faltan ' + missing.length + '.';
    headerLeft.appendChild(icon);
    headerLeft.appendChild(label);

    var arrow = document.createElement('span');
    arrow.textContent = '▾';
    arrow.style.cssText = 'font-size:12px;color:var(--text-3);transition:transform .15s;';

    header.appendChild(headerLeft);
    header.appendChild(arrow);
    card.appendChild(header);

    // List of missing dishes (collapsible)
    var list = document.createElement('div');
    list.style.cssText = 'display:none;margin-top:10px;flex-wrap:wrap;gap:6px;';
    missing.forEach(function (m) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.style.cssText = 'background:var(--surface-2);border:1px solid var(--border);border-radius:14px;padding:4px 10px;font-size:12px;color:var(--text-2);cursor:pointer;';
      // textContent for user-controlled data — names/categories from DB.
      chip.textContent = (m.category || '') + ' · ' + (m.name || '');
      chip.addEventListener('click', function (ev) {
        ev.stopPropagation();
        _scrollToMissingDish(m.name);
      });
      list.appendChild(chip);
    });
    card.appendChild(list);

    header.addEventListener('click', function () {
      var open = list.style.display !== 'none';
      list.style.display = open ? 'none' : 'flex';
      arrow.style.transform = open ? '' : 'rotate(180deg)';
    });

    widget.appendChild(card);
    widget.setAttribute('data-loaded', 'true');
  }

  function _scrollToMissingDish(name) {
    if (!name) return;
    // Dish rows carry data-dish-name set by renderMenu.
    var sel = '[data-dish-name="' + name.replace(/"/g, '\\"') + '"]';
    var el = document.querySelector(sel);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.style.transition = 'box-shadow .25s';
    el.style.boxShadow = '0 0 0 2px var(--brand)';
    setTimeout(function () { el.style.boxShadow = ''; }, 1400);
  }

  // ── Inventory modal ────────────────────────────────────────────────
  var _invItems = [];

  // Stock belongs to ONE sede (PM 2026-09-20), so the inventory screen has
  // to know the org's sedes: to make the owner pick one before adding a
  // product, and to offer a destination for a transfer. `_invSedes` stays
  // empty for a single-sede restaurant and for staff, who never choose.
  var _invSedes = [];
  var _invCurrentSede = null;

  async function loadInvSedes() {
    try {
      // /api/staff/locations, not /api/team/branches: the latter is
      // owner-only, and a gerente also needs the list to pick a transfer
      // destination. This one answers for anyone authenticated in the org.
      var res = await fetch('/api/staff/locations', { headers: mesioHeaders() });
      if (!res.ok) { _invSedes = []; return; }
      var data = await res.json();
      var list = data.branches || data.locations || data || [];
      _invSedes = Array.isArray(list) ? list : [];
    } catch (e) {
      _invSedes = [];
    }
  }

  function _fillSedeSelect(selectEl, selectedId, excludeId) {
    if (!selectEl) return;
    selectEl.innerHTML = '';
    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Elegí una sede…';
    selectEl.appendChild(placeholder);
    _invSedes.forEach(function (sede) {
      if (excludeId != null && String(sede.id) === String(excludeId)) return;
      var opt = document.createElement('option');
      opt.value = String(sede.id);
      // textContent, never innerHTML — the sede name is user data.
      opt.textContent = sede.name || ('Sede ' + sede.id);
      if (selectedId != null && String(sede.id) === String(selectedId)) opt.selected = true;
      selectEl.appendChild(opt);
    });
  }

  function _sedeNameById(id) {
    for (var i = 0; i < _invSedes.length; i++) {
      if (String(_invSedes[i].id) === String(id)) return _invSedes[i].name || ('Sede ' + id);
    }
    return '';
  }

  // More than one sede AND no single sede pinned by the backend = an owner
  // looking at everything. That is exactly who must choose before saving.
  function _mustPickSede() {
    return _invSedes.length > 1 && _invCurrentSede == null;
  }

  function openInvModal(item, focusStock) {
    var modal = document.getElementById('invModal');
    if (!modal) return;
    document.getElementById('invModalId').value = item ? (item.id || '') : '';
    document.getElementById('invModalTitle').textContent = item ? 'Editar producto' : 'Nuevo producto';
    document.getElementById('invModalName').value = item ? (item.name || '') : '';
    document.getElementById('invModalUnit').value = item ? (item.unit || '') : '';
    document.getElementById('invModalStock').value = item
      ? (item.stock != null ? item.stock : (item.current_stock != null ? item.current_stock : ''))
      : '';
    document.getElementById('invModalMin').value = item
      ? (item.low_stock_threshold != null ? item.low_stock_threshold : (item.min_stock != null ? item.min_stock : ''))
      : '';
    document.getElementById('invModalCost').value = item ? (item.cost_per_unit != null ? item.cost_per_unit : '') : '';

    // The sede row only appears when there is a real choice to make: a new
    // product, several sedes, and no sede already pinned. Editing never
    // moves a product between sedes — that is what a transfer is for.
    var sedeRow = document.getElementById('invModalSedeRow');
    var sedeSel = document.getElementById('invModalSede');
    var needsSede = !item && _mustPickSede();
    if (sedeRow) sedeRow.style.display = needsSede ? '' : 'none';
    if (needsSede) _fillSedeSelect(sedeSel, null, null);
    else if (sedeSel) sedeSel.value = '';

    modal.style.display = 'flex';
    if (focusStock) {
      setTimeout(function () { document.getElementById('invModalStock').focus(); }, 60);
    }
  }

  function closeInvModal() {
    var modal = document.getElementById('invModal');
    if (modal) modal.style.display = 'none';
  }

  async function saveInvModal() {
    var id = document.getElementById('invModalId').value;
    var name = document.getElementById('invModalName').value.trim();
    var unit = document.getElementById('invModalUnit').value.trim() || 'unidades';
    var stock = parseFloat(document.getElementById('invModalStock').value) || 0;
    var min = parseFloat(document.getElementById('invModalMin').value) || 0;
    var cost = parseFloat(document.getElementById('invModalCost').value) || 0;

    if (!name) { mesioToast('El nombre es requerido', 'warn'); return; }

    // The backend refuses a create with no sede (400). Catching it here just
    // saves the round-trip and points at the field.
    var sedeSel = document.getElementById('invModalSede');
    var sedeId = (!id && _mustPickSede()) ? (sedeSel ? sedeSel.value : '') : '';
    if (!id && _mustPickSede() && !sedeId) {
      mesioToast('Elegí la sede a la que pertenece este producto', 'warn');
      if (sedeSel) sedeSel.focus();
      return;
    }

    var saveBtn = document.getElementById('invModalSave');
    if (saveBtn) saveBtn.disabled = true;

    try {
      var url = id ? '/api/inventory/' + id : '/api/inventory';
      var method = id ? 'PUT' : 'POST';
      var body = { name: name, unit: unit, current_stock: stock, min_stock: min, cost_per_unit: cost };
      if (sedeId) body.location_id = parseInt(sedeId, 10);
      var res = await fetch(url, {
        method: method,
        headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
        body: JSON.stringify(body)
      });
      if (!res.ok) {
        var err = await res.json().catch(function () { return {}; });
        throw new Error(err.detail || 'HTTP ' + res.status);
      }
      mesioToast(id ? 'Producto actualizado' : 'Producto agregado', 'success');
      closeInvModal();
      loadInventory();
    } catch (e) {
      mesioToast('Error: ' + e.message, 'error');
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  var invModalClose = document.getElementById('invModalClose');
  var invModalCancel = document.getElementById('invModalCancel');
  var invModalSave = document.getElementById('invModalSave');
  var invModalOverlay = document.getElementById('invModal');
  if (invModalClose) invModalClose.addEventListener('click', closeInvModal);
  if (invModalCancel) invModalCancel.addEventListener('click', closeInvModal);
  if (invModalSave) invModalSave.addEventListener('click', saveInvModal);
  if (invModalOverlay) {
    invModalOverlay.addEventListener('click', function (e) {
      if (e.target === invModalOverlay) closeInvModal();
    });
  }

  // + Add product button
  var addInvBtn = document.getElementById('btn-add-inventory');
  if (addInvBtn) addInvBtn.addEventListener('click', function () { openInvModal(null); });

  // ── Inventory category pills ──────────────────────────────────────
  var _invCategoryFilter = 'all';

  function _buildCategoryPills(items) {
    var seg = document.querySelector('#tab-inv .seg');
    if (!seg) return;

    // Collect distinct categories from loaded data
    var cats = [];
    var seen = {};
    (items || []).forEach(function (it) {
      var c = (it.category || '').trim();
      if (c && !seen[c]) { seen[c] = true; cats.push(c); }
    });

    if (!cats.length) {
      // No categories — hide the filter row entirely
      var filterRow = seg.closest('.row');
      if (filterRow) filterRow.style.display = 'none';
      return;
    }

    var filterRow = seg.closest('.row');
    if (filterRow) filterRow.style.display = '';

    // Rebuild pills: Todo + one per distinct category
    var pillsHtml = '<button class="seg-btn' + (_invCategoryFilter === 'all' ? ' active' : '') + '" data-cat-pill="all">Todo</button>';
    cats.forEach(function (c) {
      pillsHtml += '<button class="seg-btn' + (_invCategoryFilter === c ? ' active' : '') + '" data-cat-pill="' + _escHtml(c) + '">' + _escHtml(c) + '</button>';
    });
    seg.innerHTML = pillsHtml;

    seg.querySelectorAll('[data-cat-pill]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        seg.querySelectorAll('[data-cat-pill]').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        _invCategoryFilter = btn.dataset.catPill;
        renderInventory(_invItems);
      });
    });
  }

  // ── Render inventory ──────────────────────────────────────────────

  function renderInventory(items) {
    var body = document.getElementById('inv-body');
    if (!body) return;

    // Filter by selected category
    var filtered = (_invCategoryFilter === 'all')
      ? items
      : (items || []).filter(function (it) {
          return (it.category || '').trim() === _invCategoryFilter;
        });

    // Inventory alert strip
    var alertEl = document.getElementById('inv-alert-strip');
    if (alertEl) {
      var listForAlert = Array.isArray(items) ? items : [];
      var critical = listForAlert.filter(function (it) {
        var stock = +(it.stock || it.quantity || it.current_stock || 0);
        var low   = +(it.low_stock_threshold || it.min_stock || 0);
        return stock <= 0 || (low > 0 && stock <= low);
      });
      if (!critical.length) {
        alertEl.style.display = 'none';
        alertEl.innerHTML = '';
      } else {
        var names = critical.slice(0, 5).map(function (it) {
          var stock = +(it.stock || it.quantity || it.current_stock || 0);
          var tag = stock <= 0 ? 'agotado' : 'bajo mínimo';
          return _escHtml(it.name || it.sku || '') + ' ' + tag;
        }).join(' · ');
        var extra = critical.length > 5 ? ' · +' + (critical.length - 5) + ' más' : '';
        alertEl.style.display = '';
        alertEl.className = 'alert-strip';
        alertEl.innerHTML =
          '<span style="font-size:18px;line-height:1;">⚠️</span>' +
          '<div style="flex:1;">' +
            '<div style="font-size:13px;font-weight:600;color:var(--warning-text);">' +
              critical.length + ' producto' + (critical.length === 1 ? '' : 's') +
              ' requiere' + (critical.length === 1 ? '' : 'n') + ' atención inmediata' +
            '</div>' +
            '<div style="font-size:12px;color:var(--text-2);margin-top:3px;">' + names + extra + '</div>' +
          '</div>' +
          '<button class="btn sm ghost" id="btn-ordenar-compra" style="white-space:nowrap;">Ordenar compra</button>';

        var ordenarBtn = document.getElementById('btn-ordenar-compra');
        if (ordenarBtn) {
          ordenarBtn.addEventListener('click', function () {
            var lines = critical.map(function (it) {
              var stock = +(it.stock || it.quantity || it.current_stock || 0);
              return (it.name || it.sku || '') + ' — stock actual ' + stock + ' ' + (it.unit || 'u');
            }).join('\n');
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(lines).then(function () {
                mesioToast('Lista de compra copiada al portapapeles', 'success');
              }).catch(function () {
                window.print();
              });
            } else {
              window.print();
            }
          });
        }
      }
    }

    // inv-summary
    var summaryEl = document.getElementById('inv-summary');
    if (summaryEl) {
      var list = Array.isArray(items) ? items : [];
      var totalValue = 0;
      list.forEach(function (item) {
        var stock = +(item.stock || item.quantity || item.current_stock || 0);
        var cost  = +(item.cost_per_unit || item.unit_cost || 0);
        totalValue += stock * cost;
      });
      summaryEl.textContent = list.length + ' producto' + (list.length === 1 ? '' : 's') +
        ' · ' + mesioFmt(totalValue) + ' valor stock';
    }

    if (!filtered || !filtered.length) {
      body.innerHTML = '<div style="padding:18px;color:var(--text-3);">' +
        ((!items || !items.length) ? '(sin inventario)' : 'No hay productos en esta categoría.') + '</div>';
      body.setAttribute('data-loaded', 'true');
      return;
    }

    body.innerHTML = filtered.map(function (item) {
      var stock = +(item.stock || item.quantity || item.current_stock || 0);
      var low   = +(item.low_stock_threshold || item.min_stock || 0);
      var stockCls = stock <= 0 ? 'danger' : (low > 0 && stock <= low) ? 'warn' : '';
      var unit = item.unit || 'u';
      var linked = Array.isArray(item.linked_dishes) ? item.linked_dishes : [];
      var linkedText = linked.length ? linked.slice(0, 2).map(function (d) { return _escHtml(d); }).join(', ') + (linked.length > 2 ? ' +' + (linked.length - 2) : '') : '—';
      var statusLabel = stock <= 0 ? '<span class="danger">Agotado</span>'
        : (low > 0 && stock <= low) ? '<span class="warn">Bajo mínimo</span>'
        : '<span class="brand">OK</span>';
      return '<div class="inv-row">' +
        '<div>' +
          '<div style="font-weight:500;">' + _escHtml(item.name || item.sku || '') + '</div>' +
          '<div style="font-size:11px;color:var(--text-3);">' + _escHtml(item.category || '') + '</div>' +
        '</div>' +
        '<div class="num"><span class="' + stockCls + '">' + stock + ' ' + _escHtml(unit) + '</span></div>' +
        '<div class="num">' + (low > 0 ? low + ' ' + _escHtml(unit) : '—') + '</div>' +
        '<div style="font-size:12px;color:var(--text-3);">' + linkedText + '</div>' +
        '<div>' + statusLabel + '</div>' +
        '<div style="text-align:right;">' +
          '<button class="btn sm ghost" data-inv-action="restock" data-id="' + (item.id || '') + '">Reponer</button> ' +
          (_invSedes.length > 1
            ? '<button class="btn sm ghost" data-inv-action="transfer" data-id="' + (item.id || '') + '">Trasladar</button> '
            : '') +
          '<button class="btn sm ghost" data-inv-action="edit" data-id="' + (item.id || '') + '">Editar</button>' +
        '</div>' +
      '</div>';
    }).join('');

    body.setAttribute('data-loaded', 'true');
  }

  // Delegated listener on #inv-body for restock / edit / delete actions
  var invBodyEl = document.getElementById('inv-body');
  if (invBodyEl) {
    invBodyEl.addEventListener('click', async function (e) {
      var btn = e.target.closest('[data-inv-action]');
      if (!btn) return;
      e.stopPropagation();
      var itemId = btn.dataset.id;
      var action = btn.dataset.invAction;
      var item = _invItems.find(function (it) { return String(it.id) === String(itemId); });

      if (action === 'restock') {
        // Open the inv modal prefilled, focused on stock field
        openInvModal(item || { id: itemId }, true);

      } else if (action === 'transfer') {
        openInvXferModal(item || { id: itemId });

      } else if (action === 'edit') {
        openInvModal(item || { id: itemId }, false);

      } else if (action === 'delete') {
        var confirmed = await mesioConfirm('Eliminar este producto del inventario. Esta acción no se puede deshacer.', { confirmText: 'Eliminar', danger: true });
        if (!confirmed) return;
        try {
          var res = await fetch('/api/inventory/' + itemId, {
            method: 'DELETE',
            headers: mesioHeaders()
          });
          if (!res.ok) {
            var err = await res.json().catch(function () { return {}; });
            throw new Error(err.detail || 'HTTP ' + res.status);
          }
          mesioToast('Producto eliminado', 'success');
          loadInventory();
        } catch (e2) {
          mesioToast('Error: ' + e2.message, 'error');
        }
      }
    });
  }

  // ── Transfer stock to another sede ─────────────────────────────────
  //
  // "Se puede hacer intercambios de inventario por sede" (PM 2026-09-20).
  // The backend moves it in one transaction and creates the product at the
  // destination if that sede never stocked it, so this only has to collect
  // where and how much.
  var _invXferItem = null;

  function openInvXferModal(item) {
    var modal = document.getElementById('invXferModal');
    if (!modal || !item) return;
    _invXferItem = item;

    var from = item.location_id != null ? _sedeNameById(item.location_id) : '';
    var stock = +(item.stock || item.current_stock || 0);
    var fromEl = document.getElementById('invXferFrom');
    if (fromEl) {
      fromEl.textContent = (item.name || '') +
        (from ? ' · en ' + from : '') +
        ' · disponible ' + stock + ' ' + (item.unit || 'u');
    }
    document.getElementById('invXferId').value = item.id || '';
    document.getElementById('invXferQty').value = '';
    document.getElementById('invXferNote').value = '';
    // Never offer the sede the stock already sits in.
    _fillSedeSelect(document.getElementById('invXferTo'), null, item.location_id);
    modal.style.display = 'flex';
  }

  function closeInvXferModal() {
    var modal = document.getElementById('invXferModal');
    if (modal) modal.style.display = 'none';
    _invXferItem = null;
  }

  async function saveInvXfer() {
    var itemId = document.getElementById('invXferId').value;
    var to = document.getElementById('invXferTo').value;
    var qty = parseFloat(document.getElementById('invXferQty').value);
    var note = document.getElementById('invXferNote').value.trim();

    if (!to) { mesioToast('Elegí la sede de destino', 'warn'); return; }
    if (!qty || qty <= 0) { mesioToast('Indicá una cantidad mayor que cero', 'warn'); return; }

    var btn = document.getElementById('invXferSave');
    if (btn) btn.disabled = true;
    try {
      var res = await fetch('/api/inventory/' + itemId + '/transfer', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
        body: JSON.stringify({ to_location_id: parseInt(to, 10), quantity: qty, note: note })
      });
      if (!res.ok) {
        var err = await res.json().catch(function () { return {}; });
        throw new Error(err.detail || 'HTTP ' + res.status);
      }
      mesioToast('Traslado registrado', 'success');
      closeInvXferModal();
      loadInventory();
    } catch (e) {
      mesioToast('Error: ' + e.message, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  var xferCloseBtn = document.getElementById('invXferClose');
  if (xferCloseBtn) xferCloseBtn.addEventListener('click', closeInvXferModal);
  var xferCancelBtn = document.getElementById('invXferCancel');
  if (xferCancelBtn) xferCancelBtn.addEventListener('click', closeInvXferModal);
  var xferSaveBtn = document.getElementById('invXferSave');
  if (xferSaveBtn) xferSaveBtn.addEventListener('click', saveInvXfer);

  async function loadInventory() {
    try {
      if (!_invSedes.length) await loadInvSedes();
      var res = await fetch('/api/inventory', { headers: mesioHeaders() });
      if (!res.ok) { return; }
      var data = await res.json();
      var items = data.inventory || data.items || data;
      // The response says which sede it is scoped to: an int when the caller
      // is pinned to one (staff, or an owner who picked one), null when they
      // are seeing every sede.
      _invCurrentSede = (data && typeof data.location_id === 'number') ? data.location_id : null;
      _invItems = Array.isArray(items) ? items : [];
      _buildCategoryPills(_invItems);
      renderInventory(_invItems);
    } catch (e) {
      console.error('menu-admin: inventory error', e);
    }
  }

  // ── Image upload modal ────────────────────────────────────────────
  var _imgModalDish = null;
  var _imgModalCategories = null;

  function _findDish(dishName, categories) {
    for (var cat in categories) {
      var list = categories[cat];
      for (var i = 0; i < list.length; i++) {
        if (list[i].name === dishName) return { cat: cat, dish: list[i] };
      }
    }
    return null;
  }

  function openImageModal(dishName, categories) {
    var found = _findDish(dishName, categories);
    if (!found) return;
    _imgModalDish = found.dish;
    _imgModalCategories = categories;

    var modal = document.getElementById('imgModal');
    if (!modal) { _createImageModal(); modal = document.getElementById('imgModal'); }

    var preview = document.getElementById('imgModalPreview');
    if (_imgModalDish.image_url) {
      preview.innerHTML = '';
      var img = document.createElement('img');
      img.src = mesioImageUrl(_imgModalDish.image_url, 'card');
      img.alt = '';
      img.style.cssText = 'max-width:100%;max-height:200px;border-radius:10px;display:block;margin:0 auto 10px;';
      preview.appendChild(img);
    } else {
      preview.textContent = '';
    }

    document.getElementById('imgModalName').textContent = _imgModalDish.name || '';
    document.getElementById('imgModalFileInput').value = '';
    document.getElementById('imgModalDeleteBtn').style.display = _imgModalDish.image_public_id ? '' : 'none';
    modal.style.display = 'flex';
  }

  function _createImageModal() {
    var overlay = document.createElement('div');
    overlay.id = 'imgModal';
    overlay.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;align-items:center;justify-content:center;';

    var box = document.createElement('div');
    box.style.cssText = 'background:#fff;border-radius:16px;padding:1.5rem;width:420px;max-width:95vw;box-shadow:0 24px 60px rgba(0,0,0,.2);';

    var titleRow = document.createElement('div');
    titleRow.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;';
    var title = document.createElement('div');
    title.style.cssText = 'font-weight:700;font-size:.95rem;';
    title.textContent = 'Imagen del plato';
    var closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.cssText = 'background:none;border:none;cursor:pointer;font-size:1.1rem;color:#777;';
    closeBtn.addEventListener('click', function () { overlay.style.display = 'none'; });
    titleRow.appendChild(title);
    titleRow.appendChild(closeBtn);

    var dishNameEl = document.createElement('div');
    dishNameEl.id = 'imgModalName';
    dishNameEl.style.cssText = 'font-size:.82rem;color:#777;margin-bottom:1rem;';

    var preview = document.createElement('div');
    preview.id = 'imgModalPreview';
    preview.style.cssText = 'min-height:40px;margin-bottom:1rem;';

    var hint = document.createElement('div');
    hint.style.cssText = 'border:2px dashed #E0E0D8;border-radius:10px;padding:1.25rem;text-align:center;margin-bottom:1rem;cursor:pointer;';
    hint.innerHTML = '<div style="font-size:.84rem;font-weight:600;margin-bottom:4px;">Haz clic para seleccionar imagen</div><div style="font-size:.75rem;color:#777;">JPG, PNG o WebP · Máx. 5 MB</div>';

    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = 'image/*';
    fileInput.id = 'imgModalFileInput';
    fileInput.style.display = 'none';
    hint.addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function () {
      if (fileInput.files[0]) _handleImageUpload(fileInput.files[0]);
    });

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';

    var uploadBtn = document.createElement('button');
    uploadBtn.id = 'imgModalUploadBtn';
    uploadBtn.style.cssText = 'background:#1D9E75;color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:.84rem;font-weight:600;cursor:pointer;font-family:inherit;';
    uploadBtn.textContent = '📷 Seleccionar y subir';
    uploadBtn.addEventListener('click', function () { fileInput.click(); });

    var deleteBtn = document.createElement('button');
    deleteBtn.id = 'imgModalDeleteBtn';
    deleteBtn.style.cssText = 'background:#FEE2E2;color:#EF4444;border:none;border-radius:8px;padding:8px 16px;font-size:.84rem;font-weight:600;cursor:pointer;font-family:inherit;';
    deleteBtn.textContent = '🗑 Eliminar imagen';
    deleteBtn.addEventListener('click', _handleImageDelete);

    btnRow.appendChild(uploadBtn);
    btnRow.appendChild(deleteBtn);

    box.appendChild(titleRow);
    box.appendChild(dishNameEl);
    box.appendChild(preview);
    box.appendChild(hint);
    box.appendChild(fileInput);
    box.appendChild(btnRow);
    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) overlay.style.display = 'none'; });
    document.body.appendChild(overlay);
  }

  async function _handleImageUpload(file) {
    if (!file.type.startsWith('image/')) { mesioToast('Solo se permiten imágenes (JPG, PNG, WebP)', 'error'); return; }
    if (file.size > 5 * 1024 * 1024) { mesioToast('La imagen supera los 5 MB. Usa una imagen más pequeña.', 'error'); return; }

    var uploadBtn = document.getElementById('imgModalUploadBtn');
    if (uploadBtn) { uploadBtn.disabled = true; uploadBtn.textContent = 'Subiendo…'; }

    try {
      var signRes = await fetch('/api/menu/image/sign', { method: 'POST', headers: mesioHeaders() });
      if (!signRes.ok) {
        var signErr = await signRes.json().catch(function () { return {}; });
        throw new Error(signErr.detail || 'No se pudo firmar el upload');
      }
      var signData = await signRes.json();
      var formData = new FormData();
      formData.append('file', file);
      formData.append('signature', signData.signature);
      formData.append('timestamp', String(signData.timestamp));
      formData.append('api_key', signData.api_key);
      formData.append('folder', signData.folder);
      if (signData.public_id_prefix) formData.append('public_id', signData.public_id_prefix + '_' + Date.now());

      var cloudUrl = 'https://api.cloudinary.com/v1_1/' + encodeURIComponent(signData.cloud_name) + '/image/upload';
      var upRes = await fetch(cloudUrl, { method: 'POST', body: formData });
      if (!upRes.ok) {
        var upErr = await upRes.json().catch(function () { return {}; });
        throw new Error((upErr.error && upErr.error.message) || 'Error al subir a Cloudinary');
      }
      var upData = await upRes.json();

      if (_imgModalDish.image_public_id && _imgModalDish.image_public_id !== upData.public_id) {
        await fetch('/api/menu/image', {
          method: 'DELETE',
          headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
          body: JSON.stringify({ public_id: _imgModalDish.image_public_id })
        }).catch(function () {});
      }

      _imgModalDish.image_url = upData.secure_url;
      _imgModalDish.image_public_id = upData.public_id;

      await _saveMenuWithImage();
      mesioToast('Imagen subida correctamente', 'success');
      document.getElementById('imgModal').style.display = 'none';
      renderMenu(_imgModalCategories);
      loadPhotoCoverage();
    } catch (err) {
      mesioToast('Error subiendo imagen: ' + err.message, 'error');
    } finally {
      if (uploadBtn) { uploadBtn.disabled = false; uploadBtn.textContent = '📷 Seleccionar y subir'; }
    }
  }

  async function _handleImageDelete() {
    if (!_imgModalDish || !_imgModalDish.image_public_id) return;
    var confirmed = await mesioConfirm('Eliminar la imagen de este plato.');
    if (!confirmed) return;

    try {
      await fetch('/api/menu/image', {
        method: 'DELETE',
        headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
        body: JSON.stringify({ public_id: _imgModalDish.image_public_id })
      });
      _imgModalDish.image_url = null;
      _imgModalDish.image_public_id = null;
      await _saveMenuWithImage();
      mesioToast('Imagen eliminada', 'success', 1500);
      document.getElementById('imgModal').style.display = 'none';
      renderMenu(_imgModalCategories);
      loadPhotoCoverage();
    } catch (e) {
      mesioToast('Error al eliminar la imagen', 'error');
    }
  }

  async function _saveMenuWithImage() {
    var res = await fetch('/api/menu/update', {
      method: 'PUT',
      headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
      body: JSON.stringify({ menu: _imgModalCategories })
    });
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(err.detail || 'Error al guardar el menú');
    }
  }

  // ── Edit menu — full visual editor ─────────────────────────────
  async function openCartaEditor() {
    window._dashHeaders = mesioHeaders();

    try {
      var rMenu = await fetch('/api/dashboard/menu', { headers: window._dashHeaders });
      if (!rMenu.ok) throw new Error('HTTP ' + rMenu.status);
      var menu = (await rMenu.json()).menu || {};
      // Collected here, handed over with setMenuItems below: assigning
      // window.MENU_ITEMS does NOT reach the variable openMenuEditor reads
      // (a top-level `let` in a classic script is not a window property),
      // which is why this editor used to open empty.
      var items = [];
      Object.entries(menu).forEach(function (entry) {
        var cat = entry[0], dishes = entry[1];
        if (!Array.isArray(dishes)) return;
        dishes.forEach(function (d) {
          items.push({
            name:            d.name            || '',
            cat:             cat,
            price:           d.price           != null ? d.price : 0,
            desc:            d.description     || '',
            // sku must round-trip or a save from here wipes it (the diner
            // chat's tap-to-cart resolves dishes by it).
            sku:             d.sku             || null,
            image_url:       d.image_url       || null,
            image_public_id: d.image_public_id || null,
            tags:            d.tags            || [],
            badges:          d.badges          || [],
            allergens:       d.allergens       || [],
            featured:        !!d.featured,
            active:          d.active          !== false,
            sort_order:      d.sort_order      != null ? d.sort_order : 999,
            calories:        d.calories        != null ? d.calories   : null,
            prep_time_min:   d.prep_time_min   != null ? d.prep_time_min : null,
          });
        });
      });
    } catch (e) {
      mesioToast('No se pudo cargar la carta: ' + e.message, 'error');
      return;
    }

    if (typeof openMenuEditor !== 'function' || typeof window.setMenuItems !== 'function') {
      mesioToast('Editor no disponible (dashboard-features.js no cargó)', 'error');
      return;
    }
    window.setMenuItems(items);
    openMenuEditor();
  }

  var cartaBtn = document.getElementById('btn-edit-carta');
  if (cartaBtn) cartaBtn.addEventListener('click', openCartaEditor);
  // The general carta is the owner's/admin's (PM 2026-09-21). A gerente
  // changes their own sede's in the "Carta de esta sede" tab.
  var _role = (localStorage.getItem('rb_role') || '').toLowerCase();
  if (cartaBtn && !/owner|admin/.test(_role)) cartaBtn.style.display = 'none';

  // ── Import a carta from a photo or pasted text ──────────────────────────────
  // This produces a DRAFT and nothing else. The parsed dishes are loaded into
  // MENU_ITEMS and handed to the SAME editor as "Editar carta", so they are
  // only written when the owner saves there. A misread price is then a line to
  // fix in an unsaved draft, never a price change on a live carta — which is
  // the whole reason the reader is allowed to be wrong.

  var _importModal   = document.getElementById('importCartaModal');
  var _importFile    = document.getElementById('importCartaFile');
  var _importText    = document.getElementById('importCartaText');
  var _importStatus  = document.getElementById('importCartaStatus');
  var _importGo      = document.getElementById('importCartaGo');
  var _importDraft   = null;   // parsed menu, waiting for the owner to open it

  function _importSetStatus(msg, kind) {
    if (!_importStatus) return;
    _importStatus.textContent = msg || '';
    _importStatus.style.color = kind === 'error' ? 'var(--danger)'
      : kind === 'ok' ? 'var(--brand)' : 'var(--text-3)';
  }

  function _openImportModal() {
    if (!_importModal) return;
    _importDraft = null;
    if (_importFile) _importFile.value = '';
    if (_importText) _importText.value = '';
    if (_importGo) { _importGo.textContent = 'Leer carta'; _importGo.disabled = false; }
    _importSetStatus('');
    _importModal.style.display = 'flex';
  }

  function _closeImportModal() {
    if (_importModal) _importModal.style.display = 'none';
  }

  function _readFileAsBase64(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        // "data:image/png;base64,AAAA" → "AAAA"
        var result = String(reader.result || '');
        var comma  = result.indexOf(',');
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = function () { reject(new Error('No se pudo leer el archivo')); };
      reader.readAsDataURL(file);
    });
  }

  function _draftIntoEditor(menu) {
    var items = [];
    Object.keys(menu).forEach(function (cat) {
      var dishes = menu[cat];
      if (!Array.isArray(dishes)) return;
      dishes.forEach(function (d) {
        items.push({
          name:  d.name  || '',
          cat:   cat,
          price: d.price != null ? d.price : 0,
          desc:  d.description || '',
          // A draft has no sku, no photo and no ordering yet — those are
          // assigned when the owner saves, exactly as for a dish typed by
          // hand. Inventing them here would claim a history the dish lacks.
          sku: null, image_url: null, image_public_id: null,
          tags: [], badges: [], allergens: [],
          featured: false, active: true, sort_order: 999,
          calories: null, prep_time_min: null,
        });
      });
    });
    window._dashHeaders = mesioHeaders();
    if (typeof openMenuEditor !== 'function' || typeof window.setMenuItems !== 'function') {
      mesioToast('Editor no disponible (dashboard-features.js no cargó)', 'error');
      return false;
    }
    window.setMenuItems(items);
    openMenuEditor();
    return true;
  }

  function _describeDraft(data) {
    // Everything the reader was unsure about, said before the owner walks
    // into the editor. Silent warnings are the same as no warnings.
    var needReview = [];
    Object.keys(data.menu || {}).forEach(function (cat) {
      (data.menu[cat] || []).forEach(function (d) {
        if (d.import_warnings && d.import_warnings.length) needReview.push(d.name);
      });
    });
    var lines = ['Leí ' + data.dish_count + ' plato(s).'];
    (data.warnings || []).forEach(function (w) { lines.push(w); });
    if (needReview.length) {
      var shown = needReview.slice(0, 8).join(', ');
      lines.push(
        needReview.length + ' necesitan que revises el precio: ' + shown +
        (needReview.length > 8 ? '…' : '')
      );
    }
    lines.push('Nada se ha guardado todavía.');
    return lines.join(' ');
  }

  async function _runImport() {
    if (!_importGo) return;

    // Second press: the draft is ready and the owner wants to see it.
    if (_importDraft) {
      if (_draftIntoEditor(_importDraft.menu)) _closeImportModal();
      return;
    }

    var file = _importFile && _importFile.files && _importFile.files[0];
    var text = _importText ? _importText.value.trim() : '';
    if (!file && !text) {
      _importSetStatus('Sube una foto o pega el texto de la carta.', 'error');
      return;
    }
    if (file && text) {
      _importSetStatus('Usa una foto o el texto, no ambos.', 'error');
      return;
    }

    _importGo.disabled = true;
    _importSetStatus('Leyendo la carta… puede tardar unos segundos.');

    try {
      var body;
      if (file) {
        body = { image_b64: await _readFileAsBase64(file), image_type: file.type };
      } else {
        body = { text: text };
      }
      var res = await fetch('/api/menu/import', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
        body: JSON.stringify(body),
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) {
        // The reader failing is normal and survivable: the editor is
        // untouched and the owner can still type the carta by hand.
        _importSetStatus(data.detail || 'No pude leer la carta. Puedes escribirla a mano.', 'error');
        _importGo.disabled = false;
        return;
      }
      _importDraft = data;
      _importSetStatus(_describeDraft(data), 'ok');
      _importGo.textContent = 'Abrir en el editor';
      _importGo.disabled = false;
    } catch (e) {
      _importSetStatus('Error de conexión: ' + e.message, 'error');
      _importGo.disabled = false;
    }
  }

  var importBtn = document.getElementById('btn-import-carta');
  if (importBtn) importBtn.addEventListener('click', _openImportModal);
  // Same permission as the base carta: a gerente edits their own sede's.
  if (importBtn && !/owner|admin/.test(_role)) importBtn.style.display = 'none';

  var importClose  = document.getElementById('importCartaClose');
  var importCancel = document.getElementById('importCartaCancel');
  if (importClose)  importClose.addEventListener('click', _closeImportModal);
  if (importCancel) importCancel.addEventListener('click', _closeImportModal);
  if (_importGo)    _importGo.addEventListener('click', _runImport);
  if (_importModal) {
    _importModal.addEventListener('click', function (e) {
      if (e.target === _importModal) _closeImportModal();
    });
  }

  // ── Recipe costing sheets (recipes) ─────────────────────────────────────────
  var _allInventoryForRecipes = []; // populated by loadInventory for the recipe modal select

  async function loadRecipes() {
    var container = document.getElementById('recipes-container');
    if (!container) return;
    try {
      var r = await fetch('/api/inventory/recipes', { headers: mesioHeaders() });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var recipes = (await r.json()).recipes || [];
      renderRecipes(recipes);
    } catch (e) {
      container.innerHTML = '<div style="padding:18px;color:var(--text-3);font-size:13px;">Error al cargar escandallos: ' + _escHtml(e.message) + '</div>';
    }
  }

  function renderRecipes(recipes) {
    var container = document.getElementById('recipes-container');
    if (!container) return;
    if (!recipes || !recipes.length) {
      container.innerHTML =
        '<div style="grid-column:1/-1;padding:32px;text-align:center;color:var(--text-3);font-size:13px;">' +
          'No hay escandallos definidos todavía.<br>' +
          'Usa <strong>+ Nuevo escandallo</strong> para asociar ingredientes a cada plato y calcular food cost.' +
        '</div>';
      return;
    }
    var gradients = [
      'linear-gradient(135deg,#1D9E75,#0F6E56)',
      'linear-gradient(135deg,#F59E0B,#B45309)',
      'linear-gradient(135deg,#3B82F6,#1E40AF)',
      'linear-gradient(135deg,#EF4444,#991B1B)',
      'linear-gradient(135deg,#8B5CF6,#5B21B6)',
    ];
    container.innerHTML = recipes.map(function (rec, idx) {
      var name       = rec.dish_name || rec.name || '—';
      var initial    = (name[0] || '?').toUpperCase();
      var salePrice  = +(rec.sale_price || rec.price || 0);
      var foodCost   = +(rec.food_cost || rec.cost || 0);
      var lines      = Array.isArray(rec.lines) ? rec.lines : (rec.ingredients || []);
      var pct        = salePrice > 0 ? (foodCost / salePrice) * 100 : 0;
      var margin     = Math.max(0, salePrice - foodCost);
      var pctColor   = pct < 20 ? 'var(--brand)' : pct < 30 ? 'var(--warning-text)' : 'var(--danger)';

      var ingredientsHtml = lines.map(function (ln) {
        var iname = ln.ingredient_name || ln.name || ln.sku || '';
        var qty   = ln.quantity != null ? ln.quantity : '—';
        var unit  = ln.unit || '';
        var cost  = ln.line_cost != null ? ln.line_cost : ln.cost;
        var costStr = cost != null ? mesioFmt(+cost) : '';
        return '<div style="display:flex;justify-content:space-between;font-size:12.5px;">' +
          '<span>' + _escHtml(iname) + '</span>' +
          '<span class="mono">' + _escHtml(String(qty)) + ' ' + _escHtml(unit) + (costStr ? ' · ' + costStr : '') + '</span>' +
        '</div>';
      }).join('');

      return '<div class="card">' +
        '<div class="row" style="margin-bottom:10px;">' +
          '<div class="dish-thumb" style="background:' + gradients[idx % gradients.length] + ';">' + _escHtml(initial) + '</div>' +
          '<div>' +
            '<div style="font-size:14px;font-weight:600;">' + _escHtml(name) + '</div>' +
            '<div style="font-size:11.5px;color:var(--text-3);">Precio venta ' + mesioFmt(salePrice) + ' · ' + lines.length + ' ingrediente' + (lines.length === 1 ? '' : 's') + '</div>' +
          '</div>' +
          '<button class="btn sm ghost" data-recipe-edit="' + _escHtml(name) + '" style="margin-left:auto;">Editar</button>' +
        '</div>' +
        (ingredientsHtml
          ? '<div style="display:flex;flex-direction:column;gap:6px;margin:12px 0;padding-top:12px;border-top:0.5px dashed var(--border);">' + ingredientsHtml + '</div>'
          : '<div style="padding:12px 0;color:var(--text-3);font-size:12px;font-style:italic;">Sin ingredientes registrados.</div>'
        ) +
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding-top:12px;border-top:0.5px dashed var(--border);">' +
          '<div><div style="font-size:10.5px;color:var(--text-3);">Food cost</div><div class="mono" style="font-weight:600;">' + mesioFmt(foodCost) + '</div></div>' +
          '<div><div style="font-size:10.5px;color:var(--text-3);">% sobre venta</div><div class="mono" style="font-weight:600;color:' + pctColor + ';">' + pct.toFixed(1) + '%</div></div>' +
          '<div><div style="font-size:10.5px;color:var(--text-3);">Margen bruto</div><div class="mono" style="font-weight:600;">' + mesioFmt(margin) + '</div></div>' +
        '</div>' +
      '</div>';
    }).join('');

    // Delegated listener for "Editar" buttons on recipe cards
    container.querySelectorAll('[data-recipe-edit]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        openRecipeModal(btn.dataset.recipeEdit);
      });
    });
  }

  // ── Recipe modal ──────────────────────────────────────────────────

  function _recipeModalClose() {
    var m = document.getElementById('recipeModal');
    if (m) m.style.display = 'none';
  }

  function _buildDishOptions(selectedDish) {
    var select = document.getElementById('recipeModalDish');
    if (!select) return;

    // Build options from the loaded menu categories
    var cats = _rawCategories || {};
    var options = '<option value="">— Selecciona un plato —</option>';
    Object.keys(cats).forEach(function (cat) {
      (cats[cat] || []).forEach(function (d) {
        var name = d.name || '';
        var sel = name === selectedDish ? ' selected' : '';
        options += '<option value="' + _escHtml(name) + '"' + sel + '>' + _escHtml(name) + '</option>';
      });
    });
    select.innerHTML = options;
  }

  function _addRecipeLine(ingredientId, quantity) {
    var linesEl = document.getElementById('recipeModalLines');
    if (!linesEl) return;

    var row = document.createElement('div');
    row.style.cssText = 'display:grid;grid-template-columns:1fr 100px 32px;gap:8px;align-items:center;';

    // Ingredient selector
    var sel = document.createElement('select');
    sel.className = 'input';
    sel.style.padding = '6px 8px';
    var defOpt = document.createElement('option');
    defOpt.value = '';
    defOpt.textContent = '— Ingrediente —';
    sel.appendChild(defOpt);
    (_invItems.length ? _invItems : _allInventoryForRecipes).forEach(function (it) {
      var opt = document.createElement('option');
      opt.value = it.id || '';
      opt.textContent = (it.name || it.sku || '') + (it.unit ? ' (' + it.unit + ')' : '');
      if (ingredientId && String(it.id) === String(ingredientId)) opt.selected = true;
      sel.appendChild(opt);
    });

    // Quantity input
    var qty = document.createElement('input');
    qty.className = 'input';
    qty.type = 'number';
    qty.min = '0';
    qty.step = '0.001';
    qty.placeholder = 'Cant.';
    qty.style.padding = '6px 8px';
    if (quantity != null) qty.value = quantity;

    // Remove button
    var rmBtn = document.createElement('button');
    rmBtn.type = 'button';
    rmBtn.className = 'btn sm ghost';
    rmBtn.textContent = '✕';
    rmBtn.style.padding = '4px 8px';
    rmBtn.addEventListener('click', function () { row.remove(); });

    row.appendChild(sel);
    row.appendChild(qty);
    row.appendChild(rmBtn);
    linesEl.appendChild(row);
  }

  async function openRecipeModal(dishName) {
    // Ensure we have inventory loaded for the ingredient selects
    if (!_invItems.length) {
      try {
        var r = await fetch('/api/inventory', { headers: mesioHeaders() });
        if (r.ok) {
          var d = await r.json();
          var items = d.inventory || d.items || d;
          _invItems = Array.isArray(items) ? items : [];
        }
      } catch (_) { /* leave _invItems as-is */ }
    }

    var modal = document.getElementById('recipeModal');
    if (!modal) return;

    document.getElementById('recipeModalTitle').textContent = dishName ? 'Editar escandallo' : 'Nuevo escandallo';
    document.getElementById('recipeModalLines').innerHTML = '';

    _buildDishOptions(dishName || '');

    if (dishName) {
      // Lock the dish selector when editing an existing recipe
      var select = document.getElementById('recipeModalDish');
      if (select) select.disabled = true;

      try {
        var res = await fetch('/api/inventory/recipes/' + encodeURIComponent(dishName), { headers: mesioHeaders() });
        if (res.ok) {
          var data = await res.json();
          var lines = data.lines || [];
          lines.forEach(function (ln) {
            _addRecipeLine(ln.ingredient_id, ln.quantity);
          });
        }
      } catch (e) {
        mesioToast('Error al cargar el escandallo: ' + e.message, 'error');
      }
    } else {
      var select2 = document.getElementById('recipeModalDish');
      if (select2) select2.disabled = false;
      // Start with one empty ingredient row
      _addRecipeLine(null, null);
    }

    modal.style.display = 'flex';
  }

  async function saveRecipeModal() {
    var dishSelect = document.getElementById('recipeModalDish');
    var dishName = dishSelect ? dishSelect.value.trim() : '';
    if (!dishName) { mesioToast('Selecciona un plato', 'warn'); return; }

    var linesEl = document.getElementById('recipeModalLines');
    var lineRows = linesEl ? linesEl.querySelectorAll('div') : [];
    var lines = [];
    var valid = true;

    lineRows.forEach(function (row) {
      var sel = row.querySelector('select');
      var qty = row.querySelector('input[type="number"]');
      if (!sel || !qty) return;
      var ingId = parseInt(sel.value, 10);
      var q = parseFloat(qty.value);
      if (!ingId || isNaN(q) || q <= 0) { valid = false; return; }
      lines.push({ ingredient_id: ingId, quantity: q });
    });

    if (!valid || !lines.length) {
      mesioToast('Completa todos los ingredientes con cantidad mayor a 0', 'warn');
      return;
    }

    var saveBtn = document.getElementById('recipeModalSave');
    if (saveBtn) saveBtn.disabled = true;

    try {
      var res = await fetch('/api/inventory/recipes', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
        body: JSON.stringify({ dish_name: dishName, lines: lines })
      });
      if (!res.ok) {
        var err = await res.json().catch(function () { return {}; });
        throw new Error(err.detail || 'HTTP ' + res.status);
      }
      mesioToast('Escandallo guardado', 'success');
      _recipeModalClose();
      loadRecipes();
    } catch (e) {
      mesioToast('Error: ' + e.message, 'error');
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  // Wire recipe modal buttons
  var recipeModalClose = document.getElementById('recipeModalClose');
  var recipeModalCancel = document.getElementById('recipeModalCancel');
  var recipeModalSave = document.getElementById('recipeModalSave');
  var recipeModalOverlay = document.getElementById('recipeModal');
  var recipeModalAddLine = document.getElementById('recipeModalAddLine');

  if (recipeModalClose) recipeModalClose.addEventListener('click', _recipeModalClose);
  if (recipeModalCancel) recipeModalCancel.addEventListener('click', _recipeModalClose);
  if (recipeModalSave) recipeModalSave.addEventListener('click', saveRecipeModal);
  if (recipeModalOverlay) {
    recipeModalOverlay.addEventListener('click', function (e) {
      if (e.target === recipeModalOverlay) _recipeModalClose();
    });
  }
  if (recipeModalAddLine) {
    recipeModalAddLine.addEventListener('click', function () { _addRecipeLine(null, null); });
  }

  // "+ New recipe" button
  var newRecipeBtn = document.getElementById('btn-new-recipe');
  if (newRecipeBtn) {
    newRecipeBtn.addEventListener('click', function () { openRecipeModal(null); });
  }

  // The carta editor (dashboard-features.js) saves on its own; refresh our list.
  document.addEventListener('mesio:menu-saved', function () {
    loadMenu();
    loadPhotoCoverage();
  });

  // ── Boot ──────────────────────────────────────────────────────────
  loadMenu();
  loadPhotoCoverage();
  loadInventory();
  loadRecipes();
})();
