/* ═══════════════════════════════════════════════════════════════════
   Mesio — Diner Chat (chat pivot)

   The diner's own chat surface, opened from a table QR code. The bot
   presents the carta and takes the order INSIDE the conversation —
   this page renders whatever `blocks` the backend sends alongside each
   turn (dish cards, category chips, cart summary, payment options,
   waiter acknowledgements) and falls back to the plain-text `message`
   for anything it doesn't recognise (forward compatibility).

   Backend contract (live — see app/routes/diner.py):
     POST /api/diner/session      → {table_id} → session token + opening turn
     POST /api/diner/chat         → {token, message} → {message, blocks}
     GET  /api/diner/menu         → ?token= → full carta for the "Ver carta completa" panel
     POST /api/diner/waiter-call  → {token, reason: bill|cutlery|napkins|other}
     POST /api/diner/cart/add     → {token, sku?, name?, qty, note?} → {message, blocks:[cart_summary]}
     POST /api/diner/cart/update  → {token, line_id, qty?, note?} → {message, blocks:[cart_summary]} (qty=0 removes)
     POST /api/diner/cart/remove  → {token, line_id} → {message, blocks:[cart_summary]}
     GET  /api/diner/cart         → ?token= → {message, blocks:[cart_summary]}

   A tap (add/change qty/remove/edit note) is a deterministic operation with
   nothing for the LLM to interpret, so every cart mutation calls the
   cart/* endpoints directly — NEVER sendMessage()/natural language. Only
   free-text typed in the composer goes through POST /api/diner/chat.

   Depends on (loaded before this file):
     mesio-utils.js   → _escHtml, mesioToast, mesioConfirm, mesioPrompt, mesioFocusTrap
     catalog-v2.js    → window.MesioCatalogRenderer (dish image/badge/price helpers)
     diner-session.js → window.DinerSession (session token + fetch wrapper)

   XSS: every string in a block (dish name/description, chip labels, cart
   item names/notes, payment labels, bot text) comes from the network and
   is treated as untrusted — textContent / _escHtml only, never innerHTML
   with data. innerHTML is used ONLY for static SVG icon markup that
   carries no external data.
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

var WAITER_REASONS = [
  { value: 'bill', label: 'La cuenta', icon: '🧾' },
  { value: 'cutlery', label: 'Cubiertos', icon: '🍴' },
  { value: 'napkins', label: 'Servilletas', icon: '🧻' },
  { value: 'other', label: 'Otra cosa', icon: '✋' },
];

var state = {
  token: null,
  assistant: true,
  restaurantName: '',
  tableLabel: '',
  currency: 'COP',
  locale: 'es-CO',
  cart: null,
  busy: false,
  // joinCode: the host diner's own code, shown in the header so they can
  // read it aloud. joined: false while a second diner is waiting on
  // POST /api/diner/join — composer/send stay disabled until then (see
  // setBusy()). Browsing the menu / adding to cart is still allowed.
  joinCode: '',
  joined: true,
  // Diner memory (app/services/diner_memory.py): orgId keys the "No,
  // gracias" choice; remembered = this browser is a known diner here.
  orgId: null,
  remembered: false,

  // ── Delivery/pickup (docs/claude/delivery-web.md chunk 5) ──────────
  // orderMode stays 'dine_in' for the /chat/{table_id} entry point (the
  // ORIGINAL behavior above, untouched). /pedir/{slug} sets it to
  // 'delivery' or 'pickup' once the sede-assignment ladder + session open
  // finish (see startDeliveryEntry()/openDeliverySession() below) — every
  // dine-in-only affordance (Tu mesa, join code, waiter FAB "la cuenta")
  // keys off `document.body.classList.contains('pedir-mode')` (CSS) rather
  // than re-checking orderMode at every call site.
  orderMode: 'dine_in',
  slug: null,
  sedeName: '',
  sedePhone: '',
  deliveryConfig: { payment_methods: [], delivery_fee: 0, min_order: 0 },
  turnstileSiteKey: null,
  turnstileSessionToken: null,
  turnstileCheckoutToken: null,
  lastPos: null,
};

function isDeliveryOrderMode() {
  return state.orderMode === 'delivery' || state.orderMode === 'pickup';
}

function dinerEl(id) { return document.getElementById(id); }

function getToken() { return state.token; }

/* ── Realtime (SSE invalidation events) ───────────────────────────────
 * Connects once the diner has a session token (either freshly minted or
 * restored from sessionStorage — see startSession()/restoreSavedSession()
 * below, both of which call this right after state.token is set).
 * Idempotent: MesioRealtime.connect() is only ever called once per page
 * load even though both of those paths can reach here. ────────────────*/
var _rtConnected = false;
function connectDinerRealtime() {
  if (_rtConnected || !state.token || !window.MesioRealtime) return;
  _rtConnected = true;
  MesioRealtime.connect('/api/diner/stream', getToken);
  MesioRealtime.on('table_order.created', () => TablePanel.refresh());
  MesioRealtime.on('table_order.updated', () => { TablePanel.refresh(); announceKitchenProgress(); });
  MesioRealtime.on('check.updated', () => pollDinerStatus());
  MesioRealtime.on('nps.updated', () => pollDinerStatus());
  MesioRealtime.on('resync', () => { TablePanel.refresh(); pollDinerStatus(); });
  announceKitchenProgress();  // baseline, so the first change is the one announced
}

/* ── Kitchen progress in the chat ───────────────────────────────────
 * table_order.updated used to refresh only the "Tu mesa" panel, and only
 * while it was open — a diner never learned their food was ready. Compare
 * the statuses of the diner's OWN orders with the last ones seen and say
 * the change in the chat. The first read only records the baseline, so a
 * reload never repeats an old notice. */
var KITCHEN_NOTICES = {
  listo: '¡Tu pedido está listo! Ya te lo llevan a la mesa.',
  entregado: '¡Buen provecho!',
};
var _kitchenSeen = null;  // order_id -> status; null until the baseline read

async function announceKitchenProgress() {
  if (!state.token || isDeliveryOrderMode()) return;
  var data;
  try {
    data = await DinerSession.fetch('/api/diner/table', 'GET', null, getToken());
  } catch (e) { return; }
  var mine = (data && Array.isArray(data.orders) ? data.orders : []).filter(function (o) { return o.mine; });
  var first = _kitchenSeen === null;
  var seen = _kitchenSeen || {};
  _kitchenSeen = {};
  mine.forEach(function (o) {
    _kitchenSeen[o.order_id] = o.status;
    if (!first && seen[o.order_id] !== o.status && KITCHEN_NOTICES[o.status]) {
      processBotTurn({ message: KITCHEN_NOTICES[o.status] });
    }
  });
}

function renderer() { return window.MesioCatalogRenderer; }

function genIdempotencyKey() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }
  return 'idem-' + Date.now() + '-' + Math.random().toString(16).slice(2);
}

/* ── Chat log — bubbles ────────────────────────────────────────────── */

function createBotBubble() {
  var bubble = document.createElement('div');
  bubble.className = 'diner-msg diner-msg--bot';
  return bubble;
}

function createDinerBubble(text) {
  var bubble = document.createElement('div');
  bubble.className = 'diner-msg diner-msg--diner';
  var p = document.createElement('p');
  p.textContent = text;
  bubble.appendChild(p);
  return bubble;
}

function createTextNode(text) {
  var p = document.createElement('p');
  p.className = 'diner-block-text';
  p.textContent = text;
  return p;
}

function appendBubble(node) {
  var log = dinerEl('diner-log');
  if (!log || !node) return;
  log.appendChild(node);
  log.scrollTop = log.scrollHeight;
}

function showTyping() {
  hideTyping();
  var bubble = document.createElement('div');
  bubble.className = 'diner-msg diner-msg--bot diner-typing';
  bubble.id = 'diner-typing-indicator';
  bubble.setAttribute('aria-label', 'El restaurante está escribiendo');
  for (var i = 0; i < 3; i++) {
    var dot = document.createElement('span');
    dot.className = 'diner-typing-dot';
    bubble.appendChild(dot);
  }
  appendBubble(bubble);
}

function hideTyping() {
  var existing = dinerEl('diner-typing-indicator');
  if (existing) existing.remove();
}

/* Plans without the AI assistant (Esencial) have no free-text box: the
 * diner orders from the carta chips and cards, and the waiter button stays.
 * `enabled` comes from the session response; anything but false keeps the
 * box (older saved sessions carry no flag). */
function applyAssistant(enabled) {
  state.assistant = enabled !== false;
  var composer = dinerEl('diner-composer');
  if (composer) composer.style.display = state.assistant ? '' : 'none';
  document.body.classList.toggle('diner-no-assistant', !state.assistant);
}

function setBusy(busy) {
  state.busy = busy;
  var input = dinerEl('diner-input');
  var sendBtn = dinerEl('diner-send-btn');
  var locked = busy || !state.token || !state.joined;
  if (input) input.disabled = locked;
  if (sendBtn) sendBtn.disabled = locked;
}

/* ── Dish card (shared between chat bubbles and the full carta panel) ─
 * Reuses catalog-v2.js's image/fallback/badge rendering and price
 * formatting so photos, sold-out/chef-pick badges and money formatting
 * stay pixel-identical to the rest of the product. The card container,
 * dietary-tag row and add-with-note flow are new — this surface has no
 * equivalent in the standalone /menu page.
 * ════════════════════════════════════════════════════════════════════ */
function buildDinerDishCard(dish, opts) {
  opts = opts || {};
  var R = renderer();
  // Dishes carry `available` (live stock, see _dish_cards_for_category in
  // app/routes/diner.py) — NOT `active` (that's a menu-publish flag the
  // backend already filters out before this ever reaches the client).
  var available = !dish || dish.available !== false;

  var card = document.createElement('article');
  card.className = 'diner-dish-card' + (opts.compact ? ' diner-dish-card--compact' : ' diner-dish-card--grid');
  card.setAttribute('role', 'group');
  card.setAttribute('aria-label', (dish && dish.name) || 'Plato');

  var imgWrap = R.buildDishImage(dish || {});
  imgWrap.appendChild(R.buildBadge(dish || {}, available));
  card.appendChild(imgWrap);

  var info = document.createElement('div');
  info.className = 'diner-dish-info';

  var nameEl = document.createElement('h3');
  nameEl.className = 'diner-dish-name';
  nameEl.textContent = (dish && dish.name) || '';
  info.appendChild(nameEl);

  if (dish && dish.description) {
    var descEl = document.createElement('p');
    descEl.className = 'diner-dish-desc';
    descEl.textContent = dish.description;
    info.appendChild(descEl);
  }

  var tags = (dish && Array.isArray(dish.tags)) ? dish.tags : [];
  var allergens = (dish && Array.isArray(dish.allergens)) ? dish.allergens : [];
  if (tags.length || allergens.length) {
    var iconsRow = document.createElement('div');
    iconsRow.className = 'diner-dish-icons';
    tags.forEach(function (tag) {
      var meta = R.DietaryMeta[tag];
      if (!meta) return;
      var chip = document.createElement('span');
      chip.className = 'diner-dish-tag';
      chip.innerHTML = meta.svg; // static SVG markup only, no external data
      chip.appendChild(document.createTextNode(' ' + meta.label));
      iconsRow.appendChild(chip);
    });
    if (allergens.length) {
      var aChip = document.createElement('span');
      aChip.className = 'diner-dish-tag diner-dish-tag--allergen';
      aChip.innerHTML = R.SvgAllergen; // static SVG markup only
      aChip.title = 'Alérgenos: ' + allergens.join(', ');
      aChip.appendChild(document.createTextNode(' ' + allergens.length));
      iconsRow.appendChild(aChip);
    }
    info.appendChild(iconsRow);
  }

  var footer = document.createElement('div');
  footer.className = 'diner-dish-footer';

  var priceEl = document.createElement('span');
  priceEl.className = 'diner-dish-price';
  priceEl.textContent = R.fmtPrice(dish ? dish.price : 0, state.locale, state.currency);
  footer.appendChild(priceEl);

  var addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'diner-dish-add-btn';
  addBtn.setAttribute('aria-label', 'Agregar ' + ((dish && dish.name) || 'plato'));
  if (!available) addBtn.disabled = true;
  addBtn.innerHTML = R.SvgPlus; // static SVG markup only
  addBtn.addEventListener('click', function () {
    if (!available) return;
    AddSheet.open(dish);
  });
  footer.appendChild(addBtn);

  info.appendChild(footer);
  card.appendChild(info);
  return card;
}

/* ── Block renderers ──────────────────────────────────────────────── */

function renderTextBlock(block) {
  if (block.text == null || block.text === '') return null;
  return createTextNode(String(block.text));
}

function renderDishCardsBlock(block) {
  var dishes = Array.isArray(block.dishes) ? block.dishes : [];
  if (!dishes.length) return null;
  var wrap = document.createElement('div');
  wrap.className = 'diner-dish-scroll';
  dishes.forEach(function (dish) {
    wrap.appendChild(buildDinerDishCard(dish, { compact: true }));
  });
  return wrap;
}

function renderCategoryChipsBlock(block) {
  var chips = Array.isArray(block.chips) ? block.chips : [];
  if (!chips.length) return null;
  var wrap = document.createElement('div');
  wrap.className = 'diner-chip-row';
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', 'Sugerencias');
  var any = false;
  chips.forEach(function (chip) {
    if (!chip || typeof chip.value !== 'string' || !chip.value) return;
    any = true;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'diner-chip';
    var label = chip.label != null && chip.label !== '' ? String(chip.label) : chip.value;
    btn.textContent = label;
    btn.addEventListener('click', function () {
      sendMessage(chip.value, { displayText: label });
    });
    wrap.appendChild(btn);
  });
  return any ? wrap : null;
}

function renderPaymentOptionsBlock(block) {
  var options = Array.isArray(block.options) ? block.options : [];
  if (!options.length) return null;
  var wrap = document.createElement('div');
  wrap.className = 'diner-chip-row diner-pay-row';
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', 'Opciones de pago');
  var any = false;
  options.forEach(function (opt) {
    if (!opt || typeof opt.value !== 'string' || !opt.value) return;
    any = true;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'diner-chip diner-chip--pay';
    var label = opt.label != null && opt.label !== '' ? String(opt.label) : opt.value;
    btn.textContent = label;
    btn.addEventListener('click', function () {
      sendMessage(opt.value, { displayText: label });
    });
    wrap.appendChild(btn);
  });
  return any ? wrap : null;
}

// Every "Tu pedido" card currently in the conversation. The card is a view of
// THE cart, not a receipt of the moment it was added: before this, removing a
// dish in the cart panel left the older chat cards still listing it (and still
// offering "Enviar pedido" for an order that no longer existed).
var liveCartCards = [];

function renderCartSummaryBlock(block) {
  var card = document.createElement('div');
  card.className = 'diner-cart-card';
  fillCartCard(card, block);
  liveCartCards.push(card);
  return card;
}

// Re-draw every cart card already on screen from the current cart.
function refreshCartCards() {
  var current = state.cart || { items: [], subtotal: 0 };
  liveCartCards = liveCartCards.filter(function (card) { return card.isConnected; });
  liveCartCards.forEach(function (card) { fillCartCard(card, current); });
}

