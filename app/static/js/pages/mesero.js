// --- UTILIDADES ---
function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

// --- AUTENTICACIÓN Y LOCALIZACIÓN ---
const token = localStorage.getItem('rb_token');
const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
const _locale = restaurant.locale || 'en-US';
const _currency = restaurant.currency || 'USD';

if (!token) window.location.href = '/login';
const headers = { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' };
const _restNameEl = document.getElementById('rest-name');
if (_restNameEl) _restNameEl.textContent = restaurant.name || 'Mi Restaurante';

// Formateador Universal Inteligente
const fmt = (amount) => {
    return new Intl.NumberFormat(_locale, {
        style: 'currency',
        currency: _currency,
        minimumFractionDigits: ['COP', 'CLP', 'PYG', 'JPY'].includes(_currency) ? 0 : 2
    }).format(Number(amount));
};

function doLogout() { doStaffLogout(); }

function switchTab(id, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}

let toastTimer;
function showToast(text, type='info') {
  const t = document.getElementById('toast');
  document.getElementById('toast-icon').textContent = type === 'waiter' ? '🙋' : '🔔';
  document.getElementById('toast-text').textContent = text;
  t.className = 'toast show';
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 4000);
}

// ════════════════════════════════════════════════════════
// ── LÓGICA DEL POS Y MAPA DE MESAS (NUEVO) ──────────────
// ════════════════════════════════════════════════════════
let posMenu = null;
let posCart = [];
let currentPosTable = null;
let currentPosTableName = '';

async function loadTablesStatus() {
    try {
        const r = await fetch('/api/pos/tables-status', { headers });
        if (!r.ok) {
            const errText = await r.text().catch(() => r.status);
            document.getElementById('tables-grid').innerHTML =
                `<div class="empty" style="color:red;">Error ${r.status} al cargar mesas. ${errText}</div>`;
            return;
        }
        const data = await r.json();
        const container = document.getElementById('tables-grid');

        if (!data.tables || data.tables.length === 0) {
            container.innerHTML = '<div class="empty">No hay mesas configuradas. Un administrador debe crearlas.</div>';
            return;
        }

        container.innerHTML = data.tables.map(t => {
            let statusClass = 'tb-free';
            let statusText = 'Libre';

            // Lógica de colores semánticos
            if (t.pending_orders && t.pending_orders.length > 0) {
                statusClass = 'tb-busy'; statusText = '🍽️ Ocupada / Esperando';
            } else if (t.bot_active) {
                statusClass = 'tb-bot'; statusText = '🤖 Bot Atendiendo';
            }

            return `
            <div class="table-card ${statusClass}" onclick="openPOS('${t.id}', '${t.name}')">
                <div class="tc-name">${t.name}</div>
                <div class="tc-status">${statusText}</div>
            </div>`;
        }).join('');
    } catch(e) {
        document.getElementById('tables-grid').innerHTML =
            `<div class="empty" style="color:red;">Error inesperado al cargar mesas: ${e.message}</div>`;
    }
}

async function openPOS(tableId, tableName) {
    currentPosTable = tableId;
    currentPosTableName = tableName;
    document.getElementById('pos-table-name').textContent = `Ordenando en: ${tableName}`;

    // Limpiar carrito al abrir nueva mesa
    posCart = [];
    document.getElementById('pos-notes').value = '';
    renderCart();

    document.getElementById('pos-modal').classList.add('open');

    // Descargar el menú solo la primera vez que se abre el POS
    if (!posMenu) {
        document.getElementById('pos-menu-area').innerHTML = '<div class="empty spinner" style="color:#1D9E75;">Cargando productos...</div>';
        try {
            const r = await fetch('/api/pos/menu', { headers });
            const data = await r.json();
            posMenu = data.menu || {};
        } catch(e) {
            document.getElementById('pos-menu-area').innerHTML = '<div class="empty" style="color:red;">Error al cargar el menú. Verifica tu conexión.</div>';
            return;
        }
    }
    renderMenu();
}

