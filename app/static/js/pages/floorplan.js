/* ══ Floor plan page ════════════════════════════════════════════════
   Phase 1 — Adaptive layout + dynamic zones
   Phase 2 — Edit layout mode (drag-to-reposition, create, delete)
   Phase 3 — QR view + real detail-panel actions
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

// ── Auth guard ────────────────────────────────────────────────────
(function () {
  if (!localStorage.getItem('rb_token')) { window.location.href = '/login'; }
})();

// ── State ─────────────────────────────────────────────────────────
var _tables = [];
var _selectedTableId = null;
var _refreshTimer = null;
var _activeZone = 'all';
var _editMode = false;
var _currentView = 'map'; // 'map' | 'qr'
var _propsTableId = null; // which table is open in props modal
var _dragState = null;    // {tableId, startX, startY, origX, origY, el}

// ── Status maps ───────────────────────────────────────────────────
var STATUS_CLASS = {
  free:      '',
  available: '',
  seated:    'sent',
  eating:    'occ',
  billing:   'fac',
  reserved:  'res',
  occupied:  'occ'
};

var STATUS_LABEL = {
  free:      'Libre',
  available: 'Libre',
  seated:    'Sentados',
  eating:    'COMIENDO',
  billing:   'PIDIÓ CUENTA',
  reserved:  'Reservada',
  occupied:  'COMIENDO'
};

// ── Helpers ───────────────────────────────────────────────────────
function el(id) { return document.getElementById(id); }

function _hasCustomPositions(tables) {
  return tables.some(function (t) {
    return (t.position_x != null && t.position_x !== 0) ||
           (t.position_y != null && t.position_y !== 0);
  });
}

function _distinctZones(tables) {
  var seen = {};
  var zones = [];
  tables.forEach(function (t) {
    var z = (t.zone || 'Salón').trim();
    if (!seen[z]) { seen[z] = true; zones.push(z); }
  });
  return zones;
}

function _tileSize(capacity) {
  var base = capacity ? Math.max(96, Math.min(140, 60 + capacity * 10)) : 96;
  return base;
}

// ── Build zone datalist for modals ────────────────────────────────
function _populateZoneDatalist(listId) {
  var dl = el(listId);
  if (!dl) return;
  dl.innerHTML = '';
  var zones = _distinctZones(_tables);
  zones.forEach(function (z) {
    var opt = document.createElement('option');
    opt.value = z;
    dl.appendChild(opt);
  });
}

// ── Fetch floor plan ──────────────────────────────────────────────
async function loadFloorPlan() {
  var badge = el('refreshBadge');
  if (badge) badge.classList.add('loading');

  try {
    var res = await fetch('/api/tables/floor-plan', { headers: mesioHeaders() });
    mesioTrackFetch(res.ok);
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    _tables = Array.isArray(data) ? data : (data.tables || []);
    console.log('[floorplan] /api/tables/floor-plan:', _tables.length, 'tables'); // lint-allow: diagnostic for production debug
    _rebuildZoneFilter();
    renderTables();
    if (_currentView === 'qr') renderQrGrid();
    if (_selectedTableId) selectTable(_selectedTableId);
    updateRefreshBadge();
    _populateReservaTables();
  } catch (e) {
    mesioTrackFetch(false);
    console.warn('floor-plan fetch failed:', e);
    mesioToast('Error cargando mesas: ' + e.message, 'error');
    // Show error hint in empty state so user can diagnose
    var emptyEl = el('canvasEmpty');
    if (emptyEl) emptyEl.style.display = '';
    var errHint = el('canvasEmptyError');
    if (errHint) { errHint.textContent = 'Error: ' + e.message; errHint.style.display = ''; }
  } finally {
    if (badge) badge.classList.remove('loading');
  }
}

// ── Dynamic zone filter ───────────────────────────────────────────
function _rebuildZoneFilter() {
  var container = el('zoneFilter');
  if (!container) return;
  var zones = _distinctZones(_tables);

  // Count per zone
  var counts = {};
  _tables.forEach(function (t) {
    var z = (t.zone || 'Salón').trim();
    counts[z] = (counts[z] || 0) + 1;
  });

  // Rebuild buttons: Todas + one per zone
  container.innerHTML = '';
  var allBtn = document.createElement('button');
  allBtn.className = 'seg-btn' + (_activeZone === 'all' ? ' active' : '');
  allBtn.dataset.zone = 'all';
  allBtn.textContent = 'Todas';
  container.appendChild(allBtn);

  zones.forEach(function (z) {
    var btn = document.createElement('button');
    var key = z.toLowerCase();
    btn.className = 'seg-btn' + (_activeZone === key ? ' active' : '');
    btn.dataset.zone = key;
    btn.textContent = z + ' · ' + (counts[z] || 0);
    container.appendChild(btn);
  });

  // Bind clicks
  container.querySelectorAll('.seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      container.querySelectorAll('.seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      _activeZone = btn.dataset.zone;
      renderTables();
    });
  });
}

// ── Render table tiles ────────────────────────────────────────────
function renderTables() {
  var canvas = el('fpCanvas');
  if (!canvas) return;

  // Remove existing dynamic tiles
  canvas.querySelectorAll('.t, .fp-zone-swimlane').forEach(function (n) { n.remove(); });

  // Show empty state when 0 tables
  var emptyEl = el('canvasEmpty');
  if (_tables.length === 0) {
    if (emptyEl) emptyEl.style.display = '';
    canvas.classList.remove('grid-mode');
    return;
  }
  if (emptyEl) emptyEl.style.display = 'none';

  var filtered = _tables.filter(function (t) {
    if (_activeZone === 'all') return true;
    return (t.zone || 'Salón').trim().toLowerCase() === _activeZone;
  });

  // Grid mode by default for predictable layout. Absolute mode only inside edit
  // mode (when user is actively dragging tiles to a custom layout).
  var useGrid = !_editMode || !_hasCustomPositions(_tables);
  canvas.classList.toggle('grid-mode', useGrid);

  if (useGrid) {
    _renderGridMode(canvas, filtered);
  } else {
    _renderAbsoluteMode(canvas, filtered);
  }
}

function _renderGridMode(canvas, tables) {
  // Group by zone
  var groups = {};
  var order = [];
  tables.forEach(function (t) {
    var z = (t.zone || 'Salón').trim();
    if (!groups[z]) { groups[z] = []; order.push(z); }
    groups[z].push(t);
  });

  order.forEach(function (zone) {
    var swimlane = document.createElement('div');
    swimlane.className = 'fp-zone-swimlane';

    var header = document.createElement('div');
    header.className = 'fp-zone-header';
    var nameSpan = document.createElement('span');
    nameSpan.className = 'fp-zone-name';
    nameSpan.textContent = zone;
    var countSpan = document.createElement('span');
    countSpan.className = 'fp-zone-count';
    countSpan.textContent = groups[zone].length;
    header.appendChild(nameSpan);
    header.appendChild(countSpan);
    swimlane.appendChild(header);

    var tilesRow = document.createElement('div');
    tilesRow.className = 'fp-zone-tiles';
    groups[zone].forEach(function (tbl) {
      tilesRow.appendChild(_buildTile(tbl));
    });
    swimlane.appendChild(tilesRow);
    canvas.appendChild(swimlane);
  });
}

function _renderAbsoluteMode(canvas, tables) {
  // Compute canvas minimum size so tiles don't go off-screen
  var maxX = 0; var maxY = 0;
  tables.forEach(function (t) {
    var x = t.position_x || 0;
    var y = t.position_y || 0;
    var s = _tileSize(t.capacity);
    if (x + s + 20 > maxX) maxX = x + s + 20;
    if (y + s + 20 > maxY) maxY = y + s + 20;
  });
  canvas.style.minWidth = maxX + 'px';
  canvas.style.minHeight = maxY + 'px';

  tables.forEach(function (tbl) {
    var tile = _buildTile(tbl);
    var x = tbl.position_x || 0;
    var y = tbl.position_y || 0;
    var s = _tileSize(tbl.capacity);
    tile.style.left = x + 'px';
    tile.style.top  = y + 'px';
    tile.style.width  = s + 'px';
    tile.style.height = s + 'px';
    canvas.appendChild(tile);
  });
}

function _buildTile(tbl) {
  var tile = document.createElement('div');
  tile.className = 't';
  var status = (tbl.status || 'free').toLowerCase();
  var cls = STATUS_CLASS[status] || '';
  if (cls) tile.classList.add(cls);
  if (String(tbl.id) === String(_selectedTableId)) tile.classList.add('sel');

  // Tooltip
  var cap = tbl.capacity ? tbl.capacity + ' personas' : '';
  var typ = tbl.table_type ? ' · ' + tbl.table_type : '';
  var zon = tbl.zone ? ' · ' + tbl.zone : '';
  tile.title = [cap, typ, zon].filter(Boolean).join('') || 'Mesa';

  // Number label
  var numDiv = document.createElement('div');
  numDiv.className = 'tn';
  numDiv.textContent = tbl.table_number || tbl.id;
  tile.appendChild(numDiv);

  // Status + capacity inline: "LIBRE · 4P"
  var stDiv = document.createElement('div');
  stDiv.className = 'st';
  var stLabel = STATUS_LABEL[status] || status;
  if (tbl.capacity) {
    stLabel += ' · ' + tbl.capacity + 'P';
  }
  stDiv.textContent = stLabel;
  tile.appendChild(stDiv);

  // Reserved: show reservation time and note if available
  if (status === 'reserved' && tbl.next_reservation) {
    var resInfo = tbl.next_reservation;
    var resDiv = document.createElement('div');
    resDiv.className = 'tile-res-info';
    if (resInfo.time) {
      var timeSpan = document.createTextNode('RESERVA ' + resInfo.time);
      resDiv.appendChild(timeSpan);
    }
    if (resInfo.note || resInfo.party_size) {
      var noteDiv = document.createElement('div');
      noteDiv.className = 'tile-res-note';
      var noteParts = [];
      if (resInfo.note) noteParts.push(resInfo.note);
      if (resInfo.party_size) noteParts.push(resInfo.party_size + 'p');
      noteDiv.textContent = noteParts.join(' · ');
      resDiv.appendChild(noteDiv);
    }
    tile.appendChild(resDiv);
  }

  // Current total
  if (tbl.current_total) {
    var mtDiv = document.createElement('div');
    mtDiv.className = 'mt';
    mtDiv.textContent = mesioFmt(tbl.current_total);
    tile.appendChild(mtDiv);
  }

  // Alert badge
  if (tbl.has_alert || tbl.waiter_called) {
    var alertDiv = document.createElement('div');
    alertDiv.className = 'alert';
    alertDiv.textContent = 'LLAMA';
    tile.appendChild(alertDiv);
  }

  tile.dataset.tableId = tbl.id;

  // Click: select (unless we're dragging)
  tile.addEventListener('click', function (e) {
    if (_editMode && e._wasDrag) return;
    if (!_editMode) { selectTable(tbl.id); }
  });

  // Right-click in edit mode: open props modal
  tile.addEventListener('contextmenu', function (e) {
    if (!_editMode) return;
    e.preventDefault();
    _openPropsModal(tbl.id);
  });

  // Drag in edit mode
  if (_editMode) {
    _attachDrag(tile, tbl);
  }

  return tile;
}

// ── Phase 2: Drag-to-reposition ───────────────────────────────────
function _attachDrag(tile, tbl) {
  tile.addEventListener('pointerdown', function (e) {
    if (!_editMode) return;
    e.preventDefault();
    var canvas = el('fpCanvas');
    var rect = canvas.getBoundingClientRect();
    var startClientX = e.clientX;
    var startClientY = e.clientY;
    var origLeft = parseFloat(tile.style.left) || 0;
    var origTop  = parseFloat(tile.style.top)  || 0;
    var moved = false;

    tile.setPointerCapture(e.pointerId);
    tile.style.zIndex = '200';
    tile.style.transition = 'none';

    function onMove(ev) {
      var dx = ev.clientX - startClientX;
      var dy = ev.clientY - startClientY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
      tile.style.left = Math.max(0, origLeft + dx) + 'px';
      tile.style.top  = Math.max(0, origTop  + dy) + 'px';
    }

    async function onUp(ev) {
      tile.removeEventListener('pointermove', onMove);
      tile.removeEventListener('pointerup', onUp);
      tile.style.zIndex = '';
      tile.style.transition = '';

      if (!moved) return;
      ev._wasDrag = true;

      var newX = parseFloat(tile.style.left) || 0;
      var newY = parseFloat(tile.style.top)  || 0;

      // Persist position
      try {
        var r = await fetch('/api/tables/' + tbl.id + '/position', {
          method: 'PUT',
          headers: mesioHeaders(),
          body: JSON.stringify({ position_x: newX, position_y: newY })
        });
        mesioTrackFetch(r.ok);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        // Update local state
        var t = _tables.find(function (t) { return String(t.id) === String(tbl.id); });
        if (t) { t.position_x = newX; t.position_y = newY; }
        mesioToast('Posición guardada', 'success');
      } catch (err) {
        mesioToast('Error guardando posición', 'error');
        tile.style.left = origLeft + 'px';
        tile.style.top  = origTop  + 'px';
      }
    }

    tile.addEventListener('pointermove', onMove);
    tile.addEventListener('pointerup', onUp);
  });
}

// ── Phase 2: Edit mode toggle ─────────────────────────────────────
function _enterEditMode() {
  _editMode = true;
  document.body.classList.add('editing');
  var btn = el('editLayoutBtn');
  if (btn) { btn.textContent = '✓ Modo edición'; btn.classList.add('brand'); }
  var tb = el('editToolbar');
  if (tb) tb.style.display = '';
  renderTables();
}

function _exitEditMode() {
  _editMode = false;
  document.body.classList.remove('editing');
  var btn = el('editLayoutBtn');
  if (btn) { btn.textContent = 'Editar layout'; btn.classList.remove('brand'); }
  var tb = el('editToolbar');
  if (tb) tb.style.display = 'none';
  renderTables();
}

// ── Phase 2: Props modal ──────────────────────────────────────────
function _openPropsModal(tableId) {
  _propsTableId = tableId;
  var tbl = _tables.find(function (t) { return String(t.id) === String(tableId); });
  if (!tbl) return;

  el('propNum').value      = tbl.table_number || '';
  el('propName').value     = tbl.name || '';
  el('propCapacity').value = tbl.capacity || '';
  el('propType').value     = tbl.table_type || 'interior';
  el('propZone').value     = tbl.zone || '';
  _populateZoneDatalist('propZoneList');

  el('propsModal').classList.add('open');
}

async function _saveProps() {
  if (!_propsTableId) return;
  var body = {
    name:       el('propName').value.trim() || null,
    capacity:   parseInt(el('propCapacity').value) || null,
    table_type: el('propType').value,
    zone:       el('propZone').value.trim() || null
  };
  try {
    var r = await fetch('/api/tables/' + _propsTableId + '/properties', {
      method: 'PUT',
      headers: mesioHeaders(),
      body: JSON.stringify(body)
    });
    mesioTrackFetch(r.ok);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    mesioToast('Mesa actualizada', 'success');
    el('propsModal').classList.remove('open');
    await loadFloorPlan();
  } catch (e) {
    mesioToast('Error al guardar: ' + e.message, 'error');
  }
}

async function _deleteTable() {
  if (!_propsTableId) return;
  var tbl = _tables.find(function (t) { return String(t.id) === String(_propsTableId); });
  var name = tbl ? 'Mesa ' + (tbl.table_number || _propsTableId) : 'esta mesa';
  var ok = await mesioConfirm('¿Eliminar ' + name + '? Esta acción no se puede deshacer.', { confirmText: 'Eliminar', danger: true });
  if (!ok) return;
  try {
    var r = await fetch('/api/tables/' + _propsTableId, {
      method: 'DELETE',
      headers: mesioHeaders()
    });
    mesioTrackFetch(r.ok);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    mesioToast('Mesa eliminada', 'success');
    el('propsModal').classList.remove('open');
    if (String(_selectedTableId) === String(_propsTableId)) closePanel();
    await loadFloorPlan();
  } catch (e) {
    mesioToast('Error al eliminar: ' + e.message, 'error');
  }
}

// ── Phase 2: Create table modal ───────────────────────────────────
function _openNewTableModal() {
  el('ntCapacity').value = '4';
  el('ntType').value = 'interior';
  el('ntZone').value = '';
  _populateZoneDatalist('ntZoneList');
  el('newTableModal').classList.add('open');
}

async function _createTable() {
  // Step 1: POST /api/tables with empty body — backend auto-generates number/name
  try {
    var r = await fetch('/api/tables', {
      method: 'POST',
      headers: mesioHeaders(),
      body: JSON.stringify({})
    });
    mesioTrackFetch(r.ok);
    if (!r.ok) {
      var err = await r.json().catch(function () { return {}; });
      throw new Error(err.detail || 'HTTP ' + r.status);
    }
    var created = await r.json();
    var tableId = created.table_id;
    var tableName = created.name || ('Mesa ' + tableId);

    // Step 2: if user filled capacity/type/zone, apply via PUT /api/tables/{id}/properties
    var capacity   = parseInt(el('ntCapacity').value) || null;
    var tableType  = el('ntType').value;
    var zone       = el('ntZone').value.trim() || null;
    var hasProps   = capacity || tableType !== 'interior' || zone;

    if (hasProps && tableId) {
      var propsBody = {};
      if (capacity)              propsBody.capacity   = capacity;
      if (tableType)             propsBody.table_type = tableType;
      if (zone)                  propsBody.zone       = zone;

      var r2 = await fetch('/api/tables/' + tableId + '/properties', {
        method: 'PUT',
        headers: mesioHeaders(),
        body: JSON.stringify(propsBody)
      });
      mesioTrackFetch(r2.ok);
      // Non-fatal: table already created, props failure just means defaults remain
      if (!r2.ok) {
        console.warn('[floorplan] properties update failed for', tableId, r2.status); // lint-allow: diagnostic for production debug
      }
    }

    mesioToast(tableName + ' creada', 'success');
    el('newTableModal').classList.remove('open');
    await loadFloorPlan();
  } catch (e) {
    mesioToast('Error al crear: ' + e.message, 'error');
  }
}

// ── Phase 2: Bulk save layout ─────────────────────────────────────
// The drag handler already persists each position on drop via the per-table
// PUT /position. This function bundles the WHOLE current layout (positions +
// props) into one POST so the user gets explicit "saved" feedback after a
// long editing session and any drag-failures get a chance to retry.
async function saveLayout() {
  if (!_tables.length) {
    mesioToast('No hay mesas para guardar', 'info');
    return;
  }

  var btn = el('btnSaveLayout');
  if (btn) btn.disabled = true;

  var payload = _tables.map(function (t) {
    var entry = { id: String(t.id) };
    if (t.position_x != null) entry.position_x = t.position_x;
    if (t.position_y != null) entry.position_y = t.position_y;
    if (t.capacity != null)   entry.capacity   = t.capacity;
    if (t.table_type)         entry.table_type = t.table_type;
    if (t.zone)               entry.zone       = t.zone;
    if (t.name)               entry.name       = t.name;
    return entry;
  });

  try {
    var headers = mesioHeaders();
    headers['Content-Type'] = 'application/json';
    var r = await fetch('/api/tables/floor-plan', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({ tables: payload })
    });
    mesioTrackFetch(r.ok);
    if (!r.ok) {
      var err = await r.json().catch(function () { return {}; });
      throw new Error(err.detail || 'HTTP ' + r.status);
    }
    var result = await r.json();
    if (result.errors && result.errors.length) {
      mesioToast(
        'Guardado parcial: ' + result.updated + ' actualizadas, ' +
          result.errors.length + ' con error',
        'warning'
      );
    } else {
      mesioToast('Layout guardado (' + result.updated + ' mesas)', 'success');
    }
  } catch (e) {
    mesioToast('No se pudo guardar: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Phase 3: Select table → detail panel ─────────────────────────
async function selectTable(tableId) {
  _selectedTableId = tableId;
  renderTables();

  var tbl = _tables.find(function (t) { return String(t.id) === String(tableId); });
  if (!tbl) return;

  el('detailPanel').classList.remove('hidden');
  el('detailEmpty').classList.add('hidden');

  // Header number + state
  var status = (tbl.status || 'free').toLowerCase();
  var numEl = el('dNum');
  if (numEl) {
    numEl.textContent = 'M' + (tbl.table_number || tbl.id);
    var stateColors = { seated: 'fp-sentados-text', eating: 'fp-ocupada-text', billing: 'fp-factura-text', reserved: 'fp-reservada-text' };
    numEl.style.color = 'var(--' + (stateColors[status] || 'fp-libre-text') + ')';
  }
  var stateEl = el('dState');
  if (stateEl) stateEl.textContent = STATUS_LABEL[status] || status;

  // Meta
  var metaEl = el('dMeta');
  if (metaEl) {
    var parts = [];
    if (tbl.customer_name) parts.push('<strong>' + _escHtml(tbl.customer_name) + '</strong>');
    if (tbl.guests) parts.push(tbl.guests + ' personas');
    if (tbl.opened_at) {
      var mins = Math.round((Date.now() - new Date(tbl.opened_at).getTime()) / 60000);
      parts.push('Abierta hace <strong>' + mins + ' min</strong>');
    }
    // Mesera line — only render if API provides it
    var waiterName = tbl.assigned_staff_name || tbl.waiter_name;
    if (waiterName) parts.push('Mesero/a: <strong>' + _escHtml(waiterName) + '</strong>');
    // Last event — only render if API provides it
    if (tbl.last_event && tbl.last_event.at) {
      var evMins = Math.round((Date.now() - new Date(tbl.last_event.at).getTime()) / 60000);
      var evLabel = tbl.last_event.label || 'Último evento';
      parts.push(_escHtml(evLabel) + ' hace <strong>' + evMins + ' min</strong>');
    }
    metaEl.innerHTML = parts.length ? parts.join(' · ') : '<span style="color:var(--text-4)">Sin sesión activa</span>';
  }

  // Orders list
  var ordersEl = el('dOrders');
  if (ordersEl) {
    ordersEl.innerHTML = '';
    var orders = tbl.current_orders || [];
    if (orders.length) {
      orders.forEach(function (item) {
        var row = document.createElement('div');
        row.className = 'd-item';
        var left = document.createElement('div');
        var q = document.createElement('span');
        q.className = 'q';
        q.textContent = (item.quantity || 1) + '×';
        left.appendChild(q);
        left.appendChild(document.createTextNode(item.name || ''));
        var right = document.createElement('div');
        right.className = 'mono';
        right.textContent = mesioFmt(item.price * (item.quantity || 1));
        row.appendChild(left);
        row.appendChild(right);
        ordersEl.appendChild(row);
      });
    } else {
      var noItems = document.createElement('div');
      noItems.style.cssText = 'font-size:12px;color:var(--text-4);padding:8px 0;';
      noItems.textContent = 'Sin ítems aún';
      ordersEl.appendChild(noItems);
    }
  }

  // Total
  var totalRow = el('dTotalRow');
  var totalEl = el('dTotal');
  if (totalEl && totalRow) {
    if (tbl.current_total) {
      totalEl.textContent = mesioFmt(tbl.current_total);
      totalRow.style.display = '';
    } else {
      totalRow.style.display = 'none';
    }
  }

  // Timeline
  var timelineEl = el('dTimeline');
  if (timelineEl) {
    timelineEl.innerHTML = '';
    var events = tbl.timeline || [];
    if (events.length) {
      events.forEach(function (ev) {
        var row = document.createElement('div');
        row.className = 'timeline-row';
        var timeDiv = document.createElement('div');
        timeDiv.className = 'tl-time';
        timeDiv.textContent = ev.time || '';
        var dot = document.createElement('div');
        dot.className = 'tl-dot' + (ev.past ? ' past' : '');
        var textDiv = document.createElement('div');
        var strong = document.createElement('strong');
        strong.textContent = ev.title || '';
        textDiv.appendChild(strong);
        if (ev.subtitle) {
          var sub = document.createElement('div');
          sub.style.cssText = 'font-size:11px;color:var(--text-3);';
          sub.textContent = ev.subtitle;
          textDiv.appendChild(sub);
        }
        row.appendChild(timeDiv);
        row.appendChild(dot);
        row.appendChild(textDiv);
        timelineEl.appendChild(row);
      });
    } else {
      var noEv = document.createElement('div');
      noEv.style.cssText = 'font-size:12px;color:var(--text-4);padding:8px 0;';
      noEv.textContent = 'Sin eventos registrados';
      timelineEl.appendChild(noEv);
    }
  }

  // QR — render client-side via qrcodejs (iframe blocked by X-Frame-Options DENY)
  _renderDetailQr(tableId);
}

function _renderDetailQr(tableId) {
  var canvas = el('dQrCanvas');
  if (!canvas) return;
  canvas.innerHTML = '';
  if (typeof QRCode === 'undefined') {
    canvas.textContent = 'QR no disponible';
    return;
  }
  var url = window.location.origin + '/menu/' + encodeURIComponent(tableId);
  try {
    new QRCode(canvas, {
      text: url,
      width: 180,
      height: 180,
      colorDark: '#0D1412',
      colorLight: '#ffffff',
      correctLevel: QRCode.CorrectLevel.M
    });
  } catch (e) {
    console.warn('[floorplan] QR render failed', e); // lint-allow: diagnostic
    canvas.textContent = 'No se pudo generar el QR';
  }
}

// ── Close detail panel ────────────────────────────────────────────
function closePanel() {
  _selectedTableId = null;
  el('detailPanel').classList.add('hidden');
  el('detailEmpty').classList.remove('hidden');
  var qrCanvas = el('dQrCanvas');
  if (qrCanvas) qrCanvas.innerHTML = '';
  renderTables();
}

// ── Phase 3: Ver ticket ───────────────────────────────────────────
async function _showTicket(tableId) {
  var tbl = _tables.find(function (t) { return String(t.id) === String(tableId); });
  if (!tbl) return;

  // Try to get order_id from the table data, else fetch active orders
  var orderId = tbl.current_order_id;
  if (!orderId) {
    try {
      var r = await fetch('/api/table-orders?status=active', { headers: mesioHeaders() });
      mesioTrackFetch(r.ok);
      if (r.ok) {
        var data = await r.json();
        var orders = Array.isArray(data) ? data : (data.orders || []);
        var match = orders.find(function (o) {
          return String(o.table_id) === String(tableId);
        });
        if (match) orderId = match.id;
      }
    } catch (e) { /* ignore */ }
  }

  if (!orderId) {
    mesioToast('Sin orden activa para esta mesa', 'info');
    return;
  }

  try {
    var r2 = await fetch('/api/table-orders/' + orderId + '/ticket', { headers: mesioHeaders() });
    mesioTrackFetch(r2.ok);
    if (!r2.ok) throw new Error('HTTP ' + r2.status);
    var ticket = await r2.json();

    var body = el('ticketBody');
    if (!body) return;
    body.innerHTML = '';

    // Render items
    var items = ticket.items || ticket.orders || [];
    if (items.length) {
      var list = document.createElement('div');
      list.style.cssText = 'margin-bottom:12px;';
      items.forEach(function (item) {
        var row = document.createElement('div');
        row.className = 'd-item';
        var left = document.createElement('div');
        var q = document.createElement('span');
        q.className = 'q';
        q.textContent = (item.quantity || 1) + '×';
        left.appendChild(q);
        left.appendChild(document.createTextNode(item.name || item.dish_name || ''));
        var right = document.createElement('div');
        right.textContent = mesioFmt((item.unit_price || item.price || 0) * (item.quantity || 1));
        row.appendChild(left);
        row.appendChild(right);
        list.appendChild(row);
      });
      body.appendChild(list);
    }

    // Total line
    var totalDiv = document.createElement('div');
    totalDiv.style.cssText = 'display:flex;justify-content:space-between;font-weight:700;padding-top:10px;border-top:0.5px solid var(--border);font-size:15px;';
    var lbl = document.createElement('span');
    lbl.textContent = 'Total';
    var val = document.createElement('span');
    val.style.fontFamily = 'var(--font-display)';
    val.textContent = mesioFmt(ticket.total || 0);
    totalDiv.appendChild(lbl);
    totalDiv.appendChild(val);
    body.appendChild(totalDiv);

    el('ticketModal').classList.add('open');
  } catch (e) {
    mesioToast('Error cargando ticket: ' + e.message, 'error');
  }
}

