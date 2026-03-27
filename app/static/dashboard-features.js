/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Features
   app/static/dashboard-features.js
═══════════════════════════════════════════════════ */

// ── MENÚ ─────────────────────────────────────────────────────────────
let menuAvailability = {};
let MENU_ITEMS = [];
const CAT_ICONS = { 'Entradas':'🥗','Pastas':'🍝','Pizzas':'🍕','Postres':'🍮','Bebidas':'🥤','Extras':'🫙','default':'🍽️' };

async function loadMenu() {
  const h = window._dashHeaders;
  try {
    const [rMenu, rAvail] = await Promise.all([
      fetch('/api/dashboard/menu', { headers: h }),
      fetch('/api/menu/availability', { headers: h })
    ]);
    if (rAvail.ok) menuAvailability = (await rAvail.json()).availability || {};
    if (rMenu.ok) {
      const menu = (await rMenu.json()).menu || {};
      MENU_ITEMS = [];
      Object.entries(menu).forEach(([cat, dishes]) => {
        if (Array.isArray(dishes)) dishes.forEach(d => MENU_ITEMS.push({ name:d.name||'', cat, price:d.price?'$'+d.price:'$0' }));
      });
    }
  } catch(e) { console.error('loadMenu:', e); }
  renderMenu();
}

function renderMenu() {
  const grid = document.getElementById('menu-grid');
  if (!grid) return;
  if (!MENU_ITEMS.length) {
    grid.innerHTML = '<div style="padding:2rem;text-align:center;color:#aaa;font-size:13px;">Sin platos en el menú.</div>';
    return;
  }
  const cats = [...new Set(MENU_ITEMS.map(m => m.cat))];
  grid.innerHTML = cats.map((cat, ci) => {
    const items = MENU_ITEMS.filter(m => m.cat === cat);
    const avail = items.filter(m => menuAvailability[m.name] !== false).length;
    const icon  = CAT_ICONS[cat] || CAT_ICONS['default'];
    const isOpen = ci === 0;
    return `<div class="menu-category">
      <div class="menu-cat-header" onclick="toggleCat(this)">
        <div class="menu-cat-title"><span>${icon}</span><span>${cat}</span><span class="menu-cat-meta">${avail}/${items.length} disponibles</span></div>
        <span class="menu-cat-arrow ${isOpen?'open':''}">▼</span>
      </div>
      <div class="menu-cat-body ${isOpen?'open':''}">
        ${items.map(m => {
          const av = menuAvailability[m.name] !== false;
          const safe = m.name.replace(/'/g,"\\'");
          return `<div class="menu-row" style="${av?'':'opacity:.55;'}">
            <div style="flex:1;min-width:0;"><div class="menu-row-name" style="${av?'':'text-decoration:line-through;color:#bbb;'}">${m.name}</div></div>
            <div class="menu-row-price">${m.price}</div>
            <div class="menu-row-status ${av?'status-on':'status-off'}">${av?'Disponible':'No disponible'}</div>
            <label class="toggle-switch"><input type="checkbox" ${av?'checked':''} onchange="toggleDish('${safe}',this.checked)"><span class="toggle-slider"></span></label>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }).join('');
}

function toggleCat(header) {
  header.nextElementSibling.classList.toggle('open');
  header.querySelector('.menu-cat-arrow').classList.toggle('open');
}

async function toggleDish(name, available) {
  const h = window._dashHeaders;
  try {
    await fetch('/api/menu/availability', {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ dish_name: name, available })
    });
    menuAvailability[name] = available;
    renderMenu();
  } catch(e) {}
}

// ── MESAS & QR ────────────────────────────────────────────────────────
async function loadTables() {
  const h = window._dashHeaders;
  const rest = window._dashRestaurant;
  const grid = document.getElementById('tables-grid');
  if (!grid) return;
  try {
    const r = await fetch('/api/tables', { headers: h });
    if (!r.ok) return;
    const { tables } = await r.json();
    if (!tables.length) {
      grid.innerHTML = '<div style="text-align:center;padding:2rem;color:#aaa;font-size:13px;grid-column:1/-1;">No hay mesas configuradas.</div>';
      return;
    }
    grid.innerHTML = tables.map(t => `
      <div style="background:#fff;border:0.5px solid #e0e0d8;border-radius:12px;padding:1.25rem;text-align:center;">
        <div style="font-size:28px;margin-bottom:6px;">🪑</div>
        <div style="font-size:15px;font-weight:600;margin-bottom:2px;">${t.name}</div>
        <div style="font-size:11px;color:#888;margin-bottom:12px;">ID: ${t.id}</div>
        <div id="qr-${t.id}" style="width:120px;height:120px;margin:0 auto 10px;"></div>
        <div style="display:flex;gap:6px;justify-content:center;flex-wrap:wrap;">
          <a href="/api/tables/${t.id}/qr-sheet" target="_blank" style="font-size:11px;padding:5px 10px;background:#E1F5EE;color:#0F6E56;border-radius:6px;text-decoration:none;font-weight:500;">🖨️ Imprimir QR</a>
          <button onclick="deleteTable('${t.id}')" style="font-size:11px;padding:5px 10px;background:#FDE8E8;color:#C0392B;border:none;border-radius:6px;cursor:pointer;">Eliminar</button>
        </div>
      </div>`).join('');

      if (typeof QRCode !== 'undefined') {
        tables.forEach(t => {
          const el = document.getElementById('qr-' + t.id);
          if (el && !el.hasChildNodes()) {
            const botNum = (rest && rest.whatsapp_number) ? rest.whatsapp_number : null;
          
          if (!botNum) {
              console.error("No se encontró el número de WhatsApp para este restaurante.");
              return; // Detiene la generación del QR para esta mesa y evita enlaces rotos
          }

          const catalogUrl = window.location.origin + '/catalog?bot=' + botNum + '&mesa=' + encodeURIComponent(t.name) + '&table_id=' + encodeURIComponent(t.id);
            try { 
              new QRCode(el, { 
                text: catalogUrl, 
                width: 120, 
                height: 120, 
                colorDark: '#0D1412', 
                colorLight: '#ffffff', 
                correctLevel: QRCode.CorrectLevel.M 
              }); 
            } catch(e) {
              console.error("Error generando QR:", e);
            }
          }
        });
      } 
  } catch(e) { console.error('loadTables:', e); }
}

async function createTable() {
  const h = window._dashHeaders;
  const num  = parseInt(document.getElementById('new-table-num').value);
  const name = document.getElementById('new-table-name').value.trim();
  if (!num || num < 1) { alert('Ingresa un número de mesa válido'); return; }
  try {
    const r = await fetch('/api/tables', {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ number: num, name: name || 'Mesa ' + num })
    });
    if (r.ok) {
      document.getElementById('new-table-num').value = '';
      document.getElementById('new-table-name').value = '';
      loadTables();
    } else {
      const err = await r.json().catch(() => ({}));
      alert('Error: ' + (err.detail || r.status));
    }
  } catch(e) { alert('Error de conexión'); }
}

async function deleteTable(tableId) {
  if (!confirm('¿Eliminar esta mesa?')) return;
  try {
    await fetch('/api/tables/' + tableId, { method: 'DELETE', headers: window._dashHeaders });
    loadTables();
  } catch(e) {}
}

// ── MI EQUIPO ─────────────────────────────────────────────────────────
let allBranches = [];
let currentBranchId = null;
let selectedRoles = new Set(['mesero']); // Set para guardar los multiroles

async function loadBranches() {
  const h = window._dashHeaders;
  const rest = window._dashRestaurant;
  const role = (rest && rest.role) || 'owner';
  const btnCreate = document.getElementById('btn-create-branch');
  if (btnCreate) btnCreate.style.display = role.includes('owner') ? '' : 'none';
  try {
    const r = await fetch('/api/team/branches', { headers: h });
    if (r.status === 401) { logout(); return; }
    const d = await r.json();
    allBranches = d.branches || [];
    const countEl = document.getElementById('branch-count');
    if (countEl) countEl.textContent = allBranches.length + ' sucursal(es)';
    renderBranches(allBranches);
  } catch(e) { console.error('loadBranches:', e); }
}

