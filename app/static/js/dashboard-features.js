/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Features
   app/static/dashboard-features.js
═══════════════════════════════════════════════════ */

// _escHtml provided by mesio-utils.js
if (typeof _escHtml === 'undefined') {
  function _escHtml(s) { if(s==null)return''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
}

// ── MENÚ ─────────────────────────────────────────────────────────────
let menuAvailability = {};
let MENU_ITEMS = [];
let editorMenuState = []; // 🛡️ FIX: Declarado al inicio para evitar errores de TDZ

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
            // Extended shape (catálogo v2)
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
  
  // Lógica de visibilidad de botones exclusivos de Matriz
  const role = localStorage.getItem('rb_role') || '';
  const branchVal = window._dashHeaders['X-Branch-ID'];
  const isMatriz = (!branchVal || branchVal === 'matriz');
  
  const btnEdit = document.getElementById('btn-edit-menu');
  const btnSync = document.getElementById('btn-sync-menu');
  
  if (btnEdit) btnEdit.style.display = (role.includes('owner') && isMatriz) ? '' : 'none';
  if (btnSync) btnSync.style.display = (role.includes('owner') && isMatriz) ? '' : 'none';

  renderMenu();
}

// 🛡️ Memoria global para recordar qué pestañas del menú dejamos abiertas
window._openMenuCats = window._openMenuCats || new Set();

function renderMenu() {
  const grid = document.getElementById('menu-grid');
  if (!grid) return;
  if (!MENU_ITEMS.length) {
    grid.innerHTML = '<div style="padding:2rem;text-align:center;color:#aaa;font-size:13px;">Sin platos en el menú.</div>';
    return;
  }
  const cats = [...new Set(MENU_ITEMS.map(m => m.cat))];
  
  // Si es la primera vez que carga y no hay nada abierto, abrimos la primera categoría por defecto
  if (window._openMenuCats.size === 0 && cats.length > 0) {
    const firstCat = /\p{Emoji}/u.test(cats[0]) ? cats[0] : `🍽️ ${cats[0]}`;
    window._openMenuCats.add(firstCat);
  }

  grid.innerHTML = cats.map((cat, ci) => {
    const items = MENU_ITEMS.filter(m => m.cat === cat);
    const avail = items.filter(m => menuAvailability[m.name] !== false).length;
    
    const hasEmoji = /\p{Emoji}/u.test(cat);
    const displayCat = hasEmoji ? cat : `🍽️ ${cat}`;
    
    // 🛡️ FIX: Leemos la memoria para saber si debe estar abierto o cerrado
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
  
  // 🛡️ FIX: Actualizamos la memoria cuando el usuario hace clic
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

// ── EDITOR DE MENÚ v2 — Catálogo visual (Fase 2) ──

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
    img.src = dish.image_url;
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
    Object.keys(DISH_LABELS.tags),
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
    img.src = dish.image_url;
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
      mesioToast('Carta actualizada en la Casa Matriz', 'success');
      closeMenuEditor();
      loadMenu();
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

// ── MESAS & QR ────────────────────────────────────────────────────────
async function loadTables() {
  const h = window._dashHeaders;
  const rest = window._dashRestaurant;
  const grid = document.getElementById('tables-grid');
  if (!grid) return;
  try {
    const r = await fetch('/api/tables', { headers: h });
    if (!r.ok) return;
    const { tables } = await r.json();
    if (!tables.length) {
      grid.innerHTML = '<div style="text-align:center;padding:2rem;color:#aaa;font-size:13px;grid-column:1/-1;">No hay mesas configuradas.</div>';
      return;
    }
    grid.innerHTML = tables.map(t => `
      <div style="background:#fff;border:0.5px solid #e0e0d8;border-radius:12px;padding:1.25rem;text-align:center;">
        <div style="font-size:28px;margin-bottom:6px;">🪑</div>
        <div style="font-size:15px;font-weight:600;margin-bottom:2px;">${_escHtml(t.name)}</div>
        <div style="font-size:11px;color:#888;margin-bottom:12px;">ID: ${_escHtml(t.id)}</div>
        <div id="qr-${t.id}" style="width:120px;height:120px;margin:0 auto 10px;"></div>
        <div style="display:flex;gap:6px;justify-content:center;flex-wrap:wrap;">
          <a href="/api/tables/${t.id}/qr-sheet" target="_blank" style="font-size:11px;padding:5px 10px;background:#E1F5EE;color:#0F6E56;border-radius:6px;text-decoration:none;font-weight:500;">🖨️ Imprimir QR</a>
          <button onclick="deleteTable('${t.id}')" style="font-size:11px;padding:5px 10px;background:#FDE8E8;color:#C0392B;border:none;border-radius:6px;cursor:pointer;">Eliminar</button>
        </div>
      </div>`).join('');

      if (typeof QRCode !== 'undefined') {
        tables.forEach(t => {
          const el = document.getElementById('qr-' + t.id);
          if (el && !el.hasChildNodes()) {
            const botNum = (rest && rest.whatsapp_number) ? rest.whatsapp_number : null;
          
          if (!botNum) {
              console.error("No se encontró el número de WhatsApp para este restaurante.");
              return; // Detiene la generación del QR para esta mesa y evita enlaces rotos
          }

          const catalogUrl = window.location.origin + '/catalog?bot=' + encodeURIComponent(botNum) + '&mesa=' + encodeURIComponent(t.name) + '&table_id=' + encodeURIComponent(t.id);
            try { 
              new QRCode(el, { 
                text: catalogUrl, 
                width: 120, 
                height: 120, 
                colorDark: '#0D1412', 
                colorLight: '#ffffff', 
                correctLevel: QRCode.CorrectLevel.M 
              }); 
            } catch(e) {
              console.error("Error generando QR:", e);
            }
          }
        });
      } 
  } catch(e) { console.error('loadTables:', e); }
}

async function createTable() {
  const h = window._dashHeaders;
  
  // Ya no leemos inputs manuales, el backend asigna el ID inteligentemente
  try {
    const r = await fetch('/api/tables', {
      method: 'POST', 
      headers: { ...h, 'Content-Type': 'application/json' }
      // Hemos eliminado el body. El backend sabe quién es el usuario y qué sucursal es.
    });
    
    if (r.ok) {
      // Si los inputs viejos siguen en tu HTML (dashboard.html), los limpiamos y ocultamos para no confundir
      const inputNum = document.getElementById('new-table-num');
      const inputName = document.getElementById('new-table-name');
      if (inputNum) { inputNum.value = ''; inputNum.style.display = 'none'; }
      if (inputName) { inputName.value = ''; inputName.style.display = 'none'; }
      
      loadTables(); // Refresca la cuadrícula de mesas con la nueva ya creada
    } else {
      const err = await r.json().catch(() => ({}));
      alert('Error al crear mesa: ' + (err.detail || r.status));
    }
  } catch(e) { 
    alert('Error de conexión al intentar crear la mesa.'); 
  }
}

async function deleteTable(tableId) {
  if (!confirm('¿Eliminar esta mesa?')) return;
  try {
    await fetch('/api/tables/' + tableId, { method: 'DELETE', headers: window._dashHeaders });
    loadTables();
  } catch(e) {}
}

// ── MI EQUIPO ─────────────────────────────────────────────────────────
let allBranches = [];
let currentBranchId = null;
let selectedRoles = new Set(['mesero']); // Set para guardar los multiroles

async function loadBranches() {
  const h = window._dashHeaders;
  const rest = window._dashRestaurant;
  const role = (rest && rest.role) || 'owner';
  const btnCreate = document.getElementById('btn-create-branch');
  if (btnCreate) btnCreate.style.display = role.includes('owner') ? '' : 'none';
  try {
    const r = await fetch('/api/team/branches', { headers: h });
    if (r.status === 401) { logout(); return; }
    const d = await r.json();
    allBranches = d.branches || [];
    const countEl = document.getElementById('branch-count');
    if (countEl) countEl.textContent = allBranches.length + ' sucursal(es)';
    renderBranches(allBranches);
  } catch(e) { console.error('loadBranches:', e); }
}

function filterBranches() {
  const q = document.getElementById('branch-search').value.toLowerCase();
  renderBranches(allBranches.filter(b => b.name.toLowerCase().includes(q) || (b.whatsapp_number||'').includes(q)));
}

function renderBranches(branches) {
  const container = document.getElementById('branches-list');
  if (!container) return;
  if (!branches.length) { container.innerHTML = '<div class="empty-state">No hay sucursales.</div>'; return; }
  
  container.innerHTML = branches.map(b => {
    // 🛡️ FIX VISUAL: Cortamos el identificador interno (_b...) para que solo se vea el número
    const cleanWa = (b.whatsapp_number || 'N/A').split('_b')[0];
    
    return `
    <div style="background:#fff;border:0.5px solid #e0e0d8;border-radius:12px;margin-bottom:12px;overflow:hidden;">
      <div data-branch-id="${b.id}" style="display:flex;align-items:center;justify-content:space-between;padding:1rem 1.25rem;border-bottom:0.5px solid #f0f0e8;flex-wrap:wrap;gap:8px;">
        <div>
          <div style="font-size:15px;font-weight:600;">${_escHtml(b.name)}</div>
          <div style="font-size:11px;color:#888;margin-top:2px;"><span style="background:#E1F5EE;color:#0F6E56;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:500;margin-right:6px;">WA: +${_escHtml(cleanWa)}</span>${_escHtml(b.address||'')}</div>
        </div>
        <button onclick="openInviteModal(${b.id},'${b.name.replace(/'/g,"\\'")}')" style="background:#E1F5EE;color:#0F6E56;border:none;padding:7px 14px;border-radius:8px;font-size:12px;cursor:pointer;font-weight:500;">+ Añadir Admin</button>
      </div>
      <div id="users-branch-${b.id}" style="padding:.75rem 1.25rem;"><div style="font-size:11px;color:#aaa;">Cargando...</div></div>
    </div>`;
  }).join('');

  branches.forEach(b => loadBranchUsers(b.id));

  const role = localStorage.getItem('rb_role') || '';
  if (role.includes('owner')) {
    branches.forEach(b => {
      const header = document.querySelector('[data-branch-id="' + b.id + '"]');
      if (header) {
        const btn = document.createElement('button');
        btn.textContent = 'Eliminar';
        btn.style.cssText = 'background:#FDE8E8;color:#C0392B;border:none;padding:7px 12px;border-radius:8px;font-size:12px;cursor:pointer;';
        btn.onclick = () => deleteBranch(b.id, b.name);
        header.appendChild(btn);
      }
    });
  }
}

function formatRoles(roleStr) { return ''; } // Ya no es necesaria aquí pero la dejamos vacía por si acaso

async function loadBranchUsers(branchId) {
  const h = window._dashHeaders;
  try {
    const r = await fetch('/api/team/users?branch_id=' + branchId, { headers: h });
    if (!r.ok) return;
    const users = ((await r.json()).users || []).filter(u => u.branch_id == branchId);
    const el = document.getElementById('users-branch-' + branchId);
    if (!el) return;
    if (!users.length) { el.innerHTML = '<div style="font-size:12px;color:#aaa;padding:4px 0;">Sin administradores asignados</div>'; return; }

    el.innerHTML = '<div style="display:flex;flex-wrap:wrap;gap:12px;">' +
      users.map(u => {
        const displayName = u.display_name || u.username || '?';
        return `
        <div style="display:flex;align-items:center;gap:12px;background:#f8f8f5;border-radius:8px;padding:8px 12px;width:100%;max-width:340px;justify-content:space-between;border:1px solid #f0f0e8;">
          <div style="display:flex;align-items:center;gap:10px;">
            <div style="width:34px;height:34px;border-radius:50%;background:#e0e0d8;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600;color:#555;">${_escHtml(displayName[0].toUpperCase())}</div>
            <div>
              <div style="font-size:13px;font-weight:600;color:#333;">${_escHtml(displayName)}</div>
              <div style="font-size:11px;color:#888;margin-top:2px;">🛡️ Administrador</div>
            </div>
          </div>
          <button onclick="deleteUser('${(u.username||'').replace(/'/g,"\\'")}')" style="background:#FDE8E8;border:none;color:#C0392B;border-radius:6px;font-size:16px;cursor:pointer;width:28px;height:28px;display:flex;align-items:center;justify-content:center;">×</button>
        </div>`;
      }).join('') + '</div>';
  } catch(e) {}
}

// 🗺️ LÓGICA DEL MAPA INTERACTIVO
// 🗺️ LÓGICA DEL MAPA INTERACTIVO (MEJORADA)
let locationMap = null;
let locationMarker = null;

function showCreateBranch() {
  document.getElementById('create-branch-form').style.display = 'block';
  document.getElementById('branch-name').focus();
  
  if (!locationMap) {
      // Centramos por defecto en Colombia (Bogotá)
      const defaultLat = 4.6097; 
      const defaultLon = -74.0817;
      
      locationMap = L.map('interactive-map').setView([defaultLat, defaultLon], 12);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
          attribution: '© OpenStreetMap'
      }).addTo(locationMap);

      locationMarker = L.marker([defaultLat, defaultLon], { draggable: true }).addTo(locationMap);

      // 1. Cuando se arrastra el pin
      locationMarker.on('dragend', function (e) {
          updatePinLocation(locationMarker.getLatLng());
      });

      // 2. Mover el pin con solo hacer CLIC en el mapa
      locationMap.on('click', function(e) {
          locationMarker.setLatLng(e.latlng);
          updatePinLocation(e.latlng);
      });
  } else {
      setTimeout(() => locationMap.invalidateSize(), 200);
  }
}

// 🪄 Función que traduce la coordenada a una dirección CORTA y limpia
async function updatePinLocation(latlng) {
  document.getElementById('branch-lat').value = latlng.lat;
  document.getElementById('branch-lon').value = latlng.lng;
  
  try {
      // Añadimos &addressdetails=1 para que nos dé las partes separadas
      const res = await fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${latlng.lat}&lon=${latlng.lng}&addressdetails=1`);
      if (res.ok) {
          const data = await res.json();
          if (data && data.address) {
              const a = data.address;
              
              // Extraemos solo lo útil: Calle, Número, Barrio, Ciudad
              const street = a.road || a.pedestrian || a.path || '';
              const number = a.house_number || '';
              const hood   = a.neighbourhood || a.suburb || '';
              const city   = a.city || a.town || a.county || '';
              
              // Armamos una dirección comercial natural
              let cleanAddress = `${street} ${number}`.trim();
              if (hood) cleanAddress += `, ${hood}`;
              if (city) cleanAddress += `, ${city}`;
              
              // Limpiamos comas extra y lo ponemos en el input
              document.getElementById('branch-address').value = cleanAddress.replace(/^,|,$/g, '').trim() || data.display_name;
          } else if (data && data.display_name) {
              document.getElementById('branch-address').value = data.display_name;
          }
      }
  } catch (e) {
      console.error("Error obteniendo el texto de la dirección:", e);
  }
}

// 🔍 Buscador de texto directo a OpenStreetMap (Más robusto)
async function validateAddress() {
  const addressInput = document.getElementById('branch-address').value.trim();
  if (!addressInput) { alert('Ingresa una dirección primero'); return; }
  
  const btn = document.querySelector('[onclick="validateAddress()"]');
  const prev = btn.textContent;
  btn.textContent = 'Buscando...'; btn.disabled = true;
  
  try {
    const r = await fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(addressInput)}&limit=1`);
    if (r.ok) {
      const data = await r.json();
      if (data && data.length > 0) {
          const lat = parseFloat(data[0].lat);
          const lon = parseFloat(data[0].lon);
          
          document.getElementById('branch-lat').value = lat;
          document.getElementById('branch-lon').value = lon;
          
          if (locationMap && locationMarker) {
              const newPos = new L.LatLng(lat, lon);
              locationMap.setView(newPos, 16);
              locationMarker.setLatLng(newPos);
          }
      } else {
          alert('❌ No se encontró la dirección exacta. Por favor, haz clic directamente en el mapa para ubicar el marcador.');
      }
    }
  } catch(e) {
    alert('❌ Error de red al buscar. Usa el clic en el mapa.');
  } finally { 
    btn.textContent = prev; btn.disabled = false; 
  }
}

