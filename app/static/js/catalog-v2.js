/* ═══════════════════════════════════════════════════════════════════
   Catalog v2 — Main Logic
   Vanilla JS. Depends on mesio-utils.js loaded before this script.
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

/* ── Constants ── */
const CAT_ICONS = {
  'Entradas': '🥗', 'Pastas': '🍝', 'Pizzas': '🍕', 'Postres': '🍮',
  'Bebidas': '🥤', 'Desayunos': '🍳', 'Carnes': '🥩', 'Hamburguesas': '🍔',
  'Tacos': '🌮', 'Sushi': '🍣', 'Ensaladas': '🥬', 'Sopas': '🍲',
  'Mariscos': '🦞', 'Pollo': '🍗', 'Vegetariano': '🥦', 'default': '🍽️'
};

const ZERO_DECIMAL_CURRENCIES = ['COP','CLP','JPY','KRW','VND','PYG','ISK'];

const DIETARY_META = {
  vegan: {
    label: 'Vegano',
    svg: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="M7 13C7 13 2 9.5 2 5.5C2 3 4 1 7 1C10 1 12 3 12 5.5C12 9.5 7 13 7 13Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M7 1V13" stroke="currentColor" stroke-width="1" stroke-dasharray="2 1.5"/></svg>`
  },
  gluten_free: {
    label: 'Sin gluten',
    svg: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><circle cx="7" cy="7" r="5.5" stroke="currentColor" stroke-width="1.4"/><path d="M3.5 10.5l7-7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`
  },
  spicy: {
    label: 'Picante',
    svg: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="M7 13C4.5 13 3 11 3 8.5C3 7 3.5 6 5 5C4.5 7 5.5 8 7 8C5.5 6 6 4 8 2C8 4 9 5 9 7C10.5 6 10 4.5 10 4.5C11.5 5.5 11 7.5 11 8.5C11 11 9.5 13 7 13Z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>`
  }
};

/* ── Helpers ── */
function fmtPrice(n, locale, currency) {
  try {
    return new Intl.NumberFormat(locale || 'es-CO', {
      style: 'currency',
      currency: currency || 'COP',
      minimumFractionDigits: ZERO_DECIMAL_CURRENCIES.includes(currency) ? 0 : 2,
      maximumFractionDigits: ZERO_DECIMAL_CURRENCIES.includes(currency) ? 0 : 2,
    }).format(Number(n) || 0);
  } catch {
    return '$' + (Number(n) || 0).toLocaleString();
  }
}

function dishGradient(name) {
  let h = 0;
  const s = String(name || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return `hsl(${h}, 45%, 65%)`;
}

function slugify(str) {
  return String(str).toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '');
}

function dishId(dish, cat) {
  return slugify((dish.name || '') + '-' + (cat || ''));
}

function trackEvent(dishName, eventType, botNumber) {
  try {
    if (navigator.sendBeacon) {
      const payload = JSON.stringify({ dish_name: dishName, event_type: eventType, bot_number: botNumber });
      navigator.sendBeacon('/api/public/menu/track', new Blob([payload], { type: 'application/json' }));
    }
  } catch { /* analytics non-critical */ }
}

function getAvailableTags(menu) {
  const tags = new Set();
  for (const dishes of Object.values(menu)) {
    for (const d of dishes) {
      for (const t of (d.tags || [])) {
        if (DIETARY_META[t]) tags.add(t);
      }
    }
  }
  return [...tags];
}

function getFilteredMenu(menu, availability, search, activeFilters) {
  const q = search.toLowerCase().trim();
  const result = {};
  for (const [cat, dishes] of Object.entries(menu)) {
    const filtered = dishes.filter(d => {
      if (d.active === false) return false;
      if (q && !d.name.toLowerCase().includes(q) && !(d.description || '').toLowerCase().includes(q)) return false;
      if (activeFilters.length > 0 && !activeFilters.every(f => (d.tags || []).includes(f))) return false;
      return true;
    });
    if (filtered.length > 0) result[cat] = filtered;
  }
  return result;
}

function getFeaturedDishes(menu) {
  const result = [];
  for (const dishes of Object.values(menu)) {
    for (const d of dishes) {
      if (d.featured && d.active !== false) result.push(d);
      if (result.length >= 5) return result;
    }
  }
  return result;
}

function isRestaurantOpen(availability) {
  // If backend doesn't provide explicit open/close, assume open
  if (typeof availability === 'object' && availability !== null && 'is_open' in availability) {
    return availability.is_open;
  }
  return true;
}

