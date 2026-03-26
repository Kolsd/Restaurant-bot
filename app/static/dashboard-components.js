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
// StaffSection — Phase 6: Staff roster, clock-in/out, tip cut
//
// State shape:
//   {
//     loading: bool,
//     staff:   [...],        // from GET /api/staff
//     shifts:  [...],        // open shifts, from GET /api/staff/open-shifts
//     tipPreview: null | {...},  // result of POST /api/staff/tip-cut
//     tab: 'roster' | 'shifts' | 'tips',
//   }
// ─────────────────────────────────────────────────────────────────────────────

const _ROLE_LABELS = {
  mesero:        'Mesero',
  cocina:        'Cocina',
  bar:           'Bar',
  caja:          'Caja',
  gerente:       'Gerente',
  domiciliario:  'Domiciliario',
  otro:          'Otro',
};

function _apiHeaders() {
  const token = localStorage.getItem('rb_token') || '';
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` };
}

// ── helpers — all DOM writes use textContent / createElement ────────────────

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

// ── Async API calls ─────────────────────────────────────────────────────────

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

// ── Tab renderers ───────────────────────────────────────────────────────────

function _renderRosterTab(state, el, self) {
  // ── Add staff form ────────────────────────────────────────────────────────
  const formWrap = document.createElement('div');
  formWrap.style.cssText = 'background:#f8f8f5;border:1px solid #e0e0d8;border-radius:10px;padding:1rem;margin-bottom:1.25rem;';

  const formTitle = document.createElement('div');
  formTitle.textContent = 'Agregar empleado';
  formTitle.style.cssText = 'font-size:14px;font-weight:600;margin-bottom:.75rem;';
  formWrap.appendChild(formTitle);

  const nameIn  = _makeInput('Nombre completo');
  const roleIn  = _makeSelect(Object.entries(_ROLE_LABELS), 'mesero');
  const pinIn   = _makeInput('PIN (4–8 dígitos)', 'password');
  const phoneIn = _makeInput('Teléfono (opcional)');

  const row1 = document.createElement('div');
  row1.style.cssText = _rowStyle();
  row1.appendChild(nameIn);
  row1.appendChild(roleIn);
  formWrap.appendChild(row1);

  const row2 = document.createElement('div');
  row2.style.cssText = _rowStyle();
  row2.appendChild(pinIn);
  row2.appendChild(phoneIn);
  formWrap.appendChild(row2);

  const errMsg = document.createElement('div');
  errMsg.style.cssText = 'color:#C0392B;font-size:12px;margin-top:4px;min-height:16px;';
  formWrap.appendChild(errMsg);

  const addBtn = _makeBtn('+ Agregar', 'btn-sm btn-primary', async () => {
    errMsg.textContent = '';
    const name  = nameIn.value.trim();
    const pin   = pinIn.value.trim();
    if (!name)            { errMsg.textContent = 'El nombre es obligatorio.'; return; }
    if (pin.length < 4)   { errMsg.textContent = 'El PIN debe tener al menos 4 dígitos.'; return; }
    if (!/^\d+$/.test(pin)) { errMsg.textContent = 'El PIN debe ser solo dígitos.'; return; }

    addBtn.disabled = true;
    addBtn.textContent = 'Guardando...';
    try {
      await _staffFetch('', {
        method: 'POST',
        body: JSON.stringify({
          name, role: roleIn.value, pin, phone: phoneIn.value.trim(),
        }),
      });
      nameIn.value = ''; pinIn.value = ''; phoneIn.value = '';
      await _reloadRoster(self);
    } catch (e) {
      errMsg.textContent = e.message;
    } finally {
      addBtn.disabled = false;
      addBtn.textContent = '+ Agregar';
    }
  });
  formWrap.appendChild(addBtn);
  el.appendChild(formWrap);

  // ── Staff list ────────────────────────────────────────────────────────────
  if (!state.staff.length) {
    const empty = document.createElement('div');
    empty.className   = 'empty-state';
    empty.textContent = 'No hay empleados registrados.';
    el.appendChild(empty);
    return;
  }

  const tableWrap = document.createElement('div');
  tableWrap.className = 'card';
  tableWrap.style.overflowX = 'auto';

  const tbl = document.createElement('table');
  tbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;';

  // header
  const thead = document.createElement('thead');
  const hrow  = document.createElement('tr');
  ['Nombre', 'Rol', 'Teléfono', 'Estado', 'Acciones'].forEach(h => {
    const th = document.createElement('th');
    th.textContent = h;
    th.style.cssText = 'text-align:left;padding:8px 10px;border-bottom:1px solid #e0e0d8;color:#888;font-weight:500;';
    hrow.appendChild(th);
  });
  thead.appendChild(hrow);
  tbl.appendChild(thead);

  const tbody = document.createElement('tbody');
  state.staff.forEach(member => {
    const tr = document.createElement('tr');

    const tdName = document.createElement('td');
    tdName.textContent = member.name;
    tdName.style.padding = '9px 10px';

    const tdRole = document.createElement('td');
    tdRole.textContent = _ROLE_LABELS[member.role] || member.role;
    tdRole.style.padding = '9px 10px';

    const tdPhone = document.createElement('td');
    tdPhone.textContent = member.phone || '—';
    tdPhone.style.padding = '9px 10px';

    const tdStatus = document.createElement('td');
    tdStatus.style.padding = '9px 10px';
    const badge = document.createElement('span');
    badge.textContent = member.active ? 'Activo' : 'Inactivo';
    badge.style.cssText = member.active
      ? 'background:#DCFCE7;color:#16A34A;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;'
      : 'background:#F3F4F6;color:#9CA3AF;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;';
    tdStatus.appendChild(badge);

    const tdActions = document.createElement('td');
    tdActions.style.padding = '9px 10px';
    const toggleBtn = _makeBtn(
      member.active ? 'Desactivar' : 'Activar',
      'btn-sm ' + (member.active ? 'btn-outline' : 'btn-primary'),
      async () => {
        toggleBtn.disabled = true;
        try {
          await _staffFetch(`/${member.id}`, {
            method: 'PUT',
            body: JSON.stringify({ active: !member.active }),
          });
          await _reloadRoster(self);
        } catch (e) {
          alert(e.message);
        } finally {
          toggleBtn.disabled = false;
        }
      },
    );
    tdActions.appendChild(toggleBtn);

    [tdName, tdRole, tdPhone, tdStatus, tdActions].forEach(td => tr.appendChild(td));
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);
  tableWrap.appendChild(tbl);
  el.appendChild(tableWrap);
}


function _renderShiftsTab(state, el, self) {
  // ── Clock-in/out panel ────────────────────────────────────────────────────
  const panel = document.createElement('div');
  panel.style.cssText = 'background:#f8f8f5;border:1px solid #e0e0d8;border-radius:10px;padding:1rem;margin-bottom:1.25rem;';

  const panelTitle = document.createElement('div');
  panelTitle.textContent = 'Registrar entrada / salida';
  panelTitle.style.cssText = 'font-size:14px;font-weight:600;margin-bottom:.75rem;';
  panel.appendChild(panelTitle);

  const activeStaff = state.staff.filter(s => s.active);
  if (!activeStaff.length) {
    const msg = document.createElement('div');
    msg.className   = 'empty-state';
    msg.textContent = 'No hay empleados activos. Agrega empleados en la pestana Roster.';
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

    const ciBtn = _makeBtn('Entrada (Clock In)', 'btn-sm btn-primary', async () => {
      errMsg.textContent = '';
      ciBtn.disabled = true;
      try {
        await _staffFetch('/clock-in', {
          method: 'POST',
          body: JSON.stringify({ staff_id: staffSel.value }),
        });
        await _reloadShifts(self);
      } catch (e) {
        errMsg.textContent = e.message;
      } finally {
        ciBtn.disabled = false;
      }
    });

    const coBtn = _makeBtn('Salida (Clock Out)', 'btn-sm btn-outline', async () => {
      errMsg.textContent = '';
      coBtn.disabled = true;
      try {
        await _staffFetch('/clock-out', {
          method: 'POST',
          body: JSON.stringify({ staff_id: staffSel.value }),
        });
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

  // ── Open shifts table ─────────────────────────────────────────────────────
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
  ['Empleado', 'Rol', 'Entrada'].forEach(h => {
    const th = document.createElement('th');
    th.textContent = h;
    th.style.cssText = 'text-align:left;padding:8px 10px;border-bottom:1px solid #e0e0d8;color:#888;font-weight:500;';
    hrow.appendChild(th);
  });
  thead.appendChild(hrow);
  tbl.appendChild(thead);

  const tbody = document.createElement('tbody');
  state.shifts.forEach(sh => {
    const tr    = document.createElement('tr');
    const tdN   = document.createElement('td');
    const tdR   = document.createElement('td');
    const tdIn  = document.createElement('td');
    tdN.textContent  = sh.staff_name;
    tdR.textContent  = _ROLE_LABELS[sh.staff_role] || sh.staff_role;
    tdIn.textContent = new Date(sh.clock_in).toLocaleString();
    [tdN, tdR, tdIn].forEach(td => {
      td.style.padding = '9px 10px';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);

  const card = document.createElement('div');
  card.className = 'card';
  card.style.overflowX = 'auto';
  card.appendChild(tbl);
  el.appendChild(card);
}


function _renderTipsTab(state, el, self) {
  // ── Tip cut form ──────────────────────────────────────────────────────────
  const formWrap = document.createElement('div');
  formWrap.style.cssText = 'background:#f8f8f5;border:1px solid #e0e0d8;border-radius:10px;padding:1rem;margin-bottom:1.25rem;';

  const formTitle = document.createElement('div');
  formTitle.textContent = 'Corte de propinas';
  formTitle.style.cssText = 'font-size:14px;font-weight:600;margin-bottom:.75rem;';
  formWrap.appendChild(formTitle);

  // Default period: today 00:00 → now
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const now = new Date();

  const toLocalInput = d => {
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  const fromIn   = _makeInput('Inicio del período', 'datetime-local', toLocalInput(todayStart));
  const toIn     = _makeInput('Fin del período',    'datetime-local', toLocalInput(now));
  const totalIn  = _makeInput('Total propinas ($)',  'number');
  totalIn.min    = '0';
  totalIn.step   = '0.01';

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

    cutBtn.disabled = true;
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
      cutBtn.disabled = false;
      cutBtn.textContent = 'Calcular y guardar corte';
    }
  });
  formWrap.appendChild(cutBtn);
  el.appendChild(formWrap);

  // ── Preview results ───────────────────────────────────────────────────────
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
      const cells = [
        e.name,
        _ROLE_LABELS[e.role] || e.role,
        e.hours.toFixed(1) + ' h',
        e.pct + '%',
        '$' + e.amount.toLocaleString('es-CO', { minimumFractionDigits: 2 }),
      ];
      cells.forEach(txt => {
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
    const alloc = document.createElement('span');
    alloc.textContent = 'Distribuido: $' + preview.total_allocated.toLocaleString('es-CO', { minimumFractionDigits: 2 });
    const unalloc = document.createElement('span');
    unalloc.textContent = 'Sin asignar: $' + preview.total_unallocated.toLocaleString('es-CO', { minimumFractionDigits: 2 });
    totals.appendChild(alloc);
    totals.appendChild(unalloc);
    resWrap.appendChild(totals);
    el.appendChild(resWrap);
  }
}


// ── Data reload helpers ─────────────────────────────────────────────────────

async function _reloadRoster(self) {
  const data = await _staffFetch('');
  self.setState({ staff: data.staff, loading: false });
}

async function _reloadShifts(self) {
  const data = await _staffFetch('/open-shifts');
  self.setState({ shifts: data.shifts, loading: false });
}


// ── Main component ──────────────────────────────────────────────────────────

const StaffSection = MesioComponent({
  state: { loading: true, staff: [], shifts: [], tipPreview: null, tab: 'roster' },

  render(state, el) {
    el.textContent = '';

    if (state.loading) {
      const msg = document.createElement('div');
      msg.className   = 'empty-state';
      msg.textContent = 'Cargando equipo...';
      el.appendChild(msg);
      return;
    }

    // ── Tab bar ───────────────────────────────────────────────────────────
    const tabBar = document.createElement('div');
    tabBar.style.cssText = 'display:flex;gap:4px;margin-bottom:1.25rem;border-bottom:1px solid #e0e0d8;';

    const tabs = [
      ['roster', 'Roster'],
      ['shifts', 'Turnos'],
      ['tips',   'Propinas'],
    ];
    tabs.forEach(([id, label]) => {
      const btn = document.createElement('button');
      btn.textContent = label;
      btn.style.cssText = `padding:8px 16px;border:none;background:none;cursor:pointer;font-size:13px;
        border-bottom:2px solid ${state.tab === id ? '#1D9E75' : 'transparent'};
        color:${state.tab === id ? '#1D9E75' : '#555'};font-weight:${state.tab === id ? '600' : '400'};`;
      btn.addEventListener('click', () => StaffSection.setState({ tab: id }));
      tabBar.appendChild(btn);
    });
    el.appendChild(tabBar);

    // ── Active tab content ────────────────────────────────────────────────
    const content = document.createElement('div');
    if (state.tab === 'roster') _renderRosterTab(state, content, StaffSection);
    if (state.tab === 'shifts') _renderShiftsTab(state, content, StaffSection);
    if (state.tab === 'tips')   _renderTipsTab(state, content, StaffSection);
    el.appendChild(content);
  },

  async onMount(self) {
    try {
      const [rosterData, shiftsData] = await Promise.all([
        _staffFetch(''),
        _staffFetch('/open-shifts'),
      ]);
      self.setState({ staff: rosterData.staff, shifts: shiftsData.shifts, loading: false });
    } catch {
      self.setState({ loading: false });
    }
  },
});


// ─────────────────────────────────────────────────────────────────────────────
// loadStaffSection — called by dashboard-core.js when the user navigates to
// the 'staff' section. Mounts StaffSection into #staff-component on first
// visit; subsequent calls refresh open shifts data.
// ─────────────────────────────────────────────────────────────────────────────
let _staffMounted = false;

function loadStaffSection() {
  const el = document.getElementById('staff-component');
  if (!el) return;

  if (!_staffMounted) {
    _staffMounted = true;
    StaffSection.mount('#staff-component');
  } else {
    // Refresh open shifts each time the user revisits the tab
    _reloadShifts(StaffSection);
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
