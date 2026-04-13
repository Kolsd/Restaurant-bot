/* ── Floor Plan Component v2 — Mesio Dashboard ──────────────────────────────
   Polished floor plan with:
   - Shape by table_type, size by capacity, 5-state color system
   - Pointer events (mouse + touch), snap-to-grid, zoom
   - Slide-in detail sidebar (replaces auto-dismiss toast)
   - Modal for property editing (replaces prompt())
   - Zone/status filters, 15s auto-refresh polling
   ─────────────────────────────────────────────────────────────────────────── */

let _fpTables       = [];
let _fpEditMode     = false;
let _fpDirty        = false;
let _fpDragTarget   = null;
let _fpDragOffset   = { x: 0, y: 0 };
let _fpZoomLevel    = 100;
let _fpSnapEnabled  = true;
let _fpSelectedId   = null;
let _fpPollTimer    = null;

const _fpSNAP_SIZE = 24; // matches CSS grid dot spacing

// ── Status mapping ──────────────────────────────────────────────────────────

function _fpGetStatus(t) {
  if (t.status === 'factura_entregada') return 'factura';
  if (t.status === 'reservada')         return 'reservada';
  if (t.occupied && t.session_status)   return 'ocupada';
  if (t.occupied)                       return 'sentados';
  return 'libre';
}

const _fpStatusLabels = {
  libre: 'Libre', sentados: 'Sentados', ocupada: 'Ocupada',
  factura: 'Facturando', reservada: 'Reservada',
};

// ── Size class by capacity ──────────────────────────────────────────────────

function _fpSizeClass(cap, type) {
  if (type === 'barra') {
    if (cap <= 2)  return 'fp-table--sm';
    if (cap <= 6)  return '';
    return cap <= 8 ? 'fp-table--lg' : 'fp-table--xl';
  }
  if (cap <= 2) return 'fp-table--sm';
  if (cap <= 4) return '';
  if (cap <= 6) return 'fp-table--lg';
  return 'fp-table--xl';
}

// ── Load ────────────────────────────────────────────────────────────────────

async function loadFloorPlan() {
  const canvas = document.getElementById('fp-canvas');
  if (!canvas) return;
  canvas.innerHTML = '';
  // Show loading skeleton
  const loadEl = document.createElement('div');
  loadEl.className = 'fp-empty';
  loadEl.innerHTML = '<div class="fp-empty__icon">🍽️</div><div class="fp-empty__title">Cargando mapa...</div>';
  canvas.appendChild(loadEl);

  try {
    const r = await fetch('/api/tables/floor-plan', { headers: window._dashHeaders });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _fpTables = await r.json();
    if (!_fpTables.length) {
      canvas.innerHTML = '';
      const empty = document.createElement('div');
      empty.className = 'fp-empty';
      empty.innerHTML = '<div class="fp-empty__icon">🗺️</div>'
        + '<div class="fp-empty__title">Sin mesas configuradas</div>'
        + '<div class="fp-empty__text">Agrega mesas desde la pestaña "Mesas & QR" para verlas aquí.</div>';
      canvas.appendChild(empty);
      return;
    }
    _fpAutoLayout();
    _fpPopulateZoneFilter();
    _fpRender();
    _fpStartPolling();
  } catch (e) {
    console.error('Floor plan load error', e);
    canvas.innerHTML = '';
    const err = document.createElement('div');
    err.className = 'fp-empty';
    err.innerHTML = '<div class="fp-empty__icon">⚠️</div><div class="fp-empty__title">Error al cargar el mapa</div>';
    canvas.appendChild(err);
  }
}

// Auto-layout for tables without positions
function _fpAutoLayout() {
  const COLS = 4, CELL = 130, PAD = 30;
  _fpTables.forEach((t, i) => {
    if (!t.position_x && !t.position_y) {
      t.position_x = PAD + (i % COLS) * CELL;
      t.position_y = PAD + Math.floor(i / COLS) * CELL;
    }
  });
}

