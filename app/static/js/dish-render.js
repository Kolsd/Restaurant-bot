/* ═══════════════════════════════════════════════════════════════════
   Dish rendering primitives for the diner chat (pages/diner-chat.js):
   photo with fallback avatar, badges, dietary icons, price formatting.
   Extracted 2026-09-25 from catalog-v2.js — the WhatsApp-era standalone
   /menu page it served was deleted with the WhatsApp channel.
   Depends on mesio-utils.js (mesioImageUrl, _escHtml).
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

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

const SVG_PLUS = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 3v10M3 8h10" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>`;

const SVG_ALLERGEN = `<svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true"><path d="M5.5 1L10 9.5H1L5.5 1Z" stroke="#D97706" stroke-width="1.2" stroke-linejoin="round"/><path d="M5.5 4.5v2" stroke="#D97706" stroke-width="1.2" stroke-linecap="round"/><circle cx="5.5" cy="7.8" r="0.6" fill="#D97706"/></svg>`;

const BADGE_SVG = {
  chef_pick: `<svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M1 8h8M2 8V5.5L5 3l3 2.5V8" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><circle cx="2" cy="5" r="0.8" fill="currentColor"/><circle cx="5" cy="2.5" r="0.8" fill="currentColor"/><circle cx="8" cy="5" r="0.8" fill="currentColor"/></svg>`,
  new: `<svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M5 1v2M5 7v2M1 5h2M7 5h2M2.5 2.5l1.5 1.5M6 6l1.5 1.5M2.5 7.5L4 6M6 4l1.5-1.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  popular: `<svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M5 9C3 9 2 7.5 2 6C2 4.5 3 4 4 3C3.5 5 4.5 5.5 5 5.5C4 4 4.5 2.5 6.5 1C6.5 3 7 3.5 7 5C8 4 7.5 3 7.5 3C9 4 8.5 6 8.5 6.5C8.5 8 7 9 5 9Z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/></svg>`
};

function buildDishImage(dish) {
  const wrap = document.createElement('div');
  wrap.className = 'dish-img-wrap';

  if (dish.image_url) {
    const img = document.createElement('img');
    img.alt = dish.name; // safe — no innerHTML
    img.loading = 'lazy';
    img.decoding = 'async';
    img.src = mesioImageUrl(dish.image_url, 'card');
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

window.MesioCatalogRenderer = {
  buildDishImage,
  buildFallback,
  buildBadge,
  fmtPrice,
  dishGradient,
  DietaryMeta: DIETARY_META,
  BadgeSvg: BADGE_SVG,
  CatIcons: CAT_ICONS,
  SvgPlus: SVG_PLUS,
  SvgAllergen: SVG_ALLERGEN,
};
