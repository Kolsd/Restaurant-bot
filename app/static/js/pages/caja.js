// XSS helper — escapes all HTML special chars before injecting into innerHTML
function _escHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const reqHeaders = { 'Authorization': 'Bearer ' + localStorage.getItem('rb_token'), 'Content-Type': 'application/json' };
let _billsCache = {}; // id → bill object, used by Ajustar Factura modal
let _adjBill = null;
let currentChatPhone = null;

function doLogout() { doStaffLogout(); }

// Reloj en tiempo real
(function _initClock() {
  function _tc() {
    const el = document.getElementById('header-clock');
    if (el) el.textContent = new Date().toLocaleTimeString(navigator.language || 'default', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  }
  _tc(); setInterval(_tc, 1000);
})();

// Leemos la configuración del restaurante desde LocalStorage
const _restData = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
const _locale = _restData.locale || 'en-US';
const _currency = _restData.currency || 'USD';

// Formateador Universal Inteligente
const fmt = (amount) => {
    return new Intl.NumberFormat(_locale, {
        style: 'currency',
        currency: _currency,
        // Monedas que no usan decimales (Pesos, Guaraníes, etc.)
        minimumFractionDigits: ['COP', 'CLP', 'PYG', 'JPY'].includes(_currency) ? 0 : 2
    }).format(Number(amount));
};

// ── NAVEGACIÓN TABS ──
function switchTab(tabId) {
  document.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tabId).classList.add('active');

  ['mesas', 'pickup', 'proposals'].forEach(id => {
    const el = document.getElementById('view-' + id);
    if (el) el.style.display = 'none';
  });

  document.getElementById('view-' + tabId).style.display = 'block';

  if (tabId === 'proposals') loadPaymentProofs();
  if (tabId === 'pickup') loadPickupOrders();
}

// ── RECEIPT HTML BUILDER (shared by preview + print) ──
function _buildReceiptHtml(bill, paymentContext) {
  const taxPct = _billingConfig?.tax_percentage ?? 19.0;
  const taxRegime = _billingConfig?.tax_regime || 'iva';
  const taxLabel = taxRegime === 'ico' ? `Impoconsumo ${taxPct}%` : `IVA ${taxPct}%`;
  const total = Number(bill.total) || 0;
  const subtotal = taxPct > 0 ? total / (1 + taxPct / 100) : total;
  const tax = total - subtotal;

  const iso = (bill.created_at || '').endsWith('Z') ? bill.created_at : (bill.created_at || '') + 'Z';
  const dt = new Date(iso);
  const timeStr = dt.toLocaleTimeString('es-CO', {hour:'2-digit', minute:'2-digit'});
  const dateStr = dt.toLocaleDateString('es-CO');

  // 🛡️ FIX ESTRELLA: Si 'items' llega como texto desde la BD, lo convertimos a Array
  let safeItems = bill.items || [];
  if (typeof safeItems === 'string') {
      try { safeItems = JSON.parse(safeItems); } catch(e) { safeItems = []; }
  }

  const itemsHtml = safeItems.map(i => {
    const qty = Number(i.quantity || i.qty || 1);
    const price = Number(i.price || i.unit_price || 0);
    const lineTotal = price * qty;
    return `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:11px;">
      <span>${qty}x ${_escHtml(i.name)}</span><span style="white-space:nowrap;">${fmt(lineTotal)}</span>
    </div>`;
  }).join('');

  let paymentsHtml = '';
  if (paymentContext && paymentContext.payments && paymentContext.payments.length > 0) {
    paymentsHtml = `<div style="border-top:1px dashed #000;margin:6px 0;padding-top:4px;">`;
    paymentContext.payments.forEach(p => {
      paymentsHtml += `<div style="display:flex;justify-content:space-between;font-size:10px;color:#444;">
        <span>${p.method || p.icon || ''}</span><span>${fmt(p.amount)}</span></div>`;
    });
    if (paymentContext.change > 0) {
      paymentsHtml += `<div style="display:flex;justify-content:space-between;font-size:10px;color:#059669;font-weight:700;">
        <span>Cambio</span><span>${fmt(paymentContext.change)}</span></div>`;
    }
    paymentsHtml += `</div>`;
  }

  let customerHtml = '';
  if (paymentContext && (paymentContext.customerName || paymentContext.customerNit)) {
    customerHtml = `<div style="font-size:9px;color:#666;margin-top:4px;">
      Cliente: ${paymentContext.customerName || 'Consumidor Final'}<br>
      CC/NIT: ${paymentContext.customerNit || '222222222'}
    </div>`;
  }

  let fiscalHtml = '';
  if (paymentContext?.fiscal) {
    const f = paymentContext.fiscal;
    if (f.cufe) {
      fiscalHtml = `
        <div style="border-top:1px dashed #000;margin:6px 0;padding-top:4px;">
          <div style="font-size:9px;color:#444;">Factura N°: ${f.invoice_number || ''}</div>
          <div style="font-size:9px;color:#444;">Estado DIAN: ${f.dian_status || '-'}</div>
          <div style="font-size:7px;color:#444;word-break:break-all;">CUFE: ${f.cufe}</div>
          ${f.qr_data ? `<div style="font-size:7px;color:#444;word-break:break-all;">QR: ${f.qr_data}</div>` : ''}
        </div>`;
    }
  }

  return `
    <div style="text-align:center;font-weight:bold;font-size:14px;letter-spacing:1px;">FACTURA DE VENTA</div>
    <div style="text-align:center;font-size:10px;color:#444;">Mesio &middot; Sistema POS</div>
    <div style="border-top:1px dashed #000;margin:6px 0;"></div>
    <div style="font-size:9px;color:#444;">MESA</div>
    <div style="font-size:16px;font-weight:bold;">${bill.table_name || ''}</div>
    <div style="font-size:9px;color:#444;">Fecha: ${dateStr} &nbsp;${timeStr}</div>
    ${customerHtml}
    <div style="border-top:1px dashed #000;margin:6px 0;"></div>
    <div style="display:flex;justify-content:space-between;font-size:8px;color:#888;font-weight:700;text-transform:uppercase;margin-bottom:2px;">
      <span>Item</span><span>Valor</span>
    </div>
    ${itemsHtml}
    <div style="border-top:1px dashed #000;margin:6px 0;"></div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#444;">
      <span>Subtotal (base gravable)</span><span>${fmt(subtotal)}</span>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#444;">
      <span>${taxLabel}</span><span>${fmt(tax)}</span>
    </div>
    <div style="display:flex;justify-content:space-between;font-weight:bold;font-size:13px;border-top:1.5px solid #000;padding-top:4px;margin-top:4px;">
      <span>TOTAL</span><span>${fmt(total)}</span>
    </div>
    ${paymentsHtml}
    ${fiscalHtml}
    <div style="border-top:1px dashed #000;margin:8px 0;"></div>
    <div style="text-align:center;font-size:10px;">Gracias por su visita</div>
    <div style="text-align:center;font-size:9px;color:#444;">— Mesio POS —</div>
  `;
}

function _updateReceiptPreview() {
  const el = document.getElementById('receipt-preview');
  if (!el || !_payBillId) return;

  const bill = _billsCache[_payBillId];
  const received = _activePayments.reduce((s, p) => s + p.amount, 0);

  const receiptBill = {
    table_name: bill?.table_name || '',
    items: _payCheckData?.items || bill?.items || [],
    total: _payCheckTotal,
    created_at: bill?.created_at || new Date().toISOString()
  };

  el.innerHTML = _buildReceiptHtml(receiptBill, {
    payments: _activePayments.map(p => ({ method: p.method, amount: p.amount })),
    customerName: document.getElementById('pay-customer-name')?.value.trim() || 'Consumidor Final',
    customerNit: document.getElementById('pay-customer-nit')?.value.trim() || '222222222',
    change: Math.max(0, received - _payCheckTotal),
    fiscal: null
  });
}

// ── THERMAL PRINT VIA HIDDEN IFRAME ──
function _printThermal(contentHtml) {
  const frame = document.getElementById('print-frame');
  if (!frame) { console.warn('print-frame not found'); return; }
  const doc = frame.contentDocument || frame.contentWindow.document;
  doc.open();
  doc.write(`<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Courier New',Courier,monospace;width:72mm;padding:4mm;font-size:10pt;color:#000;background:#fff}
@page{size:80mm auto;margin:0}
@media print{body{width:72mm;}}
</style></head><body>${contentHtml}</body></html>`);
  doc.close();
  setTimeout(() => { frame.contentWindow.print(); }, 300);
}

async function printFactura(billId) {
  const bill = _billsCache[billId];
  if (!bill) return;

  let fiscal = null;
  try {
    const r = await fetch(`/api/table-orders/${billId}/ticket`, { headers: reqHeaders });
    if (r.ok) { fiscal = (await r.json()).fiscal; }
  } catch(e) {}

  const paymentContext = fiscal ? { payments: [], customerName: '', customerNit: '', change: 0, fiscal } : null;
  _printThermal(_buildReceiptHtml(bill, paymentContext));
}

