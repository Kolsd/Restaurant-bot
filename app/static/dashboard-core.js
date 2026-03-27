/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Core (Métricas en Tiempo Real)
   app/static/dashboard-core.js
═══════════════════════════════════════════════════ */

const token = localStorage.getItem('rb_token');
const restaurantStr = localStorage.getItem('rb_restaurant');

// 1. Verificación de Token
if (!token) {
    window.location.href = '/login';
}

const restaurant = JSON.parse(restaurantStr || '{}');
const path = window.location.pathname.toLowerCase();

// 🛡️ 2. GUARDIÁN DE SEGURIDAD (EJECUCIÓN INMEDIATA)
const rawRole = (restaurant.role || '').toLowerCase();
const roles = rawRole.split(',').map(r => r.trim()).filter(Boolean);
const isAdmin = roles.some(r => ['owner', 'admin', 'gerente'].includes(r));

console.log("🔐 Seguridad Mesio - Roles detectados:", roles);

// REGLA DE ORO: Si no eres admin y estás en zona administrativa, ¡FUERA!
if (path.includes('/dashboard') || path.includes('/settings')) {
    if (!isAdmin) {
        alert("Acceso Restringido: No tienes permisos administrativos.");
        if (roles.includes('mesero')) window.location.href = '/mesero';
        else if (roles.includes('cocina')) window.location.href = '/cocina';
        else if (roles.includes('bar')) window.location.href = '/bar';
        else if (roles.includes('caja')) window.location.href = '/caja';
        else window.location.href = '/staff';
    }
}

// 3. Configuración de entorno
const headers = { 'Authorization': 'Bearer ' + token };
const _locale = restaurant.locale || 'es-CO';
const _currency = restaurant.currency || 'COP';

const fmt = (amount) => {
    return new Intl.NumberFormat(_locale, {
        style: 'currency', currency: _currency,
        minimumFractionDigits: ['COP', 'CLP', 'PYG', 'JPY'].includes(_currency) ? 0 : 2
    }).format(Number(amount));
};

window._dashHeaders    = headers;
window._dashRestaurant = restaurant;

function _applyFeatureToggles(feats) {
  const toggleNav = (sectionId, isEnabled) => {
    const byId = document.getElementById(`nav-${sectionId}`);
    if (byId) { byId.style.display = isEnabled ? '' : 'none'; return; }
    const byOnclick = document.querySelector(`[onclick*="'${sectionId}'"]`);
    if (byOnclick) byOnclick.style.display = isEnabled ? '' : 'none';
  };
  // Core modules — opt-out model (hidden only when explicitly false)
  toggleNav('pedidos',       feats.module_orders       !== false);
  toggleNav('reservaciones', feats.module_reservations !== false);
  toggleNav('mesas',         feats.module_tables       !== false);
  // POS tab inside Salón — hide the tab button if module_pos is explicitly false
  const posTab = document.getElementById('nav-mesas-pos');
  if (posTab) posTab.style.display = feats.module_pos !== false ? '' : 'none';
  // Inventario tab inside Menú — hide if module_inventory is explicitly false
  const invTab = document.getElementById('nav-menu-inv');
  if (invTab) invTab.style.display = feats.module_inventory !== false ? '' : 'none';
  // Opt-in modules — visible only when explicitly true
  toggleNav('nps',     feats.module_nps  === true);
  toggleNav('staff',   feats.staff_tips  === true);
  toggleNav('loyalty', feats.loyalty     === true);
}

