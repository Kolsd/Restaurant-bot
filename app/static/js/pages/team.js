/* ══ Team admin page — team management view ═══════════════════════
   Admin-only view: the roster — invite, edit role and sede.
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
var _refreshTimer = null;
var _activeRoleFilter = 'all';
var _locations = [];       // org's sedes — GET /api/staff/locations
var _multiSede = false;    // > 1 active sede — gates the sede selector (docs/claude/delivery-web.md chunk 8)
var _editingStaffId = null;

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
    var [staffData, locationsData] = await Promise.all([
      apiFetch('/api/staff'),
      apiFetch('/api/staff/locations').catch(function () { return null; })
    ]);

    _staff = Array.isArray(staffData) ? staffData : (staffData && staffData.staff ? staffData.staff : []);

    _locations = (locationsData && Array.isArray(locationsData.locations)) ? locationsData.locations : [];
    // multi_sede on the staff response is the authoritative source (server
    // knows every active sede, including ones this call to /locations might
    // have raced with); fall back to counting _locations if it's missing.
    _multiSede = (staffData && typeof staffData.multi_sede === 'boolean')
      ? staffData.multi_sede
      : (_locations.length > 1);
    populateLocationSelects();

    renderMembersTable();
    mesioTrackFetch(true);
  } catch (e) {
    mesioTrackFetch(false);
    console.warn('equipo load failed:', e);
    mesioToast('Error cargando datos del equipo', 'error');
  }
}

// ── Sede selectors (invite + edit modals) ────────────────────────
function populateLocationSelects() {
  var inviteField = document.getElementById('inviteLocationField');
  var inviteSelect = document.getElementById('inviteLocation');
  var editField = document.getElementById('editStaffLocationField');
  var editSelect = document.getElementById('editStaffLocation');

  [inviteSelect, editSelect].forEach(function (sel) {
    if (!sel) return;
    sel.innerHTML = '';
    _locations.forEach(function (loc) {
      var opt = document.createElement('option');
      opt.value = loc.id;
      opt.textContent = loc.name || ('Sede ' + loc.id);
      sel.appendChild(opt);
    });
  });

  // Single-sede orgs: no selector needed — never add friction there.
  if (inviteField) inviteField.style.display = _multiSede ? '' : 'none';
  if (editField) editField.style.display = _multiSede ? '' : 'none';
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

// ── Members table ─────────────────────────────────────────────────
function renderMembersTable() {
  var tbody = document.getElementById('membersTbody');
  if (!tbody) return;
  tbody.innerHTML = '';

  var subEl = document.getElementById('teamSub');
  if (subEl) { subEl.textContent = _staff.length === 1 ? '1 persona' : _staff.length + ' personas'; }
  var countEl = document.getElementById('memberCount');
  if (countEl) { countEl.textContent = _staff.length + ' total · filtrado por: ' + (_activeRoleFilter === 'all' ? 'todos los roles' : _activeRoleFilter); }

  var filtered = _staff.filter(function (s) {
    if (_activeRoleFilter === 'all') return true;
    return (s.role || '').toLowerCase() === _activeRoleFilter;
  });

  if (!filtered.length) {
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = 4;
    td.style.cssText = 'text-align:center;color:var(--text-4);padding:24px;';
    td.textContent = _staff.length
      ? 'Sin miembros en este filtro'
      : 'Aún no hay nadie en tu equipo. Invita a tu primer mesero, cajero o cocinero.';
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
    // The username is what they type at the staff login — not an email.
    avRole.textContent = s.username ? 'Usuario: ' + s.username : (s.email || '');
    meta.appendChild(avName);
    meta.appendChild(avRole);
    avRow.appendChild(av);
    avRow.appendChild(meta);
    tdMember.appendChild(avRow);
    tr.appendChild(tdMember);

    // Role
    var tdRole = document.createElement('td');
    tdRole.textContent = s.role || '—';
    // Multi-sede orgs: make an unassigned staff member obvious so the owner
    // can fix it (docs/claude/delivery-web.md chunk 8) — such staff are
    // refused by the Domicilios section until given a sede.
    if (_multiSede) {
      var sedeBadge = document.createElement('div');
      if (s.location_id) {
        sedeBadge.className = 'badge';
        sedeBadge.style.cssText = 'margin-top:4px;font-weight:400;';
        sedeBadge.textContent = s.location_name || ('Sede #' + s.location_id);
      } else {
        sedeBadge.className = 'badge';
        sedeBadge.style.cssText = 'margin-top:4px;font-weight:400;color:#B45309;background:rgba(245,158,11,.15);';
        sedeBadge.textContent = 'Sin sede';
      }
      tdRole.appendChild(sedeBadge);
    }
    tr.appendChild(tdRole);

    // Status
    var tdStatus = document.createElement('td');
    var badge = document.createElement('span');
    if (s.active === false || s.status === 'inactive') {
      badge.className = 'badge';
      badge.textContent = 'Inactivo';
    } else {
      badge.className = 'badge success';
      badge.textContent = 'Activo';
    }
    tdStatus.appendChild(badge);
    tr.appendChild(tdStatus);

    // Actions
    var tdActions = document.createElement('td');
    var menuBtn = document.createElement('button');
    menuBtn.className = 'icon-btn';
    menuBtn.setAttribute('aria-label', 'Opciones de ' + (s.name || ''));
    menuBtn.textContent = '⋯';
    menuBtn.dataset.staffId = s.id;
    menuBtn.addEventListener('click', function () {
      openEditStaffModal(s);
    });
    tdActions.appendChild(menuBtn);
    tr.appendChild(tdActions);

    tbody.appendChild(tr);
  });

}

// ── Invite modal ──────────────────────────────────────────────────
function openInviteModal() {
  var modal = document.getElementById('inviteModal');
  if (modal) { modal.classList.add('open'); }
}

function closeInviteModal() {
  var modal = document.getElementById('inviteModal');
  if (modal) { modal.classList.remove('open'); }
  var pw = document.getElementById('invitePassword');
  if (pw) { pw.value = ''; }
}

async function submitInvite() {
  var name = document.getElementById('inviteName');
  var role = document.getElementById('inviteRole');
  var doc = document.getElementById('inviteDoc');
  var password = document.getElementById('invitePassword');
  var locationSel = document.getElementById('inviteLocation');
  if (!name || !name.value.trim()) { mesioToast('Nombre requerido', 'warn'); return; }
  if (!password || password.value.length < 4) { mesioToast('Contraseña de al menos 4 caracteres requerida', 'warn'); return; }

  var payload = {
    name: name.value.trim(),
    role: role ? role.value : 'mesero',
    document_number: doc ? doc.value.trim() : '',
    password: password.value
  };
  // Sede — only sent for multi-sede orgs; a single-sede org auto-assigns
  // its only sede server-side (_resolve_new_staff_location).
  if (_multiSede && locationSel && locationSel.value) {
    payload.location_id = parseInt(locationSel.value, 10);
  }

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

// ── Edit member modal ────────────────────────────────────────────
function openEditStaffModal(staff) {
  _editingStaffId = staff.id;
  var nameEl = document.getElementById('editStaffName');
  var roleEl = document.getElementById('editStaffRole');
  var activeEl = document.getElementById('editStaffActive');
  var locationEl = document.getElementById('editStaffLocation');

  if (nameEl) nameEl.value = staff.name || staff.username || '';
  if (roleEl) roleEl.value = staff.role || 'mesero';
  if (activeEl) activeEl.checked = staff.status !== 'inactive' && staff.active !== false;
  if (locationEl && staff.location_id) { locationEl.value = String(staff.location_id); }
  var pwdEl = document.getElementById('editStaffPassword');
  if (pwdEl) pwdEl.value = '';
  _editingStaffName = staff.name || staff.username || '';

  var modal = document.getElementById('editStaffModal');
  if (modal) { modal.classList.add('open'); }
}

function closeEditStaffModal() {
  var modal = document.getElementById('editStaffModal');
  if (modal) { modal.classList.remove('open'); }
  _editingStaffId = null;
}

async function submitEditStaff() {
  if (!_editingStaffId) return;
  var nameEl = document.getElementById('editStaffName');
  var roleEl = document.getElementById('editStaffRole');
  var activeEl = document.getElementById('editStaffActive');
  var locationEl = document.getElementById('editStaffLocation');

  var pwdEl = document.getElementById('editStaffPassword');
  var newPwd = pwdEl ? pwdEl.value.trim() : '';
  if (newPwd && newPwd.length < 4) {
    mesioToast('El PIN debe tener al menos 4 caracteres', 'warning');
    return;
  }
  var payload = {
    name: nameEl ? nameEl.value.trim() : undefined,
    role: roleEl ? roleEl.value : undefined,
    active: activeEl ? activeEl.checked : undefined,
    password: newPwd || undefined
  };
  if (_multiSede && locationEl && locationEl.value) {
    payload.location_id = parseInt(locationEl.value, 10);
  }
  // Strip undefined keys — PUT /api/staff/{id} treats an omitted field as
  // "leave unchanged" (StaffUpdate.model_dump(exclude_none=True)).
  Object.keys(payload).forEach(function (k) { if (payload[k] === undefined) delete payload[k]; });

  try {
    var res = await fetch('/api/staff/' + encodeURIComponent(_editingStaffId), {
      method: 'PUT',
      headers: mesioHeaders(),
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    mesioToast('Miembro actualizado', 'success');
    closeEditStaffModal();
    loadAll();
  } catch (e) {
    mesioToast('Error: ' + e.message, 'error');
  }
}

var _editingStaffName = '';

async function deleteEditingStaff() {
  if (!_editingStaffId) return;
  var ok = typeof mesioConfirm === 'function'
    ? await mesioConfirm('¿Eliminar a ' + (_editingStaffName || 'este miembro') + ' del equipo? Ya no podrá entrar.', { confirmText: 'Eliminar', danger: true })
    : window.confirm('¿Eliminar a ' + (_editingStaffName || 'este miembro') + ' del equipo?');
  if (!ok) return;
  try {
    var res = await fetch('/api/staff/' + encodeURIComponent(_editingStaffId), {
      method: 'DELETE',
      headers: mesioHeaders()
    });
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    mesioToast('Miembro eliminado', 'success');
    closeEditStaffModal();
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

  // Edit member modal
  var editClose = document.getElementById('editStaffModalClose');
  var editDelete = document.getElementById('editStaffModalDelete');
  if (editDelete) editDelete.addEventListener('click', deleteEditingStaff);
  var editCancel = document.getElementById('editStaffModalCancel');
  var editSubmit = document.getElementById('editStaffModalSubmit');
  var editOverlay = document.getElementById('editStaffModal');

  if (editClose) editClose.addEventListener('click', closeEditStaffModal);
  if (editCancel) editCancel.addEventListener('click', closeEditStaffModal);
  if (editSubmit) editSubmit.addEventListener('click', submitEditStaff);
  if (editOverlay) {
    editOverlay.addEventListener('click', function (e) {
      if (e.target === editOverlay) closeEditStaffModal();
    });
  }

  bindRoleFilter();
  bindLogout();
  loadAll();
  startAutoRefresh();
});