async function loadCajaOrders() {
  try {
    const r = await fetch('/api/table-orders', { headers: reqHeaders });
    if (!r.ok) {
      if(r.status === 401) window.location.href = '/login';
      return;
    }
    const { orders = [] } = await r.json();

    // 1. Encontrar qué mesas (base_id) ya solicitaron la cuenta (factura_generada)
    const mesasParaCobrar = new Set();
    orders.forEach(o => {
        if (o.status === 'factura_generada') {
            const baseId = o.base_order_id || o.id.replace(/-\d+$/, '');
            mesasParaCobrar.add(baseId);
        }
    });

    // 2. Agrupar TODOS los ítems de las mesas que están en 'mesasParaCobrar'
    const billsMap = {};

    orders.forEach(o => {
        const baseId = o.base_order_id || o.id.replace(/-\d+$/, '');

        // Si esta sub-orden pertenece a una mesa que ya pidió la cuenta...
        if (mesasParaCobrar.has(baseId)) {
            // Inicializar el ticket si no existe
            if (!billsMap[baseId]) {
                billsMap[baseId] = {
                    id: baseId,
                    table_name: o.table_name,
                    created_at: o.created_at, // Usa la hora de la orden base
                    items: [],
                    total: 0
                };
            }

            // Parsear y sumar los items de esta sub-orden específica
            let parsedItems = [];
            try {
                const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items;
                parsedItems = Array.isArray(arr) ? arr : [];
            } catch(e) {}

            billsMap[baseId].items.push(...parsedItems);
            billsMap[baseId].total += (Number(o.total) || 0);
        }
    });

    const pendingBills = Object.values(billsMap);
    _billsCache = {}; // refresh cache
    pendingBills.forEach(b => { _billsCache[b.id] = b; });

    document.getElementById('total-pending').textContent = `${pendingBills.length} cuenta${pendingBills.length !== 1 ? 's' : ''} pendiente${pendingBills.length !== 1 ? 's' : ''}`;

    const board = document.getElementById('board');

    if (pendingBills.length === 0) {
      board.innerHTML = `
        <div class="empty-state">
          <h3>💸 Todo al día</h3>
          <p>No hay cuentas pendientes de cobro en este momento.</p>
        </div>`;
      return;
    }

    board.innerHTML = pendingBills.map(b => {
      const isoStr = (b.created_at || '').endsWith('Z') ? b.created_at : b.created_at + 'Z';
      const time = new Date(isoStr).toLocaleTimeString(_locale, {hour: '2-digit', minute: '2-digit'});

      // Renderizar items agrupados
      const itemsHtml = b.items.map(i => `
        <div class="t-item">
          <div><span class="t-qty">${_escHtml(String(i.quantity||1))}x</span> <span style="font-weight:500;">${_escHtml(i.name)}</span></div>
          <div style="font-weight:600;">${fmt((i.price || 0) * (i.quantity || 1))}</div>
        </div>
      `).join('');

      return `
        <div class="ticket-card" onclick="selectTicketCard('${_escHtml(b.id)}', this)">
          <div class="t-header">
            <div>
              <div class="t-table">${_escHtml(b.table_name)}</div>
              <div style="font-size:11px; color:#A1A1AA; margin-top:4px; font-family:monospace;">ID: ${_escHtml(b.id.substring(0,8))}...</div>
            </div>
            <div class="t-time">⏱ ${time}</div>
          </div>

          <div class="t-items">
            ${itemsHtml}
          </div>

          <div class="t-status-badge">
            ✅ Registrada (Enviada a Sist. Contable si aplica)
          </div>

          <div class="t-total-row">
            <span class="t-total-label">Total a Cobrar</span>
            <span class="t-total-val">${fmt(b.total)}</span>
          </div>

          <button class="btn-adjust" onclick="event.stopPropagation(); openAdjModal('${b.id}')">
            ✏️ Ajustar Factura
          </button>
          <button class="btn-split" onclick="event.stopPropagation(); openSplitModal('${b.id}')">
            ✂️ Dividir Cuenta
          </button>
          <button class="btn-pay" onclick="event.stopPropagation(); cobrarTodo('${b.id}')">
            💰 Cobrar Todo
          </button>
        </div>
      `;
    }).join('');

    // Re-apply selection after re-render
    if (_selectedBillId && _billsCache[_selectedBillId]) {
      const selectedCard = document.querySelector(`.ticket-card[onclick*="${_selectedBillId}"]`);
      if (selectedCard) selectedCard.classList.add('selected');
      document.getElementById('mesas-receipt-preview').innerHTML = _buildReceiptHtml(_billsCache[_selectedBillId], null);
    } else if (_selectedBillId) {
      closeMesasPreview();
    }

  } catch(e) {
    console.error('Error cargando caja:', e);
  }
}

// ── SPLIT SCREEN: TICKET SELECTION & PREVIEW ──
let _selectedBillId = null;

function selectTicketCard(billId, el) {
  const bill = _billsCache[billId];
  if (!bill) return;

  if (_selectedBillId === billId) {
    closeMesasPreview();
    return;
  }

  _selectedBillId = billId;

  document.querySelectorAll('.ticket-card').forEach(c => c.classList.remove('selected'));
  if (el) el.classList.add('selected');

  const pane = document.getElementById('mesas-right');
  pane.style.display = 'flex';

  document.getElementById('mesas-receipt-preview').innerHTML = _buildReceiptHtml(bill, null);

  document.getElementById('preview-cobrar-btn').onclick = (e) => { e.stopPropagation(); cobrarTodo(billId); };
  document.getElementById('preview-print-btn').onclick = (e) => { e.stopPropagation(); printFactura(billId); };
}

function closeMesasPreview() {
  _selectedBillId = null;
  document.getElementById('mesas-right').style.display = 'none';
  document.querySelectorAll('.ticket-card').forEach(c => c.classList.remove('selected'));
}

// ══════════════════════════════════════════════════════════════════
// FASE 5 — SPLIT CHECKS & PAGOS MIXTOS
// ══════════════════════════════════════════════════════════════════

let _splitBillId   = null;
let _splitCheckN   = 2;      // número de columnas de cuenta en fase 1
let _splitChecks   = [];     // check objects del servidor (fase 2)
let _payBillId     = null;
let _payCheckId    = null;
let _payCheckTotal = 0;
let _payCheckData  = null;
let _activePayments = [];
let _billingConfig = null;

// Carga la config de billing para obtener tax_pct
async function loadBillingConfig() {
  try {
    const r = await fetch('/api/billing/config', { headers: reqHeaders });
    if (r.ok) { const d = await r.json(); _billingConfig = d.config || null; }
  } catch(e) {}
}

// ── "Cobrar Todo" — crea un check único con todos los ítems ────────
async function cobrarTodo(billId) {
  const bill = _billsCache[billId];
  if (!bill) return;

  // Agrupar ítems por nombre
  const byName = {};
  (bill.items || []).forEach(i => {
    const k = i.name;
    if (!byName[k]) byName[k] = { name: k, qty: 0, unit_price: Number(i.price) || 0 };
    byName[k].qty += Number(i.quantity) || 1;
  });
  const items = Object.values(byName);

  const taxPct = _billingConfig?.tax_percentage ?? 19.0;

  try {
    const r = await fetch(`/api/table-orders/${billId}/checks`, {
      method: 'POST',
      headers: reqHeaders,
      body: JSON.stringify({
        checks: [{ check_number: 1, items }],
        tax_pct: taxPct,
        tax_regime: _billingConfig?.tax_regime ?? 'iva'
      })
    });

    // 🛡️ Leer la respuesta como texto primero para evitar crash de JSON
    const textResponse = await r.text();
    let data;
    try {
        data = JSON.parse(textResponse);
    } catch(err) {
        throw new Error(`El servidor devolvió HTML en lugar de JSON. Status: ${r.status}`);
    }

    if (!r.ok) {
        alert(data.detail || 'Error al crear check');
        return;
    }

    const checks = data.checks;
    if (!checks || checks.length === 0) {
        throw new Error('El backend respondió bien, pero no devolvió los datos de la cuenta (checks).');
    }

    const check = checks[0];

    // 🛡️ Prevenir el error más común: que la base de datos no haya devuelto el 'id'
    if (!check.id) {
        console.warn("⚠️ El check devuelto no tiene ID.");
        check.id = "tmp-" + Date.now();
    }

    // Si todo va bien, abrimos el modal
    openPayModal(billId, check.id, check);

  } catch(e) {
    // 🕵️‍♂️ AQUÍ ATRAPAMOS AL VERDADERO CULPABLE
    console.error("🔥 Error real atrapado en cobrarTodo:", e);
    alert(`Error JS: ${e.message}`);
  }
}

// ── MODAL DIVIDIR CUENTA ──────────────────────────────────────────

function openSplitModal(billId) {
  const bill = _billsCache[billId];
  if (!bill) return;
  _splitBillId   = billId;
  _splitCheckN   = 2;
  _splitChecks   = [];

  document.getElementById('split-modal-title').textContent =
    `✂️ Dividir Cuenta — ${bill.table_name}`;
  document.getElementById('split-phase-1').style.display = '';
  document.getElementById('split-phase-2').style.display = 'none';
  document.getElementById('split-footer').style.display = '';
  document.getElementById('split-complete-msg').style.display = 'none';

  _renderSplitTable();
  document.getElementById('split-modal').classList.add('open');
}

function closeSplitModal() {
  document.getElementById('split-modal').classList.remove('open');
  _splitBillId = null;
  loadCajaOrders();  // Refresh board
}

function addSplitCheck() {
  _splitCheckN++;
  _renderSplitTable();
}

function removeSplitCheck(n) {
  if (_splitCheckN <= 1) return;
  _splitCheckN--;
  _renderSplitTable();
}

function _splitItems() {
  const bill = _billsCache[_splitBillId];
  const byName = {};
  (bill.items || []).forEach(i => {
    const k = i.name;
    if (!byName[k]) byName[k] = { name: k, qty: 0, unit_price: Number(i.price) || 0 };
    byName[k].qty += Number(i.quantity) || 1;
  });
  return Object.values(byName);
}

