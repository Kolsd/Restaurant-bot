/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Features
   app/static/dashboard-features.js
═══════════════════════════════════════════════════ */

// _escHtml provided by mesio-utils.js
if (typeof _escHtml === 'undefined') {
  function _escHtml(s) { if(s==null)return''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
}

// ── MENU ─────────────────────────────────────────────────────────────
let menuAvailability = {};
let MENU_ITEMS = [];
let editorMenuState = []; // 🛡️ FIX: Declared up front to avoid TDZ errors

// `let` at the top level of a classic script does NOT create a property on
// window, so `window.MENU_ITEMS = [...]` from another file writes a
// DIFFERENT variable and openMenuEditor keeps reading this empty one. That
// is exactly what happened to the full carta editor: menu-admin.js filled
// window.MENU_ITEMS and the editor opened saying "Tu carta está vacía".
// This setter is the only supported way in from another file.
window.setMenuItems = function (items) {
  MENU_ITEMS = Array.isArray(items) ? items : [];
  // Kept in sync for any reader that still looks at the window property.
  window.MENU_ITEMS = MENU_ITEMS;
  return MENU_ITEMS.length;
};

async function loadMenu() {
  const h = window._dashHeaders;
  try {
    const [rMenu, rAvail] = await Promise.all([
      fetch('/api/dashboard/menu', { headers: h }),
      fetch('/api/menu/availability', { headers: h })
    ]);
    if (rAvail.ok) menuAvailability = (await rAvail.json()).availability || {};
    if (rMenu.ok) {
      const menu = (await rMenu.json()).menu || {};
      MENU_ITEMS = [];
      Object.entries(menu).forEach(([cat, dishes]) => {
        if (Array.isArray(dishes)) {
          dishes.forEach(d => MENU_ITEMS.push({
            name:            d.name            || '',
            cat:             cat,
            price:           d.price           != null ? d.price : 0,
            desc:            d.description     || '',
            // sku: stable id used by the diner-chat tap-to-cart flow
            // (app/services/orders.py::resolve_dish_for_cart) — must
            // round-trip through the editor or a re-save silently wipes it.
            sku:             d.sku             || null,
            // Extended shape (catalog v2)
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
          }));
        }
      });
    }
  } catch(e) { console.error('loadMenu:', e); }
  
  // Visibility logic for buttons exclusive to the main branch (Matriz)
  const role = localStorage.getItem('rb_role') || '';
  const branchVal = window._dashHeaders['X-Branch-ID'];
  const isMatriz = (!branchVal || branchVal === 'matriz');
  
  const btnEdit = document.getElementById('btn-edit-menu');
  const btnSync = document.getElementById('btn-sync-menu');
  
  if (btnEdit) btnEdit.style.display = (role.includes('owner') && isMatriz) ? '' : 'none';
  if (btnSync) btnSync.style.display = (role.includes('owner') && isMatriz) ? '' : 'none';

  renderMenu();
}

// 🛡️ Global memory to remember which menu tabs we left open
window._openMenuCats = window._openMenuCats || new Set();

function renderMenu() {
  const grid = document.getElementById('menu-grid');
  if (!grid) return;
  if (!MENU_ITEMS.length) {
    grid.innerHTML = '<div style="padding:2rem;text-align:center;color:#aaa;font-size:13px;">Sin platos en el menú.</div>';
    return;
  }
  const cats = [...new Set(MENU_ITEMS.map(m => m.cat))];
  
  // If this is the first load and nothing is open, open the first category by default
  if (window._openMenuCats.size === 0 && cats.length > 0) {
    const firstCat = /\p{Emoji}/u.test(cats[0]) ? cats[0] : `🍽️ ${cats[0]}`;
    window._openMenuCats.add(firstCat);
  }

  grid.innerHTML = cats.map((cat, ci) => {
    const items = MENU_ITEMS.filter(m => m.cat === cat);
    const avail = items.filter(m => menuAvailability[m.name] !== false).length;
    
    const hasEmoji = /\p{Emoji}/u.test(cat);
    const displayCat = hasEmoji ? cat : `🍽️ ${cat}`;
    
    // 🛡️ FIX: Read the memory to know whether it should be open or closed
    const isOpen = window._openMenuCats.has(displayCat);

    return `<div class="menu-category">
      <div class="menu-cat-header" onclick="toggleCat(this)">
        <div class="menu-cat-title"><span>${_escHtml(displayCat)}</span><span class="menu-cat-meta">${avail}/${items.length} disponibles</span></div>
        <span class="menu-cat-arrow ${isOpen?'open':''}">▼</span>
      </div>
      <div class="menu-cat-body ${isOpen?'open':''}">
        ${items.map(m => {
          const av = menuAvailability[m.name] !== false;
          const safe = m.name.replace(/'/g,"\\'");
          return `<div class="menu-row" style="${av?'':'opacity:.55;'}">
            <div style="flex:1;min-width:0;"><div class="menu-row-name" style="${av?'':'text-decoration:line-through;color:#bbb;'}">${_escHtml(m.name)}</div></div>
            <div class="menu-row-price">${_escHtml(typeof m.price === 'number' ? mesioFmt(m.price) : m.price)}</div>
            <div class="menu-row-status ${av?'status-on':'status-off'}">${av?'Disponible':'No disponible'}</div>
            <label class="toggle-switch"><input type="checkbox" ${av?'checked':''} onchange="toggleDish('${safe}',this.checked)"><span class="toggle-slider"></span></label>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }).join('');
}

function toggleCat(header) {
  const body = header.nextElementSibling;
  const arrow = header.querySelector('.menu-cat-arrow');
  const catName = header.querySelector('.menu-cat-title span:first-child').textContent; 
  
  body.classList.toggle('open');
  arrow.classList.toggle('open');
  
  // 🛡️ FIX: Update the memory when the user clicks
  if (body.classList.contains('open')) {
    window._openMenuCats.add(catName);
  } else {
    window._openMenuCats.delete(catName);
  }
}

async function toggleDish(name, available) {
  const h = window._dashHeaders;
  try {
    await fetch('/api/menu/availability', {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ dish_name: name, available })
    });
    menuAvailability[name] = available;
    renderMenu();
  } catch(e) {}
}

