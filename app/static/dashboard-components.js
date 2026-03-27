/**
 * Mesio Dashboard — Component Architecture
 * app/static/dashboard-components.js
 *
 * Provides MesioComponent: a lightweight factory for encapsulated Vanilla JS
 * components. New modules (Phase 6+) use this pattern instead of adding global
 * functions.
 *
 * Pattern benefits:
 *   - Encapsulated state (no globals per component)
 *   - Safe DOM writes via textContent / createElement (no innerHTML in component core)
 *   - Predictable lifecycle: mount → setState → unmount
 *   - Easy to test: component.setState({...}) and inspect the container
 *
 * Usage — define a component:
 *
 *   const MyView = MesioComponent({
 *     state: { items: [], loading: true },
 *
 *     render(state, el) {
 *       el.textContent = '';                       // clear safely
 *       if (state.loading) {
 *         const p = document.createElement('p');
 *         p.textContent = 'Cargando...';
 *         el.appendChild(p);
 *         return;
 *       }
 *       state.items.forEach(item => {
 *         const row = document.createElement('div');
 *         row.textContent = item.name;
 *         el.appendChild(row);
 *       });
 *     },
 *
 *     onMount(self) {
 *       fetchItems().then(items => self.setState({ items, loading: false }));
 *     },
 *   });
 *
 *   // Mount to a DOM selector:
 *   MyView.mount('#my-section');
 *
 *   // Update state (triggers re-render):
 *   MyView.setState({ loading: true });
 *
 *   // Unmount and clean up:
 *   MyView.unmount();
 */

/**
 * @typedef {Object} ComponentConfig
 * @property {object}   [state]     - Initial state object
 * @property {Function} render      - (state, containerEl) => void  — writes to DOM
 * @property {Function} [onMount]   - (self, state) => void
 * @property {Function} [onUnmount] - (self, state) => void
 */

/**
 * Create a new component instance.
 * @param {ComponentConfig} config
 * @returns {{ mount, unmount, setState, getState }}
 */
function MesioComponent(config) {
  let _state     = Object.assign({}, config.state || {});
  let _container = null;
  let _mounted   = false;

  function _render() {
    if (_container && config.render) {
      config.render(_state, _container);
    }
  }

  const self = {
    /**
     * Mount the component into a DOM element.
     * @param {string|HTMLElement} target - CSS selector or element
     * @returns {object} self (for chaining)
     */
    mount(target) {
      _container = typeof target === 'string'
        ? document.querySelector(target)
        : target;
      if (!_container) {
        console.warn('[MesioComponent] mount target not found:', target);
        return self;
      }
      _render();
      if (!_mounted) {
        _mounted = true;
        if (config.onMount) config.onMount(self, _state);
      }
      return self;
    },

    /**
     * Merge patch into state and re-render.
     * @param {object} patch
     */
    setState(patch) {
      _state = Object.assign({}, _state, patch);
      _render();
    },

    /** Return a shallow copy of current state. */
    getState() {
      return Object.assign({}, _state);
    },

    /** Tear down the component (run onUnmount, release references). */
    unmount() {
      if (_mounted && config.onUnmount) config.onUnmount(self, _state);
      _container = null;
      _mounted   = false;
    },
  };

  return self;
}


// ─────────────────────────────────────────────────────────────────────────────
// ConnectionStatus — sidebar connectivity indicator
// Replaces the static ".live-badge" with a reactive component.
//
// Targets:
//   #conn-dot   → the animated dot (class toggled: '' | 'offline')
//   #conn-text  → the status text node
// ─────────────────────────────────────────────────────────────────────────────
const ConnectionStatus = MesioComponent({
  state: { online: navigator.onLine },

  render({ online }, _el) {
    // Update dot color class
    const dot = document.getElementById('conn-dot');
    if (dot) {
      dot.className = online ? 'live-dot' : 'live-dot offline';
    }
    // Update text — textContent only, no innerHTML
    const txt = document.getElementById('conn-text');
    if (txt) {
      txt.textContent = online ? 'Bot activo 24/7' : 'Sin conexion · Guardando offline';
    }
  },

  onMount(self) {
    window.addEventListener('online',  () => self.setState({ online: true }));
    window.addEventListener('offline', () => self.setState({ online: false }));
  },
});


// ─────────────────────────────────────────────────────────────────────────────
// StaffSection — Roster, Turnos & Propinas
//
// State shape:
//   {
//     loading:    bool,
//     staff:      [...],          // GET /api/staff
//     shifts:     [...],          // GET /api/staff/open-shifts  (includes staff_id)
//     tipPreview: null | {...},   // result of POST /api/staff/tip-cut
//     tab:        'roster' | 'shifts' | 'tips',
//     filter:     'all' | role,   // active role filter chip
//     search:     string,         // name search term
//     error:      string | null,
//   }
// ─────────────────────────────────────────────────────────────────────────────

const _ROLE_LABELS = {
  mesero:       'Mesero',
  cocina:       'Cocina',
  bar:          'Bar',
  caja:         'Caja',
  domiciliario: 'Domiciliario',
  admin:        'Administrador',
  owner:        'Dueño'
};

const _ROLE_META = {
  mesero:       { icon: '🍽️', bg: '#E1F5EE', color: '#0F6E56' },
  cocina:       { icon: '👨‍🍳', bg: '#FEF3C7', color: '#92400E' },
  bar:          { icon: '🍹', bg: '#F0E6FF', color: '#6B21A8' },
  caja:         { icon: '💰', bg: '#FFF8E6', color: '#BA7517' },
  domiciliario: { icon: '🛵', bg: '#E3F2FD', color: '#1565C0' },
  admin:        { icon: '🛡️', bg: '#E6F1FB', color: '#185FA5' },
  owner:        { icon: '👑', bg: '#E1F5EE', color: '#1D9E75' }
};

function getDynamicRoleMeta(roleKey) {
  if (_ROLE_META[roleKey]) return _ROLE_META[roleKey];
  // Genera un color consistente basado en el string
  const colors = [
    { bg: '#F3E8FF', color: '#7C3AED' }, // Morado
    { bg: '#FCE8F3', color: '#BE185D' }, // Rosa
    { bg: '#E0F2FE', color: '#1D4ED8' }, // Azul
    { bg: '#FEF3C7', color: '#B45309' }, // Ambar
    { bg: '#F3F4F6', color: '#4B5563' }  // Gris
  ];
  let hash = 0;
  for(let i = 0; i < roleKey.length; i++) hash = roleKey.charCodeAt(i) + ((hash << 5) - hash);
  return { icon: '🏷️', ...colors[Math.abs(hash) % colors.length] };
}

function getDynamicRoleLabel(roleKey) {
  if (_ROLE_LABELS[roleKey]) return _ROLE_LABELS[roleKey];
  // Capitaliza la primera letra del rol inventado
  return roleKey.charAt(0).toUpperCase() + roleKey.slice(1);
}