function _renderSplitTable() {
  const items = _splitItems();
  const n = _splitCheckN;

  // Header
  let thHtml = `<th>Ítem</th><th>Precio Unit.</th><th style="text-align:center">Total</th>`;
  for (let c = 1; c <= n; c++) {
    thHtml += `<th class="chk-col">Cuenta ${c}
      ${n > 1 ? `<button onclick="removeSplitCheck(${c})" style="background:none;border:none;color:#EF4444;cursor:pointer;font-size:11px;margin-left:3px;" title="Quitar">✕</button>` : ''}
    </th>`;
  }
  document.getElementById('split-table-head').innerHTML = `<tr>${thHtml}</tr>`;

  // Body
  let tbHtml = '';
  items.forEach((item, idx) => {
    let tdHtml = `<td style="font-weight:500;">${_escHtml(item.name)}</td>
      <td style="color:#71717A;">${fmt(item.unit_price)}</td>
      <td style="text-align:center;font-weight:700;">${_escHtml(String(item.qty))}</td>`;
    for (let c = 1; c <= n; c++) {
      const defaultQty = c === 1 ? item.qty : 0;
      tdHtml += `<td class="chk-cell">
        <input class="split-qty-in" type="number" min="0" max="${item.qty}"
               id="sq-${idx}-${c}" value="${defaultQty}"
               oninput="_onSplitQtyChange(${idx},${c},${n})">
      </td>`;
    }
    tbHtml += `<tr>${tdHtml}</tr>`;
  });
  document.getElementById('split-table-body').innerHTML = tbHtml;

  _validateSplit();
}

function _onSplitQtyChange(itemIdx, checkCol, totalCols) {
  _validateSplit();
}

function _validateSplit() {
  const items = _splitItems();
  const n = _splitCheckN;
  let allOk = true;
  const checkTotals = Array(n + 1).fill(0);

  items.forEach((item, idx) => {
    let assigned = 0;
    for (let c = 1; c <= n; c++) {
      const el = document.getElementById(`sq-${idx}-${c}`);
      const v = Math.max(0, parseInt(el?.value || '0') || 0);
      assigned += v;
      checkTotals[c] += item.unit_price * v;
      el?.classList.toggle('over', assigned > item.qty);
    }
    if (assigned !== item.qty) allOk = false;
  });

  const badge = document.getElementById('split-unassigned');
  if (allOk) {
    badge.textContent = '✓ Todo asignado correctamente';
    badge.className = 'ok';
  } else {
    badge.textContent = '⚠ Hay ítems sin asignar o excedidos';
    badge.className = 'bad';
  }
  document.getElementById('btn-create-split').disabled = !allOk;

  // Totals strip
  let totalsHtml = '';
  for (let c = 1; c <= n; c++) {
    totalsHtml += `<div class="split-total-badge">Cuenta ${c}: <strong>${fmt(checkTotals[c])}</strong></div>`;
  }
  document.getElementById('split-totals-row').innerHTML = totalsHtml;
}

async function submitSplit() {
  const items = _splitItems();
  const n = _splitCheckN;
  const taxPct = _billingConfig?.tax_percentage ?? 19.0;

  const checks = [];
  for (let c = 1; c <= n; c++) {
    const checkItems = [];
    items.forEach((item, idx) => {
      const el = document.getElementById(`sq-${idx}-${c}`);
      const qty = parseInt(el?.value || '0') || 0;
      if (qty > 0) checkItems.push({ name: item.name, qty, unit_price: item.unit_price });
    });
    if (checkItems.length > 0) checks.push({ check_number: c, items: checkItems });
  }

  const btn = document.getElementById('btn-create-split');
  btn.textContent = 'Creando...'; btn.disabled = true;

  try {
    const r = await fetch(`/api/table-orders/${_splitBillId}/checks`, {
      method: 'POST', headers: reqHeaders,
      body: JSON.stringify({ checks, tax_pct: taxPct,
                             tax_regime: _billingConfig?.tax_regime ?? 'iva' })
    });
    if (!r.ok) {
      const e = await r.json();
      alert(e.detail || 'Error al crear la división');
      btn.textContent = 'Crear División'; btn.disabled = false;
      return;
    }
    const { checks: createdChecks } = await r.json();
    _splitChecks = createdChecks;
    _showSplitPhase2();
  } catch(e) {
    alert('Error de conexión');
    btn.textContent = 'Crear División'; btn.disabled = false;
  }
}

function _showSplitPhase2() {
  document.getElementById('split-phase-1').style.display = 'none';
  document.getElementById('split-phase-2').style.display = '';
  document.getElementById('split-footer').style.display = 'none';
  _renderSplitChecks();
}