async function syncMenuToBranches() {
  if (!confirm("⚠️ ¿Estás seguro de sincronizar el menú?\n\nEsto sobrescribirá el catálogo de TODAS las sucursales con los precios y platos actuales de la Casa Matriz.\n\nNota: La disponibilidad y el stock de las sucursales NO se verán afectados.")) return;
  
  const btn = document.getElementById('btn-sync-menu');
  const originalText = btn.textContent;
  btn.textContent = 'Sincronizando...';
  btn.disabled = true;
  
  const h = window._dashHeaders;
  try {
    const r = await fetch('/api/menu/sync-branches', {
      method: 'POST', 
      headers: { ...h, 'Content-Type': 'application/json' }
    });
    
    if (r.ok) {
      const res = await r.json();
      alert(`✅ Sincronización exitosa.\n\nEl menú ha sido actualizado en ${res.branches_updated} sucursales.`);
    } else {
      const e = await r.json();
      alert('Error: ' + (e.detail || 'No se pudo sincronizar el menú.'));
    }
  } catch(e) {
    alert('Error de conexión al intentar sincronizar el menú.');
  } finally {
    btn.textContent = originalText;
    btn.disabled = false;
  }
}

// ── MENU EDITOR v2 — Visual catalog (Phase 2) ──

// ── i18n: slug → Spanish label ──────────────────────────────────────
const DISH_LABELS = {
  tags: {
    vegan:         'Vegano',
    vegetarian:    'Vegetariano',
    gluten_free:   'Sin Gluten',
    lactose_free:  'Sin Lactosa',
    spicy:         'Picante',
    halal:         'Halal',
    kosher:        'Kosher',
    healthy:       'Saludable',
    popular:       'Popular',
    popular_latam: 'Popular',
  },
  badges: {
    chef_pick: 'Recomendación del chef',
    new:       'Nuevo',
    popular:   'Popular',
  },
  allergens: {
    gluten:        'Gluten',
    lacteos:       'Lácteos',
    huevo:         'Huevo',
    frutos_secos:  'Frutos Secos',
    mariscos:      'Mariscos',
    pescado:       'Pescado',
    soya:          'Soya',
    sulfitos:      'Sulfitos',
  },
};

function _dishLabel(group, slug) {
  return (DISH_LABELS[group] && DISH_LABELS[group][slug]) || slug;
}

/** Returns comma-separated labels for an array of slugs in a group */
function _dishLabelList(group, slugs) {
  if (!slugs || !slugs.length) return '';
  return slugs.map(s => _dishLabel(group, s)).join(', ');
}

/** Builds accessible chip aria-label: "Vegano — activo" / "Vegano — inactivo" */
function _chipAriaLabel(group, slug, selected) {
  const label = _dishLabel(group, slug);
  return `${label}: ${selected ? 'seleccionado' : 'no seleccionado'}`;
}

