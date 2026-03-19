/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Core (stats, pedidos, reservas, chats, charts)
   app/static/dashboard-core.js
═══════════════════════════════════════════════════ */

const token      = localStorage.getItem('rb_token');
const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{"name":"Mi Restaurante"}');
if (!token) window.location.href = '/login';

const headers = { 'Authorization': 'Bearer ' + token };
const fmt = n => '$' + Number(n).toLocaleString('es-CO');

// Exponer para que otros módulos lo usen
window._dashHeaders    = headers;
window._dashRestaurant = restaurant;

document.addEventListener('DOMContentLoaded', () => {
  const nameEl = document.getElementById('sidebar-name');
  if (nameEl) nameEl.textContent = restaurant.name || 'Mi Restaurante';

  const role = restaurant.role || 'owner';
  const equipoNav = document.getElementById('nav-equipo');
  if (equipoNav) equipoNav.style.display = (role === 'owner' || role === 'admin') ? '' : 'none';

  // ── MAGIA DE MÓDULOS SAAS (Ocultar/Mostrar según compra) ──
  const feats = restaurant.features || {};

  const toggleNav = (id, isEnabled) => {
    const el = document.querySelector(`[onclick*="'${id}'"]`);
    if (el) el.style.display = isEnabled ? '' : 'none';
  };

  // Por defecto (si el restaurante es viejo y no tiene features) dejamos todo activado (retrocompatibilidad)
  toggleNav('pedidos', feats.module_orders !== false);
  toggleNav('mesas', feats.module_tables !== false);
  toggleNav('sesiones', feats.module_tables !== false); 
  toggleNav('reservaciones', feats.module_reservations !== false);
  toggleNav('pos', feats.module_pos !== false);
});

// ── TIEMPO ──────────────────────────────────────────────────────────
function updateTime() {
  const el = document.getElementById('current-time');
  if (el) el.textContent = new Date().toLocaleString('es-MX', { weekday:'short', day:'numeric', month:'short', hour:'2-digit', minute:'2-digit' });
}
updateTime();
setInterval(updateTime, 60000);

// ── LOGOUT ──────────────────────────────────────────────────────────
function logout() {
  localStorage.removeItem('rb_token');
  localStorage.removeItem('rb_restaurant');
  window.location.href = '/login';
}

// ── SECCIÓN ACTIVA ──────────────────────────────────────────────────
let currentPeriod = 'today';
const titles = {
  resumen:'Resumen', pedidos:'Pedidos', reservaciones:'Reservaciones',
  conversaciones:'WhatsApp', menu:'Menú', pos:'POS con IA',
  mesas:'Mesas & QR', equipo:'Mi Equipo', sesiones:'Sesiones'
};

function showSection(id, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');

  const titleEl = document.getElementById('page-title');
  if (titleEl) titleEl.textContent = titles[id] || '';
  const mobileTitle = document.getElementById('mobile-page-title');
  if (mobileTitle) mobileTitle.textContent = titles[id] || '';

  const hidePeriod = ['conversaciones', 'menu', 'equipo', 'sesiones'];
  const periodBar = document.getElementById('period-bar');
  if (periodBar) periodBar.style.display = hidePeriod.includes(id) ? 'none' : 'flex';

  if (id === 'pos')        loadPOSData();
  if (id === 'mesas')      loadTables();
  if (id === 'equipo')     loadBranches();
  if (id === 'sesiones')   loadSessions();
  if (id === 'menu')       loadMenu();
}

function setPeriod(period, btn) {
  currentPeriod = period;
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  refreshAll();
}

// ── SIDEBAR MOBILE ───────────────────────────────────────────────────
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('mobile-overlay').classList.toggle('open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('mobile-overlay').classList.remove('open');
}

// ── CHARTS ──────────────────────────────────────────────────────────
let revenueChart = null, statusChart = null, tiposChart = null;

function updateStatusChart(paid, pending) {
  const ctx = document.getElementById('chart-status');
  if (!ctx) return;
  if (statusChart) statusChart.destroy();
  statusChart = new Chart(ctx, {
    type: 'doughnut',
    data: { labels:['Pagados','Pendientes'], datasets:[{ data:[paid||0, pending||0], backgroundColor:['#1D9E75','#FAC775'], borderWidth:0 }] },
    options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, cutout:'65%' }
  });
}