function applyManualCoords() {
  const lat = document.getElementById('branch-lat-manual').value;
  const lon = document.getElementById('branch-lon-manual').value;
  if (lat && lon) {
    document.getElementById('branch-lat').value = lat;
    document.getElementById('branch-lon').value = lon;
    document.getElementById('branch-lat-display').textContent = parseFloat(lat).toFixed(6);
    document.getElementById('branch-lon-display').textContent = parseFloat(lon).toFixed(6);
    document.getElementById('branch-maps-link').href = 'https://www.google.com/maps?q=' + lat + ',' + lon;
    document.getElementById('branch-address-display').textContent = 'Coordenadas manuales';
    document.getElementById('branch-map-preview').style.display = 'block';
  }
}

// 🚀 CREAR SUCURSAL (VERSIÓN LIMPIA)
async function createBranch() {
  const h = window._dashHeaders;
  const name    = document.getElementById('branch-name').value.trim();
  const address = document.getElementById('branch-address').value.trim();
  const lat     = document.getElementById('branch-lat').value;
  const lon     = document.getElementById('branch-lon').value;
  
  if (!name)    { alert('El nombre es obligatorio'); return; }
  if (!address) { alert('Ingresa la dirección'); return; }
  
  try {
    const body = { name, whatsapp_number:'', address, menu:{} };
    if (lat && lon) { 
        body.latitude = parseFloat(lat); 
        body.longitude = parseFloat(lon); 
    }
    
    const r = await fetch('/api/team/branches', { 
        method:'POST', 
        headers:{ ...h,'Content-Type':'application/json' }, 
        body:JSON.stringify(body) 
    });
    
    if (r.ok) {
      // 1. Ocultamos el formulario
      document.getElementById('create-branch-form').style.display = 'none';
      
      // 2. Limpiamos los campos
      ['branch-name','branch-address','branch-lat','branch-lon'].forEach(id => { 
          document.getElementById(id).value = ''; 
      });
      
      // 3. Recargamos la vista de sucursales
      loadBranches();
      if (typeof loadGlobalBranches === 'function') loadGlobalBranches();
      
    } else { 
        const e = await r.json(); 
        alert('Error: ' + (e.detail||'No se pudo crear')); 
    }
  } catch(e) {
      console.error("Error al crear la sucursal:", e);
      alert('❌ Error de conexión.');
  }
}