// ── Deterministic gradient fallback for dishes without image ─────────
function _dishGradient(name) {
  // Simple hash to pick from a palette
  let h = 0;
  for (let i = 0; i < (name || '').length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  const palettes = [
    ['#1D9E75','#0F6E56'], ['#3B82F6','#1D4ED8'], ['#F59E0B','#B45309'],
    ['#EF4444','#DC2626'], ['#8B5CF6','#7C3AED'], ['#EC4899','#BE185D'],
    ['#06B6D4','#0E7490'], ['#84CC16','#4D7C0F'],
  ];
  const pair = palettes[Math.abs(h) % palettes.length];
  return `linear-gradient(135deg, ${pair[0]}, ${pair[1]})`;
}

function _dishInitial(name) {
  return (name || '?').trim().charAt(0).toUpperCase();
}

// ── Render thumbnail (img or gradient placeholder) ──────────────────
function _renderDishThumb(dish, size = 44) {
  if (dish.image_url) {
    const img = document.createElement('img');
    img.className = 'dish-thumb';
    img.style.width = size + 'px';
    img.style.height = size + 'px';
    img.alt = '';
    img.src = mesioImageUrl(dish.image_url, 'thumb');
    return img;
  }
  const div = document.createElement('div');
  div.className = 'dish-thumb-placeholder';
  div.style.width = size + 'px';
  div.style.height = size + 'px';
  div.style.background = _dishGradient(dish.name);
  div.style.fontSize = Math.floor(size * 0.45) + 'px';
  div.textContent = _dishInitial(dish.name);
  return div;
}

// ── Normalize dish to full shape (backward-compat) ───────────────────
function _normalizeDish(d) {
  return {
    name:           d.name         || '',
    description:    d.description  || '',
    price:          d.price        != null ? d.price : 0,
    sku:            d.sku          || null,
    image_url:      d.image_url    || null,
    image_public_id: d.image_public_id || null,
    tags:           Array.isArray(d.tags)      ? d.tags      : [],
    badges:         Array.isArray(d.badges)    ? d.badges    : [],
    allergens:      Array.isArray(d.allergens) ? d.allergens : [],
    featured:       !!d.featured,
    active:         d.active !== false, // default true
    sort_order:     d.sort_order != null ? d.sort_order : 999,
    calories:       d.calories    != null ? d.calories    : null,
    prep_time_min:  d.prep_time_min != null ? d.prep_time_min : null,
  };
}

// ── State for dish modal (current editing) ───────────────────────────
let _dishModalState = null; // { catIndex, dishIndex, dish }
let _dishModalUploading = false;

// ── Open menu editor ─────────────────────────────────────────────────
function openMenuEditor() {
  editorMenuState = [];
  const catMap = {};

  MENU_ITEMS.forEach(m => {
    if (!catMap[m.cat]) {
      catMap[m.cat] = [];
      editorMenuState.push({ catName: m.cat, isOpen: false, dishes: catMap[m.cat] });
    }
    catMap[m.cat].push(_normalizeDish({
      name: m.name,
      price: String(m.price).replace(/[^0-9.-]+/g, ''),
      description: m.desc || '',
      sku: m.sku || null,
      // Extended fields may exist on MENU_ITEMS if loaded from extended endpoint
      image_url:       m.image_url       || null,
      image_public_id: m.image_public_id || null,
      tags:            m.tags            || [],
      badges:          m.badges          || [],
      allergens:       m.allergens       || [],
      featured:        m.featured        || false,
      active:          m.active          !== false,
      sort_order:      m.sort_order      != null ? m.sort_order : 999,
      calories:        m.calories        || null,
      prep_time_min:   m.prep_time_min   || null,
    }));
  });

  if (editorMenuState.length > 0) editorMenuState[0].isOpen = true;

  // Ensure dish modal overlay exists in DOM
  _ensureDishModalDOM();

  renderMenuEditor();

  document.body.style.overflow = 'hidden';
  document.getElementById('full-menu-editor').style.display = 'block';
}

function closeMenuEditor() {
  document.getElementById('full-menu-editor').style.display = 'none';
  document.body.style.overflow = '';
}

function toggleEditorCat(index) {
  editorMenuState[index].isOpen = !editorMenuState[index].isOpen;
  renderMenuEditor();
}

// ── Render main editor canvas (category list) ────────────────────────
function renderMenuEditor() {
  const canvas = document.getElementById('menu-editor-canvas');
  canvas.innerHTML = '';

  if (editorMenuState.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.style.cssText = 'background:#fff; border-radius:12px; padding:4rem; font-size:16px;';
    empty.textContent = 'Tu carta está vacía. Añade una categoría para comenzar.';
    canvas.appendChild(empty);
    return;
  }

  editorMenuState.forEach((catObj, catIndex) => {
    const catCard = document.createElement('div');
    catCard.style.cssText = 'background:#fff; border:1px solid #e0e0d8; border-radius:12px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.04); margin-bottom:12px;';

    // Header
    const header = document.createElement('div');
    header.style.cssText = `display:flex; align-items:center; justify-content:space-between; padding:14px 16px; background:${catObj.isOpen ? '#f8f8f5' : '#fff'}; cursor:pointer; border-bottom:${catObj.isOpen ? '1px solid #e0e0d8' : 'none'};`;

    const headerLeft = document.createElement('div');
    headerLeft.style.cssText = 'display:flex; align-items:center; gap:10px; flex:1;';

    const catInput = document.createElement('input');
    catInput.type = 'text';
    catInput.value = catObj.catName;
    catInput.placeholder = 'Nombre de categoría';
    catInput.style.cssText = 'font-size:16px; font-weight:700; color:#111; border:1px solid transparent; background:transparent; padding:5px 8px; border-radius:6px; outline:none; width:70%; transition:border 0.2s; font-family:inherit;';
    catInput.addEventListener('click', e => e.stopPropagation());
    catInput.addEventListener('focus', () => { catInput.style.borderColor = '#1D9E75'; catInput.style.background = '#fff'; });
    catInput.addEventListener('blur', () => { catInput.style.borderColor = 'transparent'; catInput.style.background = 'transparent'; });
    catInput.addEventListener('change', () => { editorMenuState[catIndex].catName = catInput.value.trim() || 'Sin Nombre'; });

    headerLeft.appendChild(catInput);
    header.appendChild(headerLeft);

    const headerRight = document.createElement('div');
    headerRight.style.cssText = 'display:flex; align-items:center; gap:10px;';

    const delCatBtn = document.createElement('button');
    delCatBtn.style.cssText = 'background:#FDE8E8; color:#C0392B; border:none; border-radius:8px; padding:6px 12px; font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap;';
    delCatBtn.textContent = 'Eliminar';
    delCatBtn.addEventListener('click', (e) => { e.stopPropagation(); removeMenuEditorCategory(catIndex); });

    const arrow = document.createElement('span');
    arrow.style.cssText = 'font-size:13px; color:#888; width:18px; text-align:center;';
    arrow.textContent = catObj.isOpen ? '▲' : '▼';

    headerRight.appendChild(delCatBtn);
    headerRight.appendChild(arrow);
    header.appendChild(headerRight);
    header.addEventListener('click', () => toggleEditorCat(catIndex));
    catCard.appendChild(header);

    // Body (dish list)
    if (catObj.isOpen) {
      const body = document.createElement('div');
      body.style.cssText = 'padding:14px 16px; background:#fafaf8;';

      const dishList = document.createElement('div');
      dishList.id = 'editor-dishes-' + catIndex;
      dishList.style.cssText = 'display:flex; flex-direction:column; gap:8px; margin-bottom:10px;';

      if (catObj.dishes.length === 0) {
        const hint = document.createElement('div');
        hint.style.cssText = 'color:#aaa; font-size:13px; font-style:italic; text-align:center; padding:16px 0;';
        hint.textContent = 'No hay platos en esta categoría.';
        dishList.appendChild(hint);
      } else {
        _renderDishCards(dishList, catIndex, catObj.dishes);
      }

      body.appendChild(dishList);

      // Add dish button
      const addBtn = document.createElement('button');
      addBtn.className = 'btn-add-dish';
      addBtn.innerHTML = '<span style="font-size:16px;">+</span> Añadir plato';
      addBtn.addEventListener('click', () => addMenuEditorDish(catIndex));
      body.appendChild(addBtn);

      catCard.appendChild(body);
    }

    canvas.appendChild(catCard);
  });
}

// ── Render draggable dish cards ───────────────────────────────────────
function _renderDishCards(container, catIndex, dishes) {
  dishes.forEach((dish, dishIndex) => {
    const card = _createDishCard(catIndex, dishIndex, dish);
    container.appendChild(card);
  });
}

function _createDishCard(catIndex, dishIndex, dish) {
  const card = document.createElement('div');
  card.className = 'dish-card' + (dish.active === false ? ' dish-card-inactive' : '');
  card.draggable = true;
  card.dataset.catIndex = catIndex;
  card.dataset.dishIndex = dishIndex;

  // Drag handle
  const handle = document.createElement('span');
  handle.className = 'dish-drag-handle';
  handle.innerHTML = '&#9776;'; // ≡
  handle.title = 'Arrastrar para reordenar';
  card.appendChild(handle);

  // Thumbnail
  const thumb = _renderDishThumb(dish);
  card.appendChild(thumb);

  // Info
  const info = document.createElement('div');
  info.className = 'dish-card-info';

  const nameEl = document.createElement('div');
  nameEl.className = 'dish-card-name';
  nameEl.textContent = dish.name || '(sin nombre)';
  info.appendChild(nameEl);

  const priceEl = document.createElement('div');
  priceEl.className = 'dish-card-price';
  priceEl.textContent = dish.price ? mesioFmt(dish.price) : '';
  info.appendChild(priceEl);

  // Mini badges + tag/allergen summary
  const hasBadges    = dish.badges    && dish.badges.length;
  const hasTags      = dish.tags      && dish.tags.length;
  const hasAllergens = dish.allergens && dish.allergens.length;

  if (hasBadges || hasTags || hasAllergens) {
    const badgeRow = document.createElement('div');
    badgeRow.className = 'dish-card-badges';

    if (hasBadges) {
      dish.badges.forEach(b => {
        const span = document.createElement('span');
        span.className = 'dish-mini-badge' + (b === 'new' ? ' dish-mini-badge--new' : b === 'chef_pick' ? ' dish-mini-badge--chef' : '');
        span.textContent = _dishLabel('badges', b);
        badgeRow.appendChild(span);
      });
    }

    if (hasTags) {
      // Show first 2 tags; if more show +N
      const visible = dish.tags.slice(0, 2);
      visible.forEach(t => {
        const span = document.createElement('span');
        span.className = 'dish-mini-badge';
        span.style.cssText = 'background:var(--info-light);color:#1D4ED8;';
        span.textContent = _dishLabel('tags', t);
        badgeRow.appendChild(span);
      });
      if (dish.tags.length > 2) {
        const more = document.createElement('span');
        more.className = 'dish-mini-badge';
        more.style.cssText = 'background:var(--bg);color:var(--text-3);';
        more.textContent = '+' + (dish.tags.length - 2);
        badgeRow.appendChild(more);
      }
    }

    if (hasAllergens) {
      const aSpan = document.createElement('span');
      aSpan.className = 'dish-mini-badge';
      aSpan.style.cssText = 'background:var(--warning-light);color:#854F0B;';
      aSpan.title = dish.allergens.map(a => _dishLabel('allergens', a)).join(', ');
      aSpan.textContent = dish.allergens.length === 1
        ? _dishLabel('allergens', dish.allergens[0])
        : dish.allergens.length + ' alérgenos';
      badgeRow.appendChild(aSpan);
    }

    info.appendChild(badgeRow);
  }

  card.appendChild(info);

  // Featured star + inactive badge
  if (dish.featured) {
    const star = document.createElement('span');
    star.title = 'Destacado en el catálogo';
    star.setAttribute('aria-label', 'Plato destacado');
    star.style.cssText = 'font-size:14px; flex-shrink:0;';
    star.textContent = '★';
    card.appendChild(star);
  }
  if (dish.active === false) {
    const inactiveSpan = document.createElement('span');
    inactiveSpan.style.cssText = 'font-size:10px; padding:2px 7px; border-radius:999px; background:#FDE8E8; color:#C0392B; font-weight:600; white-space:nowrap;';
    inactiveSpan.textContent = 'Inactivo';
    inactiveSpan.setAttribute('aria-label', 'Plato inactivo');
    card.appendChild(inactiveSpan);
  }

  // Edit button
  const editBtn = document.createElement('button');
  editBtn.style.cssText = 'background:#E1F5EE; color:#0F6E56; border:none; border-radius:8px; padding:6px 12px; font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap; flex-shrink:0;';
  editBtn.textContent = 'Editar';
  editBtn.addEventListener('click', (e) => { e.stopPropagation(); openDishModal(catIndex, dishIndex); });
  card.appendChild(editBtn);

  // Delete button
  const delBtn = document.createElement('button');
  delBtn.style.cssText = 'background:#FDE8E8; color:#C0392B; border:none; border-radius:8px; padding:6px 10px; font-size:12px; font-weight:600; cursor:pointer; flex-shrink:0;';
  delBtn.textContent = '✕';
  delBtn.title = 'Eliminar plato';
  delBtn.addEventListener('click', (e) => { e.stopPropagation(); removeMenuEditorDish(catIndex, dishIndex); });
  card.appendChild(delBtn);

  // ── Drag & Drop handlers (mouse/touch) ──
  card.addEventListener('dragstart', (e) => {
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', JSON.stringify({ catIndex, dishIndex }));
    card.classList.add('dragging');
  });
  card.addEventListener('dragend', () => {
    card.classList.remove('dragging');
    document.querySelectorAll('.dish-card').forEach(c => c.classList.remove('drag-over'));
  });
  card.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    document.querySelectorAll('.dish-card').forEach(c => c.classList.remove('drag-over'));
    card.classList.add('drag-over');
  });
  card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
  card.addEventListener('drop', (e) => {
    e.preventDefault();
    card.classList.remove('drag-over');
    let src;
    try { src = JSON.parse(e.dataTransfer.getData('text/plain')); } catch { return; }
    const srcCat = parseInt(src.catIndex, 10);
    const srcDish = parseInt(src.dishIndex, 10);
    const dstCat = parseInt(card.dataset.catIndex, 10);
    const dstDish = parseInt(card.dataset.dishIndex, 10);
    if (srcCat !== dstCat || srcDish === dstDish) return;
    _reorderDish(srcCat, srcDish, dstDish);
  });

  // ── Keyboard reorder on drag handle (accessibility) ──
  handle.setAttribute('tabindex', '0');
  handle.setAttribute('role', 'button');
  handle.setAttribute('aria-label', `Reordenar ${dish.name || 'plato'}. Usa Alt+Arriba / Alt+Abajo`);
  handle.addEventListener('keydown', (e) => {
    const dishes = editorMenuState[catIndex].dishes;
    if (e.altKey && e.key === 'ArrowUp') {
      e.preventDefault();
      if (dishIndex > 0) { _reorderDish(catIndex, dishIndex, dishIndex - 1); }
    } else if (e.altKey && e.key === 'ArrowDown') {
      e.preventDefault();
      if (dishIndex < dishes.length - 1) { _reorderDish(catIndex, dishIndex, dishIndex + 1); }
    }
  });

  // Click card (not handle/buttons) → open editor
  card.addEventListener('click', (e) => {
    if (e.target === handle) return;
    openDishModal(catIndex, dishIndex);
  });

  return card;
}

