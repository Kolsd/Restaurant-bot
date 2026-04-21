/* ══ Floor plan page — read-only live view ═══════════════════════════
   Fetches /api/floor-plan on load and auto-refreshes every 15s.
   Click a table tile → show detail panel.
   Keyboard: Esc closes panel.
   Edit mode: deferred to v2 — requires PATCH /api/floor-plan (tracked separately).  // lint-allow: v2 feature note, not stale wiring
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

// ── Auth guard ────────────────────────────────────────────────────
(function () {
  var token = localStorage.getItem('rb_token');
  if (!token) { window.location.href = '/login'; }
})();

// ── State ─────────────────────────────────────────────────────────
var _tables = [];
var _selectedTableId = null;
var _refreshTimer = null;
var _activeZone = 'all';
var _lastRefresh = null;

// ── DOM refs ──────────────────────────────────────────────────────
function el(id) { return document.getElementById(id); }

// ── Status → CSS class mapping ────────────────────────────────────
var STATUS_CLASS = {
  free:       '',
  available:  '',
  seated:     'sent',
  eating:     'occ',
  billing:    'fac',
  reserved:   'res'
};

var STATUS_LABEL = {
  free:       'Libre',
  available:  'Libre',
  seated:     'Sentados',
  eating:     'Comiendo',
  billing:    'Facturando',
  reserved:   'Reservada',
  occupied:   'Ocupada'
};

// ── Fetch floor plan ──────────────────────────────────────────────
async function loadFloorPlan() {
  try {
    var res = await fetch('/api/tables/floor-plan', { headers: mesioHeaders() });
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    _tables = Array.isArray(data) ? data : (data.tables || []);
    renderTables();
    updateRefreshBadge();
    mesioTrackFetch(true);
  } catch (e) {
    mesioTrackFetch(false);
    console.warn('floor-plan fetch failed:', e);
  }
}

// ── Render table tiles ────────────────────────────────────────────
function renderTables() {
  var canvas = el('fpCanvas');
  if (!canvas) return;

  // Remove existing dynamic tiles (keep static structure: zones, walls, legend)
  canvas.querySelectorAll('.t').forEach(function (t) { t.remove(); });

  var zoneCounts = {};
  _tables.forEach(function (tbl) {
    var zone = (tbl.zone || 'Salón').toLowerCase();
    if (_activeZone !== 'all' && zone !== _activeZone) return;

    var tile = document.createElement('div');
    tile.className = 't';
    var status = (tbl.status || 'free').toLowerCase();
    var cls = STATUS_CLASS[status] || 'occ';
    if (cls) tile.classList.add(cls);
    if (String(tbl.id) === String(_selectedTableId)) tile.classList.add('sel');

    // Position
    var x = tbl.position_x != null ? tbl.position_x : (20 + Math.random() * 60);
    var y = tbl.position_y != null ? tbl.position_y : (80 + Math.random() * 300);
    var w = tbl.capacity ? Math.max(80, 60 + tbl.capacity * 8) : 92;
    var h = w;
    tile.style.cssText = 'left:' + x + 'px;top:' + y + 'px;width:' + w + 'px;height:' + h + 'px;';

    // Content
    var numDiv = document.createElement('div');
    numDiv.className = 'tn';
    numDiv.textContent = tbl.table_number || tbl.id;
    tile.appendChild(numDiv);

    var stDiv = document.createElement('div');
    stDiv.className = 'st';
    stDiv.textContent = (STATUS_LABEL[status] || status) + (tbl.capacity ? ' · ' + tbl.capacity + 'p' : '');
    tile.appendChild(stDiv);

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
    tile.addEventListener('click', function () { selectTable(tbl.id); });

    canvas.appendChild(tile);
    zoneCounts[zone] = (zoneCounts[zone] || 0) + 1;
  });

  // Update sidebar table count
  var countEl = el('tableCount');
  if (countEl) { countEl.textContent = _tables.length + ' mesas'; }
}

// ── Select table → detail panel ───────────────────────────────────
async function selectTable(tableId) {
  _selectedTableId = tableId;
  // Re-render to update .sel class
  renderTables();

  var tbl = _tables.find(function (t) { return String(t.id) === String(tableId); });
  if (!tbl) return;

  var panel = el('detailPanel');
  var empty = el('detailEmpty');
  if (panel) panel.classList.remove('hidden');
  if (empty) empty.classList.add('hidden');

  // Header
  var numEl = el('dNum');
  var stateEl = el('dState');
  if (numEl) { numEl.textContent = 'M' + (tbl.table_number || tbl.id); }
  if (stateEl) {
    var status = (tbl.status || 'free').toLowerCase();
    stateEl.textContent = STATUS_LABEL[status] || status;
    stateEl.className = 'd-state';
    var stateColor = {
      seated: 'fp-sentados-text', eating: 'fp-ocupada-text',
      billing: 'fp-factura-text', reserved: 'fp-reservada-text'
    };
    if (numEl) { numEl.style.color = 'var(--' + (stateColor[status] || 'fp-libre-text') + ')'; }
  }

  // Meta
  var metaEl = el('dMeta');
  if (metaEl) {
    var meta = '';
    if (tbl.customer_name) { meta += '<strong>' + _escHtml(tbl.customer_name) + '</strong>'; }
    if (tbl.guests) { meta += (tbl.customer_name ? ' · ' : '') + tbl.guests + ' personas'; }
    if (tbl.opened_at) {
      var mins = Math.round((Date.now() - new Date(tbl.opened_at).getTime()) / 60000);
      meta += '<br>Abierta hace <strong>' + mins + ' min</strong>';
    }
    metaEl.innerHTML = meta || '<span style="color:var(--text-4)">Sin sesión activa</span>';
  }

  // Orders list
  var ordersList = el('dOrders');
  if (ordersList) {
    ordersList.innerHTML = '';
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
        ordersList.appendChild(row);
      });
    } else {
      var empty2 = document.createElement('div');
      empty2.style.cssText = 'font-size:12px;color:var(--text-4);padding:8px 0;';
      empty2.textContent = 'Sin ítems aún';
      ordersList.appendChild(empty2);
    }
  }

  // Total
  var totalEl = el('dTotal');
  if (totalEl && tbl.current_total) {
    totalEl.textContent = mesioFmt(tbl.current_total);
    totalEl.closest('.d-total').style.display = '';
  } else if (totalEl) {
    totalEl.closest('.d-total').style.display = 'none';
  }

  // Timeline
  var timelineEl = el('dTimeline');
  if (timelineEl) {
    timelineEl.innerHTML = '';
    var events = tbl.timeline || [];
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
        sub.className = 'text-mute';
        sub.style.fontSize = '11px';
        sub.textContent = ev.subtitle;
        textDiv.appendChild(sub);
      }
      row.appendChild(timeDiv);
      row.appendChild(dot);
      row.appendChild(textDiv);
      timelineEl.appendChild(row);
    });
    if (!events.length) {
      var noEv = document.createElement('div');
      noEv.style.cssText = 'font-size:12px;color:var(--text-4);padding:8px 0;';
      noEv.textContent = 'Sin eventos registrados';
      timelineEl.appendChild(noEv);
    }
  }
}

// ── Close panel ───────────────────────────────────────────────────
function closePanel() {
  _selectedTableId = null;
  var panel = el('detailPanel');
  var empty = el('detailEmpty');
  if (panel) panel.classList.add('hidden');
  if (empty) empty.classList.remove('hidden');
  renderTables();
}

// ── Zone filter ───────────────────────────────────────────────────
function bindZoneFilter() {
  var btns = document.querySelectorAll('.seg-btn[data-zone]');
  btns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      btns.forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      _activeZone = btn.dataset.zone || 'all';
      renderTables();
    });
  });
}

// ── Refresh badge ─────────────────────────────────────────────────
function updateRefreshBadge() {
  _lastRefresh = new Date();
  var badgeEl = el('refreshBadge');
  if (badgeEl) { badgeEl.textContent = 'Actualizado ' + _lastRefresh.toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
}

// ── Sync button ───────────────────────────────────────────────────
function bindSync() {
  var syncBtn = el('syncBtn');
  if (syncBtn) {
    syncBtn.addEventListener('click', function () { loadFloorPlan(); });
  }
}

// ── Keyboard ──────────────────────────────────────────────────────
function bindKeyboard() {
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closePanel(); }
  });
}

// ── Detail action buttons (stubs) ─────────────────────────────────
function bindDetailActions() {
  var btnTicket = el('btnTicket');
  var btnInvoice = el('btnInvoice');
  var btnAddItem = el('btnAddItem');
  var btnMove = el('btnMove');
  var btnClosePanel = el('btnClosePanel');

  if (btnTicket) { btnTicket.addEventListener('click', function () {
    mesioToast('Ver ticket — navega a /caja', 'info');
  }); }
  if (btnInvoice) { btnInvoice.addEventListener('click', function () {
    mesioToast('Facturar — navega a /caja', 'info');
  }); }
  if (btnAddItem) { btnAddItem.addEventListener('click', function () {
    mesioToast('Añadir ítem — usa el POS en /caja', 'info');
  }); }
  if (btnMove) { btnMove.addEventListener('click', function () {
    mesioToast('Mover mesa — disponible en modo edición (próximamente)', 'warn');
  }); }
  if (btnClosePanel) { btnClosePanel.addEventListener('click', closePanel); }
}

// ── New reserva button (stub) ─────────────────────────────────────
function bindNewReserva() {
  var btn = el('newReservaBtn');
  if (btn) { btn.addEventListener('click', function () {
    mesioToast('Gestión de reservas — navega a /dashboard#reservations', 'info');
  }); }
}

// ── Logout ────────────────────────────────────────────────────────
function bindLogout() {
  var btn = el('logoutBtn');
  if (btn) { btn.addEventListener('click', mesioLogout); }
}

// ── Auto-refresh every 15s ────────────────────────────────────────
function startAutoRefresh() {
  clearInterval(_refreshTimer);
  _refreshTimer = setInterval(function () {
    loadFloorPlan();
    if (_selectedTableId) { selectTable(_selectedTableId); }
  }, 15000);
}

// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  bindZoneFilter();
  bindSync();
  bindKeyboard();
  bindDetailActions();
  bindNewReserva();
  bindLogout();
  loadFloorPlan();
  startAutoRefresh();
});