document.addEventListener('DOMContentLoaded', () => {
  const nameEl = document.getElementById('sidebar-name');
  if (nameEl) nameEl.textContent = restaurant.name || 'Mi Restaurante';

  const roleStr = restaurant.role || 'owner';
  const equipoNav = document.getElementById('nav-equipo');
  if (equipoNav) equipoNav.style.display = (roleStr.includes('owner') || roleStr.includes('admin')) ? '' : 'none';

  // Apply from localStorage immediately (fast, may be stale)
  _applyFeatureToggles(restaurant.features || {});

  // Then fetch fresh features from API and re-apply (keeps localStorage in sync)
  fetch('/api/settings', { headers }).then(r => r.ok ? r.json() : null).then(data => {
    if (!data) return;
    
    // --- INICIO CÓDIGO CORREGIDO ---
    let freshFeats = data.features || {};
    if (typeof freshFeats === 'string') {
        try { freshFeats = JSON.parse(freshFeats); } catch(e) { freshFeats = {}; }
    }

    // Update localStorage so next page load is also correct
    const stored = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    stored.features = freshFeats;
    stored.locale   = data.locale   || freshFeats.locale   || stored.locale;
    stored.currency = data.currency || freshFeats.currency || stored.currency;
    localStorage.setItem('rb_restaurant', JSON.stringify(stored));
    window._dashRestaurant = stored;
    _applyFeatureToggles(freshFeats);
  }).catch(() => {});

  loadMenu();
  refreshAll();
  setInterval(refreshAll, 30000);
});

function updateTime() {
  const el = document.getElementById('current-time');
  if (el) el.textContent = new Date().toLocaleString(_locale, { weekday:'short', day:'numeric', month:'short', hour:'2-digit', minute:'2-digit' });
}
updateTime(); setInterval(updateTime, 60000);

function logout() {
  localStorage.clear(); window.location.href = '/login';
}

let currentPeriod = 'today';
window.customStart = '';
window.customEnd = '';
const titles = { resumen:'Resumen', pedidos:'Pedidos', reservaciones:'Reservaciones', conversaciones:'WhatsApp', menu:'Menú', mesas:'Salón', equipo:'Mi Equipo', nps:'NPS', staff:'Staff & Propinas', loyalty:'Fidelización' };

function setPeriod(p, btn) {
  currentPeriod = p;
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  refreshAll();
}

function showSection(id, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');

  const titleEl = document.getElementById('page-title');
  if (titleEl) titleEl.textContent = titles[id] || '';

  const hidePeriod = ['conversaciones', 'menu', 'equipo', 'mesas', 'nps', 'staff', 'loyalty'];
  const periodBar = document.getElementById('period-bar');
  
  if (periodBar) {
    if (hidePeriod.includes(id)) {
        periodBar.style.display = 'none';
    } else if (id === 'pedidos') {
        const rtActive = document.getElementById('tab-rt')?.classList.contains('active');
        periodBar.style.display = rtActive ? 'none' : 'flex';
    } else {
        periodBar.style.display = 'flex';
    }
  }

  // Salón: always load tables; load POS only if that tab is active
  if (id === 'mesas') {
    loadTables();
    const posPanel = document.getElementById('mesas-panel-pos');
    if (posPanel && posPanel.style.display !== 'none') loadPOSData();
  }
  if (id === 'equipo')   loadBranches();
  if (id === 'menu')     loadMenu();
  if (id === 'staff'   && typeof loadStaffSection   === 'function') loadStaffSection();
  if (id === 'loyalty' && typeof loadLoyaltySection === 'function') loadLoyaltySection();
  if (window.innerWidth <= 768) closeSidebar();
}

// ── Tab switchers para secciones unificadas ──────────────────────────────────

