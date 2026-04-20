/* ═══════════════════════════════════════════════════════════════════════════
   Mesio Dashboard — Page loader  (app/static/js/pages/dashboard.js)
   Wires the 7 new /api/stats/* endpoints to the new design HTML.
   Depends on mesio-utils.js (mesioHeaders, mesioFmt, mesioToast,
                               mesioInterval, mesioTrackFetch, mesioLogout).
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

// ── Period state ──────────────────────────────────────────────────────────────
let _period = 'week';           // currently active period key
let _periodStart = '';          // ISO date string (filled from backend response)
let _periodEnd   = '';

// ── Auth guard ────────────────────────────────────────────────────────────────
(function authGuard() {
  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  const rawRole = (localStorage.getItem('rb_role') || '').toLowerCase();
  const roles   = rawRole.split(',').map(r => r.trim()).filter(Boolean);
  const isAdmin = roles.some(r => ['owner', 'admin', 'gerente'].includes(r));
  if (!isAdmin) {
    if (roles.includes('mesero'))    { location.href = '/mesero';   return; }
    if (roles.includes('cocina'))    { location.href = '/cocina';   return; }
    if (roles.includes('bar'))       { location.href = '/bar';      return; }
    if (roles.includes('caja'))      { location.href = '/caja';     return; }
    location.href = '/staff-hq';
  }
})();

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  _setupChrome();
  _setupPeriodControl();
  _setupLogout();
  _setupOfflineBanner();

  await _loadAll();

  // Refresh every 30 s (visibility-aware via mesioInterval)
  mesioInterval(_loadAll, 30_000);
});

// ── Chrome (sidebar user info, org name) ─────────────────────────────────────
function _setupChrome() {
  // Org / restaurant info
  const org  = mesioGetOrg();
  const rest = org || _legacyRestaurant();

  const orgName = rest?.name || 'Mi Restaurante';
  const orgEl   = document.getElementById('sb-org-name');
  const subEl   = document.getElementById('sb-org-sub');
  const avaEl   = document.getElementById('sb-org-avatar');
  if (orgEl) orgEl.textContent = orgName;
  if (subEl) subEl.textContent = 'Panel de control';
  if (avaEl) {
    const initials = (orgName.split(' ').map(w => w[0]).join('').slice(0, 2) || 'M').toUpperCase();
    avaEl.textContent = initials;
    // Generate a stable hue from the org name for a colourful avatar
    let hash = 0;
    for (let i = 0; i < orgName.length; i++) hash = orgName.charCodeAt(i) + ((hash << 5) - hash);
    const hue = Math.abs(hash) % 360;
    avaEl.style.background = `hsl(${hue},45%,85%)`;
    avaEl.style.color       = `hsl(${hue},45%,35%)`;
  }

  // User info
  const role     = (localStorage.getItem('rb_role') || '').toLowerCase();
  const userName = localStorage.getItem('rb_user_name') || localStorage.getItem('rb_username') || 'Usuario';
  const roleLabel = { owner: 'Propietario', admin: 'Administrador', gerente: 'Gerente' }[role.split(',')[0].trim()] || 'Administrador';

  const userNameEl = document.getElementById('sb-user-name');
  const userRoleEl = document.getElementById('sb-user-role');
  const userAvaEl  = document.getElementById('sb-user-avatar');
  if (userNameEl) userNameEl.textContent = userName;
  if (userRoleEl) userRoleEl.textContent = roleLabel;
  if (userAvaEl) userAvaEl.textContent   = (userName.split(' ').map(w => w[0]).join('').slice(0, 2) || 'U').toUpperCase();

  // Greeting
  const hour = new Date().getHours();
  const greeting = hour < 12 ? 'Buenos días' : hour < 18 ? 'Buenas tardes' : 'Buenas noches';
  const greetEl  = document.getElementById('page-greeting');
  if (greetEl) greetEl.textContent = `${greeting}, ${userName.split(' ')[0]}`;

  // Date subtitle (filled dynamically after first load)
  const subPageEl = document.getElementById('page-sub');
  const now = new Date();
  const dayName = now.toLocaleDateString('es-CO', { weekday: 'long' });
  const datePart = now.toLocaleDateString('es-CO', { day: 'numeric', month: 'long' });
  if (subPageEl) {
    subPageEl.textContent = `Hoy es ${dayName} ${datePart}.`;
  }

  // Feature-gated nav items
  const features = _features();
  if (features.module_reservations === false)   _hideNav('nav-reservaciones');
  if (!features.module_nps)                     _hideNav('nav-nps');
  if (!features.loyalty)                        _hideNav('nav-loyalty');
  if (!features.staff_tips)                     _hideNav('nav-payroll');
  if (!(role.includes('owner')))                _hideNav('nav-equipo');
}

function _hideNav(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = 'none';
}

function _legacyRestaurant() {
  try { return JSON.parse(localStorage.getItem('rb_restaurant') || 'null'); } catch { return null; }
}

function _features() {
  try {
    const org = mesioGetOrg();
    const feats = org?.features || _legacyRestaurant()?.features || {};
    if (typeof feats === 'string') return JSON.parse(feats);
    return feats;
  } catch { return {}; }
}

// ── Period control ────────────────────────────────────────────────────────────
function _setupPeriodControl() {
  const ctrl = document.getElementById('period-control');
  if (!ctrl) return;
  ctrl.querySelectorAll('button[data-period]').forEach(btn => {
    btn.addEventListener('click', () => {
      ctrl.querySelectorAll('button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _period = btn.dataset.period;
      _loadAll();
    });
  });
}

// ── Logout ────────────────────────────────────────────────────────────────────
function _setupLogout() {
  const btn = document.getElementById('btn-logout');
  if (btn) btn.addEventListener('click', () => mesioLogout());
  const userEl = document.getElementById('sb-user');
  if (userEl) userEl.addEventListener('click', () => mesioLogout());
}

// ── Offline banner ────────────────────────────────────────────────────────────
function _setupOfflineBanner() {
  const banner = document.getElementById('offline-banner');
  if (!banner) return;
  window.addEventListener('offline', () => banner.classList.add('show'));
  window.addEventListener('online',  () => banner.classList.remove('show'));
}

// ── Helpers: date range from period key ───────────────────────────────────────
function _dateRange(period) {
  const today = new Date();
  const iso   = d => d.toISOString().slice(0, 10);
  const sub   = (d, n) => { const x = new Date(d); x.setDate(x.getDate() - n); return x; };
  switch (period) {
    case 'today':    return [iso(today), iso(today)];
    case 'week':     return [iso(sub(today, 6)), iso(today)];
    case 'month': {
      const m = new Date(today.getFullYear(), today.getMonth(), 1);
      return [iso(m), iso(today)];
    }
    case 'semester': {
      const s = today.getMonth() < 6
        ? new Date(today.getFullYear(), 0, 1)
        : new Date(today.getFullYear(), 6, 1);
      return [iso(s), iso(today)];
    }
    case 'year':
      return [iso(new Date(today.getFullYear(), 0, 1)), iso(today)];
    default:
      return [iso(sub(today, 6)), iso(today)];
  }
}

// ── Fetch helper ──────────────────────────────────────────────────────────────
async function _apiFetch(url) {
  const r = await fetch(url, { headers: mesioHeaders() });
  mesioTrackFetch(r.ok);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ── Master load ───────────────────────────────────────────────────────────────
async function _loadAll() {
  await Promise.allSettled([
    loadDailyInsight(),
    loadMetricsRow(),
    loadRevenueChart(),
    loadSalesByChannel(),
    loadLiveOrders(),
    loadPaymentStatus(),
    loadTopDishes(),
    loadInventoryCritical(),
    loadAtRiskBadge(),
  ]);
}

// ── 1. Daily insight banner ───────────────────────────────────────────────────
async function loadDailyInsight() {
  const banner = document.getElementById('ai-insight-banner');
  const textEl = document.getElementById('ai-insight-text');
  if (!banner || !textEl) return;
  try {
    const data = await _apiFetch('/api/stats/daily-insight');
    if (!data.enabled) { banner.style.display = 'none'; return; }
    banner.style.display = '';
    // Sanitise: render insight text safely
    // Use textContent only — user data may be embedded in the insight
    textEl.textContent = data.insight || data.text || '';
  } catch {
    banner.style.display = 'none';
  }
}

// ── 2. Metrics row (from /api/dashboard/sync) ─────────────────────────────────
async function loadMetricsRow() {
  const [ps, pe] = _dateRange(_period);
  try {
    const data = await _apiFetch(`/api/dashboard/sync?period=${_period}`);
    const stats = data.stats || {};
    const orders = stats.orders || {};
    const convs  = stats.conversations || {};

    // Revenue
    _setText('m-revenue', mesioFmt(orders.revenue || 0));
    const revDelta = document.getElementById('m-revenue-delta');
    if (revDelta && orders.revenue_delta_pct != null) {
      const up = orders.revenue_delta_pct >= 0;
      revDelta.textContent = `${up ? '▲' : '▼'} ${Math.abs(orders.revenue_delta_pct).toFixed(1)}% vs período anterior`;
      revDelta.className = `metric-sub metric-delta ${up ? 'up' : 'down'}`;
    }

    // Orders count
    _setText('m-orders', (orders.total || 0).toLocaleString('es-CO'));
    const ordDelta = document.getElementById('m-orders-delta');
    if (ordDelta && orders.count_delta_pct != null) {
      const up = orders.count_delta_pct >= 0;
      ordDelta.textContent = `${up ? '▲' : '▼'} ${Math.abs(orders.count_delta_pct).toFixed(1)}%`;
      ordDelta.className = `metric-sub metric-delta ${up ? 'up' : 'down'}`;
    }

    // Avg ticket
    const avgTicket = (orders.total && orders.revenue) ? orders.revenue / orders.total : 0;
    _setText('m-ticket', mesioFmt(avgTicket));

    // Chats
    _setText('m-chats', (convs.active || 0).toLocaleString('es-CO'));

    // Revenue sparkline from chart data
    if (data.chart?.revenue?.length) {
      _renderSparkline('m-revenue-spark', data.chart.revenue, false);
      _renderSparkline('m-orders-spark',  data.chart.orders,  false);
    }
  } catch (e) {
    console.warn('[dashboard] loadMetricsRow failed', e);
    ['m-revenue','m-orders','m-ticket','m-chats'].forEach(id => _setError(id));
  }
}

// ── 3. Revenue bar chart (/api/stats/by-channel?compare=true) ─────────────────
async function loadRevenueChart() {
  const [ps, pe] = _dateRange(_period);
  const container = document.getElementById('revenue-chart-bars');
  const labelsEl  = document.getElementById('revenue-chart-labels');
  if (!container || !labelsEl) return;
  try {
    const data = await _apiFetch(
      `/api/stats/by-channel?period_start=${ps}&period_end=${pe}&compare=true`
    );
    // Use chart data from /api/dashboard/sync for day-by-day bars
    const syncData = await _apiFetch(`/api/dashboard/sync?period=${_period}`);
    const chart    = syncData.chart || {};
    const labels   = chart.labels || [];
    const revenue  = chart.revenue || [];
    const prevData = data.previous || {};

    if (!revenue.length) {
      container.innerHTML = '<div style="padding:24px;text-align:center;font-size:12px;color:var(--text-3);">Sin datos para este período.</div>';
      labelsEl.textContent = '';
      return;
    }

    const maxRev = Math.max(...revenue, 1);
    // Estimate previous period per-day (distribute total evenly across days as approximation)
    const prevDayEst = (prevData.total || 0) / Math.max(revenue.length, 1);

    // Clear grid + rebuild bars
    container.innerHTML = '<div class="chart-grid"><div></div><div></div><div></div><div></div></div>';
    revenue.forEach((rev, i) => {
      const pct     = Math.round((rev / maxRev) * 100);
      const prevPct = Math.round((prevDayEst / maxRev) * 100);
      const pair = document.createElement('div');
      pair.style.cssText = 'flex:1;display:flex;gap:2px;align-items:flex-end;height:100%;';
      const prevBar = document.createElement('div');
      prevBar.className = 'chart-bar prev';
      prevBar.style.height = `${Math.max(prevPct, 2)}%`;
      const curBar = document.createElement('div');
      curBar.className = 'chart-bar';
      curBar.style.height = `${Math.max(pct, 2)}%`;
      // Tooltip via title (accessible, no JS needed)
      curBar.title  = mesioFmt(rev);
      prevBar.title = mesioFmt(prevDayEst);
      pair.appendChild(prevBar);
      pair.appendChild(curBar);
      container.appendChild(pair);
    });

    // X-axis labels
    labelsEl.innerHTML = '';
    labels.forEach(lbl => {
      const span = document.createElement('span');
      span.textContent = lbl;
      labelsEl.appendChild(span);
    });

    // Update subtitle
    const subEl = document.getElementById('chart-period-sub');
    if (subEl) {
      const periodNames = { today: 'Hoy', week: 'Últimos 7 días', month: 'Este mes', semester: 'Este semestre', year: 'Este año' };
      subEl.textContent = `${periodNames[_period] || '7 días'} · sombreado = período anterior`;
    }
  } catch (e) {
    console.warn('[dashboard] loadRevenueChart failed', e);
    if (container) container.setAttribute('data-error', 'true');
  }
}

// ── 4. Sales by channel (/api/stats/by-channel) ───────────────────────────────
const CHANNEL_META = {
  whatsapp_bot: {
    label: 'WhatsApp · Bot',
    bg: 'var(--brand-light,#e1f5ee)', color: 'var(--brand)',
    svg: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1.5c-3.6 0-6.5 2.9-6.5 6.5 0 1.2.3 2.3.9 3.3L1.5 14.5l3.4-.9c1 .5 2 .8 3.1.8 3.6 0 6.5-2.9 6.5-6.5S11.6 1.5 8 1.5zm3.5 9.1c-.1.4-.9.8-1.2.9-.3.1-.7.1-1.2-.1-.5-.2-1.2-.4-2-1.2-.8-.8-1.3-1.8-1.5-2.1-.1-.3-.4-1-.4-1.4 0-.4.2-.6.4-.9.1-.1.3-.2.4-.2h.3c.1 0 .2 0 .3.3.1.3.4.9.4 1 0 .1.1.2 0 .3-.1.1-.1.2-.2.3l-.2.2c-.1.1-.2.2-.1.4.1.3.6 1 1.2 1.5.8.6 1.5.8 1.8.9.3.1.4.1.5-.1.2-.2.5-.6.7-.8.2-.3.4-.2.6-.1.2.1 1.2.6 1.5.7.2.1.3.2.4.3.1 0 .1.5-.1.9z"/></svg>',
    barColor: 'var(--brand)',
  },
  pos: {
    label: 'Salón · POS',
    bg: '#FEF3C7', color: '#92400E',
    svg: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="9" rx="1"/><circle cx="5.5" cy="7.5" r="1"/><path d="M8 9l2-2 4 4"/></svg>',
    barColor: 'var(--warning,#f59e0b)',
  },
  delivery: {
    label: 'Domicilios',
    bg: '#DBEAFE', color: '#1E40AF',
    svg: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><path d="M3 12H1V6h7l3 3h3v3h-3M7 3h4"/></svg>',
    barColor: 'var(--info,#3b82f6)',
  },
  table_qr: {
    label: 'QR de mesa',
    bg: '#EDE9FE', color: '#5B21B6',
    svg: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="2" width="10" height="12" rx="1"/><path d="M6 5h4M6 8h4M6 11h2"/></svg>',
    barColor: 'var(--purple,#8b5cf6)',
  },
};

async function loadSalesByChannel() {
  const [ps, pe] = _dateRange(_period);
  const list = document.getElementById('channels-list');
  if (!list) return;
  try {
    const data = await _apiFetch(
      `/api/stats/by-channel?period_start=${ps}&period_end=${pe}`
    );
    const channels  = data.channels || [];
    const totalRev  = data.total || 0;

    if (!channels.length) {
      list.innerHTML = '<div style="padding:16px;text-align:center;font-size:12px;color:var(--text-3);">Sin datos de canales.</div>';
      return;
    }

    list.innerHTML = '';
    const maxRev = Math.max(...channels.map(c => c.revenue || 0), 1);

    channels.forEach(ch => {
      const meta    = CHANNEL_META[ch.channel] || {
        label: ch.channel_label || ch.channel,
        bg: '#f0f0e8', color: '#555',
        svg: '',
        barColor: 'var(--brand)',
      };
      const pct     = totalRev ? Math.round(ch.revenue / totalRev * 100) : 0;
      const barPct  = Math.round(ch.revenue / maxRev * 100);

      const item = document.createElement('div');
      item.className = 'chan';

      const ico = document.createElement('div');
      ico.className = 'chan-ico';
      ico.style.background = meta.bg;
      ico.style.color       = meta.color;
      ico.innerHTML         = meta.svg;   // trusted static SVG strings

      const body = document.createElement('div');
      body.className = 'chan-body';

      const row1 = document.createElement('div');
      row1.className = 'row';
      row1.style.cssText = 'justify-content:space-between;gap:4px;';

      const nameEl = document.createElement('div');
      nameEl.className   = 'chan-name';
      nameEl.textContent = meta.label;

      const valEl = document.createElement('div');
      valEl.className   = 'chan-value';
      valEl.textContent = mesioFmt(ch.revenue || 0);

      row1.appendChild(nameEl);
      row1.appendChild(valEl);

      const statEl = document.createElement('div');
      statEl.className   = 'chan-stat';
      statEl.textContent = `${pct}% del total · ${(ch.count || 0).toLocaleString('es-CO')} pedidos`;

      const barWrap = document.createElement('div');
      barWrap.className = 'chan-bar';
      const barFill = document.createElement('div');
      barFill.style.width      = `${barPct}%`;
      barFill.style.background = meta.barColor;
      barWrap.appendChild(barFill);

      body.appendChild(row1);
      body.appendChild(statEl);
      body.appendChild(barWrap);
      item.appendChild(ico);
      item.appendChild(body);
      list.appendChild(item);
    });
  } catch (e) {
    console.warn('[dashboard] loadSalesByChannel failed', e);
    if (list) list.setAttribute('data-error', 'true');
  }
}

// ── 5. Live orders (/api/stats/live-orders) ───────────────────────────────────
const STATUS_LABELS = {
  pending:   'Pendiente',
  confirmed: 'Confirmado',
  preparing: 'En cocina',
  ready:     'Listo',
  delivered: 'Entregado',
  cancelled: 'Cancelado',
  paid:      'Pagado',
};
const ORDER_TYPE_LABELS = {
  delivery: 'Domicilio',
  pickup:   'Para recoger',
  dine_in:  'Salón',
  table:    'Salón',
};

async function loadLiveOrders() {
  const list      = document.getElementById('live-orders-list');
  const badgeEl   = document.getElementById('live-count-badge');
  const sbBadge   = document.getElementById('sb-live-orders-badge');
  if (!list) return;
  try {
    const data   = await _apiFetch('/api/stats/live-orders?limit=5');
    const orders = data.orders || [];

    if (badgeEl) badgeEl.textContent = (data.total_active || orders.length) + ' activos';
    if (sbBadge) {
      sbBadge.textContent = String(data.total_active || orders.length);
      sbBadge.style.display = orders.length ? '' : 'none';
    }

    if (!orders.length) {
      list.innerHTML = '<div style="padding:24px;text-align:center;font-size:12px;color:var(--text-3);">Sin pedidos activos.</div>';
      return;
    }

    list.innerHTML = '';
    orders.forEach(o => {
      const row = document.createElement('div');
      row.className = 'live-row';

      // ID cell
      const idEl = document.createElement('div');
      idEl.className   = 'live-row-id';
      idEl.textContent = `#${o.id}`;

      // Middle cell
      const mid = document.createElement('div');

      const nameEl = document.createElement('div');
      nameEl.className = 'live-row-name';
      // Primary label: table or customer
      let nameTxt = '';
      if (o.table_name)   nameTxt = `Mesa ${o.table_name}`;
      else if (o.channel === 'delivery') nameTxt = `Domicilio · ${o.customer_phone || ''}`;
      else if (o.channel === 'pickup')   nameTxt = `WhatsApp · ${o.customer_phone || ''}`;
      else                               nameTxt = ORDER_TYPE_LABELS[o.order_type] || o.channel || '—';
      nameEl.textContent = nameTxt;

      // Status badge appended inline (trusted static classes only)
      if (o.status) {
        const stBadge = document.createElement('span');
        const badgeCls = {
          preparing: 'info', ready: 'success', pending: 'warn',
          delivered: 'success', paid: 'success', cancelled: 'danger',
        }[o.status] || 'default';
        stBadge.className   = `badge ${badgeCls}`;
        stBadge.style.marginLeft = '4px';
        stBadge.textContent = STATUS_LABELS[o.status] || o.status;
        nameEl.appendChild(stBadge);
      }

      const subEl = document.createElement('div');
      subEl.className   = 'live-row-sub';
      subEl.textContent = o.items_summary || o.address || '';

      mid.appendChild(nameEl);
      mid.appendChild(subEl);

      // Right cell
      const right = document.createElement('div');
      const priceEl = document.createElement('div');
      priceEl.className   = 'live-row-price';
      priceEl.textContent = mesioFmt(o.total || 0);
      const timeEl = document.createElement('div');
      timeEl.className   = 'live-row-time';
      timeEl.textContent = o.elapsed_label || o.created_at_label || '';
      right.appendChild(priceEl);
      right.appendChild(timeEl);

      row.appendChild(idEl);
      row.appendChild(mid);
      row.appendChild(right);
      list.appendChild(row);
    });
  } catch (e) {
    console.warn('[dashboard] loadLiveOrders failed', e);
    if (list) list.setAttribute('data-error', 'true');
  }
}

// ── 6. Payment status donut (/api/stats/payment-status) ──────────────────────
async function loadPaymentStatus() {
  const [ps, pe] = _dateRange(_period);
  const pctEl    = document.getElementById('donut-pct');
  const arcEl    = document.getElementById('donut-arc');
  const legendEl = document.getElementById('payment-legend');
  if (!pctEl || !arcEl || !legendEl) return;
  try {
    const data    = await _apiFetch(`/api/stats/payment-status?period_start=${ps}&period_end=${pe}`);
    const buckets = data.buckets || [];
    const total   = data.total_count || 0;

    const paidBucket  = buckets.find(b => b.key === 'paid');
    const paidCount   = paidBucket?.count || 0;
    const paidPct     = total ? Math.round(paidCount / total * 100) : 0;

    // Donut arc: circumference = 2πr = 2π×52 ≈ 326.7
    const circ    = 326.7;
    const offset  = circ - (circ * paidPct / 100);
    arcEl.style.strokeDashoffset = String(offset);
    pctEl.textContent = `${paidPct}%`;

    // Legend
    legendEl.innerHTML = '';
    const bucketMeta = {
      paid:      { label: 'Pagados',   color: 'var(--brand)' },
      pending:   { label: 'Pendientes',color: 'var(--warning,#f59e0b)' },
      disputed:  { label: 'Disputa',   color: 'var(--danger,#ef4444)' },
      courtesy:  { label: 'Cortesía',  color: '#e0e0d8' },
    };
    const display = ['paid','pending','disputed','courtesy'];
    display.forEach(key => {
      const meta  = bucketMeta[key] || { label: key, color: '#e0e0d8' };
      const count = (buckets.find(b => b.key === key) || {}).count || 0;
      const item  = document.createElement('div');
      item.className = 'legend-item';

      const dot  = document.createElement('span');
      dot.className         = 'legend-dot-sq';
      dot.style.background  = meta.color;

      const lbl  = document.createElement('span');
      lbl.style.cssText = 'font-size:12.5px;color:var(--text-2);';
      lbl.textContent   = meta.label;

      const num  = document.createElement('span');
      num.className   = 'legend-num';
      num.textContent = count.toLocaleString('es-CO');

      item.appendChild(dot);
      item.appendChild(lbl);
      item.appendChild(num);
      legendEl.appendChild(item);
    });

    // Total row
    const hr = document.createElement('div');
    hr.className = 'hairline';
    hr.style.cssText = 'margin:10px 0 6px;';
    legendEl.appendChild(hr);

    const totalRow = document.createElement('div');
    totalRow.className   = 'legend-item';
    totalRow.style.padding = '2px 0';
    const totLbl   = document.createElement('span');
    totLbl.style.cssText = 'font-size:12px;color:var(--text-3);';
    totLbl.textContent   = 'Total pedidos';
    const totNum   = document.createElement('span');
    totNum.className      = 'legend-num';
    totNum.style.fontWeight = '700';
    totNum.textContent    = total.toLocaleString('es-CO');
    totalRow.appendChild(totLbl);
    totalRow.appendChild(totNum);
    legendEl.appendChild(totalRow);
  } catch (e) {
    console.warn('[dashboard] loadPaymentStatus failed', e);
    if (pctEl) pctEl.textContent = '—';
    if (legendEl) legendEl.setAttribute('data-error', 'true');
  }
}

// ── 7. Top dishes (/api/stats/top-dishes) ────────────────────────────────────
async function loadTopDishes() {
  const [ps, pe] = _dateRange(_period);
  const tbody    = document.getElementById('top-dishes-body');
  if (!tbody) return;
  try {
    const data   = await _apiFetch(`/api/stats/top-dishes?period_start=${ps}&period_end=${pe}&limit=5`);
    const dishes = data.dishes || [];

    tbody.innerHTML = '';
    if (!dishes.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 4;
      td.style.cssText = 'text-align:center;padding:24px;font-size:12px;color:var(--text-3);';
      td.textContent = 'Sin datos para este período.';
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    dishes.forEach((dish, idx) => {
      const tr = document.createElement('tr');

      // Rank
      const tdRank = document.createElement('td');
      tdRank.className = 'muted mono';
      tdRank.textContent = String(idx + 1).padStart(2, '0');

      // Name + meta
      const tdName = document.createElement('td');
      const nameDiv = document.createElement('div');
      nameDiv.style.fontWeight = '500';
      nameDiv.textContent      = dish.name || '—';
      const subDiv  = document.createElement('div');
      subDiv.style.cssText = 'font-size:11px;color:var(--text-3);';
      subDiv.textContent   = `${mesioFmt(dish.price || 0)} · ${dish.category || '—'}`;
      tdName.appendChild(nameDiv);
      tdName.appendChild(subDiv);

      // Count
      const tdCount = document.createElement('td');
      tdCount.className   = 'num';
      tdCount.textContent = (dish.count || 0).toLocaleString('es-CO');

      // Margin badge
      const tdMargin = document.createElement('td');
      tdMargin.className = 'num';
      const pct = dish.margin_pct != null ? Math.round(dish.margin_pct) : null;
      if (pct != null) {
        const badge = document.createElement('span');
        badge.className   = pct >= 50 ? 'badge success' : 'badge';
        badge.textContent = `${pct}%`;
        tdMargin.appendChild(badge);
      } else {
        tdMargin.textContent = '—';
      }

      tr.appendChild(tdRank);
      tr.appendChild(tdName);
      tr.appendChild(tdCount);
      tr.appendChild(tdMargin);
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.warn('[dashboard] loadTopDishes failed', e);
    if (tbody) tbody.setAttribute('data-error', 'true');
  }
}

// ── 8. Inventory critical (/api/stats/inventory-critical) ─────────────────────
async function loadInventoryCritical() {
  const tbody   = document.getElementById('inventory-body');
  const badgeEl = document.getElementById('inventory-alerts-badge');
  if (!tbody) return;
  try {
    const data   = await _apiFetch('/api/stats/inventory-critical');
    const alerts = data.alerts || [];
    const ok     = data.ok     || [];
    const rows   = [...alerts, ...ok];

    if (badgeEl) {
      if (alerts.length) {
        badgeEl.textContent    = `${alerts.length} ${alerts.length === 1 ? 'alerta' : 'alertas'}`;
        badgeEl.style.display  = '';
      } else {
        badgeEl.style.display  = 'none';
      }
    }

    tbody.innerHTML = '';
    if (!rows.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 3;
      td.style.cssText = 'text-align:center;padding:24px;font-size:12px;color:var(--text-3);';
      td.textContent   = 'Sin alertas de inventario.';
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    rows.forEach(item => {
      const isCritical = item.status === 'critical';
      const isWarning  = item.status === 'warning';

      const tr = document.createElement('tr');

      // Name + unit / category
      const tdName = document.createElement('td');
      const nameDiv = document.createElement('div');
      nameDiv.style.fontWeight = '500';
      nameDiv.textContent      = item.name || '—';
      const subDiv  = document.createElement('div');
      subDiv.style.cssText = 'font-size:11px;color:var(--text-3);';
      subDiv.textContent   = `${item.unit || ''} · ${item.category || '—'}`;
      tdName.appendChild(nameDiv);
      tdName.appendChild(subDiv);

      // Stock
      const tdStock = document.createElement('td');
      tdStock.className = 'num';
      const stockVal = document.createElement('div');
      stockVal.style.fontWeight = '600';
      stockVal.style.color = isCritical ? 'var(--danger,#ef4444)' : isWarning ? 'var(--warning-text,#92400e)' : 'inherit';
      stockVal.textContent = `${item.stock ?? '—'} ${item.unit || ''}`.trim();
      const minVal  = document.createElement('div');
      minVal.style.cssText = 'font-size:10.5px;color:var(--text-3);';
      minVal.textContent   = item.min_stock != null ? `mín. ${item.min_stock} ${item.unit || ''}`.trim() : '';
      tdStock.appendChild(stockVal);
      tdStock.appendChild(minVal);

      // Affected dishes
      const tdAffects = document.createElement('td');
      const affects   = item.affects || [];
      if (affects.length) {
        affects.slice(0, 2).forEach(dishName => {
          const badge = document.createElement('span');
          badge.className   = isCritical ? 'badge danger' : isWarning ? 'badge warn' : 'badge success';
          badge.style.marginRight = '4px';
          badge.textContent = dishName;
          tdAffects.appendChild(badge);
        });
      } else if (item.status === 'ok') {
        const badge = document.createElement('span');
        badge.className   = 'badge success';
        badge.textContent = 'OK';
        tdAffects.appendChild(badge);
      }

      tr.appendChild(tdName);
      tr.appendChild(tdStock);
      tr.appendChild(tdAffects);
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.warn('[dashboard] loadInventoryCritical failed', e);
    if (tbody) tbody.setAttribute('data-error', 'true');
  }
}

// ── 9. At-risk customers badge (/api/stats/customers-at-risk) ─────────────────
async function loadAtRiskBadge() {
  const badgeEl = document.getElementById('sb-risk-badge');
  if (!badgeEl) return;
  try {
    const data  = await _apiFetch('/api/stats/customers-at-risk?limit=1');
    const count = data.count || 0;
    if (count > 0) {
      badgeEl.textContent   = String(count);
      badgeEl.style.display = '';
    } else {
      badgeEl.style.display = 'none';
    }
  } catch {
    badgeEl.style.display = 'none';
  }
}

// ── Utility: sparkline renderer ───────────────────────────────────────────────
function _renderSparkline(containerId, values, muted) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = '';
  if (!values || !values.length) return;
  const maxVal = Math.max(...values, 1);
  values.forEach(v => {
    const span = document.createElement('span');
    span.style.height = `${Math.max(Math.round((v / maxVal) * 100), 5)}%`;
    el.appendChild(span);
  });
  if (muted) el.classList.add('mute');
}

// ── Utility: set text content safely ─────────────────────────────────────────
function _setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function _setError(id) {
  const el = document.getElementById(id);
  if (el) el.textContent = '—';
}