function fillCartCard(card, block) {
  var items = Array.isArray(block.items) ? block.items : [];
  card.textContent = '';

  var title = document.createElement('p');
  title.className = 'diner-cart-card-title';
  title.textContent = 'Tu pedido';
  card.appendChild(title);

  if (!items.length) {
    var empty = document.createElement('p');
    empty.className = 'diner-cart-card-empty';
    empty.textContent = 'Todavía no has agregado nada.';
    card.appendChild(empty);
  } else {
    var list = document.createElement('ul');
    list.className = 'diner-cart-card-list';
    items.slice(0, 5).forEach(function (item) {
      var li = document.createElement('li');
      var qty = document.createElement('span');
      qty.className = 'diner-cart-card-qty';
      qty.textContent = (item.qty != null ? item.qty : 1) + '×';
      var name = document.createElement('span');
      name.className = 'diner-cart-card-name';
      name.textContent = item.name || '';
      li.appendChild(qty);
      li.appendChild(name);
      list.appendChild(li);
    });
    card.appendChild(list);
    if (items.length > 5) {
      var more = document.createElement('p');
      more.className = 'diner-cart-card-more';
      more.textContent = '+' + (items.length - 5) + ' más';
      card.appendChild(more);
    }
    var subtotalRow = document.createElement('p');
    subtotalRow.className = 'diner-cart-card-subtotal';
    var subtotalLabel = document.createElement('span');
    subtotalLabel.textContent = 'Subtotal';
    var subtotalValue = document.createElement('strong');
    subtotalValue.textContent = renderer().fmtPrice(block.subtotal, state.locale, block.currency || state.currency);
    subtotalRow.appendChild(subtotalLabel);
    subtotalRow.appendChild(subtotalValue);
    card.appendChild(subtotalRow);
  }

  var actionsRow = document.createElement('div');
  actionsRow.className = 'diner-cart-card-actions';

  var viewBtn = document.createElement('button');
  viewBtn.type = 'button';
  viewBtn.className = 'm-btn m-btn--secondary m-btn--sm diner-cart-card-btn';
  viewBtn.textContent = 'Ver / editar pedido';
  viewBtn.addEventListener('click', function () { CartPanel.open(); });
  actionsRow.appendChild(viewBtn);

  if (items.length) {
    var sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'm-btn m-btn--primary m-btn--sm diner-cart-card-btn';
    sendBtn.textContent = isDeliveryOrderMode() ? 'Finalizar pedido' : 'Enviar pedido';
    sendBtn.addEventListener('click', function () {
      if (isDeliveryOrderMode()) { DeliveryCheckoutSheet.open(); } else { SendOrderSheet.open(); }
    });
    actionsRow.appendChild(sendBtn);
  }

  card.appendChild(actionsRow);
}

function renderWaiterAckBlock(block) {
  var card = document.createElement('div');
  card.className = 'diner-ack-card';
  var icon = document.createElement('span');
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = '🔔';
  var p = document.createElement('p');
  p.textContent = block.text || 'Ya avisamos al mesero.';
  card.appendChild(icon);
  card.appendChild(p);
  return card;
}

/* ── NPS prompt block — backed by the SAME agent.py NPS state machine used
 * on WhatsApp (see app/services/blocks.py build_nps_prompt_block). A star
 * tap / skip / comment submit is just a normal POST /api/diner/chat message
 * ("1".."5", "no calificar", or free text) — no separate NPS endpoint.
 * ════════════════════════════════════════════════════════════════════ */
function renderNpsPromptBlock(block) {
  var scale = (block && block.scale) || 5;
  var stage = (block && block.stage) === 'comment' ? 'comment' : 'score';
  var card = document.createElement('div');
  card.className = 'diner-nps-card';

  var title = document.createElement('p');
  title.className = 'diner-nps-title';
  title.textContent = stage === 'comment'
    ? '¿Qué podríamos mejorar? (opcional)'
    : '¿Cómo calificarías tu experiencia?';
  card.appendChild(title);

  if (stage === 'comment') {
    var row = document.createElement('div');
    row.className = 'diner-nps-comment-row';
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'diner-nps-comment-input';
    input.maxLength = 500;
    input.placeholder = 'Escribe tu comentario...';
    input.setAttribute('aria-label', '¿Qué podríamos mejorar?');
    row.appendChild(input);

    var sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'm-btn m-btn--primary m-btn--sm';
    sendBtn.textContent = 'Enviar';
    sendBtn.addEventListener('click', function () {
      var text = input.value.trim() || 'Sin comentario';
      sendMessage(text, { displayText: text });
    });
    row.appendChild(sendBtn);
    card.appendChild(row);
  } else {
    var stars = document.createElement('div');
    stars.className = 'diner-nps-stars';
    stars.setAttribute('role', 'group');
    stars.setAttribute('aria-label', 'Calificación de 1 a ' + scale + ' estrellas');
    for (var i = 1; i <= scale; i++) {
      (function (score) {
        var starBtn = document.createElement('button');
        starBtn.type = 'button';
        starBtn.className = 'diner-nps-star';
        starBtn.setAttribute('aria-label', score + ' estrella' + (score > 1 ? 's' : ''));
        starBtn.textContent = '★';
        starBtn.addEventListener('mouseenter', function () {
          Array.prototype.forEach.call(stars.children, function (el, idx) {
            el.classList.toggle('diner-nps-star--filled', idx < score);
          });
        });
        starBtn.addEventListener('click', function () {
          sendMessage(String(score), { displayText: score + ' ★' });
        });
        stars.appendChild(starBtn);
      })(i);
    }
    card.appendChild(stars);

    var skipBtn = document.createElement('button');
    skipBtn.type = 'button';
    skipBtn.className = 'diner-nps-skip';
    skipBtn.textContent = 'No calificar';
    skipBtn.addEventListener('click', function () {
      sendMessage('no calificar', { displayText: 'No calificar' });
    });
    card.appendChild(skipBtn);
  }

  return card;
}

function renderCheckoutStatusBlock(status) {
  var card = document.createElement('div');
  card.className = 'diner-checkout-status-card' + (status.status === 'paid' ? ' diner-checkout-status-card--paid' : '');
  var icon = document.createElement('span');
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = status.status === 'paid' ? '✅' : '🧾';
  var p = document.createElement('p');
  p.textContent = status.status === 'paid'
    ? 'Tu cuenta ya fue pagada. ¡Gracias por tu visita!'
    : 'Ya le avisamos al mesero, ya viene con tu cuenta.';
  card.appendChild(icon);
  card.appendChild(p);
  return card;
}

/* ── Diner memory ("lo de siempre") ──────────────────────────────────
 * memory_actions comes in a remembered diner's greeting: repeat the last
 * order (POST /api/diner/memory/repeat — the server re-prices and skips what
 * this sede doesn't serve today) or forget this phone. The consent offer is
 * shown once an order went to the kitchen (see SendSheet.confirmSend).
 * ════════════════════════════════════════════════════════════════════ */

function renderMemoryActionsBlock(block) {
  var wrap = document.createElement('div');
  wrap.className = 'diner-memory';
  var unavailable = Array.isArray(block.unavailable) ? block.unavailable : [];
  if (block.can_repeat) {
    var row = document.createElement('div');
    row.className = 'diner-chip-row';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'diner-chip';
    btn.textContent = block.repeat_label ? String(block.repeat_label) : 'Repetir mi último pedido';
    btn.addEventListener('click', function () { repeatLastOrder(btn); });
    row.appendChild(btn);
    wrap.appendChild(row);
  }
  if (unavailable.length) {
    var note = document.createElement('p');
    note.className = 'diner-memory-note';
    note.textContent = 'Hoy aquí no tenemos: ' + unavailable.map(String).join(', ') + '.';
    wrap.appendChild(note);
  }
  var forget = document.createElement('button');
  forget.type = 'button';
  forget.className = 'diner-memory-forget';
  forget.textContent = '¿No eres tú? Olvidar este celular';
  forget.addEventListener('click', function () { forgetThisPhone(forget); });
  wrap.appendChild(forget);
  return wrap;
}

async function repeatLastOrder(btn) {
  if (!state.token) return;
  btn.disabled = true;
  try {
    var data = await DinerSession.fetch('/api/diner/memory/repeat', 'POST', {}, getToken());
    applyCartResult(data);
    var bubble = createBotBubble();
    bubble.appendChild(createTextNode((data && data.message) || 'Agregamos tu último pedido.'));
    appendBubble(bubble);
  } catch (e) {
    btn.disabled = false;
    mesioToast((e && e.message) || 'No pudimos repetir tu pedido. Intenta de nuevo.', 'error', 4000);
  }
}

async function forgetThisPhone(btn) {
  var key = DinerSession.getMemoryKey(false);
  btn.disabled = true;
  try {
    if (key && state.token) {
      await DinerSession.fetch('/api/diner/memory/forget', 'POST', { memory_key: key }, getToken());
    }
    DinerSession.forgetMemoryKey();
    state.remembered = false;
    saveMemoryState();
    var bubble = createBotBubble();
    bubble.appendChild(createTextNode('Listo, olvidamos tus pedidos en este celular.'));
    appendBubble(bubble);
  } catch (e) {
    btn.disabled = false;
    mesioToast((e && e.message) || 'No pudimos completar la acción. Intenta de nuevo.', 'error', 4000);
  }
}

function saveMemoryState() {
  var saved = DinerSession.load();
  if (!saved) return;
  saved.orgId = state.orgId;
  saved.remembered = state.remembered;
  DinerSession.save(saved);
}

function maybeOfferMemory(orderResult) {
  if (!orderResult || !orderResult.memory_offer || state.remembered) return;
  if (state.orgId == null || DinerSession.memoryDeclined(state.orgId)) return;
  var bubble = createBotBubble();
  bubble.appendChild(createTextNode(
    '¿Quieres que ' + (state.restaurantName || 'el restaurante') +
    ' recuerde tus pedidos en este celular? La próxima vez, en cualquiera de sus sedes, ' +
    'te saludamos con lo de siempre.'
  ));
  var row = document.createElement('div');
  row.className = 'diner-chip-row';
  var yes = document.createElement('button');
  yes.type = 'button';
  yes.className = 'diner-chip';
  yes.textContent = 'Sí, recuérdame';
  var no = document.createElement('button');
  no.type = 'button';
  no.className = 'diner-chip diner-chip--quiet';
  no.textContent = 'No, gracias';
  row.appendChild(yes);
  row.appendChild(no);
  bubble.appendChild(row);
  var fine = document.createElement('p');
  fine.className = 'diner-memory-note';
  fine.textContent = 'Solo guardamos lo que pides aquí. Puedes pedir que lo olvidemos cuando quieras.';
  bubble.appendChild(fine);
  appendBubble(bubble);

  function done(text) {
    row.hidden = true;
    bubble.insertBefore(createTextNode(text), fine);
  }
  yes.addEventListener('click', async function () {
    var key = DinerSession.getMemoryKey(true);
    if (!key) {
      done('Este navegador no nos deja guardar datos, así que no podemos recordarte aquí.');
      return;
    }
    yes.disabled = true;
    no.disabled = true;
    try {
      var data = await DinerSession.fetch('/api/diner/memory/consent', 'POST', { memory_key: key }, getToken());
      state.remembered = true;
      saveMemoryState();
      done((data && data.message) || 'Listo, te recordaremos.');
    } catch (e) {
      yes.disabled = false;
      no.disabled = false;
      mesioToast((e && e.message) || 'No pudimos guardar tu preferencia. Intenta de nuevo.', 'error', 4000);
    }
  });
  no.addEventListener('click', function () {
    DinerSession.setMemoryDeclined(state.orgId);
    done('Entendido, no guardamos nada.');
  });
}

function renderBlock(block) {
  if (!block || typeof block.type !== 'string') return null;
  switch (block.type) {
    case 'text': return renderTextBlock(block);
    case 'dish_cards': return renderDishCardsBlock(block);
    case 'category_chips': return renderCategoryChipsBlock(block);
    case 'cart_summary': return renderCartSummaryBlock(block);
    case 'payment_options': return renderPaymentOptionsBlock(block);
    case 'waiter_ack': return renderWaiterAckBlock(block);
    case 'nps_prompt': return renderNpsPromptBlock(block);
    case 'memory_actions': return renderMemoryActionsBlock(block);
    default: return null; // forward-compat: unrecognised block types are skipped, never crash
  }
}

function processBotTurn(turn) {
  turn = turn || {};
  var blocks = Array.isArray(turn.blocks) ? turn.blocks : [];
  var bubble = createBotBubble();
  var renderedAny = false;

  // `message` is the bot's spoken line for this turn and `blocks` are the
  // rich UI that goes ALONGSIDE it (see app/services/blocks.py docstring) —
  // NOT a fallback pair where only one half ever shows. A turn like the QR
  // greeting ("¡Hola!... Esto es lo que tenemos hoy:" + category_chips) or
  // a category browse ("Esto es lo que tenemos en Pastas:" + dish_cards)
  // needs both rendered, in reading order, or the diner sees chips/cards
  // with no idea what they're chips/cards OF.
  var messageText = (turn.message != null && turn.message !== '') ? String(turn.message) : '';
  if (messageText) {
    bubble.appendChild(createTextNode(messageText));
    renderedAny = true;
  }

  blocks.forEach(function (block) {
    if (block && block.type === 'cart_summary') {
      state.cart = block;
      updateCartChip();
      CartPanel.refresh();
    }
    if (block && block.type === 'nps_prompt') {
      // Keep the status-poll's dedup in sync so the NEXT poll tick doesn't
      // re-render a second copy of the same stage this turn already showed
      // (see pollDinerStatus — it only appends on a STAGE CHANGE).
      lastNpsStage = block.stage || 'score';
    }
    var node = renderBlock(block);
    if (node) {
      bubble.appendChild(node);
      renderedAny = true;
    }
  });

  if (!renderedAny) {
    bubble.appendChild(createTextNode('Estoy aquí para ayudarte. ¿Qué se te antoja hoy?'));
  }

  appendBubble(bubble);
}

/* ── Cart tap-mutations (deterministic — never sendMessage/LLM) ───────
 * A tap on +/-/remove/note carries a known dish + qty + note already; there
 * is nothing for the bot to interpret, so these call the /api/diner/cart/*
 * endpoints directly. Every response carries the SAME shape:
 * {message, blocks:[cart_summary]} — see app/routes/diner.py.
 * ════════════════════════════════════════════════════════════════════ */

function applyCartResult(data) {
  var blocksArr = (data && Array.isArray(data.blocks)) ? data.blocks : [];
  var cartBlock = null;
  blocksArr.forEach(function (block) {
    if (block && block.type === 'cart_summary') cartBlock = block;
  });
  if (cartBlock) {
    state.cart = cartBlock;
    updateCartChip();
    CartPanel.refresh();
  }
  return cartBlock;
}

async function cartApiCall(path, body) {
  return DinerSession.fetch(path, 'POST', body, getToken());
}

async function cartAdd(dish, qty, note) {
  var payload = { qty: qty };
  if (dish && dish.sku) payload.sku = dish.sku;
  if (dish && dish.name) payload.name = dish.name;
  if (note) payload.note = note;
  return cartApiCall('/api/diner/cart/add', payload);
}

async function cartUpdate(lineId, changes) {
  var payload = Object.assign({ line_id: lineId }, changes || {});
  return cartApiCall('/api/diner/cart/update', payload);
}

async function cartRemove(lineId) {
  return cartApiCall('/api/diner/cart/remove', { line_id: lineId });
}

async function cartLoad() {
  return DinerSession.fetch('/api/diner/cart', 'GET', null, getToken());
}

/* ── Sending messages ─────────────────────────────────────────────── */

async function sendMessage(text, opts) {
  opts = opts || {};
  var trimmed = (text || '').trim();
  if (!trimmed || state.busy || !state.token) return;

  appendBubble(createDinerBubble(opts.displayText != null ? opts.displayText : trimmed));
  setBusy(true);
  showTyping();

  try {
    var data = await DinerSession.fetch('/api/diner/chat', 'POST', { message: trimmed }, state.token);
    hideTyping();
    processBotTurn(data);
  } catch (e) {
    hideTyping();
    appendBubble(createTextNode('No pudimos enviar tu mensaje. Intenta de nuevo.'));
    mesioToast('Error de conexión', 'error', 3000);
  } finally {
    setBusy(false);
    var input = dinerEl('diner-input');
    if (input) input.focus();
  }
}