// ── LÓGICA MULTIROL ──
function toggleRole(role, el) {
  const isAdminRole = role === 'admin' || role === 'gerente';
  const pwdField = document.getElementById('invite-password');
  const pinField = document.getElementById('invite-pin');

  if (isAdminRole) {
    // Admin exclusivo: limpia todos y selecciona solo admin
    selectedRoles = new Set(['admin']);
    document.querySelectorAll('#modal-invite .role-card').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
    // Admin requiere contraseña, no PIN
    if (pwdField) pwdField.style.display = '';
    if (pinField) pinField.style.display = 'none';
  } else {
    // Si admin estaba activo, se desmarca al elegir rol operativo
    if (selectedRoles.has('admin')) {
      selectedRoles.delete('admin');
      const adminCard = document.querySelector('#modal-invite .role-card[data-role="admin"]');
      if (adminCard) adminCard.classList.remove('active');
    }

    // Toggle para roles operativos (multi-rol)
    if (selectedRoles.has(role)) {
      if (selectedRoles.size === 1) return; // Al menos un rol siempre
      selectedRoles.delete(role);
      el.classList.remove('active');
    } else {
      selectedRoles.add(role);
      el.classList.add('active');
    }

    // Roles operativos usan PIN
    if (pwdField) pwdField.style.display = 'none';
    if (pinField) pinField.style.display = '';
  }

  document.getElementById('invite-role').value = Array.from(selectedRoles).join(',');
}

function openInviteModal(branchId, branchName) {
  currentBranchId = branchId;
  document.getElementById('modal-branch-name').textContent = branchName;
  document.getElementById('invite-username').value = '';
  document.getElementById('invite-password').value = '';
  if (document.getElementById('invite-phone')) document.getElementById('invite-phone').value = '';
  document.getElementById('invite-role').value = 'admin';
  document.getElementById('modal-invite').style.display = 'flex';
}

function closeInviteModal() {
  document.getElementById('modal-invite').style.display = 'none';
  currentBranchId = null;
}

async function sendInvite() {
  const h        = window._dashHeaders;
  const username = document.getElementById('invite-username').value.trim();
  const password = document.getElementById('invite-password').value.trim();
  const phone    = (document.getElementById('invite-phone') || {}).value?.trim() || '';

  if (!username || !password) { alert('El usuario y la contraseña son obligatorios.'); return; }

  try {
    const r = await fetch('/api/team/invite', {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, pin: '', phone, role: 'admin', branch_id: currentBranchId }),
    });
    if (r.ok) { closeInviteModal(); loadBranches(); alert('¡Admin creado exitosamente!'); }
    else { const e = await r.json(); alert('Error: ' + (e.detail || 'No se pudo crear')); }
  } catch(e) {}
}

async function deleteBranch(id, name) {
  if (!confirm('Eliminar sucursal "' + name + '"?')) return;
  try {
    const r = await fetch('/api/team/branches/' + id, { method:'DELETE', headers: window._dashHeaders });
    if (r.ok) loadBranches();
    else { const e = await r.json(); alert('Error: ' + (e.detail||'No se pudo eliminar')); }
  } catch(e) {}
}

async function deleteUser(userId) {
  if (!confirm('Eliminar miembro "' + userId + '"?')) return;
  try {
    const r = await fetch('/api/team/users/' + encodeURIComponent(userId), { method:'DELETE', headers: window._dashHeaders });
    if (r.ok) loadBranches();
    else { const e = await r.json(); alert('Error: ' + (e.detail || 'No se pudo eliminar')); }
  } catch(e) {}
}

// ── SESIONES DE MESA ──────────────────────────────────────────────────
let _sesionHours  = 24;
let _currentSesId = null;

const CLOSE_REASON = {
  waiter_manual:      { text:'Mesero',      icon:'👤', color:'#BA7517', bg:'#FFF8E6' },
  inactivity_timeout: { text:'Inactividad', icon:'⏰', color:'#555',    bg:'#F5F5F0' },
  client_goodbye:     { text:'Cliente',     icon:'👋', color:'#0F6E56', bg:'#E1F5EE' },
  factura_entregada:  { text:'Factura OK',  icon:'🧾', color:'#6B21A8', bg:'#F0E6FF' },
  superseded:         { text:'Reemplazada', icon:'🔄', color:'#888',    bg:'#F5F5F0' },
};

function reasonBadge(r) {
  const d = CLOSE_REASON[r] || { text:r||'—', icon:'❓', color:'#888', bg:'#f0f0f0' };
  return `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:3px 8px;border-radius:20px;font-weight:500;background:${d.bg};color:${d.color};">${d.icon} ${d.text}</span>`;
}

function fmtDur(a, b) {
  if (!a||!b) return '—';
  // Añadimos la Z para asegurar que la matemática del tiempo sea exacta
  const zA = a.endsWith('Z') ? a : a + 'Z';
  const zB = b.endsWith('Z') ? b : b + 'Z';
  const m = Math.round((new Date(zB) - new Date(zA)) / 60000);
  return m < 60 ? m + 'min' : Math.floor(m/60) + 'h ' + (m%60) + 'min';
}

function fmtTime(iso) {
  if (!iso) return '—';
  const zIso = iso.endsWith('Z') ? iso : iso + 'Z';
  return new Date(zIso).toLocaleTimeString('es-CO', { hour:'2-digit', minute:'2-digit' });
}