// ── Phase 3: QR view ──────────────────────────────────────────────
function renderQrGrid() {
  var grid = el('qrGrid');
  if (!grid) return;
  grid.innerHTML = '';

  var filtered = _tables.filter(function (t) {
    if (_activeZone === 'all') return true;
    return (t.zone || 'Salón').trim().toLowerCase() === _activeZone;
  });

  filtered.forEach(function (tbl) {
    var card = document.createElement('div');
    card.className = 'qr-card';

    var name = document.createElement('div');
    name.className = 'qr-card-name';
    name.textContent = 'Mesa ' + (tbl.table_number || tbl.id);
    card.appendChild(name);

    var meta = document.createElement('div');
    meta.className = 'qr-card-meta';
    var metaParts = [];
    if (tbl.capacity) metaParts.push(tbl.capacity + ' personas');
    if (tbl.zone) metaParts.push(tbl.zone);
    meta.textContent = metaParts.join(' · ');
    card.appendChild(meta);

    // Status badge
    var status = (tbl.status || 'free').toLowerCase();
    var badgeCls = STATUS_CLASS[status] || 'libre';
    if (!badgeCls) badgeCls = 'libre';
    var badge = document.createElement('span');
    badge.className = 'qr-card-badge ' + badgeCls;
    badge.textContent = STATUS_LABEL[status] || 'Libre';
    card.appendChild(badge);

    // QR canvas container
    var qrWrap = document.createElement('div');
    qrWrap.className = 'qr-card-canvas';
    var qrDiv = document.createElement('div');
    qrWrap.appendChild(qrDiv);
    card.appendChild(qrWrap);

    var qrUrl = window.location.origin + '/menu/' + tbl.id;
    // Generate QR client-side via qrcodejs
    if (typeof QRCode !== 'undefined') {
      new QRCode(qrDiv, {
        text: qrUrl,
        width: 140,
        height: 140,
        correctLevel: QRCode.CorrectLevel.M
      });
    } else {
      // Fallback: link text
      var link = document.createElement('a');
      link.href = qrUrl;
      link.textContent = qrUrl;
      link.style.fontSize = '10px';
      link.style.wordBreak = 'break-all';
      qrDiv.appendChild(link);
    }

    // Print link
    var printLink = document.createElement('a');
    printLink.className = 'qr-card-print';
    printLink.href = '/api/tables/' + tbl.id + '/qr-sheet';
    printLink.target = '_blank';
    printLink.rel = 'noopener noreferrer';
    printLink.textContent = '🖨️ Imprimir QR';
    card.appendChild(printLink);

    grid.appendChild(card);
  });

  if (filtered.length === 0) {
    var empty = document.createElement('div');
    empty.style.cssText = 'grid-column:1/-1;text-align:center;color:var(--text-4);padding:60px 20px;font-size:14px;';
    empty.textContent = 'No hay mesas para mostrar';
    grid.appendChild(empty);
  }
}