/* ── Session bootstrap ────────────────────────────────────────────── */

function renderHeader() {
  var nameEl = dinerEl('diner-restaurant-name');
  var tableEl = dinerEl('diner-table-label');
  var codeEl = dinerEl('diner-join-code-chip');
  if (nameEl) nameEl.textContent = state.restaurantName || 'Mesio';
  if (tableEl) {
    if (isDeliveryOrderMode()) {
      var modeLabel = state.orderMode === 'delivery' ? 'Domicilio' : 'Recoger en tienda';
      tableEl.textContent = state.sedeName ? (modeLabel + ' · ' + state.sedeName) : modeLabel;
    } else {
      // Table names are plain numbers now ("3"); custom names stay as-is.
      var label = String(state.tableLabel || '');
      tableEl.textContent = /^\d+$/.test(label) ? ('Mesa ' + label) : label;
    }
  }
  if (codeEl) {
    if (state.joinCode) {
      codeEl.textContent = 'Código para invitar: ' + state.joinCode;
      codeEl.hidden = false;
    } else {
      codeEl.hidden = true;
    }
  }
}

function showErrorBanner(msg) {
  var banner = dinerEl('diner-error-banner');
  var textEl = dinerEl('diner-error-text');
  if (!banner || !textEl) return;
  if (!msg) {
    banner.hidden = true;
    return;
  }
  textEl.textContent = msg;
  banner.hidden = false;
}

function showJoinBanner(show) {
  var banner = dinerEl('diner-join-banner');
  if (banner) banner.hidden = !show;
}

/* Occupied table (Gap 1): a second diner must supply the join_code shown on
 * the first diner's screen before the greeting/carta ever appears. Entered
 * this state either straight out of startSession() (fresh scan) or out of
 * restoreSavedSession() (reload before ever joining). */
function enterJoinRequiredState() {
  state.joined = false;
  setBusy(false);
  renderHeader();
  showJoinBanner(true);
  JoinSheet.open();
}

function onJoinedSuccessfully(turn) {
  state.joined = true;
  state.joinCode = '';
  applyAssistant(turn && turn.assistant);
  var saved = DinerSession.load() || {};
  saved.needsJoin = false;
  saved.assistant = state.assistant;
  DinerSession.save(saved);
  showJoinBanner(false);
  setBusy(false);
  renderHeader();
  processBotTurn(turn);
}

async function restoreSavedSession(tableId) {
  // A page reload must not lose the diner's cart: reuse the SAME session
  // token (same `phone` slot everywhere — carts/conversations/NPS/waiter
  // alerts all key off it) instead of minting a fresh one, as long as the
  // saved session was for THIS same table (a different QR scan in the same
  // tab starts fresh — see dinerGetTableToken()'s query/path resolution).
  var saved = DinerSession.load();
  if (!saved || !saved.token || saved.tableId !== tableId) return false;

  state.token = saved.token;
  state.restaurantName = saved.restaurantName || '';
  state.tableLabel = saved.tableLabel || '';
  state.currency = saved.currency || 'COP';
  state.locale = saved.locale || 'es-CO';
  state.joinCode = saved.joinCode || '';
  state.orgId = saved.orgId != null ? saved.orgId : null;
  state.remembered = !!saved.remembered;
  applyAssistant(saved.assistant);
  connectDinerRealtime();

  if (saved.needsJoin) {
    // Reloaded before ever entering the code — ask again. Never calls
    // POST /api/diner/session again (that would mint a NEW token and could
    // never re-detect "still occupied, still waiting" for THIS diner).
    enterJoinRequiredState();
    return true;
  }

  try {
    var cartData = await cartLoad();
    applyCartResult(cartData);
  } catch (e) {
    // Saved token is gone/expired server-side (e.g. old session pruned) —
    // fall through to minting a brand new session.
    DinerSession.clear();
    state.token = null;
    return false;
  }

  state.joined = true;
  renderHeader();
  renderWelcomeBack();
  return true;
}

/**
 * Welcome-back turn for a RESTORED session (page reload, or coming back to
 * the tab later). Restoring brings back the token and the cart, but the chat
 * history is not persisted, so without this the diner landed on an empty
 * chat with nothing but the text box — no categories, no way into the menu.
 * On /pedir it also links the last order's status page: the code was saved
 * to localStorage at checkout precisely so a returning customer can find it.
 * Best-effort: if the menu can't be fetched, the plain greeting still shows.
 */
async function renderWelcomeBack() {
  var chips = [];
  try {
    var menu = await DinerSession.fetch('/api/diner/menu', 'GET', null, getToken());
    var cats = (menu && Array.isArray(menu.categories)) ? menu.categories : [];
    cats.forEach(function (c) {
      if (c && c.name) chips.push({ label: String(c.name), value: 'cat:' + c.name });
    });
  } catch (e) {
    // Non-fatal: the greeting below still renders without chips.
  }
  var name = state.restaurantName ? (' a ' + state.restaurantName) : '';
  processBotTurn({
    message: '¡Hola de nuevo! Bienvenido otra vez' + name + '. ¿Qué se te antoja?',
    blocks: chips.length ? [{ type: 'category_chips', chips: chips }] : [],
  });

  if (state.orderMode === 'delivery' || state.orderMode === 'pickup') {
    var code = DinerSession.loadLastOrderCode();
    if (code) {
      var bubble = createBotBubble();
      bubble.appendChild(createTextNode('Tu último pedido: ' + code));
      var link = document.createElement('a');
      link.className = 'm-btn m-btn--ghost';
      link.href = '/pedido/' + encodeURIComponent(code);
      link.textContent = 'Ver estado de mi pedido';
      bubble.appendChild(link);
      appendBubble(bubble);
    }
  }
}

/**
 * Entry dispatcher — the ONE thing that tells /chat/{table_id} and
 * /pedir/{slug} apart (docs/claude/delivery-web.md chunk 5: "Parameterize
 * it (a mode flag, a different entry bootstrap) so one chat serves both").
 * Everything downstream of this (blocks rendering, cart, dish cards,
 * composer) is 100% shared, unchanged code.
 */
function startSession() {
  var entry = DinerSession.getEntryMode();
  if (entry.mode === 'pedir') {
    return startDeliveryEntry(entry.slug);
  }
  return startDineInSession(entry.tableId);
}

async function startDineInSession(tableId) {
  setBusy(true);
  showTyping();
  showErrorBanner(null);

  if (!tableId) {
    hideTyping();
    setBusy(false);
    showErrorBanner('No pudimos identificar tu mesa. Escanea el código QR de nuevo.');
    return;
  }

  if (await restoreSavedSession(tableId)) {
    hideTyping();
    setBusy(false);
    return;
  }

  try {
    // POST /api/diner/session body shape is {table_id} — see
    // app/routes/diner.py::DinerSessionRequest. No token exists yet at
    // this point (minting one is the whole point of this call), so the
    // 4th arg to DinerSession.fetch is null.
    // A phone that already sat at this table (browser closed, new tab) asks
    // for its seat back; the server decides whether it is still valid.
    var seat = DinerSession.loadSeat(tableId);
    var sessionBody = { table_id: tableId };
    if (seat) sessionBody.resume_token = seat.token;
    var memoryKey = DinerSession.getMemoryKey(false);
    if (memoryKey) sessionBody.memory_key = memoryKey;
    var data = await DinerSession.fetch('/api/diner/session', 'POST', sessionBody, null);
    hideTyping();
    state.token = data.token || '';
    state.restaurantName = data.restaurant_name || '';
    state.tableLabel = data.table_name || '';
    state.currency = data.currency || 'COP';
    state.locale = data.locale || 'es-CO';
    state.orgId = data.org_id != null ? data.org_id : null;
    state.remembered = !!data.remembered;
    applyAssistant(data.assistant);
    if (!state.token) throw new Error('missing session token');
    connectDinerRealtime();

    if (data.resumed) {
      // Same diner, same table: no code, no greeting from scratch — their
      // cart is still there under this token.
      state.joinCode = data.join_code || '';
      state.joined = true;
      DinerSession.save({
        token: state.token,
        tableId: tableId,
        orgId: state.orgId,
        remembered: state.remembered,
        restaurantName: state.restaurantName,
        tableLabel: state.tableLabel,
        currency: state.currency,
        locale: state.locale,
        assistant: state.assistant,
        joinCode: state.joinCode,
        needsJoin: false,
      });
      try { applyCartResult(await cartLoad()); } catch (e) { /* empty cart is fine */ }
      renderHeader();
      setBusy(false);
      renderWelcomeBack();
      return;
    }

    if (data.requires_join_code) {
      // Table occupied — never show the greeting/carta (PM decision). Save
      // the token now (needed by POST /api/diner/join) with needsJoin=true
      // so a reload before joining re-asks instead of losing the token.
      DinerSession.save({
        token: state.token,
        tableId: tableId,
        orgId: state.orgId,
        remembered: state.remembered,
        restaurantName: state.restaurantName,
        tableLabel: state.tableLabel,
        currency: state.currency,
        locale: state.locale,
        assistant: state.assistant,
        needsJoin: true,
      });
      setBusy(false);
      enterJoinRequiredState();
      return;
    }

    state.joinCode = data.join_code || '';
    state.joined = true;
    DinerSession.save({
      token: state.token,
      tableId: tableId,
      orgId: state.orgId,
      remembered: state.remembered,
      restaurantName: state.restaurantName,
      tableLabel: state.tableLabel,
      currency: state.currency,
      locale: state.locale,
      assistant: state.assistant,
      joinCode: state.joinCode,
      needsJoin: false,
    });
    renderHeader();
    setBusy(false);
    processBotTurn(data);
  } catch (e) {
    hideTyping();
    setBusy(false);
    showErrorBanner('No pudimos conectar con el restaurante. Puedes llamar al mesero mientras tanto.');
  }
}

/* ── Delivery/pickup entry bootstrap (docs/claude/delivery-web.md chunk 5)
 * /pedir/{slug} — GPS + GET /api/diner/org/{slug} + POST
 * /api/diner/order-mode/resolve, THEN the exact same POST /api/diner/session
 * the dine-in path uses (order_mode=delivery|pickup instead of dine_in —
 * already wired server-side, see app/routes/diner.py
 * _create_delivery_pickup_session). Everything after openDeliverySession()
 * hands off into processBotTurn() — the SAME shared chat rendering the
 * dine-in path uses, no fork.
 * ════════════════════════════════════════════════════════════════════ */

var TurnstileHelper = (function () {
  var scriptPromise = null;

  function ensureScript() {
    if (scriptPromise) return scriptPromise;
    scriptPromise = new Promise(function (resolve, reject) {
      if (window.turnstile) { resolve(); return; }
      var s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
      s.async = true;
      s.defer = true;
      s.addEventListener('load', function () { resolve(); });
      s.addEventListener('error', function () { reject(new Error('turnstile_script_failed')); });
      document.head.appendChild(s);
    });
    return scriptPromise;
  }

  // Renders a widget into `container` and calls onToken(token) whenever
  // Turnstile issues/expires one. Never throws — a script-load failure
  // just leaves the action gated by "site key present, no token yet",
  // which every caller below already treats as "not ready", not a crash.
  function renderInto(container, siteKey, onToken) {
    if (!container || !siteKey) return;
    container.textContent = '';
    ensureScript().then(function () {
      if (!window.turnstile) return;
      window.turnstile.render(container, {
        sitekey: siteKey,
        callback: onToken,
        'expired-callback': function () { onToken(null); },
        'error-callback': function () { onToken(null); },
      });
    }).catch(function () { /* no-op — see docstring above */ });
  }

  return { renderInto: renderInto };
})();

var PedirEntry = (function () {
  var REASON_TEXT = {
    out_of_coverage: 'Tu ubicación quedó fuera de la zona de cobertura de domicilios. Puedes recoger tu pedido en una de estas sedes:',
    all_closed: 'Nuestras sedes de domicilio están cerradas en este momento. Puedes recoger en una de estas sedes:',
    delivery_disabled: 'Este restaurante no ofrece domicilios en este momento. Elige una sede para recoger tu pedido:',
    no_gps: 'No pudimos acceder a tu ubicación. Elige la sede donde quieres recoger tu pedido:',
  };

  function clear(container) { if (container) container.textContent = ''; }

  function renderLoading(container, text) {
    clear(container);
    var p = document.createElement('p');
    p.className = 'pedir-entry-status';
    p.textContent = text || 'Cargando...';
    container.appendChild(p);
  }

  function renderError(container, text, retry) {
    clear(container);
    var p = document.createElement('p');
    p.className = 'pedir-entry-status pedir-entry-status--error';
    p.textContent = text;
    container.appendChild(p);
    if (retry) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'm-btn m-btn--primary m-btn--sm';
      btn.textContent = 'Reintentar';
      btn.addEventListener('click', retry);
      container.appendChild(btn);
    }
  }

  function buildTurnstileBox(container) {
    var box = document.createElement('div');
    box.className = 'pedir-turnstile';
    container.appendChild(box);
    if (state.turnstileSiteKey) {
      TurnstileHelper.renderInto(box, state.turnstileSiteKey, function (token) {
        state.turnstileSessionToken = token;
      });
    }
  }

  function guardTurnstileThen(fn) {
    if (state.turnstileSiteKey && !state.turnstileSessionToken) {
      mesioToast('Completa la verificación de seguridad para continuar.', 'error', 3500);
      return;
    }
    fn();
  }

  function buildSedeCard(candidate, onSelect) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'pedir-sede-card';

    var name = document.createElement('p');
    name.className = 'pedir-sede-name';
    name.textContent = candidate.name || 'Sede';
    card.appendChild(name);

    if (candidate.address) {
      var addr = document.createElement('p');
      addr.className = 'pedir-sede-address';
      addr.textContent = candidate.address;
      card.appendChild(addr);
    }

    var meta = document.createElement('p');
    meta.className = 'pedir-sede-meta';
    var bits = [];
    if (typeof candidate.distance_km === 'number') bits.push(candidate.distance_km.toFixed(1) + ' km');
    bits.push(candidate.open_now ? 'Abierto ahora' : 'Cerrado ahora');
    meta.textContent = bits.join(' · ');
    card.appendChild(meta);

    card.addEventListener('click', function () { onSelect(candidate); });
    return card;
  }

  function renderAssigned(container, slug, result) {
    clear(container);

    var card = document.createElement('div');
    card.className = 'pedir-assigned-card';
    var intro = document.createElement('p');
    intro.className = 'pedir-entry-status';
    intro.textContent = 'Te atenderá nuestra sede:';
    card.appendChild(intro);
    var name = document.createElement('p');
    name.className = 'pedir-sede-name';
    name.textContent = result.location.name || 'Sede';
    card.appendChild(name);
    if (result.location.address) {
      var addr = document.createElement('p');
      addr.className = 'pedir-sede-address';
      addr.textContent = result.location.address;
      card.appendChild(addr);
    }
    container.appendChild(card);
    buildTurnstileBox(container);

    var continueBtn = document.createElement('button');
    continueBtn.type = 'button';
    continueBtn.className = 'm-btn m-btn--primary pedir-entry-btn';
    continueBtn.textContent = 'Continuar con domicilio';
    continueBtn.addEventListener('click', function () {
      guardTurnstileThen(function () { openDeliverySession(slug, result.location.location_id, 'delivery'); });
    });
    container.appendChild(continueBtn);

    var pickupBtn = document.createElement('button');
    pickupBtn.type = 'button';
    pickupBtn.className = 'm-btn m-btn--ghost pedir-entry-btn';
    pickupBtn.textContent = 'Prefiero recoger en tienda';
    pickupBtn.addEventListener('click', function () { switchToPickup(container, slug); });
    container.appendChild(pickupBtn);
  }

  function renderPickupList(container, slug, result) {
    clear(container);

    var intro = document.createElement('p');
    intro.className = 'pedir-entry-status';
    intro.textContent = (result.reason && REASON_TEXT[result.reason])
      || 'Elige la sede donde quieres recoger tu pedido:';
    container.appendChild(intro);

    var candidates = Array.isArray(result.candidates) ? result.candidates : [];
    if (!candidates.length) {
      renderError(container, 'Este restaurante no tiene sedes disponibles para recoger en este momento.', null);
      return;
    }

    var list = document.createElement('div');
    list.className = 'pedir-sede-list';
    candidates.forEach(function (candidate) {
      list.appendChild(buildSedeCard(candidate, function (c) {
        guardTurnstileThen(function () { openDeliverySession(slug, c.location_id, 'pickup'); });
      }));
    });
    container.appendChild(list);
    buildTurnstileBox(container);
  }

  async function switchToPickup(container, slug) {
    renderLoading(container, 'Cargando sedes...');
    try {
      var reqBody = { slug: slug, mode: 'pickup', device_token: DinerSession.getDeviceToken() };
      // Carry over the GPS fix from the delivery attempt (if any) so the
      // pickup list still sorts nearest-first instead of losing distances
      // just because the customer switched modes.
      if (state.lastPos) { reqBody.lat = state.lastPos.lat; reqBody.lon = state.lastPos.lon; }
      var result = await DinerSession.fetch('/api/diner/order-mode/resolve', 'POST', reqBody, null);
      renderPickupList(container, slug, result);
    } catch (e) {
      renderError(container, 'No pudimos cargar las sedes. Intenta de nuevo.', function () { switchToPickup(container, slug); });
    }
  }

  async function resolveAndRender(container, slug) {
    renderLoading(container, 'Obteniendo tu ubicación...');
    var geo = await DinerSession.getGeolocation(8000);
    var deviceToken = DinerSession.getDeviceToken();
    var result;
    if (geo.ok) {
      state.lastPos = { lat: geo.lat, lon: geo.lon };
      try {
        result = await DinerSession.fetch('/api/diner/order-mode/resolve', 'POST', {
          slug: slug, mode: 'delivery', lat: geo.lat, lon: geo.lon, device_token: deviceToken,
        }, null);
      } catch (e) {
        renderError(container, 'No pudimos calcular la cobertura de domicilios. Intenta de nuevo.',
          function () { resolveAndRender(container, slug); });
        return;
      }
    } else {
      // Denied / timed out / unsupported (bot-rules.md #11 style exhaustive
      // handling — every geolocation outcome lands somewhere, never a hang).
      // Still ask for DELIVERY, just without coordinates: the server's ladder
      // owns rung 4 ("GPS denied or unavailable -> pickup only") and answers
      // with reason `no_gps`, so the customer is told WHY it is pickup-only.
      // Asking for mode 'pickup' here made the server treat it as the
      // customer's own choice (reason null) and the explanation never showed.
      state.lastPos = null;
      try {
        result = await DinerSession.fetch('/api/diner/order-mode/resolve', 'POST', {
          slug: slug, mode: 'delivery', device_token: deviceToken,
        }, null);
      } catch (e) {
        renderError(container, 'No pudimos cargar las sedes. Intenta de nuevo.',
          function () { resolveAndRender(container, slug); });
        return;
      }
    }

    if (result.mode === 'delivery') {
      renderAssigned(container, slug, result);
    } else {
      renderPickupList(container, slug, result);
    }
  }

  return { renderLoading: renderLoading, renderError: renderError, resolveAndRender: resolveAndRender };
})();