// ── Reorder helper (shared by drag-drop and keyboard) ───────────────
function _reorderDish(catIndex, srcIndex, dstIndex) {
  const dishes = editorMenuState[catIndex].dishes;
  if (srcIndex === dstIndex) return;
  const [moved] = dishes.splice(srcIndex, 1);
  dishes.splice(dstIndex, 0, moved);
  // Recalculate sort_order (steps of 10)
  dishes.forEach((d, i) => { d.sort_order = i * 10; });
  renderMenuEditor();
  // Re-focus the handle in the new position for keyboard users
  setTimeout(() => {
    const container = document.getElementById('editor-dishes-' + catIndex);
    if (container) {
      const handles = container.querySelectorAll('.dish-drag-handle');
      if (handles[dstIndex]) handles[dstIndex].focus();
    }
  }, 50);
  _autoSaveMenuReorder();
}

// ── Auto-save reorder ────────────────────────────────────────────────
async function _autoSaveMenuReorder() {
  const finalMenu = _buildFinalMenu();
  try {
    const r = await fetch('/api/menu/update', {
      method: 'PUT',
      headers: { ...mesioHeaders() },
      body: JSON.stringify({ menu: finalMenu })
    });
    if (r.ok) {
      mesioToast('Orden guardado', 'success', 1500);
    }
  } catch (e) {
    // Silent fail for reorder — main save will catch it
  }
}

