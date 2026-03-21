/* ═══════════════════════════════════════════════════════════════════
   Mesio CRM — Javascript
   app/static/crm.js
═══════════════════════════════════════════════════════════════════ */

const ADMIN_KEY = localStorage.getItem('mesio_admin_key');
if (!ADMIN_KEY) window.location.href = '/superadmin?redirect=crm';

const H = { 'Authorization': 'Bearer ' + ADMIN_KEY, 'Content-Type': 'application/json' };
const STAGES = ['prospecto','contactado','respondio','demo','negociacion','cerrado','perdido'];
const STAGE_LABELS = { prospecto:'Prospecto', contactado:'Contactado', respondio:'Respondió', demo:'En Demo', negociacion:'Negociación', cerrado:'Cerrado ✅', perdido:'Perdido' };
const NOTE_ICONS = { note:'📝', call:'📞', whatsapp:'💬', email:'📧', meeting:'🤝' };

let prospects = [], selectedIds = new Set(), currentPid = null;
let currentView = 'inbox', activeFilter = '', searchQuery = '', templates = [];
let _toastTimer = null, currentInboxPid = null;

document.addEventListener('DOMContentLoaded', () => {
  loadStats(); loadProspects(); loadTemplates();
  setView('inbox', document.getElementById('btn-inbox'));
});

// ── UI HELPERS ──
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = (type==='ok' ? '✅ ' : type==='err' ? '❌ ' : 'ℹ️ ') + msg;
  el.className = `toast show ${type}`;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 4000);
}

function toggleSidebar() {
  document.querySelector('.sidebar').classList.toggle('open');
  document.getElementById('mobile-overlay').classList.toggle('open');
}
function closeSidebar() {
  document.querySelector('.sidebar').classList.remove('open');
  document.getElementById('mobile-overlay').classList.remove('open');
}

// ── VISTAS ──
function setView(v, btn) {
  // Asegurarnos de limpiar la vista móvil del chat si cambiamos de vista
  document.body.classList.remove('mobile-chat-open');

  currentView = v;
  document.getElementById('view-kanban').style.display = v === 'kanban' ? 'block' : 'none';
  document.getElementById('view-tabla').style.display  = v === 'tabla'  ? 'block' : 'none';
  
  const inboxView = document.getElementById('view-inbox');
  if (inboxView) {
    inboxView.style.display = v === 'inbox' ? 'flex' : 'none';
    if (v === 'inbox') renderInbox();
  }

  document.querySelectorAll('.vt').forEach(b => b.classList.remove('active'));
  const topBtn = document.getElementById('vt-'+v);
  if(topBtn) topBtn.classList.add('active');

  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  
  const titles = { 'kanban': 'Pipeline', 'tabla': 'Tabla Base', 'inbox': 'Inbox' };
  document.getElementById('page-title').textContent = titles[v] || 'CRM';
  
  if (v !== 'inbox') renderView();
  if (window.innerWidth <= 800) closeSidebar();
}

function filterStage(stage) { activeFilter = stage; loadProspects(); }
function onSearch() { searchQuery = document.getElementById('search-input').value.trim(); loadProspects(); }
function renderView() { if (currentView === 'kanban') renderKanban(); else if (currentView === 'tabla') renderTable(); }

// ── API CALLS ──
async function api(method, path, body=null) {
  const opts = { method, headers: H };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch('/api/crm' + path, opts);
  if (r.status === 401) { window.location.href = '/login'; return null; }
  return r.json();
}

async function loadStats() {
  const d = await api('GET', '/stats');
  if (!d) return;
  document.getElementById('s-total').textContent = d.total;
  document.getElementById('s-contacted').textContent = d.contacted;
  document.getElementById('s-responded').textContent = d.stage_counts?.respondio || 0;
  document.getElementById('s-converted').textContent = d.converted;
  document.getElementById('s-rate').textContent = d.conversion_rate + '%';
}

async function loadProspects() {
  const stage = activeFilter || '';
  let url = `/prospects?archived=false`;
  if (stage) url += `&stage=${stage}`;
  if (searchQuery) url += `&search=${encodeURIComponent(searchQuery)}`;
  const d = await api('GET', url);
  if (!d) return;
  prospects = d.prospects || [];
  renderView();
}