function _apiHeaders() {
  const token = localStorage.getItem('rb_token') || '';
  const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` };
  
  // 🛡️ Inyectar ID de sucursal si el owner seleccionó una en el dropdown
  const branchSelect = document.getElementById('staff-branch-select');
  if (branchSelect && branchSelect.value && branchSelect.value !== 'matriz') {
      headers['X-Branch-ID'] = branchSelect.value;
  }
  return headers;
}

// ── DOM helpers ───────────────────────────────────────────────────────────────

function _makeBtn(label, cls, onClick) {
  const b = document.createElement('button');
  b.textContent = label;
  b.className   = cls;
  b.addEventListener('click', onClick);
  return b;
}

function _makeInput(placeholder, type = 'text', value = '') {
  const i = document.createElement('input');
  i.type        = type;
  i.placeholder = placeholder;
  i.value       = value;
  i.style.cssText = 'padding:7px 11px;border:1px solid #e0e0d8;border-radius:8px;font-size:13px;';
  return i;
}

function _makeSelect(options, value = '') {
  const s = document.createElement('select');
  s.style.cssText = 'padding:7px 11px;border:1px solid #e0e0d8;border-radius:8px;font-size:13px;';
  options.forEach(([val, label]) => {
    const opt = document.createElement('option');
    opt.value = val;
    opt.textContent = label;
    if (val === value) opt.selected = true;
    s.appendChild(opt);
  });
  return s;
}

function _rowStyle() {
  return 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;';
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function _staffFetch(path, opts = {}) {
  const res = await fetch('/api/staff' + path, {
    headers: _apiHeaders(),
    ...opts,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Avatar helpers ────────────────────────────────────────────────────────────

function _initials(name) {
  return name.split(' ').slice(0, 2).map(w => w[0] || '').join('').toUpperCase() || '?';
}

function _avatarEl(member) {
  const roles   = (member.roles && member.roles.length) ? member.roles : [member.role];
  const primary = roles[0] || 'otro';
  // ✅ Usamos el generador dinámico de colores
  const meta    = getDynamicRoleMeta(primary);
  const el      = document.createElement('div');
  el.textContent = _initials(member.name);
  el.style.cssText = `width:46px;height:46px;border-radius:12px;background:${meta.bg};
    color:${meta.color};font-size:15px;font-weight:800;display:flex;align-items:center;
    justify-content:center;flex-shrink:0;letter-spacing:-0.5px;`;
  return el;
}

// ── Add / Edit modal ──────────────────────────────────────────────────────────

function _openStaffModal(self, existing = null) {
  const isEdit = !!existing;
  const old    = document.getElementById('_staff-modal');
  if (old) old.remove();

  const overlay = document.createElement('div');
  overlay.id = '_staff-modal';
  overlay.style.cssText = 'display:flex;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:2000;align-items:center;justify-content:center;padding:1rem;';

  const box = document.createElement('div');
  box.style.cssText = 'background:#fff;border-radius:20px;padding:2rem;width:520px;max-width:100%;max-height:92vh;overflow-y:auto;box-shadow:0 24px 64px rgba(0,0,0,0.2);';

  const title = document.createElement('div');
  title.textContent = isEdit ? 'Editar empleado' : 'Nuevo empleado operativo';
  title.style.cssText = 'font-size:17px;font-weight:700;margin-bottom:1.5rem;color:#111;';
  box.appendChild(title);

  const nameLabel = document.createElement('div');
  nameLabel.textContent = 'Nombre completo';
  nameLabel.style.cssText = 'font-size:12px;font-weight:700;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:0.04em;';
  box.appendChild(nameLabel);
  const nameIn = _makeInput('Nombre del empleado');
  if (existing) nameIn.value = existing.name;
  nameIn.style.cssText += 'width:100%;box-sizing:border-box;margin-bottom:1rem;font-size:14px;padding:10px 12px;';
  box.appendChild(nameIn);

  const phoneLabel = document.createElement('div');
  phoneLabel.textContent = 'Teléfono (opcional)';
  phoneLabel.style.cssText = 'font-size:12px;font-weight:700;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:0.04em;';
  box.appendChild(phoneLabel);
  const phoneIn = _makeInput('Ej: 3001234567');
  if (existing) phoneIn.value = existing.phone || '';
  phoneIn.style.cssText += 'width:100%;box-sizing:border-box;margin-bottom:1.25rem;font-size:14px;padding:10px 12px;';
  box.appendChild(phoneIn);

  // ── SECCIÓN DE ROLES CON TARJETAS ──
  const roleLabel = document.createElement('div');
  roleLabel.textContent = 'Roles del empleado';
  roleLabel.style.cssText = 'font-size:12px;font-weight:700;color:#555;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.04em;';
  box.appendChild(roleLabel);

  const CORE_ROLES = [
    ['mesero',       '🍽️', 'Mesero'],
    ['caja',         '💰', 'Cajero'],
    ['cocina',       '👨‍🍳', 'Cocina'],
    ['bar',          '🍹', 'Bar'],
    ['domiciliario', '🛵', 'Domicilios'],
    ['otro',         '🏷️', 'Otro...']
  ];

  const cardsGrid = document.createElement('div');
  cardsGrid.style.cssText = 'display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:1rem;';
  box.appendChild(cardsGrid);

  // ZONA DE INPUT PERSONALIZADO (oculta por defecto)
  const customRoleWrap = document.createElement('div');
  customRoleWrap.style.cssText = 'display:none;background:#f8f8f5;border:1px dashed #ccc;border-radius:12px;padding:12px;margin-bottom:1rem;';
  
  const customRoleWarning = document.createElement('div');
  customRoleWarning.textContent = '⚠️ Los roles personalizados no tienen una pantalla o app dedicada. Servirán para métricas, cortes de propina y organización interna.';
  customRoleWarning.style.cssText = 'font-size:11px;color:#888;margin-bottom:8px;line-height:1.4;';
  customRoleWrap.appendChild(customRoleWarning);

  const roleInputRow = document.createElement('div');
  roleInputRow.style.cssText = 'display:flex;gap:8px;';
  
  const customRoleIn = _makeInput('Escribe un rol (ej: Hostess)');
  customRoleIn.style.cssText += 'flex:1;font-size:13px;padding:8px 12px;';
  
  const addRoleBtn = _makeBtn('Añadir rol', 'btn-sm btn-outline', () => addRoleChip(customRoleIn.value));
  addRoleBtn.style.padding = '0 12px';
  
  roleInputRow.appendChild(customRoleIn);
  roleInputRow.appendChild(addRoleBtn);
  customRoleWrap.appendChild(roleInputRow);
  box.appendChild(customRoleWrap);

  // CONTENEDOR DE ETIQUETAS (ROLES SELECCIONADOS)
  const selectedChipsContainer = document.createElement('div');
  selectedChipsContainer.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-bottom:1.5rem;';
  box.appendChild(selectedChipsContainer);

  // 🛡️ LIMPIEZA EXTREMA DE ROLES EXISTENTES PARA EVITAR DUPLICADOS
  let currentRoles = new Set();
  if (existing) {
    let rArr = [];
    if (Array.isArray(existing.roles)) rArr = existing.roles;
    else if (typeof existing.roles === 'string') rArr = existing.roles.split(',');
    else if (existing.role) rArr = [existing.role];
    
    rArr.forEach(r => {
        // Quitamos comillas, llaves y espacios fantasma de la DB
        const cleanRole = r.replace(/["\[\]{}]/g, '').trim().toLowerCase();
        if(cleanRole) currentRoles.add(cleanRole);
    });
  }
  if(currentRoles.size === 0) currentRoles.add('mesero');

  let showCustomInput = false;
  const coreRoleKeys = CORE_ROLES.map(r => r[0]);
  Array.from(currentRoles).forEach(r => {
      if(!coreRoleKeys.includes(r) && r !== 'admin' && r !== 'owner') showCustomInput = true;
  });

  function updateUI() {
      // 1. Dibujar tarjetas
      cardsGrid.innerHTML = '';
      CORE_ROLES.forEach(([roleKey, icon, lbl]) => {
          const isOtro = roleKey === 'otro';
          const active = isOtro ? showCustomInput : currentRoles.has(roleKey);
          const meta = getDynamicRoleMeta(roleKey);
          
          const card = document.createElement('div');
          card.style.cssText = `border:2px solid ${active ? meta.color : '#e0e0d8'};
            background:${active ? meta.bg : '#fafafa'};border-radius:11px;padding:10px 6px;
            text-align:center;cursor:pointer;transition:all .15s;user-select:none;`;

          card.innerHTML = `<div style="font-size:20px;margin-bottom:4px;">${icon}</div>
                            <div style="font-size:11px;font-weight:700;color:${active ? meta.color : '#999'};">${lbl}</div>`;
          
          card.addEventListener('click', () => {
              if(isOtro) {
                  showCustomInput = !showCustomInput;
                  updateUI();
                  if(showCustomInput) setTimeout(() => customRoleIn.focus(), 50);
              } else {
                  if (currentRoles.has(roleKey)) {
                      if (currentRoles.size > 1) currentRoles.delete(roleKey);
                      else alert('El empleado debe tener al menos un rol.');
                  } else {
                      currentRoles.add(roleKey);
                  }
                  updateUI();
              }
          });
          cardsGrid.appendChild(card);
      });

      customRoleWrap.style.display = showCustomInput ? 'block' : 'none';

      // 2. Dibujar etiquetas (Chips)
      selectedChipsContainer.innerHTML = '';
      currentRoles.forEach(roleKey => {
          const meta = getDynamicRoleMeta(roleKey);
          const label = getDynamicRoleLabel(roleKey);
          
          const chip = document.createElement('div');
          chip.style.cssText = `background:${meta.bg};color:${meta.color};border:1px solid ${meta.color};
            padding:4px 10px;border-radius:16px;font-size:12px;font-weight:600;display:flex;align-items:center;gap:6px;cursor:pointer;`;
          
          const textSpan = document.createElement('span');
          textSpan.textContent = `${meta.icon} ${label}`;
          const closeSpan = document.createElement('span');
          closeSpan.textContent = '×';
          closeSpan.style.cssText = 'font-size:14px;line-height:1;';
          
          chip.appendChild(textSpan);
          chip.appendChild(closeSpan);
          
          chip.addEventListener('click', () => {
              if(currentRoles.size > 1) {
                  currentRoles.delete(roleKey);
                  updateUI();
              } else {
                  alert('El empleado debe tener al menos un rol.');
              }
          });
          selectedChipsContainer.appendChild(chip);
      });
  }

  function addRoleChip(rawVal) {
      const val = rawVal.replace(/["\[\]{}]/g, '').trim().toLowerCase();
      if(val && val !== 'admin' && val !== 'owner' && val !== 'otro') {
          // Evitamos que añadan duplicados escribiendo en el input
          if(!currentRoles.has(val)) {
              currentRoles.add(val);
          }
          customRoleIn.value = '';
          updateUI();
      } else if(val === 'admin' || val === 'owner') {
          alert('Los roles administrativos se gestionan desde "Mis Sucursales".');
          customRoleIn.value = '';
      }
  }

  customRoleIn.addEventListener('keydown', (e) => {
      if(e.key === 'Enter') { e.preventDefault(); addRoleChip(customRoleIn.value); }
  });

  updateUI();

  // ── PIN Y BOTONES ──
  const pinLabel = document.createElement('div');
  pinLabel.textContent = isEdit ? 'Nueva contraseña (dejar vacío para no cambiar)' : 'Contraseña / PIN (mínimo 4 caracteres)';
  pinLabel.style.cssText = 'font-size:12px;font-weight:700;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:0.04em;';
  box.appendChild(pinLabel);
  const pinIn = _makeInput(isEdit ? '(sin cambio)' : 'Mínimo 4 caracteres', 'password');
  pinIn.style.cssText += 'width:100%;box-sizing:border-box;margin-bottom:1rem;font-size:14px;padding:10px 12px;';
  box.appendChild(pinIn);

  const errMsg = document.createElement('div');
  errMsg.style.cssText = 'color:#C0392B;font-size:12px;margin-bottom:10px;min-height:16px;';
  box.appendChild(errMsg);

  const btnRow = document.createElement('div');
  btnRow.style.cssText = 'display:flex;gap:8px;';

  const submitBtn = _makeBtn(isEdit ? 'Guardar cambios' : 'Crear empleado', 'btn-sm btn-primary', async () => {
      errMsg.textContent = '';
      const name  = nameIn.value.trim();
      const pin   = pinIn.value.trim();
      const phone = phoneIn.value.trim();

      if (!name) { errMsg.textContent = 'El nombre es obligatorio.'; return; }
      if (!isEdit && pin.length < 4) { errMsg.textContent = 'La contraseña debe tener al menos 4 caracteres.'; return; }
      if (currentRoles.size === 0) { errMsg.textContent = 'Añade al menos un rol.'; return; }

      submitBtn.disabled = true;
      submitBtn.textContent = isEdit ? 'Guardando...' : 'Creando...';
      try {
        const rolesArr = Array.from(currentRoles);
        if (isEdit) {
          const patch = { name, roles: rolesArr, role: rolesArr[0], phone };
          if (pin) patch.password = pin;
          await _staffFetch(`/${existing.id}`, { method: 'PUT', body: JSON.stringify(patch) });
        } else {
          await _staffFetch('', {
            method: 'POST',
            body: JSON.stringify({ name, role: rolesArr[0], roles: rolesArr, password: pin, phone }),
          });
        }
        overlay.remove();
        await _reloadRoster(self);
      } catch (e) {
        errMsg.textContent = e.message;
        submitBtn.disabled = false;
        submitBtn.textContent = isEdit ? 'Guardar cambios' : 'Crear empleado';
      }
  });

  const cancelBtn = _makeBtn('Cancelar', 'btn-sm btn-outline', () => overlay.remove());

  btnRow.appendChild(submitBtn);
  btnRow.appendChild(cancelBtn);
  box.appendChild(btnRow);

  overlay.appendChild(box);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  setTimeout(() => nameIn.focus(), 50);
}

// ── Roster tab ────────────────────────────────────────────────────────────────

function _renderRosterTab(state, el, self) {
  const onShiftIds = new Set((state.shifts || []).map(s => s.staff_id));

  // ── Stats banner
  const statsBar = document.createElement('div');
  statsBar.style.cssText = 'display:flex;gap:10px;margin-bottom:1.25rem;flex-wrap:wrap;';

  const activeCount  = state.staff.filter(s => s.active).length;
  const onShiftCount = state.staff.filter(s => onShiftIds.has(s.id)).length;

  [
    ['Total',    state.staff.length, '#F3F4F6', '#555555'],
    ['Activos',  activeCount,        '#E1F5EE', '#0F6E56'],
    ['En turno', onShiftCount,       '#EFF6FF', '#1D4ED8'],
  ].forEach(([label, count, bg, color]) => {
    const pill = document.createElement('div');
    pill.style.cssText = `background:${bg};border-radius:10px;padding:8px 18px;display:flex;align-items:center;gap:8px;`;
    const num  = document.createElement('span');
    num.textContent = count;
    num.style.cssText = `font-size:22px;font-weight:800;color:${color};`;
    const lbl  = document.createElement('span');
    lbl.textContent = label;
    lbl.style.cssText = 'font-size:12px;color:#888;';
    pill.appendChild(num);
    pill.appendChild(lbl);
    statsBar.appendChild(pill);
  });
  el.appendChild(statsBar);

  // ── Search + add button
  const topRow = document.createElement('div');
  topRow.style.cssText = 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px;';

  const searchIn = document.createElement('input');
  searchIn.type        = 'text';
  searchIn.placeholder = 'Buscar empleado...';
  searchIn.value       = state.search || '';
  searchIn.style.cssText = 'flex:1;min-width:160px;padding:8px 12px;border:1px solid #e0e0d8;border-radius:9px;font-size:13px;outline:none;';
  searchIn.addEventListener('input', () => self.setState({ search: searchIn.value }));
  topRow.appendChild(searchIn);

  const addBtn = _makeBtn('+ Nuevo empleado', 'btn-sm btn-primary', () => _openStaffModal(self));
  topRow.appendChild(addBtn);
  el.appendChild(topRow);

  // ── Role filter chips
  const chips = document.createElement('div');
  chips.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin-bottom:1.25rem;';

  const FILTER_CHIPS = [
    ['all',          'Todos'],
    ['mesero',       '🍽️ Mesero'],
    ['cocina',       '👨‍🍳 Cocina'],
    ['bar',          '🍹 Bar'],
    ['caja',         '💰 Caja'],
    ['domiciliario', '🛵 Domiciliario'],
  ];

  const activeFilter = state.filter || 'all';
  FILTER_CHIPS.forEach(([id, label]) => {
    const active = activeFilter === id;
    const chip   = document.createElement('button');
    chip.textContent = label;
    chip.style.cssText = `padding:5px 13px;border-radius:20px;font-size:12px;font-weight:500;cursor:pointer;transition:all .15s;
      border:1px solid ${active ? '#1D9E75' : '#e0e0d8'};
      background:${active ? '#1D9E75' : '#fff'};
      color:${active ? '#fff' : '#666'};`;
    chip.addEventListener('click', () => self.setState({ filter: id }));
    chips.appendChild(chip);
  });
  el.appendChild(chips);

  // ── Filter + search
  const searchTerm = (state.search || '').toLowerCase().trim();
  let visible = [...state.staff].sort((a, b) => (b.active ? 1 : 0) - (a.active ? 1 : 0));
  if (activeFilter !== 'all') {
    visible = visible.filter(m => {
      let rolesArr = [];
      if (Array.isArray(m.roles)) rolesArr = m.roles;
      else if (typeof m.roles === 'string') rolesArr = m.roles.replace(/[{}]/g, '').split(',');
      else if (m.role) rolesArr = [m.role];
      
      return rolesArr.map(r => r.trim()).includes(activeFilter);
    });
  }
  if (searchTerm) {
    visible = visible.filter(m => m.name.toLowerCase().includes(searchTerm));
  }

  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className   = 'empty-state';
    empty.textContent = state.staff.length === 0
      ? 'No hay empleados aún. Crea el primero con el botón + Nuevo empleado.'
      : 'Ningún empleado coincide con el filtro aplicado.';
    el.appendChild(empty);
  } else {
    // ── Card grid
    const grid = document.createElement('div');
    grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:12px;';

    visible.forEach(member => {
      const card = document.createElement('div');
      card.style.cssText = `background:#fff;border:1px solid #e8e8e0;border-radius:16px;padding:1.25rem;
        transition:box-shadow .15s,border-color .15s;${member.active ? '' : 'opacity:0.55;'}`;
      card.addEventListener('mouseenter', () => {
        card.style.boxShadow  = '0 4px 18px rgba(0,0,0,0.08)';
        card.style.borderColor = '#d4d4cc';
      });
      card.addEventListener('mouseleave', () => {
        card.style.boxShadow  = '';
        card.style.borderColor = '#e8e8e0';
      });

      // ── Top row: avatar | info | status
      const topDiv = document.createElement('div');
      topDiv.style.cssText = 'display:flex;gap:12px;align-items:flex-start;margin-bottom:12px;';

      topDiv.appendChild(_avatarEl(member));

      const info = document.createElement('div');
      info.style.cssText = 'flex:1;min-width:0;';

      const nameEl = document.createElement('div');
      nameEl.textContent = member.name;
      nameEl.style.cssText = 'font-size:14px;font-weight:700;color:#111;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px;';
      info.appendChild(nameEl);

      // Role badges
      // Role badges
      const badgeRow = document.createElement('div');
      badgeRow.style.cssText = 'display:flex;flex-wrap:wrap;gap:3px;';
      
      let rolesArray = [];
      if (Array.isArray(member.roles)) {
        rolesArray = member.roles;
      } else if (typeof member.roles === 'string') {
        rolesArray = member.roles.replace(/[{}]/g, '').split(',');
      } else if (member.role) {
        rolesArray = [member.role];
      } else {
        rolesArray = ['mesero'];
      }

      rolesArray.forEach(r => {
        const cleanRole = r.trim();
        if (!cleanRole) return;
        // ✅ Usamos el generador dinámico de colores y etiquetas
        const meta  = getDynamicRoleMeta(cleanRole);
        const badge = document.createElement('span');
        badge.textContent = getDynamicRoleLabel(cleanRole);
        badge.style.cssText = `background:${meta.bg};color:${meta.color};padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;`;
        badgeRow.appendChild(badge);
      });
      
      info.appendChild(badgeRow);

      if (member.phone) {
        const phoneEl = document.createElement('div');
        phoneEl.textContent = member.phone;
        phoneEl.style.cssText = 'font-size:11px;color:#bbb;margin-top:4px;';
        info.appendChild(phoneEl);
      }
      topDiv.appendChild(info);

      // Status chip
      const isOnShift = onShiftIds.has(member.id);
      const chip = document.createElement('div');
      chip.style.cssText = `flex-shrink:0;padding:3px 10px;border-radius:20px;font-size:10px;font-weight:700;white-space:nowrap;
        background:${isOnShift ? '#E1F5EE' : '#f5f5f0'};
        color:${isOnShift ? '#0F6E56' : (member.active ? '#aaa' : '#ccc')};
        border:1px solid ${isOnShift ? '#A7F3D0' : '#e8e8e0'};`;
      chip.textContent = isOnShift ? '● En turno' : (member.active ? '○ Libre' : '○ Inactivo');
      topDiv.appendChild(chip);
      card.appendChild(topDiv);

      // ── Action row
      const actRow = document.createElement('div');
      actRow.style.cssText = 'display:flex;gap:5px;align-items:center;border-top:1px solid #f2f2ea;padding-top:10px;flex-wrap:wrap;';

      if (member.active) {
        if (!isOnShift) {
          const ciBtn = _makeBtn('▶ Entrada', 'btn-sm btn-primary', async () => {
            ciBtn.disabled = true;
            try {
              await _staffFetch('/clock-in', { method: 'POST', body: JSON.stringify({ staff_id: member.id }) });
              await _reloadRoster(self);
            } catch (e) { alert(e.message); ciBtn.disabled = false; }
          });
          actRow.appendChild(ciBtn);
        } else {
          const coBtn = _makeBtn('■ Salida', 'btn-sm btn-outline', async () => {
            coBtn.disabled = true;
            try {
              await _staffFetch('/clock-out', { method: 'POST', body: JSON.stringify({ staff_id: member.id }) });
              await _reloadRoster(self);
            } catch (e) { alert(e.message); coBtn.disabled = false; }
          });
          actRow.appendChild(coBtn);
        }
      }

      const editBtn = _makeBtn('✏ Editar', 'btn-sm btn-outline', () => _openStaffModal(self, member));
      actRow.appendChild(editBtn);

      const spacer = document.createElement('div');
      spacer.style.flex = '1';
      actRow.appendChild(spacer);

      const toggleBtn = _makeBtn(
        member.active ? 'Desactivar' : 'Reactivar',
        'btn-sm btn-outline',
        async () => {
          toggleBtn.disabled = true;
          try {
            await _staffFetch(`/${member.id}`, { method: 'PUT', body: JSON.stringify({ active: !member.active }) });
            await _reloadRoster(self);
          } catch (e) { alert(e.message); toggleBtn.disabled = false; }
        },
      );
      actRow.appendChild(toggleBtn);

      const delBtn = _makeBtn('🗑', 'btn-sm btn-danger', async () => {
        if (!confirm(`¿Eliminar permanentemente a ${member.name}? Esta acción no se puede deshacer.`)) return;
        delBtn.disabled = true;
        try {
          await _staffFetch(`/${member.id}`, { method: 'DELETE' });
          await _reloadRoster(self);
        } catch (e) { alert(e.message); delBtn.disabled = false; }
      });
      delBtn.title = 'Eliminar empleado';
      actRow.appendChild(delBtn);

      card.appendChild(actRow);
      grid.appendChild(card);
    });

    el.appendChild(grid);
  }

  // ── Share portal link
  const shareBar = document.createElement('div');
  shareBar.style.cssText = 'margin-top:1.75rem;background:#f8f8f5;border:1px dashed #ccc;border-radius:13px;padding:1rem 1.25rem;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;';

  const shareInfo = document.createElement('div');
  const shareTitle = document.createElement('div');
  shareTitle.textContent = '🔗 Portal de acceso para tu equipo';
  shareTitle.style.cssText = 'font-size:13px;font-weight:700;color:#555;';
  const shareDesc = document.createElement('div');
  shareDesc.textContent = 'Comparte este enlace para que tu equipo ingrese con su nombre y contraseña.';
  shareDesc.style.cssText = 'font-size:12px;color:#999;margin-top:2px;';
  shareInfo.appendChild(shareTitle);
  shareInfo.appendChild(shareDesc);

  const copyBtn = document.createElement('button');
  copyBtn.textContent = 'Copiar enlace';
  copyBtn.style.cssText = 'padding:8px 16px;background:#1D9E75;color:#fff;border:none;border-radius:9px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;flex-shrink:0;';
  copyBtn.addEventListener('click', () => {
    const rest = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    const rid  = rest.branch_id || '';
    const url  = window.location.origin + '/staff' + (rid ? '?r=' + rid : '');
    navigator.clipboard.writeText(url).then(() => {
      copyBtn.textContent = '✓ ¡Copiado!';
      setTimeout(() => { copyBtn.textContent = 'Copiar enlace'; }, 2200);
    }).catch(() => {
      prompt('Copia este enlace para tu equipo:', url);
    });
  });

  shareBar.appendChild(shareInfo);
  shareBar.appendChild(copyBtn);
  el.appendChild(shareBar);
}


// ── Shifts tab ────────────────────────────────────────────────────────────────

function _renderShiftsTab(state, el, self) {
  // Clock-in/out panel
  const panel = document.createElement('div');
  panel.style.cssText = 'background:#f8f8f5;border:1px solid #e0e0d8;border-radius:12px;padding:1rem 1.25rem;margin-bottom:1.25rem;';

  const panelTitle = document.createElement('div');
  panelTitle.textContent = 'Registrar entrada / salida';
  panelTitle.style.cssText = 'font-size:14px;font-weight:600;margin-bottom:.75rem;';
  panel.appendChild(panelTitle);

  const activeStaff = state.staff.filter(s => s.active);
  if (!activeStaff.length) {
    const msg = document.createElement('div');
    msg.className   = 'empty-state';
    msg.textContent = 'No hay empleados activos. Agrega empleados en la pestaña Roster.';
    panel.appendChild(msg);
    el.appendChild(panel);
  } else {
    const staffSel = _makeSelect(
      activeStaff.map(s => [s.id, `${s.name} (${_ROLE_LABELS[s.role] || s.role})`]),
    );

    const errMsg = document.createElement('div');
    errMsg.style.cssText = 'color:#C0392B;font-size:12px;margin-top:6px;min-height:16px;';

    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:8px;margin-top:.75rem;';

    const ciBtn = _makeBtn('▶ Entrada', 'btn-sm btn-primary', async () => {
      errMsg.textContent = '';
      ciBtn.disabled = true;
      try {
        await _staffFetch('/clock-in', { method: 'POST', body: JSON.stringify({ staff_id: staffSel.value }) });
        await _reloadShifts(self);
      } catch (e) {
        errMsg.textContent = e.message;
      } finally {
        ciBtn.disabled = false;
      }
    });

    const coBtn = _makeBtn('■ Salida', 'btn-sm btn-outline', async () => {
      errMsg.textContent = '';
      coBtn.disabled = true;
      try {
        await _staffFetch('/clock-out', { method: 'POST', body: JSON.stringify({ staff_id: staffSel.value }) });
        await _reloadShifts(self);
      } catch (e) {
        errMsg.textContent = e.message;
      } finally {
        coBtn.disabled = false;
      }
    });

    btnRow.appendChild(ciBtn);
    btnRow.appendChild(coBtn);
    panel.appendChild(staffSel);
    panel.appendChild(btnRow);
    panel.appendChild(errMsg);
    el.appendChild(panel);
  }

  // Open shifts
  const openTitle = document.createElement('div');
  openTitle.textContent = 'Turnos abiertos ahora';
  openTitle.style.cssText = 'font-size:14px;font-weight:600;margin-bottom:.75rem;';
  el.appendChild(openTitle);

  if (!state.shifts.length) {
    const empty = document.createElement('div');
    empty.className   = 'empty-state';
    empty.textContent = 'No hay turnos abiertos en este momento.';
    el.appendChild(empty);
    return;
  }

  const tbl = document.createElement('table');
  tbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;';
  const thead = document.createElement('thead');
  const hrow  = document.createElement('tr');
  ['Empleado', 'Rol', 'Entrada', 'Tiempo'].forEach(h => {
    const th = document.createElement('th');
    th.textContent = h;
    th.style.cssText = 'text-align:left;padding:8px 10px;border-bottom:1px solid #e0e0d8;color:#888;font-weight:500;';
    hrow.appendChild(th);
  });
  thead.appendChild(hrow);
  tbl.appendChild(thead);

  const tbody = document.createElement('tbody');
  const now   = Date.now();
  state.shifts.forEach(sh => {
    const tr    = document.createElement('tr');
    const tdN   = document.createElement('td');
    const tdR   = document.createElement('td');
    const tdIn  = document.createElement('td');
    const tdDur = document.createElement('td');
    tdN.textContent  = sh.staff_name;
    tdR.textContent  = _ROLE_LABELS[sh.staff_role] || sh.staff_role;
    tdIn.textContent = new Date(sh.clock_in).toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
    const diffH = (now - new Date(sh.clock_in).getTime()) / 3600000;
    tdDur.textContent = diffH < 1
      ? Math.round(diffH * 60) + ' min'
      : diffH.toFixed(1) + ' h';
    tdDur.style.cssText = 'color:#1D9E75;font-weight:600;';
    [tdN, tdR, tdIn, tdDur].forEach(td => {
      td.style.padding = '9px 10px';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);

  const tableCard = document.createElement('div');
  tableCard.className    = 'card';
  tableCard.style.overflowX = 'auto';
  tableCard.appendChild(tbl);
  el.appendChild(tableCard);
}


// ── Tips tab ──────────────────────────────────────────────────────────────────

function _renderTipsTab(state, el, self) {
  const formWrap = document.createElement('div');
  formWrap.style.cssText = 'background:#f8f8f5;border:1px solid #e0e0d8;border-radius:12px;padding:1rem 1.25rem;margin-bottom:1.25rem;';

  const formTitle = document.createElement('div');
  formTitle.textContent = 'Corte de propinas';
  formTitle.style.cssText = 'font-size:14px;font-weight:600;margin-bottom:.75rem;';
  formWrap.appendChild(formTitle);

  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const now = new Date();

  const toLocalInput = d => {
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  const fromIn  = _makeInput('Inicio del período', 'datetime-local', toLocalInput(todayStart));
  const toIn    = _makeInput('Fin del período',    'datetime-local', toLocalInput(now));
  const totalIn = _makeInput('Total propinas ($)',  'number');
  totalIn.min   = '0';
  totalIn.step  = '0.01';

  const row1 = document.createElement('div');
  row1.style.cssText = _rowStyle();
  row1.appendChild(fromIn);
  row1.appendChild(toIn);
  row1.appendChild(totalIn);
  formWrap.appendChild(row1);

  const errMsg = document.createElement('div');
  errMsg.style.cssText = 'color:#C0392B;font-size:12px;margin-top:4px;min-height:16px;';
  formWrap.appendChild(errMsg);

  const cutBtn = _makeBtn('Calcular y guardar corte', 'btn-sm btn-primary', async () => {
    errMsg.textContent = '';
    const total = parseFloat(totalIn.value);
    if (!fromIn.value || !toIn.value) { errMsg.textContent = 'Selecciona el período.'; return; }
    if (isNaN(total) || total < 0)    { errMsg.textContent = 'Ingresa el total de propinas.'; return; }

    cutBtn.disabled    = true;
    cutBtn.textContent = 'Calculando...';
    try {
      const data = await _staffFetch('/tip-cut', {
        method: 'POST',
        body: JSON.stringify({
          period_start: new Date(fromIn.value).toISOString(),
          period_end:   new Date(toIn.value).toISOString(),
          total_tips:   total,
        }),
      });
      self.setState({ tipPreview: data.preview });
    } catch (e) {
      errMsg.textContent = e.message;
    } finally {
      cutBtn.disabled    = false;
      cutBtn.textContent = 'Calcular y guardar corte';
    }
  });
  formWrap.appendChild(cutBtn);
  el.appendChild(formWrap);

  const preview = state.tipPreview;
  if (preview && preview.entries && preview.entries.length) {
    const resWrap = document.createElement('div');
    resWrap.className = 'card';

    const resTitle = document.createElement('div');
    resTitle.textContent = 'Distribución calculada';
    resTitle.style.cssText = 'font-size:14px;font-weight:600;margin-bottom:.75rem;';
    resWrap.appendChild(resTitle);

    const tbl = document.createElement('table');
    tbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;';
    const thead = document.createElement('thead');
    const hrow  = document.createElement('tr');
    ['Empleado', 'Rol', 'Horas', '% Rol', 'Monto'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      th.style.cssText = 'text-align:left;padding:8px 10px;border-bottom:1px solid #e0e0d8;color:#888;font-weight:500;';
      hrow.appendChild(th);
    });
    thead.appendChild(hrow);
    tbl.appendChild(thead);

    const tbody = document.createElement('tbody');
    preview.entries.forEach(e => {
      const tr = document.createElement('tr');
      [
        e.name,
        _ROLE_LABELS[e.role] || e.role,
        e.hours.toFixed(1) + ' h',
        e.pct + '%',
        '$' + e.amount.toLocaleString('es-CO', { minimumFractionDigits: 2 }),
      ].forEach(txt => {
        const td = document.createElement('td');
        td.textContent   = txt;
        td.style.padding = '9px 10px';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    resWrap.appendChild(tbl);

    const totals = document.createElement('div');
    totals.style.cssText = 'margin-top:.75rem;font-size:12px;color:#888;display:flex;gap:16px;';
    const alloc   = document.createElement('span');
    alloc.textContent = 'Distribuido: $' + preview.total_allocated.toLocaleString('es-CO', { minimumFractionDigits: 2 });
    const unalloc = document.createElement('span');
    unalloc.textContent = 'Sin asignar: $' + preview.total_unallocated.toLocaleString('es-CO', { minimumFractionDigits: 2 });
    totals.appendChild(alloc);
    totals.appendChild(unalloc);
    resWrap.appendChild(totals);
    el.appendChild(resWrap);
  }
}


// ── Data reload helpers ───────────────────────────────────────────────────────

async function _reloadRoster(self) {
  const [rosterData, shiftsData] = await Promise.all([
    _staffFetch(''),
    _staffFetch('/open-shifts'),
  ]);
  self.setState({ staff: rosterData.staff, shifts: shiftsData.shifts, loading: false });
}

async function _reloadShifts(self) {
  const data = await _staffFetch('/open-shifts');
  self.setState({ shifts: data.shifts, loading: false });
}


// ── Main StaffSection component ───────────────────────────────────────────────

const StaffSection = MesioComponent({
  state: {
    loading:    true,
    staff:      [],
    shifts:     [],
    tipPreview: null,
    tab:        'roster',
    filter:     'all',
    search:     '',
    error:      null,
  },

  render(state, el) {
    el.textContent = '';

    if (state.loading) {
      const msg = document.createElement('div');
      msg.className   = 'empty-state';
      msg.textContent = 'Cargando equipo...';
      el.appendChild(msg);
      return;
    }

    if (state.error) {
      const msg = document.createElement('div');
      msg.className   = 'empty-state';
      msg.textContent = state.error.includes('403') || state.error.toLowerCase().includes('módulo')
        ? 'El módulo Staff & Propinas no está activo para este restaurante.'
        : `Error al cargar el equipo: ${state.error}`;
      el.appendChild(msg);
      return;
    }

    // Tab bar
    const tabBar = document.createElement('div');
    tabBar.style.cssText = 'display:flex;gap:2px;margin-bottom:1.5rem;border-bottom:1px solid #e0e0d8;';

    [
      ['roster', '👥  Roster'],
      ['shifts', '⏱  Turnos'],
      ['tips',   '💸  Propinas'],
    ].forEach(([id, label]) => {
      const btn = document.createElement('button');
      btn.textContent = label;
      const active = state.tab === id;
      btn.style.cssText = `padding:9px 20px;border:none;background:none;cursor:pointer;font-size:13px;font-weight:${active ? '600' : '400'};
        color:${active ? '#1D9E75' : '#666'};border-bottom:2px solid ${active ? '#1D9E75' : 'transparent'};
        margin-bottom:-1px;transition:color .15s;`;
      btn.addEventListener('click', () => StaffSection.setState({ tab: id }));
      tabBar.appendChild(btn);
    });
    el.appendChild(tabBar);

    const content = document.createElement('div');
    if (state.tab === 'roster') _renderRosterTab(state, content, StaffSection);
    if (state.tab === 'shifts') _renderShiftsTab(state, content, StaffSection);
    if (state.tab === 'tips')   _renderTipsTab(state, content, StaffSection);
    el.appendChild(content);
  },

  async onMount(self) {
    try {
      // ⬇️ Carga las sucursales en el select si es Dueño
      const role = (localStorage.getItem('rb_role') || '').toLowerCase();
      if (role.includes('owner')) {
          await _loadStaffBranchesSelect();
      }
      
      const [rosterData, shiftsData] = await Promise.all([
        _staffFetch(''),
        _staffFetch('/open-shifts'),
      ]);
      self.setState({ staff: rosterData.staff, shifts: shiftsData.shifts, loading: false });
    } catch (err) {
      self.setState({ loading: false, error: err.message });
    }
  },
});


// ─────────────────────────────────────────────────────────────────────────────
// loadStaffSection — called by dashboard-core.js when the user navigates to
// the 'staff' section. Mounts StaffSection into #staff-component on first
// visit; subsequent calls refresh all data.
// ─────────────────────────────────────────────────────────────────────────────
let _staffMounted = false;

function loadStaffSection() {
  const el = document.getElementById('staff-component');
  if (!el) return;

  if (!_staffMounted) {
    _staffMounted = true;
    StaffSection.mount('#staff-component');
  } else {
    _reloadRoster(StaffSection);
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// loadLoyaltySection — called by dashboard-core.js when the user navigates to
// the 'loyalty' section. Loads top customers on each visit.
// ─────────────────────────────────────────────────────────────────────────────
function loadLoyaltySection() {
  _loadLoyaltyStats();
}

async function _loadLoyaltyStats() {
  const el = document.getElementById('loyalty-stats-body');
  if (!el) return;
  try {
    const r = await fetch('/api/loyalty/stats?limit=50', { headers: window._dashHeaders });
    if (!r.ok) { el.innerHTML = '<div style="color:#C0392B;font-size:13px;padding:1rem;">Error al cargar datos.</div>'; return; }
    const data = await r.json();
    const rows = data.customers || [];
    if (!rows.length) {
      el.innerHTML = '<div style="text-align:center;padding:2rem;color:#aaa;font-size:13px;">Aún no hay clientes con puntos.</div>';
      return;
    }
    const feats = (window._dashRestaurant || {}).features || {};
    const pointVal = feats.loyalty_point_value_cop || 10;
    const locale   = (window._dashRestaurant || {}).locale   || 'es-CO';
    const currency = (window._dashRestaurant || {}).currency || 'COP';
    const fmtCur = (v) => new Intl.NumberFormat(locale, { style:'currency', currency, minimumFractionDigits: 0 }).format(v);
    el.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="border-bottom:1px solid #e0e0d8;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;">
            <th style="text-align:left;padding:6px 8px;">#</th>
            <th style="text-align:left;padding:6px 8px;">Teléfono</th>
            <th style="text-align:right;padding:6px 8px;">Puntos</th>
            <th style="text-align:right;padding:6px 8px;">Equivalencia</th>
            <th style="text-align:right;padding:6px 8px;">Total ganado</th>
            <th style="text-align:right;padding:6px 8px;">Total canjeado</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((c, i) => `
            <tr style="border-bottom:0.5px solid #f0f0e8;">
              <td style="padding:8px;color:#aaa;">${i + 1}</td>
              <td style="padding:8px;font-weight:500;">${c.phone}</td>
              <td style="padding:8px;text-align:right;font-weight:700;color:#1D9E75;">${c.points_balance}</td>
              <td style="padding:8px;text-align:right;color:#555;">${fmtCur(c.points_balance * pointVal)}</td>
              <td style="padding:8px;text-align:right;color:#888;">${c.total_earned}</td>
              <td style="padding:8px;text-align:right;color:#888;">${c.total_redeemed || 0}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  } catch(e) {
    el.innerHTML = '<div style="color:#C0392B;font-size:13px;padding:1rem;">Error inesperado.</div>';
  }
}

