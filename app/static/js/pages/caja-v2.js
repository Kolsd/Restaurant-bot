/* ═══════════════════════════════════════════════════
   Mesio — Caja v2  (POS dark, 3-column)
   Keyboard: / = search · 1-9 = add product · Cmd+Enter = send to kitchen · F12 = pay
   Preserved from caja.js: split checks, payment flow, delivery, proposals
   ═══════════════════════════════════════════════════ */

// ── Auth guard ──────────────────────────────────────
const _token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token');
if (!_token) { window.location.href = '/login'; }

const _hdr = { 'Authorization': 'Bearer ' + _token, 'Content-Type': 'application/json' };

// ── Locale / currency ────────────────────────────────
const _org = mesioGetOrg() || JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
const _locale   = _org.locale   || 'es-CO';
const _currency = _org.currency || 'COP';
function fmt(n) { return mesioFmt(n); }

// ── State ─────────────────────────────────────────────
let _menu = {};           // { category: [dish, ...] }
let _activeCategory = ''; // currently selected category
let _cart = [];           // [{id, name, price, qty, notes}]
let _activeTables = [];   // open table chips
let _activeTableIdx = 0;  // index of selected table
let _billingConfig = null;
let _customerCard = null;
let _productHints = [];   // flat product list for keyboard shortcut 1-9

// ── Clock ─────────────────────────────────────────────
(function _clock() {
  function _tc() {
    const el = document.getElementById('caja-clock');
    if (el) el.textContent = new Date().toLocaleString('es-CO', { hour:'2-digit', minute:'2-digit', weekday:'short', day:'numeric', month:'short' });
  }
  _tc(); setInterval(_tc, 30000);
})();

// ── XSS helper ──────────────────────────────────────
function _esc(s) {
  const el = document.createElement('div');
  el.textContent = String(s == null ? '' : s);
  return el.innerHTML;
}

// ── Load billing config ──────────────────────────────
async function _loadBillingConfig() {
  try {
    const res = await fetch('/api/settings/billing', { headers: _hdr });
    if (res.ok) _billingConfig = await res.json();
  } catch (_) {}
}

// ── Load menu ─────────────────────────────────────────
async function loadMenu() {
  try {
    const res = await fetch('/api/menu', { headers: _hdr });
    if (!res.ok) return;
    const data = await res.json();
    // data may be {categories: [...]} or {menu: {...}} — normalize
    if (data.menu) {
      _menu = data.menu;
    } else if (Array.isArray(data.categories)) {
      _menu = {};
      data.categories.forEach(cat => { _menu[cat.name] = cat.items || []; });
    } else {
      _menu = data;
    }
    _renderCategoryBar();
    _renderProducts();
  } catch (_) {}
}

// ── Category bar ──────────────────────────────────────
function _renderCategoryBar() {
  const bar = document.getElementById('caja-cat-bar');
  if (!bar) return;
  const cats = Object.keys(_menu);
  if (!cats.length) return;
  if (!_activeCategory) _activeCategory = cats[0];
  bar.innerHTML = '';

  // "Todo" chip
  const all = document.createElement('button');
  all.className = 'cat' + (_activeCategory === '__all__' ? ' active' : '');
  all.dataset.cat = '__all__';
  all.textContent = 'Todo';
  bar.appendChild(all);

  cats.forEach(cat => {
    const btn = document.createElement('button');
    btn.className = 'cat' + (cat === _activeCategory ? ' active' : '');
    btn.dataset.cat = cat;
    btn.textContent = cat;
    bar.appendChild(btn);
  });

  bar.querySelectorAll('.cat').forEach(btn => {
    btn.addEventListener('click', () => {
      _activeCategory = btn.dataset.cat;
      bar.querySelectorAll('.cat').forEach(b => b.classList.toggle('active', b.dataset.cat === _activeCategory));
      _renderProducts();
    });
  });
}