// ── Category actions ─────────────────────────────────────────────────
async function addMenuEditorCategory() {
  const input = document.createElement('input');
  // Use mesioConfirm-style prompt via a simple inline approach
  const name = prompt('Nombre de la nueva categoría:');
  if (!name || !name.trim()) return;
  editorMenuState.forEach(c => c.isOpen = false);
  editorMenuState.push({ catName: name.trim(), isOpen: true, dishes: [] });
  renderMenuEditor();
  setTimeout(() => {
    const editorEl = document.getElementById('full-menu-editor');
    if (editorEl) editorEl.scrollTo({ top: editorEl.scrollHeight, behavior: 'smooth' });
  }, 100);
}

async function removeMenuEditorCategory(catIndex) {
  const catName = editorMenuState[catIndex].catName;
  const ok = await mesioConfirm(`¿Eliminar la categoría "${catName}" y todos sus platos?`, { danger: true, confirmText: 'Eliminar' });
  if (!ok) return;
  editorMenuState.splice(catIndex, 1);
  renderMenuEditor();
}

function addMenuEditorDish(catIndex) {
  editorMenuState[catIndex].isOpen = true;
  const newDish = _normalizeDish({ name: '', price: '', description: '' });
  editorMenuState[catIndex].dishes.push(newDish);
  // Open modal for new dish immediately
  openDishModal(catIndex, editorMenuState[catIndex].dishes.length - 1);
}

async function removeMenuEditorDish(catIndex, dishIndex) {
  const dish = editorMenuState[catIndex].dishes[dishIndex];
  const ok = await mesioConfirm(`¿Eliminar el plato "${dish.name || '(sin nombre)'}"?`, { danger: true, confirmText: 'Eliminar' });
  if (!ok) return;

  // Delete image from Cloudinary if exists
  if (dish.image_public_id) {
    try {
      await fetch('/api/menu/image', {
        method: 'DELETE',
        headers: { ...mesioHeaders() },
        body: JSON.stringify({ public_id: dish.image_public_id })
      });
    } catch (e) { /* best effort */ }
  }

  editorMenuState[catIndex].dishes.splice(dishIndex, 1);
  renderMenuEditor();
}

function updateMenuEditorDish(catIndex, dishIndex, field, value) {
  editorMenuState[catIndex].dishes[dishIndex][field] = value;
}

// ══════════════════════════════════════════════════════════════════════
// DISH EDITOR MODAL
// ══════════════════════════════════════════════════════════════════════