async function startDeliveryEntry(slug) {
  state.slug = slug;
  document.body.classList.add('pedir-mode', 'pedir-entry-open');
  var entryWrap = dinerEl('pedir-entry');
  if (entryWrap) entryWrap.hidden = false;
  var entryBody = dinerEl('pedir-entry-body');

  // Reload continuity — same slug, already-open session -> skip straight
  // back into the chat instead of re-running GPS/resolve (mirrors
  // restoreSavedSession()'s dine-in equivalent above).
  var saved = DinerSession.load();
  if (saved && saved.token && saved.entryMode === 'pedir' && saved.slug === slug) {
    state.token = saved.token;
    state.restaurantName = saved.restaurantName || '';
    state.sedeName = saved.sedeName || '';
    state.sedePhone = saved.sedePhone || '';
    state.currency = saved.currency || 'COP';
    state.locale = saved.locale || 'es-CO';
    state.orderMode = saved.orderMode || 'delivery';
    applyAssistant(saved.assistant);
    state.deliveryConfig = saved.deliveryConfig || { payment_methods: [], delivery_fee: 0, min_order: 0 };
    state.lastPos = saved.lastPos || null;
    connectDinerRealtime();
    try {
      var cartData = await cartLoad();
      applyCartResult(cartData);
      finishDeliveryEntry();
      renderWelcomeBack();
      return;
    } catch (e) {
      DinerSession.clear();
      state.token = null;
    }
  }

  setBusy(true);
  PedirEntry.renderLoading(entryBody, 'Cargando...');

  var orgInfo;
  try {
    orgInfo = await DinerSession.fetch('/api/diner/org/' + encodeURIComponent(slug), 'GET', null, null);
  } catch (e) {
    setBusy(false);
    PedirEntry.renderError(entryBody, 'No pudimos cargar este restaurante. Verifica el enlace e intenta de nuevo.',
      function () { startDeliveryEntry(slug); });
    return;
  }

  state.restaurantName = orgInfo.name || 'Mesio';
  state.currency = orgInfo.currency || 'COP';
  state.turnstileSiteKey = orgInfo.turnstile_site_key || null;
  var pedirNameEl = dinerEl('pedir-restaurant-name');
  if (pedirNameEl) pedirNameEl.textContent = state.restaurantName;
  var headerNameEl = dinerEl('diner-restaurant-name');
  if (headerNameEl) headerNameEl.textContent = state.restaurantName;

  if (!orgInfo.delivery_enabled && !orgInfo.pickup_enabled) {
    setBusy(false);
    PedirEntry.renderError(entryBody, 'Este restaurante no tiene domicilios ni recogida disponibles en este momento.', null);
    return;
  }

  setBusy(false);
  await PedirEntry.resolveAndRender(entryBody, slug);
}

var _openingDeliverySession = false;

async function openDeliverySession(slug, locationId, orderMode) {
  // Guards a double-tap on "Continuar"/a sede card while the request is
  // in flight from minting two sessions (setBusy() alone only locks the
  // composer, not these entry-screen buttons).
  if (_openingDeliverySession) return;
  _openingDeliverySession = true;
  setBusy(true);
  try {
    var body = { order_mode: orderMode, slug: slug, location_id: locationId };
    if (state.turnstileSessionToken) body.turnstile_token = state.turnstileSessionToken;
    var deliveryMemoryKey = DinerSession.getMemoryKey(false);
    if (deliveryMemoryKey) body.memory_key = deliveryMemoryKey;
    var data = await DinerSession.fetch('/api/diner/session', 'POST', body, null);
    state.token = data.token || '';
    state.orgId = data.org_id != null ? data.org_id : null;
    state.remembered = !!data.remembered;
    if (!state.token) throw new Error('missing session token');
    state.orderMode = data.order_mode || orderMode;
    state.restaurantName = data.restaurant_name || state.restaurantName;
    state.sedeName = data.sede_name || '';
    state.sedePhone = data.sede_phone || '';
    state.currency = data.currency || state.currency;
    state.locale = data.locale || 'es-CO';
    applyAssistant(data.assistant);
    state.deliveryConfig = {
      payment_methods: Array.isArray(data.payment_methods) ? data.payment_methods : [],
      delivery_fee: Number(data.delivery_fee) || 0,
      min_order: Number(data.min_order) || 0,
    };
    connectDinerRealtime();
    DinerSession.save({
      token: state.token,
      entryMode: 'pedir',
      orgId: state.orgId,
      remembered: state.remembered,
      slug: slug,
      orderMode: state.orderMode,
      restaurantName: state.restaurantName,
      sedeName: state.sedeName,
      sedePhone: state.sedePhone,
      currency: state.currency,
      locale: state.locale,
      assistant: state.assistant,
      deliveryConfig: state.deliveryConfig,
      // The checkout re-validates coverage on this pin server-side; without
      // it a reloaded delivery session could never be checked out.
      lastPos: state.lastPos || null,
    });
    finishDeliveryEntry();
    processBotTurn(data);
  } catch (e) {
    mesioToast((e && e.message) || 'No pudimos abrir tu pedido. Intenta de nuevo.', 'error', 4500);
    setBusy(false);
  }
}

function finishDeliveryEntry() {
  document.body.classList.remove('pedir-entry-open');
  var entryWrap = dinerEl('pedir-entry');
  if (entryWrap) entryWrap.hidden = true;
  state.joined = true;
  renderHeader();
  setBusy(false);
}

/* ── Waiter call ──────────────────────────────────────────────────── */

var WaiterSheet = (function () {
  var overlay = null;
  var box = null;
  var optionsWrap = null;
  var trap = null;

  function ensureBuilt() {
    if (overlay) return;

    overlay = document.createElement('div');
    overlay.className = 'diner-overlay';

    box = document.createElement('div');
    box.className = 'diner-sheet';

    var title = document.createElement('h2');
    title.id = 'waiter-sheet-title';
    title.textContent = '¿En qué te ayudamos?';
    box.appendChild(title);

    optionsWrap = document.createElement('div');
    optionsWrap.className = 'diner-sheet-options';
    WAITER_REASONS.forEach(function (reason) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'diner-waiter-option';
      var icon = document.createElement('span');
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = reason.icon;
      btn.appendChild(icon);
      btn.appendChild(document.createTextNode(' ' + reason.label));
      btn.addEventListener('click', function () { callWaiter(reason); });
      optionsWrap.appendChild(btn);
    });
    box.appendChild(optionsWrap);

    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'm-btn m-btn--ghost diner-sheet-cancel';
    cancelBtn.textContent = 'Cancelar';
    cancelBtn.addEventListener('click', close);
    box.appendChild(cancelBtn);

    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  function setBusyState(busy) {
    if (!optionsWrap) return;
    Array.prototype.forEach.call(optionsWrap.querySelectorAll('button'), function (b) {
      b.disabled = busy;
    });
  }

  function open() {
    ensureBuilt();
    setBusyState(false);
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'waiter-sheet-title' });
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
  }

  return { open: open, close: close, setBusy: setBusyState };
})();

function appendWaiterAck(text) {
  var bubble = createBotBubble();
  bubble.appendChild(renderWaiterAckBlock({ text: text }));
  appendBubble(bubble);
}

async function callWaiter(reasonObj) {
  // "La cuenta" needs scope/method/tip, not a bare ping — routes to the
  // richer CheckoutSheet (POST /api/diner/checkout) instead of the plain
  // waiter-call endpoint. Every other reason keeps the original direct ping.
  if (reasonObj.value === 'bill') {
    WaiterSheet.close();
    CheckoutSheet.open();
    return;
  }
  WaiterSheet.setBusy(true);
  try {
    var data = await DinerSession.fetch('/api/diner/waiter-call', 'POST', { reason: reasonObj.value }, getToken());
    var ackText = (data && (data.text || data.message))
      ? String(data.text || data.message)
      : ('Ya avisamos al mesero: ' + reasonObj.label + '. Ya va para tu mesa.');
    WaiterSheet.close();
    mesioToast(ackText, 'success', 4000);
    appendWaiterAck(ackText);
  } catch (e) {
    WaiterSheet.setBusy(false);
    mesioToast('No pudimos avisar al mesero. Intenta de nuevo o hazle señas a alguien del equipo.', 'error', 5000);
  }
}

/* ── Join sheet (Gap 1 — second diner enters the host's 4-digit code) ─
 * POST /api/diner/join validates the code against the table's active
 * session. Wrong code re-prompts (server also throttles brute force);
 * right code hands back the SAME opening turn shape a free-table scan
 * gets (message + category_chips), rendered via processBotTurn so the
 * chat starts identically either way.
 * ════════════════════════════════════════════════════════════════════ */

var JoinSheet = (function () {
  var overlay = null;
  var box = null;
  var input = null;
  var errorEl = null;
  var trap = null;

  function ensureBuilt() {
    if (overlay) return;

    overlay = document.createElement('div');
    overlay.className = 'diner-overlay';

    box = document.createElement('div');
    box.className = 'diner-sheet';

    var title = document.createElement('h2');
    title.id = 'join-sheet-title';
    title.textContent = 'Únete a la mesa';
    box.appendChild(title);

    var desc = document.createElement('p');
    desc.className = 'diner-join-desc';
    desc.textContent = 'Esta mesa ya tiene un pedido activo. Pídele el código de 4 dígitos a quien la abrió.';
    box.appendChild(desc);

    input = document.createElement('input');
    input.type = 'tel';
    input.inputMode = 'numeric';
    input.autocomplete = 'one-time-code';
    input.maxLength = 4;
    input.className = 'diner-join-input';
    input.placeholder = '0000';
    input.setAttribute('aria-label', 'Código de 4 dígitos de la mesa');
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); submit(); }
    });
    box.appendChild(input);

    errorEl = document.createElement('p');
    errorEl.className = 'diner-join-error';
    errorEl.setAttribute('role', 'alert');
    errorEl.hidden = true;
    box.appendChild(errorEl);

    var actions = document.createElement('div');
    actions.className = 'diner-sheet-actions';

    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'm-btn m-btn--ghost';
    cancelBtn.textContent = 'Ahora no';
    cancelBtn.addEventListener('click', close);

    var confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'm-btn m-btn--primary';
    confirmBtn.textContent = 'Unirme';
    confirmBtn.addEventListener('click', submit);

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(actions);

    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  function open() {
    ensureBuilt();
    input.value = '';
    errorEl.hidden = true;
    var confirmBtn = box.querySelector('.m-btn--primary');
    if (confirmBtn) confirmBtn.disabled = false;
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'join-sheet-title' });
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
  }

  async function submit() {
    var code = (input.value || '').trim();
    if (!/^\d{4}$/.test(code)) {
      errorEl.textContent = 'Ingresa los 4 dígitos del código.';
      errorEl.hidden = false;
      input.focus();
      return;
    }
    var confirmBtn = box.querySelector('.m-btn--primary');
    if (confirmBtn) confirmBtn.disabled = true;
    errorEl.hidden = true;
    try {
      var data = await DinerSession.fetch('/api/diner/join', 'POST', { code: code }, getToken());
      close();
      onJoinedSuccessfully(data);
    } catch (e) {
      errorEl.textContent = (e && e.message) || 'Código incorrecto. Intenta de nuevo.';
      errorEl.hidden = false;
      input.select();
    } finally {
      if (confirmBtn) confirmBtn.disabled = false;
    }
  }

  return { open: open, close: close };
})();

/* ── Add-to-cart sheet (qty + free-text note) ─────────────────────── */