function filterBranches() {
  const q = document.getElementById('branch-search').value.toLowerCase();
  renderBranches(allBranches.filter(b => b.name.toLowerCase().includes(q) || (b.whatsapp_number||'').includes(q)));
}

function renderBranches(branches) {
  const container = document.getElementById('branches-list');
  if (!container) return;
  if (!branches.length) { container.innerHTML = '<div class="empty-state">No hay sucursales.</div>'; return; }
  
  container.innerHTML = branches.map(b => `
    <div style="background:#fff;border:0.5px solid #e0e0d8;border-radius:12px;margin-bottom:12px;overflow:hidden;">
      <div data-branch-id="${b.id}" style="display:flex;align-items:center;justify-content:space-between;padding:1rem 1.25rem;border-bottom:0.5px solid #f0f0e8;flex-wrap:wrap;gap:8px;">
        <div>
          <div style="font-size:15px;font-weight:600;">${b.name}</div>
          <div style="font-size:11px;color:#888;margin-top:2px;"><span style="background:#E1F5EE;color:#0F6E56;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:500;margin-right:6px;">WA: +${b.whatsapp_number||'N/A'}</span>${b.address||''}</div>
        </div>
        <button onclick="openInviteModal(${b.id},'${b.name.replace(/'/g,"\\'")}')" style="background:#E1F5EE;color:#0F6E56;border:none;padding:7px 14px;border-radius:8px;font-size:12px;cursor:pointer;font-weight:500;">+ Agregar miembro</button>
      </div>
      <div id="users-branch-${b.id}" style="padding:.75rem 1.25rem;"><div style="font-size:11px;color:#aaa;">Cargando...</div></div>
    </div>`).join('');

  branches.forEach(b => loadBranchUsers(b.id));

  const rest = window._dashRestaurant;
  const role = (rest && rest.role) || 'owner';
  if (role.includes('owner')) {
    branches.forEach(b => {
      const header = document.querySelector('[data-branch-id="' + b.id + '"]');
      if (header) {
        const btn = document.createElement('button');
        btn.textContent = 'Eliminar';
        btn.style.cssText = 'background:#FDE8E8;color:#C0392B;border:none;padding:7px 12px;border-radius:8px;font-size:12px;cursor:pointer;';
        btn.onclick = () => deleteBranch(b.id, b.name);
        header.appendChild(btn);
      }
    });
  }
}

// Función de renderizado para multi-rol (soporta roles de users y staff)
function formatRoles(roleStr) {
  if (!roleStr) return '';
  const roleColors = {
    owner:'#1D9E75', admin:'#185FA5',
    cashier:'#BA7517', cook:'#854F0B', waiter:'#534AB7',
    // staff roles
    mesero:'#534AB7', caja:'#BA7517', cocina:'#854F0B',
    domiciliario:'#0F6E56', gerente:'#185FA5', bar:'#7B3FA0', otro:'#888',
  };
  const roleBg = {
    owner:'#E1F5EE', admin:'#E6F1FB',
    cashier:'#FFF8E6', cook:'#FAEEDA', waiter:'#EEEDFE',
    mesero:'#EEEDFE', caja:'#FFF8E6', cocina:'#FAEEDA',
    domiciliario:'#E1F5EE', gerente:'#E6F1FB', bar:'#F3E8FF', otro:'#f0f0e8',
  };
  const roleLabels = {
    owner:'Dueño', admin:'Admin',
    cashier:'Cajero', cook:'Cocinero', waiter:'Mesero',
    mesero:'Mesero', caja:'Cajero', cocina:'Cocinero',
    domiciliario:'Domiciliario', gerente:'Gerente', bar:'Bar', otro:'Otro',
  };

  return roleStr.split(',').map(r => r.trim()).filter(Boolean).map(r => {
    const c = roleColors[r] || '#555';
    const b = roleBg[r] || '#f0f0e8';
    const l = roleLabels[r] || r;
    return `<span style="background:${b}; color:${c}; padding:3px 8px; border-radius:6px; font-size:10px; font-weight:600; margin-right:4px; display:inline-block; margin-top:4px;">${l}</span>`;
  }).join('');
}

async function loadBranchUsers(branchId) {
  const h = window._dashHeaders;
  try {
    const r = await fetch('/api/team/users?branch_id=' + branchId, { headers: h });
    if (!r.ok) return;
    const users = ((await r.json()).users || []).filter(u => u.branch_id == branchId);
    const el = document.getElementById('users-branch-' + branchId);
    if (!el) return;
    if (!users.length) { el.innerHTML = '<div style="font-size:12px;color:#aaa;padding:4px 0;">Sin miembros asignados</div>'; return; }

    el.innerHTML = '<div style="display:flex;flex-wrap:wrap;gap:12px;">' +
      users.map(u => {
        const displayName = u.display_name || u.username || '?';
        const roleLabel   = u.role === 'gerente' ? '👔 Gerente' : '🛡️ Administrador';
        return `
        <div style="display:flex;align-items:center;gap:12px;background:#f8f8f5;border-radius:8px;padding:8px 12px;width:100%;max-width:340px;justify-content:space-between;border:1px solid #f0f0e8;">
          <div style="display:flex;align-items:center;gap:10px;">
            <div style="width:34px;height:34px;border-radius:50%;background:#e0e0d8;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600;color:#555;">${displayName[0].toUpperCase()}</div>
            <div>
              <div style="font-size:13px;font-weight:600;color:#333;">${displayName}</div>
              <div style="font-size:11px;color:#888;margin-top:2px;">${roleLabel}</div>
            </div>
          </div>
          <button onclick="deleteUser('${u.username}')" style="background:#FDE8E8;border:none;color:#C0392B;border-radius:6px;font-size:16px;cursor:pointer;width:28px;height:28px;display:flex;align-items:center;justify-content:center;">×</button>
        </div>`;
      }).join('') + '</div>';
  } catch(e) {}
}

function showCreateBranch() {
  document.getElementById('create-branch-form').style.display = 'block';
  document.getElementById('branch-name').focus();
}

async function validateAddress() {
  const h = window._dashHeaders;
  const address = document.getElementById('branch-address').value.trim();
  if (!address) { alert('Ingresa una dirección primero'); return; }
  const btn = document.querySelector('[onclick="validateAddress()"]');
  const prev = btn.textContent;
  btn.textContent = '...'; btn.disabled = true;
  document.getElementById('branch-map-preview').style.display = 'none';
  document.getElementById('branch-map-error').style.display = 'none';
  try {
    const r = await fetch('/api/geocode?address=' + encodeURIComponent(address), { headers: h });
    if (r.ok) {
      const d = await r.json();
      document.getElementById('branch-lat').value = d.latitude;
      document.getElementById('branch-lon').value = d.longitude;
      document.getElementById('branch-address-display').textContent = d.display_name;
      document.getElementById('branch-lat-display').textContent = d.latitude.toFixed(6);
      document.getElementById('branch-lon-display').textContent = d.longitude.toFixed(6);
      document.getElementById('branch-maps-link').href = d.maps_url;
      document.getElementById('branch-map-preview').style.display = 'block';
    } else {
      const e = await r.json();
      document.getElementById('branch-error-text').textContent = '❌ ' + (e.detail || 'No se encontró la dirección.');
      document.getElementById('branch-map-error').style.display = 'block';
      document.getElementById('branch-lat').value = '';
      document.getElementById('branch-lon').value = '';
    }
  } catch(e) {
    document.getElementById('branch-error-text').textContent = '❌ Error de conexión.';
    document.getElementById('branch-map-error').style.display = 'block';
  } finally { btn.textContent = prev; btn.disabled = false; }
}

function applyManualCoords() {
  const lat = document.getElementById('branch-lat-manual').value;
  const lon = document.getElementById('branch-lon-manual').value;
  if (lat && lon) {
    document.getElementById('branch-lat').value = lat;
    document.getElementById('branch-lon').value = lon;
    document.getElementById('branch-lat-display').textContent = parseFloat(lat).toFixed(6);
    document.getElementById('branch-lon-display').textContent = parseFloat(lon).toFixed(6);
    document.getElementById('branch-maps-link').href = 'https://www.google.com/maps?q=' + lat + ',' + lon;
    document.getElementById('branch-address-display').textContent = 'Coordenadas manuales';
    document.getElementById('branch-map-preview').style.display = 'block';
  }
}