function _renderSplitChecks() {
  const listEl = document.getElementById('split-checks-list');
  let html = '';
  _splitChecks.forEach(chk => {
    const isPaid = chk.status === 'invoiced' || chk.status === 'paid';
    html += `<div class="check-row${isPaid ? ' invoiced' : ''}" id="chk-row-${chk.id.replace(/[^a-z0-9]/gi,'-')}">
      <div>
        <div class="check-row-label">Cuenta ${chk.check_number}</div>
        <div style="font-size:11px;color:#71717A;">${(chk.items||[]).length} ítem(s)</div>
      </div>
      <div class="check-row-total">${fmt(chk.total)}</div>
      <div class="check-row-actions">
        ${isPaid
          ? `<span style="font-size:12px;color:#059669;font-weight:700;">✅ Cobrada</span>
             <button class="btn-print-check" onclick="printCheckTicket('${_splitBillId}','${chk.id}')">🖨️</button>`
          : `<button class="btn-cobrar-check" onclick="openPayModal('${_splitBillId}','${chk.id}',${JSON.stringify(chk).replace(/"/g,'&quot;')})">💰 Cobrar</button>`
        }
      </div>
    </div>`;
  });
  listEl.innerHTML = html;

  const allPaid = _splitChecks.every(c => c.status === 'invoiced' || c.status === 'paid');
  document.getElementById('split-complete-msg').style.display = allPaid ? '' : 'none';
}

// ── MODAL COBRAR CHECK ────────────────────────────────────────────

function openPayModal(billId, checkId, checkData) {
  _payBillId     = billId;
  _payCheckId    = checkId;
  _payCheckTotal = parseFloat(checkData.total) || 0;

  // 🛡️ FIX: Asegurarnos de que checkData.items también se vuelva Array
  if (typeof checkData.items === 'string') {
      try { checkData.items = JSON.parse(checkData.items); } catch(e) { checkData.items = []; }
  }

  _payCheckData  = checkData;
  _activePayments = [];

  document.getElementById('pay-modal-title').textContent = `💰 Cobrar Cuenta ${checkData.check_number || ''}`;
  document.getElementById('pay-modal-subtitle').textContent = `Mesa: ${_billsCache[billId]?.table_name || ''} | ID: ${(checkId || '').substring(0,8)}...`;

  document.getElementById('pay-customer-name').value  = '';
  document.getElementById('pay-customer-nit').value   = '';

  // Reset tip field
  const tipInput = document.getElementById('payTipAmount');
  if (tipInput) {
    tipInput.value = '';
    tipInput.style.borderColor = '#E4E4E7';
  }
  const tipBadge = document.getElementById('payTipBadge');
  if (tipBadge) { tipBadge.style.display = 'none'; tipBadge.textContent = ''; }

  // Pre-rellenar propina del bot si existe
  const botTip = parseFloat(checkData.tip_amount || checkData.proposed_tip || 0);
  if (tipInput && botTip > 0) {
    tipInput.value = botTip.toFixed(2);
    tipInput.style.borderColor = '#27ae60';
    if (tipBadge) {
      tipBadge.textContent = '🤖 Propina sugerida por el cliente';
      tipBadge.style.display = 'block';
    }
  }

  const fb = document.getElementById('pay-feedback');
  fb.style.display = 'none';

  const btn = document.getElementById('btn-process-pay');
  btn.disabled = false;
  btn.textContent = 'Cobrar e Imprimir Factura';

  document.getElementById('pay-methods-list').innerHTML = '';

  renderPayMethodsGrid();

  // Agregar automáticamente Efectivo con el valor exacto por comodidad
  addPayMethod('Efectivo', '💵', _payCheckTotal);

  document.getElementById('pay-modal').style.display = 'flex';
  _updateReceiptPreview();
}

function closePayModal() {
  document.getElementById('pay-modal').style.display = 'none';
  _payBillId = _payCheckId = null;
}

// ── RENDERIZAR MÉTODOS DE PAGO DINÁMICOS ──
function renderPayMethodsGrid() {
  const container = document.getElementById('dynamic-pay-methods');
  if (!container) return;

  // 1. Opciones por defecto (Fallback)
  let methods = [
    { name: 'Efectivo', icon: '💵' },
    { name: 'Tarjeta', icon: '💳' },
    { name: 'Nequi', icon: '📱' },
    { name: 'Transferencia', icon: '🏦' }
  ];

  // 2. Leer de la configuración del restaurante
  const feats = _restData.features || {};
  const customMethods = feats.payment_methods;

  if (Array.isArray(customMethods) && customMethods.length > 0) {
    methods = customMethods.map(pm => {
      if (typeof pm === 'string') {
        let icon = '💳';
        const lower = pm.toLowerCase();
        if (lower.includes('efectivo') || lower.includes('cash')) icon = '💵';
        else if (lower.includes('nequi') || lower.includes('daviplata') || lower.includes('zelle')) icon = '📱';
        else if (lower.includes('trans')) icon = '🏦';
        return { name: pm, icon: icon };
      }
      return { name: pm.name || 'Pago', icon: pm.icon || '💳' };
    });
  }

  // 3. Inyectar los botones al HTML
  container.innerHTML = methods.map(m => `
    <button class="pay-method-btn" onclick="addPayMethod('${m.name}', '${m.icon}')">
      <span style="font-size:24px;">${m.icon}</span> ${m.name}
    </button>
  `).join('');
}

function addPayMethod(method, icon, forcedAmount = null) {
  const received = _activePayments.reduce((s, p) => s + p.amount, 0);
  const pending = _payCheckTotal - received;

  if (pending <= 0 && forcedAmount === null) return;

  let amt = forcedAmount !== null ? forcedAmount : pending;

  // Si es efectivo y no viene un monto forzado, preguntamos cuánto entrega el cliente
  if (method === 'Efectivo' && forcedAmount === null) {
    const input = prompt(`Monto entregado en Efectivo (Pendiente: ${fmt(pending)}):`, pending);
    if (!input) return;
    amt = parseFloat(input.replace(/[^0-9.-]+/g,""));
    if (isNaN(amt) || amt <= 0) return;
  }

  _activePayments.push({ method, icon, amount: amt, id: Date.now() });
  _renderPaymentsList();
  _recalcChange();
  _updateReceiptPreview();
}

function _renderPaymentsList() {
  const container = document.getElementById('pay-methods-list');
  container.innerHTML = _activePayments.map((p, i) => `
    <div class="pay-method-row" data-idx="${i}">
      <span style="font-size:20px;">${p.icon}</span>
      <select class="pay-method-sel" onchange="_activePayments[${i}].method=this.value;_recalcChange();">
        ${_getPayMethodOptions().map(m =>
          '<option value="'+m+'" '+(m===p.method?'selected':'')+'>'+m+'</option>'
        ).join('')}
      </select>
      <input type="number" class="pay-method-amt" value="${p.amount}" min="0" step="100"
             oninput="_activePayments[${i}].amount=parseFloat(this.value)||0;_recalcChange();">
      <button class="pay-method-rm" onclick="_activePayments.splice(${i},1);_renderPaymentsList();_recalcChange();">✕</button>
    </div>
  `).join('');

  const received = _activePayments.reduce((s, p) => s + p.amount, 0);
  const change = received - _payCheckTotal;
  container.innerHTML += `
    <div class="pay-summary-box">
      <div class="pay-summary-line"><span>Total cuenta</span><span id="pay-check-total">${fmt(_payCheckTotal)}</span></div>
      <div class="pay-summary-line"><span>Recibido</span><span id="pay-received">${fmt(received)}</span></div>
      <div class="pay-summary-line total"><span>Cambio</span>
        <span id="pay-change" class="pay-change${change<0?' insuf':''}">${fmt(Math.abs(change))}</span>
      </div>
    </div>`;
}

function _getPayMethodOptions() {
  const feats = _restData.features || {};
  const custom = feats.payment_methods;
  if (Array.isArray(custom) && custom.length > 0)
    return custom.map(pm => typeof pm === 'string' ? pm : pm.name || 'Pago');
  return ['Efectivo', 'Tarjeta', 'Nequi', 'Transferencia'];
}

function _recalcChange() {
  const received = _activePayments.reduce((s, p) => s + p.amount, 0);
  const change = received - _payCheckTotal;

  document.getElementById('pay-pending-display').textContent = fmt(_payCheckTotal);
  document.getElementById('pay-change-display').textContent = change >= 0 ? fmt(change) : fmt(0);
  document.getElementById('pay-change-display').style.color = change >= 0 ? '#059669' : '#EF4444';

  const rcv = document.getElementById('pay-received');
  if (rcv) rcv.textContent = fmt(received);
  const chg = document.getElementById('pay-change');
  if (chg) { chg.textContent = fmt(Math.abs(change)); chg.className = `pay-change${change < 0 ? ' insuf' : ''}`; }
  const tot = document.getElementById('pay-check-total');
  if (tot) tot.textContent = fmt(_payCheckTotal);

  _updateReceiptPreview();
}

async function processPayment() {
  const payments = [];
  let received = 0;
  let valid = true;
  document.querySelectorAll('.pay-method-row').forEach(row => {
    const method = row.querySelector('.pay-method-sel')?.value;
    const amount = parseFloat(row.querySelector('.pay-method-amt')?.value) || 0;
    if (!method || amount <= 0) { valid = false; return; }
    payments.push({ method, amount });
    received += amount;
  });

  const fb = document.getElementById('pay-feedback');
  if (!valid || payments.length === 0) {
    fb.style.cssText = 'display:block;background:#FEF2F2;color:#EF4444;border-radius:8px;padding:8px 12px;font-size:13px;';
    fb.textContent = 'Completa al menos un método de pago con monto válido.';
    return;
  }
  if (received < _payCheckTotal) {
    fb.style.cssText = 'display:block;background:#FEF2F2;color:#EF4444;border-radius:8px;padding:8px 12px;font-size:13px;';
    fb.textContent = `Pago insuficiente: faltan ${fmt(_payCheckTotal - received)}.`;
    return;
  }

  const btn = document.getElementById('btn-process-pay');
  btn.textContent = 'Procesando...';
  btn.disabled = true;
  fb.style.display = 'none';

  const tipVal = parseFloat(document.getElementById('payTipAmount')?.value || 0) || 0;
  const body = {
    payments,
    customer_name:  document.getElementById('pay-customer-name').value.trim()  || 'Consumidor Final',
    customer_nit:   document.getElementById('pay-customer-nit').value.trim()   || '222222222',
    customer_email: document.getElementById('pay-customer-email').value.trim() || '',
    ...(tipVal > 0 ? { tip_amount: tipVal } : {}),
  };

  try {
    const r = await fetch(`/api/table-orders/${_payBillId}/checks/${_payCheckId}/pay`, {
      method: 'POST',
      headers: reqHeaders,
      body: JSON.stringify(body)
    });

    // 🛡️ FIX: Validar si la respuesta es JSON antes de parsear
    const textResponse = await r.text();
    let data;
    try {
        data = JSON.parse(textResponse);
    } catch(err) {
        throw new Error(`El servidor no devolvió JSON. Status: ${r.status}. Respuesta: ${textResponse.substring(0, 50)}...`);
    }

    if (r.ok) {
      fb.style.cssText = 'display:block;background:#ECFDF5;color:#059669;border-radius:8px;padding:8px 12px;font-size:13px;';
      const change = data.change || 0;
      fb.textContent = `✅ Pago registrado. CUFE: ${(data.fiscal?.cufe||'').slice(0,16)}...${change > 0 ? ` Cambio: ${fmt(change)}` : ''}`;

      try {
        const ticketResp = await fetch(`/api/table-orders/${_payBillId}/checks/${_payCheckId}/ticket`, { headers: reqHeaders });
        if (ticketResp.ok) {
          const ticket = await ticketResp.json();
          _printCheckWindow(ticket);
        }
      } catch(printErr) { console.warn('Auto-print failed:', printErr); }

      const updIdx = _splitChecks.findIndex(c => c.id === _payCheckId);
      if (updIdx >= 0) {
        _splitChecks[updIdx].status = 'invoiced';
        setTimeout(() => { closePayModal(); _renderSplitChecks(); }, 1800);
      } else {
        setTimeout(() => { closePayModal(); loadCajaOrders(); }, 1800);
      }
    } else {
      fb.style.cssText = 'display:block;background:#FEF2F2;color:#EF4444;border-radius:8px;padding:8px 12px;font-size:13px;';
      fb.textContent = data.detail || 'Error al procesar el pago.';
      btn.textContent = 'Cobrar e Imprimir Factura';
      btn.disabled = false;
    }
  } catch(e) {
    // 🕵️‍♂️ AQUÍ ATRAPAMOS AL VERDADERO CULPABLE
    console.error("Error crítico en processPayment:", e);
    fb.style.cssText = 'display:block;background:#FEF2F2;color:#EF4444;border-radius:8px;padding:8px 12px;font-size:13px;';
    fb.textContent = `Error JS/Red: ${e.message}`;
    btn.textContent = 'Cobrar e Imprimir Factura';
    btn.disabled = false;
  }
}

async function printCheckTicket(billId, checkId) {
  try {
    const r = await fetch(`/api/table-orders/${billId}/checks/${checkId}/ticket`, { headers: reqHeaders });
    if (!r.ok) { alert('No se encontró el ticket del check'); return; }
    const ticket = await r.json();
    _printCheckWindow(ticket);
  } catch(e) { alert('Error de conexión'); }
}

function _printCheckWindow(ticket) {
  const total = parseFloat(ticket.total) || 0;
  const bill = {
    table_name: ticket.table_name || '',
    items: (ticket.items || []).map(i => ({
      name: i.name,
      quantity: i.qty || i.quantity || 1,
      price: i.unit_price || i.price || 0
    })),
    total: total,
    created_at: ticket.created_at || new Date().toISOString()
  };

  const fiscal = ticket.cufe ? {
    cufe: ticket.cufe,
    qr_data: ticket.qr_data,
    invoice_number: ticket.invoice_number,
    dian_status: ticket.dian_status
  } : null;

  const paymentContext = {
    payments: (ticket.payments || []).map(p => ({ method: p.method, amount: p.amount })),
    customerName: ticket.customer_name || '',
    customerNit: ticket.customer_nit || '',
    change: parseFloat(ticket.change_amount) || 0,
    fiscal: fiscal
  };

  _printThermal(_buildReceiptHtml(bill, paymentContext));
}

// ── MODAL AJUSTAR FACTURA ──

function openAdjModal(billId) {
  const bill = _billsCache[billId];
  if (!bill) return;
  _adjBill = {
    id: billId,
    items: bill.items.map(i => ({ name: i.name, price: Number(i.price) || 0, quantity: i.quantity || 1, adjQty: i.quantity || 1 }))
  };
  renderAdjItems();
  document.getElementById('adj-tip').value = '';
  updateAdjTotal();
  document.getElementById('adj-modal').classList.add('open');
}

function closeAdjModal() {
  document.getElementById('adj-modal').classList.remove('open');
  _adjBill = null;
}

function renderAdjItems() {
  const container = document.getElementById('adj-items-list');
  container.innerHTML = _adjBill.items.map((item, idx) => `
    <div class="adj-item ${item.adjQty === 0 ? 'adj-removed' : ''}" id="adj-row-${idx}">
      <div class="adj-item-name">${_escHtml(item.name)}</div>
      <div class="adj-qty-ctrl">
        <button class="adj-qty-btn" onclick="adjQty(${idx},-1)" aria-label="Reducir cantidad">−</button>
        <span class="adj-qty-val" id="adj-qty-${idx}">${_escHtml(String(item.adjQty))}</span>
        <button class="adj-qty-btn" onclick="adjQty(${idx},1)" aria-label="Aumentar cantidad">+</button>
      </div>
      <div class="adj-item-price" id="adj-price-${idx}">${fmt(item.price * item.adjQty)}</div>
    </div>
  `).join('');
}

function adjQty(idx, delta) {
  const item = _adjBill.items[idx];
  item.adjQty = Math.max(0, item.adjQty + delta);
  const row = document.getElementById(`adj-row-${idx}`);
  document.getElementById(`adj-qty-${idx}`).textContent = item.adjQty;
  document.getElementById(`adj-price-${idx}`).textContent = fmt(item.price * item.adjQty);
  if (item.adjQty === 0) row.classList.add('adj-removed');
  else row.classList.remove('adj-removed');
  updateAdjTotal();
}

function updateAdjTotal() {
  if (!_adjBill) return;
  const tip = Number(document.getElementById('adj-tip').value) || 0;
  const itemsTotal = _adjBill.items.reduce((sum, i) => sum + i.price * i.adjQty, 0);
  document.getElementById('adj-total-val').textContent = fmt(itemsTotal + tip);
}

async function submitAdjuste() {
  if (!_adjBill) return;
  const tip = Number(document.getElementById('adj-tip').value) || 0;
  const adjustedItems = _adjBill.items
    .filter(i => i.adjQty > 0)
    .map(i => ({ name: i.name, price: i.price, quantity: i.adjQty }));
  const newTotal = adjustedItems.reduce((s, i) => s + i.price * i.quantity, 0) + tip;

  try {
    const r = await fetch(`/api/table-orders/${_adjBill.id}/adjust`, {
      method: 'PATCH',
      headers: reqHeaders,
      body: JSON.stringify({ items: adjustedItems, tip, total: newTotal })
    });
    if (r.ok) {
      closeAdjModal();
      loadCajaOrders();
    } else {
      const err = await r.json().catch(() => ({}));
      alert(err.detail || 'Error al ajustar la factura.');
    }
  } catch(e) {
    alert('Error de conexión.');
  }
}


async function confirmarDomicilio(id) {
    if(!confirm("¿Confirmar pedido y enviarlo a la cocina?")) return;
    try {
        await fetch(`/api/delivery/orders/${id}/status`, {
            method: 'PATCH', headers: reqHeaders, body: JSON.stringify({status: 'confirmado'})
        });
        const activeTab = document.querySelector('.seg-btn.active')?.id;
        if (activeTab === 'tab-pickup') { loadPickupOrders(); _refreshPickupBadge(); }
        else loadPaymentProofs();
    } catch(e) {}
}

// ── WHATSAPP CHATS ──
async function loadChats() {
    try {
        const r = await fetch('/api/dashboard/conversations', { headers: reqHeaders });
        if (!r.ok) return;
        const data = await r.json();
        const allConvs = data.conversations || [];
        // Caja solo muestra comprobantes de pago pendientes de validar
        const convs = allConvs.filter(c => c.has_voucher);

        const badge = document.getElementById('badge-chat');
        if (convs.length > 0) { badge.textContent = convs.length; badge.style.display = 'inline-block'; }
        else { badge.style.display = 'none'; }

        const list = document.getElementById('inbox-list');
        if (convs.length === 0) { list.innerHTML = '<div class="empty-state">No hay comprobantes pendientes.</div>'; return; }

        list.innerHTML = convs.map(c => `
            <div class="conv-row" onclick="openChatModal('${_escHtml(c.phone)}')">
                <div class="conv-avatar">${_escHtml(c.phone.slice(-4))}</div>
                <div style="flex:1;">
                    <div style="font-weight:700;font-size:14px;">${_escHtml(c.phone)}</div>
                    <div style="color:#71717A;font-size:12px;">${_escHtml(c.preview)}</div>
                </div>
            </div>
        `).join('');
    } catch(e) {}
}

async function openChatModal(phone) {
    currentChatPhone = phone;
    document.getElementById('chat-modal-title').textContent = "Chat " + phone;
    document.getElementById('chat-modal').classList.add('open');
    document.getElementById('chat-modal-msgs').innerHTML = 'Cargando...';

    try {
        const r = await fetch('/api/conversations/' + encodeURIComponent(phone), { headers: reqHeaders });
        const d = await r.json();
        const msgs = d.history || [];
        const container = document.getElementById('chat-modal-msgs');

        container.innerHTML = msgs.map(m => {
            const isUser = m.role === 'user';
            let content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);

            // Escape all HTML first, then selectively restore safe media links
            content = _escHtml(content);
            content = content.replace(/(\/api\/media\/\S+)/g, '<a href="$1" target="_blank" style="color:#1D9E75;text-decoration:none;font-weight:800;background:#E1F5EE;padding:6px 12px;border-radius:8px;display:inline-block;margin-top:6px;border:1px solid #1D9E75;">&#128206; Abrir Comprobante</a>');

            return `<div class="msg-bubble ${isUser ? 'user' : ''}"><div class="bubble ${isUser ? 'user' : 'bot'}">${content}</div></div>`;
        }).join('');
        container.scrollTop = container.scrollHeight;
    } catch(e) {}
}

function closeChatModal() {
    document.getElementById('chat-modal').classList.remove('open');
    currentChatPhone = null;
}

async function sendManualReply() {
    const input = document.getElementById('chat-reply-input');
    const msg = input.value.trim();
    if(!msg || !currentChatPhone) return;
    input.value = '';
    try {
        const r = await fetch('/api/conversations/' + encodeURIComponent(currentChatPhone) + '/reply', {
            method: 'POST', headers: reqHeaders, body: JSON.stringify({ message: msg })
        });
        if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            alert('Error al enviar: ' + (err.detail || r.status));
            input.value = msg; // restaurar para que no se pierda
            return;
        }
        openChatModal(currentChatPhone);
    } catch(e) {
        alert('Error de red: ' + e.message);
        input.value = msg;
    }
}

// _apiHeaders removed — mesioHeaders() from mesio-utils.js is used instead

function _isAdminOrOwner() {
    try {
        const roles = JSON.parse(localStorage.getItem('rb_role') || '[]');
        return Array.isArray(roles)
            ? (roles.includes('admin') || roles.includes('owner'))
            : (roles === 'admin' || roles === 'owner');
    } catch(e) { return false; }
}

// ── COMPROBANTES UNIFICADOS (mesas bot + domicilios) ───────────────
let _proposalsCache = {};  // base_order_id → proposal object

async function loadPaymentProofs() {
  const container = document.getElementById('proposals-list');
  if (!container) return;
  try {
    const [propRes, domRes, convRes] = await Promise.all([
      fetch('/api/checkout-proposals',       { headers: reqHeaders }),
      fetch('/api/delivery/orders',           { headers: reqHeaders }),
      fetch('/api/dashboard/conversations',   { headers: reqHeaders }),
    ]);

    const proposals  = propRes.ok  ? ((await propRes.json()).proposals  || []) : [];
    const allOrders  = domRes.ok   ? ((await domRes.json()).orders      || []) : [];
    const allConvs   = convRes.ok  ? ((await convRes.json()).conversations || []) : [];

    // Poblar caché para openEditProposalModal
    _proposalsCache = {};
    proposals.forEach(p => { _proposalsCache[p.base_order_id] = p; });

    const pendingDomicilios = allOrders.filter(o => o.status === 'pendiente' && o.order_type !== 'recoger');
    const voucherPhones = new Set(allConvs.filter(c => c.has_voucher).map(c => c.phone));

    const total = proposals.length + pendingDomicilios.length;
    const badge = document.getElementById('proposals-badge');
    if (badge) { badge.textContent = total; badge.style.display = total > 0 ? 'inline' : 'none'; }

    if (total === 0) {
      container.innerHTML = '<div style="padding:32px;text-align:center;color:#999;font-size:14px;">✅ No hay comprobantes ni domicilios pendientes.</div>';
      return;
    }

    container.innerHTML =
      proposals.map(p => renderProposalCard(p)).join('') +
      pendingDomicilios.map(o => renderDeliveryCard(o, voucherPhones.has(o.phone))).join('');
  } catch(e) {
    console.error('loadPaymentProofs:', e);
    container.innerHTML = `<p style="color:#e74c3c;padding:20px;">Error de red: ${e.message}</p>`;
  }
}

// Alias para referencias internas (confirmProposal, etc.)
function loadCheckoutProposals() { loadPaymentProofs(); }

// ── PEDIDOS PARA RECOGER ───────────────────────────────────────────
async function loadPickupOrders() {
  const container = document.getElementById('pickup-list');
  if (!container) return;
  try {
    const [domRes, convRes] = await Promise.all([
      fetch('/api/delivery/orders',          { headers: reqHeaders }),
      fetch('/api/dashboard/conversations',  { headers: reqHeaders }),
    ]);
    const allOrders = domRes.ok  ? ((await domRes.json()).orders        || []) : [];
    const allConvs  = convRes.ok ? ((await convRes.json()).conversations || []) : [];

    const pickups = allOrders.filter(o => ['pendiente','confirmado','listo'].includes(o.status) && o.order_type === 'recoger');
    const voucherPhones = new Set(allConvs.filter(c => c.has_voucher).map(c => c.phone));

    const badge = document.getElementById('pickup-badge');
    if (badge) { badge.textContent = pickups.length; badge.style.display = pickups.length > 0 ? 'inline' : 'none'; }

    if (pickups.length === 0) {
      container.innerHTML = '<div style="padding:32px;text-align:center;color:#999;font-size:14px;">✅ No hay pedidos para recoger pendientes.</div>';
      return;
    }
    container.innerHTML = pickups.map(o => renderPickupCard(o, voucherPhones.has(o.phone))).join('');
  } catch(e) {
    console.error('loadPickupOrders:', e);
    container.innerHTML = `<p style="color:#e74c3c;padding:20px;">Error de red: ${e.message}</p>`;
  }
}

async function confirmarPickup(id) {
  if (!confirm('¿Confirmar pedido y enviarlo a cocina?')) return;
  try {
    await fetch(`/api/delivery/orders/${id}/status`, {
      method: 'PATCH', headers: reqHeaders, body: JSON.stringify({ status: 'confirmado' })
    });
    loadPickupOrders();
  } catch(e) { alert('Error: ' + e.message); }
}

async function avisarListoPickup(id) {
  if (!confirm('¿Marcar como listo y notificar al cliente por WhatsApp?')) return;
  try {
    const r = await fetch(`/api/delivery/orders/${id}/status`, {
      method: 'PATCH', headers: reqHeaders, body: JSON.stringify({ status: 'listo' })
    });
    if (!r.ok) { const d = await r.json(); alert('Error: ' + (d.detail || r.status)); return; }
    loadPickupOrders();
    _refreshPickupBadge();
  } catch(e) { alert('Error: ' + e.message); }
}

async function marcarEntregadoPickup(id) {
  if (!confirm('¿Confirmar que el cliente ya recogió su pedido?')) return;
  try {
    const r = await fetch(`/api/delivery/orders/${id}/status`, {
      method: 'PATCH', headers: reqHeaders, body: JSON.stringify({ status: 'entregado' })
    });
    if (!r.ok) { const d = await r.json(); alert('Error: ' + (d.detail || r.status)); return; }
    loadPickupOrders();
    _refreshPickupBadge();
  } catch(e) { alert('Error: ' + e.message); }
}

function renderPickupCard(o, hasVoucher) {
  let itemsHtml = '';
  try {
    const arr = typeof o.items === 'string' ? JSON.parse(o.items) : (o.items || []);
    itemsHtml = arr.map(i => `<div style="font-size:13px;padding:2px 0;">${_escHtml(String(i.quantity||1))}× ${_escHtml(i.name)}</div>`).join('');
  } catch(e) {}

  const isPaid = ['nequi','daviplata','transferencia'].includes((o.payment_method||'').toLowerCase());
  const voucherBadge = isPaid
    ? (hasVoucher
        ? '<span style="background:#27ae60;color:white;border-radius:4px;padding:1px 7px;font-size:11px;">📎 Comprobante recibido</span>'
        : '<span style="background:#f39c12;color:white;border-radius:4px;padding:1px 7px;font-size:11px;">⏳ Esperando comprobante</span>')
    : '<span style="background:#6b7280;color:white;border-radius:4px;padding:1px 7px;font-size:11px;">💵 Paga al recoger</span>';

  const isPendiente  = o.status === 'pendiente';
  const isConfirmado = o.status === 'confirmado';
  const isListo      = o.status === 'listo';

  const statusBadge = isConfirmado
    ? '<span style="background:#f59e0b;color:white;border-radius:4px;padding:1px 7px;font-size:11px;margin-left:6px;">🍳 En preparación</span>'
    : isListo
      ? '<span style="background:#16a34a;color:white;border-radius:4px;padding:1px 7px;font-size:11px;margin-left:6px;animation:pulse 1.5s infinite;">✅ LISTO PARA ENTREGAR</span>'
      : '';

  const borderColor = isListo ? '#16a34a' : '#8b5cf6';
  const shadowColor = isListo ? 'rgba(22,163,74,0.15)' : 'rgba(139,92,246,0.12)';

  const actionBtns = isPendiente
    ? `<button onclick="confirmarPickup('${o.id}')"
         style="flex:1;background:#6366f1;color:white;border:none;border-radius:8px;padding:10px 12px;cursor:pointer;font-size:13px;font-weight:600;">
         🍳 Enviar a cocina
       </button>`
    : isListo
      ? `<button onclick="marcarEntregadoPickup('${o.id}')"
           style="flex:1;background:#16a34a;color:white;border:none;border-radius:8px;padding:10px 12px;cursor:pointer;font-size:13px;font-weight:600;">
           ✅ Entregar al cliente
         </button>`
      : '';

  return `
    <div style="background:white;border-radius:12px;padding:18px;min-width:260px;max-width:300px;box-shadow:0 2px 10px ${shadowColor};border-top:4px solid ${borderColor};">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <span style="background:#8b5cf6;color:white;border-radius:6px;padding:3px 10px;font-size:12px;font-weight:700;">🛍️ Para Recoger</span>
        <span style="font-size:12px;color:#999;">${_escHtml(o.phone||'')}</span>
      </div>
      <div style="margin-bottom:8px;display:flex;flex-wrap:wrap;gap:4px;">${voucherBadge}${statusBadge}</div>
      <div style="border-top:1px solid #f3f0ff;padding-top:8px;margin-bottom:8px;">${itemsHtml}</div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <span style="font-size:13px;color:#666;">${_escHtml(o.payment_method||'Sin método')}</span>
        <strong style="font-size:16px;color:#18181b;">${fmt(o.total||0)}</strong>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        ${actionBtns}
        <button onclick="openChatModal('${_escHtml(o.phone||'')}')"
          style="background:#f3f0ff;color:#8b5cf6;border:none;border-radius:8px;padding:10px 12px;cursor:pointer;font-size:13px;">
          💬
        </button>
        ${_isAdminOrOwner()
          ? `<button onclick="cancelarDomicilio('${o.id}')"
               style="background:#fee2e2;color:#dc2626;border:none;border-radius:8px;padding:10px 10px;cursor:pointer;font-size:13px;"
               title="Solo admin/owner">❌</button>`
          : ''}
      </div>
    </div>`;
}

function renderDeliveryCard(o, hasVoucher) {
  let itemsHtml = '';
  try {
    const arr = typeof o.items === 'string' ? JSON.parse(o.items) : (o.items || []);
    itemsHtml = arr.map(i => `<div style="font-size:13px;padding:1px 0;">${_escHtml(String(i.quantity||1))}× ${_escHtml(i.name)}</div>`).join('');
  } catch(e) {}

  const typeLabel = o.order_type === 'recoger' ? '🛍️ Recoger' : '🛵 Domicilio';
  const typeBg    = o.order_type === 'recoger' ? '#8b5cf6'   : '#f59e0b';

  const voucherBadge = hasVoucher
    ? '<span style="background:#27ae60;color:white;border-radius:4px;padding:1px 6px;font-size:11px;margin-left:6px;">📎 Comprobante enviado</span>'
    : '<span style="background:#f39c12;color:white;border-radius:4px;padding:1px 6px;font-size:11px;margin-left:6px;">⏳ Sin comprobante</span>';

  const chatBtn = `<button onclick="openChatModal('${o.phone}')"
    style="background:#ecf0f1;color:#333;border:none;border-radius:6px;padding:8px 10px;cursor:pointer;font-size:13px;">
    💬 Chat
  </button>`;

  const confirmBtn = `<button onclick="confirmarDomicilio('${o.id}')"
    style="background:#27ae60;color:white;border:none;border-radius:6px;padding:8px 14px;cursor:pointer;font-size:13px;flex:1;">
    ✅ Confirmar
  </button>`;

  const safePhone = _escHtml(o.phone || '');

  return `
    <div style="background:white;border-radius:10px;padding:16px;min-width:260px;max-width:300px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <span style="background:${typeBg};color:white;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:700;">${typeLabel}</span>
        <span style="font-size:12px;color:#999;">${safePhone}</span>
      </div>
      ${voucherBadge}
      <div style="margin:8px 0;border-top:1px solid #eee;padding-top:8px;">
        ${itemsHtml}
        ${o.order_type === 'domicilio' && o.address
          ? `<div style="font-size:12px;color:#666;margin-top:4px;">&#128205; ${_escHtml(o.address)}</div>`
          : o.order_type === 'recoger'
            ? `<div style="font-size:12px;color:#8b5cf6;margin-top:4px;font-weight:600;">🛍️ Pasa a recoger en restaurante</div>`
            : ''}
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <span style="font-size:13px;color:#666;">${o.payment_method || 'Sin método'}</span>
        <strong style="font-size:15px;">${fmt(o.total || 0)}</strong>
      </div>
      <div style="display:flex;gap:8px;">
        ${confirmBtn}
        ${chatBtn}
        ${_isAdminOrOwner()
            ? `<button onclick="cancelarDomicilio('${o.id}')"
                 style="background:#fee2e2;color:#dc2626;border:none;border-radius:6px;padding:8px 10px;cursor:pointer;font-size:13px;"
                 title="Solo admin/owner">❌</button>`
            : ''}
      </div>
    </div>`;
}

function renderProposalCard(p) {
  const checks = Array.isArray(p.checks) ? p.checks :
    (typeof p.checks === 'string' ? JSON.parse(p.checks) : []);

  const timeAgo = checks[0]?.proposal_created_at ? _timeAgo(checks[0].proposal_created_at) : '';
  const customerPhone = checks[0]?.proposal_customer_phone || '';

  // Por-check: solo bloquea si algún check digital aún no tiene comprobante
  const anyAwaitingProof = checks.some(c => c.proposal_status === 'awaiting_proof' && !c.proof_media_url);
  const canConfirm = !anyAwaitingProof;

  const checksHtml = checks.map(c => {
    const pmts = Array.isArray(c.proposed_payments) ? c.proposed_payments :
      (typeof c.proposed_payments === 'string' ? JSON.parse(c.proposed_payments || '[]') : []);
    const method = pmts[0]?.method || '?';
    const amount = fmt(c.total || 0);
    let proofHtml = '';
    if (c.proposal_status === 'awaiting_proof' && !c.proof_media_url) {
      proofHtml = '<span style="color:#f39c12;font-size:11px;">⏳ sin comprobante</span>';
    } else if (c.proof_media_url) {
      proofHtml = `<a href="${c.proof_media_url}" target="_blank" style="color:#3498db;font-size:12px;">📎 Ver comprobante</a>`;
    }
    return `<div style="font-size:13px;padding:3px 0;display:flex;justify-content:space-between;align-items:center;">
      <span>#${c.check_number} ${amount} · ${method}</span>${proofHtml}
    </div>`;
  }).join('');

  const tipTotal = checks.reduce((s, c) => s + parseFloat(c.tip_amount || 0), 0);
  const tipHtml = tipTotal > 0
    ? `<div style="font-size:13px;color:#27ae60;">Propina: ${fmt(tipTotal)}</div>`
    : '';

  const statusBadge = anyAwaitingProof
    ? '<span style="background:#f39c12;color:white;border-radius:4px;padding:1px 6px;font-size:11px;">⏳ Esperando comprobante</span>'
    : '<span style="background:#27ae60;color:white;border-radius:4px;padding:1px 6px;font-size:11px;">✅ Listo para cobrar</span>';

  const tableName = document.createTextNode(p.table_name || 'Mesa').nodeValue;
  const tableLabel = `<span style="background:#3498db;color:white;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:700;">🪑 ${tableName}</span>`;
  const confirmBtn = canConfirm
    ? `<button onclick="confirmProposal('${p.base_order_id}')"
         style="background:#27ae60;color:white;border:none;border-radius:6px;padding:8px 14px;cursor:pointer;font-size:13px;flex:1;">
         ✅ Confirmar y Facturar
       </button>`
    : `<button disabled
         style="background:#ccc;color:#666;border:none;border-radius:6px;padding:8px 14px;font-size:13px;flex:1;cursor:not-allowed;">
         ⏳ Esperando comprobante
       </button>`;

  const chatBtn = customerPhone
    ? `<button onclick="openChatModal('${customerPhone}')"
         style="background:#25D366;color:white;border:none;border-radius:6px;padding:8px 10px;cursor:pointer;font-size:13px;"
         title="Ver chat del cliente">💬</button>`
    : '';

  return `
    <div style="background:white;border-radius:10px;padding:16px;min-width:260px;max-width:300px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        ${tableLabel}
        <span style="font-size:11px;color:#999;">${timeAgo} 🤖 BOT</span>
      </div>
      ${statusBadge}
      <div style="margin:8px 0;border-top:1px solid #eee;padding-top:8px;">
        ${checks.length > 1 ? `<div style="font-size:12px;color:#666;margin-bottom:4px;">Dividido en ${checks.length}</div>` : ''}
        ${checksHtml}
        ${tipHtml}
      </div>
      <div style="display:flex;gap:8px;margin-top:10px;">
        ${confirmBtn}
        ${chatBtn}
        <button onclick="openEditProposalModal('${p.base_order_id}')"
          style="background:#ecf0f1;color:#333;border:none;border-radius:6px;padding:8px 10px;cursor:pointer;font-size:13px;"
          title="Ver detalle / cancelar propuesta">✏️</button>
        ${_isAdminOrOwner()
            ? `<button onclick="cancelProposal('${p.base_order_id}')"
                 style="background:#fee2e2;color:#dc2626;border:none;border-radius:6px;padding:8px 10px;cursor:pointer;font-size:13px;"
                 title="Solo admin/owner">❌</button>`
            : ''}
      </div>
    </div>`;
}

function _timeAgo(isoStr) {
  try {
    const diff = Math.floor((Date.now() - new Date(isoStr)) / 60000);
    if (diff < 1) return 'ahora';
    if (diff < 60) return `hace ${diff} min`;
    return `hace ${Math.floor(diff / 60)}h`;
  } catch(e) { return ''; }
}

async function confirmProposal(baseOrderId) {
  // Mostrar preview de factura antes de facturar
  const p = _proposalsCache[baseOrderId];
  const checks = p
    ? (Array.isArray(p.checks) ? p.checks : JSON.parse(p.checks || '[]'))
    : [];

  // Intentar obtener ítems reales de la orden (desde caché o API)
  let bill = typeof _billsCache !== 'undefined' ? _billsCache[baseOrderId] : null;
  if (!bill) {
    try {
      const r = await fetch('/api/table-orders', { headers: mesioHeaders() });
      const data = await r.json();
      const all = data.orders || data || [];
      const matches = all.filter(o => o.base_order_id === baseOrderId || o.id === baseOrderId);
      if (matches.length) {
        bill = {
          table_name: matches[0].table_name || p?.table_name || 'Mesa',
          total: matches.reduce((s, o) => s + parseFloat(o.total || 0), 0),
          items: matches.flatMap(o => {
            const its = typeof o.items === 'string' ? JSON.parse(o.items || '[]') : (o.items || []);
            return its;
          }),
          created_at: matches[0].created_at,
        };
      }
    } catch(e) {}
  }

  if (!bill) {
    // Fallback: construir bill mínimo desde los checks del proposal
    bill = {
      table_name: p?.table_name || 'Mesa',
      total: checks.reduce((s, c) => s + parseFloat(c.total || 0), 0),
      items: checks.map(c => ({ name: `Cuenta ${c.check_number}`, quantity: 1, price: parseFloat(c.total || 0) })),
      created_at: checks[0]?.proposal_created_at || new Date().toISOString(),
    };
  }

  const paymentContext = {
    payments: checks.map(c => {
      const pmts = Array.isArray(c.proposed_payments) ? c.proposed_payments :
        (typeof c.proposed_payments === 'string' ? JSON.parse(c.proposed_payments || '[]') : []);
      const method = (pmts[0]?.method || '?').charAt(0).toUpperCase() + (pmts[0]?.method || '?').slice(1);
      return { method: `Cuenta ${c.check_number} · ${method}`, amount: parseFloat(c.total || 0) };
    }),
    customerName: 'Consumidor Final',
    customerNit: '222222222',
    change: 0,
  };

  const tipTotal = checks.reduce((s, c) => s + parseFloat(c.tip_amount || 0), 0);
  if (tipTotal > 0) {
    paymentContext.payments.push({ method: 'Propina', amount: tipTotal });
  }

  document.getElementById('ppm-preview').innerHTML = _buildReceiptHtml(bill, paymentContext);
  document.getElementById('ppm-confirm-btn').onclick = async () => {
    document.getElementById('proposal-pay-modal').style.display = 'none';
    await _executeConfirmProposal(baseOrderId);
  };
  document.getElementById('proposal-pay-modal').style.display = 'flex';
}

async function _executeConfirmProposal(baseOrderId) {
  try {
    const res = await fetch(`/api/table-orders/${baseOrderId}/checks`, { headers: mesioHeaders() });
    if (!res.ok) throw new Error('No se pudieron obtener los checks');
    const data = await res.json();
    const checks = (data.checks || []).filter(c => c.status === 'open');

    for (const check of checks) {
      const payRes = await fetch(
        `/api/table-orders/${baseOrderId}/checks/${check.id}/pay`,
        {
          method: 'POST',
          headers: { ...mesioHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify({
            payments: [],
            customer_name: 'Consumidor Final',
            customer_nit: '222222222',
            tip_amount: check.tip_amount || 0,
          })
        }
      );
      if (!payRes.ok) {
        const err = await payRes.json().catch(() => ({}));
        throw new Error(err.detail || `Error al pagar check ${check.check_number}`);
      }
    }
    alert('✅ Pagos confirmados y facturados correctamente');
    loadCheckoutProposals();
    if (typeof loadCajaOrders === 'function') loadCajaOrders();
  } catch(e) {
    alert('Error: ' + e.message);
  }
}

function editProposalManually(baseOrderId) {
  openEditProposalModal(baseOrderId);
}

function openEditProposalModal(baseOrderId) {
  const p = _proposalsCache[baseOrderId];
  if (!p) { alert('Propuesta no encontrada. Recarga la página.'); return; }

  const checks = Array.isArray(p.checks) ? p.checks :
    (typeof p.checks === 'string' ? JSON.parse(p.checks) : []);
  const customerPhone = checks[0]?.proposal_customer_phone || '';
  const tableName = p.table_name || 'Mesa';

  document.getElementById('epm-title').textContent = `Detalle — ${tableName}`;

  const rows = checks.map(c => {
    const pmts = Array.isArray(c.proposed_payments) ? c.proposed_payments :
      (typeof c.proposed_payments === 'string' ? JSON.parse(c.proposed_payments || '[]') : []);
    const method = pmts[0]?.method || '?';
    const amount = fmt(c.total || 0);
    let proofCell = '—';
    if (c.proof_media_url) {
      proofCell = `<a href="${c.proof_media_url}" target="_blank" style="color:#3498db;">📎 Ver</a>`;
    } else if (c.proposal_status === 'awaiting_proof') {
      proofCell = '<span style="color:#f39c12;">⏳ Pendiente</span>';
    }
    return `<tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:8px 6px;font-weight:600;">#${c.check_number}</td>
      <td style="padding:8px 6px;">${amount}</td>
      <td style="padding:8px 6px;">${method}</td>
      <td style="padding:8px 6px;">${proofCell}</td>
    </tr>`;
  }).join('');

  document.getElementById('epm-checks').innerHTML = `
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="color:#999;font-size:11px;text-transform:uppercase;">
        <th style="padding:6px;text-align:left;">#</th>
        <th style="padding:6px;text-align:left;">Monto</th>
        <th style="padding:6px;text-align:left;">Método</th>
        <th style="padding:6px;text-align:left;">Comprobante</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  document.getElementById('epm-chat-btn').style.display = customerPhone ? '' : 'none';
  document.getElementById('epm-chat-btn').onclick = () => {
    closeEditProposalModal();
    openChatModal(customerPhone);
  };
  document.getElementById('epm-cancel-btn').onclick = async () => {
    closeEditProposalModal();
    await cancelProposal(baseOrderId);
  };
  document.getElementById('epm-goto-btn').onclick = () => {
    closeEditProposalModal();
    switchTab('mesas');
    if (typeof selectOrder === 'function') selectOrder(baseOrderId);
  };

  document.getElementById('edit-proposal-modal').style.display = 'flex';
}