function closePOS() {
    document.getElementById('pos-modal').classList.remove('open');
    document.getElementById('pos-cart-area').classList.remove('open');
    document.getElementById('pos-cart-backdrop').classList.remove('open');
}

function toggleMobileCart() {
    const isMobile = window.innerWidth < 800;
    if(isMobile) {
        document.getElementById('pos-cart-area').classList.toggle('open');
        document.getElementById('pos-cart-backdrop').classList.toggle('open');
        document.getElementById('mobile-cart-close').style.display = 'block';
    }
}

function renderMenu() {
    const container = document.getElementById('pos-menu-area');
    let html = '';

    if (Object.keys(posMenu).length === 0) {
        container.innerHTML = '<div class="empty">El menú del restaurante está vacío o no se ha configurado.</div>';
        return;
    }

    for (const [category, items] of Object.entries(posMenu)) {
        html += `<div class="menu-cat-title">${category}</div>`;
        html += `<div class="menu-grid">`;
        items.forEach(item => {
            const priceFmt = fmt(item.price); // Uso del formateador universal
            // Escapar comillas simples para evitar errores en el onclick
            const safeName = item.name.replace(/'/g, "\\'");
            html += `
            <div class="pos-item-card" onclick="addToCart('${safeName}', ${item.price})">
                <div class="pos-item-name">${item.name}</div>
                <div class="pos-item-price">${priceFmt}</div>
            </div>`;
        });
        html += `</div>`;
    }
    container.innerHTML = html;
}

function addToCart(name, price) {
    const existing = posCart.find(i => i.name === name);
    if (existing) {
        existing.quantity++;
    } else {
        posCart.push({ name: name, price: price, quantity: 1 });
    }
    renderCart();

    // Animación muy sutil tipo "haptic feedback" visual
    const btn = document.getElementById('mobile-cart-toggle');
    if(btn.style.display !== 'none') {
        btn.style.transform = 'scale(1.05)';
        setTimeout(() => btn.style.transform = 'scale(1)', 150);
    }
}

function updateQty(index, delta) {
    posCart[index].quantity += delta;
    if (posCart[index].quantity <= 0) {
        posCart.splice(index, 1); // Eliminar si llega a 0
    }
    renderCart();
}

function renderCart() {
    const container = document.getElementById('pos-cart-items');
    let total = 0;
    let count = 0;

    if (posCart.length === 0) {
        container.innerHTML = '<div class="empty" style="padding: 2rem;">Toca un producto del menú para agregarlo a la comanda.</div>';
        document.getElementById('pos-total').textContent = fmt(0);
        document.getElementById('m-cart-total').textContent = fmt(0);
        document.getElementById('m-cart-count').textContent = '0';
        document.getElementById('mobile-cart-toggle').style.display = 'none';
        return;
    }

    let html = '';
    posCart.forEach((item, index) => {
        const lineTotal = item.price * item.quantity;
        total += lineTotal;
        count += item.quantity;
        html += `
        <div class="cart-item">
            <div class="cart-item-info">
                <div style="margin-bottom:4px; line-height:1.2;">${item.name}</div>
                <div style="color:#1D9E75;">${fmt(lineTotal)}</div>
            </div>
            <div class="cart-item-qty">
                <button class="qty-btn" onclick="updateQty(${index}, -1)">−</button>
                <span style="font-weight:bold; width:15px; text-align:center;">${item.quantity}</span>
                <button class="qty-btn" onclick="updateQty(${index}, 1)">+</button>
            </div>
        </div>`;
    });

    container.innerHTML = html;
    const totalFmt = fmt(total);
    document.getElementById('pos-total').textContent = totalFmt;
    document.getElementById('m-cart-total').textContent = totalFmt;
    document.getElementById('m-cart-count').textContent = count;

    if (window.innerWidth < 800) {
        document.getElementById('mobile-cart-toggle').style.display = 'flex';
    } else {
        document.getElementById('mobile-cart-toggle').style.display = 'none';
    }
}

async function sendPosOrder() {
    if (posCart.length === 0) return alert('El carrito está vacío. Agrega productos primero.');

    const total = posCart.reduce((sum, item) => sum + (item.price * item.quantity), 0);
    const notes = document.getElementById('pos-notes').value.trim();
    const btn = document.querySelector('.btn-send-order');

    btn.disabled = true;
    btn.innerHTML = 'Enviando... ⏳';

    try {
        const res = await fetch('/api/pos/order', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({
                table_id: currentPosTable,
                table_name: currentPosTableName,
                items: posCart,
                total: total,
                notes: notes
            })
        });

        if (res.ok) {
            showToast('✅ Comanda enviada a cocina exitosamente.', 'waiter');
            closePOS();
            loadTablesStatus(); // Refresca las mesas para ponerla color Naranja (Ocupada)
            loadOrders();       // Refresca la pestaña de pedidos a cocina
        } else {
            alert('Hubo un problema al enviar la orden. Intenta nuevamente.');
        }
    } catch(e) {
        alert('Error de conexión con el servidor principal.');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '🚀 Enviar a Cocina';
    }
}