async function createBranch() {
  const h = window._dashHeaders;
  const name    = document.getElementById('branch-name').value.trim();
  const address = document.getElementById('branch-address').value.trim();
  const lat     = document.getElementById('branch-lat').value;
  const lon     = document.getElementById('branch-lon').value;
  if (!name)    { alert('El nombre es obligatorio'); return; }
  if (!address) { alert('Ingresa la dirección'); return; }
  try {
    const body = { name, whatsapp_number:'', address, menu:{} };
    if (lat && lon) { body.latitude = parseFloat(lat); body.longitude = parseFloat(lon); }
    const r = await fetch('/api/team/branches', { method:'POST', headers:{ ...h,'Content-Type':'application/json' }, body:JSON.stringify(body) });
    if (r.ok) {
      document.getElementById('create-branch-form').style.display = 'none';
      ['branch-name','branch-address','branch-lat','branch-lon'].forEach(id => { document.getElementById(id).value = ''; });
      document.getElementById('branch-map-preview').style.display = 'none';
      loadBranches();
    } else { const e = await r.json(); alert('Error: ' + (e.detail||'No se pudo crear')); }
  } catch(e) {}
}

// ── LÓGICA MULTIROL ──
function toggleRole(role, el) {
  const isAdminRole = role === 'admin' || role === 'gerente';
  const pwdField = document.getElementById('invite-password');
  const pinField = document.getElementById('invite-pin');

  if (isAdminRole) {
    // Admin exclusivo: limpia todos y selecciona solo admin
    selectedRoles = new Set(['admin']);
    document.querySelectorAll('#modal-invite .role-card').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
    // Admin requiere contraseña, no PIN
    if (pwdField) pwdField.style.display = '';
    if (pinField) pinField.style.display = 'none';
  } else {
    // Si admin estaba activo, se desmarca al elegir rol operativo
    if (selectedRoles.has('admin')) {
      selectedRoles.delete('admin');
      const adminCard = document.querySelector('#modal-invite .role-card[data-role="admin"]');
      if (adminCard) adminCard.classList.remove('active');
    }

    // Toggle para roles operativos (multi-rol)
    if (selectedRoles.has(role)) {
      if (selectedRoles.size === 1) return; // Al menos un rol siempre
      selectedRoles.delete(role);
      el.classList.remove('active');
    } else {
      selectedRoles.add(role);
      el.classList.add('active');
    }

    // Roles operativos usan PIN
    if (pwdField) pwdField.style.display = 'none';
    if (pinField) pinField.style.display = '';
  }

  document.getElementById('invite-role').value = Array.from(selectedRoles).join(',');
}

function openInviteModal(branchId, branchName) {
  currentBranchId = branchId;
  document.getElementById('modal-branch-name').textContent = branchName;
  document.getElementById('invite-username').value = '';
  document.getElementById('invite-password').value = '';
  const pinField   = document.getElementById('invite-pin');
  const phoneField = document.getElementById('invite-phone');
  if (pinField)   pinField.value   = '';
  if (phoneField) phoneField.value = '';

  // Mis Sucursales: default Admin, siempre contraseña (nunca PIN)
  selectedRoles = new Set(['admin']);
  document.getElementById('invite-role').value = 'admin';

  document.querySelectorAll('#modal-invite .role-card').forEach(c => {
    if (c.getAttribute('data-role') === 'admin') c.classList.add('active');
    else c.classList.remove('active');
  });

  const pwdField = document.getElementById('invite-password');
  if (pwdField) pwdField.style.display = '';
  if (pinField) pinField.style.display = 'none';

  document.getElementById('modal-invite').style.display = 'flex';
}

function closeInviteModal() {
  document.getElementById('modal-invite').style.display = 'none';
  currentBranchId = null;
}

async function sendInvite() {
  const h        = window._dashHeaders;
  const username = document.getElementById('invite-username').value.trim();
  const role     = document.getElementById('invite-role').value;
  const isAdmin  = role === 'admin' || role === 'gerente';
  const password = document.getElementById('invite-password').value.trim();
  const pin      = (document.getElementById('invite-pin') || {}).value?.trim() || '';
  const phone    = (document.getElementById('invite-phone') || {}).value?.trim() || '';

  if (!username) { alert('El nombre es obligatorio'); return; }
  if (isAdmin && !password) { alert('La contraseña es obligatoria para administrador'); return; }
  if (!isAdmin && pin.length < 4) { alert('El PIN debe tener al menos 4 dígitos'); return; }

  try {
    const r = await fetch('/api/team/invite', {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, pin, phone, role, branch_id: currentBranchId }),
    });
    if (r.ok) { closeInviteModal(); loadBranches(); alert('¡Miembro creado exitosamente!'); }
    else { const e = await r.json(); alert('Error: ' + (e.detail || 'No se pudo crear')); }
  } catch(e) {}
}

async function deleteBranch(id, name) {
  if (!confirm('Eliminar sucursal "' + name + '"?')) return;
  try {
    const r = await fetch('/api/team/branches/' + id, { method:'DELETE', headers: window._dashHeaders });
    if (r.ok) loadBranches();
    else { const e = await r.json(); alert('Error: ' + (e.detail||'No se pudo eliminar')); }
  } catch(e) {}
}

async function deleteUser(userId) {
  if (!confirm('Eliminar miembro "' + userId + '"?')) return;
  try {
    const r = await fetch('/api/team/users/' + encodeURIComponent(userId), { method:'DELETE', headers: window._dashHeaders });
    if (r.ok) loadBranches();
    else { const e = await r.json(); alert('Error: ' + (e.detail || 'No se pudo eliminar')); }
  } catch(e) {}
}

// ── SESIONES DE MESA ──────────────────────────────────────────────────
let _sesionHours  = 24;
let _currentSesId = null;

const CLOSE_REASON = {
  waiter_manual:      { text:'Mesero',      icon:'👤', color:'#BA7517', bg:'#FFF8E6' },
  inactivity_timeout: { text:'Inactividad', icon:'⏰', color:'#555',    bg:'#F5F5F0' },
  client_goodbye:     { text:'Cliente',     icon:'👋', color:'#0F6E56', bg:'#E1F5EE' },
  factura_entregada:  { text:'Factura OK',  icon:'🧾', color:'#6B21A8', bg:'#F0E6FF' },
  superseded:         { text:'Reemplazada', icon:'🔄', color:'#888',    bg:'#F5F5F0' },
};

function reasonBadge(r) {
  const d = CLOSE_REASON[r] || { text:r||'—', icon:'❓', color:'#888', bg:'#f0f0f0' };
  return `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:3px 8px;border-radius:20px;font-weight:500;background:${d.bg};color:${d.color};">${d.icon} ${d.text}</span>`;
}

function fmtDur(a, b) {
  if (!a||!b) return '—';
  // Añadimos la Z para asegurar que la matemática del tiempo sea exacta
  const zA = a.endsWith('Z') ? a : a + 'Z';
  const zB = b.endsWith('Z') ? b : b + 'Z';
  const m = Math.round((new Date(zB) - new Date(zA)) / 60000);
  return m < 60 ? m + 'min' : Math.floor(m/60) + 'h ' + (m%60) + 'min';
}

function fmtTime(iso) {
  if (!iso) return '—';
  const zIso = iso.endsWith('Z') ? iso : iso + 'Z';
  return new Date(zIso).toLocaleTimeString('es-CO', { hour:'2-digit', minute:'2-digit' });
}