function setSesionPeriod(h, btn) {
  _sesionHours = h;
  document.querySelectorAll('#sesiones .period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadSessions();
}

async function loadSessions() {
  const headers = window._dashHeaders;
  const c = document.getElementById('sessions-container');
  if (!c) return;
  c.innerHTML = '<div class="empty-state">Cargando...</div>';
  try {
    const r = await fetch('/api/table-sessions/closed?hours=' + _sesionHours, { headers });
    if (!r.ok) { c.innerHTML = '<div class="empty-state">Error.</div>'; return; }
    const { sessions = [] } = await r.json();

    const byWaiter = sessions.filter(s => s.closed_by === 'waiter_manual').length;
    document.getElementById('ses-total').textContent      = sessions.length;
    document.getElementById('ses-waiter').textContent     = byWaiter;
    document.getElementById('ses-client').textContent     = sessions.filter(s => s.closed_by === 'client_goodbye').length;
    document.getElementById('ses-inactivity').textContent = sessions.filter(s => s.closed_by === 'inactivity_timeout').length;
    document.getElementById('ses-badge').textContent      = sessions.length + ' sesiones';

    const banner = document.getElementById('ses-alert-banner');
    if (banner) banner.style.display = byWaiter > 0 ? 'flex' : 'none';

    if (!sessions.length) { c.innerHTML = '<div class="empty-state">Sin sesiones cerradas en este período.</div>'; return; }

    let html = `<table><thead><tr>
      <th>Mesa</th><th>Teléfono</th><th>Inicio</th><th>Cierre</th>
      <th>Duración</th><th>Cerrada por</th><th>Usuario</th><th>Total</th><th>Acciones</th>
    </tr></thead><tbody>`;
    sessions.forEach(s => {
      const warn = s.closed_by === 'waiter_manual';
      const safeTableName = (s.table_name||'').replace(/'/g,"\\'");
      const safeBotNumber = (s.bot_number||'').replace(/'/g,"\\'");
      const safePhone     = (s.phone||'').replace(/'/g,"\\'");
      const safeTableId   = (s.table_id||'').replace(/'/g,"\\'");
      html += `<tr class="${warn ? 'ses-warn-row' : ''}">
        <td style="font-weight:500;">${_escHtml(s.table_name||'—')}</td>
        <td style="color:#888;font-size:11px;">${_escHtml(s.phone)}</td>
        <td style="color:#888;">${fmtTime(s.started_at)}</td>
        <td style="color:#888;">${fmtTime(s.closed_at)}</td>
        <td>${fmtDur(s.started_at, s.closed_at)}</td>
        <td>${reasonBadge(s.closed_by)}</td>
        <td style="font-size:12px;${warn?'color:#BA7517;font-weight:500;':'color:#888;'}">${_escHtml(s.closed_by_username||'—')}${warn?' ⚠️':''}</td>
        <td style="font-weight:500;">${s.total_spent?'$'+Number(s.total_spent).toLocaleString('es-CO'):'—'}</td>
        <td>
          ${!s.total_spent
            ? `<button onclick="callWaiterAdmin('${safeBotNumber}','${safePhone}','${safeTableName}','${safeTableId}')"
                style="font-size:11px;padding:4px 9px;background:#FFF8E6;color:#BA7517;border:1px solid #FDE68A;border-radius:6px;cursor:pointer;font-weight:500;">
                📞 Llamar al Mesero
              </button>`
            : ''
          }
        </td>
      </tr>`;
    });
    html += '</tbody></table>';
    c.innerHTML = html;
  } catch(e) { console.error(e); c.innerHTML = '<div class="empty-state">Error de conexión.</div>'; }
}

async function viewSession(id, tableName, phone, closedBy) {
  const headers = window._dashHeaders;
  _currentSesId = id;
  document.getElementById('ses-modal-title').textContent = 'Sesión — ' + tableName;
  document.getElementById('ses-modal-sub').textContent   = phone;
  document.getElementById('ses-modal-msgs').innerHTML    = '<div style="text-align:center;font-size:12px;color:#888;padding:1rem;">Cargando...</div>';
  document.getElementById('ses-close-info').textContent  = '';
  document.getElementById('ses-action-feedback').style.display = 'none';
  document.getElementById('ses-msg-input').value         = '';
  document.getElementById('ses-waiter-msg-input').value  = '';
  const reopenRow = document.getElementById('ses-reopen-row');
  if (reopenRow) reopenRow.style.display = closedBy === 'waiter_manual' ? 'flex' : 'none';
  document.getElementById('ses-modal-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  try {
    const r = await fetch('/api/table-sessions/' + id + '/history', { headers });
    const d = await r.json();
    const session = d.session || {};
    const msgs    = d.history  || [];
    const reason  = CLOSE_REASON[session.closed_by] || { text:session.closed_by||'?', icon:'❓' };
    const infoEl  = document.getElementById('ses-close-info');
    infoEl.textContent = '';
    const _addBold = (parent, txt) => { const b = document.createElement('strong'); b.textContent = txt; parent.appendChild(b); };
    const _addText = (parent, txt) => parent.appendChild(document.createTextNode(txt));
    _addText(infoEl, 'Cerrada por: '); _addBold(infoEl, `${reason.icon} ${reason.text}`);
    if (session.closed_by_username) { _addText(infoEl, ' · usuario: '); _addBold(infoEl, session.closed_by_username); }
    _addText(infoEl, ` · duración: ${fmtDur(session.started_at, session.closed_at)}`);
    if (session.total_spent) { _addText(infoEl, ' · total: '); _addBold(infoEl, `$${Number(session.total_spent).toLocaleString('es-CO')}`); }
    const chatEl = document.getElementById('ses-modal-msgs');
    if (!msgs.length) {
      chatEl.textContent = '';
      const emptyDiv = document.createElement('div');
      emptyDiv.style.cssText = 'text-align:center;font-size:12px;color:#888;padding:1rem;';
      emptyDiv.textContent = 'Historial no disponible.';
      chatEl.appendChild(emptyDiv);
      return;
    }
    chatEl.textContent = '';
    msgs.forEach(m => {
      const isUser = m.role === 'user';
      const text   = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
      const wrap = document.createElement('div');
      wrap.className = 'msg-bubble' + (isUser ? ' user' : '');
      const bubble = document.createElement('div');
      bubble.className = 'bubble ' + (isUser ? 'user' : 'bot');
      bubble.textContent = text;
      wrap.appendChild(bubble);
      chatEl.appendChild(wrap);
    });
    chatEl.scrollTop = chatEl.scrollHeight;
  } catch(e) {
    const errEl = document.getElementById('ses-modal-msgs');
    errEl.textContent = '';
    const errDiv = document.createElement('div');
    errDiv.style.cssText = 'text-align:center;font-size:12px;color:#888;padding:1rem;';
    errDiv.textContent = 'Error al cargar.';
    errEl.appendChild(errDiv);
  }
}

function closeSesModal() {
  document.getElementById('ses-modal-overlay').classList.remove('open');
  document.body.style.overflow = '';
  _currentSesId = null;
}

async function callWaiterAdmin(botNumber, phone, tableName, tableId) {
  const headers = window._dashHeaders;
  try {
    const r = await fetch('/api/waiter-alerts/admin-call', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ bot_number: botNumber, phone, table_name: tableName, table_id: tableId })
    });
    if (r.ok) {
      _showAdminCallToast('✅ Alerta enviada al mesero', true);
    } else {
      _showAdminCallToast('Error al enviar la alerta', false);
    }
  } catch(e) {
    _showAdminCallToast('Error de conexión', false);
  }
}

function _showAdminCallToast(msg, ok) {
  let el = document.getElementById('_admin-call-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = '_admin-call-toast';
    el.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:10px 20px;border-radius:20px;font-size:13px;font-weight:600;z-index:9999;transition:opacity .3s;pointer-events:none;';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.background = ok ? '#1D9E75' : '#E24B4A';
  el.style.color = '#fff';
  el.style.opacity = '1';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.opacity = '0'; }, 3000);
}

function showSesFeedback(msg, ok = true) {
  const el = document.getElementById('ses-action-feedback');
  el.textContent = msg;
  el.className = 'ses-feedback ' + (ok ? 'ok' : 'err');
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 4000);
}

async function reopenFromModal() {
  const headers = window._dashHeaders;
  if (!_currentSesId) return;
  if (!confirm('¿Reabrir esta sesión?')) return;
  try {
    const r = await fetch('/api/table-sessions/' + _currentSesId + '/reopen', { method:'POST', headers });
    if (r.ok) {
      showSesFeedback('✅ Sesión reabierta. El cliente puede volver a escribir.');
      document.getElementById('ses-reopen-row').style.display = 'none';
      loadSessions();
    } else { const e = await r.json(); showSesFeedback('Error: ' + (e.detail||'No se pudo reabrir.'), false); }
  } catch(e) { showSesFeedback('Error de conexión.', false); }
}

async function sendMsgFromModal() {
  const headers = window._dashHeaders;
  if (!_currentSesId) return;
  const msg = document.getElementById('ses-msg-input').value.trim();
  if (!msg) { showSesFeedback('Escribe un mensaje primero.', false); return; }
  try {
    const r = await fetch('/api/table-sessions/' + _currentSesId + '/send-message', {
      method:'POST', headers:{ ...headers,'Content-Type':'application/json' },
      body: JSON.stringify({ message: msg })
    });
    if (r.ok) { document.getElementById('ses-msg-input').value = ''; showSesFeedback('✅ Mensaje enviado al cliente.'); }
    else { const e = await r.json(); showSesFeedback('Error: ' + (e.detail||'No se pudo enviar.'), false); }
  } catch(e) { showSesFeedback('Error de conexión.', false); }
}

async function alertWaiterFromModal() {
  const headers = window._dashHeaders;
  if (!_currentSesId) return;
  const nota = document.getElementById('ses-waiter-msg-input').value.trim();
  try {
    const r = await fetch('/api/table-sessions/' + _currentSesId + '/alert-waiter', {
      method:'POST', headers:{ ...headers,'Content-Type':'application/json' },
      body: JSON.stringify({ message: nota })
    });
    if (r.ok) { document.getElementById('ses-waiter-msg-input').value = ''; showSesFeedback('✅ Alerta enviada al panel de meseros.'); }
    else { const e = await r.json(); showSesFeedback('Error: ' + (e.detail||'No se pudo alertar.'), false); }
  } catch(e) { showSesFeedback('Error de conexión.', false); }
}

// ── POS CON IA ───────────────────────────────────────────────────────
const posCache = { data: null, timestamp: 0, orderCount: 0 };
const CACHE_TTL = 8 * 60 * 60 * 1000;

async function loadPOSData(forceRefresh = false) {
  const headers = window._dashHeaders;
  const now = Date.now();
  const cacheValid = posCache.data && (now - posCache.timestamp) < CACHE_TTL;
  try {
    const r = await fetch('/api/dashboard/stats?period=today', { headers });
    if (r.ok) {
      const d = await r.json();
      const currentOrders = d.orders?.total || 0;
      if (cacheValid && !forceRefresh && currentOrders === posCache.orderCount) { renderPOSFromCache(); return; }
      posCache.orderCount = currentOrders;
    }
  } catch(e) {}
  posCache.timestamp = now;
  await loadPOSDataFresh();
}

function renderPOSFromCache() {
  if (!posCache.data) return;
  const { mainText, stockText, upsells, horaCounts, avgTicket, topPlato, topHora } = posCache.data;
  const mainEl = document.getElementById('ai-main-text');
  mainEl.textContent = mainText || '';
  if (mainText) {
    const cacheTag = document.createElement('span');
    cacheTag.style.cssText = 'font-size:10px;color:#888;';
    cacheTag.textContent = ' (caché)';
    mainEl.appendChild(cacheTag);
  }
  document.getElementById('ai-stock-text').textContent = stockText || '';
  if (upsells) document.getElementById('upsell-container').innerHTML = upsells;
  if (topHora) document.getElementById('pos-hora-pico').textContent = topHora[0] + 'h';
  if (avgTicket) document.getElementById('pos-ticket').textContent = '$' + avgTicket.toLocaleString('es-CO');
  if (topPlato) { document.getElementById('pos-top-plato').textContent = topPlato[0]; document.getElementById('pos-top-sub').textContent = topPlato[1] + ' pedidos'; }
  renderHoraDist(horaCounts || {});
  renderDemandBars(horaCounts || {});
}