function updateTiposChart(domicilio, recoger) {
  const ctx = document.getElementById('chart-tipos');
  if (!ctx) return;
  if (tiposChart) tiposChart.destroy();
  tiposChart = new Chart(ctx, {
    type: 'doughnut',
    data: { labels:['Domicilio','Recoger'], datasets:[{ data:[domicilio||0, recoger||0], backgroundColor:['#1D9E75','#378ADD'], borderWidth:0 }] },
    options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, cutout:'65%' }
  });
}

// ── STATS ────────────────────────────────────────────────────────────
async function fetchStats() {
  try {
    const r = await fetch(`/api/dashboard/stats?period=${currentPeriod}`, { headers });
    if (r.status === 401) { logout(); return; }
    const d = await r.json();
    const pendingRev = d.orders.pending_revenue || 0;
    document.getElementById('m-revenue').textContent = fmt(d.orders.revenue);
    document.getElementById('m-revenue-sub').innerHTML = d.orders.paid + ' pagados'
      + (pendingRev > 0 ? ' · <span class="delta-warn">' + fmt(pendingRev) + ' pendiente</span>' : '');
    document.getElementById('m-orders').textContent = d.orders.total;
    document.getElementById('m-orders-sub').textContent = d.orders.pending + ' sin pagar';
    document.getElementById('m-res').textContent = d.reservations.total;
    document.getElementById('m-res-sub').textContent = d.reservations.guests + ' personas';
    document.getElementById('m-convs').textContent = d.conversations.active;
    document.getElementById('p-total').textContent = d.orders.total;
    document.getElementById('p-paid').textContent = d.orders.paid;
    document.getElementById('p-pending').textContent = d.orders.pending;
    document.getElementById('r-total').textContent = d.reservations.total;
    document.getElementById('r-guests').textContent = d.reservations.guests;
    updateStatusChart(d.orders.paid, d.orders.pending);
  } catch(e) { console.error('fetchStats:', e); }
}

async function fetchChart() {
  try {
    const r = await fetch(`/api/dashboard/chart?period=${currentPeriod}`, { headers });
    const d = await r.json();
    const periodLabels = { today:'Hoy', week:'Últimos 7 días', month:'Este mes', semester:'Este semestre', year:'Este año' };
    const titleEl = document.getElementById('chart-title');
    if (titleEl) titleEl.textContent = `Ingresos por día — ${periodLabels[currentPeriod]}`;
    if (revenueChart) revenueChart.destroy();
    revenueChart = new Chart(document.getElementById('chart-revenue'), {
      type: 'bar',
      data: {
        labels: d.labels,
        datasets: [
          { label:'Ingresos', data:d.revenue, backgroundColor:'#1D9E75', borderRadius:4, yAxisID:'y' },
          { label:'Pedidos',  data:d.orders,  type:'line', borderColor:'#378ADD', backgroundColor:'transparent', tension:.3, pointRadius:3, yAxisID:'y2' }
        ]
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        plugins:{ legend:{ display:false } },
        scales: {
          y:  { ticks:{ callback: v => '$' + Math.round(v/1000) + 'k', font:{size:11} }, grid:{color:'#f0f0e8'} },
          y2: { position:'right', ticks:{font:{size:11}}, grid:{display:false} },
          x:  { ticks:{font:{size:10}, maxRotation:45}, grid:{display:false} }
        }
      }
    });
  } catch(e) { console.error('fetchChart:', e); }
}