function setSesionPeriod(h, btn) {
  _sesionHours = h;
  document.querySelectorAll('#sesiones .period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadSessions();
}

async function loadSessions() {
  const headers = window._dashHeaders;
  const c = document.getElementById('sessions-container');
  if (!c) return;
  c.innerHTML = '<div class="empty-state">Cargando...</div>';
  try {
    const r = await fetch('/api/table-sessions/closed?hours=' + _sesionHours, { headers });
    if (!r.ok) { c.innerHTML = '<div class="empty-state">Error.</div>'; return; }
    const { sessions = [] } = await r.json();

    const byWaiter = sessions.filter(s => s.closed_by === 'waiter_manual').length;
    document.getElementById('ses-total').textContent      = sessions.length;
    document.getElementById('ses-waiter').textContent     = byWaiter;
    document.getElementById('ses-client').textContent     = sessions.filter(s => s.closed_by === 'client_goodbye').length;
    document.getElementById('ses-inactivity').textContent = sessions.filter(s => s.closed_by === 'inactivity_timeout').length;
    document.getElementById('ses-badge').textContent      = sessions.length + ' sesiones';

    const banner = document.getElementById('ses-alert-banner');
    if (banner) banner.style.display = byWaiter > 0 ? 'flex' : 'none';

    if (!sessions.length) { c.innerHTML = '<div class="empty-state">Sin sesiones cerradas en este período.</div>'; return; }

    let html = `<table><thead><tr>
      <th>Mesa</th><th>Teléfono</th><th>Inicio</th><th>Cierre</th>
      <th>Duración</th><th>Cerrada por</th><th>Usuario</th><th>Total</th><th>Acciones</th>
    </tr></thead><tbody>`;
    sessions.forEach(s => {
      const warn = s.closed_by === 'waiter_manual';
      html += `<tr class="${warn ? 'ses-warn-row' : ''}">
        <td style="font-weight:500;">${s.table_name||'—'}</td>
        <td style="color:#888;font-size:11px;">${s.phone}</td>
        <td style="color:#888;">${fmtTime(s.started_at)}</td>
        <td style="color:#888;">${fmtTime(s.closed_at)}</td>
        <td>${fmtDur(s.started_at, s.closed_at)}</td>
        <td>${reasonBadge(s.closed_by)}</td>
        <td style="font-size:12px;${warn?'color:#BA7517;font-weight:500;':'color:#888;'}">${s.closed_by_username||'—'}${warn?' ⚠️':''}</td>
        <td style="font-weight:500;">${s.total_spent?'$'+Number(s.total_spent).toLocaleString('es-CO'):'—'}</td>
        <td>
          ${!s.total_spent
            ? `<button onclick="callWaiterAdmin('${s.bot_number||''}','${s.phone}','${(s.table_name||'').replace(/'/g,"\\'")}','${s.table_id||''}')"
                style="font-size:11px;padding:4px 9px;background:#FFF8E6;color:#BA7517;border:1px solid #FDE68A;border-radius:6px;cursor:pointer;font-weight:500;">
                📞 Llamar al Mesero
              </button>`
            : ''
          }
        </td>
      </tr>`;
    });
    html += '</tbody></table>';
    c.innerHTML = html;
  } catch(e) { console.error(e); c.innerHTML = '<div class="empty-state">Error de conexión.</div>'; }
}

async function viewSession(id, tableName, phone, closedBy) {
  const headers = window._dashHeaders;
  _currentSesId = id;
  document.getElementById('ses-modal-title').textContent = 'Sesión — ' + tableName;
  document.getElementById('ses-modal-sub').textContent   = phone;
  document.getElementById('ses-modal-msgs').innerHTML    = '<div style="text-align:center;font-size:12px;color:#888;padding:1rem;">Cargando...</div>';
  document.getElementById('ses-close-info').textContent  = '';
  document.getElementById('ses-action-feedback').style.display = 'none';
  document.getElementById('ses-msg-input').value         = '';
  document.getElementById('ses-waiter-msg-input').value  = '';
  const reopenRow = document.getElementById('ses-reopen-row');
  if (reopenRow) reopenRow.style.display = closedBy === 'waiter_manual' ? 'flex' : 'none';
  document.getElementById('ses-modal-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  try {
    const r = await fetch('/api/table-sessions/' + id + '/history', { headers });
    const d = await r.json();
    const session = d.session || {};
    const msgs    = d.history  || [];
    const reason  = CLOSE_REASON[session.closed_by] || { text:session.closed_by||'?', icon:'❓' };
    const infoEl  = document.getElementById('ses-close-info');
    infoEl.innerHTML = `Cerrada por: <strong>${reason.icon} ${reason.text}</strong>`
      + (session.closed_by_username ? ` · usuario: <strong>${session.closed_by_username}</strong>` : '')
      + ` · duración: ${fmtDur(session.started_at, session.closed_at)}`
      + (session.total_spent ? ` · total: <strong>$${Number(session.total_spent).toLocaleString('es-CO')}</strong>` : '');
    const chatEl = document.getElementById('ses-modal-msgs');
    if (!msgs.length) {
      chatEl.innerHTML = '<div style="text-align:center;font-size:12px;color:#888;padding:1rem;">Historial no disponible.</div>';
      return;
    }
    chatEl.innerHTML = msgs.map(m => {
      const isUser = m.role === 'user';
      const text   = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
      return `<div class="msg-bubble ${isUser?'user':''}"><div class="bubble ${isUser?'user':'bot'}">${text}</div></div>`;
    }).join('');
    chatEl.scrollTop = chatEl.scrollHeight;
  } catch(e) {
    document.getElementById('ses-modal-msgs').innerHTML = '<div style="text-align:center;font-size:12px;color:#888;padding:1rem;">Error al cargar.</div>';
  }
}

function closeSesModal() {
  document.getElementById('ses-modal-overlay').classList.remove('open');
  document.body.style.overflow = '';
  _currentSesId = null;
}

async function callWaiterAdmin(botNumber, phone, tableName, tableId) {
  const headers = window._dashHeaders;
  try {
    const r = await fetch('/api/waiter-alerts/admin-call', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ bot_number: botNumber, phone, table_name: tableName, table_id: tableId })
    });
    if (r.ok) {
      _showAdminCallToast('✅ Alerta enviada al mesero', true);
    } else {
      _showAdminCallToast('Error al enviar la alerta', false);
    }
  } catch(e) {
    _showAdminCallToast('Error de conexión', false);
  }
}

function _showAdminCallToast(msg, ok) {
  let el = document.getElementById('_admin-call-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = '_admin-call-toast';
    el.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:10px 20px;border-radius:20px;font-size:13px;font-weight:600;z-index:9999;transition:opacity .3s;pointer-events:none;';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.background = ok ? '#1D9E75' : '#E24B4A';
  el.style.color = '#fff';
  el.style.opacity = '1';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.opacity = '0'; }, 3000);
}

function showSesFeedback(msg, ok = true) {
  const el = document.getElementById('ses-action-feedback');
  el.textContent = msg;
  el.className = 'ses-feedback ' + (ok ? 'ok' : 'err');
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 4000);
}

async function reopenFromModal() {
  const headers = window._dashHeaders;
  if (!_currentSesId) return;
  if (!confirm('¿Reabrir esta sesión?')) return;
  try {
    const r = await fetch('/api/table-sessions/' + _currentSesId + '/reopen', { method:'POST', headers });
    if (r.ok) {
      showSesFeedback('✅ Sesión reabierta. El cliente puede volver a escribir.');
      document.getElementById('ses-reopen-row').style.display = 'none';
      loadSessions();
    } else { const e = await r.json(); showSesFeedback('Error: ' + (e.detail||'No se pudo reabrir.'), false); }
  } catch(e) { showSesFeedback('Error de conexión.', false); }
}

async function sendMsgFromModal() {
  const headers = window._dashHeaders;
  if (!_currentSesId) return;
  const msg = document.getElementById('ses-msg-input').value.trim();
  if (!msg) { showSesFeedback('Escribe un mensaje primero.', false); return; }
  try {
    const r = await fetch('/api/table-sessions/' + _currentSesId + '/send-message', {
      method:'POST', headers:{ ...headers,'Content-Type':'application/json' },
      body: JSON.stringify({ message: msg })
    });
    if (r.ok) { document.getElementById('ses-msg-input').value = ''; showSesFeedback('✅ Mensaje enviado al cliente.'); }
    else { const e = await r.json(); showSesFeedback('Error: ' + (e.detail||'No se pudo enviar.'), false); }
  } catch(e) { showSesFeedback('Error de conexión.', false); }
}

