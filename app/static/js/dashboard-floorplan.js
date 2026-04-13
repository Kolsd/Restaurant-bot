/* ── Floor Plan Component — Mesio Dashboard ─────────────────────────────────
   Renders restaurant tables as draggable elements on a relative canvas.
   Edit mode: drag to reposition, double-click to edit properties.
   View mode: click to inspect current session/occupancy.
   Positions are persisted via PUT /api/tables/{id}/position.
   Properties (capacity, table_type, zone) via PUT /api/tables/{id}/properties.
   ─────────────────────────────────────────────────────────────────────────── */

let _fpTables    = [];
let _fpEditMode  = false;
let _fpDirty     = false;          // tracks unsaved position changes
let _fpDragTarget = null;
let _fpDragOffset = { x: 0, y: 0 };

// ── Load ─────────────────────────────────────────────────────────────────────

async function loadFloorPlan() {
  const canvas = document.getElementById('fp-canvas');
  if (canvas) canvas.innerHTML = '<div style="padding:2rem;text-align:center;color:#aaa;font-size:13px;">Cargando mapa...</div>';
  try {
    const r = await fetch('/api/tables/floor-plan', { headers: window._dashHeaders });
    if (!r.ok) {
      if (canvas) canvas.innerHTML = '<div style="padding:2rem;text-align:center;color:#aaa;font-size:13px;">No se pudo cargar el mapa.</div>';
      return;
    }
    _fpTables = await r.json();
    _fpAutoLayout();
    _fpRender();
  } catch (e) {
    console.error('Floor plan load error', e);
    if (canvas) canvas.innerHTML = '<div style="padding:2rem;text-align:center;color:#ef4444;font-size:13px;">Error al cargar el mapa.</div>';
  }
}

// Auto-layout: assign grid positions to tables that have no position set yet
function _fpAutoLayout() {
  const COLS = 4;
  const CELL_W = 110;
  const CELL_H = 110;
  const PAD = 20;
  _fpTables.forEach((t, i) => {
    if (!t.position_x && !t.position_y) {
      t.position_x = PAD + (i % COLS) * CELL_W;
      t.position_y = PAD + Math.floor(i / COLS) * CELL_H;
    }
  });
}

// ── Render ───────────────────────────────────────────────────────────────────

