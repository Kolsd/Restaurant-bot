/* ── Menu Admin page ─────────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Tab switching
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
  document.querySelectorAll('.btn.primary').forEach(function (btn) {
    if (btn.textContent.includes('Sincronizar')) {
      btn.addEventListener('click', async function () {
        const ok = await mesioConfirm('Sincronizar menú a todas las sucursales. Los cambios locales de cada sucursal se sobreescribirán.', { confirmText: 'Sincronizar', danger: false });
        if (!ok) return;
        btn.disabled = true;
        try {
          const res = await fetch('/api/menu/sync-branches', {
            method: 'POST',
            headers: mesioHeaders()
          });
          if (!res.ok) {
            const err = await res.json().catch(function () { return {}; });
            throw new Error(err.detail || 'HTTP ' + res.status);
          }
          mesioToast('Menú sincronizado a todas las sucursales', 'success');
        } catch (e) {
          mesioToast('Error al sincronizar: ' + e.message, 'error');
        } finally {
          btn.disabled = false;
        }
      });
    }
  });

  // ── Inventory modal ────────────────────────────────────────────────
  var _invItems = [];

  function openInvModal(item) {
    var modal = document.getElementById('invModal');
    if (!modal) return;
    document.getElementById('invModalId').value = item ? (item.id || '') : '';
    document.getElementById('invModalTitle').textContent = item ? 'Editar producto' : 'Nuevo producto';
    document.getElementById('invModalName').value = item ? (item.name || '') : '';
    document.getElementById('invModalUnit').value = item ? (item.unit || '') : '';
    document.getElementById('invModalStock').value = item ? (item.stock != null ? item.stock : (item.current_stock != null ? item.current_stock : '')) : '';
    document.getElementById('invModalMin').value = item ? (item.low_stock_threshold != null ? item.low_stock_threshold : (item.min_stock != null ? item.min_stock : '')) : '';
    document.getElementById('invModalCost').value = item ? (item.cost_per_unit != null ? item.cost_per_unit : '') : '';
    modal.style.display = 'flex';
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
      var body = id
        ? { name: name, unit: unit, current_stock: stock, min_stock: min, cost_per_unit: cost }
        : { name: name, unit: unit, current_stock: stock, min_stock: min, cost_per_unit: cost };
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

  // Add inventory item
  document.querySelectorAll('#tab-inv .btn.primary').forEach(function (btn) {
    if (btn.textContent.includes('Agregar')) {
      btn.addEventListener('click', function () { openInvModal(null); });
    }
  });

  // ── Render menu ───────────────────────────────────────────────────

  function initToggle(input) {
    input.addEventListener('change', function () {
      const dish = input.closest('.dish');
      if (dish) dish.classList.toggle('off', !input.checked);
      saveDishAvailability(input);
    });
  }

  async function saveDishAvailability(input) {
    const dish = input.closest('.dish');
    if (!dish) return;
    const name = dish.querySelector('.dish-name');
    if (!name) return;
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/menu/availability', {
        method: 'POST', headers,
        body: JSON.stringify({ dish_name: name.textContent.trim(), available: input.checked })
      });
      if (!res.ok) throw new Error('status ' + res.status);
    } catch (e) {
      // Revert optimistic update on error
      input.checked = !input.checked;
      const dishEl = input.closest('.dish');
      if (dishEl) dishEl.classList.toggle('off', !input.checked);
      if (typeof mesioToast === 'function') mesioToast('Error al guardar disponibilidad', 'error');
    }
  }

  function renderMenu(categories) {
    const tab = document.getElementById('tab-disp');
    if (!tab) return;

    // Find or create the live menu container
    let container = document.getElementById('live-menu-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'live-menu-container';
      // Insert after the filter row (first child)
      const filterRow = tab.querySelector('.row');
      if (filterRow && filterRow.parentNode) {
        filterRow.parentNode.insertBefore(container, filterRow.nextSibling);
      } else {
        tab.appendChild(container);
      }
      // Remove old static category cards
      tab.querySelectorAll('.card.flush').forEach(function (c) { c.remove(); });
    }

    if (!categories || !Object.keys(categories).length) {
      container.innerHTML = '<div style="padding:24px;color:var(--text-3);">(sin platos)</div>';
      container.setAttribute('data-loaded', 'true');
      return;
    }

    container.innerHTML = Object.keys(categories).map(function (cat) {
      const dishes = categories[cat];
      const dishHtml = dishes.map(function (d) {
        const available = d.available !== false;
        const initials = (d.name || '?')[0].toUpperCase();
        const price = typeof mesioFmt === 'function' ? mesioFmt(d.price || 0) : '$' + (d.price || 0);
        const thumbContent = d.image_url
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

    // Re-bind category collapse
    container.querySelectorAll('.cat-head').forEach(function (head) {
      head.addEventListener('click', function () {
        const grid = head.closest('.card').querySelector('.dish-grid');
        const arrow = head.querySelector('.cat-arrow');
        if (grid) {
          const hidden = grid.style.display === 'none';
          grid.style.display = hidden ? '' : 'none';
          if (arrow) arrow.classList.toggle('open', hidden);
        }
      });
    });

    // Bind toggles
    container.querySelectorAll('.toggle input').forEach(initToggle);

    // Bind image buttons
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
    // menuData may be a flat list or a dict keyed by category
    if (!menuData) return {};
    if (Array.isArray(menuData)) {
      return { 'Menú': menuData };
    }
    // Already categorized dict
    return menuData;
  }

  async function loadMenu() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/dashboard/menu', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const menuRaw = data.menu || data.categories || data;
      renderMenu(buildCategoriesFromMenu(menuRaw));
    } catch (e) {
      console.error('menu-admin: load menu error', e);
    }
  }

  // ── Render inventory ──────────────────────────────────────────────

  function renderInventory(items) {
    let body = document.getElementById('inv-body');
    if (!body) {
      const tab = document.getElementById('tab-inv');
      if (!tab) return;
      const existingRows = tab.querySelectorAll('.inv-row:not(.head)');
      existingRows.forEach(function (r) { r.remove(); });
      body = document.createElement('div');
      body.id = 'inv-body';
      const head = tab.querySelector('.inv-row.head');
      if (head) {
        head.parentNode.insertBefore(body, head.nextSibling);
      } else {
        tab.appendChild(body);
      }
    }

    // Populate inv-summary regardless of whether items array is empty
    var summaryEl = document.getElementById('inv-summary');
    if (summaryEl) {
      var fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };
      var list = Array.isArray(items) ? items : [];
      var totalValue = 0;
      list.forEach(function (item) {
        var stock = +(item.stock || item.quantity || item.current_stock || 0);
        var cost  = +(item.cost_per_unit || item.unit_cost || 0);
        totalValue += stock * cost;
      });
      summaryEl.textContent = list.length + ' producto' + (list.length === 1 ? '' : 's') +
        ' · ' + fmt(totalValue) + ' valor stock';
    }

    if (!items || !items.length) {
      body.innerHTML = '<div style="padding:18px;color:var(--text-3);">(sin inventario)</div>';
      body.setAttribute('data-loaded', 'true');
      return;
    }

    body.innerHTML = items.map(function (item) {
      const stock = item.stock || item.quantity || 0;
      const low = item.low_stock_threshold || item.min_stock || 10;
      const stockCls = stock <= 0 ? 'danger' : stock <= low ? 'warn' : '';
      const unit = item.unit || 'u';
      return '<div class="inv-row">' +
        '<div style="font-weight:500;">' + _escHtml(item.name || item.sku || '') + '</div>' +
        '<div style="font-size:12px;color:var(--text-3);">' + _escHtml(item.category || '') + '</div>' +
        '<div class="num"><span class="' + stockCls + '">' + stock + ' ' + _escHtml(unit) + '</span></div>' +
        '<div><button class="btn sm ghost" data-inv-action="restock" data-id="' + (item.id || '') + '">Reponer</button> ' +
        '<button class="btn sm ghost" data-inv-action="edit" data-id="' + (item.id || '') + '">Editar</button></div>' +
        '</div>';
    }).join('');

    body.setAttribute('data-loaded', 'true');

    body.querySelectorAll('[data-inv-action]').forEach(function (btn) {
      btn.addEventListener('click', async function (e) {
        e.stopPropagation();
        var itemId = btn.dataset.id;
        var action = btn.dataset.invAction;
        var item = _invItems.find(function (it) { return String(it.id) === String(itemId); });

        if (action === 'restock') {
          var delta = window.prompt('Cantidad a reponer:');
          if (delta === null) return;
          delta = parseFloat(delta);
          if (isNaN(delta) || delta <= 0) { mesioToast('Ingresa una cantidad positiva', 'warn'); return; }
          try {
            var res = await fetch('/api/inventory/' + itemId + '/adjust', {
              method: 'POST',
              headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
              body: JSON.stringify({ quantity: delta, reason: 'restock manual' })
            });
            if (!res.ok) {
              var err = await res.json().catch(function () { return {}; });
              throw new Error(err.detail || 'HTTP ' + res.status);
            }
            mesioToast('Stock actualizado', 'success');
            loadInventory();
          } catch (e2) {
            mesioToast('Error: ' + e2.message, 'error');
          }

        } else if (action === 'edit') {
          openInvModal(item || { id: itemId });

        } else if (action === 'delete') {
          var confirmed = await mesioConfirm('Eliminar este producto del inventario. Esta acción no se puede deshacer.', { confirmText: 'Eliminar', danger: true });
          if (!confirmed) return;
          try {
            var res2 = await fetch('/api/inventory/' + itemId, {
              method: 'DELETE',
              headers: mesioHeaders()
            });
            if (!res2.ok) {
              var err2 = await res2.json().catch(function () { return {}; });
              throw new Error(err2.detail || 'HTTP ' + res2.status);
            }
            mesioToast('Producto eliminado', 'success');
            loadInventory();
          } catch (e3) {
            mesioToast('Error: ' + e3.message, 'error');
          }
        }
      });
    });
  }

  async function loadInventory() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/inventory', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const items = data.inventory || data.items || data;
      _invItems = Array.isArray(items) ? items : [];
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
    var confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('Eliminar la imagen de este plato.')
      : confirm('Eliminar la imagen de este plato.');
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

  loadMenu();
  loadInventory();
})();