function switchConvsTab(id, btn) {
  document.querySelectorAll('#conversaciones .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('convs-panel-chat').style.display = id === 'chat' ? '' : 'none';
  document.getElementById('convs-panel-ses').style.display  = id === 'ses'  ? '' : 'none';
  if (id === 'ses') loadSessions();
}

function switchMenuTab(id, btn) {
  document.querySelectorAll('#menu .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('menu-panel-disp').style.display = id === 'disp' ? '' : 'none';
  document.getElementById('menu-panel-inv').style.display  = id === 'inv'  ? '' : 'none';
  if (id === 'inv') { loadInventory(); if (typeof loadFoodCosts === 'function') loadFoodCosts(); }
}

function switchMesasTab(id, btn) {
  document.querySelectorAll('#mesas .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('mesas-panel-mesas').style.display = id === 'mesas' ? '' : 'none';
  document.getElementById('mesas-panel-pos').style.display   = id === 'pos'   ? '' : 'none';
  if (id === 'pos') loadPOSData();
}

function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); document.getElementById('mobile-overlay').classList.toggle('open'); }
function closeSidebar() { document.getElementById('sidebar').classList.remove('open'); document.getElementById('mobile-overlay').classList.remove('open'); }

// ── CHARTS ──
let revenueChart = null, statusChart = null, tiposChart = null;

function updateStatusChart(paid, pending) {
  const ctx = document.getElementById('chart-status');
  if (!ctx) return;
  if (statusChart) statusChart.destroy();
  statusChart = new Chart(ctx, {
    type: 'doughnut', data: { labels:['Pagados','Pendientes'], datasets:[{ data:[paid||0, pending||0], backgroundColor:['#1D9E75','#FAC775'], borderWidth:0 }] },
    options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, cutout:'65%' }
  });
}

function updateTiposChart(domicilio, recoger) {
  const ctx = document.getElementById('chart-tipos');
  if (!ctx) return;
  if (tiposChart) tiposChart.destroy();
  tiposChart = new Chart(ctx, {
    type: 'doughnut', data: { labels:['Domicilio','Recoger'], datasets:[{ data:[domicilio||0, recoger||0], backgroundColor:['#1D9E75','#378ADD'], borderWidth:0 }] },
    options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, cutout:'65%' }
  });
}

function renderChart(orders) {
  const periodLabels = { today:'Hoy', week:'Últimos 7 días', month:'Este mes', semester:'Este semestre', year:'Este año' };
  const titleEl = document.getElementById('chart-title');
  if (titleEl) titleEl.textContent = `Ingresos por día — ${periodLabels[currentPeriod]}`;
  
  const dataMap = {};
  orders.forEach(o => {
      if(o.paid) {
          const isoString = o.created_at.endsWith('Z') ? o.created_at : o.created_at + 'Z';
          const d = new Date(isoString);
          const year = d.getFullYear();
          const month = String(d.getMonth() + 1).padStart(2, '0');
          const day = String(d.getDate()).padStart(2, '0');
          const localDate = `${year}-${month}-${day}`;
          if(!dataMap[localDate]) dataMap[localDate] = { rev: 0, count: 0 };
          dataMap[localDate].rev += o.total;
          dataMap[localDate].count += 1;
      }
  });

  const labels = Object.keys(dataMap).sort();
  const revData = labels.map(l => dataMap[l].rev);
  const countData = labels.map(l => dataMap[l].count);

  if (revenueChart) revenueChart.destroy();
  revenueChart = new Chart(document.getElementById('chart-revenue'), {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        { label:'Pedidos',  data:countData, type:'line', borderColor:'#378ADD', backgroundColor:'transparent', tension:.3, pointRadius:3, yAxisID:'y2', order:1 },
        { label:'Ingresos', data:revData, backgroundColor:'#1D9E75', borderRadius:4, yAxisID:'y', order:2 }
      ]
    },
    options: {
      responsive:true, maintainAspectRatio:false, plugins:{ legend:{ display:false } },
      scales: {
        y:  { 
          ticks:{ callback: v => '$' + Math.round(v/1000) + 'k', font:{size:11} }, 
          grid:{color:'#f0f0e8'},
          beginAtZero: true
        },
        y2: { 
          position:'right', 
          ticks:{font:{size:11}}, 
          grid:{display:false},
          beginAtZero: true,
          suggestedMax: Math.max(...countData) * 1.5  // ← da espacio arriba para que la línea quede sobre las barras
        },
        x:  { ticks:{font:{size:10}, maxRotation:45}, grid:{display:false} }
      }
    }
  });
}