async function loyaltyLookup() {
  const phone = document.getElementById('loyalty-phone-input').value.trim();
  const out   = document.getElementById('loyalty-lookup-result');
  if (!phone) return;
  out.textContent = 'Buscando...';
  try {
    const r = await fetch(`/api/loyalty/balance?phone=${encodeURIComponent(phone)}`, { headers: window._dashHeaders });
    if (r.status === 404) { out.innerHTML = '<span style="color:#C0392B;">Cliente sin registro de fidelización.</span>'; return; }
    if (!r.ok) { out.innerHTML = '<span style="color:#C0392B;">Error al consultar.</span>'; return; }
    const d = await r.json();
    const feats    = (window._dashRestaurant || {}).features || {};
    const locale   = (window._dashRestaurant || {}).locale   || 'es-CO';
    const currency = (window._dashRestaurant || {}).currency || 'COP';
    const fmtCur = (v) => new Intl.NumberFormat(locale, { style:'currency', currency, minimumFractionDigits: 0 }).format(v);
    out.innerHTML = `
      <div style="background:#E1F5EE;border-radius:8px;padding:10px 14px;display:inline-flex;gap:24px;align-items:center;">
        <div><div style="font-size:11px;color:#888;">Puntos actuales</div><div style="font-size:20px;font-weight:700;color:#0F6E56;">${d.puntos_actuales}</div></div>
        <div><div style="font-size:11px;color:#888;">Equivalencia</div><div style="font-size:16px;font-weight:600;color:#1D9E75;">${fmtCur(d.equivalencia_cop)}</div></div>
      </div>`;
  } catch(e) {
    out.innerHTML = '<span style="color:#C0392B;">Error inesperado.</span>';
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Initialize on DOMContentLoaded
// ─────────────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Mount connection status — targets #conn-dot and #conn-text inside #live-badge
  ConnectionStatus.mount('#live-badge');
});

