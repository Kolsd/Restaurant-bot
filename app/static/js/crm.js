/* ═══════════════════════════════════════════════════
   Mesio CRM Enterprise — v3.0
   ═══════════════════════════════════════════════════ */

const ADMIN_KEY = localStorage.getItem('mesio_admin_key');
if (!ADMIN_KEY) window.location.href = '/superadmin?redirect=crm';

// ── STATE ────────────────────────────────────────────
const S = {
  view: 'dashboard',
  prospects: [],
  filtered: [],
  templates: [],
  stats: {},
  page: 0,
  pageSize: 25,
  sortCol: 'created_at',
  sortDir: 'desc',
  selectedIds: new Set(),
  filters: { stage:'', priority:'', city:'', source:'', search:'', archived: false },
  activeId: null,
  detailTab: 'overview',
  inboxId: null,
  pollTimer: null,
  lastUpdate: null,
  dragSrcId: null,
};

const STAGES = ['prospecto','contactado','respondio','demo','negociacion','cerrado','perdido'];
const STAGE_LABEL = {prospecto:'Prospecto',contactado:'Contactado',respondio:'Respondió',
  demo:'En Demo',negociacion:'Negociación',cerrado:'Cerrado',perdido:'Perdido'};
const STAGE_COLOR = {prospecto:'#6B7280',contactado:'#3B82F6',respondio:'#F59E0B',
  demo:'#8B5CF6',negociacion:'#F97316',cerrado:'#10B981',perdido:'#EF4444'};
const NOTE_ICON = {note:'📝',call:'📞',whatsapp:'💬',email:'📧',meeting:'🤝'};
const VIEW_TITLE = {dashboard:'Dashboard',inbox:'Inbox',pipeline:'Pipeline',contacts:'Contactos'};

const H = () => ({ 'Authorization':'Bearer '+ADMIN_KEY, 'Content-Type':'application/json' });

// ── API ──────────────────────────────────────────────
async function api(method, path, body) {
  try {
    const opts = { method, headers: H() };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch('/api/crm' + path, opts);
    if (r.status === 401) { window.location.href = '/superadmin?redirect=crm'; return null; }
    if (!r.ok) { const e = await r.json().catch(()=>({detail:'Error'})); throw new Error(e.detail||'Error'); }
    return r.status === 204 ? {} : await r.json();
  } catch(e) { toast(e.message, 'err'); return null; }
}

// ── INIT ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await Promise.all([loadProspects(), loadTemplates(), loadStats()]);
  renderDashboard();
  startPoll();
});

async function loadProspects() {
  const archived = S.filters.archived ? 'true' : 'false';
  const d = await api('GET', `/prospects?archived=${archived}&limit=2000`);
  if (d) { S.prospects = d.prospects || []; applyFilters(); }
}

async function loadStats() {
  const d = await api('GET', '/stats');
  if (d) S.stats = d;
}

async function loadTemplates() {
  const d = await api('GET', '/templates');
  if (d) { S.templates = d.templates || []; populateTemplateSelects(); }
}

function populateTemplateSelects() {
  ['tpl-send-sel'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">— Elegir —</option>' +
      S.templates.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('');
    sel.value = cur;
  });
}

// ── POLL ─────────────────────────────────────────────
function startPoll() {
  S.pollTimer = setInterval(async () => {
    const d = await api('GET', '/check-updates');
    if (!d) return;
    if (S.lastUpdate && d.latest !== S.lastUpdate) {
      await loadProspects();
      await loadStats();
      if (S.view === 'dashboard') renderDashboard();
      else if (S.view === 'inbox') renderInbox();
      else if (S.view === 'pipeline') renderPipeline();
      else if (S.view === 'contacts') renderContacts();
      if (S.activeId) refreshDetailSoft();
    }
    S.lastUpdate = d.latest;
  }, 5000);
}

// ── FILTERS ──────────────────────────────────────────
function applyFilters() {
  const f = S.filters;
  f.stage    = document.getElementById('f-stage')?.value   || '';
  f.priority = document.getElementById('f-priority')?.value || '';
  f.city     = (document.getElementById('f-city')?.value   || '').trim().toLowerCase();
  f.source   = document.getElementById('f-source')?.value  || '';
  f.archived = document.getElementById('f-archived')?.checked || false;

  const q = f.search.toLowerCase();
  S.filtered = S.prospects.filter(p => {
    if (!f.archived && p.archived) return false;
    if (f.archived && !p.archived) return false;
    if (f.stage    && p.stage    !== f.stage)    return false;
    if (f.priority && p.priority !== f.priority) return false;
    if (f.source   && p.source   !== f.source)   return false;
    if (f.city     && !(p.city||'').toLowerCase().includes(f.city)) return false;
    if (q && !( (p.restaurant_name||'').toLowerCase().includes(q) ||
                (p.phone||'').includes(q) ||
                (p.owner_name||'').toLowerCase().includes(q) ||
                (p.city||'').toLowerCase().includes(q) )) return false;
    return true;
  });

  // sort
  S.filtered.sort((a, b) => {
    let av = a[S.sortCol] || '', bv = b[S.sortCol] || '';
    if (av < bv) return S.sortDir === 'asc' ? -1 : 1;
    if (av > bv) return S.sortDir === 'asc' ? 1 : -1;
    return 0;
  });

  S.page = 0;
  updateFilterDot();

  if (S.view === 'contacts') renderContacts();
  else if (S.view === 'pipeline') renderPipeline();
  else if (S.view === 'inbox') renderInbox();
}

function onSearch(v) { S.filters.search = v; applyFilters(); }