async function loadTemplates() { const d = await api('GET', '/templates'); if (d) templates = d.templates || []; }

// ── KANBAN Y TABLA ──
function renderKanban() {
  const stages = activeFilter ? [activeFilter] : STAGES;
  const board  = document.getElementById('kanban-board');
  board.innerHTML = '';
  stages.forEach(stage => {
    const cols = prospects.filter(p => p.stage === stage);
    const col = document.createElement('div');
    col.className = 'k-col';
    col.innerHTML = `<div class="k-header"><div class="k-dot ${stage}"></div><span>${STAGE_LABELS[stage]}</span><span class="k-cnt">${cols.length}</span></div><div class="k-body" id="kcol-${stage}"></div>`;
    board.appendChild(col);
    const body = col.querySelector('.k-body');
    cols.forEach(p => {
      const el = document.createElement('div');
      el.className = 'k-card';
      el.innerHTML = `<div class="k-card-name">${p.restaurant_name}</div><div class="k-card-owner">${p.phone}</div>`;
      el.onclick = () => openPanel(p.id, 'info');
      body.appendChild(el);
    });
  });
}

function renderTable() {
  const tbody = document.getElementById('prospects-tbody');
  tbody.innerHTML = prospects.map(p => `
    <tr onclick="openPanel(${p.id},'info')">
      <td><input type="checkbox"></td>
      <td>${p.restaurant_name}</td>
      <td>${p.phone}</td>
      <td><span class="stage-badge">${p.stage}</span></td>
      <td><button class="btn btn-outline btn-xs" onclick="event.stopPropagation(); openPanel(${p.id},'chat')">💬</button></td>
    </tr>`).join('');
}

// ── PANEL LATERAL ──
function openPanel(pid, tab='info') {
  currentPid = pid;
  const p = prospects.find(x => x.id === pid);
  if (!p) return;
  document.getElementById('dp-name').textContent = p.restaurant_name;
  document.getElementById('dp-stage').innerHTML = STAGES.map(s => `<option value="${s}" ${s===p.stage?'selected':''}>${STAGE_LABELS[s]}</option>`).join('');
  document.getElementById('detail-panel').classList.add('open');
  dpTab(tab);
}
function closePanel() { currentPid = null; document.getElementById('detail-panel').classList.remove('open'); }