function closeEditProposalModal() {
  document.getElementById('edit-proposal-modal').style.display = 'none';
}

async function cancelProposal(baseOrderId) {
    if (!confirm('¿Cancelar esta propuesta de pago? El cliente deberá reiniciar el proceso.')) return;
    try {
        const r = await fetch(`/api/checkout-proposals/${baseOrderId}`, {
            method: 'DELETE', headers: reqHeaders
        });
        if (!r.ok) { const e = await r.json().catch(()=>{}); alert('Error: ' + (e?.detail || r.status)); return; }
        loadPaymentProofs();
    } catch(e) { alert('Error: ' + e.message); }
}

async function cancelarDomicilio(id) {
    if (!confirm('¿Cancelar este domicilio?')) return;
    try {
        const r = await fetch(`/api/delivery/orders/${id}/status`, {
            method: 'PATCH', headers: reqHeaders, body: JSON.stringify({status: 'cancelado'})
        });
        if (!r.ok) { const e = await r.json().catch(()=>{}); alert('Error: ' + (e?.detail || r.status)); return; }
        loadPaymentProofs();
    } catch(e) { alert('Error: ' + e.message); }
}

async function _refreshPickupBadge() {
  try {
    const r = await fetch('/api/delivery/orders', { headers: reqHeaders });
    if (!r.ok) return;
    const orders = (await r.json()).orders || [];
    const count = orders.filter(o => ['pendiente','confirmado','listo'].includes(o.status) && o.order_type === 'recoger').length;
    const badge = document.getElementById('pickup-badge');
    if (badge) { badge.textContent = count; badge.style.display = count > 0 ? 'inline' : 'none'; }
  } catch(e) {}
}