// ── PEDIDOS ──────────────────────────────────────────────────────────
async function fetchOrders() {
  const container = document.getElementById('orders-container');
  try {
    const r = await fetch(`/api/dashboard/orders?period=${currentPeriod}`, { headers });
    if (!r.ok) { container.innerHTML = '<div class="empty-state">Error cargando pedidos.</div>'; return; }
    const d = await r.json();
    if (!d.orders || !d.orders.length) {
      container.innerHTML = '<div class="empty-state">Sin pedidos en este período.</div>';
      updateTiposChart(0, 0); return;
    }
    let html = '<table><thead><tr><th>ID</th><th>Platos</th><th>Tipo</th><th>Estado</th><th>Total</th><th>Hora</th></tr></thead><tbody>';
    d.orders.forEach(o => {
      html += `<tr>
        <td style="font-weight:500;font-size:12px;">${o.id}</td>
        <td style="color:#555;">${o.items || '—'}</td>
        <td><span class="badge ${o.type==='domicilio'?'badge-delivery':'badge-pickup'}">${o.type||'—'}</span></td>
        <td><span class="badge ${o.paid?'badge-paid':'badge-pending'}">${o.paid?'pagado':'pendiente'}</span></td>
        <td style="font-weight:500;">${fmt(o.total)}</td>
        <td style="color:#888;">${o.time||'—'}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
    updateTiposChart(
      d.orders.filter(o => o.type === 'domicilio').length,
      d.orders.filter(o => o.type === 'recoger').length
    );
  } catch(e) { console.error('fetchOrders:', e); }
}

// ── RESERVACIONES ────────────────────────────────────────────────────
async function fetchReservations() {
  try {
    const r = await fetch(`/api/dashboard/reservations?period=${currentPeriod}`, { headers });
    const d = await r.json();
    const container = document.getElementById('res-container');
    if (!d.reservations.length) {
      container.innerHTML = '<div class="empty-state">Sin reservaciones en este período.</div>';
      document.getElementById('r-next').textContent = '—'; return;
    }
    const today = new Date().toISOString().split('T')[0];
    const now   = new Date().toTimeString().slice(0,5);
    const next  = d.reservations.find(res => res.date === today && res.time >= now);
    document.getElementById('r-next').textContent = next ? next.time + ' · ' + next.name.split(' ')[0] : '—';
    let html = '<table><thead><tr><th>Cliente</th><th>Fecha</th><th>Hora</th><th>Personas</th><th>Teléfono</th><th>Notas</th></tr></thead><tbody>';
    d.reservations.forEach(res => {
      html += `<tr>
        <td style="font-weight:500;">${res.name}</td>
        <td style="color:#888;">${res.date}</td>
        <td>${res.time}</td><td>${res.guests}</td>
        <td style="color:#888;">${res.phone||'—'}</td>
        <td style="color:#888;">${res.notes||'—'}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  } catch(e) { console.error('fetchReservations:', e); }
}

// ── CONVERSACIONES ───────────────────────────────────────────────────
async function fetchConversations() {
  try {
    const r = await fetch('/api/dashboard/conversations', { headers });
    const d = await r.json();
    const container = document.getElementById('convs-container');
    if (!d.conversations.length) {
      container.innerHTML = '<div class="empty-state">Sin conversaciones aún.</div>';
      document.getElementById('c-avg').textContent = '0'; return;
    }
    document.getElementById('c-total').textContent = d.conversations.length;
    const avg = Math.round(d.conversations.reduce((s,c) => s + c.messages, 0) / d.conversations.length);
    document.getElementById('c-avg').textContent = avg;
    container.innerHTML = d.conversations.map(c => `
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
  } catch(e) { console.error('fetchConversations:', e); }
}

async function cleanupConversations() {
  if (!confirm('¿Eliminar conversaciones de más de 7 días?')) return;
  try {
    await fetch('/api/conversations/cleanup', { method: 'DELETE', headers });
    fetchConversations();
  } catch(e) {}
}

// ── CHAT MODAL ───────────────────────────────────────────────────────
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
      return `<div class="msg-bubble ${isUser ? 'user' : ''}">
        <div class="bubble ${isUser ? 'user' : 'bot'}">${content}</div>
      </div>`;
    }).join('');
    container.scrollTop = container.scrollHeight;
  } catch(e) { console.error('loadChatHistory:', e); }
}

async function toggleBotPause() {
  if (!currentChatPhone) return;
  botPaused = !botPaused;
  try {
    await fetch('/api/conversations/' + encodeURIComponent(currentChatPhone) + '/pause', {
      method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ paused: botPaused })
    });
    const btn = document.getElementById('chat-pause-btn');
    if (btn) {
      btn.textContent = botPaused ? '▶ Reanudar bot' : '⏸ Pausar bot';
      btn.style.background = botPaused ? '#FDE8E8' : '#fff';
      btn.style.color = botPaused ? '#C0392B' : '#555';
    }
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

// ── REFRESH GLOBAL ───────────────────────────────────────────────────
async function refreshAll() {
  const badge = document.getElementById('sync-badge');
  if (badge) badge.textContent = 'Sincronizando...';
  await Promise.all([fetchStats(), fetchChart(), fetchOrders(), fetchReservations(), fetchConversations()]);
  if (badge) badge.textContent = 'En vivo · ' + new Date().toLocaleTimeString('es-MX', { hour:'2-digit', minute:'2-digit', second:'2-digit' });
}

// ── INIT ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadMenu();
  refreshAll();
  setInterval(refreshAll, 10000);

  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => { if (window.innerWidth <= 768) closeSidebar(); });
  });
});