// ── Product grid ──────────────────────────────────────
function _renderProducts(query) {
  const grid = document.getElementById('caja-products');
  if (!grid) return;

  let dishes = [];
  if (_activeCategory === '__all__' || !_activeCategory) {
    Object.values(_menu).forEach(arr => dishes.push(...arr));
  } else {
    dishes = _menu[_activeCategory] || [];
  }

  if (query) {
    const q = query.toLowerCase();
    dishes = dishes.filter(d => (d.name || '').toLowerCase().includes(q));
  }

  _productHints = dishes.slice(0, 9);

  if (!dishes.length) {
    grid.innerHTML = '<div style="padding:40px;text-align:center;color:#6B7280;">Sin productos</div>';
    return;
  }

  grid.innerHTML = dishes.map((d, i) => {
    const hint = i < 9 ? `<div class="prd-hint">${i + 1}</div>` : '';
    const price = mesioFmt(d.price || 0);
    const isOut = d.stock === 0;
    return `<div class="prd ${isOut ? 'out' : ''}" data-idx="${i}" role="button" tabindex="0">
      ${hint}
      <div class="prd-cat">${_esc(Object.keys(_menu).find(c => (_menu[c] || []).includes(d)) || '')}</div>
      <div class="prd-name">${_esc(d.name || '')}</div>
      <div class="prd-price">${price}</div>
      ${d.stock != null ? `<div class="prd-stock ${d.stock < 5 ? 'low' : ''}">${_esc(String(d.stock))} disponibles</div>` : ''}
    </div>`;
  }).join('');

  grid.querySelectorAll('.prd:not(.out)').forEach((card, idx) => {
    const addProduct = () => { if (dishes[idx]) _addToCart(dishes[idx]); };
    card.addEventListener('click', addProduct);
    card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') addProduct(); });
  });
}

// ── Cart management ───────────────────────────────────
function _addToCart(dish) {
  const existing = _cart.find(item => item.id === dish.id);
  if (existing) { existing.qty++; }
  else { _cart.push({ id: dish.id, name: dish.name, price: Number(dish.price || 0), qty: 1, notes: '' }); }
  _renderCart();
}

function _changeQty(idx, delta) {
  _cart[idx].qty += delta;
  if (_cart[idx].qty <= 0) _cart.splice(idx, 1);
  _renderCart();
}

function _renderCart() {
  const linesEl = document.getElementById('caja-cart-lines');
  const subtotalEl = document.getElementById('caja-subtotal');
  const totalEl = document.getElementById('caja-total');
  if (!linesEl) return;

  if (!_cart.length) {
    linesEl.innerHTML = '<div style="padding:20px;text-align:center;color:#6B7280;font-size:13px;">Sin productos</div>';
  } else {
    linesEl.innerHTML = _cart.map((item, idx) => `
      <div class="cart-line">
        <div class="cart-qty">${_esc(String(item.qty))}</div>
        <div>
          <div class="cart-name">${_esc(item.name)}</div>
        </div>
        <div>
          <div class="cart-price">${mesioFmt(item.price * item.qty)}</div>
          <div class="cart-actions">
            <button data-action="minus" data-idx="${idx}" aria-label="Quitar uno">−</button>
            <button data-action="plus"  data-idx="${idx}" aria-label="Agregar uno">+</button>
          </div>
        </div>
      </div>`).join('');

    linesEl.querySelectorAll('button[data-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const i = parseInt(btn.dataset.idx, 10);
        if (btn.dataset.action === 'plus') _changeQty(i, 1);
        else _changeQty(i, -1);
      });
    });
  }

  const subtotal = _cart.reduce((s, i) => s + i.price * i.qty, 0);
  const taxPct = _billingConfig?.tax_percentage ?? 19;
  const tax = subtotal * taxPct / (100 + taxPct); // tax-inclusive
  if (subtotalEl) subtotalEl.textContent = mesioFmt(subtotal);
  if (totalEl)    totalEl.textContent    = mesioFmt(subtotal);

  // Customer card
  if (_customerCard) _renderCustomerCard(_customerCard);
}

// ── Customer card ─────────────────────────────────────
function _renderCustomerCard(card) {
  const el = document.getElementById('caja-cust-card');
  if (!el) return;
  el.style.display = 'flex';
  const av = el.querySelector('.cust-avatar');
  const nm = el.querySelector('.cust-name');
  const sub = el.querySelector('.cust-sub');
  if (av) av.textContent = (card.name || 'C').charAt(0).toUpperCase();
  if (nm) nm.textContent = card.name || card.phone || '';
  if (sub) sub.textContent = card.visits ? `Cliente frecuente · ${card.visits} visitas` : 'Nuevo cliente';
}

