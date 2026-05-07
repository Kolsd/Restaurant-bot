/* ═══════════════════════════════════════════════════
   Mesio — Caja v2  (POS dark, 3-column)
   Keyboard: / = search · 1-9 = add product · Cmd+Enter = send to kitchen · F12 = pay
   ═══════════════════════════════════════════════════ */

// ── Auth guard ──────────────────────────────────────
const _token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token');
if (!_token) { window.location.href = '/login'; }

// ── Locale / currency ────────────────────────────────
const _org = mesioGetOrg() || JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
const _locale   = _org.locale   || 'es-CO';
const _currency = _org.currency || 'COP';
const _taxPct   = 19;
function fmt(n) { return mesioFmt(n); }

// ── State ─────────────────────────────────────────────
let _menu = {};
let _activeCategory = '';
let _cart = [];
let _activeTables = [];
let _activeTableIdx = -1;
let _mesaGridMode = true;
let _billingConfig = null;
let _customerCard = null;
let _productHints = [];
let _currentTab = 'mesas';

// Active table session context
let _selectedTableOrder = null; // { base_order_id, table_name, table_id, total }
let _checks = [];               // list of checks for selected table order

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
    const res = await fetch('/api/billing/config', { headers: mesioHeaders() });
    if (res.ok) _billingConfig = await res.json();
  } catch (_) {}
}

// ── Load restaurant settings (feature flags) ─────────
// Populates rb_restaurant in localStorage so mesioFeatureEnabled() works.
async function _loadRestaurantSettings() {
  try {
    const res = await fetch('/api/settings', { headers: mesioHeaders() });
    if (res.ok) {
      const data = await res.json();
      localStorage.setItem('rb_restaurant', JSON.stringify(data));
    }
  } catch (_) {}
}

// ── Load menu ─────────────────────────────────────────
async function loadMenu() {
  try {
    const res = await fetch('/api/pos/menu', { headers: mesioHeaders() });
    if (!res.ok) return;
    const data = await res.json();
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
  if (subtotalEl) subtotalEl.textContent = mesioFmt(subtotal);
  if (totalEl)    totalEl.textContent    = mesioFmt(subtotal);
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
    const res = await fetch(`/api/caja/customer/${encodeURIComponent(phone)}`, { headers: mesioHeaders() });
    if (!res.ok) return;
    _customerCard = await res.json();
    _renderCustomerCard(_customerCard);
  } catch (_) {}
}