async function alertWaiterFromModal() {
  const headers = window._dashHeaders;
  if (!_currentSesId) return;
  const nota = document.getElementById('ses-waiter-msg-input').value.trim();
  try {
    const r = await fetch('/api/table-sessions/' + _currentSesId + '/alert-waiter', {
      method:'POST', headers:{ ...headers,'Content-Type':'application/json' },
      body: JSON.stringify({ message: nota })
    });
    if (r.ok) { document.getElementById('ses-waiter-msg-input').value = ''; showSesFeedback('✅ Alerta enviada al panel de meseros.'); }
    else { const e = await r.json(); showSesFeedback('Error: ' + (e.detail||'No se pudo alertar.'), false); }
  } catch(e) { showSesFeedback('Error de conexión.', false); }
}

// ── POS CON IA ───────────────────────────────────────────────────────
const posCache = { data: null, timestamp: 0, orderCount: 0 };
const CACHE_TTL = 8 * 60 * 60 * 1000;

async function loadPOSData(forceRefresh = false) {
  const headers = window._dashHeaders;
  const now = Date.now();
  const cacheValid = posCache.data && (now - posCache.timestamp) < CACHE_TTL;
  try {
    const r = await fetch('/api/dashboard/stats?period=today', { headers });
    if (r.ok) {
      const d = await r.json();
      const currentOrders = d.orders?.total || 0;
      if (cacheValid && !forceRefresh && currentOrders === posCache.orderCount) { renderPOSFromCache(); return; }
      posCache.orderCount = currentOrders;
    }
  } catch(e) {}
  posCache.timestamp = now;
  await loadPOSDataFresh();
}

function renderPOSFromCache() {
  if (!posCache.data) return;
  const { mainText, stockText, upsells, horaCounts, avgTicket, topPlato, topHora } = posCache.data;
  document.getElementById('ai-main-text').innerHTML = mainText + ' <span style="font-size:10px;color:#888;">(caché)</span>';
  document.getElementById('ai-stock-text').innerHTML = stockText;
  if (upsells) document.getElementById('upsell-container').innerHTML = upsells;
  if (topHora) document.getElementById('pos-hora-pico').textContent = topHora[0] + 'h';
  if (avgTicket) document.getElementById('pos-ticket').textContent = '$' + avgTicket.toLocaleString('es-CO');
  if (topPlato) { document.getElementById('pos-top-plato').textContent = topPlato[0]; document.getElementById('pos-top-sub').textContent = topPlato[1] + ' pedidos'; }
  renderHoraDist(horaCounts || {});
  renderDemandBars(horaCounts || {});
}

async function loadPOSDataFresh() {
  const headers = window._dashHeaders;
  const rest    = window._dashRestaurant;
  try {
    const r = await fetch('/api/dashboard/orders?period=week', { headers });
    if (r.status === 401) { logout(); return; }
    const orders = (await r.json()).orders || [];
    const paid = orders.filter(o => o.paid);
    const avgTicket = paid.length > 0 ? Math.round(paid.reduce((s,o) => s+o.total, 0) / paid.length) : 0;
    document.getElementById('pos-ticket').textContent = avgTicket > 0 ? '$' + avgTicket.toLocaleString('es-CO') : '—';
    document.getElementById('pos-ticket-trend').textContent = paid.length + ' pedidos pagados esta semana';

    const platoCounts = {};
    orders.forEach(o => {
      let items = [];
      if (!o.items) return;
      if (typeof o.items === 'string' && o.items.trim().startsWith('[')) {
        try { items = JSON.parse(o.items).map(i => (i.quantity||1)+'x '+(i.name||'')); } catch(e) { items = o.items.split(', '); }
      } else if (Array.isArray(o.items)) { items = o.items.map(i => (i.quantity||1)+'x '+(i.name||'')); }
      else { items = o.items.split(', '); }
      items.forEach(item => { const name = item.replace(/^\d+x\s+/, '').trim(); if (name) platoCounts[name] = (platoCounts[name]||0) + 1; });
    });
    const topPlato = Object.entries(platoCounts).sort((a,b) => b[1]-a[1])[0];
    if (topPlato) { document.getElementById('pos-top-plato').textContent = topPlato[0]; document.getElementById('pos-top-sub').textContent = topPlato[1] + ' pedidos esta semana'; }

    const horaCounts = {};
    orders.forEach(o => { if (o.time) { const hora = o.time.split(':')[0]+':00'; horaCounts[hora] = (horaCounts[hora]||0)+1; } });
    const topHora = Object.entries(horaCounts).sort((a,b) => b[1]-a[1])[0];
    document.getElementById('pos-hora-pico').textContent = topHora ? topHora[0]+'h' : 'N/D';

    renderHoraDist(horaCounts);
    renderDemandBars(horaCounts);
    if (!posCache.data) posCache.data = {};
    Object.assign(posCache.data, { horaCounts, avgTicket, topPlato, topHora });
    await generateAIInsights(orders, avgTicket, topPlato, topHora);
  } catch(e) { console.error('POS error:', e); }
}

function renderHoraDist(horaCounts) {
  const horas = ['11:00','12:00','13:00','14:00','18:00','19:00','20:00','21:00','22:00'];
  const maxVal = Math.max(...Object.values(horaCounts), 1);
  const container = document.getElementById('hora-dist');
  if (!container) return;
  container.innerHTML = horas.map(h => {
    const val = horaCounts[h] || 0;
    const pct = Math.round(val / maxVal * 100);
    return `<div class="hora-item"><span class="hora-label">${h}</span><div class="hora-bar-wrap"><div class="hora-bar" style="width:${pct}%"></div></div><span class="hora-val">${val} ped</span></div>`;
  }).join('');
}

function renderDemandBars(horaCounts) {
  const now = new Date().getHours();
  const maxVal = Math.max(...Object.values(horaCounts), 1);
  const container = document.getElementById('demand-bars');
  if (!container) return;
  container.innerHTML = [now+1, now+2, now+3].map(h => {
    const hora = String(h%24).padStart(2,'0') + ':00';
    const base = horaCounts[hora] || 0;
    const predicted = Math.max(1, Math.round(base * (0.8 + Math.random() * 0.4)));
    const pct = Math.min(100, Math.round(predicted / maxVal * 100 + 20));
    const cls = pct > 70 ? '' : pct > 40 ? 'warn' : 'danger';
    const nivel = pct > 70 ? 'Alta demanda' : pct > 40 ? 'Demanda media' : 'Baja demanda';
    return `<div class="predict-bar"><div class="predict-label"><span>${hora}h — ${nivel}</span><span>~${predicted} pedidos esperados</span></div><div class="predict-track"><div class="predict-fill ${cls}" style="width:${pct}%"></div></div></div>`;
  }).join('');
}