// Populate zone filter from data
function _fpPopulateZoneFilter() {
  const sel = document.getElementById('fp-zone-filter');
  if (!sel) return;
  const zones = [...new Set(_fpTables.map(t => t.zone).filter(Boolean))].sort();
  // Keep first option, remove rest
  while (sel.options.length > 1) sel.remove(1);
  zones.forEach(z => {
    const opt = document.createElement('option');
    opt.value = z;
    opt.textContent = z;
    sel.appendChild(opt);
  });
}

// ── Render ───────────────────────────────────────────────────────────────────

function _fpRender() {
  const canvas = document.getElementById('fp-canvas');
  if (!canvas) return;
  canvas.innerHTML = '';

  // Canvas height
  let maxBottom = 560;
  _fpTables.forEach(t => {
    const bottom = (t.position_y || 0) + 140;
    if (bottom > maxBottom) maxBottom = bottom;
  });
  canvas.style.minHeight = maxBottom + 'px';

  // Edit mode classes
  const wrap = document.getElementById('fp-canvas-wrap');
  const banner = document.getElementById('fp-edit-banner');
  if (wrap) wrap.classList.toggle('fp-canvas-wrap--edit', _fpEditMode);
  if (banner) banner.classList.toggle('fp-edit-banner--visible', _fpEditMode);

  // Get filters
  const zoneFilter = (document.getElementById('fp-zone-filter') || {}).value || '';
  const statusFilter = (document.getElementById('fp-status-filter') || {}).value || '';

  _fpTables.forEach(t => {
    const status = _fpGetStatus(t);
    const type = t.table_type || 'interior';
    const cap = t.capacity || 4;

    // Apply filters
    if (zoneFilter && (t.zone || '') !== zoneFilter) return;
    if (statusFilter && status !== statusFilter) return;

    const div = document.createElement('div');
    const classes = [
      'fp-table',
      'fp-table--' + type,
      'fp-table--' + status,
      _fpSizeClass(cap, type),
    ].filter(Boolean);

    if (_fpEditMode) classes.push('fp-table--editable');
    if (String(t.id) === String(_fpSelectedId)) classes.push('fp-table--selected');

    div.className = classes.join(' ');
    div.dataset.id = String(t.id);
    div.style.left = (t.position_x || 0) + 'px';
    div.style.top = (t.position_y || 0) + 'px';

    // Name
    const nameEl = document.createElement('div');
    nameEl.className = 'fp-table__name';
    nameEl.textContent = t.name || ('Mesa ' + t.number);
    div.appendChild(nameEl);

    // Capacity
    const capEl = document.createElement('div');
    capEl.className = 'fp-table__cap';
    capEl.textContent = cap + 'p';
    div.appendChild(capEl);

    // Type badge (non-interior)
    if (type !== 'interior') {
      const badge = document.createElement('div');
      badge.className = 'fp-table__type-badge fp-table__type-badge--' + type;
      const labels = { terraza: 'TERRAZA', barra: 'BARRA', privado: 'PRIVADO', vip: 'VIP' };
      badge.textContent = labels[type] || type.toUpperCase();
      div.appendChild(badge);
    }

    // Status indicator dot
    if (status !== 'libre') {
      const dot = document.createElement('div');
      dot.className = 'fp-table__indicator fp-table__indicator--' + status;
      div.appendChild(dot);
    }

    // Events — use pointer events for mouse+touch
    if (_fpEditMode) {
      div.addEventListener('pointerdown', e => _fpStartDrag(e, div));
      div.addEventListener('dblclick', e => { e.stopPropagation(); _fpEditTableModal(t); });
    } else {
      div.addEventListener('click', e => { e.stopPropagation(); _fpShowDetail(t); });
    }

    canvas.appendChild(div);
  });

  // Click on canvas background closes detail
  canvas.addEventListener('click', _fpCloseDetail);
}

// ── Edit mode toggle ────────────────────────────────────────────────────────

function _fpToggleEdit() {
  _fpEditMode = !_fpEditMode;
  const btn = document.getElementById('fp-edit-btn');
  if (btn) {
    if (_fpEditMode) {
      btn.textContent = 'Guardar';
      btn.className = 'fp-btn fp-btn--save';
    } else {
      btn.textContent = 'Editar';
      btn.className = 'fp-btn fp-btn--primary';
    }
  }
  if (!_fpEditMode && _fpDirty) {
    _fpSavePositions();
    _fpDirty = false;
  }
  _fpCloseDetail();
  _fpRender();
}