// ── FUNCIONES GLOBALES PARA EL DROPDOWN DE SUCURSALES ──

async function _loadStaffBranchesSelect() {
  const select = document.getElementById('staff-branch-select');
  const adminBtn = document.getElementById('btn-staff-add-admin');
  if (!select) return;
  
  try {
      const r = await fetch('/api/team/branches', { headers: { Authorization: `Bearer ${localStorage.getItem('rb_token')}` } });
      if (r.ok) {
          const data = await r.json();
          const branches = data.branches || [];
          
          select.innerHTML = '<option value="matriz">🏠 Casa Matriz</option>';
          branches.forEach(b => {
              const opt = document.createElement('option');
              opt.value = b.id;
              opt.textContent = `📍 ${b.name}`;
              select.appendChild(opt);
          });
          
          select.style.display = 'block';
          if (adminBtn) adminBtn.style.display = 'block';
      }
  } catch(e) { console.error('Error cargando sucursales para staff', e); }
}

window.changeStaffBranch = async function() {
  // Al cambiar el select, el _apiHeaders mandará el nuevo X-Branch-ID
  // Recargamos el componente para que pida los datos de la nueva sucursal
  StaffSection.setState({ loading: true });
  try {
      const [rosterData, shiftsData] = await Promise.all([
          _staffFetch(''),
          _staffFetch('/open-shifts'),
      ]);
      StaffSection.setState({ staff: rosterData.staff, shifts: shiftsData.shifts, loading: false });
  } catch (err) {
      StaffSection.setState({ loading: false, error: err.message });
  }
};