async function generateAIInsights(orders, avgTicket, topPlato, topHora) {
  const rest = window._dashRestaurant;
  const totalRevenue = orders.filter(o => o.paid).reduce((s,o) => s+o.total, 0);
  const domicilio = orders.filter(o => o.type === 'domicilio').length;
  const recoger   = orders.filter(o => o.type === 'recoger').length;
  const dias  = ['domingo','lunes','martes','miércoles','jueves','viernes','sábado'];
  const hoy   = dias[new Date().getDay()];
  const ctx   = `Datos semana "${(rest&&rest.name)||'el restaurante'}":\n- Pedidos: ${orders.length} (${domicilio} dom, ${recoger} recoger)\n- Ingresos: $${totalRevenue.toLocaleString('es-CO')}\n- Ticket prom: $${avgTicket.toLocaleString('es-CO')}\n- Top plato: ${topPlato?topPlato[0]+' ('+topPlato[1]+')':'sin datos'}\n- Hora pico: ${topHora?topHora[0]:'sin datos'}\n- Hoy: ${hoy}`;

  const callAI = async (sys, usr) => {
    const resp = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model:'claude-sonnet-4-20250514', max_tokens:1000, system:sys, messages:[{ role:'user', content:usr }] })
    });
    const d = await resp.json();
    return d.content?.[0]?.text || '';
  };

  document.getElementById('ai-main-text').innerHTML = '<span class="ai-loading">Analizando...</span>';
  try {
    const mainText = await callAI(
      'Eres Mesio IA para restaurantes colombianos. Español, directo, máx 3 oraciones. Usa <strong> para resaltar.',
      ctx + '\n\nGenera un insight accionable para el gerente hoy.'
    );
    document.getElementById('ai-main-text').innerHTML = mainText;
    if (!posCache.data) posCache.data = {};
    posCache.data.mainText = mainText;
  } catch(e) { document.getElementById('ai-main-text').innerHTML = 'Conecta más pedidos para análisis.'; }

  try {
    const stockText = await callAI(
      'Eres Mesio IA. Español, máx 2 oraciones, enfocado en inventario.',
      ctx + '\n\nHoy es ' + hoy + '. ¿Qué ingredientes asegurar con base en el plato top?'
    );
    document.getElementById('ai-stock-text').innerHTML = stockText;
    if (posCache.data) posCache.data.stockText = stockText;
  } catch(e) { document.getElementById('ai-stock-text').innerHTML = 'Datos insuficientes.'; }

  try {
    const raw = await callAI(
      'Eres Mesio IA. Responde SOLO JSON válido sin markdown: [{"icon":"emoji","texto":"sugerencia","ganancia":"impacto"}]. Máx 3 items.',
      ctx + '\n\nGenera 3 sugerencias de upsell para el bot de WhatsApp.'
    );
    const sugerencias = JSON.parse(raw.replace(/```json|```/g,'').trim());
    const upsellHtml = sugerencias.map(s => `
      <div class="upsell-card"><span class="upsell-icon">${s.icon}</span><span class="upsell-text">${s.texto}</span><span class="upsell-badge">${s.ganancia}</span></div>`).join('');
    document.getElementById('upsell-container').innerHTML = upsellHtml;
    if (posCache.data) posCache.data.upsells = upsellHtml;
  } catch(e) { document.getElementById('upsell-container').innerHTML = '<div class="empty-state">Conecta más pedidos.</div>'; }
}

async function askMesioAI() {
  const question = document.getElementById('ai-question').value.trim();
  if (!question) return;
  const rest = window._dashRestaurant;
  const btn = document.querySelector('.ask-ai-btn');
  const responseDiv = document.getElementById('ai-response');
  btn.textContent = 'Pensando...'; btn.disabled = true;
  responseDiv.style.display = 'block';
  responseDiv.textContent   = '✦ Analizando tu pregunta...';
  try {
    const resp = await fetch('https://api.anthropic.com/v1/messages', {
      method:'POST', headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ model:'claude-sonnet-4-20250514', max_tokens:1000,
        system:'Eres Mesio IA, experto en restaurantes colombianos. Español, directo, máx 150 palabras.',
        messages:[{ role:'user', content:question }] })
    });
    const d = await resp.json();
    responseDiv.innerHTML = '✦ ' + (d.content?.[0]?.text || 'No pude procesar.');
  } catch(e) { responseDiv.textContent = 'Error al conectar.'; }
  btn.textContent = 'Preguntar a Mesio IA →'; btn.disabled = false;
}

// ── PEDIDOS MESA (dine-in) en sección Pedidos ────────────────────────
const STATUS_LABEL = {
  recibido:'Recibido', en_preparacion:'En preparación',
  listo:'Listo para servir', entregado:'Entregado',
  factura_entregada:'Factura entregada', cancelado:'Cancelado'
};
const STATUS_COLOR = {
  recibido:'#FAC775', en_preparacion:'#378ADD',
  listo:'#1D9E75', entregado:'#888',
  factura_entregada:'#6B21A8', cancelado:'#E24B4A'
};
const STATUS_BG = {
  recibido:'#FFF8E6', en_preparacion:'#E6F1FB',
  listo:'#E1F5EE', entregado:'#f0f0e8',
  factura_entregada:'#F0E6FF', cancelado:'#FEE2E2'
};