// ── Snap toggle ─────────────────────────────────────────────────────────────

function _fpToggleSnap() {
  _fpSnapEnabled = !_fpSnapEnabled;
  const btn = document.getElementById('fp-snap-btn');
  if (btn) btn.textContent = 'Snap: ' + (_fpSnapEnabled ? 'ON' : 'OFF');
}

// ── Zoom ────────────────────────────────────────────────────────────────────

function _fpZoom(dir) {
  _fpZoomLevel = Math.max(50, Math.min(150, _fpZoomLevel + dir * 10));
  const canvas = document.getElementById('fp-canvas');
  const label = document.getElementById('fp-zoom-label');
  if (canvas) canvas.style.transform = 'scale(' + (_fpZoomLevel / 100) + ')';
  if (label) label.textContent = _fpZoomLevel + '%';
}

// ── Drag (pointer events for mouse + touch) ─────────────────────────────────

function _fpStartDrag(e, el) {
  e.preventDefault();
  e.stopPropagation();
  _fpDragTarget = el;
  el.classList.add('fp-table--dragging');
  el.setPointerCapture(e.pointerId);

  const rect = el.getBoundingClientRect();
  const scale = _fpZoomLevel / 100;
  _fpDragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };

  function onMove(ev) {
    if (!_fpDragTarget) return;
    const canvasR = _fpDragTarget.parentElement.getBoundingClientRect();
    let newX = (ev.clientX - canvasR.left - _fpDragOffset.x) / scale;
    let newY = (ev.clientY - canvasR.top - _fpDragOffset.y) / scale;
    // Clamp to canvas
    newX = Math.max(0, newX);
    newY = Math.max(0, newY);
    // Snap to grid
    if (_fpSnapEnabled) {
      newX = Math.round(newX / _fpSNAP_SIZE) * _fpSNAP_SIZE;
      newY = Math.round(newY / _fpSNAP_SIZE) * _fpSNAP_SIZE;
    }
    _fpDragTarget.style.left = newX + 'px';
    _fpDragTarget.style.top = newY + 'px';
  }

  function onUp(ev) {
    if (_fpDragTarget) {
      const id = _fpDragTarget.dataset.id;
      const t = _fpTables.find(t => String(t.id) === id);
      if (t) {
        t.position_x = parseFloat(_fpDragTarget.style.left) || 0;
        t.position_y = parseFloat(_fpDragTarget.style.top) || 0;
        _fpDirty = true;
      }
      _fpDragTarget.classList.remove('fp-table--dragging');
    }
    _fpDragTarget = null;
    el.removeEventListener('pointermove', onMove);
    el.removeEventListener('pointerup', onUp);
  }

  el.addEventListener('pointermove', onMove);
  el.addEventListener('pointerup', onUp);
}

// ── Persist positions ───────────────────────────────────────────────────────