async function loadPOSDataFresh() {
  const headers = window._dashHeaders;
  const rest    = window._dashRestaurant;
  try {
    const r = await fetch('/api/dashboard/orders?period=week', { headers });
    if (r.status === 401) { logout(); return; }
    const orders = (await r.json()).orders || [];
    const paid = orders.filter(o => o.paid);
    const avgTicket = paid.length > 0 ? Math.round(paid.reduce((s,o) => s+o.total, 0) / paid.length) : 0;
    document.getElementById('pos-ticket').textContent = avgTicket > 0 ? '$' + avgTicket.toLocaleString('es-CO') : '—';
    document.getElementById('pos-ticket-trend').textContent = paid.length + ' pedidos pagados esta semana';

    const platoCounts = {};
    orders.forEach(o => {
      let items = [];
      if (!o.items) return;
      if (typeof o.items === 'string' && o.items.trim().startsWith('[')) {
        try { items = JSON.parse(o.items).map(i => (i.quantity||1)+'x '+(i.name||'')); } catch(e) { items = o.items.split(', '); }
      } else if (Array.isArray(o.items)) { items = o.items.map(i => (i.quantity||1)+'x '+(i.name||'')); }
      else { items = o.items.split(', '); }
      items.forEach(item => { const name = item.replace(/^\d+x\s+/, '').trim(); if (name) platoCounts[name] = (platoCounts[name]||0) + 1; });
    });
    const topPlato = Object.entries(platoCounts).sort((a,b) => b[1]-a[1])[0];
    if (topPlato) { document.getElementById('pos-top-plato').textContent = topPlato[0]; document.getElementById('pos-top-sub').textContent = topPlato[1] + ' pedidos esta semana'; }

    const horaCounts = {};
    orders.forEach(o => { if (o.time) { const hora = o.time.split(':')[0]+':00'; horaCounts[hora] = (horaCounts[hora]||0)+1; } });
    const topHora = Object.entries(horaCounts).sort((a,b) => b[1]-a[1])[0];
    document.getElementById('pos-hora-pico').textContent = topHora ? topHora[0]+'h' : 'N/D';

    renderHoraDist(horaCounts);
    renderDemandBars(horaCounts);
    if (!posCache.data) posCache.data = {};
    Object.assign(posCache.data, { horaCounts, avgTicket, topPlato, topHora });
    await generateAIInsights(orders, avgTicket, topPlato, topHora);
  } catch(e) { console.error('POS error:', e); }
}

function renderHoraDist(horaCounts) {
  const horas = ['11:00','12:00','13:00','14:00','18:00','19:00','20:00','21:00','22:00'];
  const maxVal = Math.max(...Object.values(horaCounts), 1);
  const container = document.getElementById('hora-dist');
  if (!container) return;
  container.innerHTML = horas.map(h => {
    const val = horaCounts[h] || 0;
    const pct = Math.round(val / maxVal * 100);
    return `<div class="hora-item"><span class="hora-label">${h}</span><div class="hora-bar-wrap"><div class="hora-bar" style="width:${pct}%"></div></div><span class="hora-val">${val} ped</span></div>`;
  }).join('');
}

function renderDemandBars(horaCounts) {
  const now = new Date().getHours();
  const maxVal = Math.max(...Object.values(horaCounts), 1);
  const container = document.getElementById('demand-bars');
  if (!container) return;
  container.innerHTML = [now+1, now+2, now+3].map(h => {
    const hora = String(h%24).padStart(2,'0') + ':00';
    const base = horaCounts[hora] || 0;
    const predicted = Math.max(1, Math.round(base * (0.8 + Math.random() * 0.4)));
    const pct = Math.min(100, Math.round(predicted / maxVal * 100 + 20));
    const cls = pct > 70 ? '' : pct > 40 ? 'warn' : 'danger';
    const nivel = pct > 70 ? 'Alta demanda' : pct > 40 ? 'Demanda media' : 'Baja demanda';
    return `<div class="predict-bar"><div class="predict-label"><span>${hora}h — ${nivel}</span><span>~${predicted} pedidos esperados</span></div><div class="predict-track"><div class="predict-fill ${cls}" style="width:${pct}%"></div></div></div>`;
  }).join('');
}

async function generateAIInsights(orders, avgTicket, topPlato, topHora) {
  const rest = window._dashRestaurant;
  const totalRevenue = orders.filter(o => o.paid).reduce((s,o) => s+o.total, 0);
  const domicilio = orders.filter(o => o.type === 'domicilio').length;
  const recoger   = orders.filter(o => o.type === 'recoger').length;
  const dias  = ['domingo','lunes','martes','miércoles','jueves','viernes','sábado'];
  const hoy   = dias[new Date().getDay()];
  const ctx   = `Datos semana "${(rest&&rest.name)||'el restaurante'}":\n- Pedidos: ${orders.length} (${domicilio} dom, ${recoger} recoger)\n- Ingresos: $${totalRevenue.toLocaleString('es-CO')}\n- Ticket prom: $${avgTicket.toLocaleString('es-CO')}\n- Top plato: ${topPlato?topPlato[0]+' ('+topPlato[1]+')':'sin datos'}\n- Hora pico: ${topHora?topHora[0]:'sin datos'}\n- Hoy: ${hoy}`;

  // Proxy server-side — the API key never leaves the server.
  const callAI = async (sys, usr) => {
    const resp = await fetch('/api/ai/proxy', {
      method: 'POST',
      headers: { ...window._dashHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify({ system: sys, user: usr, max_tokens: 1000 })
    });
    if (!resp.ok) throw new Error('AI proxy ' + resp.status);
    const d = await resp.json();
    return d.text || '';
  };

  document.getElementById('ai-main-text').innerHTML = '<span class="ai-loading">Analizando...</span>';
  try {
    const mainText = await callAI(
      'Eres Mesio IA para restaurantes colombianos. Español, directo, máx 3 oraciones. Sin HTML.',
      ctx + '\n\nGenera un insight accionable para el gerente hoy.'
    );
    document.getElementById('ai-main-text').textContent = mainText;
    if (!posCache.data) posCache.data = {};
    posCache.data.mainText = mainText;
  } catch(e) { document.getElementById('ai-main-text').textContent = 'Conecta más pedidos para análisis.'; }

  try {
    const stockText = await callAI(
      'Eres Mesio IA. Español, máx 2 oraciones, enfocado en inventario. Sin HTML.',
      ctx + '\n\nHoy es ' + hoy + '. ¿Qué ingredientes asegurar con base en el plato top?'
    );
    document.getElementById('ai-stock-text').textContent = stockText;
    if (posCache.data) posCache.data.stockText = stockText;
  } catch(e) { document.getElementById('ai-stock-text').textContent = 'Datos insuficientes.'; }

  try {
    const raw = await callAI(
      'Eres Mesio IA. Responde SOLO JSON válido sin markdown: [{"icon":"emoji","texto":"sugerencia","ganancia":"impacto"}]. Máx 3 items.',
      ctx + '\n\nGenera 3 sugerencias de upsell para el bot de WhatsApp.'
    );
    const sugerencias = JSON.parse(raw.replace(/```json|```/g,'').trim());
    const upsellHtml = sugerencias.map(s => `
      <div class="upsell-card"><span class="upsell-icon">${_escHtml(s.icon)}</span><span class="upsell-text">${_escHtml(s.texto)}</span><span class="upsell-badge">${_escHtml(s.ganancia)}</span></div>`).join('');
    document.getElementById('upsell-container').innerHTML = upsellHtml;
    if (posCache.data) posCache.data.upsells = upsellHtml;
  } catch(e) { document.getElementById('upsell-container').innerHTML = '<div class="empty-state">Conecta más pedidos.</div>'; }
}

async function askMesioAI() {
  const question = document.getElementById('ai-question').value.trim();
  if (!question) return;
  const rest = window._dashRestaurant;
  const btn = document.querySelector('.ask-ai-btn');
  const responseDiv = document.getElementById('ai-response');
  btn.textContent = 'Pensando...'; btn.disabled = true;
  responseDiv.style.display = 'block';
  responseDiv.textContent   = '✦ Analizando tu pregunta...';
  try {
    // Proxy server-side — the API key never leaves the server.
    const resp = await fetch('/api/ai/proxy', {
      method: 'POST',
      headers: { ...window._dashHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        system: 'Eres Mesio IA, experto en restaurantes colombianos. Español, directo, máx 150 palabras. Sin HTML.',
        user: question,
        max_tokens: 1000
      })
    });
    const d = await resp.json();
    responseDiv.textContent = '✦ ' + (d.text || 'No pude procesar.');
  } catch(e) { responseDiv.textContent = 'Error al conectar.'; }
  btn.textContent = 'Preguntar a Mesio IA →'; btn.disabled = false;
}

// ── PEDIDOS MESA (dine-in) en sección Pedidos ────────────────────────
const STATUS_LABEL = {
  recibido:'Recibido', en_preparacion:'En preparación',
  listo:'Listo para servir', entregado:'Entregado',
  factura_entregada:'Factura entregada', cancelado:'Cancelado'
};
const STATUS_COLOR = {
  recibido:'#FAC775', en_preparacion:'#378ADD',
  listo:'#1D9E75', entregado:'#888',
  factura_entregada:'#6B21A8', cancelado:'#E24B4A'
};
const STATUS_BG = {
  recibido:'#FFF8E6', en_preparacion:'#E6F1FB',
  listo:'#E1F5EE', entregado:'#f0f0e8',
  factura_entregada:'#F0E6FF', cancelado:'#FEE2E2'
};