async function loadTableOrdersSection() {
  const h         = window._dashHeaders;
  const container = document.getElementById('dine-in-container');
  const mContainer = document.getElementById('salon-metrics-container');
  const domContainer = document.getElementById('rt-domicilios-container');
  
  if (!container) return;

  try {
    // ── 1. Cargar pedidos de mesa ──
    const rMesa = await fetch('/api/table-orders', { headers: h });
    const { orders: allMesa = [] } = rMesa.ok ? await rMesa.json() : { orders: [] };

    const dNow = new Date();
    const today = `${dNow.getFullYear()}-${String(dNow.getMonth()+1).padStart(2,'0')}-${String(dNow.getDate()).padStart(2,'0')}`;
    
    const visible = allMesa.filter(o => {
      const closed = o.status === 'factura_entregada' || o.status === 'cancelado';
      const dOrder = new Date((o.created_at || '') + (o.created_at?.endsWith('Z') ? '' : 'Z'));
      const orderDay = `${dOrder.getFullYear()}-${String(dOrder.getMonth()+1).padStart(2,'0')}-${String(dOrder.getDate()).padStart(2,'0')}`;
      if (o.status === 'entregado') return orderDay === today;
      return !closed;
    });

    const active = visible.filter(o => ['recibido','en_preparacion','listo'].includes(o.status));
    
    const mesasParaCobrar = new Set();
    visible.forEach(o => {
      if (o.status === 'entregado' || o.status === 'factura_generada') {
        mesasParaCobrar.add(o.base_order_id || o.id.replace(/-\d+$/, ''));
      }
    });

    const billsMap = {};
    visible.forEach(o => {
      const baseId = o.base_order_id || o.id.replace(/-\d+$/, '');
      if (mesasParaCobrar.has(baseId)) {
        if (!billsMap[baseId]) {
          billsMap[baseId] = { id: baseId, table_name: o.table_name, created_at: o.created_at, items: [], total: 0, status: o.status };
        }
        if (o.status === 'factura_generada') billsMap[baseId].status = 'factura_generada';
        let parsedItems = [];
        try { const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items; parsedItems = Array.isArray(arr) ? arr : []; } catch(e) {}
        billsMap[baseId].items.push(...parsedItems);
        billsMap[baseId].total += (Number(o.total) || 0);
      }
    });
    const groupedBills = Object.values(billsMap);
    // Almacenar en window para que markTableInvoiced pueda leer el total y los items
    window._billsData = {};
    groupedBills.forEach(b => { window._billsData[b.id] = b; });

    // ── 2. Cargar pedidos de domicilio/recoger ──
    const localOffset = new Date().getTimezoneOffset();
    const rDom = await fetch(`/api/dashboard/orders?period=today&tz_offset=${localOffset}`, { headers: h });
    const allOrders = rDom.ok ? ((await rDom.json()).orders || []) : [];
    const extOrders = allOrders.filter(o => o.type !== 'mesa');
    const activeExt = extOrders.filter(o => {
      const st = (o.status || '').toLowerCase();
      return !st.includes('entregado') && !st.includes('cancelado');
    });
    const domEntregados = extOrders.filter(o => (o.status||'').includes('entregado')).length;

    const fmt = n => '$' + Number(n).toLocaleString('es-CO');

    // ── 3. Métricas salón ──
    const enCocina   = active.filter(o => ['recibido','en_preparacion'].includes(o.status));
    const conMesero  = active.filter(o => o.status === 'listo');
    const mesasAtendidas = [...new Set(visible.map(o => o.table_id))].length;

    if (mContainer) {
      mContainer.innerHTML = `
        <div class="metric"><div class="metric-label">Mesas Atendidas</div><div class="metric-value">${mesasAtendidas}</div></div>
        <div class="metric"><div class="metric-label">En Cocina</div><div class="metric-value" style="color:#BA7517;">${enCocina.length}</div></div>
        <div class="metric"><div class="metric-label">Con Mesero (Listos)</div><div class="metric-value" style="color:#378ADD;">${conMesero.length}</div></div>
        <div class="metric"><div class="metric-label">En Caja (Por Cobrar)</div><div class="metric-value" style="color:#1D9E75;">${groupedBills.length}</div></div>
      `;
    }

    // ── 4. Monitor domicilios ──
    const rtDomTotal      = document.getElementById('rt-dom-total');
    const rtDomCocina     = document.getElementById('rt-dom-cocina');
    const rtDomEntrega    = document.getElementById('rt-dom-entrega');
    const rtDomEntregados = document.getElementById('rt-dom-entregados');
    if (rtDomTotal)      rtDomTotal.textContent      = extOrders.length;
    if (rtDomCocina)     rtDomCocina.textContent     = activeExt.filter(o => !['en_camino','en_entrega'].includes(o.status||'')).length;
    if (rtDomEntrega)    rtDomEntrega.textContent    = activeExt.filter(o => ['en_camino','en_entrega'].includes(o.status||'')).length;
    if (rtDomEntregados) rtDomEntregados.textContent = domEntregados;

    if (domContainer) {
      if (activeExt.length === 0) {
        domContainer.innerHTML = '<div class="empty-state">No hay domicilios activos en este momento.</div>';
      } else {
        let domHtml = '<div style="font-size:13px;font-weight:bold;margin-bottom:10px;">🕒 ACTIVOS EN PREPARACIÓN / ENTREGA</div>';
        domHtml += '<table><thead><tr><th>Teléfono</th><th>Platos</th><th>Dirección</th><th>Pago</th><th>Estado</th><th>Total</th><th>Acción</th></tr></thead><tbody>';
        activeExt.forEach(o => {
          let itemsStr = '—';
          try {
            const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items;
            itemsStr = Array.isArray(arr) ? arr.map(i => `${i.quantity||1}x ${i.name}`).join(', ') : String(o.items);
          } catch(e) { itemsStr = String(o.items); }
          const stFormat = (o.status || 'pendiente').replace(/_/g,' ').toUpperCase();
          const nextStatus = getNextDeliveryStatus(o.status);
          domHtml += `<tr>
            <td style="font-size:12px;">${o.phone || '—'}</td>
            <td style="color:#555;font-size:12px;max-width:200px;">${itemsStr}</td>
            <td style="font-size:11px;color:#888;max-width:150px;">${o.address || (o.type === 'recoger' ? '🏠 Recoger' : '—')}</td>
            <td style="font-size:11px;color:#0F6E56;font-weight:500;">${o.payment_method || '—'}</td>
            <td><span class="badge" style="background:#E6F1FB;color:#185FA5;">${stFormat}</span></td>
            <td style="font-weight:700;">${fmt(o.total)}</td>
            <td>${nextStatus ? `<button onclick="updateDeliveryStatus('${o.id}','${nextStatus.status}')" style="font-size:11px;padding:4px 8px;background:#1D9E75;color:#fff;border:none;border-radius:6px;cursor:pointer;">${nextStatus.label}</button>` : '<span style="font-size:11px;color:#888;">—</span>'}</td>
          </tr>`;
        });
        domHtml += '</tbody></table>';
        domContainer.innerHTML = domHtml;
      }
    }

    // ── 5. Monitor salón ──
    let html = '';
    if (!active.length && !groupedBills.length) {
      container.innerHTML = '<div class="empty-state">No hay mesas con pedidos activos en este momento.</div>';
    } else {
      if (groupedBills.length > 0) {
        html += '<div style="font-size:13px;font-weight:bold;margin-bottom:10px;color:#6B21A8;">🧾 PENDIENTES DE FACTURA / PAGO</div>';
        html += '<table><thead><tr><th>Mesa</th><th>Platos Consolidados</th><th>Total</th><th>Acción</th></tr></thead><tbody>';
        groupedBills.forEach(b => {
          const itemsJoined = b.items.map(i => (i.quantity||1)+'x '+i.name).join(', ');
          html += `<tr>
            <td style="font-weight:600;">${b.table_name||'—'}</td>
            <td style="color:#555;font-size:12px;max-width:300px;">${itemsJoined}</td>
            <td style="font-weight:700;color:#6B21A8;">${fmt(b.total)}</td>
            <td><button onclick="markTableInvoiced('${b.id}')" style="font-size:11px;padding:5px 12px;background:#7C3AED;color:#fff;border:none;border-radius:6px;cursor:pointer;">Cobrar</button></td>
          </tr>`;
        });
        html += '</tbody></table><br/>';
      }
      if (active.length > 0) {
        html += '<div style="font-size:13px;font-weight:bold;margin-bottom:10px;">🕒 ACTIVOS EN COCINA / SALÓN</div>';
        html += '<table><thead><tr><th>Mesa</th><th>Platos</th><th>Estado</th><th>Hora</th><th>Acción</th></tr></thead><tbody>';
        active.forEach(o => {
          let items = '';
          try {
            const arr = typeof o.items === 'string' ? JSON.parse(o.items) : o.items;
            items = Array.isArray(arr) ? arr.map(i => `${i.quantity||1}× ${i.name||''}`).join(', ') : String(o.items);
          } catch(e) { items = String(o.items||'—'); }
          const isoStr = (o.created_at||'').endsWith('Z') ? o.created_at : (o.created_at||'')+'Z';
          const hora = new Date(isoStr).toLocaleTimeString('es-CO',{hour:'2-digit',minute:'2-digit'});
          const st    = o.status || 'recibido';
          const color = STATUS_COLOR[st] || '#888';
          const bg    = STATUS_BG[st]    || '#f0f0f0';
          const label = STATUS_LABEL[st] || st;
          const nextSt = getNextTableStatus(st);
          html += `<tr>
            <td style="font-weight:600;">${o.table_name||'—'}</td>
            <td style="color:#555;font-size:12px;max-width:280px;">${items}</td>
            <td><span style="font-size:11px;padding:3px 8px;border-radius:10px;font-weight:500;background:${bg};color:${color};">${label}</span></td>
            <td style="color:#888;">${hora}</td>
            <td>${nextSt ? `<button onclick="updateTableOrderStatus('${o.id}','${nextSt.status}')" style="font-size:11px;padding:4px 8px;background:#378ADD;color:#fff;border:none;border-radius:6px;cursor:pointer;">${nextSt.label}</button>` : ''}</td>
          </tr>`;
        });
        html += '</tbody></table>';
      }
      container.innerHTML = html;
    }
  } catch(e) {
    console.error('loadTableOrdersSection:', e);
    if (container) container.innerHTML = '<div class="empty-state">Error de conexión.</div>';
  }
}

function getNextTableStatus(current) {
  const flow = {
    recibido:        { status: 'en_preparacion', label: '🍳 En preparación' },
    en_preparacion:  { status: 'listo',          label: '✅ Listo para servir' },
    listo:           { status: 'entregado',      label: '🍽️ Entregado' },
  };
  return flow[current] || null;
}

function getNextDeliveryStatus(current) {
  const flow = {
    pendiente_pago:  { status: 'confirmado',  label: '✅ Confirmar' },
    confirmado:      { status: 'en_camino',   label: '🛵 En camino' },
    en_camino:       { status: 'entregado',   label: '✅ Entregado' },
  };
  return flow[current] || null;
}

async function updateTableOrderStatus(orderId, newStatus) {
  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/table-orders/${orderId}/status`, {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    if (r.ok) loadTableOrdersSection();
  } catch(e) { console.error('updateTableOrderStatus:', e); }
}

async function updateDeliveryStatus(orderId, newStatus) {
  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/orders/${orderId}/status`, {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    if (r.ok) loadTableOrdersSection();
    else console.error('Error actualizando domicilio status');
  } catch(e) { console.error('updateDeliveryStatus:', e); }
}