// ── Phase 3: Reserva modal ────────────────────────────────────────
function _openReservaModal() {
  // Defaults: today, current time + 1h
  var now = new Date();
  var pad = function (n) { return String(n).padStart(2, '0'); };
  el('resDate').value = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
  now.setHours(now.getHours() + 1, 0, 0, 0);
  el('resTime').value = pad(now.getHours()) + ':' + pad(now.getMinutes());
  el('resGuests').value = '2';
  el('resName').value = '';
  el('resPhone').value = '';
  el('resNotes').value = '';

  // Pre-select current table if one is selected
  var sel = el('resTable');
  if (sel && _selectedTableId) sel.value = String(_selectedTableId);
  else if (sel) sel.value = '';

  el('reservaModal').classList.add('open');
}

function _populateReservaTables() {
  var sel = el('resTable');
  if (!sel) return;
  var current = sel.value;
  sel.innerHTML = '<option value="">— Sin asignar —</option>';
  _tables.forEach(function (t) {
    var opt = document.createElement('option');
    opt.value = t.id;
    var cap = t.capacity ? ' (' + t.capacity + 'p)' : '';
    opt.textContent = 'Mesa ' + (t.table_number || t.id) + (t.zone ? ' — ' + t.zone : '') + cap;
    sel.appendChild(opt);
  });
  if (current) sel.value = current;
}