/* ── SVG icons ── */
const SVG_PLUS = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 3v10M3 8h10" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>`;
const SVG_CART = `<svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true"><path d="M1.5 1.5h2l1.5 7.5h9l1.5-6H5" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="7.5" cy="14.5" r="1" fill="white"/><circle cx="12.5" cy="14.5" r="1" fill="white"/></svg>`;
const SVG_CLOSE = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="M2 2l10 10M12 2L2 12" stroke="white" stroke-width="1.8" stroke-linecap="round"/></svg>`;
const SVG_WA = `<svg width="22" height="22" viewBox="0 0 22 22" fill="white" aria-hidden="true"><path d="M11 1C5.477 1 1 5.477 1 11c0 1.89.52 3.66 1.428 5.18L1 21l4.95-1.428A9.956 9.956 0 0011 21c5.523 0 10-4.477 10-10S16.523 1 11 1zm0 18.182a8.16 8.16 0 01-4.16-1.14l-.297-.177-3.09.891.906-3.012-.194-.31A8.182 8.182 0 1111 19.182zm4.5-6.045c-.246-.123-1.455-.718-1.68-.8-.225-.082-.39-.123-.555.123-.164.246-.636.8-.78.964-.143.164-.287.185-.533.062-.246-.123-1.04-.383-1.98-1.22-.73-.654-1.222-1.46-1.367-1.706-.144-.246-.016-.379.108-.502.11-.11.246-.287.37-.43.122-.144.163-.246.245-.41.083-.164.042-.308-.02-.43-.062-.123-.555-1.337-.76-1.83-.2-.48-.404-.415-.555-.423l-.472-.008a.908.908 0 00-.657.308c-.226.246-.86.84-.86 2.049 0 1.21.88 2.378 1.003 2.542.122.164 1.733 2.648 4.2 3.714.587.254 1.045.405 1.402.518.589.188 1.126.161 1.549.098.473-.07 1.455-.595 1.66-1.17.205-.574.205-1.066.144-1.17-.062-.103-.226-.164-.472-.287z"/></svg>`;
const SVG_SEARCH = `<svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden="true"><circle cx="6.5" cy="6.5" r="4.5" stroke="#9CA3AF" stroke-width="1.5"/><path d="M10 10l3 3" stroke="#9CA3AF" stroke-width="1.5" stroke-linecap="round"/></svg>`;
const SVG_ARROW_L = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const SVG_ARROW_R = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const SVG_ALLERGEN = `<svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true"><path d="M5.5 1L10 9.5H1L5.5 1Z" stroke="#D97706" stroke-width="1.2" stroke-linejoin="round"/><path d="M5.5 4.5v2" stroke="#D97706" stroke-width="1.2" stroke-linecap="round"/><circle cx="5.5" cy="7.8" r="0.6" fill="#D97706"/></svg>`;