function _ensureDishModalDOM() {
  if (document.getElementById('dish-modal-overlay')) return;

  const overlay = document.createElement('div');
  overlay.id = 'dish-modal-overlay';
  overlay.className = 'dish-modal-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'dish-modal-title');

  overlay.innerHTML = `
    <div class="dish-modal-box" id="dish-modal-box">
      <div class="dish-modal-header">
        <span class="dish-modal-title" id="dish-modal-title">Editar plato</span>
        <button class="dish-modal-close" id="dish-modal-close" aria-label="Cerrar">&times;</button>
      </div>
      <div class="dish-modal-body" id="dish-modal-body">
        <!-- Populated by openDishModal() -->
      </div>
      <div class="dish-modal-footer">
        <button class="m-btn m-btn--ghost" id="dish-modal-cancel">Cancelar</button>
        <button class="m-btn m-btn--primary" id="dish-modal-save">Guardar</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  // Close on overlay background click
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeDishModal();
  });
  document.getElementById('dish-modal-close').addEventListener('click', closeDishModal);
  document.getElementById('dish-modal-cancel').addEventListener('click', closeDishModal);
  document.getElementById('dish-modal-save').addEventListener('click', saveDishModal);

  // Trap focus inside modal (accessibility)
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDishModal();
  });
}

function openDishModal(catIndex, dishIndex) {
  _ensureDishModalDOM();
  const dish = editorMenuState[catIndex].dishes[dishIndex];
  _dishModalState = { catIndex, dishIndex, dish: JSON.parse(JSON.stringify(dish)) }; // deep clone

  document.getElementById('dish-modal-title').textContent =
    dish.name ? `Editar: ${dish.name}` : 'Nuevo plato';

  _renderDishModalBody(_dishModalState.dish);

  const overlay = document.getElementById('dish-modal-overlay');
  overlay.classList.add('open');
  // Focus first input
  setTimeout(() => {
    const first = overlay.querySelector('input[type="text"]');
    if (first) first.focus();
  }, 60);
}

function closeDishModal() {
  const overlay = document.getElementById('dish-modal-overlay');
  if (overlay) overlay.classList.remove('open');
  _dishModalState = null;
}

function _renderDishModalBody(dish) {
  const body = document.getElementById('dish-modal-body');
  body.innerHTML = '';

  // ── 1. Image upload zone ──────────────────────────────────────────
  const imgSection = document.createElement('div');
  imgSection.className = 'dish-field';

  const imgLabel = document.createElement('label');
  imgLabel.textContent = 'Foto del plato';
  imgSection.appendChild(imgLabel);

  const zone = document.createElement('div');
  zone.id = 'dish-img-zone';
  zone.className = 'dish-image-zone' + (dish.image_url ? ' has-image' : '');

  _renderImageZoneContent(zone, dish);
  imgSection.appendChild(zone);

  // File input (hidden, triggered by zone click)
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/*';
  fileInput.id = 'dish-file-input';
  fileInput.style.cssText = 'display:none;';
  fileInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (file) await _handleImageFile(file, zone, dish);
    fileInput.value = ''; // reset so same file can be re-selected
  });
  imgSection.appendChild(fileInput);

  // Zone click → trigger file input (unless uploading)
  zone.addEventListener('click', (e) => {
    if (_dishModalUploading) return;
    if (e.target.classList.contains('dish-image-action-btn')) return;
    document.getElementById('dish-file-input').click();
  });

  // Drag & drop onto zone
  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    if (!_dishModalUploading) zone.classList.add('drag-over');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', async (e) => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    if (_dishModalUploading) return;
    const file = e.dataTransfer.files[0];
    if (file && file.type.startsWith('image/')) {
      await _handleImageFile(file, zone, dish);
    }
  });

  body.appendChild(imgSection);

  // ── 2. Name + Price row ───────────────────────────────────────────
  const nameField = _makeField('Nombre del plato *', 'text', dish.name || '', 'Ej: Hamburguesa Clásica');
  nameField.querySelector('input').id = 'dish-input-name';
  nameField.querySelector('input').addEventListener('input', (e) => {
    dish.name = e.target.value;
    document.getElementById('dish-modal-title').textContent = dish.name ? `Editar: ${dish.name}` : 'Nuevo plato';
  });

  const priceField = _makeField('Precio *', 'number', dish.price !== null && dish.price !== '' ? dish.price : '', 'Ej: 25000');
  priceField.querySelector('input').id = 'dish-input-price';
  priceField.querySelector('input').min = '0';
  priceField.querySelector('input').addEventListener('input', (e) => { dish.price = parseFloat(e.target.value) || 0; });

  const nameRow = document.createElement('div');
  nameRow.className = 'dish-field-row';
  nameRow.appendChild(nameField);
  nameRow.appendChild(priceField);
  body.appendChild(nameRow);

  // ── 3. Description ────────────────────────────────────────────────
  const descField = _makeTextareaField('Descripción (Opcional)', dish.description || '', 'Ingredientes, tamaño, porciones...');
  descField.querySelector('textarea').addEventListener('input', (e) => { dish.description = e.target.value; });
  body.appendChild(descField);

  // ── 4. Tags (dietary) ─────────────────────────────────────────────
  const tagsSection = _makeChipsSection(
    'Categorías dietarias',
    // "Popular" is offered once, as a badge; old dishes keep these tags on re-save.
    Object.keys(DISH_LABELS.tags).filter(s => s !== 'popular' && s !== 'popular_latam'),
    dish.tags || [],
    'tags',
    'tag',
    (selected) => { dish.tags = selected; }
  );
  body.appendChild(tagsSection);

  // ── 5. Badges ─────────────────────────────────────────────────────
  const badgesSection = _makeChipsSection(
    'Badges',
    Object.keys(DISH_LABELS.badges),
    dish.badges || [],
    'badges',
    'badge',
    (selected) => { dish.badges = selected; }
  );
  body.appendChild(badgesSection);

  // ── 6. Allergens ─────────────────────────────────────────────────
  const allergensSection = _makeChipsSection(
    'Alérgenos (Contiene...)',
    Object.keys(DISH_LABELS.allergens),
    dish.allergens || [],
    'allergens',
    'allergen',
    (selected) => { dish.allergens = selected; },
    true // allergen variant
  );
  body.appendChild(allergensSection);

  // ── 7. Calories + Prep time ───────────────────────────────────────
  const calField = _makeField('Calorías (Opcional)', 'number', dish.calories != null ? dish.calories : '', 'Ej: 650');
  calField.querySelector('input').min = '0';
  calField.querySelector('input').addEventListener('input', (e) => {
    dish.calories = e.target.value !== '' ? parseInt(e.target.value, 10) : null;
  });

  const prepField = _makeField('Tiempo prep. (min, Opcional)', 'number', dish.prep_time_min != null ? dish.prep_time_min : '', 'Ej: 15');
  prepField.querySelector('input').min = '0';
  prepField.querySelector('input').addEventListener('input', (e) => {
    dish.prep_time_min = e.target.value !== '' ? parseInt(e.target.value, 10) : null;
  });

  const extraRow = document.createElement('div');
  extraRow.className = 'dish-field-row';
  extraRow.appendChild(calField);
  extraRow.appendChild(prepField);
  body.appendChild(extraRow);

  // ── 8. Toggles (Destacado, Activo) ───────────────────────────────
  const togglesWrap = document.createElement('div');
  togglesWrap.style.cssText = 'border:1px solid var(--border); border-radius:var(--radius-sm); overflow:hidden;';

  togglesWrap.appendChild(_makeToggle(
    'Destacado',
    'Aparece en el hero carousel del catálogo público',
    !!dish.featured,
    (v) => { dish.featured = v; }
  ));
  togglesWrap.appendChild(_makeToggle(
    'Activo',
    'Los platos inactivos no se muestran en el catálogo público',
    dish.active !== false,
    (v) => { dish.active = v; }
  ));

  body.appendChild(togglesWrap);
}

// ── Image zone rendering ─────────────────────────────────────────────
function _renderImageZoneContent(zone, dish) {
  zone.innerHTML = '';

  if (_dishModalUploading) {
    zone.classList.remove('has-image');
    const uploading = document.createElement('div');
    uploading.className = 'dish-image-uploading';
    uploading.innerHTML = `
      <div class="dish-image-spinner"></div>
      <span class="dish-upload-progress-text">Subiendo imagen...</span>
    `;
    zone.appendChild(uploading);
    return;
  }

  if (dish.image_url) {
    zone.classList.add('has-image');
    const img = document.createElement('img');
    img.className = 'dish-image-preview';
    img.src = mesioImageUrl(dish.image_url, 'card');
    img.alt = 'Vista previa del plato';
    zone.appendChild(img);

    const actions = document.createElement('div');
    actions.className = 'dish-image-actions';

    const replaceBtn = document.createElement('button');
    replaceBtn.className = 'dish-image-action-btn dish-image-action-btn--replace';
    replaceBtn.textContent = 'Reemplazar';
    replaceBtn.type = 'button';
    replaceBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      document.getElementById('dish-file-input').click();
    });
    actions.appendChild(replaceBtn);

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'dish-image-action-btn dish-image-action-btn--delete';
    deleteBtn.textContent = 'Eliminar';
    deleteBtn.type = 'button';
    deleteBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const ok = await mesioConfirm('¿Eliminar la foto de este plato?', { danger: true, confirmText: 'Eliminar' });
      if (!ok) return;
      await _deleteDishImage(dish, zone);
    });
    actions.appendChild(deleteBtn);

    zone.appendChild(actions);
  } else {
    zone.classList.remove('has-image');
    const hint = document.createElement('div');
    hint.className = 'dish-image-zone-hint';
    hint.innerHTML = '<strong>Haz clic o arrastra</strong> una imagen aquí';
    const sub = document.createElement('div');
    sub.className = 'dish-image-zone-sub';
    sub.textContent = 'JPG, PNG o WebP · Máx. 5 MB';
    zone.appendChild(hint);
    zone.appendChild(sub);
  }
}

// ── Upload image to Cloudinary via signed upload ─────────────────────
async function _handleImageFile(file, zone, dish) {
  if (!file.type.startsWith('image/')) {
    mesioToast('Solo se permiten imágenes (JPG, PNG, WebP)', 'error');
    return;
  }
  const MAX_MB = 5;
  if (file.size > MAX_MB * 1024 * 1024) {
    mesioToast(`La imagen supera los ${MAX_MB} MB. Usa una imagen más pequeña.`, 'error');
    return;
  }

  _dishModalUploading = true;
  _renderImageZoneContent(zone, dish);

  // Disable save button during upload
  const saveBtn = document.getElementById('dish-modal-save');
  if (saveBtn) saveBtn.disabled = true;

  try {
    // 1. Get signed upload params from backend
    const signRes = await fetch('/api/menu/image/sign', {
      method: 'POST',
      headers: { ...mesioHeaders() }
    });
    if (!signRes.ok) {
      const err = await signRes.json().catch(() => ({}));
      throw new Error(err.detail || 'No se pudo firmar el upload');
    }
    const { signature, timestamp, api_key, cloud_name, folder, public_id_prefix } = await signRes.json();

    // 2. Upload directly to Cloudinary (browser → Cloudinary, never through our server)
    const formData = new FormData();
    formData.append('file', file);
    formData.append('signature', signature);
    formData.append('timestamp', timestamp);
    formData.append('api_key', api_key);
    formData.append('folder', folder);
    if (public_id_prefix) formData.append('public_id', public_id_prefix + '_' + Date.now());

    const cloudUrl = `https://api.cloudinary.com/v1_1/${encodeURIComponent(cloud_name)}/image/upload`;
    const uploadRes = await fetch(cloudUrl, { method: 'POST', body: formData });
    if (!uploadRes.ok) {
      const errBody = await uploadRes.json().catch(() => ({}));
      throw new Error((errBody.error && errBody.error.message) || 'Error al subir a Cloudinary');
    }
    const uploadData = await uploadRes.json();

    // 3. Store in dish state
    // If there was a previous image, delete it
    if (dish.image_public_id && dish.image_public_id !== uploadData.public_id) {
      await fetch('/api/menu/image', {
        method: 'DELETE',
        headers: { ...mesioHeaders() },
        body: JSON.stringify({ public_id: dish.image_public_id })
      }).catch(() => {});
    }

    dish.image_url = uploadData.secure_url;
    dish.image_public_id = uploadData.public_id;

    mesioToast('Imagen subida correctamente', 'success');
  } catch (err) {
    mesioToast('Error subiendo imagen: ' + err.message, 'error');
    dish.image_url = dish.image_url || null; // keep previous if any
    dish.image_public_id = dish.image_public_id || null;
  } finally {
    _dishModalUploading = false;
    if (saveBtn) saveBtn.disabled = false;
    _renderImageZoneContent(zone, dish);
  }
}