async function loadTableOrdersSection() {
  const h         = window._dashHeaders;
  const container = document.getElementById('dine-in-container');
  const mContainer = document.getElementById('salon-metrics-container');
  const domContainer = document.getElementById('rt-domicilios-container');
  
  if (!container) return;

  try {
    // ── 1. Cargar pedidos de mesa ──
    const rMesa = await fetch('/api/table-orders', { headers: h });
    const { orders: allMesa = [] } = rMesa.ok ? await rMesa.json() : { orders: [] };

    const dNow = new Date();
    const today = `${dNow.getFullYear()}-${String(dNow.getMonth()+1).padStart(2,'0')}-${String(dNow.getDate()).padStart(2,'0')}`;
    
    const visible = allMesa.filter(o => {
      const closed = o.status === 'factura_entregada' || o.status === 'cancelado';
      const dOrder = new Date((o.created_at || '') + (o.created_at?.endsWith('Z') ? '' : 'Z'));
      const orderDay = `${dOrder.getFullYear()}-${String(dOrder.getMonth()+1).padStart(2,'0')}-${String(dOrder.getDate()).padStart(2,'0')}`;
      if (o.status === 'entregado') return orderDay === today;
      return !closed;
    });

    const active = visible.filter(o => ['recibido','en_preparacion','listo'].includes(o.status));
    
    const mesasParaCobrar = new Set();
    visible.forEach(o => {
      if (o.status === 'entregado' || o.status === 'factura_generada') {
        mesasParaCobrar.add(o.base_order_id || o.id.replace(/-\d+$/, ''));
      }
    });

    const billsMap = {};
    visible.forEach(o => {
      const baseId = o.base_order_id || o.id.replace(/-\d+$/, '');
      if (mesasParaCobrar.has(baseId)) {
        if (!billsMap[baseId]) {
          billsMap[baseId] = { id: baseId, table_name: o.table_name, created_at: o.created_at, items: [], total: 0, status: o.status };
        }
        if (o.status === 'factura_generada') billsMap[baseId].status = 'factura_generada';
        let parsedItems = [];
        try { const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items; parsedItems = Array.isArray(arr) ? arr : []; } catch(e) {}
        billsMap[baseId].items.push(...parsedItems);
        billsMap[baseId].total += (Number(o.total) || 0);
      }
    });
    const groupedBills = Object.values(billsMap);
    // Almacenar en window para que markTableInvoiced pueda leer el total y los items
    window._billsData = {};
    groupedBills.forEach(b => { window._billsData[b.id] = b; });

    // ── 2. Cargar pedidos de domicilio/recoger ──
    const localOffset = new Date().getTimezoneOffset();
    const rDom = await fetch(`/api/dashboard/orders?period=today&tz_offset=${localOffset}`, { headers: h });
    const allOrders = rDom.ok ? ((await rDom.json()).orders || []) : [];
    const extOrders = allOrders.filter(o => o.type !== 'mesa');
    const activeExt = extOrders.filter(o => {
      const st = (o.status || '').toLowerCase();
      return !st.includes('entregado') && !st.includes('cancelado');
    });
    const domEntregados = extOrders.filter(o => (o.status||'').includes('entregado')).length;

    const fmt = n => '$' + Number(n).toLocaleString('es-CO');

    // ── 3. Métricas salón ──
    const enCocina   = active.filter(o => ['recibido','en_preparacion'].includes(o.status));
    const conMesero  = active.filter(o => o.status === 'listo');
    const mesasAtendidas = [...new Set(visible.map(o => o.table_id))].length;

    if (mContainer) {
      mContainer.innerHTML = `
        <div class="metric"><div class="metric-label">Mesas Atendidas</div><div class="metric-value">${mesasAtendidas}</div></div>
        <div class="metric"><div class="metric-label">En Cocina</div><div class="metric-value" style="color:#BA7517;">${enCocina.length}</div></div>
        <div class="metric"><div class="metric-label">Con Mesero (Listos)</div><div class="metric-value" style="color:#378ADD;">${conMesero.length}</div></div>
        <div class="metric"><div class="metric-label">En Caja (Por Cobrar)</div><div class="metric-value" style="color:#1D9E75;">${groupedBills.length}</div></div>
      `;
    }

    // ── 4. Monitor domicilios ──
    const rtDomTotal      = document.getElementById('rt-dom-total');
    const rtDomCocina     = document.getElementById('rt-dom-cocina');
    const rtDomEntrega    = document.getElementById('rt-dom-entrega');
    const rtDomEntregados = document.getElementById('rt-dom-entregados');
    if (rtDomTotal)      rtDomTotal.textContent      = extOrders.length;
    if (rtDomCocina)     rtDomCocina.textContent     = activeExt.filter(o => !['en_camino','en_entrega'].includes(o.status||'')).length;
    if (rtDomEntrega)    rtDomEntrega.textContent    = activeExt.filter(o => ['en_camino','en_entrega'].includes(o.status||'')).length;
    if (rtDomEntregados) rtDomEntregados.textContent = domEntregados;

    if (domContainer) {
      if (activeExt.length === 0) {
        domContainer.innerHTML = '<div class="empty-state">No hay domicilios activos en este momento.</div>';
      } else {
        let domHtml = '<div style="font-size:13px;font-weight:bold;margin-bottom:10px;">🕒 ACTIVOS EN PREPARACIÓN / ENTREGA</div>';
        domHtml += '<table><thead><tr><th>Teléfono</th><th>Platos</th><th>Dirección</th><th>Pago</th><th>Estado</th><th>Total</th><th>Acción</th></tr></thead><tbody>';
        activeExt.forEach(o => {
          let itemsStr = '—';
          try {
            const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items;
            itemsStr = Array.isArray(arr) ? arr.map(i => `${i.quantity||1}x ${i.name}`).join(', ') : String(o.items);
          } catch(e) { itemsStr = String(o.items); }
          const stFormat = (o.status || 'pendiente').replace(/_/g,' ').toUpperCase();
          const nextStatus = getNextDeliveryStatus(o.status);
          domHtml += `<tr>
            <td style="font-size:12px;">${_escHtml(o.phone) || '—'}</td>
            <td style="color:#555;font-size:12px;max-width:200px;">${_escHtml(itemsStr)}</td>
            <td style="font-size:11px;color:#888;max-width:150px;">${_escHtml(o.address) || (o.type === 'recoger' ? '🏠 Recoger' : '—')}</td>
            <td style="font-size:11px;color:#0F6E56;font-weight:500;">${_escHtml(o.payment_method) || '—'}</td>
            <td><span class="badge" style="background:#E6F1FB;color:#185FA5;">${_escHtml(stFormat)}</span></td>
            <td style="font-weight:700;">${fmt(o.total)}</td>
            <td>${nextStatus ? `<button onclick="updateDeliveryStatus('${_escHtml(o.id)}','${_escHtml(nextStatus.status)}')" style="font-size:11px;padding:4px 8px;background:#1D9E75;color:#fff;border:none;border-radius:6px;cursor:pointer;">${_escHtml(nextStatus.label)}</button>` : '<span style="font-size:11px;color:#888;">—</span>'}</td>
          </tr>`;
        });
        domHtml += '</tbody></table>';
        domContainer.innerHTML = domHtml;
      }
    }

    // ── 5. Monitor salón ──
    let html = '';
    if (!active.length && !groupedBills.length) {
      container.innerHTML = '<div class="empty-state">No hay mesas con pedidos activos en este momento.</div>';
    } else {
      if (groupedBills.length > 0) {
        html += '<div style="font-size:13px;font-weight:bold;margin-bottom:10px;color:#6B21A8;">🧾 PENDIENTES DE FACTURA / PAGO</div>';
        html += '<table><thead><tr><th>Mesa</th><th>Platos Consolidados</th><th>Total</th><th>Acción</th></tr></thead><tbody>';
        groupedBills.forEach(b => {
          const itemsJoined = b.items.map(i => (i.quantity||1)+'x '+_escHtml(i.name)).join(', ');
          html += `<tr>
            <td style="font-weight:600;">${_escHtml(b.table_name||'—')}</td>
            <td style="color:#555;font-size:12px;max-width:300px;">${itemsJoined}</td>
            <td style="font-weight:700;color:#6B21A8;">${fmt(b.total)}</td>
            <td><button onclick="markTableInvoiced('${b.id}')" style="font-size:11px;padding:5px 12px;background:#7C3AED;color:#fff;border:none;border-radius:6px;cursor:pointer;">Cobrar</button></td>
          </tr>`;
        });
        html += '</tbody></table><br/>';
      }
      if (active.length > 0) {
        html += '<div style="font-size:13px;font-weight:bold;margin-bottom:10px;">🕒 ACTIVOS EN COCINA / SALÓN</div>';
        html += '<table><thead><tr><th>Mesa</th><th>Platos</th><th>Estado</th><th>Hora</th><th>Acción</th></tr></thead><tbody>';
        active.forEach(o => {
          let items = '';
          try {
            const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items;
            items = Array.isArray(arr) ? arr.map(i => `${i.quantity||1}× ${_escHtml(i.name||'')}`).join(', ') : _escHtml(String(o.items));
          } catch(e) { items = _escHtml(String(o.items||'—')); }
          const isoStr = (o.created_at||'').endsWith('Z') ? o.created_at : (o.created_at||'')+'Z';
          const hora = new Date(isoStr).toLocaleTimeString('es-CO',{hour:'2-digit',minute:'2-digit'});
          const st    = o.status || 'recibido';
          const color = STATUS_COLOR[st] || '#888';
          const bg    = STATUS_BG[st]    || '#f0f0f0';
          const label = STATUS_LABEL[st] || st;
          const nextSt = getNextTableStatus(st);
          html += `<tr>
            <td style="font-weight:600;">${_escHtml(o.table_name||'—')}</td>
            <td style="color:#555;font-size:12px;max-width:280px;">${items}</td>
            <td><span style="font-size:11px;padding:3px 8px;border-radius:10px;font-weight:500;background:${bg};color:${color};">${_escHtml(label)}</span></td>
            <td style="color:#888;">${hora}</td>
            <td>${nextSt ? `<button onclick="updateTableOrderStatus('${o.id}','${nextSt.status}')" style="font-size:11px;padding:4px 8px;background:#378ADD;color:#fff;border:none;border-radius:6px;cursor:pointer;">${_escHtml(nextSt.label)}</button>` : ''}</td>
          </tr>`;
        });
        html += '</tbody></table>';
      }
      container.innerHTML = html;
    }
  } catch(e) {
    console.error('loadTableOrdersSection:', e);
    if (container) container.innerHTML = '<div class="empty-state">Error de conexión.</div>';
  }
}