const BADGE_SVG = {
  chef_pick: `<svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M1 8h8M2 8V5.5L5 3l3 2.5V8" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><circle cx="2" cy="5" r="0.8" fill="currentColor"/><circle cx="5" cy="2.5" r="0.8" fill="currentColor"/><circle cx="8" cy="5" r="0.8" fill="currentColor"/></svg>`,
  new: `<svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M5 1v2M5 7v2M1 5h2M7 5h2M2.5 2.5l1.5 1.5M6 6l1.5 1.5M2.5 7.5L4 6M6 4l1.5-1.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  popular: `<svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M5 9C3 9 2 7.5 2 6C2 4.5 3 4 4 3C3.5 5 4.5 5.5 5 5.5C4 4 4.5 2.5 6.5 1C6.5 3 7 3.5 7 5C8 4 7.5 3 7.5 3C9 4 8.5 6 8.5 6.5C8.5 8 7 9 5 9Z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/></svg>`
};

/* ── Cart persistence ── */
function cartKey(botNumber) {
  // Returns null when botNumber is absent — callers must guard against null
  return botNumber ? 'mesio_cart_' + botNumber : null;
}

function loadCart(botNumber) {
  const key = cartKey(botNumber);
  if (!key) return {};
  try {
    return JSON.parse(localStorage.getItem(key) || '{}');
  } catch { return {}; }
}

function saveCart(botNumber, cart) {
  const key = cartKey(botNumber);
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify(cart));
  } catch (e) { console.warn('Cart persist failed (storage quota?):', e); }
}

/* ── DOM builders ── */
function buildSkeletons() {
  const grid = document.createElement('div');
  grid.className = 'skeleton-grid';
  const card = `
      <div class="skeleton-card">
        <div class="m-skeleton skeleton-img"></div>
        <div class="m-skeleton skeleton-text-1"></div>
        <div class="m-skeleton skeleton-text-2"></div>
        <div class="skeleton-row">
          <div class="m-skeleton skeleton-price"></div>
          <div class="m-skeleton skeleton-btn"></div>
        </div>
      </div>`;
  grid.innerHTML = card.repeat(6);
  return grid;
}

function buildDishImage(dish) {
  const wrap = document.createElement('div');
  wrap.className = 'dish-img-wrap';

  if (dish.image_url) {
    const img = document.createElement('img');
    img.alt = dish.name; // safe — no innerHTML
    img.loading = 'lazy';
    img.decoding = 'async';
    img.src = dish.image_url;
    // Remove skeleton on load
    img.onload = () => img.classList.remove('m-skeleton');
    img.onerror = () => {
      img.remove();
      const fb = buildFallback(dish.name, false);
      wrap.insertBefore(fb, wrap.firstChild);
    };
    wrap.appendChild(img);
  } else {
    const fb = buildFallback(dish.name, false);
    wrap.appendChild(fb);
  }
  return wrap;
}

function buildFallback(name, large) {
  const fb = document.createElement('div');
  fb.className = large ? 'modal-hero-fallback' : 'dish-img-fallback';
  fb.setAttribute('aria-label', name);
  fb.style.background = dishGradient(name);
  const initial = document.createElement('span');
  initial.className = 'fallback-initial';
  initial.textContent = (name || '?').charAt(0);
  fb.appendChild(initial);
  return fb;
}

function buildBadge(dish, available) {
  const frag = document.createDocumentFragment();

  // Top-left badge: chef_pick > new > popular (max 1)
  const badges = dish.badges || [];
  let topBadge = null;
  if (badges.includes('chef_pick')) topBadge = { key: 'chef_pick', label: 'Chef Pick', cls: 'dish-badge--chef' };
  else if (badges.includes('new')) topBadge = { key: 'new', label: 'Nuevo', cls: 'dish-badge--new' };
  else if (badges.includes('popular')) topBadge = { key: 'popular', label: 'Popular', cls: 'dish-badge--popular' };

  if (topBadge) {
    const b = document.createElement('span');
    b.className = `dish-badge dish-badge--top-left ${topBadge.cls}`;
    b.innerHTML = (BADGE_SVG[topBadge.key] || '') + _escHtml(topBadge.label);
    frag.appendChild(b);
  }

  // Bottom-left: Agotado
  if (!available) {
    const b = document.createElement('span');
    b.className = 'dish-badge dish-badge--bottom-left dish-badge--sold-out';
    b.textContent = 'Agotado';
    frag.appendChild(b);
  }
  return frag;
}

function _renderDishCtrl(ctrl, dish, cat, qty, available, callbacks) {
  ctrl.innerHTML = '';
  if (qty > 0) {
    // Stepper: [−] [count] [+]
    ctrl.classList.add('dish-ctrl--active');
    const minusBtn = document.createElement('button');
    minusBtn.className = 'dish-ctrl-btn dish-ctrl-minus';
    minusBtn.setAttribute('aria-label', 'Quitar ' + dish.name);
    minusBtn.innerHTML = '−';
    minusBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      callbacks.onRemove(dish, cat);
    });
    const countEl = document.createElement('span');
    countEl.className = 'dish-ctrl-count';
    countEl.setAttribute('aria-live', 'polite');
    countEl.setAttribute('aria-atomic', 'true');
    countEl.textContent = qty;
    const plusBtn = document.createElement('button');
    plusBtn.className = 'dish-ctrl-btn dish-ctrl-plus';
    plusBtn.setAttribute('aria-label', 'Agregar ' + dish.name);
    plusBtn.innerHTML = '+';
    if (!available) { plusBtn.disabled = true; }
    plusBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!available) return;
      plusBtn.classList.add('adding');
      setTimeout(() => plusBtn.classList.remove('adding'), 350);
      callbacks.onAdd(dish, cat);
    });
    ctrl.appendChild(minusBtn);
    ctrl.appendChild(countEl);
    ctrl.appendChild(plusBtn);
  } else {
    // Single + button
    ctrl.classList.remove('dish-ctrl--active');
    const addBtn = document.createElement('button');
    addBtn.className = 'dish-add-btn';
    addBtn.setAttribute('aria-label', 'Agregar ' + dish.name);
    if (!available) { addBtn.disabled = true; addBtn.setAttribute('aria-disabled', 'true'); }
    addBtn.innerHTML = SVG_PLUS;
    addBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!available) return;
      addBtn.classList.add('adding');
      setTimeout(() => addBtn.classList.remove('adding'), 350);
      callbacks.onAdd(dish, cat);
    });
    ctrl.appendChild(addBtn);
  }
}

function buildDishCard(dish, cat, state, callbacks) {
  const available = state.availability[dish.name] !== false;
  const id = dishId(dish, cat);
  const qty = state.cart[id] || 0;

  const card = document.createElement('article');
  card.className = 'dish-card' + (available ? '' : ' dish-unavailable');
  card.setAttribute('tabindex', '0');
  card.setAttribute('role', 'article');
  card.setAttribute('aria-label', dish.name);
  card.dataset.dishId = id;

  // Image section
  const imgWrap = buildDishImage(dish);
  imgWrap.appendChild(buildBadge(dish, available));
  card.appendChild(imgWrap);

  // Info section
  const info = document.createElement('div');
  info.className = 'dish-info';

  const nameEl = document.createElement('h3');
  nameEl.className = 'dish-name';
  nameEl.textContent = dish.name;
  info.appendChild(nameEl);

  if (dish.description) {
    const descEl = document.createElement('p');
    descEl.className = 'dish-desc';
    descEl.textContent = dish.description;
    info.appendChild(descEl);
  }

  const footer = document.createElement('div');
  footer.className = 'dish-footer';

  const priceEl = document.createElement('span');
  priceEl.className = 'dish-price';
  priceEl.textContent = fmtPrice(dish.price, state.locale, state.currency);
  footer.appendChild(priceEl);

  // Cart control: single + when qty=0, stepper when qty>0
  const ctrl = document.createElement('div');
  ctrl.className = 'dish-ctrl';
  _renderDishCtrl(ctrl, dish, cat, qty, available, callbacks);
  footer.appendChild(ctrl);
  info.appendChild(footer);
  card.appendChild(info);

  // Store meta for rerenderCards
  _cardMeta.set(card, { dish, cat, available, callbacks });

  // Card click → open modal
  card.addEventListener('click', () => callbacks.onOpen(dish, card));
  card.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); callbacks.onOpen(dish, card); }
  });

  // Analytics view observer (once, threshold 0.5)
  const viewObserver = new IntersectionObserver((entries, obs) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        trackEvent(dish.name, 'view', state.botNumber);
        obs.disconnect();
        // Remove from registry so we don't double-disconnect on re-render
        if (callbacks.viewObservers) {
          const idx = callbacks.viewObservers.indexOf(obs);
          if (idx !== -1) callbacks.viewObservers.splice(idx, 1);
        }
      }
    });
  }, { threshold: 0.5 });
  viewObserver.observe(card);
  if (callbacks.viewObservers) callbacks.viewObservers.push(viewObserver);

  return card;
}

/* ── Modal ── */
class DishModal {
  constructor(container, state, callbacks) {
    this.container = container;
    this.state = state;
    this.callbacks = callbacks;
    this._triggerEl = null;
    this._touchStartY = 0;
    this._keyHandler = this._handleKey.bind(this);
    this._render();
  }

  _render() {
    const overlay = document.createElement('div');
    overlay.className = 'dish-modal-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'modal-dish-name');
    overlay.id = 'dish-modal';

    overlay.innerHTML = `
      <div class="dish-modal-box" id="modal-box">
        <span class="modal-drag-handle" aria-hidden="true"></span>
        <div class="modal-hero-wrap" id="modal-hero"></div>
        <button class="modal-close-btn" aria-label="Cerrar" id="modal-close">${SVG_CLOSE}</button>
        <div class="modal-content">
          <h2 class="modal-dish-name" id="modal-dish-name"></h2>
          <p class="modal-dish-price" id="modal-dish-price"></p>
          <p class="modal-dish-desc" id="modal-dish-desc"></p>
          <div class="allergens-section" id="modal-allergens" style="display:none">
            <p class="allergens-title">Alérgenos</p>
            <div class="allergen-chips" id="modal-allergen-chips"></div>
          </div>
          <div class="qty-selector" id="modal-qty-selector">
            <span class="qty-label">Cantidad</span>
            <button class="qty-btn" id="modal-qty-minus" aria-label="Reducir cantidad">−</button>
            <span class="qty-count" id="modal-qty-count" aria-live="polite" aria-atomic="true">1</span>
            <button class="qty-btn" id="modal-qty-plus" aria-label="Aumentar cantidad">+</button>
          </div>
          <button class="modal-add-btn" id="modal-add-btn">${SVG_CART} Agregar al carrito</button>
        </div>
      </div>`;

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) this.close();
    });
    overlay.querySelector('#modal-close').addEventListener('click', () => this.close());

    // Qty controls
    this._qty = 1;
    overlay.querySelector('#modal-qty-minus').addEventListener('click', () => this._setQty(this._qty - 1));
    overlay.querySelector('#modal-qty-plus').addEventListener('click', () => this._setQty(this._qty + 1));
    overlay.querySelector('#modal-add-btn').addEventListener('click', () => this._addToCart());

    // Swipe-down
    const box = overlay.querySelector('#modal-box');
    box.addEventListener('touchstart', (e) => { this._touchStartY = e.touches[0].clientY; }, { passive: true });
    box.addEventListener('touchmove', (e) => {
      const delta = e.touches[0].clientY - this._touchStartY;
      if (delta > 0) box.style.transform = `translateY(${delta}px)`;
    }, { passive: true });
    box.addEventListener('touchend', (e) => {
      const delta = e.changedTouches[0].clientY - this._touchStartY;
      if (delta > 80) {
        box.style.transform = '';
        this.close();
      } else {
        box.style.transform = '';
      }
    });

    this._el = overlay;
    this.container.appendChild(overlay);
  }

  _getFocusables() {
    return [...this._el.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])'
    )];
  }

  _handleKey(e) {
    if (e.key === 'Escape') { this.close(); return; }
    if (e.key === 'Tab') {
      const focusables = this._getFocusables();
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }
  }

  _setQty(n) {
    this._qty = Math.max(1, n);
    this._el.querySelector('#modal-qty-count').textContent = this._qty;
    this._el.querySelector('#modal-qty-minus').disabled = this._qty <= 1;
  }

  _addToCart() {
    if (!this._dish) return;
    this.callbacks.onAdd(this._dish, this._cat, this._qty);
    this.close();
  }

  open(dish, cat, triggerEl, state) {
    this._dish = dish;
    this._cat = cat;
    this._triggerEl = triggerEl || null;
    this.state = state;
    this._setQty(1);

    const available = state.availability[dish.name] !== false;

    // Hero
    const heroWrap = this._el.querySelector('#modal-hero');
    heroWrap.innerHTML = '';
    if (dish.image_url) {
      const img = document.createElement('img');
      img.alt = dish.name;
      img.src = dish.image_url;
      img.loading = 'eager';
      heroWrap.appendChild(img);
    } else {
      heroWrap.appendChild(buildFallback(dish.name, true));
    }

    // Text content — textContent only, no innerHTML for user data
    this._el.querySelector('#modal-dish-name').textContent = dish.name;
    this._el.querySelector('#modal-dish-price').textContent = fmtPrice(dish.price, state.locale, state.currency);
    const descEl = this._el.querySelector('#modal-dish-desc');
    descEl.textContent = dish.description || '';
    descEl.style.display = dish.description ? '' : 'none';

    // Allergens
    const allergensSection = this._el.querySelector('#modal-allergens');
    const allergenChips = this._el.querySelector('#modal-allergen-chips');
    const allergens = dish.allergens || [];
    if (allergens.length > 0) {
      allergenChips.innerHTML = '';
      allergens.forEach(a => {
        const chip = document.createElement('span');
        chip.className = 'allergen-chip';
        chip.innerHTML = SVG_ALLERGEN;
        const txt = document.createTextNode(' ' + a);
        chip.appendChild(txt);
        allergenChips.appendChild(chip);
      });
      allergensSection.style.display = '';
    } else {
      allergensSection.style.display = 'none';
    }

    // Add button
    const addBtn = this._el.querySelector('#modal-add-btn');
    if (available) {
      addBtn.disabled = false;
      addBtn.innerHTML = SVG_CART + ' Agregar al carrito';
    } else {
      addBtn.disabled = true;
      addBtn.textContent = 'No disponible';
    }
    this._el.querySelector('#modal-qty-selector').style.display = available ? '' : 'none';

    // Show
    this._el.classList.add('open');
    document.addEventListener('keydown', this._keyHandler);
    requestAnimationFrame(() => {
      const focusables = this._getFocusables();
      if (focusables.length) focusables[0].focus();
    });

    trackEvent(dish.name, 'modal_open', state.botNumber);
  }

  close() {
    this._el.classList.remove('open');
    document.removeEventListener('keydown', this._keyHandler);
    if (this._triggerEl) {
      try { this._triggerEl.focus(); } catch {}
    }
    this._dish = null;
    this._triggerEl = null;
  }
}

/* ── HeroCarousel ── */
class HeroCarousel {
  constructor(el, dishes, state, onOpen) {
    this.el = el;
    this.dishes = dishes;
    this.state = state;
    this.onOpen = onOpen;
    this._idx = 0;
    this._timer = null;
    this._paused = false;
    this._render();
    this._startAuto();
  }

  _render() {
    const track = document.createElement('div');
    track.className = 'hero-track';
    track.id = 'hero-track';

    this.dishes.forEach((dish, i) => {
      const slide = document.createElement('div');
      slide.className = 'hero-slide';
      slide.setAttribute('aria-label', dish.name);
      slide.setAttribute('tabindex', '0');
      slide.setAttribute('role', 'button');

      if (dish.image_url) {
        const img = document.createElement('img');
        img.alt = dish.name;
        img.src = dish.image_url;
        img.loading = i === 0 ? 'eager' : 'lazy';
        img.decoding = 'async';
        slide.appendChild(img);
      } else {
        const fb = document.createElement('div');
        fb.className = 'hero-slide-fallback';
        fb.style.background = dishGradient(dish.name);
        const initial = document.createElement('span');
        initial.className = 'fallback-initial';
        initial.setAttribute('aria-hidden', 'true');
        initial.textContent = (dish.name || '?').charAt(0);
        fb.appendChild(initial);
        slide.appendChild(fb);
      }

      // Text overlay
      const textEl = document.createElement('div');
      textEl.className = 'hero-text';
      const nameEl = document.createElement('p');
      nameEl.className = 'hero-dish-name';
      nameEl.textContent = dish.name;
      const priceEl = document.createElement('p');
      priceEl.className = 'hero-dish-price';
      priceEl.textContent = fmtPrice(dish.price, this.state.locale, this.state.currency);
      textEl.appendChild(nameEl);
      textEl.appendChild(priceEl);
      if (dish.description) {
        const descEl = document.createElement('p');
        descEl.className = 'hero-dish-desc';
        descEl.textContent = dish.description;
        textEl.appendChild(descEl);
      }
      slide.appendChild(textEl);

      slide.addEventListener('click', () => this.onOpen(dish, slide));
      slide.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.onOpen(dish, slide); }
      });
      track.appendChild(slide);
    });
    this.el.appendChild(track);
    this._track = track;

    // Dots
    const dots = document.createElement('div');
    dots.className = 'hero-dots';
    dots.setAttribute('aria-hidden', 'true');
    this.dishes.forEach((_, i) => {
      const dot = document.createElement('span');
      dot.className = 'hero-dot' + (i === 0 ? ' active' : '');
      dots.appendChild(dot);
    });
    this.el.appendChild(dots);
    this._dots = dots;

    // Prev/Next buttons (desktop)
    if (this.dishes.length > 1) {
      const prev = document.createElement('button');
      prev.className = 'hero-btn hero-btn--prev';
      prev.setAttribute('aria-label', 'Anterior');
      prev.innerHTML = SVG_ARROW_L;
      prev.addEventListener('click', () => this.go(this._idx - 1));
      this.el.appendChild(prev);

      const next = document.createElement('button');
      next.className = 'hero-btn hero-btn--next';
      next.setAttribute('aria-label', 'Siguiente');
      next.innerHTML = SVG_ARROW_R;
      next.addEventListener('click', () => this.go(this._idx + 1));
      this.el.appendChild(next);
    }

    // Pause on hover/touch
    this.el.addEventListener('mouseenter', () => { this._paused = true; });
    this.el.addEventListener('mouseleave', () => { this._paused = false; });
    this.el.addEventListener('touchstart', () => { this._paused = true; }, { passive: true });
    this.el.addEventListener('touchend', () => { setTimeout(() => { this._paused = false; }, 2000); }, { passive: true });
  }

  go(idx) {
    const len = this.dishes.length;
    this._idx = ((idx % len) + len) % len;
    this._track.style.transform = `translateX(-${this._idx * 100}%)`;
    const dotEls = this._dots.querySelectorAll('.hero-dot');
    dotEls.forEach((d, i) => d.classList.toggle('active', i === this._idx));
  }

  _startAuto() {
    if (this.dishes.length <= 1) return;
    this._timer = setInterval(() => {
      if (!this._paused) this.go(this._idx + 1);
    }, 4000);
  }

  destroy() {
    if (this._timer) clearInterval(this._timer);
  }
}

/* ── Main CatalogPage ── */
// Maps card element → { dish, cat, available, callbacks } for rerenderCards
const _cardMeta = new WeakMap();

function initCatalog() {
  const root = document.getElementById('catalog-root');
  if (!root) return;

  // State
  let state = {
    menu: {},
    availability: {},
    locale: 'es-CO',
    currency: 'COP',
    restaurantName: '',
    tableName: null,
    tableId: null,
    botNumber: '',
    waUrl: '',
    search: '',
    activeCategory: '',
    activeFilters: [],
    cart: {},
    openDish: null,
    loading: true,
    error: null,
  };

  // Detect endpoint
  const path = window.location.pathname;
  const parts = path.split('/').filter(Boolean);
  const lastPart = parts[parts.length - 1];
  // Bot numbers are 10+ digits only (e.g. 573108187460). Anything else is a table_id.
  const isTableContext = !/^\d{10,}$/.test(lastPart);

  let apiUrl;
  const paramId = lastPart || '';

  // Support both /menu/TABLE_ID and /menu/BOT_NUMBER
  // Try menu-context first if it looks like a numeric table id, otherwise use bot endpoint
  // We'll attempt fetch and fall back
  if (isTableContext) {
    apiUrl = `/api/public/menu-context/${paramId}`;
  } else {
    apiUrl = `/api/public/menu/${paramId}`;
  }

  // Also check query params
  const qp = new URLSearchParams(window.location.search);
  const qTable = qp.get('table') || qp.get('mesa');
  const qBot = qp.get('bot') || qp.get('numero');
  if (qTable) apiUrl = `/api/public/menu-context/${qTable}`;
  else if (qBot) apiUrl = `/api/public/menu/${qBot}`;

  // ── DOM references ──
  const headerEl = document.getElementById('catalog-header');
  const heroEl = document.getElementById('catalog-hero');
  const navEl = document.getElementById('catalog-nav');
  const mainEl = document.getElementById('catalog-main');
  const footerEl = document.getElementById('catalog-footer');
  const announcer = document.getElementById('filter-announcer');
  const errorBannerEl = document.getElementById('error-banner');

  let modal = null;
  let carousel = null;
  let scrollObserver = null;
  let viewObservers = [];

  // ── Cart helpers ──
  function getCart() { return state.cart; }
  function updateCart(dish, cat, deltaOrQty, absolute) {
    const id = dishId(dish, cat);
    const current = state.cart[id] || 0;
    let next;
    if (absolute) {
      next = Math.max(0, deltaOrQty);
    } else {
      next = Math.max(0, current + deltaOrQty);
    }
    if (next === 0) {
      delete state.cart[id];
    } else {
      state.cart[id] = next;
    }
    saveCart(state.botNumber, state.cart);
    renderCartFooter();
    rerenderCards();
  }

  function cartItemCount() {
    return Object.values(state.cart).reduce((a, b) => a + b, 0);
  }

  function cartTotal() {
    let total = 0;
    for (const [cat, dishes] of Object.entries(state.menu)) {
      if (!Array.isArray(dishes)) continue;
      for (const dish of dishes) {
        const id = dishId(dish, cat);
        total += (state.cart[id] || 0) * (Number(dish.price) || 0);
      }
    }
    return total;
  }

  function buildWaMessage() {
    const items = [];
    for (const [cat, dishes] of Object.entries(state.menu)) {
      if (!Array.isArray(dishes)) continue;
      for (const dish of dishes) {
        const id = dishId(dish, cat);
        const qty = state.cart[id] || 0;
        if (qty > 0) items.push(`- ${qty}× ${dish.name}`);
      }
    }
    const itemsText = items.join('\n');
    if (state.tableId) {
      // Include table name so detect_table_context can always identify the table,
      // even if the user skipped the QR greeting and went straight to ordering.
      const tablePrefix = state.tableName ? `Estoy en ${state.tableName}\n` : '';
      return `${tablePrefix}Quiero pedir:\n${itemsText}`;
    }
    return `Hola, me gustaría pedir:\n${itemsText}`;
  }

  // ── Render functions ──
  function renderHeader(data) {
    headerEl.innerHTML = '';
    const h1 = document.createElement('h1');
    h1.textContent = data.restaurantName || 'Menú';
    headerEl.appendChild(h1);

    const sub = document.createElement('p');
    sub.className = 'header-subtitle';
    sub.textContent = data.tableName ? `Mesa: ${data.tableName}` : 'Haz tu pedido';
    headerEl.appendChild(sub);

    const isOpen = isRestaurantOpen(data.availability);
    const badge = document.createElement('span');
    badge.className = `badge-status badge-status--${isOpen ? 'open' : 'closed'}`;
    const dot = document.createElement('span');
    dot.className = 'dot';
    dot.setAttribute('aria-hidden', 'true');
    badge.appendChild(dot);
    const label = document.createTextNode(isOpen ? 'Abierto' : 'Cerrado');
    badge.appendChild(label);
    headerEl.appendChild(badge);
  }

  function renderHero(menu, search) {
    if (search) {
      heroEl.style.display = 'none';
      return;
    }
    const featured = getFeaturedDishes(menu);
    if (featured.length === 0) {
      heroEl.style.display = 'none';
      return;
    }
    heroEl.style.display = '';
    heroEl.innerHTML = '';
    if (carousel) { carousel.destroy(); carousel = null; }
    carousel = new HeroCarousel(heroEl, featured, state, (dish, el) => {
      openModal(dish, '', el);
    });
  }

  function renderNav(menu, search, activeCategory, activeFilters) {
    const cats = Object.keys(menu);
    const availableTags = getAvailableTags(state.menu);

    navEl.innerHTML = '';

    // Search
    const searchWrap = document.createElement('div');
    searchWrap.className = 'search-wrap';
    searchWrap.innerHTML = `
      <span class="search-icon-wrap">${SVG_SEARCH}</span>
      <input class="search-input" type="search" id="search-input"
             placeholder="Buscar plato o ingrediente..."
             aria-label="Buscar platos"
             value="${_escHtml(search)}">
      <button class="search-clear${search ? ' visible' : ''}" id="search-clear"
              aria-label="Limpiar búsqueda" type="button">×</button>`;
    navEl.appendChild(searchWrap);

    const searchInput = searchWrap.querySelector('#search-input');
    const searchClear = searchWrap.querySelector('#search-clear');
    let debounceTimer = null;
    searchInput.addEventListener('input', () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        const val = searchInput.value;
        searchClear.classList.toggle('visible', val.length > 0);
        setState({ search: val });
      }, 200);
    });
    searchClear.addEventListener('click', () => {
      searchInput.value = '';
      searchClear.classList.remove('visible');
      setState({ search: '' });
      searchInput.focus();
    });

    // Category chips
    if (cats.length > 0) {
      const chipsWrap = document.createElement('div');
      chipsWrap.className = 'category-chips';
      chipsWrap.setAttribute('role', 'tablist');
      chipsWrap.setAttribute('aria-label', 'Categorías del menú');

      cats.forEach(cat => {
        const catId = 'cat-' + slugify(cat);
        const icon = CAT_ICONS[cat] || CAT_ICONS['default'];
        const chip = document.createElement('button');
        chip.className = 'cat-chip' + (activeCategory === catId ? ' active' : '');
        chip.setAttribute('role', 'tab');
        chip.setAttribute('aria-selected', activeCategory === catId ? 'true' : 'false');
        chip.setAttribute('data-target', catId);
        chip.setAttribute('type', 'button');
        chip.setAttribute('aria-controls', catId);
        // Use span for icon to avoid aria issues
        const iconSpan = document.createElement('span');
        iconSpan.setAttribute('aria-hidden', 'true');
        iconSpan.textContent = icon;
        chip.appendChild(iconSpan);
        chip.appendChild(document.createTextNode(' ' + cat));

        chip.addEventListener('click', () => {
          const target = document.getElementById(catId);
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
        chipsWrap.appendChild(chip);
      });
      navEl.appendChild(chipsWrap);
    }

    // Dietary chips
    if (availableTags.length > 0) {
      const dietRow = document.createElement('div');
      dietRow.className = 'dietary-row';
      dietRow.setAttribute('role', 'group');
      dietRow.setAttribute('aria-label', 'Filtros dietarios');

      availableTags.forEach(tag => {
        const meta = DIETARY_META[tag];
        if (!meta) return;
        const active = activeFilters.includes(tag);
        const chip = document.createElement('button');
        chip.className = 'diet-chip' + (active ? ' active' : '');
        chip.setAttribute('role', 'button');
        chip.setAttribute('aria-pressed', active ? 'true' : 'false');
        chip.setAttribute('type', 'button');
        chip.innerHTML = meta.svg;
        const labelNode = document.createTextNode(' ' + meta.label);
        chip.appendChild(labelNode);
        if (active) {
          const dismiss = document.createElement('span');
          dismiss.className = 'chip-dismiss';
          dismiss.setAttribute('aria-hidden', 'true');
          dismiss.textContent = ' ×';
          chip.appendChild(dismiss);
        }
        chip.addEventListener('click', () => {
          const newFilters = active
            ? activeFilters.filter(f => f !== tag)
            : [...activeFilters, tag];
          setState({ activeFilters: newFilters });
        });
        dietRow.appendChild(chip);
      });

      if (activeFilters.length > 0) {
        const clearBtn = document.createElement('button');
        clearBtn.className = 'clear-filters-btn';
        clearBtn.type = 'button';
        clearBtn.textContent = 'Limpiar filtros';
        clearBtn.addEventListener('click', () => setState({ activeFilters: [], search: '' }));
        dietRow.appendChild(clearBtn);
      }
      navEl.appendChild(dietRow);
    }

    // Update --nav-height dynamically
    requestAnimationFrame(() => {
      const h = navEl.offsetHeight;
      document.documentElement.style.setProperty('--nav-height', h + 'px');
    });
  }

  function renderMenu(filteredMenu) {
    // Disconnect any live view observers before destroying the old cards
    viewObservers.forEach(o => o.disconnect());
    viewObservers.length = 0;

    mainEl.innerHTML = '';

    const allCats = Object.keys(state.menu);
    const filteredCats = Object.keys(filteredMenu);

    if (allCats.length === 0) {
      mainEl.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon" aria-hidden="true">🍽️</div>
          <p class="empty-title">Este restaurante aún no tiene platos</p>
          <p class="empty-subtitle">Vuelve pronto</p>
        </div>`;
      return;
    }

    if (filteredCats.length === 0) {
      mainEl.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">No encontramos platos con esos filtros</p>
          <p class="empty-subtitle">Intenta con otros términos</p>
          <button class="m-btn m-btn--primary m-btn--sm" id="clear-all-filters">Limpiar filtros</button>
        </div>`;
      mainEl.querySelector('#clear-all-filters').addEventListener('click', () => {
        setState({ search: '', activeFilters: [] });
      });
      return;
    }

    // Disconnect old scroll observer
    if (scrollObserver) { scrollObserver.disconnect(); scrollObserver = null; }

    const content = document.createElement('div');
    content.className = 'catalog-content';

    filteredCats.forEach(cat => {
      const catId = 'cat-' + slugify(cat);
      const icon = CAT_ICONS[cat] || CAT_ICONS['default'];
      const section = document.createElement('section');
      section.className = 'category-section';
      section.id = catId;

      const titleEl = document.createElement('h2');
      titleEl.className = 'category-title';
      const iconSpan = document.createElement('span');
      iconSpan.setAttribute('aria-hidden', 'true');
      iconSpan.textContent = icon;
      titleEl.appendChild(iconSpan);
      titleEl.appendChild(document.createTextNode(' ' + cat));
      section.appendChild(titleEl);

      const grid = document.createElement('div');
      grid.className = 'dish-grid';

      filteredMenu[cat].forEach(dish => {
        const card = buildDishCard(dish, cat, state, {
          onOpen: (d, el) => openModal(d, cat, el),
          onAdd: (d, c) => {
            updateCart(d, c, 1, false);
            mesioToast(d.name + ' agregado', 'success', 2000);
            trackEvent(d.name, 'add_to_cart', state.botNumber);
          },
          onRemove: (d, c) => {
            updateCart(d, c, -1, false);
          },
          viewObservers,
        });
        grid.appendChild(card);
      });
      section.appendChild(grid);
      content.appendChild(section);
    });

    mainEl.appendChild(content);

    // Announce to screen reader
    const totalVisible = filteredCats.reduce((a, c) => a + filteredMenu[c].length, 0);
    if (announcer) announcer.textContent = `${totalVisible} platos encontrados`;

    // Scroll spy
    setupScrollSpy(filteredCats);
  }

  function rerenderCards() {
    document.querySelectorAll('.dish-card').forEach(card => {
      const meta = _cardMeta.get(card);
      if (!meta) return;
      const qty = state.cart[card.dataset.dishId] || 0;
      const ctrl = card.querySelector('.dish-ctrl');
      if (!ctrl) return;
      _renderDishCtrl(ctrl, meta.dish, meta.cat, qty, meta.available, meta.callbacks);
    });
  }

  function renderCartFooter() {
    const count = cartItemCount();
    if (count === 0) {
      footerEl.style.display = 'none';
      return;
    }
    footerEl.style.display = '';
    const inner = footerEl.querySelector('.cart-footer-inner') || (() => {
      const el = document.createElement('div');
      el.className = 'cart-footer-inner';
      footerEl.appendChild(el);
      return el;
    })();

    const total = fmtPrice(cartTotal(), state.locale, state.currency);
    const label = `Ver pedido (${count} item${count !== 1 ? 's' : ''} · ${total})`;

    let btn = inner.querySelector('.btn-wa');
    if (!btn) {
      btn = document.createElement('button');
      btn.className = 'btn-wa';
      btn.type = 'button';
      btn.innerHTML = SVG_WA + '<span class="btn-wa-label"></span>';
      btn.addEventListener('click', () => {
        const msg = buildWaMessage();
        const waNum = state.botNumber.replace(/\D/g, '');
        window.open(`https://wa.me/${waNum}?text=${encodeURIComponent(msg)}`, '_blank', 'noopener,noreferrer');
      });
      inner.appendChild(btn);
    }
    btn.setAttribute('aria-label', label);
    btn.querySelector('.btn-wa-label').textContent = label;
  }

  function setupScrollSpy(cats) {
    if (scrollObserver) { scrollObserver.disconnect(); scrollObserver = null; }
    scrollObserver = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          state.activeCategory = entry.target.id;
          // Update chip active states
          document.querySelectorAll('.cat-chip').forEach(chip => {
            const active = chip.getAttribute('data-target') === state.activeCategory;
            chip.classList.toggle('active', active);
            chip.setAttribute('aria-selected', active ? 'true' : 'false');
            if (active) chip.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
          });
        }
      });
    }, { rootMargin: '-56px 0px -60% 0px', threshold: 0 });

    cats.forEach(cat => {
      const el = document.getElementById('cat-' + slugify(cat));
      if (el) scrollObserver.observe(el);
    });
  }

  function findDishCategory(dish) {
    // Fallback when caller doesn't know the category (e.g. HeroCarousel).
    // Without this, dishId() would produce a cart key detached from state.menu,
    // silently dropping the item from the WhatsApp message and total.
    for (const [c, dishes] of Object.entries(state.menu || {})) {
      if (Array.isArray(dishes) && dishes.some(d => d.name === dish.name)) return c;
    }
    return '';
  }

  function openModal(dish, cat, triggerEl) {
    if (!modal) {
      modal = new DishModal(document.body, state,  {
        onAdd: (d, c, qty) => {
          updateCart(d, c, qty, true);
          mesioToast(d.name + ' agregado', 'success', 2000);
          trackEvent(d.name, 'add_to_cart', state.botNumber);
        }
      });
    }
    const effectiveCat = cat || findDishCategory(dish);
    modal.open(dish, effectiveCat, triggerEl, state);
  }

  function showLoading() {
    headerEl.innerHTML = '';
    heroEl.style.display = 'none';
    navEl.innerHTML = '';
    mainEl.innerHTML = '';
    mainEl.appendChild(buildSkeletons());
    footerEl.style.display = 'none';
    errorBannerEl.style.display = 'none';
  }

  function showError(msg) {
    errorBannerEl.style.display = '';
    errorBannerEl.querySelector('#error-message').textContent = msg || 'No pudimos cargar el menú';
  }

  function setState(partial) {
    Object.assign(state, partial);
    const filtered = getFilteredMenu(state.menu, state.availability, state.search, state.activeFilters);
    renderNav(filtered, state.search, state.activeCategory, state.activeFilters);
    renderHero(state.menu, state.search);
    renderMenu(filtered);
    renderCartFooter();
  }

  // ── Fetch ──
  async function fetchMenu(url) {
    showLoading();
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      // Feature flag escape hatch. Backend exposes flags flat at the root
      // (see dashboard.py get_public_menu, tables.py public_menu_context).
      if (data.catalog_v2_enabled === false) {
        const qs = window.location.search;
        window.location.replace('/menu-legacy' + qs);
        return;
      }

      state.menu = data.menu || {};
      state.availability = data.availability || {};
      state.locale = data.locale || 'es-CO';
      state.currency = data.currency || 'COP';
      state.restaurantName = data.restaurant_name || data.restaurantName || '';
      state.tableName = data.table_name || data.tableName || null;
      state.tableId   = isTableContext ? paramId : null;
      state.botNumber = data.bot_number || data.botNumber || paramId || '';
      state.waUrl = data.wa_url || data.waUrl || '';
      state.cart = loadCart(state.botNumber);
      state.loading = false;
      state.error = null;

      errorBannerEl.style.display = 'none';
      renderHeader(state);
      setState({});
    } catch (err) {
      state.loading = false;
      state.error = String(err.message || err);
      mainEl.innerHTML = '';
      showError('No pudimos cargar el menú');
    }
  }

  // Error banner retry
  const retryBtn = errorBannerEl.querySelector('#error-retry');
  if (retryBtn) retryBtn.addEventListener('click', () => fetchMenu(apiUrl));

  // Go
  fetchMenu(apiUrl);
}

/* ── Bootstrap ── */
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initCatalog);
} else {
  initCatalog();
}