function clearFilters() {
  ['f-stage','f-priority','f-city','f-source'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  const fa = document.getElementById('f-archived');
  if (fa) fa.checked = false;
  S.filters = { stage:'', priority:'', city:'', source:'', search:'', archived:false };
  document.getElementById('search-input').value = '';
  applyFilters();
}

function updateFilterDot() {
  const f = S.filters;
  const active = !!(f.stage || f.priority || f.city || f.source || f.archived);
  const dot = document.getElementById('filter-dot');
  if (dot) dot.style.display = active ? 'inline-block' : 'none';
}

function toggleFilters() {
  const fb = document.getElementById('filter-bar');
  fb.style.display = fb.style.display === 'none' ? 'flex' : 'none';
}

// ── VIEW SWITCH ───────────────────────────────────────
function showView(v, btn) {
  S.view = v;
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  const sec = document.getElementById('view-' + v);
  if (sec) sec.classList.add('active');
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.getElementById('topbar-title').textContent = VIEW_TITLE[v] || v;
  if (v === 'dashboard') renderDashboard();
  else if (v === 'inbox')    renderInbox();
  else if (v === 'pipeline') renderPipeline();
  else if (v === 'contacts') renderContacts();
}

// ── DASHBOARD ─────────────────────────────────────────
function renderDashboard() {
  renderKPIs();
  renderFunnel();
  renderFollowups();
  renderStageDist();
  renderActivityFeed();
}

function renderKPIs() {
  const st = S.stats;
  const total     = st.total || 0;
  const contacted = st.contacted || 0;
  const demo      = (st.stage_counts||{}).demo || 0;
  const converted = st.converted || 0;
  const rate      = st.conversion_rate || 0;
  document.getElementById('kpi-row').innerHTML = `
    <div class="kpi-card"><div class="kpi-label">Total prospectos</div><div class="kpi-value">${total}</div><div class="kpi-sub">En base de datos</div></div>
    <div class="kpi-card kpi-blue"><div class="kpi-label">Contactados</div><div class="kpi-value">${contacted}</div><div class="kpi-sub">Alcanzados</div></div>
    <div class="kpi-card kpi-purple"><div class="kpi-label">En Demo</div><div class="kpi-value">${demo}</div><div class="kpi-sub">En evaluación</div></div>
    <div class="kpi-card kpi-green"><div class="kpi-label">Cerrados</div><div class="kpi-value">${converted}</div><div class="kpi-sub">Convertidos</div></div>
    <div class="kpi-card kpi-amber"><div class="kpi-label">Conversión</div><div class="kpi-value">${rate}%</div><div class="kpi-sub">Tasa global</div></div>
  `;
}

function renderFunnel() {
  const counts = S.stats.stage_counts || {};
  const max = Math.max(...STAGES.map(s => counts[s]||0), 1);
  document.getElementById('dash-funnel').innerHTML = STAGES.map(s => {
    const cnt = counts[s] || 0;
    const pct = Math.max((cnt / max) * 100, cnt > 0 ? 3 : 0);
    return `<div class="funnel-row">
      <div class="funnel-label">${STAGE_LABEL[s]}</div>
      <div class="funnel-bar-wrap"><div class="funnel-bar" style="width:${pct}%;background:${STAGE_COLOR[s]}"></div></div>
      <div class="funnel-cnt">${cnt}</div>
    </div>`;
  }).join('');
}

function renderFollowups() {
  const now = new Date();
  const items = S.prospects
    .filter(p => !p.archived && p.next_follow_up)
    .sort((a,b) => new Date(a.next_follow_up) - new Date(b.next_follow_up))
    .slice(0, 8);
  document.getElementById('followup-count').textContent = items.length;
  if (!items.length) {
    document.getElementById('dash-followups').innerHTML = '<div class="followup-empty">Sin seguimientos pendientes 🎉</div>';
    return;
  }
  document.getElementById('dash-followups').innerHTML = items.map(p => {
    const d = new Date(p.next_follow_up);
    const overdue = d < now;
    const label = overdue ? '⚠ ' + fmtDate(d) : fmtDate(d);
    return `<div class="followup-item${overdue?' followup-overdue':''}" onclick="openDetail(${p.id})">
      <span class="badge badge-${p.stage}">${STAGE_LABEL[p.stage]}</span>
      <span class="followup-name">${esc(p.restaurant_name)}</span>
      <span class="followup-time">${label}</span>
    </div>`;
  }).join('');
}

function renderStageDist() {
  const counts = S.stats.stage_counts || {};
  const total = Object.values(counts).reduce((a,b)=>a+b,0) || 1;
  document.getElementById('dash-stages').innerHTML = STAGES.map(s => {
    const cnt = counts[s] || 0;
    const pct = (cnt/total)*100;
    return `<div class="stage-dist-row">
      <div class="stage-dist-label">${STAGE_LABEL[s]}</div>
      <div class="stage-dist-bar"><div class="stage-dist-fill" style="width:${pct}%;background:${STAGE_COLOR[s]}"></div></div>
      <div class="stage-dist-cnt">${cnt}</div>
    </div>`;
  }).join('');
}

async function renderActivityFeed() {
  // Use 5 most recently updated prospects as activity proxy
  const recent = [...S.prospects]
    .filter(p => p.last_contact_at)
    .sort((a,b) => new Date(b.last_contact_at) - new Date(a.last_contact_at))
    .slice(0, 8);
  if (!recent.length) {
    document.getElementById('dash-activity').innerHTML = '<div class="activity-empty">Sin actividad reciente</div>';
    return;
  }
  document.getElementById('dash-activity').innerHTML = recent.map(p => `
    <div class="activity-item" style="cursor:pointer" onclick="openDetail(${p.id})">
      <div class="activity-dot">${stageEmoji(p.stage)}</div>
      <div class="activity-content">
        <div class="activity-name">${esc(p.restaurant_name)}</div>
        <div class="activity-desc">${STAGE_LABEL[p.stage]}${p.city?' · '+esc(p.city):''}</div>
        <div class="activity-time">${fmtRelative(p.last_contact_at)}</div>
      </div>
      <span class="badge badge-${p.stage}" style="flex-shrink:0">${STAGE_LABEL[p.stage]}</span>
    </div>`).join('');
}

// ── INBOX ─────────────────────────────────────────────
function renderInbox() {
  const sorted = [...S.filtered].sort((a,b) => {
    const ta = a.last_contact_at || a.created_at || '';
    const tb = b.last_contact_at || b.created_at || '';
    return tb.localeCompare(ta);
  });
  document.getElementById('inbox-total').textContent = sorted.length;
  document.getElementById('inbox-list').innerHTML = sorted.map(p => {
    const active   = S.inboxId === p.id;
    const dir      = p.last_message_direction;     // 'inbound' | 'outbound' | null
    const hasNew   = dir === 'inbound' && !active;
    const waiting  = dir === 'outbound';
    const preview  = p.last_message_preview
      ? esc(p.last_message_preview.slice(0, 50)) + (p.last_message_preview.length > 50 ? '…' : '')
      : '<span style="color:var(--text-3);font-style:italic">Sin mensajes</span>';
    const statusDot = hasNew
      ? `<span style="width:10px;height:10px;border-radius:50%;background:#22c55e;flex-shrink:0;display:inline-block"></span>`
      : waiting
        ? `<span style="font-size:11px;color:var(--text-3)">⏳</span>`
        : '';
    return `<div class="inbox-row${active?' active':''}${hasNew?' inbox-row--unread':''}" onclick="openInboxChat(${p.id})">
      <div class="inbox-av" style="${hasNew?'background:#22c55e;color:#fff':''}">${(p.restaurant_name||'?')[0].toUpperCase()}</div>
      <div class="inbox-info" style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:6px;justify-content:space-between">
          <div class="inbox-iname" style="${hasNew?'font-weight:700':''}">${esc(p.restaurant_name)}${p.owner_name?' · '+esc(p.owner_name):''}</div>
          ${statusDot}
        </div>
        <div class="inbox-ipreview" style="${hasNew?'color:var(--text-1);font-weight:500':''}">${preview}</div>
        <div class="inbox-imeta">${p.city?esc(p.city)+' · ':''}${STAGE_LABEL[p.stage]||''}</div>
      </div>
    </div>`;
  }).join('') || '<div class="empty-state"><div class="empty-state-ico">💬</div>Sin conversaciones</div>';
}

async function openInboxChat(id) {
  S.inboxId = id;
  const p = S.prospects.find(x => x.id === id);
  if (!p) return;
  // mark active row
  document.querySelectorAll('.inbox-row').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.inbox-row').forEach(el => {
    if (el.onclick?.toString().includes('('+id+')')) el.classList.add('active');
  });
  renderInbox(); // re-render list to update active state
  // Show chat pane
  document.getElementById('inbox-placeholder').style.display = 'none';
  document.getElementById('chat-container').style.display = 'flex';
  document.getElementById('chat-hd').innerHTML = `
    <div>
      <div class="chat-hd-name">${esc(p.restaurant_name)}</div>
      <div class="chat-hd-meta">${p.owner_name?esc(p.owner_name)+' · ':''}${esc(p.phone||'')} · <span class="badge badge-${p.stage}">${STAGE_LABEL[p.stage]}</span></div>
    </div>
    <button class="btn-ghost btn-sm" onclick="openDetail(${p.id})">Ver perfil →</button>`;
  // mobile
  document.querySelector('.inbox-wrap')?.classList.add('chat-open');
  await loadInboxMessages(id);
}

