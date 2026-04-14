/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Menu Engineering (Fase 5b)
   app/static/js/dashboard-analytics.js
═══════════════════════════════════════════════════ */

// ── State ─────────────────────────────────────────
let _meState = {
  loading: false,
  days: 30,
  data: null,
  error: null,
  sortCol: 'orders',
  sortDir: 'desc',
};

// ── Fetch ─────────────────────────────────────────
async function _meFetch() {
  _meState.loading = true;
  _meState.error = null;
  _meRender();
  try {
    const h = mesioHeaders();
    const resp = await fetch(`/api/menu/analytics?days=${_meState.days}`, { headers: h });
    mesioTrackFetch(resp.ok);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    _meState.data = await resp.json();
  } catch (err) {
    _meState.error = err.message || 'Error al cargar datos';
    mesioTrackFetch(false);
  } finally {
    _meState.loading = false;
    _meRender();
  }
}

// ── Load (called from showSection) ────────────────
function loadMenuEngineeringSection() {
  _meFetch();
}

// ── Helpers ───────────────────────────────────────
function _totalEvents(data) {
  if (!data || !Array.isArray(data.per_dish)) return 0;
  return data.per_dish.reduce((s, d) => s + (d.views || 0) + (d.orders || 0), 0);
}

function _pct(val) {
  if (val == null) return '—';
  return `${(val * 100).toFixed(1)}%`;
}

// ── Sort ──────────────────────────────────────────
function _meSortBy(col) {
  if (_meState.sortCol === col) {
    _meState.sortDir = _meState.sortDir === 'desc' ? 'asc' : 'desc';
  } else {
    _meState.sortCol = col;
    _meState.sortDir = 'desc';
  }
  _meRender();
}

function _meSortedDishes() {
  const dishes = (_meState.data && Array.isArray(_meState.data.per_dish))
    ? [..._meState.data.per_dish] : [];
  const { sortCol, sortDir } = _meState;
  dishes.sort((a, b) => {
    const va = a[sortCol] ?? 0;
    const vb = b[sortCol] ?? 0;
    return sortDir === 'desc' ? vb - va : va - vb;
  });
  return dishes;
}

// ── Quadrant card builder (DOM, no innerHTML) ─────
function _buildQuadrantCard(quadrant) {
  const configs = {
    stars:      { label: 'Stars',      icon: '⭐', desc: 'Alta popularidad + alta conversión. Promover.',     color: 'var(--brand)',   bg: 'var(--brand-light)' },
    puzzles:    { label: 'Puzzles',    icon: '🧩', desc: 'Baja popularidad + alta conversión. Dar visibilidad.', color: 'var(--warning)', bg: 'var(--warning-light)' },
    plowhorses: { label: 'Plowhorses', icon: '🐴', desc: 'Alta popularidad + baja conversión. Optimizar precio.', color: '#378ADD',        bg: '#EBF4FF' },
    dogs:       { label: 'Dogs',       icon: '🐕', desc: 'Baja popularidad + baja conversión. Evaluar eliminar.', color: 'var(--danger)',   bg: 'var(--danger-light)' },
  };
  const cfg = configs[quadrant];
  const dishes = (_meState.data && _meState.data.matrix && Array.isArray(_meState.data.matrix[quadrant]))
    ? _meState.data.matrix[quadrant] : [];

  const card = document.createElement('div');
  card.style.cssText = `border:1.5px solid ${cfg.color};border-radius:12px;padding:1rem;background:${cfg.bg};`;

  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:4px;';

  const iconEl = document.createElement('span');
  iconEl.style.cssText = 'font-size:1.3rem;line-height:1;';
  iconEl.textContent = cfg.icon;

  const labelEl = document.createElement('span');
  labelEl.style.cssText = `font-size:14px;font-weight:700;color:${cfg.color};`;
  labelEl.textContent = cfg.label;

  const countBadge = document.createElement('span');
  countBadge.style.cssText = `margin-left:auto;background:${cfg.color};color:#fff;border-radius:999px;padding:1px 9px;font-size:11px;font-weight:600;`;
  countBadge.textContent = String(dishes.length);

  header.appendChild(iconEl);
  header.appendChild(labelEl);
  header.appendChild(countBadge);

  const descEl = document.createElement('p');
  descEl.style.cssText = 'font-size:11px;color:#666;margin:0 0 10px;line-height:1.4;';
  descEl.textContent = cfg.desc;

  card.appendChild(header);
  card.appendChild(descEl);

  if (dishes.length === 0) {
    const empty = document.createElement('div');
    empty.style.cssText = 'font-size:12px;color:#aaa;text-align:center;padding:8px 0;';
    empty.textContent = 'Sin platos en este cuadrante';
    card.appendChild(empty);
    return card;
  }

  const list = document.createElement('div');
  list.style.cssText = 'display:flex;flex-direction:column;gap:4px;max-height:200px;overflow-y:auto;';

  dishes.forEach(dish => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:4px 6px;border-radius:6px;background:rgba(255,255,255,0.6);font-size:12px;';

    const nameEl = document.createElement('span');
    nameEl.style.cssText = 'font-weight:500;color:#222;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px;';
    nameEl.textContent = dish.dish_name || '—';   // textContent — XSS safe

    const stats = document.createElement('span');
    stats.style.cssText = 'color:#666;flex-shrink:0;margin-left:8px;';
    stats.textContent = `${dish.views ?? 0} vistas · ${dish.orders ?? 0} pedidos`;

    row.appendChild(nameEl);
    row.appendChild(stats);
    list.appendChild(row);
  });

  card.appendChild(list);
  return card;
}