window.openStaffAdminModal = function() {
  const select = document.getElementById('staff-branch-select');
  const val = select.value;
  const branchName = select.options[select.selectedIndex].text.replace('🏠 ', '').replace('📍 ', '');
  const branchId = val === 'matriz' ? null : parseInt(val);

  // Creamos el modal dinámicamente para asegurar que siempre funcione
  const old = document.getElementById('_admin-invite-modal');
  if (old) old.remove();

  const overlay = document.createElement('div');
  overlay.id = '_admin-invite-modal';
  overlay.style.cssText = 'display:flex;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:2000;align-items:center;justify-content:center;';

  const box = document.createElement('div');
  box.style.cssText = 'background:#fff;border-radius:16px;padding:2rem;width:460px;max-width:92vw;max-height:90vh;overflow-y:auto;box-shadow:0 24px 64px rgba(0,0,0,0.2);';

  box.innerHTML = `
      <div style="font-size:16px;font-weight:600;margin-bottom:1.25rem;">Agregar administrador a <span style="color:#1D9E75;">${branchName}</span></div>
      <div style="display:flex;flex-direction:column;gap:10px;">
        <input id="inv-admin-username" placeholder="Email o usuario" style="padding:9px 12px;border:1px solid #e0e0d8;border-radius:8px;font-size:13px;">
        <input id="inv-admin-password" type="password" placeholder="Contraseña" style="padding:9px 12px;border:1px solid #e0e0d8;border-radius:8px;font-size:13px;">
        <input id="inv-admin-phone" placeholder="Teléfono (opcional)" style="padding:9px 12px;border:1px solid #e0e0d8;border-radius:8px;font-size:13px;">
        
        <div style="font-size:13px;color:#555;margin-top:4px;font-weight:600;">Rol de acceso al dashboard:</div>
        
        <div style="margin-bottom:4px;padding:10px;border:1px solid #1D9E75;background:#E1F5EE;border-radius:11px;cursor:default;">
          <div style="font-size:22px;margin-bottom:2px;">🛡️</div>
          <div style="font-weight:600;font-size:13px;color:#111;">Administrador</div>
          <div style="font-size:11px;color:#888;margin-top:2px;">Control total del dashboard de esta sucursal.</div>
        </div>
      </div>
      <div id="inv-admin-error" style="color:#C0392B;font-size:12px;margin-top:10px;min-height:16px;"></div>
      <div style="display:flex;gap:8px;margin-top:1.25rem;">
        <button id="btn-inv-submit" style="flex:1;background:#1D9E75;color:#fff;border:none;padding:10px;border-radius:8px;font-size:13px;cursor:pointer;font-weight:500;">Crear administrador</button>
        <button id="btn-inv-cancel" style="padding:10px 16px;background:none;border:1px solid #e0e0d8;border-radius:8px;font-size:13px;cursor:pointer;color:#555;">Cancelar</button>
      </div>
  `;

  overlay.appendChild(box);
  document.body.appendChild(overlay);

  document.getElementById('btn-inv-cancel').onclick = () => overlay.remove();
  
  document.getElementById('btn-inv-submit').onclick = async () => {
    const username = document.getElementById('inv-admin-username').value.trim();
    const password = document.getElementById('inv-admin-password').value.trim();
    const phone    = document.getElementById('inv-admin-phone').value.trim();
    const errEl    = document.getElementById('inv-admin-error');

    if (!username || !password) { errEl.textContent = 'El usuario y la contraseña son obligatorios.'; return; }

    const btn = document.getElementById('btn-inv-submit');
    btn.disabled = true;
    btn.textContent = 'Creando...';

    try {
        // 🛡️ CORRECCIÓN CLAVE: Forzamos el Content-Type para que FastAPI entienda el JSON
        const headers = { 
            'Authorization': 'Bearer ' + localStorage.getItem('rb_token'), 
            'Content-Type': 'application/json' 
        };
        
        // 🛡️ Creamos el payload base
        const payload = { username, password, role: 'admin' };
            
        // 🛡️ Solo enviamos el branch_id si de verdad es un número (es decir, NO es la matriz)
        if (branchId) {
            payload.branch_id = branchId;
        }
        
        if (phone) {
            payload.phone = phone;
        }

        const r = await fetch('/api/team/invite', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify(payload),
        });
        
        if (r.ok) {
            document.getElementById('_admin-invite-modal').remove();
            alert('¡Administrador creado exitosamente!');
        } else {
            const e = await r.json();
            let errorMsg = 'No se pudo crear';
            
            if (e.detail) {
                if (Array.isArray(e.detail)) {
                    errorMsg = e.detail.map(err => `${err.loc[err.loc.length-1]}: ${err.msg}`).join(', ');
                } else {
                    errorMsg = e.detail;
                }
            }
            
            errEl.textContent = 'Error: ' + errorMsg;
            btn.disabled = false;
            btn.textContent = 'Crear administrador';
        }
    } catch(e) {
        errEl.textContent = 'Error de conexión';
        btn.disabled = false;
        btn.textContent = 'Crear administrador';
    }
  };
};