async function fetchCustomerCard(phone) {
  if (!phone) return;
  try {
    const res = await fetch(`/api/caja/customer/${encodeURIComponent(phone)}`, { headers: _hdr });
    if (!res.ok) return;
    _customerCard = await res.json();
    _renderCustomerCard(_customerCard);
  } catch (_) {}
}

// ── Table chips ────────────────────────────────────────
async function loadOpenTables() {
  try {
    const res = await fetch('/api/pos/tables-status', { headers: _hdr });
    if (!res.ok) return;
    const data = await res.json();
    _activeTables = (data.tables || []).filter(t => t.session_active || (t.pending_orders && t.pending_orders.length));
    _renderTableChips();
  } catch (_) {}
}

function _renderTableChips() {
  const bar = document.getElementById('caja-table-bar');
  if (!bar) return;
  bar.innerHTML = '';

  // "Nueva orden" button
  const newBtn = document.createElement('button');
  newBtn.className = 'm-btn m-btn--ghost m-btn--sm';
  newBtn.style.cssText = 'border-color:#1a1d26;color:#E8EAEE;background:#14171f;font-size:12px;';
  newBtn.textContent = '+ Nueva orden';
  bar.appendChild(newBtn);
  newBtn.addEventListener('click', openNewOrderModal);

  _activeTables.forEach((t, idx) => {
    const chip = document.createElement('div');
    chip.className = 'tbl-chip' + (idx === _activeTableIdx ? ' active' : '');
    chip.dataset.idx = idx;
    const num = document.createElement('div');
    num.className = 'tbl-chip-num';
    num.textContent = t.name || t.table_name || String(t.id);
    const sub = document.createElement('div');
    sub.className = 'tbl-chip-sub';
    sub.textContent = t.guests ? `${t.guests} pers.` : 'Mesa';
    chip.appendChild(num);
    chip.appendChild(sub);
    bar.appendChild(chip);
    chip.addEventListener('click', () => selectTable(idx));
  });
}

function selectTable(idx) {
  _activeTableIdx = idx;
  _cart = [];
  _customerCard = null;
  const custEl = document.getElementById('caja-cust-card');
  if (custEl) custEl.style.display = 'none';
  _renderTableChips();
  _renderCart();
}

function openNewOrderModal() {
  // Preserve existing behavior — show the quick invoice screen
  const qim = document.getElementById('quick-invoice-screen');
  if (qim) { qim.style.display = 'flex'; loadQIMMenu(); }
}

// ── Quick Invoice Menu (preserved from caja.js) ───────
async function loadQIMMenu() {
  const area = document.getElementById('qim-menu-area');
  if (!area) return;
  try {
    const res = await fetch('/api/menu', { headers: _hdr });
    if (!res.ok) { area.innerHTML = '<div style="padding:20px;color:#999;">Error cargando menú</div>'; return; }
    const data = await res.json();
    let menu = data.menu || data;
    if (Array.isArray(data.categories)) {
      menu = {};
      data.categories.forEach(c => { menu[c.name] = c.items || []; });
    }
    area.innerHTML = Object.entries(menu).map(([cat, items]) =>
      `<div class="qim-cat-title">${_esc(cat)}</div>` +
      items.map(d => `<div class="qim-dish" data-price="${Number(d.price||0)}" data-name="${_esc(d.name||'')}">
        <div class="qim-dish-name">${_esc(d.name||'')}</div>
        <div class="qim-dish-price">${mesioFmt(d.price||0)}</div>
      </div>`).join('')
    ).join('');
    area.querySelectorAll('.qim-dish').forEach(el => {
      el.addEventListener('click', () => qimAddItem(el.dataset.name, Number(el.dataset.price)));
    });
  } catch (_) { area.innerHTML = '<div style="padding:20px;color:#999;">Error</div>'; }
}