function _fpRender() {
  const canvas = document.getElementById('fp-canvas');
  if (!canvas) return;
  canvas.innerHTML = ''; // only static structure, no user data injected via innerHTML

  // Ensure canvas is tall enough to hold all tables
  let maxBottom = 520;
  _fpTables.forEach(t => {
    const bottom = (t.position_y || 0) + 90 + 20;
    if (bottom > maxBottom) maxBottom = bottom;
  });
  canvas.style.minHeight = maxBottom + 'px';

  _fpTables.forEach(t => {
    const div = document.createElement('div');
    div.className = 'fp-table';
    div.dataset.id = String(t.id);

    const x = t.position_x || 0;
    const y = t.position_y || 0;
    const isBar = (t.table_type || '') === 'barra';

    // Base styles
    div.style.cssText = [
      'position:absolute',
      `left:${x}px`,
      `top:${y}px`,
      'width:86px',
      'height:86px',
      `border-radius:${isBar ? '10px' : '50%'}`,
      'display:flex',
      'flex-direction:column',
      'align-items:center',
      'justify-content:center',
      'font-size:12px',
      'font-weight:600',
      `cursor:${_fpEditMode ? 'grab' : 'pointer'}`,
      'border:2px solid',
      'transition:box-shadow 0.15s,transform 0.1s',
      'box-sizing:border-box',
      'padding:6px 4px',
      'gap:2px',
    ].join(';');

    // Color by status
    if (t.occupied) {
      div.style.background    = 'rgba(239,68,68,0.12)';
      div.style.borderColor   = '#ef4444';
    } else {
      div.style.background    = 'rgba(34,197,94,0.10)';
      div.style.borderColor   = '#22c55e';
    }

    // Hover effect (non-destructive: pure style, no data in innerHTML)
    div.addEventListener('mouseenter', () => {
      div.style.boxShadow = '0 4px 16px rgba(0,0,0,0.14)';
      if (!_fpEditMode) div.style.transform = 'scale(1.05)';
    });
    div.addEventListener('mouseleave', () => {
      div.style.boxShadow = '';
      div.style.transform = '';
    });

    // Content — all via textContent to avoid XSS
    const nameEl = document.createElement('div');
    nameEl.textContent = t.name || `Mesa ${t.number}`;
    nameEl.style.cssText = 'font-weight:700;font-size:11px;text-align:center;line-height:1.2;max-width:78px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
    div.appendChild(nameEl);

    const capEl = document.createElement('div');
    capEl.textContent = `${t.capacity || 4}p`;
    capEl.style.cssText = 'font-size:10px;opacity:0.65;';
    div.appendChild(capEl);

    if (t.zone) {
      const zoneEl = document.createElement('div');
      zoneEl.textContent = t.zone;
      zoneEl.style.cssText = 'font-size:9px;opacity:0.45;max-width:78px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      div.appendChild(zoneEl);
    }

    // Type badge
    if (t.table_type && t.table_type !== 'interior') {
      const typeEl = document.createElement('div');
      const typeLabels = { terraza: '☀️', barra: '🍺', privado: '🔒', vip: '⭐' };
      typeEl.textContent = typeLabels[t.table_type] || t.table_type;
      typeEl.style.cssText = 'font-size:11px;line-height:1;';
      div.appendChild(typeEl);
    }

    // Occupied badge
    if (t.occupied) {
      const occEl = document.createElement('div');
      occEl.textContent = '●';
      occEl.style.cssText = 'position:absolute;top:4px;right:6px;font-size:8px;color:#ef4444;';
      div.appendChild(occEl);
    }

    // Events
    if (_fpEditMode) {
      div.addEventListener('mousedown', e => _fpStartDrag(e, div));
      div.addEventListener('dblclick',  e => { e.stopPropagation(); _fpEditTable(t); });
    } else {
      div.addEventListener('click', () => _fpShowTableInfo(t));
    }

    canvas.appendChild(div);
  });

  // Edit-mode hint
  const hint = document.getElementById('fp-edit-hint');
  if (hint) hint.style.display = _fpEditMode ? '' : 'none';
}

// ── Edit mode toggle ─────────────────────────────────────────────────────────

function _fpToggleEdit() {
  _fpEditMode = !_fpEditMode;
  const btn = document.getElementById('fp-edit-btn');
  if (btn) btn.textContent = _fpEditMode ? '💾 Guardar' : '✏️ Editar';
  if (!_fpEditMode && _fpDirty) {
    _fpSavePositions();
    _fpDirty = false;
  }
  _fpRender();
}

// ── Drag ─────────────────────────────────────────────────────────────────────

function _fpStartDrag(e, el) {
  e.preventDefault();
  e.stopPropagation();
  _fpDragTarget = el;
  el.style.cursor = 'grabbing';
  el.style.zIndex = '10';

  const rect      = el.getBoundingClientRect();
  _fpDragOffset   = { x: e.clientX - rect.left, y: e.clientY - rect.top };

  function onMove(ev) {
    if (!_fpDragTarget) return;
    const canvasR = _fpDragTarget.parentElement.getBoundingClientRect();
    const newX    = Math.max(0, ev.clientX - canvasR.left  - _fpDragOffset.x);
    const newY    = Math.max(0, ev.clientY - canvasR.top   - _fpDragOffset.y);
    _fpDragTarget.style.left = newX + 'px';
    _fpDragTarget.style.top  = newY + 'px';
  }

  function onUp() {
    if (_fpDragTarget) {
      const id = _fpDragTarget.dataset.id;
      const t  = _fpTables.find(t => String(t.id) === id);
      if (t) {
        t.position_x = parseFloat(_fpDragTarget.style.left)  || 0;
        t.position_y = parseFloat(_fpDragTarget.style.top)   || 0;
        _fpDirty = true;
      }
      _fpDragTarget.style.cursor = 'grab';
      _fpDragTarget.style.zIndex = '';
    }
    _fpDragTarget = null;
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup',   onUp);
  }

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup',   onUp);
}

// ── Persist positions ────────────────────────────────────────────────────────