async function loadInboxMessages(id) {
  const d = await api('GET', `/prospects/${id}/interactions`);
  if (!d) return;
  const msgs = d.interactions || [];
  const el = document.getElementById('chat-messages');
  if (!msgs.length) { el.innerHTML = '<div class="empty-state" style="padding:2rem">Sin mensajes aún</div>'; return; }
  el.innerHTML = msgs.filter(m => m.content?.trim()).map(m => {
    const out = m.direction === 'outbound';
    return `<div class="msg-bubble ${out?'msg-out':'msg-in'}${m.template_name?' msg-template':''}">
      ${esc(m.content)}
      <div class="msg-time">${fmtTime(m.created_at)}</div>
    </div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg || !S.inboxId) return;
  input.value = '';
  autoResizeChatInput(input);
  const d = await api('POST', '/send-message', { prospect_id: S.inboxId, message: msg });
  if (d?.success) { await loadInboxMessages(S.inboxId); toast('Mensaje enviado', 'ok'); }
}

function chatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function autoResizeChatInput(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 180) + 'px';
}

function chatInputPaste(e) {
  const html = e.clipboardData.getData('text/html');
  if (!html) return; // plain text paste: browser handles it natively
  e.preventDefault();
  const tmp = document.createElement('div');
  tmp.innerHTML = html;
  // block-level elements → newline before their text
  tmp.querySelectorAll('p, div, li, tr, blockquote').forEach(el => {
    el.prepend(document.createTextNode('\n'));
  });
  // <br> → newline
  tmp.querySelectorAll('br').forEach(el => el.replaceWith('\n'));
  let text = tmp.textContent;
  // collapse more than 2 consecutive newlines, trim edges
  text = text.replace(/\n{3,}/g, '\n\n').replace(/^\n+|\n+$/g, '');
  const ta = e.target;
  const s = ta.selectionStart, end = ta.selectionEnd;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(end);
  ta.selectionStart = ta.selectionEnd = s + text.length;
  autoResizeChatInput(ta);
}

function openQuickTemplate() {
  if (!S.inboxId) return;
  S.selectedIds.clear();
  S.selectedIds.add(S.inboxId);
  openTemplateModal();
}

// ── PIPELINE ──────────────────────────────────────────
function renderPipeline() {
  const byStage = {};
  STAGES.forEach(s => byStage[s] = []);
  S.filtered.forEach(p => { if (byStage[p.stage]) byStage[p.stage].push(p); });

  document.getElementById('kanban-board').innerHTML = STAGES.map(s => {
    const cards = byStage[s];
    return `<div class="kanban-col" data-stage="${s}">
      <div class="kanban-col-hd">
        <div class="kanban-col-dot" style="background:${STAGE_COLOR[s]}"></div>
        <div class="kanban-col-name">${STAGE_LABEL[s]}</div>
        <div class="kanban-col-cnt">${cards.length}</div>
      </div>
      <div class="kanban-col-body kanban-drop-zone" data-stage="${s}"
        ondragover="kanbanDragOver(event)" ondrop="kanbanDrop(event,this)" ondragleave="kanbanDragLeave(this)">
        ${cards.map(p => kanbanCard(p)).join('')}
      </div>
    </div>`;
  }).join('');
}

function kanbanCard(p) {
  const pri = p.priority === 'high' ? '🔴' : p.priority === 'low' ? '⚪' : '🟡';
  return `<div class="kanban-card" draggable="true" data-id="${p.id}"
    ondragstart="kanbanDragStart(event,${p.id})"
    onclick="openDetail(${p.id})">
    <div class="kc-name">${esc(p.restaurant_name)}</div>
    <div class="kc-phone">${esc(p.phone||'')}</div>
    <div class="kc-meta">
      ${p.city?`<span class="kc-city">📍 ${esc(p.city)}</span>`:''}
      <span>${pri}</span>
      ${p.next_follow_up?`<span title="Seguimiento" style="font-size:11px;color:#888">⏰</span>`:''}
    </div>
  </div>`;
}

function kanbanDragStart(e, id) {
  S.dragSrcId = id;
  e.dataTransfer.effectAllowed = 'move';
  setTimeout(() => { e.target.classList.add('dragging'); }, 0);
}
function kanbanDragOver(e) { e.preventDefault(); e.currentTarget.classList.add('drag-over'); }
function kanbanDragLeave(el) { el.classList.remove('drag-over'); }
async function kanbanDrop(e, el) {
  e.preventDefault(); el.classList.remove('drag-over');
  const newStage = el.dataset.stage;
  if (!S.dragSrcId || !newStage) return;
  const p = S.prospects.find(x => x.id === S.dragSrcId);
  if (!p || p.stage === newStage) return;
  const d = await api('PATCH', `/prospects/${S.dragSrcId}/stage`, { stage: newStage });
  if (d?.success) {
    p.stage = newStage;
    toast(`Movido a ${STAGE_LABEL[newStage]}`, 'ok');
    renderPipeline();
    await loadStats();
    renderFunnel(); renderStageDist();
  }
  S.dragSrcId = null;
}

// ── CONTACTS TABLE ────────────────────────────────────
function renderContacts() {
  const total = S.filtered.length;
  const ps    = S.pageSize;
  const start = S.page * ps;
  const page  = S.filtered.slice(start, start + ps);

  document.getElementById('contacts-count').textContent = `${total} prospecto${total!==1?'s':''}`;
  document.getElementById('ctable-body').innerHTML = page.map(p => {
    const chk = S.selectedIds.has(p.id);
    const fu = p.next_follow_up ? new Date(p.next_follow_up) : null;
    const fuOverdue = fu && fu < new Date();
    return `<tr class="${chk?'selected':''}" onclick="openDetail(${p.id})">
      <td onclick="event.stopPropagation()"><input type="checkbox" ${chk?'checked':''} onchange="toggleCheck(${p.id},this)"></td>
      <td class="td-name">${esc(p.restaurant_name)}</td>
      <td>${esc(p.owner_name||'—')}</td>
      <td class="td-phone">${esc(p.phone||'—')}</td>
      <td class="td-city">${esc(p.city||'—')}</td>
      <td><span class="badge badge-${p.stage}">${STAGE_LABEL[p.stage]||p.stage}</span></td>
      <td><span class="badge badge-pri-${p.priority}">${priLabel(p.priority)}</span></td>
      <td><span class="badge badge-source">${esc(p.source||'manual')}</span></td>
      <td class="td-date">${p.last_contact_at ? fmtDate(new Date(p.last_contact_at)) : '—'}</td>
      <td class="${fuOverdue?'td-overdue':'td-date'}">${fu ? fmtDate(fu) : '—'}</td>
      <td onclick="event.stopPropagation()">
        <button class="btn-ghost btn-sm" onclick="openDetail(${p.id})">→</button>
      </td>
    </tr>`;
  }).join('') || `<tr><td colspan="11" class="empty-state">
    <div class="empty-state-ico">🔍</div>Sin resultados
  </td></tr>`;

  renderPagination(total, ps);
  updateCheckAll();
}

function renderPagination(total, ps) {
  const pages = Math.max(1, Math.ceil(total / ps));
  const cur = S.page;
  let btns = '';
  const show = (i) => `<button class="pag-btn${i===cur?' active':''}" onclick="goPage(${i})">${i+1}</button>`;
  if (pages <= 7) {
    for (let i=0;i<pages;i++) btns += show(i);
  } else {
    btns += show(0);
    if (cur > 2) btns += '<span style="padding:0 4px;color:#888">…</span>';
    for (let i=Math.max(1,cur-1);i<=Math.min(pages-2,cur+1);i++) btns += show(i);
    if (cur < pages-3) btns += '<span style="padding:0 4px;color:#888">…</span>';
    btns += show(pages-1);
  }
  const from = total ? cur*ps+1 : 0, to = Math.min((cur+1)*ps, total);
  document.getElementById('table-pagination').innerHTML = `
    <span class="pag-info">${from}–${to} de ${total}</span>
    <div class="pag-btns">
      <button class="pag-btn" onclick="goPage(${cur-1})" ${cur===0?'disabled':''}>‹</button>
      ${btns}
      <button class="pag-btn" onclick="goPage(${cur+1})" ${cur>=pages-1?'disabled':''}>›</button>
    </div>`;
}

function goPage(p) { S.page = p; renderContacts(); }
function changePageSize(v) { S.pageSize = parseInt(v); S.page = 0; renderContacts(); }

function sortBy(col) {
  if (S.sortCol === col) S.sortDir = S.sortDir === 'asc' ? 'desc' : 'asc';
  else { S.sortCol = col; S.sortDir = 'asc'; }
  document.querySelectorAll('.sort-arr').forEach(el => {
    el.className = 'sort-arr' + (el.dataset.col === col ? (' ' + S.sortDir) : '');
  });
  applyFilters();
}

// ── SELECTION ─────────────────────────────────────────
function toggleCheck(id, cb) {
  if (cb.checked) S.selectedIds.add(id); else S.selectedIds.delete(id);
  const row = cb.closest('tr'); if (row) row.classList.toggle('selected', cb.checked);
  updateBulkBar(); updateCheckAll();
}
function toggleAllCheck(cb) {
  const ps = S.pageSize, start = S.page * ps;
  const page = S.filtered.slice(start, start + ps);
  page.forEach(p => { if (cb.checked) S.selectedIds.add(p.id); else S.selectedIds.delete(p.id); });
  renderContacts(); updateBulkBar();
}
function updateCheckAll() {
  const ps = S.pageSize, start = S.page * ps;
  const page = S.filtered.slice(start, start + ps);
  const ca = document.getElementById('check-all');
  if (ca) ca.checked = page.length > 0 && page.every(p => S.selectedIds.has(p.id));
}
function updateBulkBar() {
  const n = S.selectedIds.size;
  const bar = document.getElementById('bulk-bar');
  bar.style.display = n > 0 ? 'flex' : 'none';
  document.getElementById('bulk-count').textContent = `${n} seleccionado${n!==1?'s':''}`;
}
function clearSelection() { S.selectedIds.clear(); updateBulkBar(); renderContacts(); }

// ── DETAIL PANEL ──────────────────────────────────────
function openDetail(id) {
  S.activeId = id;
  const panel = document.getElementById('detail-panel');
  panel.classList.add('open');
  renderDetailHeader();
  dpSwitchTab(S.detailTab, panel.querySelector(`.dp-tab[onclick*="'${S.detailTab}'"]`));
}
function closeDetail() {
  S.activeId = null;
  document.getElementById('detail-panel').classList.remove('open');
}
function renderDetailHeader() {
  const p = S.prospects.find(x => x.id === S.activeId);
  if (!p) return;
  document.getElementById('dp-name').textContent = p.restaurant_name;
  document.getElementById('dp-phone').textContent = p.phone || '—';
  // stage select
  const ss = document.getElementById('dp-stage-sel');
  ss.innerHTML = STAGES.map(s => `<option value="${s}"${p.stage===s?' selected':''}>${STAGE_LABEL[s]}</option>`).join('');
  // priority
  document.getElementById('dp-priority-sel').value = p.priority || 'medium';
  // archive button
  document.getElementById('btn-dp-arch').textContent = p.archived ? 'Restaurar' : 'Archivar';
}

function dpSwitchTab(tab, btn) {
  S.detailTab = tab;
  document.querySelectorAll('.dp-tab').forEach(el => el.classList.remove('active'));
  if (btn) btn.classList.add('active');
  else {
    document.querySelectorAll('.dp-tab').forEach(el => {
      if (el.onclick?.toString().includes("'"+tab+"'")) el.classList.add('active');
    });
  }
  if (tab === 'overview')  renderDpOverview();
  else if (tab === 'timeline') renderDpTimeline();
  else if (tab === 'messages') renderDpMessages();
  else if (tab === 'notes')    renderDpNotes();
}

async function refreshDetailSoft() {
  if (!S.activeId) return;
  if (S.detailTab === 'overview') renderDetailHeader();
  else if (S.detailTab === 'timeline') renderDpTimeline();
  else if (S.detailTab === 'messages') renderDpMessages();
}

function renderDpOverview() {
  const p = S.prospects.find(x => x.id === S.activeId);
  if (!p) return;
  const fu = p.next_follow_up ? p.next_follow_up.slice(0,16) : '';
  document.getElementById('dp-body').innerHTML = `
    <div class="dp-field-group"><label>Restaurante</label><input id="dpe-name" value="${esc(p.restaurant_name)}"></div>
    <div class="dp-field-group"><label>Dueño / Contacto</label><input id="dpe-owner" value="${esc(p.owner_name||'')}"></div>
    <div class="dp-field-group"><label>Teléfono</label><input id="dpe-phone" value="${esc(p.phone||'')}"></div>
    <div class="dp-field-group"><label>Ciudad</label><input id="dpe-city" value="${esc(p.city||'')}"></div>
    <div class="dp-field-group"><label>Categoría</label><input id="dpe-cat" value="${esc(p.category||'')}"></div>
    <div class="dp-field-group"><label>Instagram</label><input id="dpe-ig" value="${esc(p.instagram||'')}"></div>
    <div class="dp-field-group"><label>Google Maps</label><input id="dpe-gm" value="${esc(p.google_maps||'')}"></div>
    <div class="dp-field-group"><label>Revenue estimado (USD)</label><input type="number" id="dpe-rev" value="${p.revenue_est||0}"></div>
    <div class="dp-field-group"><label>Seguimiento</label><input type="datetime-local" id="dpe-fu" value="${fu}"></div>
    <div class="dp-save-row"><button class="btn-primary btn-sm" onclick="dpSave()">Guardar cambios</button></div>
  `;
}

async function dpSave() {
  const id = S.activeId; if (!id) return;
  const body = {
    restaurant_name: document.getElementById('dpe-name')?.value.trim(),
    owner_name:      document.getElementById('dpe-owner')?.value.trim(),
    phone:           document.getElementById('dpe-phone')?.value.trim(),
    city:            document.getElementById('dpe-city')?.value.trim(),
    category:        document.getElementById('dpe-cat')?.value.trim(),
    instagram:       document.getElementById('dpe-ig')?.value.trim(),
    google_maps:     document.getElementById('dpe-gm')?.value.trim(),
    revenue_est:     parseInt(document.getElementById('dpe-rev')?.value)||0,
    next_follow_up:  document.getElementById('dpe-fu')?.value||null,
  };
  const d = await api('PATCH', `/prospects/${id}`, body);
  if (d?.success) {
    const idx = S.prospects.findIndex(x => x.id === id);
    if (idx >= 0) S.prospects[idx] = { ...S.prospects[idx], ...body };
    applyFilters();
    toast('Guardado', 'ok');
    renderDetailHeader();
  }
}

async function dpUpdateStage(stage) {
  if (!S.activeId) return;
  const d = await api('PATCH', `/prospects/${S.activeId}/stage`, { stage });
  if (d?.success) {
    const p = S.prospects.find(x => x.id === S.activeId);
    if (p) p.stage = stage;
    applyFilters();
    if (S.view === 'pipeline') renderPipeline();
    await loadStats(); renderFunnel(); renderStageDist();
    toast(`Etapa: ${STAGE_LABEL[stage]}`, 'ok');
  }
}

async function dpUpdatePriority(priority) {
  if (!S.activeId) return;
  const d = await api('PATCH', `/prospects/${S.activeId}`, { priority });
  if (d?.success) {
    const p = S.prospects.find(x => x.id === S.activeId);
    if (p) p.priority = priority;
    applyFilters(); toast('Prioridad actualizada', 'ok');
  }
}

async function dpArchive() {
  if (!S.activeId) return;
  const p = S.prospects.find(x => x.id === S.activeId);
  if (!p) return;
  const archived = !p.archived;
  const d = await api('PATCH', `/prospects/${S.activeId}`, { archived });
  if (d?.success) {
    p.archived = archived;
    document.getElementById('btn-dp-arch').textContent = archived ? 'Restaurar' : 'Archivar';
    applyFilters();
    toast(archived ? 'Archivado' : 'Restaurado', 'ok');
    if (archived) closeDetail();
  }
}

function dpSendTemplate() {
  if (!S.activeId) return;
  S.selectedIds.clear(); S.selectedIds.add(S.activeId);
  openTemplateModal();
}

async function renderDpTimeline() {
  const id = S.activeId; if (!id) return;
  const el = document.getElementById('dp-body');
  el.innerHTML = '<div class="spinner" style="margin:1rem auto;display:block"></div>';
  const [nd, intd] = await Promise.all([
    api('GET', `/prospects/${id}/notes`),
    api('GET', `/prospects/${id}/interactions`),
  ]);
  const notes = (nd?.notes||[]).map(n => ({...n, _type:'note', ts: n.created_at}));
  const ints  = (intd?.interactions||[]).map(i => ({...i, _type:'interaction', ts: i.created_at}));
  const items = [...notes, ...ints].sort((a,b) => b.ts.localeCompare(a.ts));
  if (!items.length) { el.innerHTML = '<div class="tl-empty">Sin actividad registrada</div>'; return; }
  el.innerHTML = '<div class="timeline">' + items.map(item => {
    const icon = item._type === 'note' ? (NOTE_ICON[item.note_type]||'📝') : (item.direction==='outbound'?'📤':'📩');
    const title = item._type === 'note'
      ? `Nota · ${item.note_type||'note'}`
      : `${item.direction==='outbound'?'Mensaje enviado':'Mensaje recibido'}${item.template_name?' ('+esc(item.template_name)+')':''}`;
    return `<div class="tl-item">
      <div class="tl-dot">${icon}</div>
      <div class="tl-content">
        <div class="tl-head"><span class="tl-title">${title}</span><span class="tl-time">${fmtRelative(item.ts)}</span></div>
        <div class="tl-body">${esc(item.content)}</div>
      </div>
    </div>`;
  }).join('') + '</div>';
}

async function renderDpMessages() {
  const id = S.activeId; if (!id) return;
  const el = document.getElementById('dp-body');
  el.innerHTML = '<div class="spinner" style="margin:1rem auto;display:block"></div>';
  const d = await api('GET', `/prospects/${id}/interactions`);
  const msgs = d?.interactions || [];
  const p = S.prospects.find(x => x.id === id);
  el.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:1rem">
      ${msgs.length ? msgs.map(m => {
        const out = m.direction === 'outbound';
        return `<div style="max-width:85%;align-self:${out?'flex-end':'flex-start'}">
          <div class="msg-bubble ${out?'msg-out':'msg-in'}${m.template_name?' msg-template':''}">${esc(m.content)}</div>
          <div class="msg-time" style="text-align:${out?'right':'left'}">${fmtTime(m.created_at)}</div>
        </div>`;
      }).join('') : '<div class="empty-state" style="padding:1.5rem">Sin mensajes</div>'}
    </div>
    <div style="border-top:.5px solid var(--border);padding-top:12px">
      <textarea id="dp-msg-input" rows="2" style="width:100%;border:.5px solid var(--border);border-radius:var(--rad);padding:8px 10px;resize:none;outline:none;font-size:12.5px;background:var(--bg)" placeholder="Escribe un mensaje…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();dpSendMsg();}"></textarea>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px">
        <button class="btn-ghost btn-sm" onclick="dpSendTemplate()">📤 Template</button>
        <button class="btn-primary btn-sm" onclick="dpSendMsg()">Enviar →</button>
      </div>
    </div>`;
}

async function dpSendMsg() {
  const id = S.activeId; if (!id) return;
  const inp = document.getElementById('dp-msg-input');
  const msg = inp?.value.trim(); if (!msg) return;
  inp.value = '';
  const d = await api('POST', '/send-message', { prospect_id: id, message: msg });
  if (d?.success) { toast('Enviado', 'ok'); renderDpMessages(); }
}

async function renderDpNotes() {
  const id = S.activeId; if (!id) return;
  const el = document.getElementById('dp-body');
  el.innerHTML = `
    <div class="notes-add">
      <textarea id="note-text" rows="3" placeholder="Escribe una nota…"></textarea>
      <div class="notes-type-row">
        <select id="note-type">
          <option value="note">📝 Nota</option>
          <option value="call">📞 Llamada</option>
          <option value="whatsapp">💬 WhatsApp</option>
          <option value="email">📧 Email</option>
          <option value="meeting">🤝 Reunión</option>
        </select>
        <button class="btn-primary btn-sm" onclick="addNote()">Agregar</button>
      </div>
    </div>
    <div id="notes-list"><div class="spinner" style="margin:1rem auto;display:block"></div></div>`;
  const d = await api('GET', `/prospects/${id}/notes`);
  const notes = d?.notes || [];
  document.getElementById('notes-list').innerHTML = notes.length
    ? notes.map(n => `<div class="note-item">
        <button class="note-del" onclick="delNote(${n.id})">✕</button>
        <div class="note-meta">${NOTE_ICON[n.note_type]||'📝'} ${n.note_type} · ${fmtRelative(n.created_at)}</div>
        <div class="note-text">${esc(n.content)}</div>
      </div>`).join('')
    : '<div style="color:var(--text-3);font-size:12px;padding:.5rem 0">Sin notas</div>';
}

async function addNote() {
  const id = S.activeId; if (!id) return;
  const content = document.getElementById('note-text')?.value.trim();
  const note_type = document.getElementById('note-type')?.value || 'note';
  if (!content) return;
  const d = await api('POST', `/prospects/${id}/notes`, { content, note_type });
  if (d?.success) { toast('Nota agregada', 'ok'); renderDpNotes(); }
}

async function delNote(nid) {
  const id = S.activeId; if (!id) return;
  const d = await api('DELETE', `/prospects/${id}/notes/${nid}`);
  if (d?.success) { toast('Nota eliminada'); renderDpNotes(); }
}

// ── BULK ACTIONS ──────────────────────────────────────
function bulkSendTemplate() {
  if (!S.selectedIds.size) return;
  openTemplateModal();
}
function openBulkStage() {
  const n = S.selectedIds.size; if (!n) return;
  document.getElementById('bulk-stage-count').textContent = n;
  openModal('modal-bulk-stage');
}
async function doBulkStage() {
  const stage = document.getElementById('bulk-stage-sel').value;
  const ids = [...S.selectedIds];
  await Promise.all(ids.map(id => api('PATCH', `/prospects/${id}/stage`, { stage })));
  ids.forEach(id => { const p = S.prospects.find(x=>x.id===id); if(p) p.stage=stage; });
  applyFilters(); closeModal('modal-bulk-stage'); clearSelection();
  await loadStats(); renderFunnel(); renderStageDist();
  toast(`${ids.length} prospectos → ${STAGE_LABEL[stage]}`, 'ok');
}
async function bulkArchive() {
  const ids = [...S.selectedIds]; if (!ids.length) return;
  if (!confirm(`¿Archivar ${ids.length} prospecto${ids.length!==1?'s':''}?`)) return;
  await Promise.all(ids.map(id => api('PATCH', `/prospects/${id}`, { archived: true })));
  ids.forEach(id => { const p = S.prospects.find(x=>x.id===id); if(p) p.archived=true; });
  applyFilters(); clearSelection();
  toast(`${ids.length} archivado${ids.length!==1?'s':''}`, 'ok');
}

// ── PROSPECT CRUD ─────────────────────────────────────
function openAddModal() { openModal('modal-add'); }

async function saveProspect() {
  const name  = document.getElementById('add-name').value.trim();
  const phone = document.getElementById('add-phone').value.trim();
  if (!name || !phone) { toast('Nombre y teléfono son requeridos', 'err'); return; }
  const body = {
    restaurant_name: name, phone,
    owner_name:   document.getElementById('add-owner').value.trim(),
    city:         document.getElementById('add-city').value.trim(),
    category:     document.getElementById('add-category').value.trim(),
    instagram:    document.getElementById('add-instagram').value.trim(),
    google_maps:  document.getElementById('add-gmaps').value.trim(),
    source:       document.getElementById('add-source').value,
    priority:     document.getElementById('add-priority').value,
    revenue_est:  parseInt(document.getElementById('add-revenue').value)||0,
    next_follow_up: document.getElementById('add-followup').value||null,
    stage: 'prospecto',
  };
  const d = await api('POST', '/prospects', body);
  if (d?.success) {
    S.prospects.unshift(d.prospect);
    applyFilters();
    closeModal('modal-add');
    toast('Prospecto creado', 'ok');
    // clear form
    ['add-name','add-phone','add-owner','add-city','add-category','add-instagram','add-gmaps','add-followup','add-revenue']
      .forEach(id => { const el = document.getElementById(id); if(el) el.value = ''; });
  }
}

// ── TEMPLATES ─────────────────────────────────────────
function openTemplateModal() {
  const n = S.selectedIds.size;
  document.getElementById('tpl-send-count').textContent = n;
  document.getElementById('tpl-send-preview').style.display = 'none';
  document.getElementById('tpl-send-sel').value = '';
  openModal('modal-tpl-send');
}

const _PROSPECT_FIELDS_JS = {
  restaurante: 'restaurant_name', restaurant: 'restaurant_name',
  nombre: 'owner_name', name: 'owner_name',
  ciudad: 'city', city: 'city',
};

function onTplSendSelect() {
  const id = parseInt(document.getElementById('tpl-send-sel').value);
  const tpl = S.templates.find(t => t.id === id);
  const preview = document.getElementById('tpl-send-preview');
  if (!tpl) { preview.style.display = 'none'; return; }
  preview.style.display = 'block';
  const params = tpl.params || [];
  // Mostrar qué campo se usará por parámetro (solo informativo, sin inputs)
  document.getElementById('tpl-params-wrap').innerHTML = params.length
    ? `<div style="margin-top:10px"><div style="font-size:11px;font-weight:600;color:var(--text-3);text-transform:uppercase;margin-bottom:6px">Parámetros automáticos</div>` +
      params.map(p => {
        const key = p.trim().toLowerCase();
        const field = _PROSPECT_FIELDS_JS[key];
        const badge = field
          ? `<span style="color:var(--green,#22c55e)">✓ se usará <b>${field === 'restaurant_name' ? 'nombre del restaurante' : field === 'owner_name' ? 'nombre del dueño' : field}</b></span>`
          : `<span style="color:var(--text-3)">— sin dato, se omitirá</span>`;
        return `<div style="font-size:12px;margin-bottom:4px"><code>{{${esc(p)}}}</code> ${badge}</div>`;
      }).join('') + '</div>'
    : '';
  document.getElementById('tpl-preview-body').textContent = tpl.body;
}

async function doSendTemplate() {
  const tplId = parseInt(document.getElementById('tpl-send-sel').value);
  if (!tplId) { toast('Selecciona un template', 'err'); return; }
  const tpl = S.templates.find(t => t.id === tplId);
  if (!tpl) return;
  const ids = [...S.selectedIds];
  if (!ids.length) { toast('No hay prospectos seleccionados', 'err'); return; }

  const btn = document.querySelector('#modal-tpl-send .btn-primary');
  if (btn) { btn.disabled = true; btn.textContent = 'Enviando…'; }

  try {
    // Parámetros se resuelven automáticamente en el backend desde los datos del prospecto
    const paramsMap = {};
    ids.forEach(id => { paramsMap[id] = []; });
    const d = await api('POST', '/send-template', { prospect_ids: ids, template_id: tplId, params_map: paramsMap });
    if (d) {
      toast(`${d.sent||0} enviados, ${d.errors||0} errores`, d.errors ? 'err' : 'ok');
      closeModal('modal-tpl-send');
      clearSelection();
      await loadProspects();
      if (S.view !== 'pipeline') applyFilters();
      else renderPipeline();
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Enviar'; }
  }
}

function openTemplateManager() {
  loadTemplateManagerList();
  openModal('modal-tpl-mgr');
}

function loadTemplateManagerList() {
  document.getElementById('tpl-mgr-list').innerHTML = S.templates.length
    ? S.templates.map(t => `<div class="tpl-row">
        <div>
          <div class="tpl-row-name">${esc(t.name)}</div>
          <div class="tpl-row-wa">${esc(t.wa_name)}</div>
        </div>
        <button class="btn-ghost btn-sm" onclick="deleteTemplate(${t.id})">Eliminar</button>
      </div>`).join('')
    : '<div style="color:var(--text-3);font-size:12px">Sin templates</div>';
}

async function saveTemplate() {
  const name     = document.getElementById('tpl-name').value.trim();
  const wa_name  = document.getElementById('tpl-wa-name').value.trim();
  const language = document.getElementById('tpl-language').value.trim() || 'es_CO';
  const body     = document.getElementById('tpl-body').value.trim();
  const params   = document.getElementById('tpl-params').value.split(',').map(s=>s.trim()).filter(Boolean);
  if (!name || !wa_name || !body) { toast('Nombre, wa_name y cuerpo son requeridos', 'err'); return; }
  const d = await api('POST', '/templates', { name, wa_name, language, body, params, category:'MARKETING' });
  if (d?.success) {
    await loadTemplates();
    loadTemplateManagerList();
    ['tpl-name','tpl-wa-name','tpl-body','tpl-params'].forEach(id => { const el=document.getElementById(id); if(el) el.value=''; });
    const langEl = document.getElementById('tpl-language'); if(langEl) langEl.value='es_CO';
    toast('Template guardado', 'ok');
  }
}

async function deleteTemplate(id) {
  if (!confirm('¿Eliminar este template?')) return;
  const d = await api('DELETE', `/templates/${id}`);
  if (d?.success) { await loadTemplates(); loadTemplateManagerList(); toast('Eliminado'); }
}

// ── CSV ───────────────────────────────────────────────
async function handleCSVUpload(e) {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  toast('Procesando CSV…');
  try {
    const r = await fetch('/api/crm/upload-csv', { method:'POST', headers:{'Authorization':'Bearer '+ADMIN_KEY}, body:fd });
    const d = await r.json();
    if (r.ok) {
      toast(`✅ ${d.inserted} importados, ${d.errors} omitidos`, 'ok');
      await loadProspects(); await loadStats();
      if (S.view==='dashboard') renderDashboard();
      else if (S.view==='contacts') renderContacts();
      else if (S.view==='pipeline') renderPipeline();
    } else { toast(d.detail||'Error al procesar CSV', 'err'); }
  } catch { toast('Error al procesar CSV', 'err'); }
  e.target.value = '';
}

function exportCSV() {
  const data = S.filtered;
  if (!data.length) { toast('No hay datos para exportar', 'err'); return; }
  const cols = ['id','restaurant_name','owner_name','phone','city','category','instagram','source','stage','priority','revenue_est','last_contact_at','next_follow_up','created_at'];
  const header = cols.join(',');
  const rows = data.map(p => cols.map(c => {
    const v = p[c]??''; return typeof v === 'string' && v.includes(',') ? `"${v.replace(/"/g,'""')}"` : v;
  }).join(','));
  const csv = [header, ...rows].join('\n');
  const blob = new Blob(['\ufeff'+csv], { type:'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href=url; a.download='prospectos_mesio.csv'; a.click();
  URL.revokeObjectURL(url);
  toast(`${data.length} prospectos exportados`, 'ok');
}

// ── MODAL HELPERS ─────────────────────────────────────
function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function closeModalBg(e, id) { if (e.target.id === id) closeModal(id); }

// ── SIDEBAR / MOBILE ──────────────────────────────────
function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  const ov = document.getElementById('mob-overlay');
  sb.classList.toggle('open');
  ov.classList.toggle('show');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('mob-overlay').classList.remove('show');
}
function doLogout() {
  localStorage.removeItem('mesio_admin_key');
  localStorage.removeItem('hq_key');
  window.location.href = '/superadmin';
}

// ── UTILS ─────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtDate(d) {
  if (!d) return '—';
  const dt = d instanceof Date ? d : new Date(d);
  return dt.toLocaleDateString('es-CO', { day:'2-digit', month:'short', year:'numeric' });
}
function fmtTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('es-CO', { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' });
}
function fmtRelative(iso) {
  if (!iso) return '—';
  const sec = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (sec < 60)   return 'Hace un momento';
  if (sec < 3600) return `Hace ${Math.floor(sec/60)} min`;
  if (sec < 86400)return `Hace ${Math.floor(sec/3600)} h`;
  if (sec < 604800)return `Hace ${Math.floor(sec/86400)} d`;
  return fmtDate(iso);
}
function priLabel(p) { return p==='high'?'Alta':p==='low'?'Baja':'Media'; }
function stageEmoji(s) {
  const map = {prospecto:'👤',contactado:'📞',respondio:'💬',demo:'🎯',negociacion:'🤝',cerrado:'✅',perdido:'❌'};
  return map[s]||'•';
}

let _toastTimer = null;
function toast(msg, type='') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (type ? ' '+type : '');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = 'toast'; }, 3500);
}
