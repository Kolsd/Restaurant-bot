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
    ['disp', 'inv', 'esc'].forEach(function (t) {
      const el = document.getElementById('tab-' + t);
      if (el) el.style.display = t === tab ? '' : 'none';
    });
  }

  document.querySelectorAll('[data-tab]').forEach(function (btn) {
    btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
  });

  // ── Sync branches ─────────────────────────────────────────────────
  var syncBtn = document.getElementById('btn-sync-branches');
  if (syncBtn) {
    syncBtn.addEventListener('click', async function () {
      var ok = await mesioConfirm(
        'Sincronizar menú a todas las sucursales. Los cambios locales de cada sucursal se sobreescribirán.',
        { confirmText: 'Sincronizar', danger: false }
      );
      if (!ok) return;
      syncBtn.disabled = true;
      try {
        var res = await fetch('/api/menu/sync-branches', {
          method: 'POST',
          headers: mesioHeaders()
        });
        if (!res.ok) {
          var err = await res.json().catch(function () { return {}; });
          throw new Error(err.detail || 'HTTP ' + res.status);
        }
        var data = await res.json();
        var n = data.branches_updated != null ? data.branches_updated : 'todas las';
        mesioToast('Menú sincronizado a ' + n + ' sucursales', 'success');
      } catch (e) {
        mesioToast('Error al sincronizar: ' + e.message, 'error');
      } finally {
        syncBtn.disabled = false;
      }
    });
  }

  // ── Disponibilidad sub-filters ────────────────────────────────────
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
          ? '<img src="' + _escHtml(d.image_url) + '" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:8px;">'
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
          '<span></span></label>' +
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

  // ── Inventory modal ────────────────────────────────────────────────
  var _invItems = [];

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

    var saveBtn = document.getElementById('invModalSave');
    if (saveBtn) saveBtn.disabled = true;

    try {
      var url = id ? '/api/inventory/' + id : '/api/inventory';
      var method = id ? 'PUT' : 'POST';
      var body = { name: name, unit: unit, current_stock: stock, min_stock: min, cost_per_unit: cost };
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

  // + Agregar producto button
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

  async function loadInventory() {
    try {
      var res = await fetch('/api/inventory', { headers: mesioHeaders() });
      if (!res.ok) { return; }
      var data = await res.json();
      var items = data.inventory || data.items || data;
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
      img.src = _imgModalDish.image_url;
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

  // ── Editar carta — full visual editor ─────────────────────────────
  async function openCartaEditor() {
    window._dashHeaders = mesioHeaders();

    try {
      var rMenu = await fetch('/api/dashboard/menu', { headers: window._dashHeaders });
      if (!rMenu.ok) throw new Error('HTTP ' + rMenu.status);
      var menu = (await rMenu.json()).menu || {};
      window.MENU_ITEMS = [];
      Object.entries(menu).forEach(function (entry) {
        var cat = entry[0], dishes = entry[1];
        if (!Array.isArray(dishes)) return;
        dishes.forEach(function (d) {
          window.MENU_ITEMS.push({
            name:            d.name            || '',
            cat:             cat,
            price:           d.price           != null ? d.price : 0,
            desc:            d.description     || '',
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

    if (typeof openMenuEditor !== 'function') {
      mesioToast('Editor no disponible (dashboard-features.js no cargó)', 'error');
      return;
    }
    openMenuEditor();
  }

  var cartaBtn = document.getElementById('btn-edit-carta');
  if (cartaBtn) cartaBtn.addEventListener('click', openCartaEditor);

  // ── Escandallos (recipes) ─────────────────────────────────────────
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

  // "+ Nuevo escandallo" button
  var newRecipeBtn = document.getElementById('btn-new-recipe');
  if (newRecipeBtn) {
    newRecipeBtn.addEventListener('click', function () { openRecipeModal(null); });
  }

  // ── Boot ──────────────────────────────────────────────────────────
  loadMenu();
  loadInventory();
  loadRecipes();
})();