// ════════════════════════════════════════════════════════
// ── LÓGICA ORIGINAL (ALERTAS, PEDIDOS Y CHAT) ───────────
// ════════════════════════════════════════════════════════

// 1. Alertas
let knownAlertIds = new Set();
async function loadAlerts() {
  try {
    const r = await fetch('/api/waiter-alerts', { headers });
    if (!r.ok) return;
    const { alerts = [] } = await r.json();

    const badge = document.getElementById('badge-alertas');
    badge.textContent = alerts.length;
    badge.style.display = alerts.length > 0 ? 'inline-block' : 'none';

    const container = document.getElementById('alerts-list');
    if (!alerts.length) {
      container.innerHTML = '<div class="empty">Sin alertas activas.</div>';
      return;
    }

    container.innerHTML = alerts.map(a => {
      if (!knownAlertIds.has(a.id)) {
        knownAlertIds.add(a.id);
        if (a.alert_type === 'admin_call') {
          showToast('👔 El Administrador te llama', 'waiter');
        } else {
          showToast('🙋 ' + (a.table_name || 'Cliente') + ' solicita atención', 'waiter');
        }
      }
      if (a.alert_type === 'admin_call') {
        return `
        <div class="alert-card" id="alert-${a.id}" style="border-left:4px solid #7C3AED;background:#F5F3FF;">
          <div class="alert-icon">👔</div>
          <div style="flex:1;">
            <div class="alert-title" style="color:#6D28D9;">Llamado del Administrador</div>
            <div class="alert-meta">${a.message || 'El Administrador requiere verte en caja/dashboard'}</div>
          </div>
          <button onclick="dismissAlert(${a.id})" class="btn" style="background:#EDE9FE;color:#6D28D9;">✓ En camino</button>
        </div>`;
      }
      return `
      <div class="alert-card" id="alert-${a.id}">
        <div class="alert-icon">🙋</div>
        <div style="flex:1;">
          <div class="alert-title">Llamada de cliente — ${a.table_name || 'Mesa'}</div>
          <div class="alert-meta">${a.message || 'El cliente necesita a un mesero'}</div>
        </div>
        <button onclick="dismissAlert(${a.id})" class="btn" style="background:#eee; color:#333;">✓ Atendido</button>
      </div>`;
    }).join('');
  } catch(e) {}
}

async function dismissAlert(id) {
  await fetch('/api/waiter-alerts/' + id + '/dismiss', { method: 'POST', headers });
  knownAlertIds.delete(id);
  loadAlerts();
}

