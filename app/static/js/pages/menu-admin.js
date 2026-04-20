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

  // Sync branches button
  const syncBtn = document.querySelector('.btn.primary');
  if (syncBtn && syncBtn.textContent.includes('Sincronizar')) {
    syncBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Sincronización de sucursales en proceso…', 'info');
      }
    });
  }

  // Add inventory item
  const addInvBtn = document.querySelector('#tab-inv .btn.primary');
  if (addInvBtn) {
    addInvBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Formulario de nuevo producto próximamente', 'info');
      }
    });
  }

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
        return '<div class="dish ' + (available ? '' : 'off') + '" data-dish-name="' + _escHtml(d.name || '') + '">' +
          '<div class="dish-thumb" style="background: linear-gradient(135deg, var(--brand), #0F6E56);">' + _escHtml(initials) + '</div>' +
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
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        if (typeof mesioToast === 'function') {
          mesioToast(btn.dataset.invAction + ' — próximamente disponible', 'info', 2000);
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
      renderInventory(Array.isArray(items) ? items : []);
    } catch (e) {
      console.error('menu-admin: inventory error', e);
    }
  }

  loadMenu();
  loadInventory();
})();