function dpTab(tab, btn=null) {
  ['info','chat','notas'].forEach(t => { const el = document.getElementById('dp-'+t); if(el) el.style.display = t===tab ? (t==='chat'?'flex':'block') : 'none'; });
  document.querySelectorAll('.dp-tab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (tab === 'chat' && currentPid) loadPanelInteractions(currentPid);
  if (tab === 'notas' && currentPid) loadNotes(currentPid);
}

async function updateStage() {
  const stage = document.getElementById('dp-stage').value;
  await api('PATCH', `/prospects/${currentPid}/stage`, { stage });
  loadProspects(); loadStats(); toast('Etapa actualizada');
}
async function archiveProspect() {
  if (!confirm('¿Archivar este prospecto?')) return;
  await api('PATCH', `/prospects/${currentPid}`, { archived: true });
  closePanel(); loadProspects(); toast('Archivado');
}

// ── CHAT PANEL ──
async function loadPanelInteractions(pid) {
  const d = await api('GET', `/prospects/${pid}/interactions`);
  const c = document.getElementById('chat-msgs');
  c.innerHTML = (d?.interactions||[]).map(i => `<div class="msg-row ${i.direction==='outbound'?'out':''}"><div class="msg-bubble ${i.direction==='outbound'?'msg-out':'msg-in'}">${i.content}</div></div>`).join('');
}
function chatKeydown(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }
async function sendMessage() {
  const i = document.getElementById('chat-input'); const m = i.value.trim(); if (!m) return;
  i.value = '';
  await api('POST', '/send-message', { prospect_id: currentPid, message: m });
  loadPanelInteractions(currentPid);
}

// ── INBOX (FULL SCREEN) ──
function renderInbox() {
  const listEl = document.getElementById('inbox-list');
  const query = (document.getElementById('inbox-search')?.value || '').toLowerCase();
  let items = prospects.filter(p => p.stage !== 'cerrado' && p.stage !== 'perdido' && (p.restaurant_name.toLowerCase().includes(query) || p.phone.includes(query)));
  items.sort((a, b) => new Date(b.last_contact_at || 0) - new Date(a.last_contact_at || 0));
  
  if (!items.length) { listEl.innerHTML = '<div style="padding:1rem;color:gray;">No hay chats</div>'; return; }
  
  listEl.innerHTML = items.map(p => `
    <div onclick="openInboxChat(${p.id})" style="padding: 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; background: ${p.id === currentInboxPid ? 'var(--bg3)' : 'transparent'}; border-left: 3px solid ${p.id === currentInboxPid ? 'var(--g)' : 'transparent'};">
      <div style="font-weight: 700; color: var(--text);">${p.restaurant_name}</div>
      <div style="font-size: 0.75rem; color: var(--muted2);">${p.phone}</div>
    </div>`).join('');
}
function filterInbox() { renderInbox(); }

async function openInboxChat(pid) {
  currentInboxPid = pid;
  renderInbox();
  const p = prospects.find(x => x.id === pid);
  if (!p) return;

  document.getElementById('inbox-rest-name').textContent = p.restaurant_name;
  document.getElementById('inbox-rest-phone').textContent = p.phone;
  document.getElementById('inbox-actions').style.display = 'flex';
  document.getElementById('inbox-input-area').style.display = 'block';

  const d = await api('GET', `/prospects/${pid}/interactions`);
  const c = document.getElementById('inbox-messages');
  c.innerHTML = (d?.interactions||[]).map(i => {
    const out = i.direction === 'outbound';
    return `<div style="display:flex; flex-direction:column; align-items:${out?'flex-end':'flex-start'}; margin-bottom:5px;">
      <div style="padding:10px 14px; border-radius:12px; background:${out?'#005C4B':'var(--card)'}; color:${out?'#fff':'var(--text)'}; max-width:85%;">${i.content}</div>
    </div>`;
  }).join('');
  c.scrollTop = c.scrollHeight;
  
  // 🚀 MAGIA: Agrega la clase al BODY para ocultar todo lo demas en móviles
  document.body.classList.add('mobile-chat-open');
}

function cerrarChatMovil() {
  // 🚀 MAGIA: Remueve la clase del BODY para restaurar la vista original
  document.body.classList.remove('mobile-chat-open');
  currentInboxPid = null; 
  renderInbox();
}

async function sendInboxMessage() {
  const i = document.getElementById('inbox-input'); const m = i.value.trim(); if (!m) return;
  i.value = ''; i.disabled = true;
  await api('POST', '/send-message', { prospect_id: currentInboxPid, message: m });
  i.disabled = false; i.focus();
  openInboxChat(currentInboxPid); loadProspects();
}

// ── AUTO-REFRESH (POLLING) MEJORADO ──
setInterval(async () => {
    if (currentView === 'inbox') {
       
       // 1. Recargar la lista de prospectos en background para que el chat salte arriba si hay nuevos mensajes
       const stage = activeFilter || '';
       let url = `/prospects?archived=false`;
       if (stage) url += `&stage=${stage}`;
       if (searchQuery) url += `&search=${encodeURIComponent(searchQuery)}`;
       
       const dp = await api('GET', url);
       if (dp && dp.prospects) {
           // Solo actualiza si hubo un cambio real en el último contacto para no interrumpir el scroll
           const latestCurrent = prospects[0]?.last_contact_at;
           const latestNew = dp.prospects[0]?.last_contact_at;
           
           prospects = dp.prospects;
           if (latestCurrent !== latestNew) {
               renderInbox(); 
           }
       }
  
       // 2. Si estás dentro de un chat, revisar si tiene mensajes nuevos para pintarlos
       if (currentInboxPid) {
           const d = await api('GET', `/prospects/${currentInboxPid}/interactions`);
           if (d && d.interactions && d.interactions.length > document.getElementById('inbox-messages').childElementCount) {
              document.getElementById('notifSound')?.play().catch(()=>{});
              openInboxChat(currentInboxPid); 
           }
       }
    }
  }, 4000);