// 2. Pedidos en curso
async function loadOrders() {
  try {
    const r = await fetch('/api/table-orders', { headers });
    if (!r.ok) {
      const errText = await r.text().catch(() => r.status);
      document.getElementById('orders-list').innerHTML =
        `<div class="empty" style="color:red;">Error ${r.status} al cargar pedidos. ${errText}</div>`;
      return;
    }
    const { orders = [] } = await r.json();

    // Filtramos los pedidos listos en cocina (cada bandeja es un viaje distinto)
    const listos = orders.filter(o => o.status === 'listo');

    // Filtramos las cuentas (todo lo que la mesa ya se comió)
    const cuentasRaw = orders.filter(o => o.status === 'entregado' || o.status === 'factura_generada');

    // 👇 LA MAGIA: Agrupar todas las sub-órdenes por Mesa
    const mesasCuentas = {};
    cuentasRaw.forEach(o => {
        const tId = o.table_id;
        if (!mesasCuentas[tId]) {
            mesasCuentas[tId] = {
                id: o.base_order_id || o.id, // ID base para mandarle la orden a la BD
                table_name: o.table_name,
                status: o.status
            };
        }
        // Si alguna sub-orden ya dice que se generó factura, toda la mesa pasa a ese estado
        if (o.status === 'factura_generada') {
            mesasCuentas[tId].status = 'factura_generada';
        }
    });

    // Convertimos el objeto agrupado nuevamente en un Array
    const cuentasAgrupadas = Object.values(mesasCuentas);

    // Actualizar el numerito rojo de notificaciones
    document.getElementById('badge-pedidos').textContent = listos.length + cuentasAgrupadas.length;
    document.getElementById('badge-pedidos').style.display = (listos.length + cuentasAgrupadas.length) > 0 ? 'inline-block' : 'none';

    let html = '';

    // Dibujar las bandejas por recoger
    listos.forEach(o => {
        html += `
        <div class="order-card" style="border-left: 4px solid #1D9E75;">
            <div class="order-header"><span>${o.table_name}</span><span>LISTO EN COCINA</span></div>
            <div style="margin-bottom:10px; font-size:13px;">Llevar a la mesa los platos solicitados. (Suborden ${o.sub_number || 1})</div>
            <button class="btn btn-green" onclick="updateOrderStatus('${o.id}', 'entregado')">✅ Marcar Entregado al Cliente</button>
        </div>`;
    });

    // Dibujar las cuentas fusionadas
    cuentasAgrupadas.forEach(o => {
        if(o.status === 'factura_generada') {
            html += `
            <div class="order-card" style="border-left: 4px solid #7C3AED;">
                <div class="order-header"><span style="color:#7C3AED;">${o.table_name}</span><span>FACTURA CREADA</span></div>
                <div style="margin-bottom:10px; font-size:13px;">La factura electrónica ya se generó. Cobra en caja.</div>
                <button class="btn" style="background:#555; color:#fff; width:100%;" onclick="updateOrderStatus('${o.id}', 'cerrar_mesa')">👋 Liberar Mesa y Despedir</button>
            </div>`;
        } else {
            html += `
            <div class="order-card" style="border-left: 4px solid #F59E0B;">
                <div class="order-header"><span style="color:#F59E0B;">${o.table_name}</span><span>ESPERANDO CUENTA</span></div>
                <div style="margin-bottom:10px; font-size:13px;">El cliente terminó. Genera su factura o cierra la mesa.</div>
                <div style="display:flex; gap:8px;">
                    <button class="btn btn-purple" style="flex:1;" onclick="updateOrderStatus('${o.id}', 'generar_factura')">🧾 Hacer Factura</button>
                    <button class="btn" style="background:#555; color:#fff; flex:1;" onclick="updateOrderStatus('${o.id}', 'cerrar_mesa')">👋 Solo Liberar</button>
                </div>
            </div>`;
        }
    });

    if(!html) html = '<div class="empty">No hay pedidos listos ni cuentas pendientes.</div>';
    document.getElementById('orders-list').innerHTML = html;
  } catch(e) {
    document.getElementById('orders-list').innerHTML =
      `<div class="empty" style="color:red;">Error inesperado: ${e.message}</div>`;
  }
}