// ── Table chips ────────────────────────────────────────
async function loadOpenTables() {
  try {
    const res = await fetch('/api/pos/tables-status', { headers: mesioHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    _activeTables = data.tables || [];
    if (_mesaGridMode) _renderMesaGrid();
    else _renderTableChips();
  } catch (_) {}
}
// ── Mesa grid mode helpers ─────────────────────────────
function _tableStateCaja(t) {
  if (t.has_waiter_alert) return { cls: 'alert',   label: 'Llamó al mesero' };
  if (!(t.session_active || t.bot_active)) return { cls: 'free', label: 'Libre' };
  if (t.has_open_check) return { cls: 'billing', label: 'Facturando' };
  if ((t.pending_orders || []).some(s => s === 'listo')) return { cls: 'active', label: 'Comiendo' };
  if ((t.pending_orders || []).length) return { cls: 'active', label: 'En cocina' };
  return { cls: 'active', label: 'Activa' };
}
function _tableBorderColor(t) {
  if (t.has_waiter_alert) return '#EF4444';
  if (!(t.session_active || t.bot_active)) return '#1a1d26';
  if (t.has_open_check) return 'rgba(167,139,250,.5)';
  return 'rgba(29,158,117,.5)';
}
function _tableStatusColor(t) {
  if (t.has_waiter_alert) return '#EF4444';
  if (!(t.session_active || t.bot_active)) return '#6B7280';
  if (t.has_open_check) return '#a78bfa';
  return '#1d9e75';
}

function _enterMesaGrid() {
  _mesaGridMode = true;
  const bar = document.getElementById('caja-table-bar');
  if (bar) { bar.classList.add('mesa-grid-mode'); bar.style.display = ''; }
  const searchRow = document.querySelector('.search-row');
  const catBar    = document.querySelector('.cat-bar');
  if (searchRow) searchRow.style.display = 'none';
  if (catBar)    catBar.style.display    = 'none';
  document.querySelectorAll('[data-view]').forEach(el => el.style.display = 'none');
  _renderMesaGrid();
}

function _exitMesaGrid(idx) {
  _mesaGridMode = false;
  const bar = document.getElementById('caja-table-bar');
  if (bar) bar.classList.remove('mesa-grid-mode');
  const searchRow = document.querySelector('.search-row');
  const catBar    = document.querySelector('.cat-bar');
  const mesasView = document.querySelector('[data-view="mesas"]');
  if (searchRow) searchRow.style.display = '';
  if (catBar)    catBar.style.display    = '';
  if (mesasView) mesasView.style.display = 'flex';
  selectTable(idx);
}

function _renderMesaGrid() {
  const bar = document.getElementById('caja-table-bar');
  if (!bar) return;
  bar.innerHTML = '';

  const grid = document.createElement('div');
  grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;width:100%;';

  if (!_activeTables.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'grid-column:1/-1;padding:40px;text-align:center;color:#6B7280;font-size:13px;';
    empty.textContent = 'No hay mesas configuradas. Configúralas desde el panel de administración.';
    grid.appendChild(empty);
  } else {
    _activeTables.forEach((t, idx) => {
      const { cls, label } = _tableStateCaja(t);
      const tile = document.createElement('div');
      tile.className = 'mesa-tile' + (cls !== 'free' ? ' t-' + (cls === 'alert' ? 'alert' : cls === 'billing' ? 'billing' : 'active') : '');

      const name = document.createElement('div');
      name.style.cssText = 'font-family:var(--font-display);font-weight:700;font-size:20px;color:#E8EAEE;';
      name.textContent = t.name || t.table_name || String(t.id);

      const statusEl = document.createElement('div');
      statusEl.style.cssText = 'font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:' + _tableStatusColor(t) + ';';
      statusEl.textContent = label;

      tile.appendChild(name);
      tile.appendChild(statusEl);

      if (t.current_total) {
        const totalEl = document.createElement('div');
        totalEl.style.cssText = 'font-family:var(--font-display);font-size:14px;color:var(--brand);font-weight:700;margin-top:6px;';
        totalEl.textContent = mesioFmt(t.current_total);
        tile.appendChild(totalEl);
      }
      if (t.has_waiter_alert) {
        const alertEl = document.createElement('div');
        alertEl.style.cssText = 'font-size:10px;color:#EF4444;font-weight:600;';
        alertEl.textContent = '🔔 Llamó';
        tile.appendChild(alertEl);
      }
      if (t.guests) {
        const guestEl = document.createElement('div');
        guestEl.style.cssText = 'font-size:10px;color:#6B7280;margin-top:auto;';
        guestEl.textContent = t.guests + ' pers.';
        tile.appendChild(guestEl);
      }

      tile.addEventListener('click', () => _exitMesaGrid(idx));
      grid.appendChild(tile);
    });
  }
  bar.appendChild(grid);
}

// ── Table chips (POS mode — single selected table) ──────
function _renderTableChips() {
  const bar = document.getElementById('caja-table-bar');
  if (!bar) return;
  bar.innerHTML = '';

  const backBtn = document.createElement('button');
  backBtn.className = 'm-btn m-btn--ghost m-btn--sm';
  backBtn.style.cssText = 'border-color:#1a1d26;color:#9CA3AF;background:#14171f;font-size:11px;flex-shrink:0;white-space:nowrap;';
  backBtn.textContent = '⬅ Mesas';
  backBtn.addEventListener('click', () => {
    _cart = [];
    _selectedTableOrder = null;
    _checks = [];
    _renderCart();
    _enterMesaGrid();
  });
  bar.appendChild(backBtn);

  const t = _activeTables[_activeTableIdx];
  if (t) {
    const chip = document.createElement('div');
    chip.className = 'tbl-chip active';
    const num = document.createElement('div');
    num.className = 'tbl-chip-num';
    num.textContent = t.name || t.table_name || String(t.id);
    const sub = document.createElement('div');
    sub.className = 'tbl-chip-sub';
    sub.textContent = _tableStateCaja(t).label;
    chip.appendChild(num);
    chip.appendChild(sub);
    bar.appendChild(chip);
  }
}
function selectTable(idx) {
  _activeTableIdx = idx;
  _cart = [];
  _selectedTableOrder = null;
  _checks = [];
  _customerCard = null;
  const custEl = document.getElementById('caja-cust-card');
  if (custEl) custEl.style.display = 'none';
  _renderTableChips();
  _renderCart();
  const lbl = document.getElementById('cart-table-label');
  const t = _activeTables[idx];
  if (lbl && t) lbl.textContent = t.name || t.table_name || `Mesa ${t.id}`;
  // Auto-load the mesa's active order so caja sees what's on the table
  // immediately after selecting it (PM feedback: 'Comanda Sin productos'
  // even when customer had ordered via bot). Read-only preview — F12
  // 'Cobrar' uses _selectedTableOrder.base_order_id to open the pay modal.
  if (t) _loadActiveOrderForTable(t).catch(e => console.warn('caja: load mesa order failed', e));
}

async function _loadActiveOrderForTable(table) {
  if (!table || !table.id) return;
  try {
    const url = `/api/table-orders?table_id=${encodeURIComponent(table.id)}`;
    const res = await fetch(url, { headers: mesioHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    const all = (data.orders || []);
    const active = all.find(o => o.base_order_id && !['factura_entregada','cancelado','closed'].includes(o.status));
    if (!active) return;
    const items = Array.isArray(active.items) ? active.items : [];
    if (!items.length) return;
    // Populate _cart from the existing order items so the comanda panel
    // and totals reflect the real mesa state. Caja can still tweak +/−
    // if they want to add extras to the current order before cobrar.
    _cart = items.map((it, i) => ({
      id: it.id || ('mesa-' + i),
      name: it.name || '—',
      price: Number(it.price || it.unit_price || 0),
      qty: Number(it.quantity || it.qty || 1),
      notes: it.notes || '',
      _from_mesa: true,
    }));
    _selectedTableOrder = {
      base_order_id: active.base_order_id,
      table_name: table.name || `Mesa ${table.id}`,
      table_id: table.id,
      status: active.status,
    };
    _renderCart();
  } catch (_) { /* non-critical */ }
}
function openNewOrderModal() {
  const qim = document.getElementById('quick-invoice-screen');
  if (qim) { qim.style.display = 'flex'; loadQIMMenu(); }
}

// ── Quick Invoice Menu ────────────────────────────────
async function loadQIMMenu() {
  const area = document.getElementById('qim-menu-area');
  if (!area) return;
  try {
    const res = await fetch('/api/pos/menu', { headers: mesioHeaders() });
    if (!res.ok) { area.innerHTML = '<div style="padding:20px;color:#999;">Error cargando menú</div>'; return; }
    const data = await res.json();
    let menu = data.menu || data;
    if (Array.isArray(data.categories)) {
      menu = {};
      data.categories.forEach(c => { menu[c.name] = c.items || []; });
    }
    area.innerHTML = '';
    for (const [cat, items] of Object.entries(menu)) {
      const catTitle = document.createElement('div');
      catTitle.className = 'qim-cat-title';
      catTitle.textContent = cat;
      area.appendChild(catTitle);
      for (const d of items) {
        const el = document.createElement('div');
        el.className = 'qim-dish';
        el.dataset.price = Number(d.price || 0);
        el.dataset.name = d.name || '';
        const nameDiv = document.createElement('div');
        nameDiv.className = 'qim-dish-name';
        nameDiv.textContent = d.name || '';
        const priceDiv = document.createElement('div');
        priceDiv.className = 'qim-dish-price';
        priceDiv.textContent = mesioFmt(d.price || 0);
        el.appendChild(nameDiv);
        el.appendChild(priceDiv);
        el.addEventListener('click', () => qimAddItem(el.dataset.name, Number(el.dataset.price)));
        area.appendChild(el);
      }
    }
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
  if (!_qimCart.length) {
    el.innerHTML = '<div style="padding:20px;text-align:center;color:#6B7280;font-size:13px;">Toca un producto para agregarlo.</div>';
    if (subEl) subEl.textContent = '—'; if (totEl) totEl.textContent = '—';
    return;
  }
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
    const total = _cart.reduce((s, i) => s + i.price * i.qty, 0);
    const body = {
      table_id: String(table.id),
      table_name: table.name || table.table_name || String(table.id),
      items: _cart.map(i => ({ name: i.name, quantity: i.qty, price: i.price })),
      total,
    };
    const res = await fetch('/api/pos/order', { method: 'POST', headers: mesioHeaders(), body: JSON.stringify(body) });
    if (!res.ok) throw new Error('status ' + res.status);
    mesioToast('Enviado a cocina', 'success');
    _cart = [];
    _renderCart();
    loadOpenTables();
  } catch (err) {
    mesioToast('Error al enviar: ' + err.message, 'error');
  }
}

// ── Tabs ──────────────────────────────────────────────
function switchTab(tabId) {
  _currentTab = tabId;
  document.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  const bar       = document.getElementById('caja-table-bar');
  const searchRow = document.querySelector('.search-row');
  const catBar    = document.querySelector('.cat-bar');
  if (tabId === 'mesas') {
    if (bar) bar.style.display = '';
    if (_mesaGridMode || _activeTableIdx < 0) { _enterMesaGrid(); return; }
    // POS mode — restore product view
    if (searchRow) searchRow.style.display = '';
    if (catBar)    catBar.style.display    = '';
    document.querySelectorAll('[data-view]').forEach(el => {
      el.style.display = el.dataset.view === 'mesas' ? 'flex' : 'none';
    });
  } else {
    // Non-mesas tabs: hide bar + search/cat (only relevant for mesas POS)
    if (bar) { bar.classList.remove('mesa-grid-mode'); bar.style.display = 'none'; }
    if (searchRow) searchRow.style.display = 'none';
    if (catBar)    catBar.style.display    = 'none';
    document.querySelectorAll('[data-view]').forEach(el => {
      el.style.display = el.dataset.view === tabId ? '' : 'none';
    });
    if (tabId === 'pickup')    loadPickupOrders();
    if (tabId === 'proposals') loadDeliveryProposals();
    if (tabId === 'chats')     loadChatsTab();
    if (tabId === 'nps')       loadRecentNpsTab();
  }
}

// ── Mesas — load pending session detail ───────────────
async function loadTableChecks(baseOrderId) {
  try {
    const res = await fetch(`/api/table-orders/${encodeURIComponent(baseOrderId)}/checks`, { headers: mesioHeaders() });
    if (!res.ok) return [];
    const data = await res.json();
    return data.checks || [];
  } catch (_) { return []; }
}

// ── Open pay modal with full check flow ───────────────
async function openPayModal() {
  const table = _activeTables[_activeTableIdx];
  if (!table) { mesioToast('Selecciona una mesa activa', 'warning'); return; }

  // Find a pending base_order_id for this table
  let baseOrderId = _selectedTableOrder?.base_order_id;
  if (!baseOrderId) {
    try {
      const res = await fetch(`/api/table-orders?table_id=${encodeURIComponent(table.id)}&status=recibido,en_preparacion,listo,entregado`, { headers: mesioHeaders() });
      if (res.ok) {
        const data = await res.json();
        const orders = data.orders || [];
        const active = orders.find(o => o.base_order_id && !['factura_entregada','cancelado'].includes(o.status));
        if (active) baseOrderId = active.base_order_id;
      }
    } catch (_) {}
  }
  if (!baseOrderId) {
    mesioToast('No hay una orden activa en esta mesa', 'warning');
    return;
  }

  // Load checks for this order
  _checks = await loadTableChecks(baseOrderId);
  _selectedTableOrder = { base_order_id: baseOrderId, table_name: table.name || `Mesa ${table.id}`, table_id: table.id };

  _openCheckModal(baseOrderId, table.name || `Mesa ${table.id}`);
}

// ── Check management modal ────────────────────────────
function _openCheckModal(baseOrderId, tableName) {
  let modal = document.getElementById('check-modal');
  if (!modal) {
    modal = _buildCheckModal();
    document.body.appendChild(modal);
  }
  modal.style.display = 'flex';
  _renderCheckModal(baseOrderId, tableName);
}

function _buildCheckModal() {
  const modal = document.createElement('div');
  modal.id = 'check-modal';
  modal.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,0.65);z-index:3100;align-items:center;justify-content:center;';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-labelledby', 'cm-title');
  modal.innerHTML = `
    <div style="background:#1a1d26;border-radius:16px;width:540px;max-width:96vw;max-height:88vh;overflow-y:auto;padding:0;box-shadow:0 24px 64px rgba(0,0,0,0.6);">
      <div style="padding:20px 24px;border-bottom:1px solid #252836;display:flex;align-items:center;justify-content:space-between;">
        <div>
          <div id="cm-title" style="font-size:16px;font-weight:700;color:#E8EAEE;"></div>
          <div id="cm-subtitle" style="font-size:12px;color:#9CA3AF;margin-top:2px;"></div>
        </div>
        <button id="cm-close" style="background:none;border:1px solid #343b4d;color:#9CA3AF;border-radius:8px;padding:7px 12px;cursor:pointer;font-family:inherit;font-size:13px;">✕</button>
      </div>
      <div id="cm-body" style="padding:20px 24px;"></div>
    </div>`;
  modal.querySelector('#cm-close').addEventListener('click', () => { modal.style.display = 'none'; });
  modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
  modal.addEventListener('keydown', e => { if (e.key === 'Escape' || e.key === 'Esc') modal.style.display = 'none'; });
  return modal;
}

function _renderCheckModal(baseOrderId, tableName) {
  const titleEl = document.getElementById('cm-title');
  const subtitleEl = document.getElementById('cm-subtitle');
  const body = document.getElementById('cm-body');
  if (titleEl) titleEl.textContent = `Cobrar — ${tableName}`;

  const open = _checks.filter(c => c.status === 'open');
  const paid = _checks.filter(c => c.status !== 'open');
  const totalAll = _checks.reduce((s, c) => s + Number(c.total || 0), 0);
  const totalPaid = paid.reduce((s, c) => s + Number(c.total || 0), 0);
  const remaining = totalAll - totalPaid;

  if (subtitleEl) subtitleEl.textContent = `Total mesa: ${fmt(totalAll)} · Cobrado: ${fmt(totalPaid)} · Pendiente: ${fmt(remaining)}`;

  if (!body) return;

  if (!_checks.length) {
    // No checks yet — show split options + direct pay
    body.innerHTML = `
      <div style="margin-bottom:20px;">
        <div style="font-size:13px;color:#9CA3AF;margin-bottom:14px;">La mesa no tiene checks divididos. Puedes cobrar el total directamente o dividir la cuenta.</div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
          <button id="cm-pay-full" style="flex:1;padding:12px 16px;background:var(--brand);color:#fff;border:none;border-radius:9px;font-weight:700;font-size:13px;cursor:pointer;font-family:inherit;">Cobrar total completo</button>
          <button id="cm-split" style="flex:1;padding:12px 16px;background:#14171f;border:1px solid #2a2f3d;color:#E8EAEE;border-radius:9px;font-weight:600;font-size:13px;cursor:pointer;font-family:inherit;">Dividir cuenta</button>
        </div>
      </div>`;
    body.querySelector('#cm-pay-full')?.addEventListener('click', () => openPayCheckForm(baseOrderId, null, remaining || totalAll));
    body.querySelector('#cm-split')?.addEventListener('click', () => openSplitModal(baseOrderId, tableName));
    return;
  }

  // Render checks list
  let html = '';
  _checks.forEach(chk => {
    const isPaid = chk.status !== 'open';
    const statusLabel = isPaid
      ? `<span style="font-size:10px;background:rgba(29,158,117,0.15);color:#4ADE9E;padding:2px 7px;border-radius:4px;font-weight:600;">Cobrado</span>`
      : `<span style="font-size:10px;background:rgba(245,158,11,0.15);color:#F59E0B;padding:2px 7px;border-radius:4px;font-weight:600;">Pendiente</span>`;
    const items = Array.isArray(chk.items) ? chk.items : (chk.items ? JSON.parse(chk.items) : []);
    const loyaltyDiscount = Number(chk.loyalty_discount_cop || 0);
    const loyaltyPoints = Number(chk.loyalty_redeemed_points || 0);
    const grossTotal = Number(chk.total || 0);
    const netTotal = Math.max(0, grossTotal - loyaltyDiscount);
    const totalDisplay = loyaltyDiscount > 0
      ? `<div style="text-align:right;"><div style="font-size:11px;color:#6B7280;text-decoration:line-through;">${fmt(grossTotal)}</div><div style="font-family:var(--font-display);font-weight:700;font-size:15px;color:var(--brand);">${fmt(netTotal)}</div></div>`
      : `<div style="font-family:var(--font-display);font-weight:700;font-size:15px;color:var(--brand);">${fmt(grossTotal)}</div>`;
    const loyaltyLine = loyaltyDiscount > 0
      ? `<div style="font-size:11.5px;color:#4ADE9E;margin-bottom:8px;font-weight:600;">Descuento puntos: -${fmt(loyaltyDiscount)} (${_esc(String(loyaltyPoints))} puntos)</div>`
      : '';
    html += `
      <div style="background:#12161f;border:1px solid #252836;border-radius:10px;padding:14px;margin-bottom:10px;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
          <div style="font-size:13px;font-weight:600;color:#E8EAEE;">Cuenta #${_esc(String(chk.check_number || chk.id))}</div>
          <div style="display:flex;align-items:center;gap:8px;">${statusLabel}${totalDisplay}</div>
        </div>
        <div style="font-size:11.5px;color:#6B7280;margin-bottom:${loyaltyLine ? '4px' : '10px'};">${items.map(it => `${_esc(String(it.qty || 1))}× ${_esc(it.name)}`).join(' · ')}</div>
        ${loyaltyLine}
        ${!isPaid ? `<button class="cm-pay-check" data-check-id="${_esc(chk.id)}" data-total="${netTotal}" style="width:100%;padding:9px;background:var(--brand);color:#fff;border:none;border-radius:8px;font-weight:700;font-size:13px;cursor:pointer;font-family:inherit;">Cobrar este check</button>` : ''}
      </div>`;
  });

  if (open.length === 0 && paid.length > 0) {
    html += `<div style="text-align:center;padding:16px;color:#4ADE9E;font-weight:600;">Todos los checks cobrados</div>`;
  }

  html += `<div style="margin-top:4px;"><button id="cm-split-btn" style="width:100%;padding:10px;background:#14171f;border:1px solid #2a2f3d;color:#9CA3AF;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;">Redistribuir / volver a dividir</button></div>`;

  body.innerHTML = html;
  body.querySelectorAll('.cm-pay-check').forEach(btn => {
    btn.addEventListener('click', () => openPayCheckForm(baseOrderId, btn.dataset.checkId, Number(btn.dataset.total)));
  });
  body.querySelector('#cm-split-btn')?.addEventListener('click', () => openSplitModal(baseOrderId, tableName));
}

// ── Pay a single check ─────────────────────────────────
function openPayCheckForm(baseOrderId, checkId, checkTotal) {
  // Resolve items for the invoice preview
  let items = [];
  if (checkId) {
    const chk = _checks.find(c => String(c.id) === String(checkId));
    if (chk) items = Array.isArray(chk.items) ? chk.items : [];
  } else {
    items = (_checks || []).flatMap(c => (Array.isArray(c.items) ? c.items : []));
  }

  let modal = document.getElementById('pay-check-modal');
  if (!modal) {
    modal = _buildPayCheckModal();
    document.body.appendChild(modal);
  }
  modal.dataset.baseOrderId = baseOrderId;
  modal.dataset.checkId = checkId || '';
  modal.dataset.checkTotal = String(checkTotal);
  modal.style.display = 'flex';
  _renderPayCheckModal(baseOrderId, checkId, checkTotal, items);
}

function _buildPayCheckModal() {
  const modal = document.createElement('div');
  modal.id = 'pay-check-modal';
  modal.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:3200;align-items:center;justify-content:center;padding:12px;';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-labelledby', 'pcm-title');
  modal.innerHTML = `
    <div style="background:#1a1d26;border-radius:16px;width:100%;max-width:920px;max-height:92vh;display:flex;flex-direction:column;box-shadow:0 24px 64px rgba(0,0,0,0.6);">
      <div style="padding:18px 24px;border-bottom:1px solid #252836;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;">
        <div id="pcm-title" style="font-size:16px;font-weight:700;color:#E8EAEE;">Cobrar Check</div>
        <button id="pcm-close" style="background:none;border:1px solid #343b4d;color:#9CA3AF;border-radius:8px;padding:7px 12px;cursor:pointer;font-family:inherit;font-size:13px;">✕</button>
      </div>
      <div style="display:flex;flex:1;min-height:0;overflow:hidden;">
        <div id="pcm-body" style="flex:1;min-width:0;padding:20px 24px;overflow-y:auto;border-right:1px solid #252836;"></div>
        <div id="pcm-invoice-preview" style="width:300px;flex-shrink:0;padding:20px 18px;overflow-y:auto;background:#0e1016;"></div>
      </div>
    </div>`;
  modal.querySelector('#pcm-close').addEventListener('click', () => { modal.style.display = 'none'; });
  modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
  modal.addEventListener('keydown', e => { if (e.key === 'Escape' || e.key === 'Esc') modal.style.display = 'none'; });
  return modal;
}

function _renderPayCheckModal(baseOrderId, checkId, checkTotal, items) {
  items = items || [];
  const body = document.getElementById('pcm-body');
  if (!body) return;

  const tipPresets = [0, 10, 15, 18, 20];

  body.innerHTML = `
    <div style="background:#12161f;border:1px solid rgba(29,158,117,0.25);padding:14px 18px;border-radius:10px;margin-bottom:18px;display:flex;justify-content:space-between;align-items:center;">
      <div>
        <div style="font-size:11px;color:#9FE1CB;font-weight:700;text-transform:uppercase;letter-spacing:1px;">Total del check</div>
        <div id="pcm-check-total" style="font-size:28px;font-weight:900;color:var(--brand);font-family:var(--font-display);margin-top:4px;">${fmt(checkTotal)}</div>
      </div>
      <div style="text-align:right;">
        <div style="font-size:11px;color:#9FE1CB;font-weight:700;text-transform:uppercase;letter-spacing:1px;">Cambio</div>
        <div id="pcm-change" style="font-size:22px;font-weight:900;color:#4ADE9E;font-family:var(--font-display);margin-top:4px;">$0</div>
      </div>
    </div>

    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:#9CA3AF;margin-bottom:8px;font-weight:600;">Propina</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;" id="pcm-tip-chips">
        ${tipPresets.map(p => `<button class="pcm-tip-preset" data-pct="${p}" style="padding:6px 12px;border-radius:7px;border:1px solid #2a2f3d;background:#14171f;color:#9CA3AF;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;">${p === 0 ? 'Sin propina' : p + '%'}</button>`).join('')}
        <button class="pcm-tip-preset" data-pct="custom" style="padding:6px 12px;border-radius:7px;border:1px solid #2a2f3d;background:#14171f;color:#9CA3AF;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;">Personalizado</button>
      </div>
      <div id="pcm-tip-custom-row" style="display:none;margin-bottom:8px;">
        <input id="pcm-tip-custom-input" type="number" min="0" placeholder="Valor propina" style="width:100%;background:#14171f;border:1px solid #2a2f3d;border-radius:8px;padding:9px 12px;color:#E8EAEE;font-family:inherit;font-size:13px;outline:none;">
      </div>
      <div id="pcm-tip-display" style="font-size:12px;color:#9FE1CB;font-weight:600;min-height:18px;"></div>
      <div id="pcm-tip-split-preview" style="margin-top:8px;font-size:11px;color:#9CA3AF;line-height:1.4;min-height:0;"></div>
    </div>

    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:#9CA3AF;margin-bottom:8px;font-weight:600;">Método de pago</div>
      <div id="pcm-payments-list" style="display:flex;flex-direction:column;gap:8px;">
        ${_paymentRowHtml(0)}
      </div>
      <button id="pcm-add-method" style="margin-top:8px;padding:7px 12px;background:#14171f;border:1px solid #2a2f3d;color:#9CA3AF;border-radius:7px;font-size:12px;cursor:pointer;font-family:inherit;">+ Agregar método</button>
    </div>

    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:#9CA3AF;margin-bottom:8px;font-weight:600;">Cargo de servicio (opcional)</div>
      <input id="pcm-service-charge" type="number" min="0" placeholder="0" style="width:100%;background:#14171f;border:1px solid #2a2f3d;border-radius:8px;padding:9px 12px;color:#E8EAEE;font-family:inherit;font-size:13px;outline:none;">
    </div>

    ${mesioFeatureEnabled('dian_enabled') ? `
    <div style="margin-bottom:20px;">
      <div style="font-size:12px;color:#9CA3AF;margin-bottom:8px;font-weight:600;">Datos del cliente (opcional, para factura)</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
        <input id="pcm-cust-name" type="text" placeholder="Nombre cliente" value="Consumidor Final" style="background:#14171f;border:1px solid #2a2f3d;border-radius:8px;padding:9px 12px;color:#E8EAEE;font-family:inherit;font-size:12px;outline:none;">
        <input id="pcm-cust-nit" type="text" placeholder="NIT / Cédula" value="222222222" style="background:#14171f;border:1px solid #2a2f3d;border-radius:8px;padding:9px 12px;color:#E8EAEE;font-family:inherit;font-size:12px;outline:none;">
      </div>
    </div>` : ''}

    <button id="pcm-submit" style="width:100%;padding:14px;background:var(--brand);color:#fff;border:none;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;font-family:inherit;">Cobrar</button>
    <div id="pcm-error" style="margin-top:10px;font-size:12px;color:#F87171;text-align:center;min-height:18px;"></div>`;

  // Tip chip logic
  let _tipPct = 0;
  let _tipCustom = 0;
  let _tipMode = 'pct'; // 'pct' | 'custom'

  function _getTipAmount() {
    const base = checkTotal + Number(document.getElementById('pcm-service-charge')?.value || 0);
    if (_tipMode === 'custom') return _tipCustom;
    return Math.round(base * _tipPct / 100);
  }

  function _renderInvoicePreview() {
    const preview = document.getElementById('pcm-invoice-preview');
    if (!preview) return;

    const tip = _getTipAmount();
    const svc = Number(document.getElementById('pcm-service-charge')?.value || 0);
    // tip is part of what the customer owes — must be included in the charged total
    const grand = checkTotal + svc + tip;
    const custName = document.getElementById('pcm-cust-name')?.value || 'Consumidor Final';
    const custNit  = document.getElementById('pcm-cust-nit')?.value  || '222222222';
    const tableName = _selectedTableOrder?.table_name || 'Mesa';
    const restName  = _org.name || 'Restaurante';
    const now = new Date();
    const dateStr = now.toLocaleString('es-CO', { day:'2-digit', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit' });

    const rows = document.querySelectorAll('.pcm-pay-row');
    const payMethods = Array.from(rows).map(r => ({
      method: r.querySelector('.pcm-pay-method')?.value || 'efectivo',
      amount: Number(r.querySelector('.pcm-pay-amount')?.value || 0),
    })).filter(p => p.amount > 0);

    const labelMap = { efectivo: 'Efectivo', tarjeta: 'Tarjeta', transferencia: 'Transferencia', nequi: 'Nequi', daviplata: 'Daviplata', otro: 'Otro' };

    const itemsHtml = items.length
      ? items.map(it => {
          const qty   = it.qty || it.quantity || 1;
          const price = it.price || it.unit_price || 0;
          const sub   = qty * price;
          return `<tr>
            <td style="padding:3px 0;color:#9CA3AF;font-size:11px;max-width:110px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${_esc(it.name)}</td>
            <td style="padding:3px 0;color:#6B7280;font-size:11px;text-align:center;">${qty}</td>
            <td style="padding:3px 0;color:#E8EAEE;font-size:11px;text-align:right;">${fmt(sub)}</td>
          </tr>`;
        }).join('')
      : `<tr><td colspan="3" style="color:#4B5563;font-size:11px;padding:6px 0;text-align:center;">sin ítems</td></tr>`;

    const payHtml = payMethods.length
      ? payMethods.map(p => `<div style="display:flex;justify-content:space-between;font-size:11px;color:#9CA3AF;margin-top:3px;"><span>${labelMap[p.method] || p.method}</span><span>${fmt(p.amount)}</span></div>`).join('')
      : `<div style="font-size:11px;color:#4B5563;">—</div>`;

    preview.innerHTML = `
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#4ADE9E;margin-bottom:14px;">Vista previa · Factura</div>
      <div style="background:#14171f;border-radius:10px;padding:16px;font-family:monospace,monospace;">
        <div style="text-align:center;margin-bottom:12px;">
          <div style="font-size:13px;font-weight:700;color:#E8EAEE;">${_esc(restName)}</div>
          <div style="font-size:10px;color:#6B7280;margin-top:2px;">${_esc(tableName)}</div>
          <div style="font-size:10px;color:#6B7280;">${dateStr}</div>
        </div>
        <div style="border-top:1px dashed #252836;margin:10px 0;"></div>
        <div style="font-size:10px;color:#9CA3AF;margin-bottom:4px;">Cliente: <span style="color:#E8EAEE;">${_esc(custName)}</span></div>
        <div style="font-size:10px;color:#9CA3AF;margin-bottom:10px;">NIT/CC: <span style="color:#E8EAEE;">${_esc(custNit)}</span></div>
        <div style="border-top:1px dashed #252836;margin:8px 0;"></div>
        <table style="width:100%;border-collapse:collapse;margin-bottom:4px;">
          <thead><tr>
            <th style="font-size:10px;color:#4B5563;text-align:left;padding-bottom:4px;font-weight:600;">Ítem</th>
            <th style="font-size:10px;color:#4B5563;text-align:center;padding-bottom:4px;font-weight:600;">Cant</th>
            <th style="font-size:10px;color:#4B5563;text-align:right;padding-bottom:4px;font-weight:600;">Valor</th>
          </tr></thead>
          <tbody>${itemsHtml}</tbody>
        </table>
        <div style="border-top:1px dashed #252836;margin:8px 0;"></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:#9CA3AF;margin-bottom:3px;"><span>Subtotal</span><span>${fmt(checkTotal)}</span></div>
        ${svc > 0 ? `<div style="display:flex;justify-content:space-between;font-size:11px;color:#9CA3AF;margin-bottom:3px;"><span>Cargo servicio</span><span>${fmt(svc)}</span></div>` : ''}
        ${tip > 0 ? `<div style="display:flex;justify-content:space-between;font-size:11px;color:#9CA3AF;margin-bottom:3px;"><span>Propina</span><span>${fmt(tip)}</span></div>` : ''}
        <div style="display:flex;justify-content:space-between;font-size:13px;font-weight:700;color:#4ADE9E;margin-top:6px;border-top:1px solid #252836;padding-top:6px;"><span>TOTAL</span><span>${fmt(grand)}</span></div>
        <div style="border-top:1px dashed #252836;margin:10px 0;"></div>
        <div style="font-size:10px;color:#6B7280;margin-bottom:4px;font-weight:600;">PAGO</div>
        ${payHtml}
      </div>`;
  }

  function _updateChange() {
    const tip = _getTipAmount();
    const svc = Number(document.getElementById('pcm-service-charge')?.value || 0);
    const rows = document.querySelectorAll('.pcm-pay-row');
    const paid = Array.from(rows).reduce((s, row) => s + (Number(row.querySelector('.pcm-pay-amount')?.value || 0)), 0);
    // tip is part of what the customer owes — must be included so cambio reflects the real due amount
    const total = checkTotal + svc + tip;
    const change = paid - total;
    const changeEl = document.getElementById('pcm-change');
    if (changeEl) changeEl.textContent = change >= 0 ? fmt(change) : `−${fmt(Math.abs(change))}`;
    const tipDisp = document.getElementById('pcm-tip-display');
    if (tipDisp) tipDisp.textContent = tip > 0 ? `Propina: ${fmt(tip)}` : '';
    _renderInvoicePreview();
    _scheduleTipSplitPreview(tip);
  }

  // Debounced tip-split preview (300ms) — calls /api/staff/tips/preview
  // and renders a compact breakdown of how the tip will be distributed
  // across staff currently on shift.
  let _tipPreviewTimer = null;
  let _tipPreviewSeq = 0;
  function _scheduleTipSplitPreview(tipAmount) {
    if (_tipPreviewTimer) {
      clearTimeout(_tipPreviewTimer);
      _tipPreviewTimer = null;
    }
    const previewEl = document.getElementById('pcm-tip-split-preview');
    if (!previewEl) return;
    if (!tipAmount || tipAmount <= 0) {
      previewEl.textContent = '';
      return;
    }
    _tipPreviewTimer = setTimeout(() => _fetchTipSplitPreview(tipAmount), 300);
  }

  async function _fetchTipSplitPreview(tipAmount) {
    const previewEl = document.getElementById('pcm-tip-split-preview');
    if (!previewEl) return;
    const seq = ++_tipPreviewSeq;
    try {
      const res = await fetch(
        `/api/staff/tips/preview?amount=${encodeURIComponent(tipAmount)}`,
        { method: 'GET', headers: mesioHeaders() }
      );
      if (seq !== _tipPreviewSeq) return; // stale response
      if (!res.ok) {
        previewEl.textContent = '';
        return;
      }
      const data = await res.json().catch(() => null);
      if (!data || seq !== _tipPreviewSeq) return;
      _renderTipSplitPreview(previewEl, data);
    } catch (_e) {
      if (seq === _tipPreviewSeq) previewEl.textContent = '';
    }
  }

  function _renderTipSplitPreview(el, data) {
    // Use textContent + DOM nodes for safety (no innerHTML with user data).
    el.textContent = '';
    const splits = Array.isArray(data && data.splits) ? data.splits : [];
    const unalloc = Number((data && data.unallocated) || 0);
    if (!splits.length && unalloc <= 0) {
      el.textContent = '';
      return;
    }
    if (!splits.length) {
      const warn = document.createElement('div');
      warn.style.color = '#F59E0B';
      warn.textContent = `⚠ ${fmt(unalloc)} sin asignar (sin staff en turno)`;
      el.appendChild(warn);
      return;
    }
    const label = document.createElement('div');
    label.style.color = '#6B7280';
    label.style.fontWeight = '600';
    label.style.marginBottom = '4px';
    label.textContent = 'Reparto:';
    el.appendChild(label);
    const list = document.createElement('div');
    list.style.display = 'flex';
    list.style.flexWrap = 'wrap';
    list.style.gap = '4px 10px';
    splits.forEach((s, idx) => {
      const span = document.createElement('span');
      span.style.color = '#9FE1CB';
      const role = s.role ? ` (${s.role})` : '';
      // textContent assigns each piece safely
      span.textContent = `${s.name || '—'}${role}: ${fmt(Number(s.amount || 0))}`;
      list.appendChild(span);
      if (idx < splits.length - 1) {
        const dot = document.createElement('span');
        dot.style.color = '#374151';
        dot.textContent = '·';
        list.appendChild(dot);
      }
    });
    el.appendChild(list);
    if (unalloc > 0) {
      const warn = document.createElement('div');
      warn.style.color = '#F59E0B';
      warn.style.marginTop = '4px';
      warn.textContent = `⚠ ${fmt(unalloc)} sin asignar`;
      el.appendChild(warn);
    }
  }

  body.querySelectorAll('.pcm-tip-preset').forEach(btn => {
    btn.addEventListener('click', () => {
      body.querySelectorAll('.pcm-tip-preset').forEach(b => { b.style.background = '#14171f'; b.style.color = '#9CA3AF'; });
      btn.style.background = 'rgba(29,158,117,0.2)'; btn.style.color = '#9FE1CB';
      const customRow = document.getElementById('pcm-tip-custom-row');
      if (btn.dataset.pct === 'custom') {
        _tipMode = 'custom';
        if (customRow) customRow.style.display = '';
      } else {
        _tipMode = 'pct';
        _tipPct = Number(btn.dataset.pct);
        if (customRow) customRow.style.display = 'none';
      }
      _updateChange();
    });
  });

  document.getElementById('pcm-tip-custom-input')?.addEventListener('input', e => {
    _tipCustom = Number(e.target.value) || 0;
    _updateChange();
  });

  document.getElementById('pcm-service-charge')?.addEventListener('input', _updateChange);

  document.getElementById('pcm-add-method')?.addEventListener('click', () => {
    const list = document.getElementById('pcm-payments-list');
    if (list) {
      const row = document.createElement('div');
      row.innerHTML = _paymentRowHtml(list.children.length);
      const rowEl = row.firstElementChild;
      rowEl.querySelector('.pcm-pay-amount')?.addEventListener('input', _updateChange);
      list.appendChild(rowEl);
    }
  });

  // Wire existing row change listeners
  body.querySelectorAll('.pcm-pay-amount').forEach(inp => inp.addEventListener('input', _updateChange));

  // Customer info updates preview in real-time
  document.getElementById('pcm-cust-name')?.addEventListener('input', _renderInvoicePreview);
  document.getElementById('pcm-cust-nit')?.addEventListener('input', _renderInvoicePreview);
  body.querySelectorAll('.pcm-pay-method').forEach(sel => sel.addEventListener('change', _updateChange));

  // Submit
  document.getElementById('pcm-submit')?.addEventListener('click', async () => {
    const errEl = document.getElementById('pcm-error');
    if (errEl) errEl.textContent = '';

    const tip = _getTipAmount();
    const svc = Number(document.getElementById('pcm-service-charge')?.value || 0);
    // tip is part of what the customer owes — include so underpayment guard uses the real total
    const total = checkTotal + svc + tip;

    if (tip > checkTotal * 0.5) {
      if (errEl) errEl.textContent = 'La propina no puede superar el 50% del subtotal de la cuenta';
      return;
    }

    const rows = document.querySelectorAll('.pcm-pay-row');
    const payments = Array.from(rows).map(row => ({
      method: row.querySelector('.pcm-pay-method')?.value || 'efectivo',
      amount: Number(row.querySelector('.pcm-pay-amount')?.value || 0),
    })).filter(p => p.amount > 0);

    if (!payments.length) {
      if (errEl) errEl.textContent = 'Ingresa al menos un método de pago con monto';
      return;
    }

    const totalPaid = payments.reduce((s, p) => s + p.amount, 0);
    if (totalPaid < total) {
      if (errEl) errEl.textContent = `Pago insuficiente: se requieren ${fmt(total)}, ingresados ${fmt(totalPaid)}`;
      return;
    }

    const custName = document.getElementById('pcm-cust-name')?.value || 'Consumidor Final';
    const custNit  = document.getElementById('pcm-cust-nit')?.value  || '222222222';

    const submitBtn = document.getElementById('pcm-submit');
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Procesando…'; }

    try {
      const url = checkId
        ? `/api/table-orders/${encodeURIComponent(baseOrderId)}/checks/${encodeURIComponent(checkId)}/pay`
        : `/api/table-orders/${encodeURIComponent(baseOrderId)}/checks/single/pay`;

      const payload = {
        payments,
        tip_amount: tip,
        service_charge: svc,
        customer_name: custName,
        customer_nit: custNit,
      };

      const res = await fetch(url, { method: 'POST', headers: mesioHeaders(), body: JSON.stringify(payload) });
      const json = await res.json().catch(() => ({}));

      if (!res.ok) {
        if (errEl) errEl.textContent = json.detail || `Error ${res.status}`;
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Cobrar'; }
        return;
      }

      const modal = document.getElementById('pay-check-modal');
      if (modal) modal.style.display = 'none';
      const checkModal = document.getElementById('check-modal');
      if (checkModal) checkModal.style.display = 'none';
      mesioToast('Check cobrado exitosamente', 'success');

      // Show DIAN invoice button if applicable
      if (json.fiscal && json.fiscal.id) {
        mesioToast('Factura electrónica emitida', 'success', 4000);
      }

      loadOpenTables();
      _checks = [];
      _selectedTableOrder = null;
    } catch (err) {
      if (errEl) errEl.textContent = 'Error de red: ' + err.message;
      if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Cobrar'; }
    }
  });

  // Initial invoice preview render
  _renderInvoicePreview();
}

function _paymentRowHtml(idx) {
  return `<div class="pcm-pay-row" style="display:grid;grid-template-columns:1fr 1fr 28px;gap:8px;align-items:center;">
    <select class="pcm-pay-method" style="background:#14171f;border:1px solid #2a2f3d;border-radius:8px;padding:9px 10px;color:#E8EAEE;font-family:inherit;font-size:12px;outline:none;">
      <option value="efectivo">Efectivo</option>
      <option value="tarjeta">Tarjeta</option>
      <option value="transferencia">Transferencia</option>
      <option value="nequi">Nequi</option>
      <option value="daviplata">Daviplata</option>
    </select>
    <input class="pcm-pay-amount" type="number" min="0" placeholder="Monto" style="background:#14171f;border:1px solid #2a2f3d;border-radius:8px;padding:9px 10px;color:#E8EAEE;font-family:inherit;font-size:13px;outline:none;">
    <button class="pcm-pay-remove" style="background:none;border:none;color:#6B7280;cursor:pointer;font-size:16px;line-height:1;" title="Quitar">✕</button>
  </div>`;
}

// Wire delete for dynamically added rows (delegated)
document.addEventListener('click', e => {
  if (e.target.classList.contains('pcm-pay-remove')) {
    const row = e.target.closest('.pcm-pay-row');
    if (row && row.parentElement && row.parentElement.children.length > 1) row.remove();
  }
});

// ── Split check modal ─────────────────────────────────
function openSplitModal(baseOrderId, tableName) {
  let modal = document.getElementById('split-modal');
  if (!modal) {
    modal = _buildSplitModal();
    document.body.appendChild(modal);
  }
  modal.dataset.baseOrderId = baseOrderId;
  modal.style.display = 'flex';
  _renderSplitModal(baseOrderId, tableName);
}

function _buildSplitModal() {
  const modal = document.createElement('div');
  modal.id = 'split-modal';
  modal.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:3300;align-items:center;justify-content:center;';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-labelledby', 'sm-title');
  modal.innerHTML = `
    <div style="background:#1a1d26;border-radius:16px;width:500px;max-width:96vw;max-height:90vh;overflow-y:auto;padding:0;box-shadow:0 24px 64px rgba(0,0,0,0.6);">
      <div style="padding:20px 24px;border-bottom:1px solid #252836;display:flex;align-items:center;justify-content:space-between;">
        <div id="sm-title" style="font-size:16px;font-weight:700;color:#E8EAEE;">Dividir cuenta</div>
        <button id="sm-close" style="background:none;border:1px solid #343b4d;color:#9CA3AF;border-radius:8px;padding:7px 12px;cursor:pointer;font-family:inherit;font-size:13px;">✕</button>
      </div>
      <div id="sm-body" style="padding:20px 24px;"></div>
    </div>`;
  modal.querySelector('#sm-close').addEventListener('click', () => { modal.style.display = 'none'; });
  modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
  modal.addEventListener('keydown', e => { if (e.key === 'Escape' || e.key === 'Esc') modal.style.display = 'none'; });
  return modal;
}

function _renderSplitModal(baseOrderId, tableName) {
  const body = document.getElementById('sm-body');
  if (!body) return;

  body.innerHTML = `
    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:#9CA3AF;margin-bottom:8px;font-weight:600;">División igual entre personas</div>
      <div style="display:flex;gap:8px;align-items:center;">
        <input id="sm-n-people" type="number" min="2" max="20" value="2" style="width:80px;background:#14171f;border:1px solid #2a2f3d;border-radius:8px;padding:9px 12px;color:#E8EAEE;font-family:inherit;font-size:13px;outline:none;">
        <span style="color:#9CA3AF;font-size:13px;">personas</span>
        <button id="sm-equal-split" style="padding:9px 16px;background:var(--brand);color:#fff;border:none;border-radius:8px;font-weight:600;font-size:13px;cursor:pointer;font-family:inherit;">Dividir igual</button>
      </div>
    </div>
    <div style="height:1px;background:#252836;margin:16px 0;"></div>
    <div style="font-size:12px;color:#9CA3AF;margin-bottom:4px;font-weight:600;">O crea los checks manualmente</div>
    <div style="font-size:11px;color:#6B7280;margin-bottom:12px;">Asigna cada plato a un check. Todos los ítems deben quedar asignados.</div>
    <div id="sm-ticket-items" style="font-size:12px;color:#9FE1CB;margin-bottom:12px;padding:10px 12px;background:#12161f;border-radius:8px;">Cargando ítems del ticket…</div>
    <div id="sm-checks-list" style="margin-bottom:12px;"></div>
    <div style="display:flex;gap:8px;">
      <button id="sm-add-check" style="padding:8px 14px;background:#14171f;border:1px solid #2a2f3d;color:#E8EAEE;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;">+ Agregar check</button>
      <button id="sm-submit-split" style="flex:1;padding:10px;background:var(--brand);color:#fff;border:none;border-radius:8px;font-weight:700;font-size:13px;cursor:pointer;font-family:inherit;">Guardar división</button>
    </div>
    <div id="sm-error" style="margin-top:10px;font-size:12px;color:#F87171;min-height:18px;"></div>`;

  // Load ticket items
  fetch(`/api/table-orders/${encodeURIComponent(baseOrderId)}/ticket`, { headers: mesioHeaders() })
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(data => {
      const items = data.items || [];
      const itemsEl = document.getElementById('sm-ticket-items');
      if (itemsEl) {
        if (!items.length) {
          itemsEl.textContent = 'Sin ítems en el ticket';
        } else {
          itemsEl.innerHTML = items.map(it => `<span style="margin-right:10px;">${_esc(String(it.quantity || it.qty || 1))}× ${_esc(it.name)}</span>`).join('');
        }
      }
      // Store items for split
      document._smItems = items;
      _initManualChecks(items);
    })
    .catch(() => {
      const itemsEl = document.getElementById('sm-ticket-items');
      if (itemsEl) itemsEl.textContent = 'No se pudo cargar el ticket';
    });

  // Equal split handler
  document.getElementById('sm-equal-split')?.addEventListener('click', async () => {
    const n = parseInt(document.getElementById('sm-n-people')?.value || '2', 10);
    if (n < 2 || n > 20) { mesioToast('Número de personas inválido (2-20)', 'warning'); return; }
    const items = document._smItems || [];
    if (!items.length) { mesioToast('Cargando ítems, intenta de nuevo', 'warning'); return; }
    const errEl = document.getElementById('sm-error');

    // Build equal split by distributing items across N checks
    const flatItems = [];
    items.forEach(it => {
      const qty = Number(it.quantity || it.qty || 1);
      for (let q = 0; q < qty; q++) flatItems.push({ name: it.name, unit_price: Number(it.price || 0) });
    });
    const checks = Array.from({ length: n }, (_, i) => ({ check_number: i + 1, items: [] }));
    flatItems.forEach((it, i) => checks[i % n].items.push({ name: it.name, qty: 1, unit_price: it.unit_price }));
    // Consolidate same-name items per check
    const consolidated = checks.map(chk => ({
      check_number: chk.check_number,
      items: Object.values(chk.items.reduce((acc, it) => {
        if (!acc[it.name]) acc[it.name] = { name: it.name, qty: 0, unit_price: it.unit_price };
        acc[it.name].qty += it.qty;
        return acc;
      }, {})),
    }));

    await _submitSplit(baseOrderId, consolidated, tableName);
  });

  document.getElementById('sm-add-check')?.addEventListener('click', _addManualCheckRow);
  document.getElementById('sm-submit-split')?.addEventListener('click', () => _submitManualSplit(baseOrderId, tableName));
}

let _manualChecks = [];
function _initManualChecks(items) {
  _manualChecks = [{ check_number: 1, items: items.map(it => ({ name: it.name, qty: Number(it.quantity || it.qty || 1), unit_price: Number(it.price || 0) })) }];
  _renderManualChecks();
}
function _addManualCheckRow() {
  _manualChecks.push({ check_number: _manualChecks.length + 1, items: [] });
  _renderManualChecks();
}
function _renderManualChecks() {
  const el = document.getElementById('sm-checks-list');
  if (!el) return;
  el.innerHTML = _manualChecks.map((chk, ci) => `
    <div style="background:#12161f;border:1px solid #252836;border-radius:8px;padding:12px;margin-bottom:8px;">
      <div style="font-size:12px;font-weight:600;color:#E8EAEE;margin-bottom:8px;">Check #${_esc(String(chk.check_number))}</div>
      ${chk.items.map((it, ii) => `
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;">
          <input class="mc-name" data-ci="${ci}" data-ii="${ii}" type="text" value="${_esc(it.name)}" style="flex:1;background:#0e1117;border:1px solid #1a1d26;border-radius:6px;padding:6px 8px;color:#E8EAEE;font-size:12px;font-family:inherit;outline:none;">
          <input class="mc-qty" data-ci="${ci}" data-ii="${ii}" type="number" min="1" value="${_esc(String(it.qty))}" style="width:50px;background:#0e1117;border:1px solid #1a1d26;border-radius:6px;padding:6px 8px;color:#E8EAEE;font-size:12px;font-family:inherit;outline:none;">
          <button class="mc-del" data-ci="${ci}" data-ii="${ii}" style="background:none;border:none;color:#6B7280;cursor:pointer;font-size:14px;">✕</button>
        </div>`).join('')}
    </div>`).join('');

  el.querySelectorAll('.mc-name').forEach(inp => inp.addEventListener('change', e => {
    _manualChecks[e.target.dataset.ci].items[e.target.dataset.ii].name = e.target.value;
  }));
  el.querySelectorAll('.mc-qty').forEach(inp => inp.addEventListener('change', e => {
    _manualChecks[e.target.dataset.ci].items[e.target.dataset.ii].qty = Number(e.target.value) || 1;
  }));
  el.querySelectorAll('.mc-del').forEach(btn => btn.addEventListener('click', e => {
    const ci = Number(e.target.dataset.ci), ii = Number(e.target.dataset.ii);
    _manualChecks[ci].items.splice(ii, 1);
    _renderManualChecks();
  }));
}
async function _submitManualSplit(baseOrderId, tableName) {
  await _submitSplit(baseOrderId, _manualChecks, tableName);
}
async function _submitSplit(baseOrderId, checks, tableName) {
  const errEl = document.getElementById('sm-error');
  if (errEl) errEl.textContent = '';
  try {
    const res = await fetch(`/api/table-orders/${encodeURIComponent(baseOrderId)}/checks`, {
      method: 'POST',
      headers: mesioHeaders(),
      body: JSON.stringify({ checks, tax_pct: _taxPct }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (errEl) errEl.textContent = json.detail || `Error ${res.status}`;
      return;
    }
    const modal = document.getElementById('split-modal');
    if (modal) modal.style.display = 'none';
    _checks = json.checks || [];
    mesioToast('División guardada', 'success');
    _openCheckModal(baseOrderId, tableName);
  } catch (err) {
    if (errEl) errEl.textContent = 'Error de red: ' + err.message;
  }
}

// ── Pre-cuenta ─────────────────────────────────────────
async function openPreCuenta() {
  const table = _activeTables[_activeTableIdx];
  if (!table) { mesioToast('Selecciona una mesa activa', 'warning'); return; }

  const btn = document.getElementById('btn-pre-cuenta');
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.textContent = 'Enviando…'; btn.disabled = true; }

  try {
    const res = await fetch('/api/pos/tables/' + encodeURIComponent(table.table_id || table.id) + '/pre-cuenta', {
      method: 'POST',
      headers: mesioHeaders(),
    });
    const data = await res.json();
    if (!res.ok) {
      mesioToast(data.detail || 'Error al enviar pre-cuenta', 'error');
      return;
    }
    mesioToast(`Pre-cuenta enviada por WhatsApp (${data.phone})`, 'success', 3500);
  } catch (err) {
    mesioToast('Error de red: ' + err.message, 'error');
  } finally {
    if (btn) { btn.textContent = orig; btn.disabled = false; }
  }
}

// ── Pickup orders ─────────────────────────────────────
async function loadPickupOrders() {
  const el = document.getElementById('pickup-list');
  if (!el) return;
  el.innerHTML = '<div style="color:#6B7280;font-size:13px;">Cargando…</div>';
  try {
    const res = await fetch('/api/delivery/orders', { headers: mesioHeaders() });
    if (!res.ok) { el.innerHTML = '<p style="color:#999;">Error al cargar.</p>'; return; }
    const data = await res.json();
    const orders = (data.orders || []).filter(o => o.order_type === 'recoger' || o.order_type === 'pickup');
    if (!orders.length) {
      el.innerHTML = '<p style="color:#6B7280;padding:20px;">Sin pedidos para recoger.</p>';
      return;
    }
    el.innerHTML = '';
    orders.forEach(o => {
      const card = document.createElement('div');
      card.className = 'order-proposal';
      card.innerHTML = _pickupCardHtml(o);
      card.querySelector('.pickup-confirm')?.addEventListener('click', () => confirmDeliveryOrder(o.id, 'entregado', el, loadPickupOrders));
      card.querySelector('.pickup-reject')?.addEventListener('click', () => rejectDeliveryOrder(o.id, loadPickupOrders));
      el.appendChild(card);
    });
  } catch (_) { el.innerHTML = '<p style="color:#999;">Error de red.</p>'; }
}
function _pickupCardHtml(o) {
  const items = Array.isArray(o.items) ? o.items : [];
  return `
    <div style="font-weight:700;font-size:14px;color:#E8EAEE;">#${_esc(String(o.id || '').slice(0,8))}</div>
    <div style="font-size:12px;color:#71717A;margin-top:2px;">${_esc(o.customer_name || o.phone || '')}</div>
    <div style="font-size:11px;color:#6B7280;margin:4px 0;">${items.map(it => `${_esc(String(it.quantity||it.qty||1))}× ${_esc(it.name||it.dish||'')}`).join(', ')}</div>
    <div style="font-size:15px;font-weight:700;color:var(--brand);margin-top:4px;">${mesioFmt(o.total||0)}</div>
    <div style="display:flex;gap:6px;margin-top:8px;">
      <button class="m-btn m-btn--primary m-btn--sm pickup-confirm" style="flex:1;">Entregar</button>
      <button class="m-btn m-btn--ghost m-btn--sm pickup-reject" style="border-color:#7F1D1D;color:#F87171;">Rechazar</button>
    </div>`;
}

// ── Delivery proposals ────────────────────────────────
async function loadDeliveryProposals() {
  const el = document.getElementById('proposals-list');
  if (!el) return;
  el.innerHTML = '<div style="color:#6B7280;font-size:13px;">Cargando…</div>';
  try {
    const res = await fetch('/api/delivery/orders', { headers: mesioHeaders() });
    if (!res.ok) { el.innerHTML = '<p style="color:#999;">Error al cargar.</p>'; return; }
    const data = await res.json();
    const orders = (data.orders || []).filter(o => {
      const t = o.order_type || '';
      return t === 'domicilio' || t === 'delivery';
    });
    if (!orders.length) {
      el.innerHTML = '<p style="color:#6B7280;padding:20px;">Sin domicilios pendientes.</p>';
      return;
    }
    el.innerHTML = '';
    orders.forEach(o => {
      const card = document.createElement('div');
      card.className = 'order-proposal';
      card.innerHTML = _deliveryCardHtml(o);
      card.querySelector('.del-confirm')?.addEventListener('click', () => confirmDeliveryOrder(o.id, 'confirmado', el, loadDeliveryProposals));
      card.querySelector('.del-reject')?.addEventListener('click', () => rejectDeliveryOrder(o.id, loadDeliveryProposals));
      // ETA send button
      const etaBtn = card.querySelector('.del-eta-send');
      if (etaBtn) {
        etaBtn.addEventListener('click', () => {
          const input = card.querySelector('.del-eta-input');
          const minutes = input ? parseInt(input.value, 10) : NaN;
          setDeliveryEta(o.id, minutes, loadDeliveryProposals);
        });
      }
      // Proof image load
      const proofImg = card.querySelector('.del-proof-img');
      if (proofImg && o.proof_url) {
        // proof_url is already stored as /api/media/{id}?bot={bot_number} — use directly.
        proofImg.src = o.proof_url;
      }
      el.appendChild(card);
    });
  } catch (_) { el.innerHTML = '<p style="color:#999;">Error de red.</p>'; }
}
function _deliveryCardHtml(o) {
  const items = Array.isArray(o.items) ? o.items : [];
  const statusColors = { pendiente:'#F59E0B', confirmado:'#3B82F6', en_preparacion:'#8B5CF6', listo:'#10B981', en_camino:'#06B6D4', en_puerta:'#EC4899' };
  const sc = statusColors[o.status] || '#9CA3AF';
  const hasProof = o.proof_url || o.comprobante_url;
  const loyaltyDiscount = Number(o.loyalty_discount_cop || 0);
  const loyaltyPoints = Number(o.loyalty_redeemed_points || 0);
  const grossTotal = Number(o.total || 0);
  const netTotal = Math.max(0, grossTotal - loyaltyDiscount);
  const loyaltyBlock = loyaltyDiscount > 0
    ? `<div style="font-size:11px;color:#4ADE9E;margin-bottom:4px;font-weight:600;">Descuento puntos: -${mesioFmt(loyaltyDiscount)} (${_esc(String(loyaltyPoints))} puntos)</div>`
    : '';
  const totalLine = loyaltyDiscount > 0
    ? `<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px;"><span style="font-size:11px;color:#6B7280;text-decoration:line-through;">${mesioFmt(grossTotal)}</span><span style="font-size:15px;font-weight:700;color:var(--brand);">${mesioFmt(netTotal)}</span></div>`
    : `<div style="font-size:15px;font-weight:700;color:var(--brand);margin-bottom:8px;">${mesioFmt(grossTotal)}</div>`;
  // ETA section: only shown when the order is paid + still active. Once an
  // ETA has been communicated, the input is replaced by a confirmation line so
  // the operator sees the customer was already notified.
  const isActive = ['confirmado', 'en_preparacion'].includes(String(o.status || ''));
  const showEta = isActive && (o.paid === true || o.paid === 'true');
  const etaMinutes = Number(o.estimated_minutes || 0);
  const etaCommunicated = o.eta_communicated === true || o.eta_communicated === 'true';
  let etaBlock = '';
  if (showEta) {
    if (etaCommunicated && etaMinutes > 0) {
      etaBlock = `<div class="del-eta-sent" style="font-size:11px;color:#4ADE9E;margin-bottom:6px;font-weight:600;">ETA enviada: ${etaMinutes} min</div>`;
    } else {
      etaBlock = `
      <div class="del-eta-row" style="display:flex;gap:6px;align-items:center;margin-bottom:6px;">
        <input class="del-eta-input" type="number" min="1" max="180" placeholder="ETA min" value="${etaMinutes > 0 ? etaMinutes : ''}" style="width:80px;padding:5px 8px;background:#0e1117;border:1px solid #2a2f3d;border-radius:6px;color:#E8EAEE;font-size:12px;">
        <button class="m-btn m-btn--ghost m-btn--sm del-eta-send" style="font-size:11px;padding:5px 10px;">Enviar ETA al cliente</button>
      </div>`;
    }
  }
  return `
    <div style="font-weight:700;font-size:14px;color:#E8EAEE;display:flex;align-items:center;justify-content:space-between;">
      <span>#${_esc(String(o.id || '').slice(0,8))}</span>
      <span style="font-size:10px;background:${sc}22;color:${sc};padding:2px 7px;border-radius:4px;font-weight:600;">${_esc(o.status||'')}</span>
    </div>
    <div style="font-size:12px;color:#71717A;margin-top:2px;">${_esc(o.customer_name || o.phone || '')}</div>
    <div style="font-size:11px;color:#6B7280;margin:2px 0;">${_esc(o.address || '')}</div>
    <div style="font-size:11px;color:#6B7280;margin-bottom:4px;">${items.map(it => `${_esc(String(it.quantity||it.qty||1))}× ${_esc(it.name||it.dish||'')}`).join(', ')}</div>
    ${loyaltyBlock}
    ${totalLine}
    ${etaBlock}
    ${hasProof ? `<img class="del-proof-img" src="" alt="Comprobante" style="width:100%;height:120px;object-fit:cover;border-radius:7px;background:#0e1117;margin-bottom:8px;display:block;" onerror="this.style.display='none'">` : ''}
    <div style="display:flex;gap:6px;">
      <button class="m-btn m-btn--primary m-btn--sm del-confirm" style="flex:1;">Confirmar pago</button>
      <button class="m-btn m-btn--ghost m-btn--sm del-reject" style="border-color:#7F1D1D;color:#F87171;">Rechazar</button>
    </div>`;
}

async function setDeliveryEta(orderId, minutes, reloadFn) {
  if (!Number.isFinite(minutes) || minutes < 1 || minutes > 180) {
    mesioToast('ETA debe estar entre 1 y 180 minutos', 'error');
    return;
  }
  try {
    const res = await fetch(`/api/delivery/orders/${encodeURIComponent(orderId)}/eta`, {
      method: 'POST',
      headers: mesioHeaders(),
      body: JSON.stringify({ minutes }),
    });
    if (res.ok) {
      mesioToast('ETA enviada al cliente', 'success');
      if (typeof reloadFn === 'function') reloadFn();
    } else {
      const j = await res.json().catch(() => ({}));
      mesioToast(j.detail || `Error ${res.status}`, 'error');
    }
  } catch (_) {
    mesioToast('Error de red', 'error');
  }
}
function _extractMediaId(url) {
  if (!url) return null;
  // /api/media/{id}?bot=... or just the raw media ID
  const m = url.match(/\/api\/media\/([^?]+)/);
  return m ? m[1] : null;
}

async function confirmDeliveryOrder(orderId, newStatus, containerEl, reloadFn) {
  // "confirmado" = caja validated proof of payment. Use the dedicated endpoint
  // that sets paid=TRUE atomically and fires loyalty + customer notification.
  // Plain status PATCH would leave paid=FALSE — the Wompi path and manual
  // validation must converge on the same post-conditions.
  const isPaymentValidation = newStatus === 'confirmado';
  const url = isPaymentValidation
    ? `/api/delivery/orders/${encodeURIComponent(orderId)}/validate`
    : `/api/delivery/orders/${encodeURIComponent(orderId)}/status`;
  const method = isPaymentValidation ? 'POST' : 'PATCH';
  const body = isPaymentValidation
    ? JSON.stringify({})
    : JSON.stringify({ status: newStatus });
  try {
    const res = await fetch(url, { method, headers: mesioHeaders(), body });
    if (res.ok) {
      const j = await res.json().catch(() => ({}));
      const msg = isPaymentValidation
        ? (j.already_paid ? 'La orden ya estaba validada' : 'Pago validado, cocina notificada')
        : 'Estado actualizado';
      mesioToast(msg, 'success');
      reloadFn();
    } else {
      const j = await res.json().catch(() => ({}));
      mesioToast(j.detail || `Error ${res.status}`, 'error');
    }
  } catch (_) { mesioToast('Error de red', 'error'); }
}

async function rejectDeliveryOrder(orderId, reloadFn) {
  const confirmed = await mesioConfirm('¿Rechazar este pedido?', { confirmText: 'Rechazar', cancelText: 'Cancelar', danger: true });
  if (!confirmed) return;
  await confirmDeliveryOrder(orderId, 'cancelado', null, reloadFn);
}

// ── Chats tab (checkout proposals with proof) ─────────
async function loadChatsTab() {
  const el = document.getElementById('chats-list');
  if (!el) return;
  el.innerHTML = '<div style="color:#6B7280;font-size:13px;">Cargando…</div>';
  try {
    const res = await fetch('/api/checkout-proposals', { headers: mesioHeaders() });
    if (!res.ok) {
      el.innerHTML = `<div style="padding:20px;color:#9CA3AF;font-size:13px;text-align:center;">Funcionalidad disponible cuando se configure el endpoint de propuestas.</div>`;
      return;
    }
    const data = await res.json();
    const proposals = data.proposals || [];
    if (!proposals.length) {
      el.innerHTML = '<p style="color:#6B7280;padding:20px;">Sin comprobantes pendientes de validación.</p>';
      return;
    }
    el.innerHTML = '';
    proposals.forEach(p => {
      const card = document.createElement('div');
      card.className = 'order-proposal';
      card.style.width = '280px';
      card.innerHTML = _chatProposalCardHtml(p);
      // Confirm button
      card.querySelector('.prop-confirm')?.addEventListener('click', () => openCheckoutProposalFlow(p));
      el.appendChild(card);
    });
  } catch (_) {
    const el2 = document.getElementById('chats-list');
    if (el2) el2.innerHTML = `<div style="padding:20px;color:#9CA3AF;font-size:13px;text-align:center;">Funcionalidad disponible cuando se configure el endpoint de propuestas.</div>`;
  }
}
function _chatProposalCardHtml(p) {
  // proof_url is already stored as /api/media/{id}?bot={bot_number} — use directly.
  return `
    <div style="font-weight:700;font-size:13px;color:#E8EAEE;">${_esc(p.table_name || p.base_order_id || '')}</div>
    <div style="font-size:12px;color:#71717A;margin:2px 0;">${_esc(p.customer_phone || '')}</div>
    ${p.proof_url ? `<img src="${_esc(p.proof_url)}" alt="Comprobante" style="width:100%;height:100px;object-fit:cover;border-radius:6px;background:#0e1117;margin:6px 0;display:block;" onerror="this.style.display='none'">` : '<div style="height:40px;background:#0e1117;border-radius:6px;margin:6px 0;display:flex;align-items:center;justify-content:center;color:#6B7280;font-size:11px;">Sin imagen</div>'}
    <div style="font-size:15px;font-weight:700;color:var(--brand);margin-bottom:8px;">${mesioFmt(p.total || 0)}</div>
    <button class="m-btn m-btn--primary m-btn--sm prop-confirm" style="width:100%;">Procesar pago</button>`;
}
function openCheckoutProposalFlow(proposal) {
  // Delegate to the check pay flow with the base_order_id from the proposal
  if (proposal.base_order_id) {
    openPayCheckForm(proposal.base_order_id, null, Number(proposal.total || 0));
  } else {
    mesioToast('No se puede procesar: falta base_order_id', 'warning');
  }
}

// ── Recent NPS tab ────────────────────────────────────
async function loadRecentNpsTab() {
  const el = document.getElementById('nps-list');
  if (!el) return;
  el.textContent = '';
  const loading = document.createElement('div');
  loading.style.cssText = 'color:#6B7280;font-size:13px;';
  loading.textContent = 'Cargando…';
  el.appendChild(loading);
  try {
    const res = await fetch('/api/caja/recent-nps?limit=10', { headers: mesioHeaders() });
    if (!res.ok) {
      el.textContent = '';
      const fallback = document.createElement('div');
      fallback.style.cssText = 'padding:20px;color:#9CA3AF;font-size:13px;text-align:center;';
      fallback.textContent = 'No se pudo cargar la lista de calificaciones.';
      el.appendChild(fallback);
      return;
    }
    const data = await res.json();
    const items = (data && data.items) || [];
    el.textContent = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.style.cssText = 'color:#6B7280;padding:20px;font-size:13px;';
      empty.textContent = 'Sin calificaciones recientes.';
      el.appendChild(empty);
      return;
    }
    items.forEach(item => el.appendChild(_renderNpsCard(item)));
  } catch (_) {
    el.textContent = '';
    const errEl = document.createElement('div');
    errEl.style.cssText = 'padding:20px;color:#9CA3AF;font-size:13px;text-align:center;';
    errEl.textContent = 'No se pudo cargar la lista de calificaciones.';
    el.appendChild(errEl);
  }
}

function _renderNpsCard(item) {
  const score = Number(item.score || 0);
  // Color by score: red (<=2) / amber (3) / green (>=4)
  let bgColor = '#10B981';
  if (score <= 2) bgColor = '#7F1D1D';
  else if (score === 3) bgColor = '#92400E';

  const card = document.createElement('div');
  card.style.cssText = `background:${bgColor};border-radius:8px;padding:12px 14px;color:#fff;display:flex;flex-direction:column;gap:6px;`;

  // Header: stars + phone + date
  const head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;';

  const starsAndPhone = document.createElement('div');
  starsAndPhone.style.cssText = 'display:flex;align-items:center;gap:10px;';

  const stars = document.createElement('span');
  stars.style.cssText = 'font-size:14px;letter-spacing:1px;';
  stars.textContent = _starString(score);
  starsAndPhone.appendChild(stars);

  const phoneEl = document.createElement('span');
  phoneEl.style.cssText = 'font-size:12px;opacity:0.85;';
  phoneEl.textContent = item.phone || '***';
  starsAndPhone.appendChild(phoneEl);

  head.appendChild(starsAndPhone);

  const dateEl = document.createElement('span');
  dateEl.style.cssText = 'font-size:11px;opacity:0.8;';
  dateEl.textContent = _relativeTime(item.created_at);
  head.appendChild(dateEl);

  card.appendChild(head);

  // Comment (if any) — truncated to 80 chars, textContent for XSS safety
  const rawComment = (item.comment || '').trim();
  if (rawComment) {
    const commentEl = document.createElement('div');
    commentEl.style.cssText = 'font-size:13px;line-height:1.4;opacity:0.95;';
    const truncated = rawComment.length > 80 ? rawComment.slice(0, 80) + '…' : rawComment;
    commentEl.textContent = truncated;
    card.appendChild(commentEl);
  }

  return card;
}

function _starString(score) {
  const n = Math.max(0, Math.min(5, Math.round(score)));
  return '★'.repeat(n) + '☆'.repeat(5 - n);
}

function _relativeTime(iso) {
  if (!iso) return '';
  const then = new Date(iso);
  if (isNaN(then.getTime())) return '';
  const diffMs = Date.now() - then.getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return 'ahora';
  if (mins < 60) return `hace ${mins} min`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `hace ${hrs} h`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `hace ${days} d`;
  return then.toLocaleDateString();
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
function closePayModal() {
  const payModal = document.getElementById('pay-modal');
  if (payModal) payModal.style.display = 'none';
}

// ── Auto-refresh ──────────────────────────────────────
mesioInterval(() => {
  if (_currentTab === 'mesas') loadOpenTables();
  else if (_currentTab === 'proposals') loadDeliveryProposals();
  else if (_currentTab === 'pickup') loadPickupOrders();
  else if (_currentTab === 'chats') loadChatsTab();
  else if (_currentTab === 'nps') loadRecentNpsTab();
}, 18000);

// NPS feed gets a shorter refresh (30s) when active — fresher signal for caja.
mesioInterval(() => {
  if (_currentTab === 'nps') loadRecentNpsTab();
}, 30000);

// ── Boot ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await Promise.all([_loadBillingConfig(), _loadRestaurantSettings(), loadMenu(), loadOpenTables()]);
  _initSearch();
  _enterMesaGrid();

  // Auto-select table when navigated from /mesero via ?tableId=X or sessionStorage caja_open_table
  const urlParams = new URLSearchParams(window.location.search);
  const targetTableId = urlParams.get('tableId');
  let targetMeta = null;
  try {
    const raw = sessionStorage.getItem('caja_open_table');
    if (raw) targetMeta = JSON.parse(raw);
  } catch (_) { /* ignore bad json */ }
  sessionStorage.removeItem('caja_open_table'); // consume once

  if (targetTableId) {
    const tIdx = (_activeTables || []).findIndex(x => String(x.id) === String(targetTableId));
    if (tIdx >= 0) _exitMesaGrid(tIdx);
  }

  document.getElementById('btn-send-kitchen')?.addEventListener('click', sendToKitchen);
  document.getElementById('btn-pay')?.addEventListener('click', openPayModal);
  document.getElementById('btn-pre-cuenta')?.addEventListener('click', openPreCuenta);

  document.querySelectorAll('.seg-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('ai-sug-dismiss')?.addEventListener('click', () => {
    const aiCard = document.getElementById('caja-ai-sug');
    if (aiCard) aiCard.style.display = 'none';
  });

  const payModal = document.getElementById('pay-modal');
  if (payModal) {
    payModal.setAttribute('role', 'dialog');
    payModal.setAttribute('aria-modal', 'true');
    payModal.addEventListener('click', e => { if (e.target === payModal) closePayModal(); });
    payModal.addEventListener('keydown', e => { if (e.key === 'Escape' || e.key === 'Esc') closePayModal(); });
  }

  const qim = document.getElementById('quick-invoice-screen');
  if (qim) {
    qim.setAttribute('role', 'dialog');
    qim.setAttribute('aria-modal', 'true');
    qim.addEventListener('keydown', e => { if (e.key === 'Escape' || e.key === 'Esc') closeQuickInvoiceModal(); });
  }
});