var AddSheet = (function () {
  var overlay = null;
  var box = null;
  var trap = null;
  var titleEl = null;
  var priceEl = null;
  var qtyEl = null;
  var noteInput = null;
  var currentDish = null;
  var qty = 1;

  function ensureBuilt() {
    if (overlay) return;

    overlay = document.createElement('div');
    overlay.className = 'diner-overlay';

    box = document.createElement('div');
    box.className = 'diner-sheet';

    titleEl = document.createElement('h2');
    titleEl.id = 'add-sheet-title';
    box.appendChild(titleEl);

    priceEl = document.createElement('p');
    priceEl.className = 'diner-sheet-price';
    box.appendChild(priceEl);

    var qtyRow = document.createElement('div');
    qtyRow.className = 'diner-sheet-qty-row';

    var qtyLabel = document.createElement('span');
    qtyLabel.className = 'diner-sheet-qty-label';
    qtyLabel.textContent = 'Cantidad';

    var minus = document.createElement('button');
    minus.type = 'button';
    minus.className = 'diner-cart-qty-btn';
    minus.setAttribute('aria-label', 'Reducir cantidad');
    minus.textContent = '−';
    minus.addEventListener('click', function () { setQty(qty - 1); });

    qtyEl = document.createElement('span');
    qtyEl.className = 'diner-cart-qty-count';
    qtyEl.setAttribute('aria-live', 'polite');

    var plus = document.createElement('button');
    plus.type = 'button';
    plus.className = 'diner-cart-qty-btn';
    plus.setAttribute('aria-label', 'Aumentar cantidad');
    plus.textContent = '+';
    plus.addEventListener('click', function () { setQty(qty + 1); });

    qtyRow.appendChild(qtyLabel);
    qtyRow.appendChild(minus);
    qtyRow.appendChild(qtyEl);
    qtyRow.appendChild(plus);
    box.appendChild(qtyRow);

    var noteLabel = document.createElement('label');
    noteLabel.setAttribute('for', 'diner-add-note');
    noteLabel.className = 'diner-sheet-note-label';
    noteLabel.textContent = 'Nota (opcional)';
    box.appendChild(noteLabel);

    noteInput = document.createElement('textarea');
    noteInput.id = 'diner-add-note';
    noteInput.className = 'diner-sheet-note';
    noteInput.rows = 2;
    noteInput.maxLength = 140;
    noteInput.placeholder = 'Ej: sin cebolla, término medio...';
    box.appendChild(noteInput);

    var actions = document.createElement('div');
    actions.className = 'diner-sheet-actions';

    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'm-btn m-btn--ghost';
    cancelBtn.textContent = 'Cancelar';
    cancelBtn.addEventListener('click', close);

    var confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'm-btn m-btn--primary';
    confirmBtn.textContent = 'Agregar';
    confirmBtn.addEventListener('click', confirmAdd);

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(actions);

    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  function setQty(n) {
    qty = Math.max(1, n);
    qtyEl.textContent = String(qty);
  }

  function open(dish) {
    ensureBuilt();
    currentDish = dish || {};
    setQty(1);
    noteInput.value = '';
    titleEl.textContent = currentDish.name || 'Plato';
    priceEl.textContent = renderer().fmtPrice(currentDish.price, state.locale, state.currency);
    // The sheet's DOM (including the confirm button) is built ONCE and
    // reused across opens (see ensureBuilt()'s `if (overlay) return` guard).
    // Always reset the disabled state here so a re-open after ANY prior
    // outcome (success or error) starts with a clickable button.
    var confirmBtn = box.querySelector('.m-btn--primary');
    if (confirmBtn) confirmBtn.disabled = false;
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'add-sheet-title' });
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
    currentDish = null;
  }

  async function confirmAdd() {
    if (!currentDish || !currentDish.name) { close(); return; }
    var dish = currentDish;
    var addedQty = qty;
    var note = (noteInput.value || '').trim();
    var confirmBtn = box.querySelector('.m-btn--primary');
    if (confirmBtn) confirmBtn.disabled = true;
    try {
      var data = await cartAdd(dish, addedQty, note);
      close();
      // Same {message, blocks:[cart_summary]} shape as a chat turn — render
      // it as a bot bubble (confirmation text + mini cart card) so adding
      // from a dish card feels identical to adding via typed chat.
      processBotTurn(data);
    } catch (e) {
      if (confirmBtn) confirmBtn.disabled = false;
      mesioToast((e && e.message) || 'No pudimos agregar el plato. Intenta de nuevo.', 'error', 4000);
    }
  }

  return { open: open };
})();

/* ── Cart panel (change qty / edit note / remove) ─────────────────── */

var CartPanel = (function () {
  var overlay = null;
  var box = null;
  var body = null;
  var trap = null;

  function ensureBuilt() {
    if (overlay) return;

    overlay = document.createElement('div');
    overlay.className = 'diner-overlay diner-overlay--full';

    box = document.createElement('div');
    box.className = 'diner-panel';

    var header = document.createElement('div');
    header.className = 'diner-panel-header';

    var h2 = document.createElement('h2');
    h2.id = 'cart-panel-title';
    h2.textContent = 'Tu pedido';

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'diner-panel-close';
    closeBtn.setAttribute('aria-label', 'Cerrar');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', close);

    header.appendChild(h2);
    header.appendChild(closeBtn);

    body = document.createElement('div');
    body.className = 'diner-panel-body';

    box.appendChild(header);
    box.appendChild(body);
    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  function render() {
    body.textContent = '';
    var items = (state.cart && Array.isArray(state.cart.items)) ? state.cart.items : [];

    if (!items.length) {
      var empty = document.createElement('p');
      empty.className = 'diner-panel-empty';
      empty.textContent = 'Tu pedido está vacío. Agrega platos desde el chat o la carta completa.';
      body.appendChild(empty);
      return;
    }

    var list = document.createElement('ul');
    list.className = 'diner-cart-list';
    items.forEach(function (item) { list.appendChild(buildCartRow(item)); });
    body.appendChild(list);

    var footer = document.createElement('div');
    footer.className = 'diner-cart-footer';
    var subLabel = document.createElement('span');
    subLabel.textContent = 'Subtotal';
    var subValue = document.createElement('strong');
    subValue.textContent = renderer().fmtPrice(state.cart.subtotal, state.locale, state.cart.currency || state.currency);
    footer.appendChild(subLabel);
    footer.appendChild(subValue);
    body.appendChild(footer);

    var sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'm-btn m-btn--primary diner-cart-send-btn';
    sendBtn.textContent = isDeliveryOrderMode() ? 'Finalizar pedido' : 'Enviar pedido';
    sendBtn.addEventListener('click', function () {
      if (isDeliveryOrderMode()) { DeliveryCheckoutSheet.open(); } else { SendOrderSheet.open(); }
    });
    body.appendChild(sendBtn);
  }

  function buildCartRow(item) {
    var li = document.createElement('li');
    li.className = 'diner-cart-row';

    var info = document.createElement('div');
    info.className = 'diner-cart-row-info';

    var name = document.createElement('p');
    name.className = 'diner-cart-row-name';
    name.textContent = item.name || '';
    info.appendChild(name);

    if (item.note) {
      var note = document.createElement('p');
      note.className = 'diner-cart-row-note';
      note.textContent = 'Nota: ' + item.note;
      info.appendChild(note);
    }

    var price = document.createElement('p');
    price.className = 'diner-cart-row-price';
    price.textContent = renderer().fmtPrice(item.subtotal, state.locale, state.cart.currency || state.currency);
    info.appendChild(price);

    var qtyControls = document.createElement('div');
    qtyControls.className = 'diner-cart-row-qty';

    var minusBtn = document.createElement('button');
    minusBtn.type = 'button';
    minusBtn.className = 'diner-cart-qty-btn';
    minusBtn.setAttribute('aria-label', 'Reducir cantidad de ' + (item.name || ''));
    minusBtn.textContent = '−';
    minusBtn.addEventListener('click', function () { changeQty(item, (Number(item.qty) || 1) - 1); });

    var qtyEl = document.createElement('span');
    qtyEl.className = 'diner-cart-qty-count';
    qtyEl.textContent = String(item.qty != null ? item.qty : 1);

    var plusBtn = document.createElement('button');
    plusBtn.type = 'button';
    plusBtn.className = 'diner-cart-qty-btn';
    plusBtn.setAttribute('aria-label', 'Aumentar cantidad de ' + (item.name || ''));
    plusBtn.textContent = '+';
    plusBtn.addEventListener('click', function () { changeQty(item, (Number(item.qty) || 1) + 1); });

    qtyControls.appendChild(minusBtn);
    qtyControls.appendChild(qtyEl);
    qtyControls.appendChild(plusBtn);

    var noteBtn = document.createElement('button');
    noteBtn.type = 'button';
    noteBtn.className = 'm-btn m-btn--ghost m-btn--sm';
    noteBtn.textContent = 'Nota';
    noteBtn.addEventListener('click', function () { editNote(item); });

    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'm-btn m-btn--ghost m-btn--sm diner-cart-remove-btn';
    removeBtn.textContent = 'Quitar';
    removeBtn.addEventListener('click', function () { removeItem(item); });

    var actionsRow = document.createElement('div');
    actionsRow.className = 'diner-cart-row-actions';
    actionsRow.appendChild(qtyControls);
    actionsRow.appendChild(noteBtn);
    actionsRow.appendChild(removeBtn);

    li.appendChild(info);
    li.appendChild(actionsRow);
    return li;
  }

  async function changeQty(item, newQty) {
    if (newQty < 0 || !item.line_id) return;
    try {
      await cartUpdate(item.line_id, { qty: newQty }).then(applyCartResult);
    } catch (e) {
      mesioToast((e && e.message) || 'No pudimos actualizar la cantidad.', 'error', 3500);
    }
  }

  async function removeItem(item) {
    if (!item.line_id) return;
    var ok = await mesioConfirm('¿Quitar "' + item.name + '" del pedido?', { confirmText: 'Quitar', danger: true });
    if (!ok) return;
    try {
      await cartRemove(item.line_id).then(applyCartResult);
    } catch (e) {
      mesioToast((e && e.message) || 'No pudimos quitar el producto.', 'error', 3500);
    }
  }

  async function editNote(item) {
    if (!item.line_id) return;
    var val = await mesioPrompt('Nota para ' + item.name, {
      title: 'Editar nota',
      defaultValue: item.note || '',
      placeholder: 'Ej: sin cebolla, término medio...',
      confirmLabel: 'Guardar',
    });
    if (val === null) return;
    try {
      await cartUpdate(item.line_id, { note: val.trim() }).then(applyCartResult);
    } catch (e) {
      mesioToast((e && e.message) || 'No pudimos guardar la nota.', 'error', 3500);
    }
  }

  function open() {
    ensureBuilt();
    render();
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'cart-panel-title' });
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
  }

  function refresh() {
    if (overlay && overlay.classList.contains('open')) render();
  }

  return { open: open, close: close, refresh: refresh };
})();

function updateCartChip() {
  refreshCartCards();
  var chip = dinerEl('cart-chip');
  var label = dinerEl('cart-chip-label');
  if (!chip || !label) return;
  var items = (state.cart && Array.isArray(state.cart.items)) ? state.cart.items : [];
  var count = items.reduce(function (a, it) { return a + (Number(it.qty) || 0); }, 0);
  if (count > 0) {
    label.textContent = count + ' · ' + renderer().fmtPrice(state.cart.subtotal, state.locale, state.cart.currency || state.currency);
    chip.hidden = false;
  } else {
    chip.hidden = true;
  }
}

/* ── Full carta panel — the "escape hatch" for a 60-dish carta ────── */

var MenuPanel = (function () {
  var overlay = null;
  var box = null;
  var body = null;
  var trap = null;
  var menu = null;
  var loaded = false;

  function ensureBuilt() {
    if (overlay) return;

    overlay = document.createElement('div');
    overlay.className = 'diner-overlay diner-overlay--full';

    box = document.createElement('div');
    box.className = 'diner-panel';

    var header = document.createElement('div');
    header.className = 'diner-panel-header';

    var h2 = document.createElement('h2');
    h2.id = 'menu-panel-title';
    h2.textContent = 'Carta completa';

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'diner-panel-close';
    closeBtn.setAttribute('aria-label', 'Cerrar');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', close);

    header.appendChild(h2);
    header.appendChild(closeBtn);

    body = document.createElement('div');
    body.className = 'diner-panel-body';

    box.appendChild(header);
    box.appendChild(body);
    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  function normalizeMenu(data) {
    // GET /api/diner/menu returns {restaurant_name, currency, categories:
    // [{name, dishes:[...]}]} — see app/routes/diner.py::diner_menu. Dishes
    // are already active-only and availability-annotated server-side.
    return (data && Array.isArray(data.categories)) ? data.categories : [];
  }

  function renderLoading() {
    body.textContent = '';
    var p = document.createElement('p');
    p.className = 'diner-panel-loading';
    p.textContent = 'Cargando carta...';
    body.appendChild(p);
  }

  function renderErrorState() {
    body.textContent = '';
    var p = document.createElement('p');
    p.className = 'diner-panel-empty';
    p.textContent = 'No pudimos cargar la carta.';
    var retryBtn = document.createElement('button');
    retryBtn.type = 'button';
    retryBtn.className = 'm-btn m-btn--primary m-btn--sm';
    retryBtn.textContent = 'Reintentar';
    retryBtn.addEventListener('click', function () { loaded = false; load(); });
    body.appendChild(p);
    body.appendChild(retryBtn);
  }

  function renderMenu() {
    body.textContent = '';
    var cats = Array.isArray(menu) ? menu : [];
    var any = false;

    cats.forEach(function (cat) {
      var dishes = Array.isArray(cat && cat.dishes) ? cat.dishes : [];
      if (!dishes.length) return;
      any = true;

      var section = document.createElement('section');
      section.className = 'diner-menu-section';

      var h3 = document.createElement('h3');
      h3.textContent = cat.name || '';
      section.appendChild(h3);

      var grid = document.createElement('div');
      grid.className = 'diner-menu-grid';
      dishes.forEach(function (dish) {
        grid.appendChild(buildDinerDishCard(dish, { compact: false }));
      });
      section.appendChild(grid);
      body.appendChild(section);
    });

    if (!any) {
      var p = document.createElement('p');
      p.className = 'diner-panel-empty';
      p.textContent = 'Este restaurante todavía no tiene platos publicados.';
      body.appendChild(p);
    }
  }

  async function load() {
    renderLoading();
    try {
      var data = await DinerSession.fetch('/api/diner/menu', 'GET', null, getToken());
      menu = normalizeMenu(data);
      loaded = true;
      renderMenu();
    } catch (e) {
      renderErrorState();
    }
  }

  function open() {
    ensureBuilt();
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'menu-panel-title' });
    if (!loaded) load();
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
  }

  return { open: open, close: close };
})();

/* ── Send-order confirm sheet (Gap 2) ─────────────────────────────────
 * "Enviar pedido" → summary → confirm, per the PM brief. Calls
 * POST /api/diner/order/send with a fresh client-generated
 * idempotency_key each time the sheet is opened and confirmed — a retry
 * of the SAME tap (network hiccup, double submit before the button
 * disables) reuses that same key so the server returns the identical
 * result instead of creating a second order.
 * ════════════════════════════════════════════════════════════════════ */