function getNextTableStatus(current) {
  const flow = {
    recibido:        { status: 'en_preparacion', label: '🍳 En preparación' },
    en_preparacion:  { status: 'listo',          label: '✅ Listo para servir' },
    listo:           { status: 'entregado',      label: '🍽️ Entregado' },
  };
  return flow[current] || null;
}

function getNextDeliveryStatus(current) {
  const flow = {
    pendiente_pago:  { status: 'confirmado',  label: '✅ Confirmar' },
    confirmado:      { status: 'en_camino',   label: '🛵 En camino' },
    en_camino:       { status: 'entregado',   label: '✅ Entregado' },
  };
  return flow[current] || null;
}

async function updateTableOrderStatus(orderId, newStatus) {
  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/table-orders/${orderId}/status`, {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    if (r.ok) loadTableOrdersSection();
  } catch(e) { console.error('updateTableOrderStatus:', e); }
}

async function updateDeliveryStatus(orderId, newStatus) {
  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/orders/${orderId}/status`, {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    if (r.ok) loadTableOrdersSection();
    else console.error('Error actualizando domicilio status');
  } catch(e) { console.error('updateDeliveryStatus:', e); }
}

async function markTableDelivered(orderId) {
  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/table-orders/${orderId}/status`, {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'entregado' })
    });
    if (r.ok) loadTableOrdersSection();
  } catch(e) { console.error('markTableDelivered:', e); }
}

async function markTableInvoiced(orderId) {
  const h = window._dashHeaders;
  const bill = window._billsData?.[orderId];
  const subtotal = bill?.total || 0;

  // Construir modal de cobro con toggle de cargo de servicio
  const existingModal = document.getElementById('_svc-modal');
  if (existingModal) existingModal.remove();

  const fmtLocal = n => '$' + Math.round(Number(n)).toLocaleString('es-CO');

  const overlay = document.createElement('div');
  overlay.id = '_svc-modal';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center;';

  const box = document.createElement('div');
  box.style.cssText = 'background:#fff;border-radius:14px;padding:1.5rem;width:340px;box-shadow:0 8px 40px rgba(0,0,0,.18);';

  box.innerHTML = `
    <div style="font-size:16px;font-weight:700;margin-bottom:1rem;">🧾 Cobrar Mesa</div>
    <div style="font-size:13px;color:#555;margin-bottom:1rem;">Subtotal: <strong>${fmtLocal(subtotal)}</strong></div>
    <label style="display:flex;align-items:center;gap:10px;font-size:13px;padding:10px;background:#f5f5f0;border-radius:8px;cursor:pointer;margin-bottom:1rem;">
      <input type="checkbox" id="_svc-toggle" style="width:16px;height:16px;cursor:pointer;">
      <span>Incluir Cargo de Servicio (10%)</span>
    </label>
    <div id="_svc-total-preview" style="font-size:14px;font-weight:700;color:#1D9E75;margin-bottom:1.25rem;">Total: ${fmtLocal(subtotal)}</div>
    <div style="display:flex;gap:8px;">
      <button id="_svc-cancel" style="flex:1;padding:9px;border:1px solid #ddd;border-radius:8px;background:none;cursor:pointer;font-size:13px;">Cancelar</button>
      <button id="_svc-confirm" style="flex:1;padding:9px;border:none;border-radius:8px;background:#1D9E75;color:#fff;cursor:pointer;font-size:13px;font-weight:600;">Confirmar Cobro</button>
    </div>
  `;

  overlay.appendChild(box);
  document.body.appendChild(overlay);

  const toggle = document.getElementById('_svc-toggle');
  const preview = document.getElementById('_svc-total-preview');

  toggle.addEventListener('change', () => {
    const newTotal = toggle.checked ? subtotal * 1.1 : subtotal;
    preview.textContent = `Total: ${fmtLocal(newTotal)}${toggle.checked ? ' (incl. 10% servicio)' : ''}`;
  });

  document.getElementById('_svc-cancel').addEventListener('click', () => overlay.remove());

  document.getElementById('_svc-confirm').addEventListener('click', async () => {
    const includeService = toggle.checked;
    overlay.remove();
    try {
      if (includeService && subtotal > 0) {
        const newTotal = Math.round(subtotal * 1.1);
        const allItems = bill?.items || [];
        await fetch(`/api/table-orders/${orderId}/adjust`, {
          method: 'PATCH',
          headers: { ...h, 'Content-Type': 'application/json' },
          body: JSON.stringify({ items: allItems, total: newTotal, service_charge: Math.round(subtotal * 0.1) })
        });
      }
      const r = await fetch(`/api/table-orders/${orderId}/status`, {
        method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'factura_entregada' })
      });
      if (r.ok) loadTableOrdersSection();
    } catch(e) { console.error('markTableInvoiced:', e); }
  });
}

const _origFetchOrders = window.fetchOrders;
const _origRefreshAll = window.refreshAll;
if (typeof _origRefreshAll === 'function') {
  window.refreshAll = async function() {
    await _origRefreshAll();
    if (document.visibilityState !== 'hidden') loadTableOrdersSection();
  };
}
document.addEventListener('DOMContentLoaded', () => {
  loadTableOrdersSection();
  setInterval(() => { if (document.visibilityState !== 'hidden') loadTableOrdersSection(); }, 15000);
});

// ── GESTIÓN DE STAFF OPERATIVO (ROSTER) ─────────────────────────────────


async function _loadStaffBranchesSelect() {
  const select = document.getElementById('invite-branch-id');
  if (!select) return;

  try {
    const r = await fetch('/api/team/branches', { headers: window._dashHeaders });
    if (r.ok) {
      const data = await r.json();
      const branches = data.branches || [];
      
      // Opción por defecto
      select.innerHTML = '<option value="">— Casa Matriz —</option>';
      
      branches.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b.id;
        opt.textContent = b.name;
        select.appendChild(opt);
      });
    }
  } catch (e) {
    console.error('Error _loadStaffBranchesSelect:', e);
  }
}

function openCreateStaffModal() {
  document.getElementById('staff-create-name').value = '';
  document.getElementById('staff-create-pin').value = '';
  // Resetear checkboxes (dejar solo mesero)
  document.querySelectorAll('#staff-create-roles input[type="checkbox"]').forEach(cb => {
    cb.checked = cb.value === 'mesero';
  });
  document.getElementById('modal-staff-create').style.display = 'flex';
}

function closeCreateStaffModal() {
  document.getElementById('modal-staff-create').style.display = 'none';
}

async function submitCreateStaff() {
  const h = window._dashHeaders;
  const name = document.getElementById('staff-create-name').value.trim();
  const pin = document.getElementById('staff-create-pin').value.trim();
  
  // Recoger los roles seleccionados
  const selectedRoles = [];
  document.querySelectorAll('#staff-create-roles input[type="checkbox"]:checked').forEach(cb => {
    selectedRoles.push(cb.value);
  });

  if (!name) { alert('Ingresa el nombre del empleado.'); return; }
  if (selectedRoles.length === 0) { alert('Selecciona al menos un rol.'); return; }
  if (pin.length < 4) { alert('El PIN debe tener al menos 4 dígitos.'); return; }

  try {
    // El backend espera el PIN en el campo "password" según tu pydantic model
    const payload = {
      name: name,
      roles: selectedRoles,
      role: selectedRoles[0], // fallback
      password: pin,
      phone: ""
    };

    const r = await fetch('/api/staff', {
      method: 'POST',
      headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (r.ok) {
      closeCreateStaffModal();
      loadStaff(); // Recargar el grid
    } else {
      const e = await r.json();
      alert('Error al crear empleado: ' + (e.detail || r.statusText));
    }
  } catch(e) {
    console.error(e);
    alert('Error de conexión.');
  }
}

async function deleteStaff(id, name) {
  if (!confirm(`¿Estás seguro de que deseas eliminar a ${name}?`)) return;
  
  const h = window._dashHeaders;
  try {
    const r = await fetch('/api/staff/' + id, {
      method: 'DELETE',
      headers: h
    });
    
    if (r.ok) {
      loadStaff();
    } else {
      const e = await r.json();
      alert('Error al eliminar: ' + (e.detail || r.statusText));
    }
  } catch(e) {
    alert('Error de conexión.');
  }
}

// ══════════════════════════════════════════════════════════════
// RESERVACIONES
// ══════════════════════════════════════════════════════════════

let _resData = [];

function loadReservationsSection() {
  const input = document.getElementById('res-date');
  if (!input) return;
  const today = new Date();
  const yyyy = today.getFullYear();
  const mm   = String(today.getMonth() + 1).padStart(2, '0');
  const dd   = String(today.getDate()).padStart(2, '0');
  input.value = `${yyyy}-${mm}-${dd}`;
  _resLoadDay();
}

function _resNavDay(delta) {
  const input = document.getElementById('res-date');
  if (!input || !input.value) return;
  const d = new Date(input.value + 'T00:00:00');
  d.setDate(d.getDate() + delta);
  const yyyy = d.getFullYear();
  const mm   = String(d.getMonth() + 1).padStart(2, '0');
  const dd   = String(d.getDate()).padStart(2, '0');
  input.value = `${yyyy}-${mm}-${dd}`;
  _resLoadDay();
}

async function _resLoadDay() {
  const input  = document.getElementById('res-date');
  const filter = document.getElementById('res-status-filter');
  const list   = document.getElementById('res-list');
  if (!input || !list) return;

  const date   = input.value;
  const status = filter ? filter.value : '';

  list.innerHTML = '<div class="empty-state" style="padding:2rem;text-align:center;color:#aaa;">Cargando...</div>';

  let url = `/api/reservations?date_from=${date}&date_to=${date}`;
  if (status) url += `&status=${encodeURIComponent(status)}`;

  try {
    const r = await fetch(url, { headers: window._dashHeaders });
    if (r.ok) {
      const data = await r.json();
      _resData = data.reservations || data || [];
    } else {
      _resData = [];
    }
  } catch (e) {
    console.error('_resLoadDay fetch:', e);
    _resData = [];
  }

  _resRender();

  // Fetch stats for the day
  try {
    const rs = await fetch(
      `/api/reservations/stats?period_start=${date}&period_end=${date}`,
      { headers: window._dashHeaders }
    );
    if (rs.ok) {
      const stats = await rs.json();
      _resRenderStats(stats);
    } else {
      _resRenderStats(null);
    }
  } catch (e) {
    console.error('_resLoadDay stats:', e);
    _resRenderStats(null);
  }
}

function _resStatusColor(status) {
  const map = {
    pending:   '#f59e0b',
    confirmed: '#22c55e',
    cancelled: '#ef4444',
    no_show:   '#f97316',
    completed: '#3b82f6',
  };
  return map[status] || '#9ca3af';
}

function _resStatusLabel(status) {
  const map = {
    pending:   'Pendiente',
    confirmed: 'Confirmada',
    cancelled: 'Cancelada',
    no_show:   'No-Show',
    completed: 'Completada',
  };
  return map[status] || status;
}

function _resRender() {
  const list = document.getElementById('res-list');
  if (!list) return;

  // Clear safely
  while (list.firstChild) list.removeChild(list.firstChild);

  if (!_resData.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.style.cssText = 'padding:2rem;text-align:center;color:#aaa;font-size:14px;';
    empty.textContent = 'No hay reservaciones para este día.';
    list.appendChild(empty);
    return;
  }

  _resData.forEach(res => {
    // Outer card
    const card = document.createElement('div');
    card.className = 'card';
    card.style.cssText = 'padding:16px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;';

    // Left info block
    const info = document.createElement('div');
    info.style.flex = '1';

    // Name + guests
    const nameLine = document.createElement('div');
    nameLine.style.marginBottom = '4px';
    const nameStrong = document.createElement('strong');
    nameStrong.textContent = res.name || res.customer_name || '—';
    nameLine.appendChild(nameStrong);
    const guestsSpan = document.createElement('span');
    guestsSpan.textContent = ` — ${res.guests || res.party_size || '?'} persona${(res.guests || res.party_size || 1) !== 1 ? 's' : ''}`;
    nameLine.appendChild(guestsSpan);
    info.appendChild(nameLine);

    // Time + phone
    const subLine = document.createElement('div');
    subLine.style.cssText = 'color:var(--text-secondary,#6b7280);font-size:13px;margin-bottom:6px;';
    const timeStr = res.time || res.reservation_time || res.scheduled_at || '';
    const phoneStr = res.phone || res.customer_phone || '';
    subLine.textContent = [timeStr, phoneStr].filter(Boolean).join(' · ');
    info.appendChild(subLine);

    // Status badge
    const badge = document.createElement('span');
    badge.style.cssText = `display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;background:${_resStatusColor(res.status)}22;color:${_resStatusColor(res.status)};`;
    badge.textContent = _resStatusLabel(res.status);
    info.appendChild(badge);

    // Table info
    if (res.table_id || res.table_number) {
      const tableSpan = document.createElement('span');
      tableSpan.style.cssText = 'font-size:13px;color:var(--text-secondary,#6b7280);margin-left:8px;';
      tableSpan.textContent = `· Mesa: ${res.table_number || res.table_id}`;
      info.appendChild(tableSpan);
    }

    // Notes
    if (res.notes) {
      const noteEl = document.createElement('div');
      noteEl.style.cssText = 'font-size:12px;color:var(--text-secondary,#6b7280);margin-top:6px;';
      noteEl.textContent = res.notes;
      info.appendChild(noteEl);
    }

    card.appendChild(info);

    // Right actions block
    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;align-items:center;';

    const id = res.id;
    const st = res.status;

    if (st === 'pending') {
      const btnConfirm = document.createElement('button');
      btnConfirm.className = 'btn-sm';
      btnConfirm.style.cssText = 'background:#22c55e;color:#fff;border-color:#22c55e;';
      btnConfirm.textContent = '✓ Confirmar';
      btnConfirm.onclick = () => _resAction(id, 'confirmed');
      actions.appendChild(btnConfirm);
    }

    if (st === 'pending' || st === 'confirmed') {
      const btnCancel = document.createElement('button');
      btnCancel.className = 'btn-sm';
      btnCancel.style.cssText = 'background:#ef4444;color:#fff;border-color:#ef4444;';
      btnCancel.textContent = '✗ Cancelar';
      btnCancel.onclick = () => _resAction(id, 'cancelled');
      actions.appendChild(btnCancel);
    }

    if (st === 'confirmed') {
      const btnNoShow = document.createElement('button');
      btnNoShow.className = 'btn-sm';
      btnNoShow.textContent = 'No-Show';
      btnNoShow.onclick = () => _resAction(id, 'no_show');
      actions.appendChild(btnNoShow);

      const btnDone = document.createElement('button');
      btnDone.className = 'btn-sm';
      btnDone.style.cssText = 'background:#3b82f6;color:#fff;border-color:#3b82f6;';
      btnDone.textContent = 'Completada';
      btnDone.onclick = () => _resAction(id, 'completed');
      actions.appendChild(btnDone);
    }

    card.appendChild(actions);
    list.appendChild(card);
  });
}

async function _resAction(id, newStatus) {
  try {
    const r = await fetch(`/api/reservations/${id}/status`, {
      method: 'PUT',
      headers: { ...window._dashHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert(err.detail || `Error al actualizar reservación (HTTP ${r.status})`);
      return;
    }
  } catch (e) {
    console.error('_resAction:', e);
    alert('Error de red al actualizar la reservación.');
    return;
  }
  _resLoadDay();
}

function _resRenderStats(stats) {
  const el = document.getElementById('res-stats');
  if (!el) return;

  // Clear safely
  while (el.firstChild) el.removeChild(el.firstChild);

  if (!stats) return;

  const total      = stats.total      ?? _resData.length;
  const confirmed  = stats.confirmed  ?? _resData.filter(r => r.status === 'confirmed').length;
  const cancelled  = stats.cancelled  ?? _resData.filter(r => r.status === 'cancelled').length;
  const noShow     = stats.no_show    ?? _resData.filter(r => r.status === 'no_show').length;
  const avgGuests  = stats.avg_guests ?? (
    _resData.length
      ? (_resData.reduce((s, r) => s + (r.guests || r.party_size || 0), 0) / _resData.length).toFixed(1)
      : '—'
  );
  const noShowRate = total > 0
    ? ((noShow / total) * 100).toFixed(0) + '%'
    : '0%';

  const statItems = [
    { label: 'Total',         value: total,       color: '' },
    { label: 'Confirmadas',   value: confirmed,   color: '#22c55e' },
    { label: 'Canceladas',    value: cancelled,   color: '#ef4444' },
    { label: 'No-Shows',      value: noShow,      color: '#f97316' },
    { label: 'Tasa No-Show',  value: noShowRate,  color: '#f97316' },
    { label: 'Prom. personas', value: avgGuests,  color: '' },
  ];

  statItems.forEach(item => {
    const card = document.createElement('div');
    card.style.cssText = 'background:var(--card-bg,#fff);border:1px solid var(--border,#e5e7eb);border-radius:10px;padding:8px 14px;min-width:90px;text-align:center;';

    const val = document.createElement('div');
    val.style.cssText = `font-size:18px;font-weight:700;${item.color ? 'color:' + item.color + ';' : ''}`;
    val.textContent = String(item.value);

    const lbl = document.createElement('div');
    lbl.style.cssText = 'font-size:11px;color:var(--text-secondary,#6b7280);margin-top:2px;';
    lbl.textContent = item.label;

    card.appendChild(val);
    card.appendChild(lbl);
    el.appendChild(card);
  });
}