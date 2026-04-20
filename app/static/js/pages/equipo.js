/* ══ Equipo admin page — team management view ═══════════════════════
   Admin-only view: shift grid, tip distribution, member table.
   Auto-refreshes every 60s. Zero inline onclick.
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

// ── Auth guard ────────────────────────────────────────────────────
(function () {
  var token = localStorage.getItem('rb_token');
  if (!token) { window.location.href = '/login'; }
})();

// ── State ─────────────────────────────────────────────────────────
var _staff = [];
var _schedules = [];
var _tipsPool = null;
var _refreshTimer = null;
var _activeRoleFilter = 'all';

// ── Week helpers ──────────────────────────────────────────────────
function getWeekStart() {
  var d = new Date();
  var day = (d.getDay() + 6) % 7; // 0=Mon
  d.setDate(d.getDate() - day);
  d.setHours(0, 0, 0, 0);
  return d;
}

function formatDate(d) {
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}

function getDayHeaders() {
  var ws = getWeekStart();
  var headers = [];
  var DAY_ABBR = ['L', 'M', 'M', 'J', 'V', 'S', 'D'];
  for (var i = 0; i < 14; i++) {
    var d = new Date(ws);
    d.setDate(ws.getDate() + i);
    headers.push(DAY_ABBR[i % 7] + d.getDate());
  }
  return headers;
}

// ── Fetch helpers ─────────────────────────────────────────────────
async function apiFetch(path, opts) {
  var res = await fetch(path, Object.assign({ headers: mesioHeaders() }, opts || {}));
  if (res.status === 401) { window.location.href = '/login'; return null; }
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

// ── Load all data ─────────────────────────────────────────────────
async function loadAll() {
  try {
    var [staffData, schedulesData, tipsData] = await Promise.all([
      apiFetch('/api/staff'),
      apiFetch('/api/staff/schedules?week_start=' + formatDate(getWeekStart())),
      apiFetch('/api/stats/tips-pool').catch(function () { return null; })
    ]);

    _staff = Array.isArray(staffData) ? staffData : (staffData && staffData.staff ? staffData.staff : []);
    _schedules = Array.isArray(schedulesData) ? schedulesData : (schedulesData && schedulesData.schedules ? schedulesData.schedules : []);
    _tipsPool = tipsData;

    renderMetrics();
    renderShiftGrid();
    renderTips();
    renderMembersTable();
    mesioTrackFetch(true);
  } catch (e) {
    mesioTrackFetch(false);
    console.warn('equipo load failed:', e);
    mesioToast('Error cargando datos del equipo', 'error');
  }
}

// ── Metrics row ───────────────────────────────────────────────────
function renderMetrics() {
  // Total equipo
  setMetric('metricTotal', _staff.length, _staff.filter(function (s) { return s.status === 'active'; }).length + ' activos');

  // En turno ahora (clock_in set, clock_out null)
  var onShift = _staff.filter(function (s) { return s.clocked_in || s.active_shift; }).length;
  setMetric('metricOnShift', onShift, '');

  // Horas semana
  var totalMins = _schedules.reduce(function (acc, s) {
    var start = parseTime(s.start_time || '00:00');
    var end = parseTime(s.end_time || '00:00');
    var mins = end - start;
    if (mins < 0) mins += 24 * 60;
    return acc + mins;
  }, 0);
  var totalHrs = Math.round(totalMins / 60);
  setMetric('metricHoras', totalHrs + 'h', '');

  // Propinas
  if (_tipsPool && _tipsPool.pool_total != null) {
    setMetric('metricTips', mesioFmt(_tipsPool.pool_total), 'pool disponible');
  } else {
    setMetric('metricTips', '—', 'sin datos');
  }

  // Costo laboral — placeholder
  setMetric('metricCosto', '—', 'próximamente');
}

function setMetric(id, value, sub) {
  var valEl = document.getElementById(id + 'Val');
  var subEl = document.getElementById(id + 'Sub');
  if (valEl) { valEl.textContent = value; }
  if (subEl && sub !== undefined) { subEl.textContent = sub; }
}

function parseTime(t) {
  var parts = (t || '00:00').split(':');
  return parseInt(parts[0], 10) * 60 + parseInt(parts[1] || 0, 10);
}

// ── Shift grid ────────────────────────────────────────────────────
function renderShiftGrid() {
  var grid = document.getElementById('shiftGrid');
  if (!grid) return;
  grid.innerHTML = '';

  var headers = getDayHeaders();
  var ws = getWeekStart();

  // Build lookup: staffId → dayOfWeek → schedule entry
  var schMap = {};
  _schedules.forEach(function (s) {
    var sid = s.staff_id;
    var dow = s.day_of_week; // 0=Mon
    if (!schMap[sid]) schMap[sid] = {};
    schMap[sid][dow] = s;
  });

  // Header row
  var emptyHdr = document.createElement('div');
  grid.appendChild(emptyHdr);
  headers.forEach(function (h) {
    var d = document.createElement('div');
    d.className = 'hdr';
    d.textContent = h;
    grid.appendChild(d);
  });

  // Staff rows (up to 6 + "+N more")
  var shown = _staff.slice(0, 6);
  shown.forEach(function (s) {
    var initials = getInitials(s.name || s.username || '??');
    var color = getAvatarColor(s.id);

    var nameCell = document.createElement('div');
    nameCell.className = 'nm';
    var av = document.createElement('span');
    av.className = 'avatar';
    av.style.background = color;
    av.textContent = initials;
    var nameText = document.createTextNode(s.name || s.username || '');
    nameCell.appendChild(av);
    nameCell.appendChild(nameText);
    grid.appendChild(nameCell);

    for (var i = 0; i < 14; i++) {
      var dow = i % 7;
      var cell = document.createElement('div');
      cell.className = 'cell';
      var sch = schMap[s.id] && schMap[s.id][dow];
      if (sch) {
        var block = document.createElement('div');
        block.className = 'block ' + getShiftClass(sch.start_time, sch.end_time);
        block.textContent = getShiftLabel(sch.start_time, sch.end_time);
        cell.appendChild(block);
      } else {
        cell.classList.add('off');
      }
      grid.appendChild(cell);
    }
  });

  // "+N more" row
  var remaining = _staff.length - shown.length;
  if (remaining > 0) {
    var moreCell = document.createElement('div');
    moreCell.className = 'nm';
    var moreAv = document.createElement('span');
    moreAv.className = 'avatar';
    moreAv.textContent = '+' + remaining;
    moreCell.appendChild(moreAv);
    moreCell.appendChild(document.createTextNode('Mostrar todos'));
    grid.appendChild(moreCell);
    var spanCell = document.createElement('div');
    spanCell.className = 'cell off';
    spanCell.style.gridColumn = 'span 14';
    grid.appendChild(spanCell);
  }
}

function getShiftClass(start, end) {
  var s = parseTime(start || '00:00');
  if (s < 12 * 60) return 'am';
  if (s < 18 * 60) return 'pm';
  return 'closing';
}

function getShiftLabel(start, end) {
  var s = parseTime(start || '00:00');
  if (s < 12 * 60) return 'AM';
  if (s < 18 * 60) return 'PM';
  return 'CIE';
}

function getInitials(name) {
  var parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

var AVATAR_COLORS = [
  'linear-gradient(135deg,#1D9E75,#0F6E56)',
  'linear-gradient(135deg,#A78BFA,#7C3AED)',
  'linear-gradient(135deg,#60A5FA,#3B82F6)',
  'linear-gradient(135deg,#F87171,#DC2626)',
  'linear-gradient(135deg,#34D399,#10B981)',
  'linear-gradient(135deg,#FB923C,#EA580C)',
  'linear-gradient(135deg,#F59E0B,#D97706)',
  'linear-gradient(135deg,#9CA3AF,#6B7280)'
];
function getAvatarColor(id) {
  return AVATAR_COLORS[id % AVATAR_COLORS.length];
}

// ── Tip distribution ──────────────────────────────────────────────
function renderTips() {
  var container = document.getElementById('tipsContainer');
  if (!container) return;

  if (!_tipsPool || !_tipsPool.entries) {
    container.innerHTML = '<div style="font-size:13px;color:var(--text-4);padding:16px 0;">Sin datos de propinas para el período actual.</div>';
    return;
  }

  var entries = _tipsPool.entries.slice(0, 5);
  var poolTotal = _tipsPool.pool_total || 1;

  container.innerHTML = '';
  entries.forEach(function (e) {
    var pct = Math.round((e.amount / poolTotal) * 100);
    var barWidth = Math.min(100, pct + 10);

    var row = document.createElement('div');
    row.className = 'tip-row';

    var avRow = document.createElement('div');
    avRow.className = 'av-row';
    var av = document.createElement('div');
    av.className = 'avatar';
    av.style.background = getAvatarColor(e.staff_id || 0);
    av.textContent = getInitials(e.name || '??');
    var meta = document.createElement('div');
    meta.className = 'av-meta';
    var avName = document.createElement('div');
    avName.className = 'av-name';
    avName.textContent = e.name || '';
    var avRole = document.createElement('div');
    avRole.className = 'av-role';
    avRole.textContent = (e.role || '') + (e.hours ? ' · ' + e.hours + 'h' : '');
    meta.appendChild(avName);
    meta.appendChild(avRole);
    avRow.appendChild(av);
    avRow.appendChild(meta);

    var bar = document.createElement('div');
    bar.className = 'tip-bar';
    var fill = document.createElement('div');
    fill.className = 'tip-bar-fill';
    fill.style.width = barWidth + '%';
    bar.appendChild(fill);

    var amtEl = document.createElement('div');
    amtEl.className = 'mono';
    amtEl.style.fontWeight = '600';
    amtEl.textContent = mesioFmt(e.amount);

    var pctBadge = document.createElement('div');
    pctBadge.className = 'badge' + (pct >= 20 ? ' success' : '');
    pctBadge.textContent = pct + '%';

    row.appendChild(avRow);
    row.appendChild(bar);
    row.appendChild(amtEl);
    row.appendChild(pctBadge);
    container.appendChild(row);
  });

  // AI suggestion stub
  var suggestion = document.getElementById('aiSuggestion');
  if (suggestion && _tipsPool.ai_note) {
    suggestion.style.display = '';
    var noteEl = suggestion.querySelector('.ai-note');
    if (noteEl) { noteEl.textContent = _tipsPool.ai_note; }
  }
}

// ── Members table ─────────────────────────────────────────────────
function renderMembersTable() {
  var tbody = document.getElementById('membersTbody');
  if (!tbody) return;
  tbody.innerHTML = '';

  var filtered = _staff.filter(function (s) {
    if (_activeRoleFilter === 'all') return true;
    return (s.role || '').toLowerCase() === _activeRoleFilter;
  });

  if (!filtered.length) {
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = 8;
    td.style.cssText = 'text-align:center;color:var(--text-4);padding:24px;';
    td.textContent = 'Sin miembros en este filtro';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  filtered.forEach(function (s) {
    var tr = document.createElement('tr');

    // Member
    var tdMember = document.createElement('td');
    var avRow = document.createElement('div');
    avRow.className = 'av-row';
    var av = document.createElement('div');
    av.className = 'avatar';
    av.style.background = getAvatarColor(s.id);
    av.textContent = getInitials(s.name || s.username || '??');
    var meta = document.createElement('div');
    meta.className = 'av-meta';
    var avName = document.createElement('div');
    avName.className = 'av-name';
    avName.textContent = s.name || s.username || '';
    var avRole = document.createElement('div');
    avRole.className = 'av-role';
    avRole.textContent = s.username ? s.username + '@' : (s.email || '');
    meta.appendChild(avName);
    meta.appendChild(avRole);
    avRow.appendChild(av);
    avRow.appendChild(meta);
    tdMember.appendChild(avRow);
    tr.appendChild(tdMember);

    // Role
    var tdRole = document.createElement('td');
    tdRole.textContent = s.role || '—';
    tr.appendChild(tdRole);

    // Status
    var tdStatus = document.createElement('td');
    var badge = document.createElement('span');
    if (s.clocked_in || s.active_shift) {
      badge.className = 'badge success';
      var dot = document.createElement('span');
      dot.className = 'dot live';
      badge.appendChild(dot);
      badge.appendChild(document.createTextNode(' En turno'));
    } else if (s.status === 'inactive') {
      badge.className = 'badge';
      badge.textContent = 'Inactivo';
    } else {
      badge.className = 'badge';
      badge.textContent = 'Libre';
    }
    tdStatus.appendChild(badge);
    tr.appendChild(tdStatus);

    // This week hours
    var tdHours = document.createElement('td');
    tdHours.className = 'mono';
    var staffSch = _schedules.filter(function (sch) { return sch.staff_id === s.id; });
    var totalMins = staffSch.reduce(function (acc, sch) {
      var start = parseTime(sch.start_time || '00:00');
      var end = parseTime(sch.end_time || '00:00');
      var mins = end - start;
      if (mins < 0) mins += 24 * 60;
      return acc + mins;
    }, 0);
    tdHours.textContent = Math.round(totalMins / 60) + ' h';
    tr.appendChild(tdHours);

    // Sales / tips
    var tdSales = document.createElement('td');
    tdSales.className = 'mono';
    tdSales.textContent = '—';
    tr.appendChild(tdSales);

    // Clock-in method
    var tdClock = document.createElement('td');
    var clockBadge = document.createElement('span');
    clockBadge.className = 'badge';
    clockBadge.textContent = s.has_biometric ? '🔑 Huella' : '🔑 PIN';
    tdClock.appendChild(clockBadge);
    tr.appendChild(tdClock);

    // Sparkline (static 7 bars for now)
    var tdSpark = document.createElement('td');
    var spark = document.createElement('div');
    spark.className = 'mini-spark';
    var heights = [60, 70, 65, 80, 72, 85, 75];
    heights.forEach(function (h) {
      var bar = document.createElement('span');
      bar.style.height = h + '%';
      spark.appendChild(bar);
    });
    tdSpark.appendChild(spark);
    tr.appendChild(tdSpark);

    // Actions
    var tdActions = document.createElement('td');
    var menuBtn = document.createElement('button');
    menuBtn.className = 'icon-btn';
    menuBtn.setAttribute('aria-label', 'Opciones de ' + (s.name || ''));
    menuBtn.textContent = '⋯';
    menuBtn.dataset.staffId = s.id;
    menuBtn.addEventListener('click', function () {
      mesioToast('Opciones de miembro — próximamente', 'info');
    });
    tdActions.appendChild(menuBtn);
    tr.appendChild(tdActions);

    tbody.appendChild(tr);
  });

  var countEl = document.getElementById('memberCount');
  if (countEl) { countEl.textContent = _staff.length + ' total · filtrado por: ' + (_activeRoleFilter === 'all' ? 'todos los roles' : _activeRoleFilter); }
}

// ── Invite modal ──────────────────────────────────────────────────
function openInviteModal() {
  var modal = document.getElementById('inviteModal');
  if (modal) { modal.classList.add('open'); }
}

function closeInviteModal() {
  var modal = document.getElementById('inviteModal');
  if (modal) { modal.classList.remove('open'); }
}

async function submitInvite() {
  var name = document.getElementById('inviteName');
  var role = document.getElementById('inviteRole');
  var doc = document.getElementById('inviteDoc');
  if (!name || !name.value.trim()) { mesioToast('Nombre requerido', 'warn'); return; }

  var payload = {
    name: name.value.trim(),
    role: role ? role.value : 'mesero',
    document_number: doc ? doc.value.trim() : ''
  };

  try {
    var res = await fetch('/api/staff', {
      method: 'POST',
      headers: mesioHeaders(),
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    mesioToast('Miembro invitado correctamente', 'success');
    closeInviteModal();
    loadAll();
  } catch (e) {
    mesioToast('Error: ' + e.message, 'error');
  }
}

// ── Role filter ───────────────────────────────────────────────────
function bindRoleFilter() {
  document.querySelectorAll('.seg-btn[data-role]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.seg-btn[data-role]').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      _activeRoleFilter = btn.dataset.role;
      renderMembersTable();
    });
  });
}

// ── Logout ────────────────────────────────────────────────────────
function bindLogout() {
  var btn = document.getElementById('logoutBtn');
  if (btn) { btn.addEventListener('click', mesioLogout); }
}

// ── Auto-refresh every 60s ────────────────────────────────────────
function startAutoRefresh() {
  clearInterval(_refreshTimer);
  _refreshTimer = setInterval(loadAll, 60000);
}

// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  // Invite modal
  var inviteBtn = document.getElementById('inviteBtn');
  var modalClose = document.getElementById('inviteModalClose');
  var modalCancel = document.getElementById('inviteModalCancel');
  var modalSubmit = document.getElementById('inviteModalSubmit');
  var modalOverlay = document.getElementById('inviteModal');

  if (inviteBtn) inviteBtn.addEventListener('click', openInviteModal);
  if (modalClose) modalClose.addEventListener('click', closeInviteModal);
  if (modalCancel) modalCancel.addEventListener('click', closeInviteModal);
  if (modalSubmit) modalSubmit.addEventListener('click', submitInvite);
  if (modalOverlay) {
    modalOverlay.addEventListener('click', function (e) {
      if (e.target === modalOverlay) closeInviteModal();
    });
  }

  bindRoleFilter();
  bindLogout();
  loadAll();
  startAutoRefresh();
});