async function updateOrderStatus(id, status) {
  await fetch('/api/table-orders/' + id + '/status', {
    method: 'POST', headers, body: JSON.stringify({ status })
  });
  loadOrders();
  loadTablesStatus(); // Para refrescar los colores de las mesas
}

// 3. Chats
let currentChatPhone = null;
async function loadChats() {
  try {
    const r = await fetch('/api/dashboard/conversations', { headers });
    if (!r.ok) {
      const errText = await r.text().catch(() => r.status);
      document.getElementById('chats-list').innerHTML =
        `<div class="empty" style="color:red;">Error ${r.status} al cargar chats. ${errText}</div>`;
      return;
    }
    const { conversations = [] } = await r.json();
    const cList = document.getElementById('chats-list');

    if(conversations.length === 0) {
        cList.innerHTML = '<div class="empty">Nadie está chateando con el bot ahora.</div>';
        return;
    }

    cList.innerHTML = conversations.map(c => `
      <div class="alert-card" style="cursor:pointer;" onclick="openChat('${escHtml(c.phone)}')">
        <div class="alert-icon">📱</div>
        <div>
          <div class="alert-title">Cliente (${escHtml(c.phone.slice(-4))})</div>
          <div class="alert-meta">${escHtml(c.preview || 'Conversación activa...')}</div>
        </div>
      </div>
    `).join('');
  } catch(e){}
}

async function openChat(phone) {
  currentChatPhone = phone;
  document.getElementById('chat-phone').textContent = `Cel: ${phone}`;
  document.getElementById('chat-modal').classList.add('open');
  try {
    const r = await fetch('/api/conversations/' + encodeURIComponent(phone), { headers });
    const { history = [] } = await r.json();
    const box = document.getElementById('chat-msgs');
    box.innerHTML = history.map(m => `
      <div class="msg ${m.role === 'user' ? 'msg-user' : 'msg-bot'}">${typeof m.content === 'string' ? escHtml(m.content) : 'Mensaje multimedia'}</div>
    `).join('');
    box.scrollTop = box.scrollHeight;
  } catch(e) {}
}

function closeChat() {
  document.getElementById('chat-modal').classList.remove('open');
  currentChatPhone = null;
}

async function sendReply() {
  const i = document.getElementById('chat-input-msg');
  if(!i.value.trim() || !currentChatPhone) return;
  await fetch('/api/conversations/' + encodeURIComponent(currentChatPhone) + '/reply', {
    method: 'POST', headers, body: JSON.stringify({ message: i.value })
  });
  i.value = '';
  openChat(currentChatPhone);
}

async function deleteCurrentChat() {
  if (!currentChatPhone) return;
  if (!confirm('¿Limpiar historial y cancelar el bot para este número?')) return;
  await fetch('/api/conversations/' + encodeURIComponent(currentChatPhone), { method: 'DELETE', headers });
  closeChat();
  loadChats();
  loadTablesStatus();
}

// ── Iniciar los bucles de actualización ──
loadTablesStatus();
loadAlerts();
loadOrders();
loadChats();

// Polling súper optimizado
setInterval(loadTablesStatus, 15000);
setInterval(loadAlerts, 10000);
setInterval(loadOrders, 15000);
setInterval(loadChats, 20000);

// Topbar clock
(function(){
  function _tc(){ const n=new Date(); document.getElementById('header-clock').textContent=String(n.getHours()).padStart(2,'0')+':'+String(n.getMinutes()).padStart(2,'0'); }
  _tc(); setInterval(_tc, 10000);
})();