loadBillingConfig();
loadCajaOrders();
loadPaymentProofs();
_refreshPickupBadge();

setInterval(() => {
  loadCajaOrders();
  loadPaymentProofs();
  _refreshPickupBadge();
}, 5000);

// ── NUEVA FACTURA RÁPIDA — POS full-screen ────────────────────────────
let _qimItems = [];   // [{name, qty, unit_price}]
let _qimTip   = 0;
let _qimType  = 'salon';
let _qimActiveTipPct = 0;

const _qimFmt = n => '$' + Math.round(n).toLocaleString('es-CO');

function openQuickInvoiceModal() {
  _qimItems = [];
  _qimTip   = 0;
  _qimActiveTipPct = 0;
  _qimType  = 'salon';
  document.getElementById('qim-tip-custom').value    = '';
  document.getElementById('qim-customer-name').value = 'Consumidor Final';
  document.getElementById('qim-customer-nit').value  = '222222222';
  document.getElementById('qim-customer-email').value = '';
  document.getElementById('qim-payment').value = 'efectivo';
  _qimHighlightTip(0);
  _qimRenderCart();
  _qimRenderTotals();
  _qimLoadMenu();
  document.getElementById('quick-invoice-screen').classList.add('open');
}

function closeQuickInvoiceModal() {
  document.getElementById('quick-invoice-screen').classList.remove('open');
}