async function _submitReserva() {
  var name = el('resName').value.trim();
  if (!name) { mesioToast('El nombre del cliente es obligatorio', 'error'); return; }
  var date = el('resDate').value;
  var time = el('resTime').value;
  if (!date || !time) { mesioToast('La fecha y hora son obligatorias', 'error'); return; }
  var guests = parseInt(el('resGuests').value);
  if (!guests || guests < 1) { mesioToast('El número de personas debe ser mayor a 0', 'error'); return; }

  // Validate future date
  var dt = new Date(date + 'T' + time);
  if (dt <= new Date()) { mesioToast('La reserva debe ser en el futuro', 'error'); return; }

  var body = {
    customer_name: name,
    customer_phone: el('resPhone').value.trim() || null,
    date: date,
    time: time,
    party_size: guests,
    notes: el('resNotes').value.trim() || null,
    source: 'admin'
  };
  var tableVal = el('resTable').value;
  if (tableVal) body.table_id = parseInt(tableVal);

  try {
    var r = await fetch('/api/reservations', {
      method: 'POST',
      headers: mesioHeaders(),
      body: JSON.stringify(body)
    });
    mesioTrackFetch(r.ok);
    if (!r.ok) {
      var err = await r.json().catch(function () { return {}; });
      throw new Error(err.detail || 'HTTP ' + r.status);
    }
    mesioToast('Reserva creada correctamente', 'success');
    el('reservaModal').classList.remove('open');
    await loadFloorPlan();
  } catch (e) {
    mesioToast('Error: ' + e.message, 'error');
  }
}