var SendOrderSheet = (function () {
  var overlay = null;
  var box = null;
  var listEl = null;
  var totalEl = null;
  var trap = null;
  var pendingKey = null;

  function ensureBuilt() {
    if (overlay) return;

    overlay = document.createElement('div');
    overlay.className = 'diner-overlay';

    box = document.createElement('div');
    box.className = 'diner-sheet';

    var title = document.createElement('h2');
    title.id = 'send-sheet-title';
    title.textContent = 'Confirmar pedido';
    box.appendChild(title);

    listEl = document.createElement('ul');
    listEl.className = 'diner-cart-card-list diner-send-list';
    box.appendChild(listEl);

    totalEl = document.createElement('p');
    totalEl.className = 'diner-cart-card-subtotal';
    box.appendChild(totalEl);

    var actions = document.createElement('div');
    actions.className = 'diner-sheet-actions';

    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'm-btn m-btn--ghost';
    cancelBtn.textContent = 'Seguir editando';
    cancelBtn.addEventListener('click', close);

    var confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'm-btn m-btn--primary';
    confirmBtn.textContent = 'Enviar a cocina';
    confirmBtn.addEventListener('click', confirmSend);

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(actions);

    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  function render() {
    listEl.textContent = '';
    var items = (state.cart && Array.isArray(state.cart.items)) ? state.cart.items : [];
    items.forEach(function (item) {
      var li = document.createElement('li');
      var qty = document.createElement('span');
      qty.className = 'diner-cart-card-qty';
      qty.textContent = (item.qty != null ? item.qty : 1) + '×';
      var name = document.createElement('span');
      name.className = 'diner-cart-card-name';
      name.textContent = item.name || '';
      li.appendChild(qty);
      li.appendChild(name);
      if (item.note) {
        var note = document.createElement('span');
        note.className = 'diner-cart-row-note';
        note.textContent = ' — ' + item.note;
        li.appendChild(note);
      }
      listEl.appendChild(li);
    });

    totalEl.textContent = '';
    var totalLabel = document.createElement('span');
    totalLabel.textContent = 'Total: ';
    var totalValue = document.createElement('strong');
    totalValue.textContent = renderer().fmtPrice(
      state.cart ? state.cart.subtotal : 0, state.locale, (state.cart && state.cart.currency) || state.currency
    );
    totalEl.appendChild(totalLabel);
    totalEl.appendChild(totalValue);
  }

  function open() {
    var items = (state.cart && Array.isArray(state.cart.items)) ? state.cart.items : [];
    if (!items.length) {
      mesioToast('Tu pedido está vacío. Agrega algo primero.', 'error', 3000);
      return;
    }
    ensureBuilt();
    render();
    pendingKey = genIdempotencyKey();
    var confirmBtn = box.querySelector('.m-btn--primary');
    if (confirmBtn) confirmBtn.disabled = false;
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'send-sheet-title' });
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
  }

  async function confirmSend() {
    var confirmBtn = box.querySelector('.m-btn--primary');
    if (confirmBtn) confirmBtn.disabled = true;
    try {
      var data = await DinerSession.fetch('/api/diner/order/send', 'POST', {
        idempotency_key: pendingKey,
      }, getToken());
      close();
      state.cart = null;
      updateCartChip();
      CartPanel.refresh();
      var bubble = createBotBubble();
      bubble.appendChild(createTextNode(data.message || 'Listo, tu pedido ya va para la cocina.'));
      appendBubble(bubble);
      mesioToast('Pedido enviado a cocina', 'success', 3000);
      TablePanel.refresh();
      maybeOfferMemory(data);
    } catch (e) {
      mesioToast((e && e.message) || 'No pudimos enviar tu pedido. Intenta de nuevo.', 'error', 4500);
    } finally {
      if (confirmBtn) confirmBtn.disabled = false;
    }
  }

  return { open: open, close: close };
})();

/* ── Checkout sheet ("Pedir la cuenta") — waiter-mediated, no gateway ──
 * PM decision (CLAUDE.md, 2026-09-11): no Wompi/Bold at launch. The diner
 * only DECLARES scope ("lo mío" / "toda la mesa") and method (tarjeta /
 * efectivo) [+ optional tip / name / phone] — the waiter physically charges
 * on whatever datáfono the restaurant has, or takes cash. No card fields,
 * no payment link, ever, anywhere in this sheet.
 * POST /api/diner/checkout → {check_id, status:'pending_waiter', total, ...}
 * ════════════════════════════════════════════════════════════════════ */
var CheckoutSheet = (function () {
  var overlay = null;
  var box = null;
  var trap = null;
  var scope = 'mine';
  var method = 'tarjeta';
  var methods = [];          // [{key,label,kind,instructions}] — GET /api/diner/payment-options
  var proofReady = false;    // transfer receipt uploaded for this open()
  var scopeBtns = [];
  var methodRow = null;
  var transferBox = null;
  var hintEl = null;
  var tipInput = null;
  var nameInput = null;
  var phoneInput = null;
  var confirmBtn = null;

  function makeToggleRow(options, selected, onSelect) {
    var row = document.createElement('div');
    row.className = 'diner-toggle-row';
    var btns = [];
    options.forEach(function (opt) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'diner-toggle-btn' + (opt.value === selected ? ' diner-toggle-btn--active' : '');
      btn.textContent = opt.label;
      btn.addEventListener('click', function () {
        onSelect(opt.value);
        btns.forEach(function (b, idx) {
          b.classList.toggle('diner-toggle-btn--active', options[idx].value === opt.value);
        });
      });
      btns.push(btn);
      row.appendChild(btn);
    });
    return { row: row, btns: btns };
  }

  function ensureBuilt() {
    if (overlay) return;

    overlay = document.createElement('div');
    overlay.className = 'diner-overlay';

    box = document.createElement('div');
    box.className = 'diner-sheet';

    var title = document.createElement('h2');
    title.id = 'checkout-sheet-title';
    title.textContent = 'Pedir la cuenta';
    box.appendChild(title);

    var scopeLabel = document.createElement('label');
    scopeLabel.className = 'diner-sheet-note-label';
    scopeLabel.textContent = '¿Qué quieres pagar?';
    box.appendChild(scopeLabel);
    var scopeToggle = makeToggleRow(
      [{ value: 'mine', label: 'Solo lo mío' }, { value: 'table', label: 'Toda la mesa' }],
      scope,
      function (v) { scope = v; }
    );
    scopeBtns = scopeToggle.btns;
    box.appendChild(scopeToggle.row);

    var methodLabel = document.createElement('label');
    methodLabel.className = 'diner-sheet-note-label';
    methodLabel.textContent = '¿Cómo vas a pagar?';
    box.appendChild(methodLabel);
    // Filled on open() with the methods THIS sede accepts.
    methodRow = document.createElement('div');
    box.appendChild(methodRow);
    transferBox = document.createElement('div');
    transferBox.className = 'diner-sheet-field';
    transferBox.hidden = true;
    box.appendChild(transferBox);

    var tipField = document.createElement('div');
    tipField.className = 'diner-sheet-field';
    var tipLabel = document.createElement('label');
    tipLabel.className = 'diner-sheet-note-label';
    tipLabel.textContent = 'Propina (opcional)';
    tipInput = document.createElement('input');
    tipInput.setAttribute('aria-label', 'Propina');
    tipInput.type = 'number';
    tipInput.min = '0';
    tipInput.className = 'diner-sheet-input';
    tipInput.placeholder = '0';
    tipField.appendChild(tipLabel);
    tipField.appendChild(tipInput);
    box.appendChild(tipField);

    var nameField = document.createElement('div');
    nameField.className = 'diner-sheet-field';
    var nameLabel = document.createElement('label');
    nameLabel.className = 'diner-sheet-note-label';
    nameLabel.textContent = 'Tu nombre (opcional)';
    nameInput = document.createElement('input');
    nameInput.setAttribute('aria-label', 'Tu nombre');
    nameInput.type = 'text';
    nameInput.maxLength = 100;
    nameInput.className = 'diner-sheet-input';
    nameField.appendChild(nameLabel);
    nameField.appendChild(nameInput);
    box.appendChild(nameField);

    var phoneField = document.createElement('div');
    phoneField.className = 'diner-sheet-field';
    var phoneLabel = document.createElement('label');
    phoneLabel.className = 'diner-sheet-note-label';
    phoneLabel.textContent = 'Tu teléfono (opcional)';
    phoneInput = document.createElement('input');
    phoneInput.setAttribute('aria-label', 'Tu teléfono');
    phoneInput.type = 'tel';
    phoneInput.maxLength = 30;
    phoneInput.className = 'diner-sheet-input';
    phoneField.appendChild(phoneLabel);
    phoneField.appendChild(phoneInput);
    box.appendChild(phoneField);

    hintEl = document.createElement('p');
    hintEl.className = 'diner-sheet-hint';
    box.appendChild(hintEl);

    var actions = document.createElement('div');
    actions.className = 'diner-sheet-actions';

    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'm-btn m-btn--ghost';
    cancelBtn.textContent = 'Cancelar';
    cancelBtn.addEventListener('click', close);

    confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'm-btn m-btn--primary';
    confirmBtn.textContent = 'Pedir la cuenta';
    confirmBtn.addEventListener('click', confirmCheckout);

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(actions);

    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  function chosenMethod() {
    for (var i = 0; i < methods.length; i++) if (methods[i].key === method) return methods[i];
    return null;
  }

  function renderHint() {
    var m = chosenMethod();
    hintEl.textContent = (m && m.kind === 'transfer')
      ? 'Transfiere desde tu app y sube el comprobante. La caja lo revisa y el mesero te confirma.'
      : 'El mesero te cobra en el datáfono del restaurante o en efectivo. Nunca vas a ingresar datos de tu tarjeta aquí.';
  }

  /* Where to send the money (the restaurant's own text) + the receipt upload. */
  function renderTransfer() {
    var m = chosenMethod();
    transferBox.textContent = '';
    proofReady = false;
    if (!m || m.kind !== 'transfer') { transferBox.hidden = true; renderHint(); return; }
    transferBox.hidden = false;
    var where = document.createElement('p');
    where.className = 'diner-transfer-instructions';
    where.textContent = m.instructions
      ? m.instructions
      : 'Pídele al mesero los datos para transferir por ' + m.label + '.';
    transferBox.appendChild(where);
    var label = document.createElement('label');
    label.className = 'diner-sheet-note-label';
    label.textContent = 'Comprobante de pago (foto o captura)';
    transferBox.appendChild(label);
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.className = 'diner-checkout-file-input';
    input.setAttribute('aria-label', 'Comprobante de pago (foto o captura)');
    transferBox.appendChild(input);
    var status = document.createElement('p');
    status.className = 'diner-checkout-upload-status';
    status.textContent = 'Sube una foto clara de la transferencia.';
    transferBox.appendChild(status);
    input.addEventListener('change', async function () {
      var file = input.files && input.files[0];
      if (!file) return;
      proofReady = false;
      confirmBtn.disabled = true;
      status.textContent = 'Subiendo comprobante...';
      try {
        var res = await DinerSession.uploadProof(getToken(), file);
        proofReady = !!(res && res.proof_url);
        status.textContent = proofReady ? 'Comprobante subido ✓' : 'No pudimos confirmar la subida.';
      } catch (e) {
        status.textContent = (e && e.message) || 'No pudimos subir el comprobante. Intenta con otra imagen.';
      } finally {
        confirmBtn.disabled = false;
      }
    });
    renderHint();
  }

  function renderMethods() {
    methodRow.textContent = '';
    var toggle = makeToggleRow(
      methods.map(function (m) { return { value: m.key, label: m.label }; }),
      method,
      function (v) { method = v; renderTransfer(); }
    );
    methodRow.appendChild(toggle.row);
    renderTransfer();
  }

  async function loadMethods() {
    try {
      var data = await DinerSession.fetch('/api/diner/payment-options', 'GET', null, getToken());
      methods = (data && Array.isArray(data.methods) && data.methods.length) ? data.methods : [];
    } catch (e) {
      methods = [];
    }
    if (!methods.length) {
      // Same as the server's default for a sede that chose none.
      methods = [
        { key: 'tarjeta', label: 'Tarjeta (datáfono)', kind: 'card', instructions: '' },
        { key: 'efectivo', label: 'Efectivo', kind: 'cash', instructions: '' },
      ];
    }
    method = methods[0].key;
    renderMethods();
  }

  function open() {
    ensureBuilt();
    scope = 'mine';
    methods = [];
    method = '';
    methodRow.textContent = '';
    transferBox.hidden = true;
    renderHint();
    loadMethods();
    scopeBtns.forEach(function (b, idx) { b.classList.toggle('diner-toggle-btn--active', idx === 0); });
    tipInput.value = '';
    nameInput.value = '';
    phoneInput.value = '';
    confirmBtn.disabled = false;
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'checkout-sheet-title' });
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
  }

  async function confirmCheckout() {
    var m = chosenMethod();
    if (!m) { mesioToast('Elige cómo vas a pagar.', 'warning', 3000); return; }
    if (m.kind === 'transfer' && !proofReady) {
      mesioToast('Sube el comprobante de la transferencia.', 'warning', 3500);
      return;
    }
    confirmBtn.disabled = true;
    try {
      var tipRaw = tipInput.value.trim();
      var tipAmount = tipRaw ? Number(tipRaw) : 0;
      if (!isFinite(tipAmount) || tipAmount < 0) tipAmount = 0;

      var body = { scope: scope, method: method, tip_amount: tipAmount };
      var nameVal = nameInput.value.trim();
      var phoneVal = phoneInput.value.trim();
      if (nameVal) body.customer_name = nameVal;
      if (phoneVal) body.customer_phone = phoneVal;

      var data = await DinerSession.fetch('/api/diner/checkout', 'POST', body, getToken());
      close();
      var bubble = createBotBubble();
      // The status card already says "ya le avisamos al mesero"; the text
      // above it gives the amount instead of repeating that sentence.
      if (data.total != null) {
        bubble.appendChild(createTextNode('Tu cuenta: ' + renderer().fmtPrice(data.total, state.locale, state.currency) + '.'));
      }
      bubble.appendChild(renderCheckoutStatusBlock({ status: data.status }));
      appendBubble(bubble);
      if (m.kind === 'transfer' && data.message) bubble.appendChild(createTextNode(data.message));
      mesioToast(m.kind === 'transfer' ? 'Comprobante enviado' : 'Ya avisamos al mesero', 'success', 4000);
      lastCheckoutStatus = data.status;
      TablePanel.refresh();
    } catch (e) {
      mesioToast((e && e.message) || 'No pudimos pedir la cuenta. Intenta de nuevo o hazle señas al mesero.', 'error', 5000);
    } finally {
      confirmBtn.disabled = false;
    }
  }

  return { open: open, close: close };
})();

/* ── Delivery/pickup checkout sheet (docs/claude/delivery-web.md chunk 5) ──
 * The DETERMINISTIC checkout form — "Finalizar pedido" on a delivery/pickup
 * session opens THIS instead of SendOrderSheet (which posts to the kitchen
 * via the dine-in-only /api/diner/order/send). Every field here is plain
 * form input, never LLM-parsed (chunk 3/5 instructions). Submits
 * POST /api/diner/delivery/checkout with a stable idempotency_key generated
 * once per open() so a double-tap or a retry after a network hiccup can
 * never create two orders (the backend also dedupes by key — belt+braces).
 * ════════════════════════════════════════════════════════════════════ */

var PAYMENT_METHOD_LABELS = {
  efectivo: 'Efectivo', cash: 'Efectivo',
  tarjeta: 'Tarjeta (datáfono)', card: 'Tarjeta (datáfono)', datafono: 'Tarjeta (datáfono)', terminal: 'Tarjeta (datáfono)',
  nequi: 'Nequi', daviplata: 'Daviplata', bancolombia: 'Bancolombia', bold: 'Bold', wompi: 'Transferencia',
};
var CASH_PAYMENT_KEYS = ['efectivo', 'cash'];
var CARD_PAYMENT_KEYS = ['tarjeta', 'card', 'datafono', 'terminal'];
var TIP_SUGGEST_PCTS = [0, 0.05, 0.10, 0.15];

function paymentMethodLabel(key) {
  return PAYMENT_METHOD_LABELS[key] || (String(key).charAt(0).toUpperCase() + String(key).slice(1));
}
function isCashPaymentMethod(key) { return CASH_PAYMENT_KEYS.indexOf(key) !== -1; }
function isCardPaymentMethod(key) { return CARD_PAYMENT_KEYS.indexOf(key) !== -1; }
function isTransferPaymentMethod(key) { return !isCashPaymentMethod(key) && !isCardPaymentMethod(key); }

