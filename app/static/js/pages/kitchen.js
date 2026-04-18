const token = localStorage.getItem('rb_token');
if (!token) window.location.href = '/login';
const hdr = { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' };

function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function clock() {
  const el = document.getElementById('header-clock');
  if (el) el.textContent = new Date().toLocaleTimeString(navigator.language || 'default', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
clock(); setInterval(clock, 1000);

let notifTimer = null;
function notify(msg) {
  const n = document.getElementById('notif');
  n.textContent = msg;
  n.classList.add('show');
  if (notifTimer) clearTimeout(notifTimer);
  notifTimer = setTimeout(() => n.classList.remove('show'), 3500);
}

function elapsed(createdAt) {
  const iso  = createdAt.endsWith('Z') ? createdAt : createdAt + 'Z';
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  const min  = Math.max(0, Math.floor(diff / 60));
  const sec  = Math.max(0, diff % 60);
  const cls  = min < 5 ? 'ok' : min < 12 ? 'warn' : 'late';
  return `<span class="elapsed ${cls}">${min}m ${sec}s</span>`;
}

async function setStatus(orderId, status) {
  try {
    await fetch(`/api/table-orders/${orderId}/status`, {
      method: 'POST', headers: hdr, body: JSON.stringify({status})
    });
    if (status === 'listo')          notify('✅ Listo para servir — avisa al mesero');
    if (status === 'en_preparacion') notify('👨‍🍳 Preparando...');
    loadOrders();
  } catch(e) { mesioToast('Error al actualizar estado', 'error'); }
}

function renderCard(o) {
  _ordersMap[o.id] = o;
  const items   = Array.isArray(o.items) ? o.items : [];
  const subNum  = o.sub_number || 1;
  const isAdd   = o.base_order_id && subNum > 1;
  const isDel   = o.is_delivery || false;

  const subTag = isDel
    ? `<div class="sub-tag add">🛵 Domicilio/Recoger</div>`
    : isAdd
      ? `<div class="sub-tag add">➕ Adicional #${subNum}</div>`
      : `<div class="sub-tag first">🆕 Pedido inicial</div>`;

  const itemsHtml = items.length
    ? items.map(i => `<div class="order-item"><span class="order-item-qty">${escHtml(String(i.quantity || i.qty || 1))}×</span><span>${escHtml(i.name || '')}</span></div>`).join('')
    : '<div class="order-item" style="color:#555;">Sin items</div>';

  const notesHtml = o.notes ? `<div class="order-notes">📝 ${escHtml(o.notes)}</div>` : '';

  // IDs are server-generated UUIDs — safe for attribute interpolation but still escaped defensively
  const safeId  = escHtml(o.id);
  const safeOid = escHtml(o.original_id || o.id);

  const isPickup = o.order_type === 'recoger';

  let actionHtml = '';
  if (isDel) {
    if (o.status === 'recibido') {
      actionHtml = `<button class="btn btn-prep" onclick="setDeliveryStatus('${safeOid}','en_preparacion')">👨‍🍳 En preparación</button>`;
    } else if (o.status === 'en_preparacion') {
      actionHtml = isPickup
        ? `<button class="btn btn-listo" onclick="setDeliveryStatus('${safeOid}','listo')">🛍️ Listo para recoger</button>`
        : `<button class="btn btn-listo" onclick="setDeliveryStatus('${safeOid}','listo')">✅ Listo para domiciliario</button>`;
    } else {
      actionHtml = `<div class="listo-badge">📦 Esperando domiciliario</div>`;
    }
  } else {
    if (o.status === 'recibido') {
      actionHtml = `<button class="btn btn-prep" onclick="setStatus('${safeId}','en_preparacion')">👨‍🍳 En preparación</button>`;
    } else if (o.status === 'en_preparacion') {
      actionHtml = `<button class="btn btn-listo" onclick="setStatus('${safeId}','listo')">✅ Listo para servir</button>`;
    } else {
      actionHtml = `<div class="listo-badge">🛎️ Esperando al mesero</div>`;
    }
  }

  return `<div class="order-card ${escHtml(o.status.replace('_','-'))}" data-id="${safeId}">
    ${subTag}
    <div class="order-header">
      <div><div class="order-table">${escHtml(o.table_name)}</div><div class="order-id">${safeId}</div></div>
      ${elapsed(o.created_at)}
    </div>
    <div class="order-items">${itemsHtml}</div>
    ${notesHtml}
    <button class="btn btn-print" onclick="printComanda('${safeId}')">🖨️ Imprimir Comanda</button>
    ${actionHtml}
  </div>`;
}

async function setDeliveryStatus(orderId, status) {
  try {
    // Usamos la nueva ruta de la cocina
    await fetch(`/api/kitchen/delivery-orders/${orderId}/status`, {
      method: 'PATCH', headers: hdr, body: JSON.stringify({ status })
    });
    if (status === 'listo') notify('📦 Listo para domiciliario');
    if (status === 'en_preparacion') notify('👨‍🍳 Preparando domicilio...');
    loadOrders();
  } catch(e) { mesioToast('Error al actualizar estado', 'error'); }
}

const _ordersMap = {};

function printComanda(orderId) {
  const o = _ordersMap[orderId];
  if (!o) return;
  const items = Array.isArray(o.items) ? o.items : [];
  const iso = (o.created_at || '').endsWith('Z') ? o.created_at : (o.created_at || '') + 'Z';
  const dt = new Date(iso);
  const timeStr = dt.toLocaleTimeString('es-CO', {hour:'2-digit', minute:'2-digit'});
  const dateStr = dt.toLocaleDateString('es-CO');
  const subNum = o.sub_number || 1;
  const subLabel = (o.base_order_id && subNum > 1) ? `ADICIONAL #${subNum}` : 'PEDIDO INICIAL';
  const itemsHtml = items.map(i =>
    `<div class="item"><span class="qty">${escHtml(String(i.quantity||i.qty||1))}x</span> ${escHtml(i.name||'')}</div>`
  ).join('');
  const notesHtml = o.notes
    ? `<div class="sep"></div><div class="notes">NOTAS: ${escHtml(o.notes)}</div>`
    : '';
  const win = window.open('', '_blank', 'width=340,height=560,toolbar=0,menubar=0');
  win.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Comanda ${escHtml(o.table_name)}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Courier New',Courier,monospace;width:72mm;padding:4mm;font-size:10pt;color:#000;background:#fff}
@page{size:80mm auto;margin:0}
.title{font-size:13pt;font-weight:bold;text-align:center;letter-spacing:2px}
.sub{font-size:8pt;text-align:center;margin-bottom:2px}
.sep{border-top:1px dashed #000;margin:5px 0}
.label{font-size:8pt;color:#444;margin-top:3px}
.mesa{font-size:15pt;font-weight:bold}
.item{font-size:11pt;margin:3px 0}
.qty{font-weight:bold;display:inline-block;width:22px}
.notes{font-size:9pt;font-style:italic;margin-top:3px}
.footer{text-align:center;font-size:8pt;margin-top:4px}
</style></head><body>
<div class="title">COMANDA</div>
<div class="sub">${subLabel}</div>
<div class="sep"></div>
<div class="label">MESA</div>
<div class="mesa">${escHtml(o.table_name)}</div>
<div class="label">Hora: ${timeStr} &nbsp;|&nbsp; ${dateStr}</div>
<div class="sep"></div>
<div class="label" style="font-weight:bold;margin-bottom:3px">ITEMS</div>
${itemsHtml}
${notesHtml}
<div class="sep"></div>
<div class="footer">— Mesio POS —</div>
</body></html>`);
  win.document.close();
  win.focus();
  win.onload = () => { win.print(); };
}

let prevIds = new Set();

async function loadOrders() {
  if (document.visibilityState === 'hidden') return;
  try {
    const r1 = await fetch('/api/table-orders?station=kitchen', { headers: hdr });
    if (!r1.ok) { if (r1.status === 401) window.location.href = '/login'; return; }
    const { orders: tableOrders = [] } = await r1.json();

    const r2 = await fetch('/api/kitchen/delivery-orders', { headers: hdr });
    const { orders: deliveryOrders = [] } = r2.ok ? await r2.json() : { orders: [] };

    const normalizedDelivery = deliveryOrders
      .filter(o => !['en_camino', 'en_puerta', 'entregado', 'cancelado'].includes(o.status))
      .filter(o => !(o.order_type === 'recoger' && o.status === 'listo')) // listo pickups → caja
      .map(o => ({
        id: o.id,
        order_type: o.order_type,
        table_name: o.order_type === 'domicilio'
          ? `🛵 Domicilio — ${(o.phone || '').slice(-4)}`
          : `🛍️ Recoger — ${(o.phone || '').slice(-4)}`,
        items: Array.isArray(o.items) ? o.items : [],
        notes: (o.notes || '') + (o.payment_method ? ` | Pago: ${o.payment_method}` : '') + (o.address ? ` | 📍 ${o.address}` : ''),
        status: mapDeliveryStatus(o.status),
        created_at: o.created_at,
        sub_number: 1,
        base_order_id: null,
        is_delivery: true,
        original_id: o.id,
      }));

    const orders = [...tableOrders, ...normalizedDelivery.filter(o => o.status !== null)];

    const curIds = new Set(orders.map(o => o.id));
    for (const id of curIds) {
      if (!prevIds.has(id)) {
        const o = orders.find(x => x.id === id);
        if (o) notify(`🔔 Nuevo pedido — ${o.table_name}`);
      }
    }
    prevIds = curIds;

    const groups = { recibido: [], en_preparacion: [], listo: [] };
    orders.forEach(o => { if (groups[o.status]) groups[o.status].push(o); });

    [['recibido', 'col-recibido', 'cnt-recibido'],
     ['en_preparacion', 'col-preparacion', 'cnt-preparacion'],
     ['listo', 'col-listo', 'cnt-listo']].forEach(([status, colId, cntId]) => {
      const col = document.getElementById(colId);
      const cnt = document.getElementById(cntId);
      if (col) col.innerHTML = groups[status].length
        ? groups[status].map(renderCard).join('')
        : '<div class="empty">Sin pedidos</div>';
      if (cnt) cnt.textContent = groups[status].length;
    });

    mesioTrackFetch(true);
  } catch(e) { mesioTrackFetch(false); }
}

function mapDeliveryStatus(s) {
  if (s === 'confirmado') return 'recibido';
  if (s === 'en_preparacion') return 'en_preparacion';
  if (s === 'listo') return 'listo';
  // en_camino / en_puerta / entregado: ya salieron de cocina — no deben aparecer
  return null;
}

loadOrders();
setInterval(loadOrders, 5000);

function doLogout() { doStaffLogout(); }