// ── Delete dish image from Cloudinary ───────────────────────────────
async function _deleteDishImage(dish, zone) {
  if (!dish.image_public_id) {
    dish.image_url = null;
    _renderImageZoneContent(zone, dish);
    return;
  }
  try {
    await fetch('/api/menu/image', {
      method: 'DELETE',
      headers: { ...mesioHeaders() },
      body: JSON.stringify({ public_id: dish.image_public_id })
    });
    dish.image_url = null;
    dish.image_public_id = null;
    mesioToast('Imagen eliminada', 'success', 1500);
  } catch (e) {
    mesioToast('Error al eliminar la imagen', 'error');
    return;
  }
  _renderImageZoneContent(zone, dish);
}

// ── Form helpers ─────────────────────────────────────────────────────
function _makeField(labelText, type, value, placeholder) {
  const wrap = document.createElement('div');
  wrap.className = 'dish-field';
  const lbl = document.createElement('label');
  lbl.textContent = labelText;
  const inp = document.createElement('input');
  inp.type = type;
  inp.value = value;
  inp.placeholder = placeholder || '';
  if (type === 'number') inp.step = 'any';
  wrap.appendChild(lbl);
  wrap.appendChild(inp);
  return wrap;
}

function _makeTextareaField(labelText, value, placeholder) {
  const wrap = document.createElement('div');
  wrap.className = 'dish-field';
  const lbl = document.createElement('label');
  lbl.textContent = labelText;
  const ta = document.createElement('textarea');
  ta.value = value;
  ta.placeholder = placeholder || '';
  wrap.appendChild(lbl);
  wrap.appendChild(ta);
  return wrap;
}