async function _fpSavePositions() {
  const btn = document.getElementById('fp-edit-btn');
  const origText = btn ? btn.textContent : '';
  if (btn) btn.textContent = 'Guardando...';

  const errors = [];
  // Save in parallel batches of 5
  for (let i = 0; i < _fpTables.length; i += 5) {
    const batch = _fpTables.slice(i, i + 5);
    const results = await Promise.allSettled(batch.map(t =>
      fetch('/api/tables/' + t.id + '/position', {
        method: 'PUT',
        headers: { ...window._dashHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify({ position_x: t.position_x || 0, position_y: t.position_y || 0 }),
      }).then(r => { if (!r.ok) throw new Error(t.id); })
    ));
    results.forEach((r, j) => { if (r.status === 'rejected') errors.push(batch[j].id); });
  }

  if (btn) btn.textContent = origText;
  if (errors.length) console.warn('Floor plan: failed to save positions for', errors);
}

// ── Edit table properties — modal (replaces prompt()) ───────────────────────

function _fpEditTableModal(t) {
  const zones = [...new Set(_fpTables.map(x => x.zone).filter(Boolean))];

  const overlay = document.createElement('div');
  overlay.className = 'fp-modal-overlay';

  const modal = document.createElement('div');
  modal.className = 'fp-modal';

  // Title
  const title = document.createElement('div');
  title.className = 'fp-modal__title';
  title.textContent = 'Editar ' + (t.name || 'Mesa ' + t.number);
  modal.appendChild(title);

  // Capacity
  const capField = _fpModalField('Capacidad', 'number', t.capacity || 4, 'fp-ed-cap');
  modal.appendChild(capField);

  // Type
  const typeField = document.createElement('div');
  typeField.className = 'fp-modal__field';
  const typeLabel = document.createElement('label');
  typeLabel.textContent = 'Tipo';
  typeLabel.setAttribute('for', 'fp-ed-type');
  typeField.appendChild(typeLabel);
  const typeSel = document.createElement('select');
  typeSel.id = 'fp-ed-type';
  ['interior', 'terraza', 'barra', 'privado', 'vip'].forEach(v => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v.charAt(0).toUpperCase() + v.slice(1);
    if (v === (t.table_type || 'interior')) opt.selected = true;
    typeSel.appendChild(opt);
  });
  typeField.appendChild(typeSel);
  modal.appendChild(typeField);

  // Zone with datalist
  const zoneField = _fpModalField('Zona', 'text', t.zone || '', 'fp-ed-zone');
  if (zones.length) {
    const dl = document.createElement('datalist');
    dl.id = 'fp-ed-zone-list';
    zones.forEach(z => { const o = document.createElement('option'); o.value = z; dl.appendChild(o); });
    zoneField.appendChild(dl);
    zoneField.querySelector('input').setAttribute('list', 'fp-ed-zone-list');
  }
  modal.appendChild(zoneField);

  // Footer
  const footer = document.createElement('div');
  footer.className = 'fp-modal__footer';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'fp-btn fp-btn--outline';
  cancelBtn.textContent = 'Cancelar';
  cancelBtn.addEventListener('click', () => overlay.remove());

  const saveBtn = document.createElement('button');
  saveBtn.className = 'fp-btn fp-btn--primary';
  saveBtn.textContent = 'Guardar';
  saveBtn.addEventListener('click', async () => {
    const cap = parseInt(document.getElementById('fp-ed-cap').value, 10) || 4;
    const type = document.getElementById('fp-ed-type').value || 'interior';
    const zone = (document.getElementById('fp-ed-zone').value || '').trim();

    saveBtn.textContent = 'Guardando...';
    saveBtn.disabled = true;
    try {
      const r = await fetch('/api/tables/' + t.id + '/properties', {
        method: 'PUT',
        headers: { ...window._dashHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify({ capacity: cap, table_type: type, zone: zone }),
      });
      if (r.ok) {
        const updated = await r.json();
        const idx = _fpTables.findIndex(x => String(x.id) === String(t.id));
        if (idx !== -1) _fpTables[idx] = { ..._fpTables[idx], ...updated };
        _fpPopulateZoneFilter();
        _fpRender();
      }
    } catch (e) {
      console.error('Floor plan edit error', t.id, e);
    }
    overlay.remove();
  });

  footer.appendChild(cancelBtn);
  footer.appendChild(saveBtn);
  modal.appendChild(footer);

  overlay.appendChild(modal);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);

  // Focus first field
  setTimeout(() => { const inp = document.getElementById('fp-ed-cap'); if (inp) inp.focus(); }, 50);
}

function _fpModalField(label, type, value, id) {
  const wrap = document.createElement('div');
  wrap.className = 'fp-modal__field';
  const lbl = document.createElement('label');
  lbl.textContent = label;
  lbl.setAttribute('for', id);
  wrap.appendChild(lbl);
  const inp = document.createElement('input');
  inp.type = type;
  inp.id = id;
  inp.value = value;
  if (type === 'number') { inp.min = '1'; inp.max = '30'; }
  wrap.appendChild(inp);
  return wrap;
}

// ── Detail sidebar ──────────────────────────────────────────────────────────