// ── Refresh badge ─────────────────────────────────────────────────
function updateRefreshBadge() {
  var now = new Date();
  var badge = el('refreshBadge');
  if (badge) {
    badge.textContent = 'Actualizado ' + now.toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
}

// ── Close panel ───────────────────────────────────────────────────
function _closeModal(id) {
  var m = el(id);
  if (m) m.classList.remove('open');
}

// ── View toggle ───────────────────────────────────────────────────
function _setView(view) {
  _currentView = view;
  var mapView  = el('mapView');
  var qrView   = el('qrView');
  var mapBtn   = el('viewMap');
  var qrBtn    = el('viewQr');
  var zoneBar  = el('fpZoneBar');

  if (view === 'map') {
    if (mapView) mapView.style.display = '';
    if (qrView)  qrView.style.display  = 'none';
    if (zoneBar) zoneBar.style.display = '';
    if (mapBtn)  { mapBtn.classList.add('active'); }
    if (qrBtn)   { qrBtn.classList.remove('active'); }
  } else {
    if (mapView) mapView.style.display = 'none';
    if (qrView)  qrView.style.display  = '';
    if (zoneBar) zoneBar.style.display = 'none';
    if (mapBtn)  { mapBtn.classList.remove('active'); }
    if (qrBtn)   { qrBtn.classList.add('active'); }
    renderQrGrid();
  }
}

// ── Keyboard ──────────────────────────────────────────────────────
function bindKeyboard() {
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      // Close any open modal first; else close panel
      var openModal = document.querySelector('.m-modal-overlay.open');
      if (openModal) { openModal.classList.remove('open'); return; }
      if (_editMode) { _exitEditMode(); return; }
      closePanel();
    }
  });
}

