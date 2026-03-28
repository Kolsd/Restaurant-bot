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
let globalLastUpdate = null; 

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
    <tr onclick="openPanel(${p.id},'info')" style="cursor:pointer;">
      <td onclick="event.stopPropagation()">
         <input type="checkbox" class="row-check" value="${p.id}" ${selectedIds.has(p.id) ? 'checked' : ''} onchange="toggleCheck(${p.id})">
      </td>
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
  
  document.body.classList.add('mobile-chat-open');
}

function cerrarChatMovil() {
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

// ── AUTO-REFRESH (POLLING OPTIMIZADO) ──
setInterval(async () => {
    if (currentView === 'inbox') {
       const res = await api('GET', '/check-updates');
       if (res && res.latest) {
           if (!globalLastUpdate) {
               globalLastUpdate = res.latest;
           } 
           else if (res.latest !== globalLastUpdate) {
               globalLastUpdate = res.latest;
               await loadProspects(); 
               if (currentInboxPid) {
                   document.getElementById('notifSound')?.play().catch(()=>{});
                   openInboxChat(currentInboxPid);
               }
           }
       }
    }
}, 4000); 
// FIJATE EN ESTA LINEA DE ARRIBA: En tu archivo original habia un `}` extra aqui. Ya lo borré.

// ════════════════════════════════════════════════════════════
// ── FUNCIONES FALTANTES (MODALES, TEMPLATES, NOTAS Y CSV) ──
// ════════════════════════════════════════════════════════════

// ── MODALES BÁSICOS ──
function openAddModal() { document.getElementById('modal-add').style.display = 'flex'; }
function closeModal(id) { document.getElementById(id).style.display = 'none'; }

// ── PROSPECTOS Y CSV ──
async function saveProspect() {
  const name = document.getElementById('f-name').value.trim();
  const owner = document.getElementById('f-owner').value.trim();
  const phone = document.getElementById('f-phone').value.trim();
  const city = document.getElementById('f-city').value.trim();
  
  if (!name || !phone) return alert('El nombre del restaurante y el teléfono son obligatorios.');

  const btn = document.getElementById('btn-save-prospect');
  btn.disabled = true; btn.textContent = 'Guardando...';

  const res = await api('POST', '/prospects', { restaurant_name: name, owner_name: owner, phone: phone, city: city, stage: 'prospecto' });
  
  btn.disabled = false; btn.textContent = 'Guardar';
  if (res && res.success) {
    closeModal('modal-add');
    document.getElementById('f-name').value = ''; document.getElementById('f-owner').value = '';
    document.getElementById('f-phone').value = ''; document.getElementById('f-city').value = '';
    toast('Prospecto creado exitosamente');
    loadProspects(); loadStats();
  }
}

async function handleCSVUpload(e) {
  const file = e.target.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  
  toast('Subiendo y procesando CSV...', 'info');
  const r = await fetch('/api/crm/upload-csv', { method: 'POST', headers: { 'Authorization': 'Bearer ' + ADMIN_KEY }, body: formData });
  if (r.ok) {
    const data = await r.json();
    toast(`CSV Listo: ${data.inserted} guardados, ${data.errors} omitidos.`);
    loadProspects(); loadStats();
  } else {
    toast('Error al procesar el archivo CSV', 'err');
  }
  e.target.value = ''; 
}

// ── NOTAS Y ESTADOS (INBOX) ──
async function loadNotes(pid) {
  const res = await api('GET', `/prospects/${pid}/notes`);
  const list = document.getElementById('notes-list');
  list.innerHTML = (res?.notes||[]).map(n => `
    <div style="background:var(--bg); border:1px solid var(--border); padding:10px; border-radius:8px; margin-bottom:8px;">
      <div style="font-size:0.75rem; color:var(--muted); margin-bottom:5px;">${NOTE_ICONS[n.note_type] || '📝'} ${new Date(n.created_at).toLocaleString()} por ${n.author}</div>
      <div style="font-size:0.85rem; white-space: pre-wrap;">${n.content}</div>
    </div>
  `).join('');
}

async function addNote() {
  const type = document.getElementById('note-type').value;
  const content = document.getElementById('note-input').value.trim();
  if(!content) return;
  await api('POST', `/prospects/${currentPid}/notes`, { content, note_type: type });
  document.getElementById('note-input').value = '';
  loadNotes(currentPid);
  toast('Nota guardada');
}

async function updateInboxStage() {
  const stage = document.getElementById('inbox-stage-select').value;
  await api('PATCH', `/prospects/${currentInboxPid}/stage`, { stage });
  loadProspects(); loadStats(); toast('Etapa actualizada');
}

// ── GESTIÓN DE TEMPLATES (CREAR Y ELIMINAR) ──
function openTemplatesListModal() {
  renderTemplatesList();
  document.getElementById('modal-templates-list').style.display = 'flex';
}

function renderTemplatesList() {
  const c = document.getElementById('templates-list-content');
  if (!templates.length) { c.innerHTML = '<p style="color:var(--muted);font-size:0.8rem;">No hay templates creados aún.</p>'; return; }
  
  c.innerHTML = templates.map(t => `
    <div style="background:var(--bg3); border:1px solid var(--border); padding:10px; border-radius:8px; margin-bottom:10px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <b style="font-size:0.9rem;">${t.name}</b>
        <button class="btn btn-red btn-xs" onclick="deleteTemplate(${t.id})" style="padding:4px 8px; font-size:10px;">🗑 Eliminar</button>
      </div>
      <div style="font-size:0.75rem; color:var(--muted); margin-bottom:5px;">ID Meta: ${t.wa_name}</div>
      <div style="font-size:0.8rem; background:var(--bg); padding:8px; border-radius:4px;">${t.body}</div>
    </div>
  `).join('');
}

async function saveTemplate() {
  const name = document.getElementById('nt-name').value.trim();
  const wa_name = document.getElementById('nt-wa-name').value.trim();
  const body = document.getElementById('nt-body').value.trim();
  const pStr = document.getElementById('nt-params').value.trim();
  
  if (!name || !wa_name || !body) return alert('Los campos Nombre, ID y Cuerpo son obligatorios.');
  
  const params = pStr ? pStr.split(',').map(s=>s.trim()).filter(Boolean) : [];
  const res = await api('POST', '/templates', { name, wa_name, body, params, category: 'MARKETING' });
  if (res && res.success) {
    toast('Template guardado en base de datos');
    document.getElementById('nt-name').value = ''; document.getElementById('nt-wa-name').value = '';
    document.getElementById('nt-body').value = ''; document.getElementById('nt-params').value = '';
    await loadTemplates();
    renderTemplatesList();
  }
}

async function deleteTemplate(tid) {
  if (!confirm('¿Seguro que deseas eliminar este template permanentemente?')) return;
  await api('DELETE', `/templates/${tid}`);
  toast('Template eliminado');
  await loadTemplates();
  renderTemplatesList();
}

// ── ENVÍO DE TEMPLATES MASIVOS O INDIVIDUALES ──
function openTemplateModal() {
  if (!templates.length) return alert('No tienes templates guardados. Créalos primero en "Mis Templates".');
  
  const sel = document.getElementById('tpl-select');
  sel.innerHTML = '<option value="">-- Selecciona un template --</option>' + templates.map(t => `<option value="${t.id}">${t.name}</option>`).join('');
  
  const plist = document.getElementById('tpl-prospect-list');
  if (selectedIds.size === 0) {
     plist.innerHTML = '<div style="padding:10px; color:var(--amber); font-size:0.8rem;">⚠️ No has seleccionado prospectos. Seleccionalos en la "Tabla Base" primero, o envíalo directo desde un chat abierto.</div>';
     document.getElementById('btn-send-tpl').disabled = true;
  } else {
     const selectedP = prospects.filter(p => selectedIds.has(p.id));
     plist.innerHTML = selectedP.map(p => `<div style="padding:5px 10px; border-bottom:1px solid var(--border); font-size:0.8rem;">${p.restaurant_name} (${p.phone})</div>`).join('');
     document.getElementById('btn-send-tpl').disabled = false;
  }
  
  document.getElementById('tpl-params-section').style.display = 'none';
  document.getElementById('send-results').style.display = 'none';
  document.getElementById('modal-template-send').style.display = 'flex';
}

function onTemplateSelect() {
  const tid = parseInt(document.getElementById('tpl-select').value);
  const tpl = templates.find(t => t.id === tid);
  if (!tpl) {
    document.getElementById('tpl-preview-text').textContent = '';
    document.getElementById('tpl-params-section').style.display = 'none';
    return;
  }
  document.getElementById('tpl-preview-text').textContent = tpl.body;
  
  const psec = document.getElementById('tpl-params-section');
  const pinputs = document.getElementById('tpl-params-inputs');
  if (tpl.params && tpl.params.length > 0) {
    psec.style.display = 'block';
    pinputs.innerHTML = tpl.params.map((p, i) => `
      <div style="margin-bottom:8px;">
        <label style="font-size:0.75rem; color:var(--muted);">${p} ({{${i+1}}})</label>
        <input type="text" class="form-input tpl-param-val" placeholder="Pista: usa {nombre_restaurante}">
      </div>
    `).join('');
  } else {
    psec.style.display = 'none';
    pinputs.innerHTML = '';
  }
}

async function doSendTemplate() {
  const tid = parseInt(document.getElementById('tpl-select').value);
  if (!tid) return alert('Por favor, selecciona un template.');
  
  const inputs = document.querySelectorAll('.tpl-param-val');
  const paramValues = Array.from(inputs).map(i => i.value.trim());
  if (paramValues.some(v => !v)) return alert('Debes llenar todas las variables del template.');

  const idsToSend = Array.from(selectedIds);
  if (!idsToSend.length) return alert('No hay prospectos seleccionados.');

  const params_map = {};
  idsToSend.forEach(id => {
    const prospect = prospects.find(x => x.id === id);
    const finalParams = paramValues.map(v => {
      if (v.includes('{nombre_restaurante}')) return prospect.restaurant_name;
      if (v.includes('{nombre_dueño}')) return prospect.owner_name || 'Dueño';
      return v;
    });
    params_map[id] = finalParams;
  });

  const btn = document.getElementById('btn-send-tpl');
  btn.disabled = true; btn.textContent = 'Enviando a Meta... ⏳';

  const res = await api('POST', '/send-template', { template_id: tid, prospect_ids: idsToSend, params_map });
  
  btn.disabled = false; btn.textContent = '📤 Enviar a seleccionados';

  if (res && res.success) {
    document.getElementById('send-results').style.display = 'block';
    document.getElementById('send-results-list').innerHTML = res.results.map(r => `
      <div style="font-size:0.8rem; color:${r.status==='sent'?'var(--g)':'var(--red)'}; border-bottom:1px solid var(--border); padding:4px 0;">
        ${r.phone}: ${r.status === 'sent' ? 'Enviado ✅' : 'Error ❌ ('+r.error+')'}
      </div>
    `).join('');
    toast(`Envíos exitosos: ${res.sent} | Errores: ${res.errors}`);
    loadProspects();
  }
}

function sendSingleTemplateFromInbox() {
  if (!currentInboxPid) return;
  selectedIds.clear();
  selectedIds.add(currentInboxPid);
  openTemplateModal();
}

// ── LÓGICA DE SELECCIÓN EN TABLA BASE ──
function toggleAllCheck() {
  const isChecked = document.getElementById('check-all').checked;
  const checkboxes = document.querySelectorAll('.row-check');
  checkboxes.forEach(c => {
    c.checked = isChecked;
    const id = parseInt(c.value);
    if(isChecked) selectedIds.add(id); else selectedIds.delete(id);
  });
  updateBulkBar();
}

function toggleCheck(id) {
  if(selectedIds.has(id)) selectedIds.delete(id); else selectedIds.add(id);
  updateBulkBar();
}

function clearSelection() {
  selectedIds.clear();
  document.querySelectorAll('.row-check, #check-all').forEach(c => c.checked = false);
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById('bulk-bar');
  const count = document.getElementById('bulk-bar-count');
  const badge = document.getElementById('selected-count-badge');
  
  if (selectedIds.size > 0) {
    if(bar) bar.style.display = 'flex';
    if(count) count.textContent = `${selectedIds.size} seleccionados`;
    if(badge) { badge.style.display = 'inline-block'; badge.textContent = selectedIds.size; }
  } else {
    if(bar) bar.style.display = 'none';
    if(badge) badge.style.display = 'none';
  }
}