var DeliveryCheckoutSheet = (function () {
  var overlay = null;
  var box = null;
  var bodyEl = null;
  var trap = null;
  var pendingKey = null;
  var selectedMethod = null;
  var tipAmount = 0;
  var proofUrl = null;
  var proofUploading = false;
  var submitting = false;
  var gpsRetried = false;

  // Field refs, rebuilt every open() (payment methods / mode can differ
  // between opens if the session changed) — see render().
  var refs = {};

  function fmt(n) { return renderer().fmtPrice(n, state.locale, state.cart && state.cart.currency || state.currency); }

  function subtotalValue() { return (state.cart && Number(state.cart.subtotal)) || 0; }
  function deliveryFeeValue() { return state.orderMode === 'delivery' ? (Number(state.deliveryConfig.delivery_fee) || 0) : 0; }
  function totalValue() { return subtotalValue() + deliveryFeeValue() + (Number(tipAmount) || 0); }

  function ensureShell() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'diner-overlay diner-overlay--full';
    box = document.createElement('div');
    box.className = 'diner-panel diner-checkout-panel';

    var header = document.createElement('div');
    header.className = 'diner-panel-header';
    var h2 = document.createElement('h2');
    h2.id = 'delivery-checkout-title';
    h2.textContent = 'Finalizar pedido';
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'diner-panel-close';
    closeBtn.setAttribute('aria-label', 'Cerrar');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', close);
    header.appendChild(h2);
    header.appendChild(closeBtn);

    bodyEl = document.createElement('div');
    bodyEl.className = 'diner-panel-body';

    box.appendChild(header);
    box.appendChild(bodyEl);
    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  var fieldSeq = 0;
  function field(labelText, inputEl) {
    // The <label> must be tied to its input (htmlFor), otherwise the field
    // has no accessible name — screen readers announced "edit text" for
    // name/phone/email — and tapping the label does not focus the input.
    fieldSeq += 1;
    if (!inputEl.id) inputEl.id = 'dc-field-' + fieldSeq;
    var wrap = document.createElement('div');
    wrap.className = 'diner-sheet-field';
    var label = document.createElement('label');
    label.className = 'diner-sheet-note-label';
    label.htmlFor = inputEl.id;
    label.textContent = labelText;
    wrap.appendChild(label);
    wrap.appendChild(inputEl);
    var err = document.createElement('p');
    err.className = 'diner-checkout-field-error';
    err.id = inputEl.id + '-error';
    err.setAttribute('role', 'alert');
    err.hidden = true;
    inputEl.setAttribute('aria-describedby', err.id);
    wrap.appendChild(err);
    return { wrap: wrap, input: inputEl, err: err };
  }

  function showFieldError(f, msg) {
    if (!f) return;
    f.err.textContent = msg;
    f.err.hidden = !msg;
    // Some refs are groups, not inputs (refs.payment has input: null) —
    // an unguarded setAttribute threw inside validate()->clearFieldErrors()
    // and silently killed every checkout submit.
    if (f.input) f.input.setAttribute('aria-invalid', msg ? 'true' : 'false');
  }

  function clearFieldErrors() {
    ['name', 'phone', 'street', 'payment', 'cashChangeFor', 'proof'].forEach(function (k) {
      if (refs[k]) showFieldError(refs[k], null);
    });
  }

  function showBanner(msg) {
    if (!refs.banner) return;
    refs.banner.textContent = msg || '';
    refs.banner.hidden = !msg;
  }

  function renderSummary() {
    if (!refs.summary) return;
    refs.summary.textContent = '';
    var rows = [['Subtotal', subtotalValue()]];
    if (state.orderMode === 'delivery') rows.push(['Domicilio', deliveryFeeValue()]);
    rows.push(['Propina', Number(tipAmount) || 0]);
    rows.forEach(function (r) {
      var row = document.createElement('p');
      row.className = 'diner-checkout-summary-row';
      var label = document.createElement('span');
      label.textContent = r[0];
      var value = document.createElement('span');
      value.textContent = fmt(r[1]);
      row.appendChild(label);
      row.appendChild(value);
      refs.summary.appendChild(row);
    });
    var totalRow = document.createElement('p');
    totalRow.className = 'diner-checkout-summary-row diner-checkout-summary-total';
    var totalLabel = document.createElement('strong');
    totalLabel.textContent = 'Total';
    var totalValueEl = document.createElement('strong');
    totalValueEl.textContent = fmt(totalValue());
    totalRow.appendChild(totalLabel);
    totalRow.appendChild(totalValueEl);
    refs.summary.appendChild(totalRow);
  }

  function setTip(amount) {
    tipAmount = Math.max(0, Number(amount) || 0);
    if (refs.tipCustom) refs.tipCustom.value = tipAmount ? String(tipAmount) : '';
    if (refs.tipBtns) {
      refs.tipBtns.forEach(function (b) { b.classList.toggle('diner-toggle-btn--active', Number(b.dataset.pct) * subtotalValue() === tipAmount && tipAmount !== 0 || (Number(b.dataset.pct) === 0 && tipAmount === 0)); });
    }
    renderSummary();
  }

  function buildPaymentSubfields(container) {
    container.textContent = '';
    if (!selectedMethod) return;

    if (isCashPaymentMethod(selectedMethod)) {
      var cashInput = document.createElement('input');
      cashInput.type = 'number';
      cashInput.min = '0';
      cashInput.className = 'diner-sheet-input';
      cashInput.placeholder = '0';
      var cashField = field('¿Con cuánto vas a pagar?', cashInput);
      refs.cashChangeFor = cashField;
      container.appendChild(cashField.wrap);
    } else if (isTransferPaymentMethod(selectedMethod)) {
      var uploadWrap = document.createElement('div');
      uploadWrap.className = 'diner-sheet-field';
      // Where to send the money — the transfer screen used to ask for a
      // receipt without ever saying which account to pay.
      var whereEl = document.createElement('p');
      whereEl.className = 'diner-transfer-instructions';
      whereEl.textContent = 'Cargando los datos para transferir...';
      uploadWrap.appendChild(whereEl);
      var methodForWhere = selectedMethod;
      DinerSession.fetch('/api/diner/payment-options', 'GET', null, getToken()).then(function (data) {
        var list = (data && Array.isArray(data.methods)) ? data.methods : [];
        var hit = list.filter(function (x) { return x.key === methodForWhere; })[0];
        whereEl.textContent = (hit && hit.instructions)
          ? hit.instructions
          : 'Llama al restaurante para pedir los datos de ' + paymentMethodLabel(methodForWhere) + '.';
      }).catch(function () {
        whereEl.textContent = 'Llama al restaurante para pedir los datos de ' + paymentMethodLabel(methodForWhere) + '.';
      });
      var uploadLabel = document.createElement('label');
      uploadLabel.className = 'diner-sheet-note-label';
      uploadLabel.textContent = 'Comprobante de pago (foto o captura)';
      uploadWrap.appendChild(uploadLabel);

      var fileInput = document.createElement('input');
      fileInput.type = 'file';
      fileInput.accept = 'image/*';
      fileInput.className = 'diner-checkout-file-input';
      fileInput.setAttribute('aria-label', 'Comprobante de pago (foto o captura)');
      uploadWrap.appendChild(fileInput);

      var status = document.createElement('p');
      status.className = 'diner-checkout-upload-status';
      status.textContent = proofUrl ? 'Comprobante subido ✓' : 'Sube una foto clara de la transferencia antes de continuar.';
      uploadWrap.appendChild(status);

      var errEl = document.createElement('p');
      errEl.className = 'diner-checkout-field-error';
      errEl.hidden = true;
      uploadWrap.appendChild(errEl);
      refs.proof = { wrap: uploadWrap, input: fileInput, err: errEl };

      fileInput.addEventListener('change', async function () {
        var file = fileInput.files && fileInput.files[0];
        if (!file) return;
        proofUploading = true;
        proofUrl = null;
        status.textContent = 'Subiendo comprobante...';
        try {
          var res = await DinerSession.uploadProof(getToken(), file);
          proofUrl = (res && res.proof_url) || null;
          status.textContent = proofUrl ? 'Comprobante subido ✓' : 'No pudimos confirmar la subida.';
        } catch (e) {
          status.textContent = 'No pudimos subir el comprobante.';
          showFieldError(refs.proof, (e && e.message) || 'Intenta con otra imagen.');
        } finally {
          proofUploading = false;
        }
      });

      container.appendChild(uploadWrap);
    }
    // Card/datáfono methods need no extra subfield — the rider/cashier
    // charges on the physical terminal, same posture as the dine-in
    // CheckoutSheet's "Tarjeta" option.
  }

  function buildPaymentRow(container) {
    var methods = Array.isArray(state.deliveryConfig.payment_methods) ? state.deliveryConfig.payment_methods : [];
    var row = document.createElement('div');
    row.className = 'diner-toggle-row diner-toggle-row--wrap';
    var subfields = document.createElement('div');
    subfields.className = 'diner-checkout-payment-sub';

    if (!methods.length) {
      var none = document.createElement('p');
      none.className = 'pedir-entry-status pedir-entry-status--error';
      none.textContent = 'Esta sede no tiene métodos de pago configurados. Llama al restaurante para completar tu pedido.';
      container.appendChild(none);
      return;
    }

    methods.forEach(function (m, idx) {
      var key = String(m).trim().toLowerCase();
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'diner-toggle-btn' + (idx === 0 ? ' diner-toggle-btn--active' : '');
      btn.textContent = paymentMethodLabel(key);
      btn.addEventListener('click', function () {
        selectedMethod = key;
        proofUrl = null;
        Array.prototype.forEach.call(row.children, function (b) { b.classList.remove('diner-toggle-btn--active'); });
        btn.classList.add('diner-toggle-btn--active');
        buildPaymentSubfields(subfields);
      });
      row.appendChild(btn);
    });
    selectedMethod = String(methods[0]).trim().toLowerCase();

    var fieldWrap = document.createElement('div');
    fieldWrap.className = 'diner-sheet-field';
    // A button group, not a form control: a <label> cannot name it, so
    // expose it as a labelled group for screen readers instead.
    var label = document.createElement('p');
    label.className = 'diner-sheet-note-label';
    label.id = 'dc-payment-label';
    label.textContent = '¿Cómo vas a pagar?';
    row.setAttribute('role', 'group');
    row.setAttribute('aria-labelledby', label.id);
    fieldWrap.appendChild(label);
    fieldWrap.appendChild(row);
    var err = document.createElement('p');
    err.className = 'diner-checkout-field-error';
    err.hidden = true;
    fieldWrap.appendChild(err);
    refs.payment = { wrap: fieldWrap, input: null, err: err };

    container.appendChild(fieldWrap);
    container.appendChild(subfields);
    buildPaymentSubfields(subfields);
  }

  function render() {
    bodyEl.textContent = '';
    refs = {};
    proofUrl = null;
    selectedMethod = null;
    tipAmount = 0;

    refs.banner = document.createElement('p');
    refs.banner.className = 'diner-checkout-banner';
    refs.banner.setAttribute('role', 'alert');
    refs.banner.hidden = true;
    bodyEl.appendChild(refs.banner);

    var profile = DinerSession.loadCheckoutProfile();

    var nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.maxLength = 100;
    nameInput.className = 'diner-sheet-input';
    nameInput.value = profile.name || '';
    refs.name = field('Tu nombre', nameInput);
    bodyEl.appendChild(refs.name.wrap);

    var phoneInput = document.createElement('input');
    phoneInput.type = 'tel';
    phoneInput.maxLength = 30;
    phoneInput.className = 'diner-sheet-input';
    phoneInput.value = profile.phone || '';
    refs.phone = field('Tu teléfono', phoneInput);
    bodyEl.appendChild(refs.phone.wrap);

    if (state.orderMode === 'delivery') {
      var streetInput = document.createElement('input');
      streetInput.type = 'text';
      streetInput.maxLength = 150;
      streetInput.className = 'diner-sheet-input';
      streetInput.placeholder = 'Calle y número';
      streetInput.value = profile.street || '';
      refs.street = field('Dirección (calle y número)', streetInput);
      bodyEl.appendChild(refs.street.wrap);

      var barrioInput = document.createElement('input');
      barrioInput.type = 'text';
      barrioInput.maxLength = 100;
      barrioInput.className = 'diner-sheet-input';
      barrioInput.placeholder = 'Barrio';
      barrioInput.value = profile.barrio || '';
      refs.barrio = field('Barrio', barrioInput);
      bodyEl.appendChild(refs.barrio.wrap);

      var indicacionesInput = document.createElement('textarea');
      indicacionesInput.rows = 2;
      indicacionesInput.maxLength = 200;
      indicacionesInput.className = 'diner-sheet-note';
      indicacionesInput.placeholder = 'Ej: apto 301, torre 2, portería...';
      indicacionesInput.value = profile.indicaciones || '';
      refs.indicaciones = field('Indicaciones (opcional)', indicacionesInput);
      bodyEl.appendChild(refs.indicaciones.wrap);
    }

    var emailInput = document.createElement('input');
    emailInput.type = 'email';
    emailInput.maxLength = 254;
    emailInput.className = 'diner-sheet-input';
    emailInput.value = profile.email || '';
    refs.email = field('Correo (opcional)', emailInput);
    bodyEl.appendChild(refs.email.wrap);

    buildPaymentRow(bodyEl);

    var tipWrap = document.createElement('div');
    tipWrap.className = 'diner-sheet-field';
    var tipLabel = document.createElement('p');
    tipLabel.className = 'diner-sheet-note-label';
    tipLabel.id = 'dc-tip-label';
    tipLabel.textContent = 'Propina (opcional)';
    tipWrap.appendChild(tipLabel);
    var tipRow = document.createElement('div');
    tipRow.className = 'diner-toggle-row';
    tipRow.setAttribute('role', 'group');
    tipRow.setAttribute('aria-labelledby', tipLabel.id);
    refs.tipBtns = TIP_SUGGEST_PCTS.map(function (pct) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'diner-toggle-btn' + (pct === 0 ? ' diner-toggle-btn--active' : '');
      btn.dataset.pct = String(pct);
      btn.textContent = pct === 0 ? 'Sin propina' : Math.round(pct * 100) + '%';
      btn.addEventListener('click', function () { setTip(Math.round(subtotalValue() * pct)); });
      tipRow.appendChild(btn);
      return btn;
    });
    tipWrap.appendChild(tipRow);
    refs.tipCustom = document.createElement('input');
    refs.tipCustom.type = 'number';
    refs.tipCustom.min = '0';
    refs.tipCustom.className = 'diner-sheet-input';
    refs.tipCustom.placeholder = 'Otro monto';
    refs.tipCustom.setAttribute('aria-label', 'Otro monto de propina');
    refs.tipCustom.addEventListener('input', function () {
      var v = Number(refs.tipCustom.value);
      tipAmount = isFinite(v) && v > 0 ? v : 0;
      renderSummary();
    });
    tipWrap.appendChild(refs.tipCustom);
    bodyEl.appendChild(tipWrap);

    var scheduleInput = document.createElement('input');
    scheduleInput.type = 'time';
    scheduleInput.className = 'diner-sheet-input';
    refs.schedule = field('Programar para hoy a las (opcional)', scheduleInput);
    bodyEl.appendChild(refs.schedule.wrap);

    refs.summary = document.createElement('div');
    refs.summary.className = 'diner-checkout-summary';
    bodyEl.appendChild(refs.summary);
    renderSummary();

    if (state.turnstileSiteKey) {
      var turnstileBox = document.createElement('div');
      turnstileBox.className = 'pedir-turnstile';
      bodyEl.appendChild(turnstileBox);
      TurnstileHelper.renderInto(turnstileBox, state.turnstileSiteKey, function (token) {
        state.turnstileCheckoutToken = token;
      });
    }

    refs.submitBtn = document.createElement('button');
    refs.submitBtn.type = 'button';
    refs.submitBtn.className = 'm-btn m-btn--primary diner-checkout-submit-btn';
    refs.submitBtn.textContent = 'Confirmar pedido';
    refs.submitBtn.addEventListener('click', submit);
    bodyEl.appendChild(refs.submitBtn);
  }

  function renderSuccess(data) {
    bodyEl.textContent = '';
    var card = document.createElement('div');
    card.className = 'diner-checkout-success';
    var icon = document.createElement('span');
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = '✅';
    card.appendChild(icon);
    var msg = document.createElement('p');
    msg.textContent = data.message || 'Listo, tu pedido fue enviado al restaurante.';
    card.appendChild(msg);
    var codeLabel = document.createElement('p');
    codeLabel.className = 'diner-checkout-success-code-label';
    codeLabel.textContent = 'Tu código de pedido:';
    card.appendChild(codeLabel);
    var code = document.createElement('p');
    code.className = 'diner-checkout-success-code';
    code.textContent = data.public_code || '';
    card.appendChild(code);
    var link = document.createElement('a');
    link.href = '/pedido/' + encodeURIComponent(data.public_code || '');
    link.className = 'm-btn m-btn--secondary diner-checkout-status-link';
    link.textContent = 'Ver estado de mi pedido';
    card.appendChild(link);
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'm-btn m-btn--primary';
    closeBtn.textContent = 'Listo';
    closeBtn.addEventListener('click', close);
    card.appendChild(closeBtn);
    bodyEl.appendChild(card);
  }

  function validate() {
    clearFieldErrors();
    var ok = true;
    var nameVal = refs.name.input.value.trim();
    if (!nameVal) { showFieldError(refs.name, 'Escribe tu nombre.'); ok = false; }
    var phoneVal = refs.phone.input.value.trim();
    if (!phoneVal) { showFieldError(refs.phone, 'Escribe tu teléfono.'); ok = false; }
    if (state.orderMode === 'delivery') {
      var streetVal = refs.street.input.value.trim();
      if (!streetVal) { showFieldError(refs.street, 'Escribe tu dirección.'); ok = false; }
    }
    if (!selectedMethod) {
      // Never fail validation without saying why: the toast tells the
      // customer to fix the fields "marked in red", so one must be.
      showFieldError(refs.payment, 'Elige cómo vas a pagar.');
      ok = false;
    }
    if (selectedMethod && isCashPaymentMethod(selectedMethod)) {
      var changeVal = refs.cashChangeFor && Number(refs.cashChangeFor.input.value);
      if (!changeVal || changeVal <= 0) {
        showFieldError(refs.cashChangeFor, 'Indica con cuánto vas a pagar.');
        ok = false;
      }
    }
    if (selectedMethod && isTransferPaymentMethod(selectedMethod) && !proofUrl) {
      showFieldError(refs.proof, 'Sube el comprobante de la transferencia antes de continuar.');
      ok = false;
    }
    return ok;
  }

  function buildScheduledIso() {
    var raw = refs.schedule && refs.schedule.input.value;
    if (!raw) return null;
    var parts = raw.split(':');
    if (parts.length !== 2) return null;
    var d = new Date();
    d.setHours(Number(parts[0]), Number(parts[1]), 0, 0);
    return d.toISOString();
  }

  async function submit() {
    if (submitting || proofUploading) return;
    showBanner(null);
    if (!validate()) {
      mesioToast('Revisa los campos marcados en rojo.', 'error', 3500);
      return;
    }
    if (state.turnstileSiteKey && !state.turnstileCheckoutToken) {
      showBanner('Completa la verificación de seguridad para continuar.');
      return;
    }

    var profile = {
      name: refs.name.input.value.trim(),
      phone: refs.phone.input.value.trim(),
      email: refs.email.input.value.trim(),
    };
    var address;
    if (state.orderMode === 'delivery') {
      var street = refs.street.input.value.trim();
      var barrio = refs.barrio.input.value.trim();
      var indicaciones = refs.indicaciones.input.value.trim();
      profile.street = street;
      profile.barrio = barrio;
      profile.indicaciones = indicaciones;
      address = street + (barrio ? ', ' + barrio : '') + (indicaciones ? ' - ' + indicaciones : '');
    } else {
      address = 'Recoger en tienda' + (state.sedeName ? ' - ' + state.sedeName : '');
    }
    DinerSession.saveCheckoutProfile(profile);

    var body = {
      token: getToken(),
      idempotency_key: pendingKey,
      customer_name: profile.name,
      customer_phone: profile.phone,
      address: address,
      payment_method: selectedMethod,
      tip_amount: Number(tipAmount) || 0,
    };
    if (profile.email) body.customer_email = profile.email;
    if (state.lastPos) { body.lat = state.lastPos.lat; body.lon = state.lastPos.lon; }
    if (isCashPaymentMethod(selectedMethod)) body.cash_change_for = Number(refs.cashChangeFor.input.value);
    var scheduledIso = buildScheduledIso();
    if (scheduledIso) body.scheduled_for = scheduledIso;
    if (state.turnstileCheckoutToken) body.turnstile_token = state.turnstileCheckoutToken;
    var deviceToken = DinerSession.getDeviceToken();
    if (deviceToken) body.device_token = deviceToken;

    submitting = true;
    refs.submitBtn.disabled = true;
    refs.submitBtn.textContent = 'Enviando...';
    try {
      var data = await DinerSession.fetch('/api/diner/delivery/checkout', 'POST', body, null);
      state.cart = null;
      updateCartChip();
      CartPanel.refresh();
      DinerSession.saveLastOrderCode(data.public_code);
      renderSuccess(data);
    } catch (e) {
      var reason = e && e.reason;
      var msg = (e && e.message) || 'No pudimos enviar tu pedido. Intenta de nuevo.';
      if (reason === 'no_gps' && state.orderMode === 'delivery' && !gpsRetried) {
        // The session lost its GPS fix (old saved session, cleared storage).
        // The server needs the pin to check coverage, so ask again and retry
        // ONCE with the same idempotency key — never a second order.
        gpsRetried = true;
        refs.submitBtn.textContent = 'Obteniendo tu ubicación...';
        var geo = await DinerSession.getGeolocation(8000);
        submitting = false;
        if (geo.ok) {
          state.lastPos = { lat: geo.lat, lon: geo.lon };
          var saved = DinerSession.load();
          if (saved) { saved.lastPos = state.lastPos; DinerSession.save(saved); }
          return submit();
        }
        msg = 'Necesitamos tu ubicación para confirmar que llegamos a tu dirección. '
          + 'Actívala en tu navegador, o recoge tu pedido en la sede.';
      }
      if (reason === 'payment_method_not_allowed') showFieldError(refs.payment, msg);
      else if (reason === 'cash_change_for_required' || reason === 'cash_change_insufficient') showFieldError(refs.cashChangeFor, msg);
      else showBanner(msg);
      submitting = false;
      refs.submitBtn.disabled = false;
      refs.submitBtn.textContent = 'Confirmar pedido';
    }
  }

  function open() {
    var items = (state.cart && Array.isArray(state.cart.items)) ? state.cart.items : [];
    if (!items.length) {
      mesioToast('Tu pedido está vacío. Agrega algo primero.', 'error', 3000);
      return;
    }
    ensureShell();
    pendingKey = genIdempotencyKey();
    submitting = false;
    gpsRetried = false;
    render();
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'delivery-checkout-title' });
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
  }

  return { open: open, close: close };
})();