// ── Auto-refresh every 15s ────────────────────────────────────────
function startAutoRefresh() {
  clearInterval(_refreshTimer);
  _refreshTimer = mesioInterval(function () {
    loadFloorPlan();
  }, 15000);
}

// ── Wire all buttons ──────────────────────────────────────────────
function bindAll() {
  // Sync
  var syncBtn = el('syncBtn');
  if (syncBtn) syncBtn.addEventListener('click', function () { loadFloorPlan(); });

  // Edit layout toggle
  var editBtn = el('editLayoutBtn');
  if (editBtn) editBtn.addEventListener('click', function () {
    if (_editMode) _exitEditMode(); else _enterEditMode();
  });

  // Exit edit from toolbar
  var exitBtn = el('btnExitEdit');
  if (exitBtn) exitBtn.addEventListener('click', _exitEditMode);

  // Save layout (bulk persist)
  var saveBtn = el('btnSaveLayout');
  if (saveBtn) saveBtn.addEventListener('click', saveLayout);

  // New table (permanent topbar button)
  var topbarNewTblBtn = el('btnTopbarNewTable');
  if (topbarNewTblBtn) topbarNewTblBtn.addEventListener('click', _openNewTableModal);

  // New table (from toolbar in edit mode)
  var newTblBtn = el('btnNewTable');
  if (newTblBtn) newTblBtn.addEventListener('click', _openNewTableModal);

  // New table (from canvas empty state)
  var firstTblBtn = el('btnFirstTable');
  if (firstTblBtn) firstTblBtn.addEventListener('click', _openNewTableModal);

  // Reload button in empty/error state
  var reloadBtn = el('btnEmptyReload');
  if (reloadBtn) reloadBtn.addEventListener('click', function () { loadFloorPlan(); });

  // New reserva
  var resBtn = el('newReservaBtn');
  if (resBtn) resBtn.addEventListener('click', _openReservaModal);

  // View toggle
  var mapBtn = el('viewMap');
  var qrBtn  = el('viewQr');
  if (mapBtn) mapBtn.addEventListener('click', function () { _setView('map'); });
  if (qrBtn)  qrBtn.addEventListener('click',  function () { _setView('qr'); });

  // Close panel
  var closePanelBtn = el('btnClosePanel');
  if (closePanelBtn) closePanelBtn.addEventListener('click', closePanel);

  // Detail actions
  var btnTicket = el('btnTicket');
  if (btnTicket) btnTicket.addEventListener('click', function () {
    if (_selectedTableId) _showTicket(_selectedTableId);
  });

  var btnInvoice = el('btnInvoice');
  if (btnInvoice) btnInvoice.addEventListener('click', function () {
    if (_selectedTableId) window.location.href = '/caja?table=' + _selectedTableId;
  });

  var btnAddItem = el('btnAddItem');
  if (btnAddItem) btnAddItem.addEventListener('click', function () {
    if (_selectedTableId) window.location.href = '/caja?table=' + _selectedTableId + '&action=add';
  });

  // Editar mesa (capacidad, tipo, zona) — abre el modal de propiedades
  var btnEditTable = el('btnEditTable');
  if (btnEditTable) btnEditTable.addEventListener('click', function () {
    if (_selectedTableId) _openPropsModal(_selectedTableId);
  });

  // Print QR from detail panel
  var btnPrintQr = el('btnPrintQr');
  if (btnPrintQr) btnPrintQr.addEventListener('click', function () {
    if (_selectedTableId) window.open('/api/tables/' + _selectedTableId + '/qr-sheet', '_blank');
  });

  // Reserva modal
  var resCancel = el('reservaModalCancel');
  var resClose  = el('reservaModalClose');
  var resSubmit = el('reservaModalSubmit');
  if (resCancel) resCancel.addEventListener('click', function () { _closeModal('reservaModal'); });
  if (resClose)  resClose.addEventListener('click',  function () { _closeModal('reservaModal'); });
  if (resSubmit) resSubmit.addEventListener('click', _submitReserva);

  // Props modal
  var propsCancel = el('propsModalCancel');
  var propsClose  = el('propsModalClose');
  var propsSubmit = el('propsModalSubmit');
  var deleteBtn   = el('btnDeleteTable');
  if (propsCancel) propsCancel.addEventListener('click', function () { _closeModal('propsModal'); });
  if (propsClose)  propsClose.addEventListener('click',  function () { _closeModal('propsModal'); });
  if (propsSubmit) propsSubmit.addEventListener('click', _saveProps);
  if (deleteBtn)   deleteBtn.addEventListener('click', _deleteTable);

  // New table modal
  var ntCancel = el('newTableModalCancel');
  var ntClose  = el('newTableModalClose');
  var ntSubmit = el('newTableModalSubmit');
  if (ntCancel) ntCancel.addEventListener('click', function () { _closeModal('newTableModal'); });
  if (ntClose)  ntClose.addEventListener('click',  function () { _closeModal('newTableModal'); });
  if (ntSubmit) ntSubmit.addEventListener('click', _createTable);

  // Ticket modal
  var ticketClose = el('ticketModalClose');
  if (ticketClose) ticketClose.addEventListener('click', function () { _closeModal('ticketModal'); });

  // Close modals on overlay click
  document.querySelectorAll('.m-modal-overlay').forEach(function (overlay) {
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) overlay.classList.remove('open');
    });
  });
}

// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  bindKeyboard();
  bindAll();
  loadFloorPlan();
  startAutoRefresh();
});