function _fpShowDetail(t) {
  _fpSelectedId = t.id;

  const detail = document.getElementById('fp-detail');
  const titleEl = document.getElementById('fp-detail-title');
  const statusEl = document.getElementById('fp-detail-status');
  const body = document.getElementById('fp-detail-body');
  const actions = document.getElementById('fp-detail-actions');
  if (!detail) return;

  const status = _fpGetStatus(t);
  const type = t.table_type || 'interior';

  // Title
  titleEl.textContent = t.name || ('Mesa ' + t.number);

  // Status badge
  statusEl.textContent = _fpStatusLabels[status] || status;
  statusEl.className = 'fp-detail__status fp-detail__status--' + status;

  // Body rows
  body.innerHTML = '';
  const rows = [
    ['Tipo', type.charAt(0).toUpperCase() + type.slice(1)],
    ['Capacidad', (t.capacity || 4) + ' personas'],
    ['Zona', t.zone || '—'],
  ];

  if (t.occupied && t.session_phone) {
    rows.push(['Cliente', t.session_phone]);
  }
  if (t.session_id) {
    rows.push(['Sesión', '#' + String(t.session_id).slice(0, 8)]);
  }

  rows.forEach(([label, value]) => {
    const row = document.createElement('div');
    row.className = 'fp-detail__row';
    const lbl = document.createElement('span');
    lbl.className = 'fp-detail__label';
    lbl.textContent = label;
    const val = document.createElement('span');
    val.className = 'fp-detail__value';
    val.textContent = value;
    row.appendChild(lbl);
    row.appendChild(val);
    body.appendChild(row);
  });

  // Actions
  actions.innerHTML = '';
  if (status === 'libre') {
    _fpDetailBtn(actions, 'Abrir mesa', 'primary');
  } else if (status === 'sentados' || status === 'ocupada') {
    _fpDetailBtn(actions, 'Ver pedido', 'primary');
  } else if (status === 'factura') {
    _fpDetailBtn(actions, 'Cobrar', 'primary');
  }

  // Highlight selected table
  _fpHighlightSelected();

  detail.classList.add('fp-detail--open');
}

function _fpDetailBtn(container, text, style) {
  const btn = document.createElement('button');
  btn.className = 'fp-detail__action-btn fp-detail__action-btn--' + style;
  btn.textContent = text;
  container.appendChild(btn);
}

function _fpCloseDetail() {
  const detail = document.getElementById('fp-detail');
  if (detail) detail.classList.remove('fp-detail--open');
  _fpSelectedId = null;
  _fpHighlightSelected();
}

function _fpHighlightSelected() {
  const canvas = document.getElementById('fp-canvas');
  if (!canvas) return;
  canvas.querySelectorAll('.fp-table--selected').forEach(el => el.classList.remove('fp-table--selected'));
  if (_fpSelectedId) {
    const el = canvas.querySelector('[data-id="' + _fpSelectedId + '"]');
    if (el) el.classList.add('fp-table--selected');
  }
}

// ── Filters ─────────────────────────────────────────────────────────────────

function _fpApplyFilters() {
  _fpRender();
}

// ── Polling (15s auto-refresh) ──────────────────────────────────────────────

function _fpStartPolling() {
  _fpStopPolling();
  _fpPollTimer = setInterval(async () => {
    if (_fpEditMode) return;
    try {
      const r = await fetch('/api/tables/floor-plan', { headers: window._dashHeaders });
      if (!r.ok) return;
      const fresh = await r.json();
      // Merge status data without changing positions
      fresh.forEach(ft => {
        const local = _fpTables.find(t => String(t.id) === String(ft.id));
        if (local) {
          local.occupied = ft.occupied;
          local.status = ft.status;
          local.session_id = ft.session_id;
          local.session_phone = ft.session_phone;
          local.session_status = ft.session_status;
        }
      });
      _fpRender();
    } catch (e) { /* silent */ }
  }, 15000);
}

function _fpStopPolling() {
  if (_fpPollTimer) { clearInterval(_fpPollTimer); _fpPollTimer = null; }
}

// ── Refresh ─────────────────────────────────────────────────────────────────

function _fpRefresh() {
  _fpCloseDetail();
  _fpStopPolling();
  loadFloorPlan();
}