/* ── Table view (Gap 3) — "Tú" vs "Otro comensal" ─────────────────────
 * GET /api/diner/table. Polled every 8s while the panel is open (and once
 * right after a successful send) via mesioInterval, mirroring the
 * visibility-aware polling pattern used across the rest of the dashboard.
 * ════════════════════════════════════════════════════════════════════ */

var TABLE_STATUS_LABELS = {
  recibido: 'Recibido',
  en_preparacion: 'En preparación',
  listo: 'Listo',
  entregado: 'Entregado',
};

var TablePanel = (function () {
  var overlay = null;
  var box = null;
  var body = null;
  var trap = null;

  function ensureBuilt() {
    if (overlay) return;

    overlay = document.createElement('div');
    overlay.className = 'diner-overlay diner-overlay--full';

    box = document.createElement('div');
    box.className = 'diner-panel';

    var header = document.createElement('div');
    header.className = 'diner-panel-header';

    var h2 = document.createElement('h2');
    h2.id = 'table-panel-title';
    h2.textContent = 'Tu mesa';

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'diner-panel-close';
    closeBtn.setAttribute('aria-label', 'Cerrar');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', close);

    header.appendChild(h2);
    header.appendChild(closeBtn);

    body = document.createElement('div');
    body.className = 'diner-panel-body';

    box.appendChild(header);
    box.appendChild(body);
    overlay.appendChild(box);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
  }

  function buildOrderCard(order) {
    var card = document.createElement('div');
    card.className = 'diner-table-order-card' + (order.mine ? ' diner-table-order-card--mine' : '');

    var head = document.createElement('div');
    head.className = 'diner-table-order-head';
    var who = document.createElement('span');
    who.className = 'diner-table-order-who';
    who.textContent = order.diner_label || (order.mine ? 'Tú' : 'Otro comensal');
    var status = document.createElement('span');
    status.className = 'diner-table-order-status';
    status.textContent = TABLE_STATUS_LABELS[order.status] || order.status || '';
    head.appendChild(who);
    head.appendChild(status);
    card.appendChild(head);

    var list = document.createElement('ul');
    list.className = 'diner-cart-card-list';
    var items = Array.isArray(order.items) ? order.items : [];
    items.forEach(function (item) {
      var li = document.createElement('li');
      var qty = document.createElement('span');
      qty.className = 'diner-cart-card-qty';
      qty.textContent = (item.qty != null ? item.qty : 1) + '×';
      var name = document.createElement('span');
      name.className = 'diner-cart-card-name';
      name.textContent = item.name || '';
      li.appendChild(qty);
      li.appendChild(name);
      if (item.notes) {
        var note = document.createElement('span');
        note.className = 'diner-cart-row-note';
        note.textContent = ' — ' + item.notes;
        li.appendChild(note);
      }
      list.appendChild(li);
    });
    card.appendChild(list);

    return card;
  }

  function renderOrders(data) {
    body.textContent = '';
    var orders = (data && Array.isArray(data.orders)) ? data.orders : [];
    if (!orders.length) {
      var empty = document.createElement('p');
      empty.className = 'diner-panel-empty';
      empty.textContent = 'Todavía no hay pedidos en esta mesa.';
      body.appendChild(empty);
      return;
    }
    orders.forEach(function (order) { body.appendChild(buildOrderCard(order)); });
  }

  function renderErrorState() {
    body.textContent = '';
    var p = document.createElement('p');
    p.className = 'diner-panel-empty';
    p.textContent = 'No pudimos cargar tu mesa.';
    body.appendChild(p);
  }

  async function load() {
    try {
      var data = await DinerSession.fetch('/api/diner/table', 'GET', null, getToken());
      renderOrders(data);
    } catch (e) {
      renderErrorState();
    }
  }

  function open() {
    ensureBuilt();
    var loading = document.createElement('p');
    loading.className = 'diner-panel-loading';
    loading.textContent = 'Cargando tu mesa...';
    body.textContent = '';
    body.appendChild(loading);
    overlay.classList.add('open');
    trap = mesioFocusTrap(box, { onEscape: close, labelledBy: 'table-panel-title' });
    load();
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    if (trap) { trap.deactivate(); trap = null; }
  }

  function refresh() {
    if (overlay && overlay.classList.contains('open')) load();
  }

  return { open: open, close: close, refresh: refresh };
})();

/* ── Wiring ────────────────────────────────────────────────────────── */

function initComposer() {
  var form = dinerEl('diner-composer');
  var input = dinerEl('diner-input');
  if (!form || !input) return;
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = input.value;
    input.value = '';
    sendMessage(text);
  });
}

function initWaiterFab() {
  var btn = dinerEl('waiter-fab');
  if (btn) btn.addEventListener('click', function () { WaiterSheet.open(); });
}

function initHeaderButtons() {
  var menuBtn = dinerEl('btn-open-menu');
  if (menuBtn) menuBtn.addEventListener('click', function () { MenuPanel.open(); });
  var tableBtn = dinerEl('btn-open-table');
  if (tableBtn) tableBtn.addEventListener('click', function () { TablePanel.open(); });
  var cartChip = dinerEl('cart-chip');
  if (cartChip) cartChip.addEventListener('click', function () { CartPanel.open(); });
}

function initJoinBanner() {
  var btn = dinerEl('diner-join-open-btn');
  if (btn) btn.addEventListener('click', function () { JoinSheet.open(); });
}

function initErrorBanner() {
  var retryBtn = dinerEl('diner-retry-btn');
  if (retryBtn) retryBtn.addEventListener('click', startSession);
}

function initTablePolling() {
  // Visibility-aware, and backs off to a 60s safety net once MesioRealtime
  // is connected (table_order.* events call TablePanel.refresh() directly —
  // see connectDinerRealtime() above). TablePanel.refresh() itself is a
  // no-op unless the panel is open.
  // The same tick catches a ready order when a realtime event was missed.
  mesioLiveInterval(function () { TablePanel.refresh(); announceKitchenProgress(); }, 8000);
}

/* ── Status polling — GET /api/diner/status ───────────────────────────
 * Payment ('pending_waiter' → 'paid') and the NPS survey both start from
 * something that happens OUTSIDE this tab (the waiter marking the check
 * paid in caja, via the EXISTING pay_check flow) — nothing the diner types
 * triggers them. Polls globally (not gated by any panel being open) so the
 * diner sees "ya viene con tu cuenta" / "ya fue pagada" / the star survey
 * show up in their own chat feed without having to do anything.
 * ════════════════════════════════════════════════════════════════════ */
var lastCheckoutStatus = null;
var lastNpsStage = null;

async function pollDinerStatus() {
  if (!state.token) return;
  var data;
  try {
    data = await DinerSession.fetch('/api/diner/status', 'GET', null, getToken());
  } catch (e) {
    return; // best-effort — a transient failure here must never surface to the diner
  }
  if (!data) return;

  var checkout = data.checkout;
  var checkoutStatus = checkout ? checkout.status : null;
  if (checkoutStatus && checkoutStatus !== lastCheckoutStatus) {
    var bubble = createBotBubble();
    bubble.appendChild(renderCheckoutStatusBlock({ status: checkoutStatus }));
    appendBubble(bubble);
    TablePanel.refresh();
  }
  lastCheckoutStatus = checkoutStatus;

  var nps = data.nps;
  var npsStage = nps ? nps.stage : null;
  if (npsStage && npsStage !== lastNpsStage) {
    var npsBubble = createBotBubble();
    npsBubble.appendChild(renderBlock(nps));
    appendBubble(npsBubble);
  }
  lastNpsStage = npsStage;
}

function initStatusPolling() {
  // Backs off to a 60s safety net once MesioRealtime is connected —
  // check.updated/nps.updated events call pollDinerStatus() directly.
  mesioLiveInterval(function () { pollDinerStatus(); }, 6000);
}

function initDinerChat() {
  initComposer();
  initWaiterFab();
  initHeaderButtons();
  initJoinBanner();
  initErrorBanner();
  initTablePolling();
  initStatusPolling();
  startSession();
}

// Exposed so a debug session can re-run bootstrap after swapping out fetch —
// harmless in production, mirrors the DOMContentLoaded bootstrap.
window.initDinerChat = initDinerChat;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initDinerChat);
} else {
  initDinerChat();
}
