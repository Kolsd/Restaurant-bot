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
//     tab:        'roster' | 'shifts',
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
  
  // 🛡️ FIX: Usar el ID correcto del selector global
  const branchSelect = document.getElementById('global-branch-select'); 
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

async function _staffFetch(path, methodOrOpts = 'GET', body = null) {
  // Supports two call styles:
  //   _staffFetch(path, optsObject)          — legacy (opts spread)
  //   _staffFetch(path, 'POST', bodyObject)  — new explicit style
  let opts;
  if (methodOrOpts && typeof methodOrOpts === 'object') {
    // Legacy: second arg is a plain options object
    opts = { headers: _apiHeaders(), ...methodOrOpts };
  } else {
    // New: second arg is an HTTP method string
    opts = { headers: { ..._apiHeaders(), 'Content-Type': 'application/json' }, method: methodOrOpts };
    if (body !== null) opts.body = JSON.stringify(body);
  }
  const res = await fetch('/api/staff' + path, opts);
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new Error(errBody.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function _staffFmt(n) {
  const restData = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
  const locale   = restData.locale   || 'es-CO';
  const currency = restData.currency || 'COP';
  return new Intl.NumberFormat(locale, {
    style: 'currency', currency,
    minimumFractionDigits: ['COP','CLP','PYG','JPY'].includes(currency) ? 0 : 2,
  }).format(Number(n) || 0);
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

  const docLabel = document.createElement('div');
  docLabel.textContent = 'Número de documento (cédula / ID)';
  docLabel.style.cssText = 'font-size:12px;font-weight:700;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:0.04em;';
  box.appendChild(docLabel);
  const docIn = _makeInput('Ej: 1234567890');
  if (existing) docIn.value = existing.document_number || '';
  docIn.style.cssText += 'width:100%;box-sizing:border-box;margin-bottom:1rem;font-size:14px;padding:10px 12px;';
  box.appendChild(docIn);

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
      const name            = nameIn.value.trim();
      const pin             = pinIn.value.trim();
      const phone           = phoneIn.value.trim();
      const document_number = docIn.value.trim();

      if (!name) { errMsg.textContent = 'El nombre es obligatorio.'; return; }
      if (!isEdit && pin.length < 4) { errMsg.textContent = 'La contraseña debe tener al menos 4 caracteres.'; return; }
      if (currentRoles.size === 0) { errMsg.textContent = 'Añade al menos un rol.'; return; }

      submitBtn.disabled = true;
      submitBtn.textContent = isEdit ? 'Guardando...' : 'Creando...';
      try {
        const rolesArr = Array.from(currentRoles);
        if (isEdit) {
          const patch = { name, roles: rolesArr, role: rolesArr[0], phone, document_number };
          if (pin) patch.password = pin;
          await _staffFetch(`/${existing.id}`, { method: 'PUT', body: JSON.stringify(patch) });
        } else {
          await _staffFetch('', {
            method: 'POST',
            body: JSON.stringify({ name, role: rolesArr[0], roles: rolesArr, password: pin, phone, document_number }),
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


// ── Shifts editor ──────────────────────────────────────────────────────────────

const _DAY_NAMES = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'];

function _shiftsWeekStart(offset = 0) {
  const d = new Date();
  const dow = (d.getDay() + 6) % 7; // 0=Mon
  d.setDate(d.getDate() - dow + offset * 7);
  d.setHours(0,0,0,0);
  return d;
}

function _fmtDateShort(d) {
  return d.toLocaleDateString('es-CO', { day:'numeric', month:'short' });
}

async function _renderShiftsEditor(container) {
  container.textContent = '';

  // Module-level week offset state on the container element
  if (container._weekOffset === undefined) container._weekOffset = 0;
  if (container._selectedStaff === undefined) container._selectedStaff = new Set();

  const weekStart = _shiftsWeekStart(container._weekOffset);
  const days = Array.from({length:7}, (_, i) => {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + i);
    return d;
  });

  // ── Week navigation ──
  const navBar = document.createElement('div');
  navBar.style.cssText = 'display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;';

  const prevBtn = document.createElement('button');
  prevBtn.textContent = '← Anterior';
  prevBtn.style.cssText = 'padding:7px 13px;border:1.5px solid #e0e0d8;background:#f9f9f7;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;';
  prevBtn.addEventListener('click', () => { container._weekOffset--; container._selectedStaff.clear(); _renderShiftsEditor(container); });

  const weekLabel = document.createElement('span');
  weekLabel.style.cssText = 'font-weight:700;font-size:13px;min-width:190px;text-align:center;';
  weekLabel.textContent = _fmtDateShort(days[0]) + ' – ' + _fmtDateShort(days[6]);

  const nextBtn = document.createElement('button');
  nextBtn.textContent = 'Siguiente →';
  nextBtn.style.cssText = prevBtn.style.cssText;
  nextBtn.addEventListener('click', () => { container._weekOffset++; container._selectedStaff.clear(); _renderShiftsEditor(container); });

  const todayBtn = document.createElement('button');
  todayBtn.textContent = 'Hoy';
  todayBtn.style.cssText = 'padding:7px 13px;border:1.5px solid #e0e0d8;background:#18181B;color:#fff;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;';
  todayBtn.addEventListener('click', () => { container._weekOffset = 0; container._selectedStaff.clear(); _renderShiftsEditor(container); });

  const copyPrevBtn = document.createElement('button');
  copyPrevBtn.textContent = '📋 Copiar semana anterior';
  copyPrevBtn.style.cssText = 'padding:7px 13px;border:1.5px solid #e0e0d8;background:#f0f0ec;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;margin-left:auto;';
  copyPrevBtn.addEventListener('click', async () => {
    if (!confirm('¿Copiar los horarios de la semana anterior a esta semana?')) return;
    const prevDays = Array.from({length:7}, (_,i) => {
      const d = new Date(weekStart);
      d.setDate(d.getDate() - 7 + i);
      return d;
    });
    try {
      const prevSched = await _staffFetch(`/schedules?week_start=${prevDays[0].toISOString().slice(0,10)}`);
      const entries = (prevSched.schedules || []).map(s => ({
        staff_id: s.staff_id, day_of_week: s.day_of_week,
        start_time: s.start_time, end_time: s.end_time,
      }));
      if (!entries.length) { alert('No hay horarios en la semana anterior.'); return; }
      await _staffFetch('/schedules/bulk', 'POST', { entries });
      _renderShiftsEditor(container);
    } catch(e) { alert('Error: ' + e.message); }
  });

  navBar.appendChild(prevBtn);
  navBar.appendChild(weekLabel);
  navBar.appendChild(nextBtn);
  navBar.appendChild(todayBtn);
  navBar.appendChild(copyPrevBtn);
  container.appendChild(navBar);

  // ── Bulk action bar ──
  const bulkBar = document.createElement('div');
  bulkBar.id = 'shifts-bulk-bar';
  bulkBar.style.cssText = 'display:none;background:#E8F4FD;border:1.5px solid #93C5FD;border-radius:10px;padding:10px 14px;margin-bottom:12px;display:flex;align-items:center;gap:12px;';
  const bulkLabel = document.createElement('span');
  bulkLabel.style.cssText = 'font-size:13px;font-weight:600;color:#1D4ED8;flex:1;';
  const bulkApplyBtn = document.createElement('button');
  bulkApplyBtn.textContent = 'Aplicar turno';
  bulkApplyBtn.style.cssText = 'background:#1D4ED8;color:#fff;border:none;padding:7px 14px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;';
  const bulkClearBtn = document.createElement('button');
  bulkClearBtn.textContent = 'Limpiar selección';
  bulkClearBtn.style.cssText = 'background:#fff;border:1.5px solid #93C5FD;color:#1D4ED8;padding:7px 14px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;';
  bulkBar.appendChild(bulkLabel);
  bulkBar.appendChild(bulkApplyBtn);
  bulkBar.appendChild(bulkClearBtn);
  bulkBar.style.display = 'none';
  container.appendChild(bulkBar);

  const refreshBulkBar = () => {
    const n = container._selectedStaff.size;
    if (n > 0) {
      bulkBar.style.display = 'flex';
      bulkLabel.textContent = `${n} empleado${n>1?'s':''} seleccionado${n>1?'s':''}`;
    } else {
      bulkBar.style.display = 'none';
    }
  };

  bulkClearBtn.addEventListener('click', () => {
    container._selectedStaff.clear();
    container.querySelectorAll('.staff-row-check').forEach(cb => { cb.checked = false; });
    refreshBulkBar();
  });

  bulkApplyBtn.addEventListener('click', () => {
    _openBulkScheduleModal(container, [...container._selectedStaff], days, refreshBulkBar);
  });

  // ── Load data ──
  const loadingMsg = document.createElement('div');
  loadingMsg.className = 'empty-state';
  loadingMsg.textContent = 'Cargando horarios...';
  container.appendChild(loadingMsg);

  let staff = [], schedules = [], shifts = [];
  try {
    // date_to is start of Monday = end of Sunday (inclusive of full week)
    const weekEnd = new Date(days[6]);
    weekEnd.setDate(weekEnd.getDate() + 1);
    const [staffRes, schedRes, shiftsRes] = await Promise.all([
      _staffFetch(''),
      _staffFetch('/schedules'),
      _staffFetch(`/shifts?date_from=${days[0].toISOString()}&date_to=${weekEnd.toISOString()}`),
    ]);
    staff = (staffRes.staff || []).filter(s => s.active);
    schedules = schedRes.schedules || [];
    shifts = shiftsRes.shifts || [];
  } catch(e) {
    loadingMsg.textContent = 'Error al cargar: ' + e.message;
    return;
  }
  loadingMsg.remove();

  if (!staff.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = 'No hay empleados activos.';
    container.appendChild(empty);
    return;
  }

  // Build lookup maps
  const schedByStaffDay = {}; // `${staff_id}_${day_of_week}` -> schedule
  schedules.forEach(s => { schedByStaffDay[`${s.staff_id}_${s.day_of_week}`] = s; });

  const shiftsByStaffDay = {}; // `${staff_id}_${dateStr}` -> shift[]
  shifts.forEach(sh => {
    const d = new Date(sh.clock_in);
    const dayStr = d.toISOString().slice(0,10);
    const key = `${sh.staff_id}_${dayStr}`;
    shiftsByStaffDay[key] = shiftsByStaffDay[key] || [];
    shiftsByStaffDay[key].push(sh);
  });

  const today = new Date(); today.setHours(0,0,0,0);

  // ── Grid table ──
  const tableWrap = document.createElement('div');
  tableWrap.style.cssText = 'overflow-x:auto;border-radius:12px;border:1px solid #e0e0d8;';

  const tbl = document.createElement('table');
  tbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;min-width:680px;';

  // Header row
  const thead = document.createElement('thead');
  const hrow = document.createElement('tr');
  hrow.style.background = '#f9f9f7';

  // Checkbox + Employee column
  const thCheck = document.createElement('th');
  thCheck.style.cssText = 'padding:10px 8px;border-bottom:1px solid #e0e0d8;width:32px;';
  hrow.appendChild(thCheck);

  const thEmp = document.createElement('th');
  thEmp.textContent = 'Empleado';
  thEmp.style.cssText = 'text-align:left;padding:10px 12px;border-bottom:1px solid #e0e0d8;font-size:11px;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.05em;min-width:140px;position:sticky;left:32px;background:#f9f9f7;z-index:2;';
  hrow.appendChild(thEmp);

  days.forEach((d, i) => {
    const th = document.createElement('th');
    const isToday = d.getTime() === today.getTime();
    th.style.cssText = `text-align:center;padding:10px 6px;border-bottom:1px solid #e0e0d8;font-size:11px;font-weight:700;color:${isToday?'#6366F1':'#666'};text-transform:uppercase;letter-spacing:.05em;min-width:90px;${isToday?'background:#EEF2FF;':''}`;
    const dayName = document.createElement('div');
    dayName.textContent = _DAY_NAMES[i];
    const dayNum = document.createElement('div');
    dayNum.textContent = _fmtDateShort(d);
    dayNum.style.fontWeight = '600';
    dayNum.style.marginTop = '2px';
    th.appendChild(dayName);
    th.appendChild(dayNum);
    hrow.appendChild(th);
  });
  thead.appendChild(hrow);
  tbl.appendChild(thead);

  // Body rows
  const tbody = document.createElement('tbody');
  staff.forEach((s, rowIdx) => {
    const tr = document.createElement('tr');
    tr.style.cssText = `background:${rowIdx%2===0?'#fff':'#fafaf8'};`;

    // Checkbox
    const tdChk = document.createElement('td');
    tdChk.style.cssText = 'padding:8px;text-align:center;border-bottom:1px solid #f0f0ec;';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'staff-row-check';
    cb.style.cursor = 'pointer';
    cb.addEventListener('change', () => {
      if (cb.checked) container._selectedStaff.add(s.id);
      else container._selectedStaff.delete(s.id);
      refreshBulkBar();
    });
    tdChk.appendChild(cb);
    tr.appendChild(tdChk);

    // Employee name + role
    const tdEmp = document.createElement('td');
    tdEmp.style.cssText = 'padding:8px 12px;border-bottom:1px solid #f0f0ec;position:sticky;left:32px;background:inherit;z-index:1;';
    const avatar = document.createElement('div');
    avatar.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:50%;background:#E0E7FF;color:#6366F1;font-weight:700;font-size:12px;margin-right:8px;vertical-align:middle;flex-shrink:0;';
    avatar.textContent = (s.name || '?')[0].toUpperCase();
    const nameSpan = document.createElement('span');
    nameSpan.textContent = s.name || '';
    nameSpan.style.cssText = 'font-weight:600;font-size:13px;';
    const rolePill = document.createElement('div');
    rolePill.textContent = _ROLE_LABELS[s.role] || s.role;
    rolePill.style.cssText = 'font-size:10px;color:#888;margin-top:2px;padding-left:36px;';
    const nameWrap = document.createElement('div');
    nameWrap.style.cssText = 'display:flex;align-items:center;';
    nameWrap.appendChild(avatar);
    nameWrap.appendChild(nameSpan);
    tdEmp.appendChild(nameWrap);
    tdEmp.appendChild(rolePill);
    tr.appendChild(tdEmp);

    // Day cells
    days.forEach((d, dayIdx) => {
      const td = document.createElement('td');
      const isPast = d < today;
      const isToday = d.getTime() === today.getTime();
      td.style.cssText = `padding:6px 8px;border-bottom:1px solid #f0f0ec;text-align:center;vertical-align:middle;cursor:pointer;${isToday?'background:#FAFAFE;':''}`;
      td.setAttribute('aria-label', `${_DAY_NAMES[dayIdx]} ${_fmtDateShort(d)}, ${s.name}`);

      const sched = schedByStaffDay[`${s.staff_id}_${dayIdx}`] || schedByStaffDay[`${s.id}_${dayIdx}`];

      if (sched) {
        const pill = document.createElement('div');
        pill.style.cssText = 'display:inline-block;background:#E0E7FF;color:#4F46E5;border-radius:12px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;white-space:nowrap;';
        // sched.start_time / end_time may be "HH:MM:SS" — slice to HH:MM
        const st = (sched.start_time || '').slice(0,5);
        const et = (sched.end_time   || '').slice(0,5);
        pill.textContent = st + '–' + et;
        td.appendChild(pill);

        // Compliance badge for past days
        if (isPast) {
          const dayStr = d.toISOString().slice(0,10);
          const dayShifts = shiftsByStaffDay[`${s.staff_id}_${dayStr}`] || shiftsByStaffDay[`${s.id}_${dayStr}`] || [];
          const dot = document.createElement('span');
          dot.style.cssText = 'display:inline-block;width:7px;height:7px;border-radius:50%;margin-left:5px;vertical-align:middle;';
          if (dayShifts.length > 0) {
            const hadLate = dayShifts.some(sh => sh.tardiness_minutes > 0 || (sh.deductions && sh.deductions.length));
            dot.style.background = hadLate ? '#F59E0B' : '#22C55E';
            dot.title = hadLate ? 'Tardanza detectada' : 'Entrada a tiempo';
          } else {
            dot.style.background = '#EF4444';
            dot.title = 'No se presentó';
          }
          td.appendChild(dot);
        }

        td.addEventListener('click', () => _openScheduleModal({ mode:'edit', schedule: sched, staffId: s.id || s.staff_id, staffName: s.name, dayIdx, container }));
      } else {
        const plusIcon = document.createElement('span');
        plusIcon.textContent = '+';
        plusIcon.style.cssText = 'color:#ccc;font-size:18px;font-weight:300;line-height:1;';
        td.appendChild(plusIcon);
        td.addEventListener('mouseenter', () => { plusIcon.style.color = '#6366F1'; });
        td.addEventListener('mouseleave', () => { plusIcon.style.color = '#ccc'; });
        td.addEventListener('click', () => _openScheduleModal({ mode:'create', staffId: s.id, staffName: s.name, dayIdx, container }));
      }

      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);
  tableWrap.appendChild(tbl);
  container.appendChild(tableWrap);

  // Empty state if no staff
  if (!staff.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = 'No hay empleados activos. Agrégalos en la pestaña Equipo.';
    container.appendChild(empty);
  }
}

function _openScheduleModal({ mode, schedule, staffId, staffName, dayIdx, container }) {
  const existing = document.getElementById('shifts-modal-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'shifts-modal-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center;';

  const modal = document.createElement('div');
  modal.style.cssText = 'background:#fff;border-radius:14px;padding:1.5rem;width:320px;max-width:95vw;box-shadow:0 20px 60px rgba(0,0,0,.2);';

  const title = document.createElement('h3');
  title.style.cssText = 'font-size:15px;font-weight:700;margin-bottom:1rem;';
  const dayLabel = _DAY_NAMES[dayIdx];
  title.textContent = (mode === 'create' ? 'Agregar turno — ' : 'Editar turno — ') + dayLabel;
  modal.appendChild(title);

  const nameEl = document.createElement('div');
  nameEl.style.cssText = 'font-size:13px;color:#666;margin-bottom:1rem;';
  nameEl.textContent = staffName;
  modal.appendChild(nameEl);

  const mkField = (lbl, type, val) => {
    const wrap = document.createElement('div');
    wrap.style.marginBottom = '12px';
    const l = document.createElement('label');
    l.textContent = lbl;
    l.style.cssText = 'display:block;font-size:11px;font-weight:700;color:#666;margin-bottom:4px;text-transform:uppercase;';
    const inp = document.createElement('input');
    inp.type = type;
    inp.value = val || '';
    inp.style.cssText = 'width:100%;box-sizing:border-box;padding:9px 12px;border:1.5px solid #e0e0d8;border-radius:8px;font-size:13px;';
    wrap.appendChild(l); wrap.appendChild(inp);
    return { wrap, inp };
  };

  const startField = mkField('Hora inicio', 'time', schedule ? (schedule.start_time || '').slice(0,5) : '09:00');
  const endField   = mkField('Hora fin',    'time', schedule ? (schedule.end_time   || '').slice(0,5) : '17:00');
  modal.appendChild(startField.wrap);
  modal.appendChild(endField.wrap);

  const errMsg = document.createElement('div');
  errMsg.style.cssText = 'color:#EF4444;font-size:12px;min-height:16px;margin-bottom:8px;';
  modal.appendChild(errMsg);

  const btnRow = document.createElement('div');
  btnRow.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';

  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancelar';
  cancelBtn.style.cssText = 'padding:9px 16px;border:1.5px solid #e0e0d8;background:#f9f9f7;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;';
  cancelBtn.addEventListener('click', () => overlay.remove());

  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Guardar';
  saveBtn.style.cssText = 'padding:9px 16px;background:#18181B;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;';
  saveBtn.addEventListener('click', async () => {
    const st = startField.inp.value;
    const et = endField.inp.value;
    if (!st || !et) { errMsg.textContent = 'Completa los horarios.'; return; }
    if (st >= et)   { errMsg.textContent = 'La hora de inicio debe ser antes del fin.'; return; }
    saveBtn.disabled = true;
    try {
      await _staffFetch('/schedules', 'POST', { staff_id: staffId, day_of_week: dayIdx, start_time: st, end_time: et });
      overlay.remove();
      _renderShiftsEditor(container);
    } catch(e) {
      errMsg.textContent = e.message;
      saveBtn.disabled = false;
    }
  });

  btnRow.appendChild(cancelBtn);

  if (mode === 'edit' && schedule) {
    const delBtn = document.createElement('button');
    delBtn.textContent = 'Eliminar';
    delBtn.style.cssText = 'padding:9px 16px;background:#FEE2E2;color:#DC2626;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;';
    delBtn.addEventListener('click', async () => {
      if (!confirm('¿Eliminar este horario?')) return;
      delBtn.disabled = true;
      try {
        await _staffFetch(`/schedules/${schedule.id}`, 'DELETE');
        overlay.remove();
        _renderShiftsEditor(container);
      } catch(e) {
        errMsg.textContent = e.message;
        delBtn.disabled = false;
      }
    });
    btnRow.appendChild(delBtn);
  }

  btnRow.appendChild(saveBtn);
  modal.appendChild(btnRow);

  overlay.appendChild(modal);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);

  startField.inp.focus();
}

function _openBulkScheduleModal(container, staffIds, days, refreshBulkBar) {
  const existing = document.getElementById('shifts-bulk-modal-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'shifts-bulk-modal-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center;';

  const modal = document.createElement('div');
  modal.style.cssText = 'background:#fff;border-radius:14px;padding:1.5rem;width:360px;max-width:95vw;box-shadow:0 20px 60px rgba(0,0,0,.2);';

  const title = document.createElement('h3');
  title.style.cssText = 'font-size:15px;font-weight:700;margin-bottom:.5rem;';
  title.textContent = 'Aplicar turno masivo';
  modal.appendChild(title);

  const sub = document.createElement('div');
  sub.style.cssText = 'font-size:13px;color:#666;margin-bottom:1rem;';
  sub.textContent = `${staffIds.length} empleado${staffIds.length>1?'s':''} seleccionado${staffIds.length>1?'s':''}`;
  modal.appendChild(sub);

  // Day checkboxes
  const daysWrap = document.createElement('div');
  daysWrap.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:1rem;';
  const dayCheckboxes = days.map((d, i) => {
    const lbl = document.createElement('label');
    lbl.style.cssText = 'display:flex;align-items:center;gap:4px;font-size:12px;font-weight:600;cursor:pointer;padding:5px 9px;border:1.5px solid #e0e0d8;border-radius:8px;';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    lbl.appendChild(cb);
    const txt = document.createElement('span');
    txt.textContent = _DAY_NAMES[i];
    lbl.appendChild(txt);
    daysWrap.appendChild(lbl);
    return { cb, dayIdx: i };
  });
  modal.appendChild(daysWrap);

  const mkField = (lbl, type, val) => {
    const wrap = document.createElement('div');
    wrap.style.marginBottom = '12px';
    const l = document.createElement('label');
    l.textContent = lbl;
    l.style.cssText = 'display:block;font-size:11px;font-weight:700;color:#666;margin-bottom:4px;text-transform:uppercase;';
    const inp = document.createElement('input');
    inp.type = type; inp.value = val || '';
    inp.style.cssText = 'width:100%;box-sizing:border-box;padding:9px 12px;border:1.5px solid #e0e0d8;border-radius:8px;font-size:13px;';
    wrap.appendChild(l); wrap.appendChild(inp);
    return { wrap, inp };
  };
  const startField = mkField('Hora inicio', 'time', '09:00');
  const endField   = mkField('Hora fin',    'time', '17:00');
  modal.appendChild(startField.wrap);
  modal.appendChild(endField.wrap);

  const errMsg = document.createElement('div');
  errMsg.style.cssText = 'color:#EF4444;font-size:12px;min-height:16px;margin-bottom:8px;';
  modal.appendChild(errMsg);

  const btnRow = document.createElement('div');
  btnRow.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';

  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancelar';
  cancelBtn.style.cssText = 'padding:9px 16px;border:1.5px solid #e0e0d8;background:#f9f9f7;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;';
  cancelBtn.addEventListener('click', () => overlay.remove());

  const applyBtn = document.createElement('button');
  applyBtn.textContent = 'Aplicar a todos';
  applyBtn.style.cssText = 'padding:9px 16px;background:#18181B;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;';
  applyBtn.addEventListener('click', async () => {
    const st = startField.inp.value;
    const et = endField.inp.value;
    if (!st || !et) { errMsg.textContent = 'Completa los horarios.'; return; }
    if (st >= et)   { errMsg.textContent = 'La hora de inicio debe ser antes del fin.'; return; }
    const selectedDays = dayCheckboxes.filter(d => d.cb.checked).map(d => d.dayIdx);
    if (!selectedDays.length) { errMsg.textContent = 'Selecciona al menos un día.'; return; }
    applyBtn.disabled = true;
    const entries = [];
    staffIds.forEach(sid => {
      selectedDays.forEach(dayIdx => {
        entries.push({ staff_id: sid, day_of_week: dayIdx, start_time: st, end_time: et });
      });
    });
    try {
      await _staffFetch('/schedules/bulk', 'POST', { entries });
      overlay.remove();
      container._selectedStaff.clear();
      refreshBulkBar();
      _renderShiftsEditor(container);
    } catch(e) {
      errMsg.textContent = e.message;
      applyBtn.disabled = false;
    }
  });

  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(applyBtn);
  modal.appendChild(btnRow);

  overlay.appendChild(modal);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
}



// Tips are now calculated automatically — see PayrollSection


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


// ── Payroll tab ───────────────────────────────────────────────────────────────

function _renderPayrollTab(state, el, self) {
  // ── Tab bar: Nómina / Overtime / Contratos ──
  const tabsNav = document.createElement('div');
  tabsNav.style.cssText = 'display:flex;gap:0;margin-bottom:24px;border-bottom:2px solid #e5e7eb;';
  const payrollTabs = ['nomina', 'overtime', 'contratos'];
  const payrollTabLabels = { nomina: '💰 Nómina', overtime: '⏱ Overtime', contratos: '📄 Contratos' };
  const payrollPanels = {};
  const payrollTabBtns = {};

  payrollTabs.forEach((tab, i) => {
    const btn = document.createElement('button');
    btn.textContent = payrollTabLabels[tab];
    btn.style.cssText = `background:none;border:none;padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;color:${i===0?'#6366F1':'#9CA3AF'};border-bottom:${i===0?'2px solid #6366F1':'2px solid transparent'};margin-bottom:-2px;white-space:nowrap;`;
    btn.addEventListener('click', () => {
      payrollTabs.forEach(k => { payrollPanels[k].style.display = 'none'; payrollTabBtns[k].style.color = '#9CA3AF'; payrollTabBtns[k].style.borderBottom = '2px solid transparent'; });
      payrollPanels[tab].style.display = 'block';
      btn.style.color = '#6366F1';
      btn.style.borderBottom = '2px solid #6366F1';
      if (tab === 'overtime') _renderOvertimePanel(payrollPanels['overtime'], self);
      if (tab === 'contratos') _renderContractsPanel(payrollPanels['contratos'], self);
    });
    tabsNav.appendChild(btn);
    payrollTabBtns[tab] = btn;
  });
  el.appendChild(tabsNav);

  // Create tab panels
  payrollTabs.forEach((tab, i) => {
    const panel = document.createElement('div');
    panel.style.display = i === 0 ? 'block' : 'none';
    payrollPanels[tab] = panel;
    el.appendChild(panel);
  });

  // Redirect el to the nomina panel for all subsequent code in this function
  el = payrollPanels['nomina'];

  // ── Tip distribution config (collapsible) ──
  const tipConfigToggle = document.createElement('button');
  tipConfigToggle.style.cssText = 'width:100%;display:flex;justify-content:space-between;align-items:center;background:#f9f9f7;border:1.5px solid #e0e0d8;border-radius:10px;padding:10px 14px;font-size:13px;font-weight:600;cursor:pointer;margin-bottom:16px;';
  const tipConfigToggleTxt = document.createElement('span');
  tipConfigToggleTxt.textContent = '⚙ Configurar % de propinas por rol';
  const tipConfigChevron = document.createElement('span');
  tipConfigChevron.textContent = '▾';
  tipConfigToggle.appendChild(tipConfigToggleTxt);
  tipConfigToggle.appendChild(tipConfigChevron);
  el.appendChild(tipConfigToggle);

  const tipConfigPanel = document.createElement('div');
  tipConfigPanel.style.cssText = 'display:none;background:#f9f9f7;border:1.5px solid #e0e0d8;border-top:none;border-radius:0 0 10px 10px;padding:14px;margin-top:-18px;margin-bottom:16px;';

  tipConfigToggle.addEventListener('click', () => {
    const open = tipConfigPanel.style.display !== 'none';
    tipConfigPanel.style.display = open ? 'none' : 'block';
    tipConfigChevron.textContent = open ? '▾' : '▴';
    if (!open) _renderTipConfig(tipConfigPanel);
  });

  el.appendChild(tipConfigPanel);

  // ── Period selector ──
  const periodCard = document.createElement('div');
  periodCard.className = 'card';
  periodCard.style.cssText = 'margin-bottom:1.5rem;padding:1.25rem;';

  const h3 = document.createElement('h3');
  h3.textContent = '💰 Calcular Nómina';
  h3.style.cssText = 'font-size:15px;font-weight:700;margin-bottom:1rem;';
  periodCard.appendChild(h3);

  // Date inputs row
  const dateRow = document.createElement('div');
  dateRow.style.cssText = 'display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:1rem;';

  const mkDateField = (labelText, stateKey) => {
    const wrap = document.createElement('div');
    const lbl = document.createElement('label');
    lbl.textContent = labelText;
    lbl.style.cssText = 'font-size:11px;font-weight:700;color:#666;display:block;margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em;';
    const inp = document.createElement('input');
    inp.type = 'date';
    inp.value = state[stateKey] || '';
    inp.style.cssText = 'padding:9px 12px;border:1.5px solid #e0e0d8;border-radius:8px;font-size:13px;font-weight:600;outline:none;';
    inp.addEventListener('change', () => self.setState({ [stateKey]: inp.value }));
    wrap.appendChild(lbl); wrap.appendChild(inp);
    return wrap;
  };

  dateRow.appendChild(mkDateField('Inicio', 'payrollPeriodStart'));
  dateRow.appendChild(mkDateField('Fin', 'payrollPeriodEnd'));

  // Quick preset buttons
  const presets = document.createElement('div');
  presets.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';
  [
    ['Esta semana', () => {
      const now = new Date();
      const mon = new Date(now); mon.setDate(now.getDate() - ((now.getDay()+6)%7));
      const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
      return [mon.toISOString().slice(0,10), sun.toISOString().slice(0,10)];
    }],
    ['Semana pasada', () => {
      const now = new Date();
      const mon = new Date(now); mon.setDate(now.getDate() - ((now.getDay()+6)%7) - 7);
      const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
      return [mon.toISOString().slice(0,10), sun.toISOString().slice(0,10)];
    }],
    ['Este mes', () => {
      const now = new Date();
      const start = new Date(now.getFullYear(), now.getMonth(), 1);
      const end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
      return [start.toISOString().slice(0,10), end.toISOString().slice(0,10)];
    }],
  ].forEach(([label, getFn]) => {
    const btn = document.createElement('button');
    btn.textContent = label;
    btn.style.cssText = 'padding:7px 12px;border:1.5px solid #e0e0d8;border-radius:8px;background:#f9f9f7;font-size:12px;font-weight:600;cursor:pointer;color:#444;';
    btn.addEventListener('click', () => {
      const [s, e] = getFn();
      self.setState({ payrollPeriodStart: s, payrollPeriodEnd: e });
    });
    presets.appendChild(btn);
  });

  dateRow.appendChild(presets);
  periodCard.appendChild(dateRow);

  // Calculate button
  const calcBtn = document.createElement('button');
  calcBtn.textContent = state.payrollLoading ? 'Calculando...' : '🔢 Calcular Nómina';
  calcBtn.style.cssText = 'background:#18181B;color:#fff;border:none;padding:11px 22px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;';
  calcBtn.disabled = state.payrollLoading;
  calcBtn.addEventListener('click', async () => {
    if (!state.payrollPeriodStart || !state.payrollPeriodEnd) {
      alert('Selecciona el período de nómina.'); return;
    }
    self.setState({ payrollLoading: true, tipsAutoData: null });
    try {
      const [r, tipsR] = await Promise.allSettled([
        _staffFetch(`/payroll/calculate?period_start=${state.payrollPeriodStart}&period_end=${state.payrollPeriodEnd}`),
        _staffFetch(`/tips/auto?period_start=${state.payrollPeriodStart}&period_end=${state.payrollPeriodEnd}`),
      ]);
      const entries = r.status === 'fulfilled' ? (r.value.entries || []) : [];
      const tipsAuto = tipsR.status === 'fulfilled' ? tipsR.value : null;
      if (r.status === 'rejected') {
        alert('Error al calcular nómina: ' + r.reason.message);
      }
      self.setState({ payrollEntries: entries, tipsAutoData: tipsAuto, payrollLoading: false });
    } catch(e) {
      alert('Error al calcular: ' + e.message);
      self.setState({ payrollLoading: false });
    }
  });
  periodCard.appendChild(calcBtn);
  el.appendChild(periodCard);

  // ── Tips auto summary card ──
  if (state.tipsAutoData) {
    const tipsCard = document.createElement('div');
    tipsCard.className = 'card';
    tipsCard.style.cssText = 'margin-bottom:1.25rem;padding:1.25rem;background:#FFFBEB;border:1px solid #FDE68A;';

    const tipsTitle = document.createElement('div');
    tipsTitle.style.cssText = 'font-size:14px;font-weight:700;margin-bottom:10px;color:#92400E;';
    const tipsTotal = state.tipsAutoData.total_tips || 0;
    tipsTitle.textContent = 'Propinas del período: ' + _staffFmt(tipsTotal);
    tipsCard.appendChild(tipsTitle);

    const tipEntries = state.tipsAutoData.entries || [];
    if (tipEntries.length > 0) {
      const tipsTbl = document.createElement('table');
      tipsTbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:12px;';
      const tipsThead = document.createElement('thead');
      const tipsThr = document.createElement('tr');
      ['Empleado', 'Rol', 'Tickets', 'Monto'].forEach(h => {
        const th = document.createElement('th');
        th.textContent = h;
        th.style.cssText = 'text-align:left;padding:6px 8px;border-bottom:1px solid #FDE68A;color:#92400E;font-weight:700;';
        tipsThr.appendChild(th);
      });
      tipsThead.appendChild(tipsThr);
      tipsTbl.appendChild(tipsThead);

      const tipsTbody = document.createElement('tbody');
      tipEntries.forEach(te => {
        const tr = document.createElement('tr');
        [
          te.name || '—',
          getDynamicRoleLabel(te.role || ''),
          String(te.tickets || te.ticket_count || 0),
          _staffFmt(te.amount || te.tip_amount || 0),
        ].forEach(txt => {
          const td = document.createElement('td');
          td.textContent = txt;
          td.style.cssText = 'padding:6px 8px;border-bottom:1px solid #FEF3C7;';
          tr.appendChild(td);
        });
        tipsTbody.appendChild(tr);
      });
      tipsTbl.appendChild(tipsTbody);
      tipsCard.appendChild(tipsTbl);
    }

    const unalloc = state.tipsAutoData.unallocated || 0;
    if (unalloc > 0) {
      const warnEl = document.createElement('div');
      warnEl.style.cssText = 'margin-top:10px;font-size:12px;color:#F59E0B;font-weight:600;';
      warnEl.textContent = '⚠ ' + _staffFmt(unalloc) + ' sin asignar (pedidos sin staff en turno)';
      tipsCard.appendChild(warnEl);
    }

    el.appendChild(tipsCard);
  }

  // ── Results table ──
  if (state.payrollEntries && state.payrollEntries.length > 0) {
    const resultsCard = document.createElement('div');
    resultsCard.className = 'card';
    resultsCard.style.cssText = 'margin-bottom:1.5rem;overflow-x:auto;';

    // Totals banner
    const totals = state.payrollEntries.reduce((acc, e) => ({
      gross: acc.gross + (e.gross_pay || 0),
      tips:  acc.tips  + (e.tip_earnings || 0),
      net:   acc.net   + (e.net_pay || 0),
    }), { gross: 0, tips: 0, net: 0 });

    const banner = document.createElement('div');
    banner.style.cssText = 'display:flex;gap:16px;flex-wrap:wrap;padding:1rem 1.25rem;background:#f0fdf4;border-bottom:1px solid #e0e0d8;';
    [
      ['Bruto Total', totals.gross, '#1D9E75'],
      ['Propinas',    totals.tips,  '#F59E0B'],
      ['Neto Total',  totals.net,   '#7C3AED'],
    ].forEach(([label, val, color]) => {
      const d = document.createElement('div');
      const labelEl = document.createElement('div');
      labelEl.textContent = label;
      labelEl.style.cssText = 'font-size:11px;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.05em;';
      const valEl = document.createElement('div');
      valEl.textContent = _staffFmt(val);
      valEl.style.cssText = `font-size:1.4rem;font-weight:900;color:${color};`;
      d.appendChild(labelEl); d.appendChild(valEl);
      banner.appendChild(d);
    });
    resultsCard.appendChild(banner);

    // Table
    const tbl = document.createElement('table');
    tbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;';
    const thead = document.createElement('thead');
    const theadRow = document.createElement('tr');
    theadRow.style.background = '#f9f9f7';
    ['Nombre','Documento','Horas','Salario Base','Propinas','Ded. Auto','Ded. Manual','Neto'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      th.style.cssText = 'padding:9px 10px;text-align:left;font-size:11px;font-weight:700;color:#666;text-transform:uppercase;white-space:nowrap;';
      theadRow.appendChild(th);
    });
    thead.appendChild(theadRow);
    tbl.appendChild(thead);

    // Build tips lookup by staff_id from tipsAutoData
    const tipsById = {};
    if (state.tipsAutoData && state.tipsAutoData.entries) {
      state.tipsAutoData.entries.forEach(te => {
        if (te.staff_id) tipsById[te.staff_id] = te.amount || te.tip_amount || 0;
      });
    }

    const tbody = document.createElement('tbody');
    state.payrollEntries.forEach((e, i) => {
      const tr = document.createElement('tr');
      tr.style.cssText = `background:${i%2===0?'#fff':'#fafaf8'};`;
      const dedsObj = e.deductions || {};
      const dedAuto   = dedsObj.auto   !== undefined ? dedsObj.auto   : (dedsObj.attendance || 0);
      const dedManual = dedsObj.manual !== undefined ? dedsObj.manual : Object.entries(dedsObj).filter(([k]) => k !== 'auto' && k !== 'attendance').reduce((s,[,v]) => s + v, 0);
      const totalHours = (e.regular_hours || 0) + (e.overtime_hours || 0);
      const tipFromAuto = tipsById[e.staff_id] !== undefined ? tipsById[e.staff_id] : (e.tip_earnings || 0);
      // Build cell values: use textContent for user data
      const cells = [
        { text: e.name || '—' },
        { text: e.document_number || '—' },
        { text: totalHours.toFixed(1) + 'h' },
        { text: _staffFmt(e.gross_pay || 0) },
        { text: _staffFmt(tipFromAuto) },
        { el: (() => {
            const span = document.createElement('span');
            if (dedAuto > 0) { span.textContent = '-' + _staffFmt(dedAuto); span.style.color = '#EF4444'; }
            else { span.textContent = '—'; }
            return span;
          })() },
        { el: (() => {
            const span = document.createElement('span');
            if (dedManual > 0) { span.textContent = '-' + _staffFmt(dedManual); span.style.color = '#EF4444'; }
            else { span.textContent = '—'; }
            return span;
          })() },
        { el: (() => {
            const span = document.createElement('strong');
            span.textContent = _staffFmt(e.net_pay || 0);
            span.style.color = '#1D9E75';
            return span;
          })() },
      ];
      cells.forEach(cell => {
        const td = document.createElement('td');
        td.style.cssText = 'padding:9px 10px;border-bottom:1px solid #f0f0ec;vertical-align:middle;';
        if (cell.text !== undefined) {
          td.textContent = cell.text;
        } else if (cell.el) {
          td.appendChild(cell.el);
        } else if (cell.html !== undefined) {
          td.innerHTML = cell.html;
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    resultsCard.appendChild(tbl);

    // Action buttons
    const actRow = document.createElement('div');
    actRow.style.cssText = 'padding:1rem 1.25rem;display:flex;gap:10px;flex-wrap:wrap;';

    const saveBtn = document.createElement('button');
    saveBtn.textContent = '💾 Guardar Borrador';
    saveBtn.style.cssText = 'background:#18181B;color:#fff;border:none;padding:10px 18px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;';
    saveBtn.addEventListener('click', async () => {
      try {
        const r = await _staffFetch('/payroll/runs', 'POST', {
          period_start: state.payrollPeriodStart,
          period_end:   state.payrollPeriodEnd,
        });
        alert('✅ Nómina guardada. ID: ' + (r.run?.id || '').slice(0, 8));
        const runsData = await _staffFetch('/payroll/runs');
        self.setState({ payrollRuns: runsData.runs || [] });
      } catch(e) { alert('Error: ' + e.message); }
    });
    actRow.appendChild(saveBtn);
    resultsCard.appendChild(actRow);
    el.appendChild(resultsCard);
  } else if (!state.payrollLoading) {
    const hint = document.createElement('div');
    hint.className = 'empty-state';
    hint.textContent = 'Selecciona un período y presiona Calcular.';
    el.appendChild(hint);
  }

  // ── Payroll history ──
  if (state.payrollRuns && state.payrollRuns.length > 0) {
    const histCard = document.createElement('div');
    histCard.className = 'card';

    const hh = document.createElement('h3');
    hh.textContent = '📋 Historial de Nóminas';
    hh.style.cssText = 'font-size:14px;font-weight:700;padding:1rem 1.25rem;border-bottom:1px solid #e0e0d8;';
    histCard.appendChild(hh);

    state.payrollRuns.forEach(run => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:10px 1.25rem;border-bottom:1px solid #f0f0ec;';
      const statusColor = run.status === 'approved' ? '#1D9E75' : run.status === 'paid' ? '#7C3AED' : '#F59E0B';

      const left = document.createElement('div');
      const titleEl = document.createElement('div');
      titleEl.style.cssText = 'font-size:13px;font-weight:600;';
      titleEl.textContent = `${run.period_start} → ${run.period_end}`;
      const subEl = document.createElement('div');
      subEl.style.cssText = 'font-size:11px;color:#666;';
      subEl.textContent = `Neto: ${_staffFmt(run.total_net)} · Por: ${run.created_by || '—'}`;
      left.appendChild(titleEl); left.appendChild(subEl);

      const right = document.createElement('div');
      right.style.cssText = 'display:flex;gap:8px;align-items:center;';

      const badge = document.createElement('span');
      badge.textContent = run.status;
      badge.style.cssText = `background:${statusColor}22;color:${statusColor};padding:3px 9px;border-radius:6px;font-size:11px;font-weight:700;text-transform:uppercase;`;
      right.appendChild(badge);

      if (run.status === 'draft') {
        const approveBtn = document.createElement('button');
        approveBtn.textContent = '✅ Aprobar';
        approveBtn.style.cssText = 'background:#1D9E75;color:#fff;border:none;padding:6px 12px;border-radius:7px;font-size:12px;font-weight:700;cursor:pointer;';
        approveBtn.addEventListener('click', async () => {
          if (!confirm('¿Aprobar esta nómina?')) return;
          await _staffFetch(`/payroll/runs/${run.id}/approve`, 'PUT', {});
          const runsData = await _staffFetch('/payroll/runs');
          self.setState({ payrollRuns: runsData.runs || [] });
        });
        right.appendChild(approveBtn);
      }

      const exportBtn = document.createElement('button');
      exportBtn.textContent = '⬇ CSV';
      exportBtn.style.cssText = 'background:#f0f0ec;border:1.5px solid #e0e0d8;padding:6px 10px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;';
      exportBtn.addEventListener('click', () => { window.open(`/api/staff/payroll/export/${run.id}`, '_blank'); });
      right.appendChild(exportBtn);

      row.appendChild(left); row.appendChild(right);
      histCard.appendChild(row);
    });
    el.appendChild(histCard);
  }
}



// ── Payroll sub-panels ────────────────────────────────────────────────────────

async function _renderTipConfig(container) {
  container.textContent = '';
  const roles = ['mesero','cocina','bar','caja','gerente','domiciliario'];
  let currentConfig = {};
  try {
    const rest = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    const f = rest.features || {};
    currentConfig = (typeof f === 'string' ? JSON.parse(f) : f).tip_distribution || {};
  } catch(_) {}

  const totalIndicator = document.createElement('div');
  totalIndicator.style.cssText = 'font-size:12px;font-weight:700;margin-bottom:12px;';

  const inputs = {};
  const grid = document.createElement('div');
  grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;margin-bottom:12px;';

  const refreshTotal = () => {
    const total = roles.reduce((sum, r) => sum + (parseFloat(inputs[r]?.value) || 0), 0);
    totalIndicator.textContent = `Total: ${total.toFixed(1)}%`;
    totalIndicator.style.color = Math.abs(total - 100) < 0.1 ? '#16A34A' : total > 100 ? '#DC2626' : '#D97706';
  };

  roles.forEach(role => {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display:flex;align-items:center;gap:8px;background:#fff;border:1.5px solid #e0e0d8;border-radius:8px;padding:8px 10px;';
    const lbl = document.createElement('label');
    lbl.textContent = _ROLE_LABELS[role] || role;
    lbl.style.cssText = 'font-size:12px;font-weight:600;flex:1;';
    const inp = document.createElement('input');
    inp.type = 'number'; inp.min = '0'; inp.max = '100'; inp.step = '0.5';
    inp.value = currentConfig[role] || 0;
    inp.style.cssText = 'width:56px;padding:5px 8px;border:1.5px solid #e0e0d8;border-radius:6px;font-size:13px;font-weight:700;text-align:right;';
    const pct = document.createElement('span');
    pct.textContent = '%';
    pct.style.cssText = 'font-size:12px;color:#666;';
    inp.addEventListener('input', refreshTotal);
    inputs[role] = inp;
    wrap.appendChild(lbl); wrap.appendChild(inp); wrap.appendChild(pct);
    grid.appendChild(wrap);
  });

  container.appendChild(grid);
  container.appendChild(totalIndicator);
  refreshTotal();

  const errMsg = document.createElement('div');
  errMsg.style.cssText = 'color:#EF4444;font-size:12px;min-height:16px;margin-bottom:8px;';
  container.appendChild(errMsg);

  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Guardar configuración';
  saveBtn.style.cssText = 'background:#18181B;color:#fff;border:none;padding:9px 18px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;';
  saveBtn.addEventListener('click', async () => {
    const config = {};
    roles.forEach(r => { const v = parseFloat(inputs[r].value) || 0; if (v > 0) config[r] = v; });
    const total = Object.values(config).reduce((s,v) => s+v, 0);
    if (total > 100.01) { errMsg.textContent = `Los porcentajes suman ${total.toFixed(1)}%, no pueden superar 100%.`; return; }
    saveBtn.disabled = true;
    try {
      await _staffFetch('/tip-distribution', 'PATCH', { config });
      errMsg.style.color = '#16A34A';
      errMsg.textContent = '✓ Guardado correctamente';
      setTimeout(() => { errMsg.textContent = ''; errMsg.style.color = '#EF4444'; }, 2500);
    } catch(e) {
      errMsg.textContent = e.message;
    } finally { saveBtn.disabled = false; }
  });
  container.appendChild(saveBtn);
}

async function _renderOvertimePanel(container, self) {
  container.textContent = '';
  const loading = document.createElement('div');
  loading.className = 'empty-state';
  loading.textContent = 'Cargando overtime...';
  container.appendChild(loading);
  try {
    const data = await _staffFetch('/payroll/overtime?status=pending');
    loading.remove();
    const requests = data.overtime_requests || [];
    if (!requests.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = 'No hay solicitudes de overtime pendientes.';
      container.appendChild(empty);
      return;
    }
    const card = document.createElement('div');
    card.className = 'card';
    card.style.overflowX = 'auto';
    const tbl = document.createElement('table');
    tbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;';
    const thead = document.createElement('thead');
    const hrow = document.createElement('tr');
    hrow.style.background = '#f9f9f7';
    ['Empleado','Rol','Semana','Hrs Regulares','Hrs Extra','Monto Est.','Acción'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      th.style.cssText = 'padding:9px 10px;text-align:left;font-size:11px;font-weight:700;color:#666;text-transform:uppercase;white-space:nowrap;border-bottom:1px solid #e0e0d8;';
      hrow.appendChild(th);
    });
    thead.appendChild(hrow);
    tbl.appendChild(thead);
    const tbody = document.createElement('tbody');
    requests.forEach((req, i) => {
      const tr = document.createElement('tr');
      tr.style.background = i%2===0?'#fff':'#fafaf8';
      const otHrs = (req.overtime_minutes/60).toFixed(1);
      const regHrs = (req.regular_minutes/60).toFixed(1);
      const cells = [
        { text: req.staff_name || '—' },
        { text: _ROLE_LABELS[req.role] || req.role || '—' },
        { text: req.week_start || '—' },
        { text: regHrs + 'h' },
        { text: otHrs + 'h' },
        { text: '—' },
      ];
      cells.forEach(cell => {
        const td = document.createElement('td');
        td.style.cssText = 'padding:9px 10px;border-bottom:1px solid #f0f0ec;vertical-align:middle;';
        td.textContent = cell.text;
        tr.appendChild(td);
      });
      // Action cell
      const actionTd = document.createElement('td');
      actionTd.style.cssText = 'padding:9px 10px;border-bottom:1px solid #f0f0ec;';
      const approveBtn = document.createElement('button');
      approveBtn.textContent = '✅ Aprobar';
      approveBtn.style.cssText = 'background:#16A34A;color:#fff;border:none;padding:5px 10px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;margin-right:6px;';
      const rejectBtn = document.createElement('button');
      rejectBtn.textContent = '✕ Rechazar';
      rejectBtn.style.cssText = 'background:#FEE2E2;color:#DC2626;border:none;padding:5px 10px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;';
      const reviewOT = async (status) => {
        approveBtn.disabled = true; rejectBtn.disabled = true;
        try {
          await _staffFetch(`/payroll/overtime/${req.id}`, 'PATCH', { status, notes: '' });
          _renderOvertimePanel(container, self);
        } catch(e) { alert('Error: ' + e.message); approveBtn.disabled = false; rejectBtn.disabled = false; }
      };
      approveBtn.addEventListener('click', () => reviewOT('approved'));
      rejectBtn.addEventListener('click', () => reviewOT('rejected'));
      actionTd.appendChild(approveBtn);
      actionTd.appendChild(rejectBtn);
      tr.appendChild(actionTd);
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    card.appendChild(tbl);
    container.appendChild(card);
  } catch(e) {
    loading.textContent = 'Error: ' + e.message;
  }
}

async function _renderContractsPanel(container, self) {
  container.textContent = '';
  const headerRow = document.createElement('div');
  headerRow.style.cssText = 'display:flex;justify-content:flex-end;margin-bottom:14px;';
  const newBtn = document.createElement('button');
  newBtn.textContent = '+ Nueva Plantilla';
  newBtn.style.cssText = 'background:#18181B;color:#fff;border:none;padding:9px 16px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;';
  newBtn.addEventListener('click', () => _openContractModal({ mode:'create', container, self }));
  headerRow.appendChild(newBtn);
  container.appendChild(headerRow);

  const loading = document.createElement('div');
  loading.className = 'empty-state';
  loading.textContent = 'Cargando plantillas...';
  container.appendChild(loading);
  try {
    const data = await _staffFetch('/payroll/contracts');
    loading.remove();
    const templates = data.templates || [];
    if (!templates.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = 'No hay plantillas de contrato. Crea una para asignarla a empleados.';
      container.appendChild(empty);
      return;
    }
    templates.forEach(t => {
      const card = document.createElement('div');
      card.style.cssText = `background:${t.active?'#fff':'#f9f9f7'};border:1.5px solid ${t.active?'#e0e0d8':'#f0f0ec'};border-radius:12px;padding:14px 16px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;`;
      const info = document.createElement('div');
      const nameEl = document.createElement('div');
      nameEl.textContent = t.name;
      nameEl.style.cssText = 'font-size:14px;font-weight:700;';
      const detailEl = document.createElement('div');
      detailEl.style.cssText = 'font-size:12px;color:#666;margin-top:4px;';
      const periodLabel = { monthly:'Mensual', biweekly:'Quincenal', weekly:'Semanal' }[t.pay_period] || t.pay_period;
      detailEl.textContent = `${t.weekly_hours}h/sem · Salario: $${Number(t.monthly_salary).toLocaleString('es-CO')} · ${periodLabel} · Break: ${t.breaks_billable?'billable':'no billable'} · Almuerzo: ${t.lunch_billable?'billable':t.lunch_minutes+'min no billable'}`;
      info.appendChild(nameEl);
      info.appendChild(detailEl);
      const editBtn = document.createElement('button');
      editBtn.textContent = 'Editar';
      editBtn.style.cssText = 'background:#f0f0ec;border:1.5px solid #e0e0d8;padding:7px 14px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;';
      editBtn.addEventListener('click', () => _openContractModal({ mode:'edit', template: t, container, self }));
      card.appendChild(info);
      card.appendChild(editBtn);
      container.appendChild(card);
    });
  } catch(e) {
    loading.textContent = 'Error: ' + e.message;
  }
}

function _openContractModal({ mode, template, container, self }) {
  const existing = document.getElementById('contract-modal-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'contract-modal-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center;overflow-y:auto;padding:20px;box-sizing:border-box;';

  const modal = document.createElement('div');
  modal.style.cssText = 'background:#fff;border-radius:14px;padding:1.5rem;width:480px;max-width:100%;box-shadow:0 20px 60px rgba(0,0,0,.2);';

  const title = document.createElement('h3');
  title.textContent = mode === 'create' ? 'Nueva plantilla de contrato' : 'Editar plantilla';
  title.style.cssText = 'font-size:15px;font-weight:700;margin-bottom:1.25rem;';
  modal.appendChild(title);

  const t = template || {};
  const mkField = (lbl, type, val, attrs = {}) => {
    const wrap = document.createElement('div');
    wrap.style.marginBottom = '12px';
    const l = document.createElement('label');
    l.textContent = lbl;
    l.style.cssText = 'display:block;font-size:11px;font-weight:700;color:#666;margin-bottom:4px;text-transform:uppercase;';
    const inp = type === 'select' ? document.createElement('select') : document.createElement('input');
    if (type !== 'select') { inp.type = type; inp.value = val !== undefined ? val : ''; }
    inp.style.cssText = 'width:100%;box-sizing:border-box;padding:9px 12px;border:1.5px solid #e0e0d8;border-radius:8px;font-size:13px;';
    Object.assign(inp, attrs);
    if (type === 'select' && Array.isArray(val)) {
      val.forEach(([v,ltext]) => { const o = document.createElement('option'); o.value = v; o.textContent = ltext; inp.appendChild(o); });
    }
    wrap.appendChild(l); wrap.appendChild(inp);
    return { wrap, inp };
  };

  const mkCheckbox = (lbl, val) => {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:12px;';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = val;
    cb.style.width = '16px'; cb.style.height = '16px';
    const l = document.createElement('label');
    l.textContent = lbl;
    l.style.cssText = 'font-size:13px;font-weight:600;cursor:pointer;';
    l.prepend(cb);
    wrap.appendChild(l);
    return { wrap, inp: cb };
  };

  const grid = document.createElement('div');
  grid.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:0 12px;';

  const nameF     = mkField('Nombre plantilla', 'text', t.name || '');
  const hoursF    = mkField('Horas semanales', 'number', t.weekly_hours || 44, { min:'1', max:'84', step:'0.5' });
  const salaryF   = mkField('Salario mensual', 'number', t.monthly_salary || 0, { min:'0', step:'1000' });
  const periodF   = mkField('Periodicidad', 'select', [['monthly','Mensual'],['biweekly','Quincenal'],['weekly','Semanal']], {});
  if (t.pay_period) periodF.inp.value = t.pay_period;
  const subsidyF  = mkField('Subsidio transporte', 'number', t.transport_subsidy || 0, { min:'0', step:'1000' });
  const arlF      = mkField('ARL % (ej: 0.00522)', 'number', t.arl_pct !== undefined ? t.arl_pct : 0.00522, { min:'0', max:'1', step:'0.001' });
  const healthF   = mkField('Salud %', 'number', t.health_pct !== undefined ? t.health_pct : 0.04, { min:'0', max:'1', step:'0.001' });
  const pensionF  = mkField('Pensión %', 'number', t.pension_pct !== undefined ? t.pension_pct : 0.04, { min:'0', max:'1', step:'0.001' });
  const lunchMinF = mkField('Minutos almuerzo', 'number', t.lunch_minutes || 60, { min:'0', max:'120', step:'5' });
  const breakBillableF = mkCheckbox('Breaks son billable (se pagan)', t.breaks_billable !== false);
  const lunchBillableF = mkCheckbox('Almuerzo es billable', t.lunch_billable === true);
  const activeF   = mkCheckbox('Plantilla activa', t.active !== false);

  modal.appendChild(nameF.wrap);
  grid.appendChild(hoursF.wrap);
  grid.appendChild(salaryF.wrap);
  grid.appendChild(periodF.wrap);
  grid.appendChild(subsidyF.wrap);
  grid.appendChild(arlF.wrap);
  grid.appendChild(healthF.wrap);
  grid.appendChild(pensionF.wrap);
  grid.appendChild(lunchMinF.wrap);
  modal.appendChild(grid);
  modal.appendChild(breakBillableF.wrap);
  modal.appendChild(lunchBillableF.wrap);
  if (mode === 'edit') modal.appendChild(activeF.wrap);

  const errMsg = document.createElement('div');
  errMsg.style.cssText = 'color:#EF4444;font-size:12px;min-height:16px;margin-bottom:8px;';
  modal.appendChild(errMsg);

  const btnRow = document.createElement('div');
  btnRow.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';

  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancelar';
  cancelBtn.style.cssText = 'padding:9px 16px;border:1.5px solid #e0e0d8;background:#f9f9f7;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;';
  cancelBtn.addEventListener('click', () => overlay.remove());

  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Guardar';
  saveBtn.style.cssText = 'padding:9px 16px;background:#18181B;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;';
  saveBtn.addEventListener('click', async () => {
    const name = nameF.inp.value.trim();
    if (!name) { errMsg.textContent = 'El nombre es requerido.'; return; }
    saveBtn.disabled = true;
    const payload = {
      name, weekly_hours: parseFloat(hoursF.inp.value), monthly_salary: parseFloat(salaryF.inp.value),
      pay_period: periodF.inp.value, transport_subsidy: parseFloat(subsidyF.inp.value),
      arl_pct: parseFloat(arlF.inp.value), health_pct: parseFloat(healthF.inp.value),
      pension_pct: parseFloat(pensionF.inp.value), lunch_minutes: parseInt(lunchMinF.inp.value),
      breaks_billable: breakBillableF.inp.checked, lunch_billable: lunchBillableF.inp.checked,
      ...(mode === 'edit' ? { active: activeF.inp.checked } : {}),
    };
    try {
      if (mode === 'create') await _staffFetch('/payroll/contracts', 'POST', payload);
      else await _staffFetch(`/payroll/contracts/${template.id}`, 'PATCH', payload);
      overlay.remove();
      _renderContractsPanel(container, self);
    } catch(e) {
      errMsg.textContent = e.message;
      saveBtn.disabled = false;
    }
  });

  btnRow.appendChild(cancelBtn);
  if (mode === 'edit') {
    const delBtn = document.createElement('button');
    delBtn.textContent = 'Eliminar';
    delBtn.style.cssText = 'padding:9px 16px;background:#FEE2E2;color:#DC2626;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;';
    delBtn.addEventListener('click', async () => {
      if (!confirm(`¿Eliminar la plantilla "${template.name}"?`)) return;
      delBtn.disabled = true;
      try {
        await _staffFetch(`/payroll/contracts/${template.id}`, 'DELETE');
        overlay.remove();
        _renderContractsPanel(container, self);
      } catch(e) { errMsg.textContent = e.message; delBtn.disabled = false; }
    });
    btnRow.appendChild(delBtn);
  }
  btnRow.appendChild(saveBtn);
  modal.appendChild(btnRow);

  overlay.appendChild(modal);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  nameF.inp.focus();
}

// ── Main StaffSection component ───────────────────────────────────────────────

const StaffSection = MesioComponent({
  state: {
    loading:      true,
    staff:        [],
    filter:       'all',
    search:       '',
    error:        null,
    staffSubTab:  'roster',
  },

  render(state, el) {
    el.textContent = '';

    const headerActions = document.createElement('div');
    headerActions.style.cssText = 'display:flex;justify-content:flex-end;gap:10px;margin-bottom:20px;';

    const btnAdmin = _makeBtn('+ Añadir Admin', 'btn-sm btn-outline', () => {
        if (typeof window.openStaffAdminModal === 'function') window.openStaffAdminModal();
    });
    btnAdmin.style.cssText += 'background:#E1F5EE;color:#0F6E56;border:1px solid #1D9E75;font-weight:600;padding:10px 18px;border-radius:10px;';

    const btnStaff = _makeBtn('+ Nuevo Empleado', 'btn-sm btn-primary', () => _openStaffModal(StaffSection));
    btnStaff.style.cssText += 'padding:10px 18px;border-radius:10px;';

    headerActions.appendChild(btnAdmin);
    headerActions.appendChild(btnStaff);
    el.appendChild(headerActions);

    if (state.loading) {
      const msg = document.createElement('div');
      msg.className = 'empty-state';
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

    // Sub-tab navigation: Equipo / Turnos
    const subtabNav = document.createElement('div');
    subtabNav.style.cssText = 'display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid #e5e7eb;';

    const panels = {};
    const subtabBtns = {};

    ['roster', 'shifts'].forEach((tab, i) => {
      const label = tab === 'roster' ? 'Equipo' : 'Turnos';
      const btn = document.createElement('button');
      btn.textContent = label;
      btn.style.cssText = `background:none;border:none;padding:10px 20px;cursor:pointer;font-size:14px;font-weight:600;color:${i===0?'#6366F1':'#9CA3AF'};border-bottom:${i===0?'2px solid #6366F1':'2px solid transparent'};margin-bottom:-2px;transition:color .15s;`;
      btn.addEventListener('click', () => {
        Object.keys(panels).forEach(k => { panels[k].style.display = 'none'; subtabBtns[k].style.color = '#9CA3AF'; subtabBtns[k].style.borderBottom = '2px solid transparent'; });
        panels[tab].style.display = 'block';
        btn.style.color = '#6366F1';
        btn.style.borderBottom = '2px solid #6366F1';
        if (tab === 'shifts') _renderShiftsEditor(panels['shifts']);
      });
      subtabNav.appendChild(btn);
      subtabBtns[tab] = btn;
    });

    el.appendChild(subtabNav);

    const rosterPanel = document.createElement('div');
    rosterPanel.id = 'staff-roster-panel';
    _renderRosterTab(state, rosterPanel, StaffSection);
    panels['roster'] = rosterPanel;
    el.appendChild(rosterPanel);

    const shiftsPanel = document.createElement('div');
    shiftsPanel.id = 'staff-shifts-panel';
    shiftsPanel.style.display = 'none';
    panels['shifts'] = shiftsPanel;
    el.appendChild(shiftsPanel);
  },

  async onMount(self) {
    try {
      const role = (localStorage.getItem('rb_role') || '').toLowerCase();
      if (role.includes('owner')) {
          await _loadStaffBranchesSelect();
      }
      const rosterData = await _staffFetch('');
      self.setState({ staff: rosterData.staff, loading: false });
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

window.openStaffAdminModal = function() {
  // 🛡️ FIX: Usar el selector global real del Topbar
  const select = document.getElementById('global-branch-select');
  if (!select) {
      alert("Error: No se encontró el selector de sucursales.");
      return;
  }
  
  const val = select.value;
  const branchName = select.options[select.selectedIndex].text.replace('🏠 ', '').replace('📍 ', '');
  
  // Obtenemos el ID real para la invitación
  let targetBranchId;
  if (val === 'matriz') {
      const mainRest = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
      targetBranchId = mainRest.id;
  } else {
      targetBranchId = parseInt(val);
  }

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
          const headers = { 
              'Authorization': 'Bearer ' + localStorage.getItem('rb_token'), 
              'Content-Type': 'application/json' 
          };
          
          // 🛡️ Siempre enviamos un targetBranchId válido (sea la matriz o la sucursal)
          const payload = { 
              username, 
              password, 
              role: 'admin', 
              branch_id: targetBranchId 
          };
          if (phone) payload.phone = phone;

          const r = await fetch('/api/team/invite', {
              method: 'POST',
              headers: headers,
              body: JSON.stringify(payload),
          });
          
          if (r.ok) {
              overlay.remove();
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

// ── PayrollSection component ──────────────────────────────────────────────────

const PayrollSection = MesioComponent({
  state: {
    payrollLoading:     false,
    payrollPeriodStart: '',
    payrollPeriodEnd:   '',
    payrollEntries:     null,
    payrollRuns:        [],
    tipConfigOpen:      false,
    tipsAutoData:       null,
  },

  render(state, el) {
    el.textContent = '';
    _renderPayrollTab(state, el, PayrollSection);
  },

  async onMount(self) {
    // Default period: current month
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), 1);
    const end   = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    const fmt   = d => d.toISOString().slice(0, 10);
    try {
      const runsData = await _staffFetch('/payroll/runs');
      self.setState({
        payrollPeriodStart: fmt(start),
        payrollPeriodEnd:   fmt(end),
        payrollRuns:        runsData.runs || [],
      });
    } catch (_) {
      self.setState({
        payrollPeriodStart: fmt(start),
        payrollPeriodEnd:   fmt(end),
      });
    }
  },
});


// ─────────────────────────────────────────────────────────────────────────────
// loadPayrollSection — called by dashboard-core.js when the user navigates to
// the 'payroll' section. Mounts PayrollSection into #payroll-component on
// first visit; subsequent calls reload the run history.
// ─────────────────────────────────────────────────────────────────────────────
let _payrollMounted = false;

async function loadPayrollSection() {
  const el = document.getElementById('payroll-component');
  if (!el) return;

  if (!_payrollMounted) {
    _payrollMounted = true;
    PayrollSection.mount('#payroll-component');
  } else {
    try {
      const runsData = await _staffFetch('/payroll/runs');
      PayrollSection.setState({ payrollRuns: runsData.runs || [] });
    } catch (_) {}
  }
}