async function markTableDelivered(orderId) {
  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/table-orders/${orderId}/status`, {
      method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'entregado' })
    });
    if (r.ok) loadTableOrdersSection();
  } catch(e) { console.error('markTableDelivered:', e); }
}

async function markTableInvoiced(orderId) {
  const h = window._dashHeaders;
  const bill = window._billsData?.[orderId];
  const subtotal = bill?.total || 0;

  // Construir modal de cobro con toggle de cargo de servicio
  const existingModal = document.getElementById('_svc-modal');
  if (existingModal) existingModal.remove();

  const fmtLocal = n => '$' + Math.round(Number(n)).toLocaleString('es-CO');

  const overlay = document.createElement('div');
  overlay.id = '_svc-modal';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center;';

  const box = document.createElement('div');
  box.style.cssText = 'background:#fff;border-radius:14px;padding:1.5rem;width:340px;box-shadow:0 8px 40px rgba(0,0,0,.18);';

  box.innerHTML = `
    <div style="font-size:16px;font-weight:700;margin-bottom:1rem;">🧾 Cobrar Mesa</div>
    <div style="font-size:13px;color:#555;margin-bottom:1rem;">Subtotal: <strong>${fmtLocal(subtotal)}</strong></div>
    <label style="display:flex;align-items:center;gap:10px;font-size:13px;padding:10px;background:#f5f5f0;border-radius:8px;cursor:pointer;margin-bottom:1rem;">
      <input type="checkbox" id="_svc-toggle" style="width:16px;height:16px;cursor:pointer;">
      <span>Incluir Cargo de Servicio (10%)</span>
    </label>
    <div id="_svc-total-preview" style="font-size:14px;font-weight:700;color:#1D9E75;margin-bottom:1.25rem;">Total: ${fmtLocal(subtotal)}</div>
    <div style="display:flex;gap:8px;">
      <button id="_svc-cancel" style="flex:1;padding:9px;border:1px solid #ddd;border-radius:8px;background:none;cursor:pointer;font-size:13px;">Cancelar</button>
      <button id="_svc-confirm" style="flex:1;padding:9px;border:none;border-radius:8px;background:#1D9E75;color:#fff;cursor:pointer;font-size:13px;font-weight:600;">Confirmar Cobro</button>
    </div>
  `;

  overlay.appendChild(box);
  document.body.appendChild(overlay);

  const toggle = document.getElementById('_svc-toggle');
  const preview = document.getElementById('_svc-total-preview');

  toggle.addEventListener('change', () => {
    const newTotal = toggle.checked ? subtotal * 1.1 : subtotal;
    preview.textContent = `Total: ${fmtLocal(newTotal)}${toggle.checked ? ' (incl. 10% servicio)' : ''}`;
  });

  document.getElementById('_svc-cancel').addEventListener('click', () => overlay.remove());

  document.getElementById('_svc-confirm').addEventListener('click', async () => {
    const includeService = toggle.checked;
    overlay.remove();
    try {
      if (includeService && subtotal > 0) {
        const newTotal = Math.round(subtotal * 1.1);
        const allItems = bill?.items || [];
        await fetch(`/api/table-orders/${orderId}/adjust`, {
          method: 'PATCH',
          headers: { ...h, 'Content-Type': 'application/json' },
          body: JSON.stringify({ items: allItems, total: newTotal, service_charge: Math.round(subtotal * 0.1) })
        });
      }
      const r = await fetch(`/api/table-orders/${orderId}/status`, {
        method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'factura_entregada' })
      });
      if (r.ok) loadTableOrdersSection();
    } catch(e) { console.error('markTableInvoiced:', e); }
  });
}

const _origFetchOrders = window.fetchOrders;
const _origRefreshAll = window.refreshAll;
if (typeof _origRefreshAll === 'function') {
  window.refreshAll = async function() {
    await _origRefreshAll();
    loadTableOrdersSection();
  };
}
document.addEventListener('DOMContentLoaded', () => {
  loadTableOrdersSection();
  setInterval(loadTableOrdersSection, 15000);
});

// ── GESTIÓN DE STAFF OPERATIVO (ROSTER) ─────────────────────────────────

async function loadStaff() {
  const h = window._dashHeaders;
  const container = document.getElementById('staff-component');
  if (!container) return;

  try {
    const r = await fetch('/api/staff', { headers: h });
    if (!r.ok) {
      container.innerHTML = '<div class="empty-state">Error al cargar el equipo.</div>';
      return;
    }
    const data = await r.json();
    const staffList = data.staff || [];
    renderStaff(staffList);
  } catch(e) {
    console.error('Error loadStaff:', e);
    container.innerHTML = '<div class="empty-state">Error de conexión.</div>';
  }
}

function renderStaff(staffList) {
  const container = document.getElementById('staff-component');
  
  // Filtrar solo los activos (opcional, o mostrar todos)
  const activeStaff = staffList.filter(s => s.active !== false);

  if (activeStaff.length === 0) {
    container.innerHTML = '<div class="empty-state">No hay empleados operativos registrados.</div>';
    return;
  }

  // Usamos un Grid CSS directo en línea para las tarjetas
  let html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;">';
  
  activeStaff.forEach(s => {
    const rolesStr = s.roles ? s.roles.join(',') : (s.role || 'mesero');
    const badgesHtml = formatRoles(rolesStr); // Reutilizamos tu función formatRoles
    
    html += `
      <div style="background:#fff;border:0.5px solid #e0e0d8;border-radius:12px;padding:1.25rem;position:relative;box-shadow:0 2px 8px rgba(0,0,0,0.02);">
        <button onclick="deleteStaff('${s.id}', '${s.name}')" style="position:absolute;top:10px;right:10px;background:none;border:none;color:#aaa;font-size:18px;cursor:pointer;line-height:1;">&times;</button>
        
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
          <div style="width:40px;height:40px;border-radius:50%;background:#f0f0e8;display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#555;">
            ${s.name.charAt(0).toUpperCase()}
          </div>
          <div>
            <div style="font-size:15px;font-weight:600;color:#222;">${s.name}</div>
            <div style="font-size:11px;color:#888;">PIN Configurado</div>
          </div>
        </div>
        
        <div style="min-height:30px;">
          ${badgesHtml}
        </div>
      </div>
    `;
  });
  
  html += '</div>';
  container.innerHTML = html;
}

function openCreateStaffModal() {
  document.getElementById('staff-create-name').value = '';
  document.getElementById('staff-create-pin').value = '';
  // Resetear checkboxes (dejar solo mesero)
  document.querySelectorAll('#staff-create-roles input[type="checkbox"]').forEach(cb => {
    cb.checked = cb.value === 'mesero';
  });
  document.getElementById('modal-staff-create').style.display = 'flex';
}

function closeCreateStaffModal() {
  document.getElementById('modal-staff-create').style.display = 'none';
}

async function submitCreateStaff() {
  const h = window._dashHeaders;
  const name = document.getElementById('staff-create-name').value.trim();
  const pin = document.getElementById('staff-create-pin').value.trim();
  
  // Recoger los roles seleccionados
  const selectedRoles = [];
  document.querySelectorAll('#staff-create-roles input[type="checkbox"]:checked').forEach(cb => {
    selectedRoles.push(cb.value);
  });

  if (!name) { alert('Ingresa el nombre del empleado.'); return; }
  if (selectedRoles.length === 0) { alert('Selecciona al menos un rol.'); return; }
  if (pin.length < 4) { alert('El PIN debe tener al menos 4 dígitos.'); return; }

  try {
    // El backend espera el PIN en el campo "password" según tu pydantic model
    const payload = {
      name: name,
      roles: selectedRoles,
      role: selectedRoles[0], // fallback
      password: pin,
      phone: ""
    };

    const r = await fetch('/api/staff', {
      method: 'POST',
      headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (r.ok) {
      closeCreateStaffModal();
      loadStaff(); // Recargar el grid
    } else {
      const e = await r.json();
      alert('Error al crear empleado: ' + (e.detail || r.statusText));
    }
  } catch(e) {
    console.error(e);
    alert('Error de conexión.');
  }
}

async function deleteStaff(id, name) {
  if (!confirm(`¿Estás seguro de que deseas eliminar a ${name}?`)) return;
  
  const h = window._dashHeaders;
  try {
    const r = await fetch('/api/staff/' + id, {
      method: 'DELETE',
      headers: h
    });
    
    if (r.ok) {
      loadStaff();
    } else {
      const e = await r.json();
      alert('Error al eliminar: ' + (e.detail || r.statusText));
    }
  } catch(e) {
    alert('Error de conexión.');
  }
}

// Asegurarnos de que se cargue al iniciar si estamos en la pestaña
// Puedes atar esto a tu función showSection del dashboard-core.js
document.addEventListener('DOMContentLoaded', () => {
  // Solo intentamos cargar si el contenedor existe
  if(document.getElementById('staff-component')) {
      loadStaff();
  }
});