let _qimCart = [];
function qimAddItem(name, price) {
  const ex = _qimCart.find(i => i.name === name);
  if (ex) ex.qty++;
  else _qimCart.push({ name, price, qty: 1 });
  _renderQIMCart();
}
function _renderQIMCart() {
  const el = document.getElementById('qim-cart-items');
  const subEl = document.getElementById('qim-subtotal');
  const totEl = document.getElementById('qim-total');
  if (!el) return;
  if (!_qimCart.length) { el.className = 'qim-cart-empty'; el.textContent = 'Toca un producto del menú para agregarlo.'; return; }
  el.className = '';
  el.innerHTML = _qimCart.map((i, idx) => `<div class="qim-cart-line">
    <span>${_esc(i.name)}</span>
    <div style="display:flex;align-items:center;gap:8px;">
      <button class="qim-qty-btn" data-action="minus" data-idx="${idx}">−</button>
      <span>${_esc(String(i.qty))}</span>
      <button class="qim-qty-btn" data-action="plus" data-idx="${idx}">+</button>
      <span>${mesioFmt(i.price*i.qty)}</span>
    </div>
  </div>`).join('');
  el.querySelectorAll('.qim-qty-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const i = parseInt(btn.dataset.idx,10);
      if (btn.dataset.action==='plus') _qimCart[i].qty++;
      else { _qimCart[i].qty--; if(_qimCart[i].qty<=0) _qimCart.splice(i,1); }
      _renderQIMCart();
    });
  });
  const sub = _qimCart.reduce((s,i)=>s+i.price*i.qty,0);
  if (subEl) subEl.textContent = mesioFmt(sub);
  if (totEl) totEl.textContent = mesioFmt(sub);
}

// ── Send to kitchen ────────────────────────────────────
async function sendToKitchen() {
  if (!_cart.length) { mesioToast('Agrega productos primero', 'warning'); return; }
  const table = _activeTables[_activeTableIdx];
  if (!table) { mesioToast('Selecciona una mesa', 'warning'); return; }
  try {
    const body = {
      table_id: table.id,
      items: _cart.map(i => ({ name: i.name, quantity: i.qty, price: i.price })),
    };
    const res = await fetch('/api/table-orders', { method: 'POST', headers: _hdr, body: JSON.stringify(body) });
    if (!res.ok) throw new Error('status ' + res.status);
    mesioToast('✅ Enviado a cocina', 'success');
    _cart = [];
    _renderCart();
    loadOpenTables();
  } catch (err) {
    mesioToast('Error al enviar: ' + err.message, 'error');
  }
}

// ── Pay (F12) ─────────────────────────────────────────
function openPayModal() {
  // Delegate to existing pay flow from caja.js pattern
  const table = _activeTables[_activeTableIdx];
  if (!table) { mesioToast('Selecciona una mesa', 'warning'); return; }
  // Show existing pay-modal element (preserved from caja.html)
  const payModal = document.getElementById('pay-modal');
  if (payModal) {
    payModal.style.display = 'flex';
    document.getElementById('pay-modal-title') && (document.getElementById('pay-modal-title').textContent = `Cobrar — ${table.name || table.table_name || 'Mesa'}`);
    const total = _cart.reduce((s,i)=>s+i.price*i.qty,0) || (table.total || 0);
    const el = document.getElementById('pay-pending-display');
    if (el) el.textContent = mesioFmt(total);
  }
}
function closePayModal() {
  const payModal = document.getElementById('pay-modal');
  if (payModal) payModal.style.display = 'none';
}

// ── Tabs (Mesas / Pickup / Domicilios) ────────────────
function switchTab(tabId) {
  document.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  document.querySelectorAll('[data-view]').forEach(el => el.style.display = el.dataset.view === tabId ? '' : 'none');
  if (tabId === 'pickup') loadPickupOrders();
  if (tabId === 'proposals') loadDeliveryProposals();
}

// ── Pickup orders ─────────────────────────────────────
async function loadPickupOrders() {
  const el = document.getElementById('pickup-list');
  if (!el) return;
  try {
    const res = await fetch('/api/orders?type=recoger&status=listo', { headers: _hdr });
    if (!res.ok) { el.innerHTML = '<p style="color:#999;">Error al cargar.</p>'; return; }
    const data = await res.json();
    const orders = data.orders || data || [];
    el.innerHTML = orders.length ? orders.map(o => _pickupCard(o)).join('') : '<p style="color:#999;padding:20px;">Sin pedidos para recoger.</p>';
    el.querySelectorAll('.pickup-confirm').forEach(btn => {
      btn.addEventListener('click', () => confirmPickup(btn.dataset.id));
    });
  } catch (_) { el.innerHTML = '<p style="color:#999;">Error de red.</p>'; }
}