// ── Table row builder (DOM) ───────────────────────
function _buildTableRow(dish, even) {
  const tr = document.createElement('tr');
  tr.style.background = even ? 'var(--surface, #fff)' : '#fafaf8';

  const tdStyle = 'padding:8px 12px;font-size:13px;border-bottom:1px solid #f0ece8;';

  const cells = [
    { val: dish.dish_name || '—',                        isText: true,  align: 'left' },
    { val: dish.views ?? 0,                              isText: false, align: 'right' },
    { val: dish.modal_opens ?? 0,                        isText: false, align: 'right' },
    { val: dish.add_to_carts ?? 0,                       isText: false, align: 'right' },
    { val: dish.orders ?? 0,                             isText: false, align: 'right' },
    { val: _pct(dish.cart_conversion_rate),              isText: true,  align: 'right' },
    { val: _pct(dish.order_conversion_rate),             isText: true,  align: 'right' },
  ];

  cells.forEach(({ val, isText, align }) => {
    const td = document.createElement('td');
    td.style.cssText = `${tdStyle}text-align:${align};`;
    if (isText) {
      td.textContent = String(val);   // XSS safe
    } else {
      td.textContent = String(val);
    }
    tr.appendChild(td);
  });

  return tr;
}

// ── Main render ───────────────────────────────────
function _meRender() {
  const container = document.getElementById('menu-engineering-root');
  if (!container) return;

  container.textContent = '';  // safe clear

  // Days selector
  const controls = document.createElement('div');
  controls.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:1.25rem;flex-wrap:wrap;';

  const label = document.createElement('span');
  label.style.cssText = 'font-size:13px;color:#666;font-weight:500;';
  label.textContent = 'Período:';
  controls.appendChild(label);

  [7, 30, 90].forEach(d => {
    const btn = document.createElement('button');
    btn.style.cssText = `padding:6px 14px;border-radius:8px;border:1.5px solid ${_meState.days === d ? 'var(--brand)' : '#e0e0d8'};background:${_meState.days === d ? 'var(--brand-light)' : '#fff'};color:${_meState.days === d ? 'var(--brand-dark)' : '#555'};font-size:13px;font-weight:${_meState.days === d ? '600' : '400'};cursor:pointer;transition:all 0.15s;`;
    btn.textContent = `${d} días`;
    btn.addEventListener('click', () => {
      _meState.days = d;
      _meFetch();
    });
    controls.appendChild(btn);
  });

  container.appendChild(controls);

  // Loading state
  if (_meState.loading) {
    const loadingEl = document.createElement('div');
    loadingEl.style.cssText = 'text-align:center;padding:3rem;color:#aaa;font-size:14px;';
    loadingEl.textContent = 'Cargando datos de Menu Engineering...';
    container.appendChild(loadingEl);
    return;
  }

  // Error state
  if (_meState.error) {
    const errEl = document.createElement('div');
    errEl.style.cssText = 'background:var(--danger-light);border:1.5px solid var(--danger);border-radius:10px;padding:1rem 1.25rem;color:#991B1B;font-size:13px;margin-bottom:1rem;';
    errEl.textContent = `Error al cargar datos: ${_meState.error}`;
    container.appendChild(errEl);
    return;
  }

  // Empty state — less than 10 total events
  if (!_meState.data || _totalEvents(_meState.data) < 10) {
    const emptyWrap = document.createElement('div');
    emptyWrap.style.cssText = 'text-align:center;padding:3rem 2rem;';

    const emptyIcon = document.createElement('div');
    emptyIcon.style.cssText = 'font-size:3rem;margin-bottom:1rem;';
    emptyIcon.textContent = '📊';

    const emptyTitle = document.createElement('div');
    emptyTitle.style.cssText = 'font-size:16px;font-weight:600;color:#333;margin-bottom:8px;';
    emptyTitle.textContent = 'Aún no hay suficientes datos';

    const emptyDesc = document.createElement('div');
    emptyDesc.style.cssText = 'font-size:13px;color:#888;max-width:380px;margin:0 auto;line-height:1.6;';
    emptyDesc.textContent = 'Necesitamos al menos 10 interacciones con el catálogo para mostrar el análisis de Menu Engineering. Los datos aparecerán automáticamente a medida que los clientes interactúen con el menú.';

    emptyWrap.appendChild(emptyIcon);
    emptyWrap.appendChild(emptyTitle);
    emptyWrap.appendChild(emptyDesc);
    container.appendChild(emptyWrap);
    return;
  }

  // ── Quadrants section ───────────────────────────
  const quadTitle = document.createElement('div');
  quadTitle.style.cssText = 'font-size:15px;font-weight:700;color:#222;margin-bottom:10px;';
  quadTitle.textContent = 'Clasificación por Cuadrante';
  container.appendChild(quadTitle);

  const quadDesc = document.createElement('p');
  quadDesc.style.cssText = 'font-size:12px;color:#888;margin:0 0 14px;';
  quadDesc.textContent = `Análisis de los últimos ${_meState.days} días. Basado en popularidad (vistas) y conversión (pedidos/vistas).`;
  container.appendChild(quadDesc);

  const quadGrid = document.createElement('div');
  quadGrid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-bottom:2rem;';

  ['stars', 'puzzles', 'plowhorses', 'dogs'].forEach(q => {
    quadGrid.appendChild(_buildQuadrantCard(q));
  });

  container.appendChild(quadGrid);

  // ── Per-dish table ──────────────────────────────
  const tableWrap = document.createElement('div');
  tableWrap.className = 'card';
  tableWrap.style.marginBottom = '0';

  const tableHeader = document.createElement('div');
  tableHeader.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px;';

  const tableTitle = document.createElement('div');
  tableTitle.style.cssText = 'font-size:14px;font-weight:700;color:#222;';
  tableTitle.textContent = 'Detalle por Plato';

  const tableCount = document.createElement('span');
  tableCount.style.cssText = 'font-size:12px;color:#888;';
  const n = (_meState.data.per_dish || []).length;
  tableCount.textContent = `${n} plato${n !== 1 ? 's' : ''}`;

  tableHeader.appendChild(tableTitle);
  tableHeader.appendChild(tableCount);
  tableWrap.appendChild(tableHeader);

  const scrollWrap = document.createElement('div');
  scrollWrap.style.cssText = 'overflow-x:auto;';

  const table = document.createElement('table');
  table.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;';

  // Table head
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  headRow.style.cssText = 'background:#f7f5f2;';

  const columns = [
    { key: 'dish_name',             label: 'Plato',          sortable: false, align: 'left' },
    { key: 'views',                 label: 'Vistas',         sortable: true,  align: 'right' },
    { key: 'modal_opens',           label: 'Detalle',        sortable: true,  align: 'right' },
    { key: 'add_to_carts',          label: 'Al carrito',     sortable: true,  align: 'right' },
    { key: 'orders',                label: 'Pedidos',        sortable: true,  align: 'right' },
    { key: 'cart_conversion_rate',  label: 'Conv. carrito',  sortable: true,  align: 'right' },
    { key: 'order_conversion_rate', label: 'Conv. pedido',   sortable: true,  align: 'right' },
  ];

  columns.forEach(col => {
    const th = document.createElement('th');
    th.style.cssText = `padding:8px 12px;font-size:11px;font-weight:600;color:#666;text-align:${col.align};white-space:nowrap;border-bottom:2px solid #e0e0d8;user-select:none;`;

    if (col.sortable) {
      th.style.cursor = 'pointer';
      th.title = 'Ordenar por esta columna';
      th.addEventListener('click', () => _meSortBy(col.key));

      const thInner = document.createElement('span');
      thInner.style.cssText = 'display:inline-flex;align-items:center;gap:3px;';

      const thLabel = document.createElement('span');
      thLabel.textContent = col.label;

      const sortIndicator = document.createElement('span');
      sortIndicator.style.cssText = `font-size:10px;color:${_meState.sortCol === col.key ? 'var(--brand)' : '#ccc'};`;
      sortIndicator.textContent = _meState.sortCol === col.key
        ? (_meState.sortDir === 'desc' ? '▼' : '▲') : '⇅';

      thInner.appendChild(thLabel);
      thInner.appendChild(sortIndicator);
      th.appendChild(thInner);
    } else {
      th.textContent = col.label;
    }

    headRow.appendChild(th);
  });

  thead.appendChild(headRow);
  table.appendChild(thead);

  // Table body
  const tbody = document.createElement('tbody');
  const sorted = _meSortedDishes();

  if (sorted.length === 0) {
    const emptyTr = document.createElement('tr');
    const emptyTd = document.createElement('td');
    emptyTd.colSpan = 7;
    emptyTd.style.cssText = 'text-align:center;padding:2rem;color:#aaa;font-size:13px;';
    emptyTd.textContent = 'Sin datos disponibles para este período.';
    emptyTr.appendChild(emptyTd);
    tbody.appendChild(emptyTr);
  } else {
    sorted.forEach((dish, i) => {
      tbody.appendChild(_buildTableRow(dish, i % 2 === 0));
    });
  }

  table.appendChild(tbody);
  scrollWrap.appendChild(table);
  tableWrap.appendChild(scrollWrap);
  container.appendChild(tableWrap);
}
