/* ══ Mesio — Public QR Menu (redesign 2026) ═══════════════════════════
   Public page — NO auth headers.
   Entry points from URL:
     /menu/<bot_number>           → fetches /api/public/menu/<bot_number>
     /menu/<bot_number>?table=<id> → also fetches /api/public/menu-context/<table_id>
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────────────
  var state = {
    restaurant: null,
    menu: {},
    availability: {},
    categories: [],
    cart: {},        // { dishKey: { dish, qty } }
    currency: 'COP',
    locale: 'es-CO',
    tableContext: null,
    activeCategory: null,
  };

  // ── DOM refs ────────────────────────────────────────────────────────
  var phoneEl     = null;
  var heroEl      = null;
  var catsEl      = null;
  var mainEl      = null;
  var cartEl      = null;

  // ── Boot ────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    phoneEl  = document.getElementById('qr-phone');
    heroEl   = document.getElementById('qr-hero');
    catsEl   = document.getElementById('qr-categories');
    mainEl   = document.getElementById('qr-main');
    cartEl   = document.getElementById('qr-cart');

    var botNumber = getBotNumber();
    if (!botNumber) {
      showError('Enlace de menú inválido.');
      return;
    }
    loadMenu(botNumber);
  });

  // ── URL parsing ─────────────────────────────────────────────────────
  function getBotNumber() {
    var parts = window.location.pathname.split('/');
    return decodeURIComponent(parts[parts.length - 1] || '');
  }

  function getTableId() {
    var p = new URLSearchParams(window.location.search);
    return p.get('table') || null;
  }

  // ── Data fetching ───────────────────────────────────────────────────
  function loadMenu(botNumber) {
    showLoading();
    var menuUrl    = '/api/public/menu/' + encodeURIComponent(botNumber);
    var tableId    = getTableId();
    var promises   = [fetch(menuUrl).then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })];
    if (tableId) {
      promises.push(
        fetch('/api/public/menu-context/' + encodeURIComponent(tableId))
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; })
      );
    }

    Promise.all(promises).then(function (results) {
      var menuData    = results[0];
      var contextData = results[1] || null;
      processMenu(menuData, contextData);
    }).catch(function (err) {
      console.error('menu.js: load failed', err);
      showError('No pudimos cargar el menú. Intenta de nuevo.');
    });
  }

  // ── Data processing ─────────────────────────────────────────────────
  function processMenu(data, contextData) {
    state.restaurant   = data;
    state.currency     = data.currency || 'COP';
    state.locale       = 'es-CO';
    state.availability = data.availability || {};

    // Build ordered categories
    var menu = data.menu || {};
    if (typeof menu === 'string') {
      try { menu = JSON.parse(menu); } catch (e) { menu = {}; }
    }
    state.menu       = menu;
    state.categories = Object.keys(menu).filter(function (k) {
      return Array.isArray(menu[k]) && menu[k].length > 0;
    });

    // Table context for "Valentina te atiende" chip
    if (contextData && contextData.table_context) {
      state.tableContext = contextData.table_context;
    }

    if (state.categories.length > 0) {
      state.activeCategory = state.categories[0];
    }

    render();
  }

  // ── Rendering ────────────────────────────────────────────────────────
  function render() {
    renderHero();
    renderCategories();
    renderDishes();
    renderCart();
  }

  function renderHero() {
    var rest = state.restaurant;
    if (!rest) return;

    // Restaurant name
    var nameEl = document.getElementById('qr-rest-name');
    if (nameEl) nameEl.textContent = rest.restaurant_name || '';

    // Address / sub-text
    var subEl = document.getElementById('qr-rest-sub');
    if (subEl) subEl.textContent = rest.address || rest.description || '';

    // Mesa tag
    var tagEl = document.getElementById('qr-mesa-tag');
    if (tagEl) {
      var tc = state.tableContext;
      if (tc) {
        var tagText = '🪑 Estás en ' + tc.table_name;
        if (tc.assigned_mesero && tc.assigned_mesero.first_name) {
          tagText += ' · ' + tc.assigned_mesero.first_name + ' te atiende';
        }
        tagEl.textContent = tagText;
        tagEl.style.display = 'inline-flex';
      } else {
        tagEl.style.display = 'none';
      }
    }
  }

  function renderCategories() {
    if (!catsEl) return;
    catsEl.innerHTML = '';
    state.categories.forEach(function (cat) {
      var btn = document.createElement('button');
      btn.className = 'qr-cat-pill' + (cat === state.activeCategory ? ' active' : '');
      btn.textContent = cat;
      btn.addEventListener('click', function () {
        state.activeCategory = cat;
        document.querySelectorAll('.qr-cat-pill').forEach(function (b) {
          b.classList.toggle('active', b.textContent === cat);
        });
        renderDishes();
        // Scroll to section
        var sec = document.getElementById('section-' + slugify(cat));
        if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      catsEl.appendChild(btn);
    });
  }

  function renderDishes() {
    if (!mainEl) return;
    mainEl.innerHTML = '';

    if (state.categories.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'qr-empty';
      empty.textContent = 'El menú está vacío por ahora.';
      mainEl.appendChild(empty);
      return;
    }

    state.categories.forEach(function (cat) {
      var dishes = (state.menu[cat] || []).filter(function (d) {
        return d.active !== false;
      });
      if (dishes.length === 0) return;

      var section = document.createElement('div');
      section.className = 'qr-section';
      section.id = 'section-' + slugify(cat);

      var title = document.createElement('h2');
      title.className = 'qr-section-title';

      var titleText = document.createElement('span');
      titleText.textContent = cat;

      var count = document.createElement('span');
      count.className = 'qr-section-count';
      count.textContent = dishes.length + ' plato' + (dishes.length !== 1 ? 's' : '');

      title.appendChild(titleText);
      title.appendChild(count);
      section.appendChild(title);

      dishes.forEach(function (dish) {
        section.appendChild(buildDishCard(dish));
      });

      mainEl.appendChild(section);
    });
  }

  function buildDishCard(dish) {
    var avail = state.availability[dish.name];
    var isUnavailable = avail === false;

    var article = document.createElement('article');
    article.className = 'qr-dish' + (isUnavailable ? ' unavailable' : '');

    // Left: info
    var info = document.createElement('div');

    // Name + tag
    var nameRow = document.createElement('div');
    nameRow.className = 'qr-dish-name';
    var nameSpan = document.createElement('span');
    nameSpan.textContent = dish.name;
    nameRow.appendChild(nameSpan);

    if (isUnavailable) {
      nameRow.appendChild(buildTag('Agotado', 'out'));
    } else if (dish.badges && dish.badges.includes('popular')) {
      nameRow.appendChild(buildTag('Más pedido', ''));
    } else if (dish.badges && dish.badges.includes('chef_pick')) {
      nameRow.appendChild(buildTag('Chef pick', ''));
    }

    if (dish.tags && dish.tags.includes('vegan')) {
      nameRow.appendChild(buildTag('Vegano', 'veg'));
    }

    info.appendChild(nameRow);

    // Description
    if (dish.description) {
      var desc = document.createElement('div');
      desc.className = 'qr-dish-desc';
      desc.textContent = dish.description;
      info.appendChild(desc);
    }

    // Meta (rating + prep time)
    var meta = buildDishMeta(dish);
    if (meta) info.appendChild(meta);

    article.appendChild(info);

    // Right: image + price + add button
    var right = document.createElement('div');
    right.className = 'qr-dish-right';

    // Image
    var imgBox = document.createElement('div');
    imgBox.className = 'qr-dish-img';
    if (dish.image_url) {
      var img = document.createElement('img');
      img.alt = dish.name;
      img.loading = 'lazy';
      img.src = dish.image_url;
      img.addEventListener('error', function () {
        imgBox.innerHTML = '';
        imgBox.textContent = dishEmoji(dish);
      });
      imgBox.appendChild(img);
    } else {
      imgBox.textContent = dishEmoji(dish);
    }
    right.appendChild(imgBox);

    // Price
    var priceEl = document.createElement('div');
    priceEl.className = 'qr-dish-price';
    priceEl.textContent = fmtPrice(dish.price);
    right.appendChild(priceEl);

    // Add button (only if available)
    if (!isUnavailable) {
      var addBtn = document.createElement('button');
      addBtn.className = 'qr-add-btn';
      addBtn.type = 'button';
      addBtn.textContent = '+';
      addBtn.setAttribute('aria-label', 'Agregar ' + dish.name);
      addBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        addToCart(dish);
      });
      right.appendChild(addBtn);
    }

    article.appendChild(right);
    return article;
  }

  function buildTag(text, cls) {
    var span = document.createElement('span');
    span.className = 'qr-dish-tag' + (cls ? ' ' + cls : '');
    span.textContent = text;
    return span;
  }

  function buildDishMeta(dish) {
    var parts = [];
    if (dish.prep_time_min) parts.push({ icon: '🕐', text: dish.prep_time_min + ' min' });
    if (!parts.length) return null;
    var meta = document.createElement('div');
    meta.className = 'qr-dish-meta';
    parts.forEach(function (p) {
      var span = document.createElement('span');
      span.textContent = p.icon + ' ' + p.text;
      meta.appendChild(span);
    });
    return meta;
  }

  // ── Cart ─────────────────────────────────────────────────────────────
  function addToCart(dish) {
    var key = slugify(dish.name);
    if (state.cart[key]) {
      state.cart[key].qty += 1;
    } else {
      state.cart[key] = { dish: dish, qty: 1 };
    }
    renderCart();
  }

  function renderCart() {
    if (!cartEl) return;
    var items = Object.values(state.cart);
    var total = items.reduce(function (sum, item) { return sum + item.dish.price * item.qty; }, 0);
    var count = items.reduce(function (sum, item) { return sum + item.qty; }, 0);

    if (count === 0) {
      cartEl.style.display = 'none';
      return;
    }

    cartEl.style.display = 'flex';
    var countEl = document.getElementById('qr-cart-count');
    var totalEl = document.getElementById('qr-cart-total');
    if (countEl) countEl.textContent = count;
    if (totalEl) totalEl.textContent = fmtPrice(total);
  }

  // ── Cart click → WhatsApp deep-link ──────────────────────────────────
  function openCartOnWhatsApp() {
    var rest = state.restaurant;
    if (!rest) return;
    var lines = Object.values(state.cart).map(function (item) {
      return item.qty + '× ' + item.dish.name + ' (' + fmtPrice(item.dish.price * item.qty) + ')';
    });
    var total = Object.values(state.cart).reduce(function (s, i) { return s + i.dish.price * i.qty; }, 0);
    lines.push('\nTotal: ' + fmtPrice(total));
    var text = 'Hola, quiero ordenar:\n' + lines.join('\n');
    var phone = (rest.bot_number || '').replace(/\D/g, '');
    window.open('https://wa.me/' + phone + '?text=' + encodeURIComponent(text), '_blank');
  }

  // ── Loading / error helpers ──────────────────────────────────────────
  function showLoading() {
    if (!mainEl) return;
    mainEl.innerHTML = '<div class="qr-loading"><div class="qr-spinner"></div><span>Cargando menú…</span></div>';
  }

  function showError(msg) {
    if (!mainEl) return;
    var div = document.createElement('div');
    div.className = 'qr-error';
    var p = document.createElement('p');
    p.textContent = msg;
    var btn = document.createElement('button');
    btn.className = 'qr-error-retry';
    btn.type = 'button';
    btn.textContent = 'Reintentar';
    btn.addEventListener('click', function () {
      location.reload();
    });
    div.appendChild(p);
    div.appendChild(btn);
    mainEl.innerHTML = '';
    mainEl.appendChild(div);
  }

  // ── Utilities ────────────────────────────────────────────────────────
  function fmtPrice(n) {
    try {
      return new Intl.NumberFormat(state.locale, {
        style: 'currency',
        currency: state.currency,
        minimumFractionDigits: ['COP','CLP','JPY','KRW','VND','PYG','ISK'].includes(state.currency) ? 0 : 2,
        maximumFractionDigits: ['COP','CLP','JPY','KRW','VND','PYG','ISK'].includes(state.currency) ? 0 : 2,
      }).format(n || 0);
    } catch (e) {
      return '$' + Number(n || 0).toLocaleString();
    }
  }

  function slugify(s) {
    return String(s).toLowerCase().replace(/[^a-z0-9]/g, '-');
  }

  function dishEmoji(dish) {
    var name = (dish.name || '').toLowerCase();
    if (name.includes('pizza'))     return '🍕';
    if (name.includes('pasta') || name.includes('espagueti')) return '🍝';
    if (name.includes('burger') || name.includes('hambur'))   return '🍔';
    if (name.includes('pollo') || name.includes('pechuga'))   return '🍗';
    if (name.includes('sopa') || name.includes('ajiaco') || name.includes('caldo')) return '🥘';
    if (name.includes('ensalada'))  return '🥗';
    if (name.includes('postre') || name.includes('torta') || name.includes('pastel')) return '🍰';
    if (name.includes('bebida') || name.includes('jugo') || name.includes('limonada')) return '🥤';
    if (name.includes('café') || name.includes('café') || name.includes('chocolate')) return '☕';
    if (name.includes('arepa'))     return '🫓';
    if (name.includes('arroz'))     return '🍚';
    if (name.includes('bandeja'))   return '🍛';
    if (name.includes('huevo'))     return '🍳';
    return '🍽️';
  }

  // ── Wire up cart click after DOM ready ──────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    var cart = document.getElementById('qr-cart');
    if (cart) {
      cart.addEventListener('click', openCartOnWhatsApp);
    }
  });

}());