async function refreshAll() {
  const badge = document.getElementById('sync-badge');
  if (badge) badge.textContent = 'Sincronizando...';

  try {
    const localOffset = new Date().getTimezoneOffset();
    
    let urlParams = `period=${currentPeriod}&tz_offset=${localOffset}`;
    if (currentPeriod === 'custom') {
        urlParams += `&custom_start=${window.customStart}&custom_end=${window.customEnd}`;
    }

    const rOrders = await fetch(`/api/dashboard/orders?${urlParams}`, { headers });
    if (rOrders.status === 401) { logout(); return; }
    const orders = (await rOrders.json()).orders || [];
    
    const rRes = await fetch(`/api/dashboard/reservations?${urlParams}`, { headers });
    const reservations = rRes.ok ? ((await rRes.json()).reservations || []) : [];

    const rChats = await fetch(`/api/dashboard/conversations`, { headers });
    const conversations = rChats.ok ? ((await rChats.json()).conversations || []) : [];

    // ── Ingresos: total de todos los pedidos no cancelados ──
    const cancelledStatuses = ['cancelado', 'cancelled'];
    const activeOrders  = orders.filter(o => !cancelledStatuses.includes(String(o.status || '')));
    const paidOrders    = orders.filter(o => o.paid);
    const pendingOrders = activeOrders.filter(o => !o.paid);
    const totalRev      = activeOrders.reduce((s,o) => s + (Number(o.total) || 0), 0);
    const pendingRev    = pendingOrders.reduce((s,o) => s + (Number(o.total) || 0), 0);

    // ── Métricas del resumen ──
    const mRevenue = document.getElementById('m-revenue');
    if (mRevenue) mRevenue.textContent = fmt(totalRev);

    const mRevenueSub = document.getElementById('m-revenue-sub');
    if (mRevenueSub) mRevenueSub.innerHTML = paidOrders.length + ' confirmados' + (pendingRev > 0 ? ' · <span class="delta-warn">' + fmt(pendingRev) + ' pendiente</span>' : '');

    const mOrders = document.getElementById('m-orders');
    if (mOrders) mOrders.textContent = orders.length;

    const mOrdersSub = document.getElementById('m-orders-sub');
    if (mOrdersSub) mOrdersSub.textContent = pendingOrders.length + ' sin pagar';

    const mRes = document.getElementById('m-res');
    if (mRes) mRes.textContent = reservations.length;

    const mResSub = document.getElementById('m-res-sub');
    if (mResSub) mResSub.textContent = reservations.reduce((s,r) => s + (r.guests||0), 0) + ' personas';

    // ── FIX: el HTML usa m-convs, no c-total ──
    const mConvs = document.getElementById('m-convs');
    if (mConvs) mConvs.textContent = conversations.length;

    // ── MÉTRICAS DE DOMICILIOS EN TIEMPO REAL ──
    const extOrders = orders.filter(o => o.type !== 'mesa');
    let domCocina = 0, domEntrega = 0, domEntregados = 0;
    const activeExt = [];
    
    extOrders.forEach(o => {
       const st = (o.status || '').toLowerCase();
       if (st.includes('entregado') || st.includes('cancelado')) {
           if (st.includes('entregado')) domEntregados++;
       } else {
           if (st.includes('camino') || st.includes('entrega')) domEntrega++;
           else domCocina++;
           activeExt.push(o);
       }
    });

    const rtDomTotal     = document.getElementById('rt-dom-total');
    const rtDomCocina    = document.getElementById('rt-dom-cocina');
    const rtDomEntrega   = document.getElementById('rt-dom-entrega');
    const rtDomEntregados = document.getElementById('rt-dom-entregados');
    if (rtDomTotal)      rtDomTotal.textContent      = extOrders.length;
    if (rtDomCocina)     rtDomCocina.textContent     = domCocina;
    if (rtDomEntrega)    rtDomEntrega.textContent    = domEntrega;
    if (rtDomEntregados) rtDomEntregados.textContent = domEntregados;
    
    // ── Tabla de domicilios activos ──
    const domContainer = document.getElementById('rt-domicilios-container');
    if (domContainer) {
       if (activeExt.length === 0) {
           domContainer.innerHTML = '<div class="empty-state">No hay domicilios con pedidos activos en este momento.</div>';
       } else {
           let domHtml = '<div style="font-size: 13px; font-weight: bold; margin-bottom: 10px;">🕒 ACTIVOS EN PREPARACIÓN / ENTREGA</div>';
           domHtml += '<table><thead><tr><th>ID</th><th>Platos</th><th>Estado</th><th>Total</th></tr></thead><tbody>';
           activeExt.forEach(o => {
               let itemsStr = '—';
               try {
                   const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items;
                   itemsStr = Array.isArray(arr) ? arr.map(i => `${i.quantity||1}x ${i.name}`).join(', ') : String(o.items);
               } catch(e) { itemsStr = String(o.items); }
               const stFormat = (o.status || 'pendiente').replace(/_/g, ' ').toUpperCase();
               domHtml += `<tr>
                 <td style="font-weight:500;font-size:12px;">${o.id.substring(0,8)}</td>
                 <td style="color:#555;font-size:12px;">${itemsStr}</td>
                 <td><span class="badge" style="background:#E6F1FB;color:#185FA5;">${stFormat}</span></td>
                 <td style="font-weight:700;">${fmt(o.total)}</td>
               </tr>`;
           });
           domHtml += '</tbody></table>';
           domContainer.innerHTML = domHtml;
       }
    }
    
    updateStatusChart(paidOrders.length, pendingOrders.length);
    renderChart(orders);
    renderOrders(orders);
    renderReservations(reservations);
    renderConversations(conversations);
    
    if(typeof loadTableOrdersSection === 'function') loadTableOrdersSection();

  } catch(e) { console.error('Sync Error:', e); }

  if (badge) badge.textContent = 'En vivo · ' + new Date().toLocaleTimeString(navigator.language || 'default', { hour:'2-digit', minute:'2-digit', second:'2-digit' });
}