function qimSetType(t) {
  _qimType = t;
  ['salon','domicilio'].forEach(id => {
    document.getElementById('qim-type-' + id).classList.toggle('active', id === t);
  });
}

// ── Menú ──────────────────────────────────────────────────────────────
async function _qimLoadMenu() {
  const area = document.getElementById('qim-menu-area');
  area.innerHTML = '<div style="text-align:center;padding:40px;color:#aaa;">Cargando menú...</div>';
  try {
    const r = await fetch('/api/pos/menu', { headers: reqHeaders });
    const d = await r.json();
    _qimRenderMenu(d.menu || {});
  } catch(e) {
    area.innerHTML = '<div style="text-align:center;padding:40px;color:#e74c3c;">Error al cargar el menú.</div>';
  }
}

function _qimRenderMenu(menu) {
  const area = document.getElementById('qim-menu-area');
  if (!Object.keys(menu).length) {
    area.innerHTML = '<div style="text-align:center;padding:40px;color:#aaa;">El menú está vacío.</div>';
    return;
  }
  let html = '';
  for (const [cat, dishes] of Object.entries(menu)) {
    if (!Array.isArray(dishes) || !dishes.length) continue;
    html += `<div class="qim-cat-title">${_escHtml(cat)}</div><div class="qim-grid">`;
    dishes.forEach(d => {
      if (d.available === false) return;
      // Encode name for safe use in both the onclick attribute and the display text
      const safeAttr = _escHtml(d.name).replace(/'/g, '&#39;');
      html += `<div class="qim-card" onclick="qimAddItem('${safeAttr}',${Number(d.price)})">
        <div class="qim-card-name">${_escHtml(d.name)}</div>
        <div class="qim-card-price">${_qimFmt(d.price)}</div>
      </div>`;
    });
    html += `</div>`;
  }
  // Ítem personalizado al final
  html += `<div class="qim-cat-title" style="margin-top:24px;">Ítem personalizado</div>
  <div class="qim-custom-bar">
    <input id="qim-custom-name"  placeholder="Nombre del ítem">
    <input id="qim-custom-price" type="number" min="0" placeholder="Precio $" style="max-width:120px;">
    <button onclick="qimAddCustom()">+ Agregar</button>
  </div>`;
  area.innerHTML = html;
}

function qimAddItem(name, price) {
  const ex = _qimItems.find(i => i.name === name && i.unit_price === price);
  if (ex) { ex.qty++; } else { _qimItems.push({ name, qty:1, unit_price:price }); }
  _qimRenderCart();
  _qimRenderTotals();
  _qimFlash(name, price);
}

function _qimFlash(name, price) {
  // Pulso visual en la card tocada
  const cards = document.querySelectorAll('.qim-card');
  cards.forEach(c => {
    if (c.querySelector('.qim-card-name')?.textContent === name) {
      c.classList.add('added');
      setTimeout(() => c.classList.remove('added'), 300);
    }
  });
}

function qimAddCustom() {
  const nameEl  = document.getElementById('qim-custom-name');
  const priceEl = document.getElementById('qim-custom-price');
  const name  = nameEl?.value.trim();
  const price = parseFloat(priceEl?.value);
  if (!name || isNaN(price) || price < 0) { alert('Ingresa nombre y precio válido'); return; }
  qimAddItem(name, price);
  if (nameEl)  nameEl.value  = '';
  if (priceEl) priceEl.value = '';
}

// ── Carrito ───────────────────────────────────────────────────────────
function _qimRenderCart() {
  const el = document.getElementById('qim-cart-items');
  if (!_qimItems.length) {
    el.className = 'qim-cart-empty';
    el.textContent = 'Toca un producto del menú para agregarlo.';
    return;
  }
  el.className = '';
  el.innerHTML = _qimItems.map((it, i) => `
    <div class="qim-cart-item">
      <div class="qim-ci-name">${_escHtml(it.name)}</div>
      <div class="qim-qty-wrap">
        <button class="qim-qty-btn" onclick="qimQty(${i},-1)" aria-label="Reducir cantidad">−</button>
        <span style="font-weight:700;min-width:16px;text-align:center;font-size:13px;">${_escHtml(String(it.qty))}</span>
        <button class="qim-qty-btn" onclick="qimQty(${i},+1)" aria-label="Aumentar cantidad">+</button>
      </div>
      <div class="qim-ci-price">${_qimFmt(it.unit_price * it.qty)}</div>
    </div>`).join('');
}

function qimQty(i, delta) {
  _qimItems[i].qty += delta;
  if (_qimItems[i].qty <= 0) _qimItems.splice(i, 1);
  _qimRenderCart();
  _qimRenderTotals();
  // Recalcular propina si hay % activo
  if (_qimActiveTipPct > 0) qimSetTipPct(_qimActiveTipPct);
}

// ── Propina ───────────────────────────────────────────────────────────
function _qimHighlightTip(pct) {
  [0,10,15,20].forEach(p => {
    const btn = document.getElementById('qtip-' + p);
    if (btn) btn.classList.toggle('active', p === pct);
  });
}

function qimSetTipPct(pct) {
  _qimActiveTipPct = pct;
  const sub = _qimItems.reduce((s,it) => s + it.unit_price * it.qty, 0);
  _qimTip   = Math.round(sub * pct / 100);
  document.getElementById('qim-tip-custom').value = '';
  _qimHighlightTip(pct);
  _qimRenderTotals();
}

function qimTipCustom(v) {
  _qimActiveTipPct = -1;
  _qimTip = Math.max(0, parseFloat(v) || 0);
  _qimHighlightTip(-1);
  _qimRenderTotals();
}

// ── Totales ───────────────────────────────────────────────────────────
function _qimRenderTotals() {
  const sub   = _qimItems.reduce((s,it) => s + it.unit_price * it.qty, 0);
  const total = sub + _qimTip;
  document.getElementById('qim-subtotal').textContent  = _qimFmt(sub);
  document.getElementById('qim-tip-total').textContent = _qimFmt(_qimTip);
  document.getElementById('qim-total').textContent     = _qimFmt(total);
}

// ── Enviar ────────────────────────────────────────────────────────────
async function submitQuickInvoice() {
  if (!_qimItems.length) { alert('Agrega al menos un ítem'); return; }
  const body = {
    items:          _qimItems.map(it => ({ name: it.name, qty: it.qty, unit_price: it.unit_price })),
    tip_amount:     _qimTip,
    payment_method: document.getElementById('qim-payment').value,
    customer_name:  document.getElementById('qim-customer-name').value.trim() || 'Consumidor Final',
    customer_nit:   document.getElementById('qim-customer-nit').value.trim()  || '222222222',
    customer_email: document.getElementById('qim-customer-email').value.trim(),
    order_type:     _qimType,
    table_name:     _qimType === 'domicilio' ? 'Domicilio' : 'Caja',
  };
  const btn = document.getElementById('qim-gen-btn');
  btn.disabled = true; btn.textContent = 'Procesando...';
  try {
    const r = await fetch('/api/pos/quick-invoice', {
      method: 'POST',
      headers: { ...reqHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) { alert('Error: ' + (d.detail || r.status)); return; }
    closeQuickInvoiceModal();
    alert(`✅ Factura generada. Total: ${_qimFmt(d.total)}`);
    loadCajaOrders();
    loadPaymentProofs();
  } catch(e) { alert('Error de red: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = '🧾 Generar Factura'; }
}
