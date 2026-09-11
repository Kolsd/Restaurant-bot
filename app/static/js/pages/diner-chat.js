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
  restaurantName: '',
  tableLabel: '',
  currency: 'COP',
  locale: 'es-CO',
  cart: null,
  busy: false,
};

function el(id) { return document.getElementById(id); }

function getToken() { return state.token; }

function renderer() { return window.MesioCatalogRenderer; }

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
  var log = el('diner-log');
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
  var existing = el('diner-typing-indicator');
  if (existing) existing.remove();
}

function setBusy(busy) {
  state.busy = busy;
  var input = el('diner-input');
  var sendBtn = el('diner-send-btn');
  if (input) input.disabled = busy || !state.token;
  if (sendBtn) sendBtn.disabled = busy || !state.token;
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

function renderCartSummaryBlock(block) {
  var items = Array.isArray(block.items) ? block.items : [];
  var card = document.createElement('div');
  card.className = 'diner-cart-card';

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

  var viewBtn = document.createElement('button');
  viewBtn.type = 'button';
  viewBtn.className = 'm-btn m-btn--secondary m-btn--sm diner-cart-card-btn';
  viewBtn.textContent = 'Ver / editar pedido';
  viewBtn.addEventListener('click', function () { CartPanel.open(); });
  card.appendChild(viewBtn);

  return card;
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

function renderBlock(block) {
  if (!block || typeof block.type !== 'string') return null;
  switch (block.type) {
    case 'text': return renderTextBlock(block);
    case 'dish_cards': return renderDishCardsBlock(block);
    case 'category_chips': return renderCategoryChipsBlock(block);
    case 'cart_summary': return renderCartSummaryBlock(block);
    case 'payment_options': return renderPaymentOptionsBlock(block);
    case 'waiter_ack': return renderWaiterAckBlock(block);
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
    var input = el('diner-input');
    if (input) input.focus();
  }
}

/* ── Session bootstrap ────────────────────────────────────────────── */

function renderHeader() {
  var nameEl = el('diner-restaurant-name');
  var tableEl = el('diner-table-label');
  if (nameEl) nameEl.textContent = state.restaurantName || 'Mesio';
  if (tableEl) tableEl.textContent = state.tableLabel || '';
}

function showErrorBanner(msg) {
  var banner = el('diner-error-banner');
  var textEl = el('diner-error-text');
  if (!banner || !textEl) return;
  if (!msg) {
    banner.hidden = true;
    return;
  }
  textEl.textContent = msg;
  banner.hidden = false;
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

  renderHeader();
  return true;
}

async function startSession() {
  setBusy(true);
  showTyping();
  showErrorBanner(null);
  var tableId = DinerSession.getTableToken();

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
    var data = await DinerSession.fetch('/api/diner/session', 'POST', { table_id: tableId }, null);
    hideTyping();
    state.token = data.token || '';
    state.restaurantName = data.restaurant_name || '';
    state.tableLabel = data.table_name || '';
    state.currency = data.currency || 'COP';
    state.locale = data.locale || 'es-CO';
    if (!state.token) throw new Error('missing session token');
    DinerSession.save({
      token: state.token,
      tableId: tableId,
      restaurantName: state.restaurantName,
      tableLabel: state.tableLabel,
      currency: state.currency,
      locale: state.locale,
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
  var chip = el('cart-chip');
  var label = el('cart-chip-label');
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

/* ── Wiring ────────────────────────────────────────────────────────── */

function initComposer() {
  var form = el('diner-composer');
  var input = el('diner-input');
  if (!form || !input) return;
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = input.value;
    input.value = '';
    sendMessage(text);
  });
}

function initWaiterFab() {
  var btn = el('waiter-fab');
  if (btn) btn.addEventListener('click', function () { WaiterSheet.open(); });
}

function initHeaderButtons() {
  var menuBtn = el('btn-open-menu');
  if (menuBtn) menuBtn.addEventListener('click', function () { MenuPanel.open(); });
  var cartChip = el('cart-chip');
  if (cartChip) cartChip.addEventListener('click', function () { CartPanel.open(); });
}

function initErrorBanner() {
  var retryBtn = el('diner-retry-btn');
  if (retryBtn) retryBtn.addEventListener('click', startSession);
}

function initDinerChat() {
  initComposer();
  initWaiterFab();
  initHeaderButtons();
  initErrorBanner();
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