async function _fpSavePositions() {
  const errors = [];
  for (const t of _fpTables) {
    try {
      const r = await fetch(`/api/tables/${t.id}/position`, {
        method:  'PUT',
        headers: { ...window._dashHeaders, 'Content-Type': 'application/json' },
        body:    JSON.stringify({ position_x: t.position_x || 0, position_y: t.position_y || 0 }),
      });
      if (!r.ok) errors.push(t.id);
    } catch (e) {
      console.error('Floor plan save position error', t.id, e);
      errors.push(t.id);
    }
  }
  if (errors.length) {
    console.warn('Floor plan: failed to save positions for', errors);
  }
}

// ── Edit table properties (double-click in edit mode) ────────────────────────

async function _fpEditTable(t) {
  const capacity = prompt(`Capacidad (personas) para "${t.name || 'Mesa ' + t.number}":`, t.capacity || 4);
  if (capacity === null) return;
  const type = prompt('Tipo (interior / terraza / barra / privado / vip):', t.table_type || 'interior');
  if (type === null) return;
  const zone = prompt('Zona:', t.zone || '');
  if (zone === null) return;

  try {
    const r = await fetch(`/api/tables/${t.id}/properties`, {
      method:  'PUT',
      headers: { ...window._dashHeaders, 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        capacity:   parseInt(capacity, 10) || 4,
        table_type: type.trim() || 'interior',
        zone:       zone.trim(),
      }),
    });
    if (r.ok) {
      const updated = await r.json();
      const idx = _fpTables.findIndex(x => String(x.id) === String(t.id));
      if (idx !== -1) _fpTables[idx] = { ..._fpTables[idx], ...updated };
      _fpRender();
    }
  } catch (e) {
    console.error('Floor plan edit table error', t.id, e);
  }
}

// ── View-mode click info ──────────────────────────────────────────────────────

function _fpShowTableInfo(t) {
  const nameStr    = t.name || `Mesa ${t.number}`;
  const typeStr    = t.table_type || 'interior';
  const capStr     = t.capacity   || 4;
  const zoneStr    = t.zone       || 'N/A';
  const statusStr  = t.occupied
    ? `Ocupada — ${t.session_phone || 'cliente activo'}`
    : 'Libre';

  // Build info panel — reuse existing or create tooltip
  let panel = document.getElementById('fp-info-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'fp-info-panel';
    panel.style.cssText = [
      'position:fixed',
      'bottom:24px',
      'right:24px',
      'background:var(--card-bg,#fff)',
      'border:1px solid var(--border,#e0e0d8)',
      'border-radius:12px',
      'padding:14px 18px',
      'font-size:13px',
      'line-height:1.7',
      'box-shadow:0 8px 24px rgba(0,0,0,0.12)',
      'z-index:200',
      'min-width:200px',
      'max-width:260px',
    ].join(';');
    document.body.appendChild(panel);
  }

  // Title
  const title = document.createElement('div');
  title.textContent = nameStr;
  title.style.cssText = 'font-weight:700;font-size:15px;margin-bottom:6px;';

  const close = document.createElement('button');
  close.textContent = '✕';
  close.style.cssText = 'float:right;background:none;border:none;cursor:pointer;font-size:14px;color:#999;margin-left:8px;';
  close.addEventListener('click', () => panel.remove());

  const rows = [
    ['Tipo',      typeStr],
    ['Capacidad', `${capStr} personas`],
    ['Zona',      zoneStr],
    ['Estado',    statusStr],
  ];

  panel.innerHTML = '';
  panel.appendChild(close);
  panel.appendChild(title);

  rows.forEach(([label, value]) => {
    const row = document.createElement('div');
    const lbl = document.createElement('span');
    const val = document.createElement('span');
    lbl.textContent = label + ': ';
    lbl.style.cssText = 'color:#888;font-size:12px;';
    val.textContent = value;
    val.style.fontWeight = '500';
    row.appendChild(lbl);
    row.appendChild(val);
    panel.appendChild(row);
  });

  // Auto-dismiss after 5 seconds
  setTimeout(() => { if (panel.parentElement) panel.remove(); }, 5000);
}

// ── Refresh ───────────────────────────────────────────────────────────────────

function _fpRefresh() {
  // Close any open info panel
  const panel = document.getElementById('fp-info-panel');
  if (panel) panel.remove();
  loadFloorPlan();
}