function renderOrders(orders) {
  const container = document.getElementById('orders-container');
  if (!container) return;
  if (!orders || !orders.length) {
    container.innerHTML = '<div class="empty-state">Sin pedidos en este período.</div>';
    updateTiposChart(0, 0); return;
  }
  
  let html = '<table><thead><tr><th>ID</th><th>Platos</th><th>Origen</th><th>Estado</th><th>Total</th><th>Fecha</th><th>Hora</th></tr></thead><tbody>';
  orders.forEach(o => {
    const isoStr = o.created_at.endsWith('Z') ? o.created_at : o.created_at + 'Z';
    const dateObj = new Date(isoStr);
    const localTime = dateObj.toLocaleTimeString(_locale, {hour: '2-digit', minute: '2-digit'});
    const localDate = dateObj.toLocaleDateString(_locale, {day: '2-digit', month: 'short', year: 'numeric'});

    let itemsStr = '—';
    try {
        const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items;
        itemsStr = Array.isArray(arr) ? arr.map(i => `${i.quantity||1}x ${i.name}`).join(', ') : String(o.items);
    } catch(e) { itemsStr = String(o.items); }

    const origenBadge = o.type === 'mesa'
      ? '<span class="badge" style="background:#E1F5EE;color:#0F6E56;">🪑 Salón</span>'
      : `<span class="badge ${o.type==='domicilio'?'badge-delivery':'badge-pickup'}">🛵 ${o.type}</span>`;
    const stFormat = (o.status || 'pendiente').replace(/_/g, ' ').toUpperCase();

    html += `<tr>
      <td style="font-weight:500;font-size:12px;">${o.id.substring(0,8)}</td>
      <td style="color:#555;font-size:12px;max-width:300px;">${itemsStr}</td>
      <td>${origenBadge}</td>
      <td><span class="badge" style="background:#f0f0e8;color:#555;">${stFormat}</span></td>
      <td style="font-weight:700;">${fmt(o.total)}</td>
      <td style="color:#888;">${localDate}</td>
      <td style="color:#888;">${localTime}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  container.innerHTML = html;
  
  updateTiposChart(orders.filter(o => o.type === 'domicilio').length, orders.filter(o => o.type === 'recoger').length);
}

function renderReservations(reservations) {
  const container = document.getElementById('res-container');
  if (!container) return;

  const rTotal  = document.getElementById('r-total');
  const rGuests = document.getElementById('r-guests');
  const rNext   = document.getElementById('r-next');

  if (rTotal)  rTotal.textContent  = reservations.length;
  if (rGuests) rGuests.textContent = reservations.reduce((s,r) => s + (r.guests||0), 0);

  const upcoming = reservations.filter(r => r.date >= new Date().toISOString().slice(0,10));
  if (rNext) rNext.textContent = upcoming.length ? `${upcoming[0].time} · ${upcoming[0].name}` : '—';

  if (!reservations.length) {
    container.innerHTML = '<div class="empty-state">Sin reservaciones en este período.</div>';
    return;
  }

  let html = '<table><thead><tr><th>Nombre</th><th>Fecha</th><th>Hora</th><th>Personas</th><th>Teléfono</th><th>Notas</th></tr></thead><tbody>';
  reservations.forEach(r => {
    html += `<tr>
      <td style="font-weight:500;">${r.name}</td>
      <td>${r.date}</td>
      <td>${r.time}</td>
      <td>${r.guests}</td>
      <td style="color:#888;font-size:12px;">${r.phone}</td>
      <td style="color:#888;font-size:12px;">${r.notes || '—'}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  container.innerHTML = html;
}

function renderConversations(conversations) {
  const container = document.getElementById('convs-container');
  if (!container) return;

  // ── FIX: actualizar c-avg (existe en HTML) pero no c-total (no existe) ──
  const cAvg = document.getElementById('c-avg');

  if (!conversations.length) {
    container.innerHTML = '<div class="empty-state">Sin conversaciones activas.</div>';
    if (cAvg) cAvg.textContent = '0';
    return;
  }
  const avg = Math.round(conversations.reduce((s,c) => s + c.messages, 0) / conversations.length);
  if (cAvg) cAvg.textContent = avg;

  container.innerHTML = conversations.map(c => `
    <div class="conv-row" onclick="openChat('${c.phone}')" style="cursor:pointer;transition:background .15s;" onmouseover="this.style.background='#f5f5f0'" onmouseout="this.style.background=''">
      <div class="conv-avatar">${c.phone.slice(-4)}</div>
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:500;">${c.phone}</div>
        <div style="font-size:12px;color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:500px;">${c.preview}</div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:11px;color:#aaa;white-space:nowrap;">${c.messages} mensajes</div>
        <div style="font-size:10px;padding:3px 8px;background:#E1F5EE;color:#0F6E56;border-radius:6px;">Ver chat →</div>
      </div>
    </div>`).join('');
}

async function cleanupConversations() {
  if (!confirm('¿Eliminar conversaciones de más de 7 días? Esto limpiará la memoria de la IA para esos números.')) return;
  try {
    await fetch('/api/conversations/cleanup', { method: 'DELETE', headers });
    refreshAll();
  } catch(e) {}
}

// ── CHAT MODAL ──
let currentChatPhone = null;
let botPaused = false;

async function openChat(phone) {
  currentChatPhone = phone;
  document.getElementById('chat-modal-phone').textContent = phone;
  document.getElementById('chat-modal-msgs').innerHTML = '<div style="text-align:center;color:#888;font-size:12px;padding:1rem;">Cargando...</div>';
  document.getElementById('chat-modal-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  await loadChatHistory(phone);
}

function closeChatModal() {
  document.getElementById('chat-modal-overlay').classList.remove('open');
  document.body.style.overflow = '';
  currentChatPhone = null;
}

async function loadChatHistory(phone) {
  try {
    const r = await fetch('/api/conversations/' + encodeURIComponent(phone), { headers });
    if (!r.ok) return;
    const d = await r.json();
    const msgs = d.history || [];
    botPaused = d.bot_paused || false;
    const btn = document.getElementById('chat-pause-btn');
    if (btn) {
      btn.textContent = botPaused ? '▶ Reanudar bot' : '⏸ Pausar bot';
      btn.style.background = botPaused ? '#FDE8E8' : '#fff';
      btn.style.color = botPaused ? '#C0392B' : '#555';
    }
    const container = document.getElementById('chat-modal-msgs');
    if (!msgs.length) {
      container.innerHTML = '<div style="text-align:center;color:#888;font-size:12px;padding:1rem;">Sin mensajes.</div>';
      return;
    }
    container.innerHTML = msgs.map(m => {
      const isUser = m.role === 'user';
      const content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
      return `<div class="msg-bubble ${isUser ? 'user' : ''}"><div class="bubble ${isUser ? 'user' : 'bot'}">${content}</div></div>`;
    }).join('');
    container.scrollTop = container.scrollHeight;
  } catch(e) { console.error('loadChatHistory:', e); }
}

async function forceCloseChat() {
  if (!currentChatPhone) return;
  if (!confirm(`¿Limpiar y cerrar el chat de ${currentChatPhone}? Esto elimina la conversación activa.`)) return;
  try {
    await fetch('/api/conversations/' + encodeURIComponent(currentChatPhone), {
      method: 'DELETE', headers
    });
    closeChatModal();
    refreshAll();
  } catch(e) { console.error('forceCloseChat:', e); }
}

async function toggleBotPause() {
  if (!currentChatPhone) return;
  botPaused = !botPaused;
  try {
    await fetch('/api/conversations/' + encodeURIComponent(currentChatPhone) + '/pause', {
      method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ paused: botPaused })
    });
    loadChatHistory(currentChatPhone);
  } catch(e) {}
}

async function sendManualReply() {
  const input = document.getElementById('chat-reply-input');
  const msg   = (input.value || '').trim();
  if (!msg || !currentChatPhone) return;
  input.value = '';
  try {
    await fetch('/api/conversations/' + encodeURIComponent(currentChatPhone) + '/reply', {
      method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg })
    });
    await loadChatHistory(currentChatPhone);
  } catch(e) {}
}

function switchOrderTab(tab, btn) {
  const rtDiv   = document.getElementById('orders-tab-rt');
  const histDiv = document.getElementById('orders-tab-hist');
  const periodBar = document.getElementById('period-bar');
  
  if (!rtDiv || !histDiv) return;

  document.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  if (tab === 'rt') {
    rtDiv.style.display   = 'block';
    histDiv.style.display = 'none';
    if (periodBar) periodBar.style.display = 'none';
    if(typeof loadTableOrdersSection === 'function') loadTableOrdersSection();
  } else {
    rtDiv.style.display   = 'none';
    histDiv.style.display = 'block';
    if (periodBar) periodBar.style.display = 'flex';
    if(typeof refreshAll === 'function') refreshAll();
  }
}

function setCustomPeriod(btn) {
  const start = document.getElementById('custom-start').value;
  const end = document.getElementById('custom-end').value;
  if(!start || !end) return alert("Por favor selecciona una fecha de inicio y una fecha de fin.");
  
  currentPeriod = 'custom';
  window.customStart = start;
  window.customEnd = end;
  
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  refreshAll();
}