function _makeChipsSection(title, slugs, selected, group, dataAttr, onChange, isAllergen) {
  const wrap = document.createElement('div');
  wrap.className = 'dish-field';

  const lbl = document.createElement('label');
  lbl.textContent = title;
  wrap.appendChild(lbl);

  const chipsWrap = document.createElement('div');
  chipsWrap.className = 'dish-chips-group';

  const currentSelected = new Set(selected);

  slugs.forEach(slug => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'dish-chip' + (isAllergen ? ' dish-chip--allergen' : '');
    if (currentSelected.has(slug)) chip.classList.add('selected');
    chip.dataset[dataAttr] = slug;
    chip.textContent = _dishLabel(group, slug);
    chip.setAttribute('aria-pressed', currentSelected.has(slug) ? 'true' : 'false');
    chip.setAttribute('aria-label', _chipAriaLabel(group, slug, currentSelected.has(slug)));
    chip.addEventListener('click', () => {
      if (currentSelected.has(slug)) {
        currentSelected.delete(slug);
        chip.classList.remove('selected');
        chip.setAttribute('aria-pressed', 'false');
        chip.setAttribute('aria-label', _chipAriaLabel(group, slug, false));
      } else {
        currentSelected.add(slug);
        chip.classList.add('selected');
        chip.setAttribute('aria-pressed', 'true');
        chip.setAttribute('aria-label', _chipAriaLabel(group, slug, true));
      }
      onChange([...currentSelected]);
    });
    chipsWrap.appendChild(chip);
  });

  wrap.appendChild(chipsWrap);
  return wrap;
}

function _makeToggle(label, hint, checked, onChange) {
  const row = document.createElement('div');
  row.className = 'dish-toggle-row';

  const labelDiv = document.createElement('div');
  labelDiv.className = 'dish-toggle-label';
  const labelSpan = document.createElement('span');
  labelSpan.textContent = label;
  const hintSpan = document.createElement('span');
  hintSpan.textContent = hint;
  labelDiv.appendChild(labelSpan);
  labelDiv.appendChild(hintSpan);
  row.appendChild(labelDiv);

  const switchLabel = document.createElement('label');
  switchLabel.className = 'dish-toggle-switch';
  switchLabel.setAttribute('aria-label', label);

  const inp = document.createElement('input');
  inp.type = 'checkbox';
  inp.checked = checked;
  inp.addEventListener('change', () => onChange(inp.checked));

  const slider = document.createElement('span');
  slider.className = 'dish-toggle-slider';

  switchLabel.appendChild(inp);
  switchLabel.appendChild(slider);
  row.appendChild(switchLabel);
  return row;
}

// ── Save dish modal → write back to editorMenuState ──────────────────
function saveDishModal() {
  if (!_dishModalState) return;
  if (_dishModalUploading) {
    mesioToast('Espera a que termine la subida de imagen', 'warning');
    return;
  }

  const dish = _dishModalState.dish;

  // Validate required fields
  const nameInput = document.getElementById('dish-input-name');
  const priceInput = document.getElementById('dish-input-price');

  const name = nameInput ? nameInput.value.trim() : dish.name.trim();
  if (!name) {
    mesioToast('El nombre del plato es obligatorio', 'error');
    if (nameInput) nameInput.focus();
    return;
  }

  const priceRaw = priceInput ? priceInput.value : String(dish.price);
  const numPrice = parseFloat(priceRaw);
  if (isNaN(numPrice) || numPrice < 0) {
    mesioToast('El precio es inválido', 'error');
    if (priceInput) priceInput.focus();
    return;
  }

  // Write back all values including any direct input changes not yet synced
  dish.name = name;
  dish.price = numPrice;

  // Commit to editorMenuState
  const { catIndex, dishIndex } = _dishModalState;
  editorMenuState[catIndex].dishes[dishIndex] = { ...dish };

  closeDishModal();
  renderMenuEditor();
}

// ── Build final menu for API ─────────────────────────────────────────
function _buildFinalMenu() {
  const finalMenu = {};
  for (const catObj of editorMenuState) {
    const catName = catObj.catName.trim();
    if (!catName) continue;
    finalMenu[catName] = [];
    for (const dish of catObj.dishes) {
      const name = dish.name.trim();
      if (!name) continue;
      finalMenu[catName].push({
        name:            name,
        description:     dish.description ? dish.description.trim() : '',
        price:           typeof dish.price === 'number' ? dish.price : parseFloat(dish.price) || 0,
        sku:             dish.sku             || null,
        image_url:       dish.image_url       || null,
        image_public_id: dish.image_public_id || null,
        tags:            dish.tags            || [],
        badges:          dish.badges          || [],
        allergens:       dish.allergens       || [],
        featured:        !!dish.featured,
        active:          dish.active !== false,
        sort_order:      typeof dish.sort_order === 'number' ? dish.sort_order : 999,
        calories:        dish.calories        != null ? dish.calories        : null,
        prep_time_min:   dish.prep_time_min   != null ? dish.prep_time_min   : null,
      });
    }
  }
  return finalMenu;
}

async function saveMenuEditor() {
  const finalMenu = _buildFinalMenu();

  // Basic validation
  for (const [catName, dishes] of Object.entries(finalMenu)) {
    for (const dish of dishes) {
      if (isNaN(dish.price) || dish.price < 0) {
        mesioToast(`Precio inválido en "${dish.name}"`, 'error');
        return;
      }
    }
  }

  const btn = document.getElementById('btn-save-menu-editor');
  if (btn) { btn.textContent = 'Guardando...'; btn.disabled = true; }

  try {
    const r = await fetch('/api/menu/update', {
      method: 'PUT',
      headers: { ...mesioHeaders() },
      body: JSON.stringify({ menu: finalMenu })
    });

    if (r.ok) {
      mesioToast('Carta guardada', 'success');
      closeMenuEditor();
      loadMenu();
      // The page that hosts the editor owns the dish list; tell it to reload.
      document.dispatchEvent(new CustomEvent('mesio:menu-saved'));
    } else {
      const e = await r.json().catch(() => ({}));
      mesioToast('Error al guardar: ' + (e.detail || 'Fallo desconocido'), 'error');
    }
  } catch (e) {
    mesioToast('Error de conexión al guardar', 'error');
  } finally {
    if (btn) { btn.textContent = 'Guardar Cambios'; btn.disabled = false; }
  }
}