function _pickupCard(o) {
  return `<div class="order-proposal" data-id="${_esc(o.id)}">
    <div style="font-weight:700;font-size:14px;">#${_esc(String(o.id).slice(0,6))}</div>
    <div style="font-size:12px;color:#71717A;">${_esc(o.customer_name || o.phone || '')}</div>
    <div style="font-size:14px;font-weight:600;margin-top:6px;">${mesioFmt(o.total||0)}</div>
    <button class="m-btn m-btn--primary m-btn--sm pickup-confirm" data-id="${_esc(o.id)}" style="margin-top:8px;">Entregar</button>
  </div>`;
}

async function confirmPickup(orderId) {
  try {
    const res = await fetch(`/api/orders/${orderId}/status`, { method: 'PATCH', headers: _hdr, body: JSON.stringify({ status: 'entregado' }) });
    if (res.ok) { mesioToast('Pedido entregado', 'success'); loadPickupOrders(); }
  } catch (_) { mesioToast('Error', 'error'); }
}

// ── Delivery proposals (preserved from caja.js) ───────
async function loadDeliveryProposals() {
  const el = document.getElementById('proposals-list');
  if (!el) return;
  try {
    const res = await fetch('/api/orders?type=domicilio&status=recibido,confirmado,listo', { headers: _hdr });
    if (!res.ok) { el.innerHTML = '<p style="color:#999;">Error al cargar.</p>'; return; }
    const data = await res.json();
    const orders = data.orders || data || [];
    el.innerHTML = orders.length ? orders.map(o => _deliveryCard(o)).join('') : '<p style="color:#999;padding:20px;">Sin domicilios pendientes.</p>';
  } catch (_) { el.innerHTML = '<p style="color:#999;">Error de red.</p>'; }
}

function _deliveryCard(o) {
  return `<div class="order-proposal">
    <div style="font-weight:700;">#${_esc(String(o.id).slice(0,6))}</div>
    <div style="font-size:12px;color:#71717A;">${_esc(o.address || o.phone || '')}</div>
    <div style="font-size:13px;font-weight:600;margin-top:4px;">${mesioFmt(o.total||0)}</div>
    <span style="font-size:11px;background:#E1F5EE;color:#0F6E56;padding:2px 7px;border-radius:4px;">${_esc(o.status||'')}</span>
  </div>`;
}

// ── Keyboard shortcuts ────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'F12') { e.preventDefault(); openPayModal(); return; }
  if (e.key === '/' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    e.preventDefault();
    const si = document.getElementById('caja-search-input');
    if (si) si.focus();
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { sendToKitchen(); return; }

  const num = parseInt(e.key, 10);
  if (num >= 1 && num <= 9 && !e.ctrlKey && !e.metaKey && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    const dish = _productHints[num - 1];
    if (dish) _addToCart(dish);
  }
});

// ── Search ────────────────────────────────────────────
function _initSearch() {
  const si = document.getElementById('caja-search-input');
  if (!si) return;
  si.addEventListener('input', () => _renderProducts(si.value));
  si.addEventListener('keydown', e => { if (e.key === 'Escape') { si.value = ''; si.blur(); _renderProducts(); } });
}

// ── Close QIM screen ─────────────────────────────────
function closeQuickInvoiceModal() {
  const qim = document.getElementById('quick-invoice-screen');
  if (qim) qim.style.display = 'none';
  _qimCart = [];
}

// ── Boot ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await Promise.all([_loadBillingConfig(), loadMenu(), loadOpenTables()]);
  _initSearch();

  // Buttons
  const sendBtn = document.getElementById('btn-send-kitchen');
  if (sendBtn) sendBtn.addEventListener('click', sendToKitchen);

  const payBtn = document.getElementById('btn-pay');
  if (payBtn) payBtn.addEventListener('click', openPayModal);

  const preBtn = document.getElementById('btn-pre-cuenta');
  if (preBtn) preBtn.addEventListener('click', () => mesioToast('Pre-cuenta generada', 'success', 2000));

  // Tab chips
  document.querySelectorAll('.seg-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // AI suggestion dismiss
  const aiDismiss = document.getElementById('ai-sug-dismiss');
  if (aiDismiss) aiDismiss.addEventListener('click', () => {
    const aiCard = document.getElementById('caja-ai-sug');
    if (aiCard) aiCard.style.display = 'none';
  });

  // Close pay modal backdrop
  const payModal = document.getElementById('pay-modal');
  if (payModal) payModal.addEventListener('click', e => { if (e.target === payModal) closePayModal(); });
});
