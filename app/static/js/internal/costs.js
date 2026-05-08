/**
 * app/static/js/internal/costs.js
 *
 * Vanilla JS for the Mesio Cost Monitor dashboard (/internal/costs).
 * Auth pattern: same session token as analytics.html (SESSION_KEY 'hq_session').
 * All DOM writes use textContent (never innerHTML with user data) to prevent XSS.
 */

// ── Auth ──────────────────────────────────────────────────────────────────────
const SESSION_KEY = 'hq_session';
let _token = sessionStorage.getItem(SESSION_KEY) || sessionStorage.getItem('mesio_admin_key') || '';

function applyToken(token) {
  _token = token;
  sessionStorage.setItem(SESSION_KEY, token);
  sessionStorage.setItem('mesio_admin_key', token); // compat alias
  document.getElementById('auth-overlay').style.display = 'none';
  document.getElementById('app').style.display = '';
  initDatePickers();
  loadAll();
}

document.getElementById('auth-btn').addEventListener('click', async () => {
  const val = document.getElementById('key-input').value.trim();
  const errEl = document.getElementById('auth-error');
  if (!val) return;
  try {
    const r = await fetch('/api/internal/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: val }),
    });
    if (!r.ok) { errEl.style.display = 'block'; return; }
    const { token } = await r.json();
    errEl.style.display = 'none';
    applyToken(token);
  } catch (_) {
    errEl.style.display = 'block';
  }
});

document.getElementById('key-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('auth-btn').click();
});

if (_token) applyToken(_token);

// ── API helper ────────────────────────────────────────────────────────────────
function apiGet(path) {
  return fetch(path, { headers: { Authorization: `Bearer ${_token}` } })
    .then(r => {
      if (r.status === 401) {
        sessionStorage.removeItem(SESSION_KEY);
        sessionStorage.removeItem('mesio_admin_key');
        location.reload();
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
}

// ── Date range state ──────────────────────────────────────────────────────────
let _start = '';
let _end   = '';

function _isoToday() {
  return new Date().toISOString().slice(0, 10);
}

function _isoNDaysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

function _isoMonthStart() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-01`;
}

function initDatePickers() {
  const startEl = document.getElementById('date-start');
  const endEl   = document.getElementById('date-end');
  _start = _isoMonthStart();
  _end   = _isoToday();
  startEl.value = _start;
  endEl.value   = _end;

  document.getElementById('apply-btn').addEventListener('click', () => {
    _start = startEl.value || _start;
    _end   = endEl.value   || _end;
    loadAll();
  });

  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const preset = btn.dataset.preset;
      if (preset === 'mtd') {
        _start = _isoMonthStart();
        _end   = _isoToday();
      } else if (preset === '7d') {
        _start = _isoNDaysAgo(6);
        _end   = _isoToday();
      } else if (preset === '30d') {
        _start = _isoNDaysAgo(29);
        _end   = _isoToday();
      } else if (preset === '90d') {
        _start = _isoNDaysAgo(89);
        _end   = _isoToday();
      }
      startEl.value = _start;
      endEl.value   = _end;
      loadAll();
    });
  });
}

// ── KPI helpers ───────────────────────────────────────────────────────────────
function setKpi(id, value, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('skeleton', 'skel-kpi');
  el.className = 'kpi-value' + (cls ? ' ' + cls : '');
  el.textContent = value != null ? String(value) : '—';
}

function fmtUSD(n) {
  if (n == null) return '—';
  return '$' + n.toFixed(4);
}

function fmtCOP(n) {
  if (n == null) return '—';
  return '$' + Math.round(n).toLocaleString('es-CO') + ' COP';
}

function fmtTokens(n) {
  if (n == null) return '—';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'k';
  return String(n);
}

// ── Summary ───────────────────────────────────────────────────────────────────
function loadSummary() {
  return apiGet(`/api/internal/costs/summary?start=${_start}&end=${_end}`)
    .then(data => {
      setKpi('kpi-tokens',   fmtTokens(data.total_tokens), 'brand');
      setKpi('kpi-cost-usd', fmtUSD(data.estimated_cost_usd));
      setKpi('kpi-cost-cop', fmtCOP(data.estimated_cost_cop));

      // avg per restaurant is set after restaurants load — store raw for cross-fn use
      window._summaryData = data;
      renderDailyChart(data.by_day || []);
    });
}

// ── Daily chart ───────────────────────────────────────────────────────────────
function renderDailyChart(data) {
  const container = document.getElementById('chart-tokens');
  if (!data.length) {
    container.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'loading-text';
    p.textContent = 'No data for this range.';
    container.appendChild(p);
    return;
  }
  const max = Math.max(...data.map(d => d.tokens), 1);
  container.innerHTML = '';
  data.forEach((d, i) => {
    const col = document.createElement('div');
    col.className = 'bar-col';

    const bar = document.createElement('div');
    bar.className = 'bar';
    bar.style.height = Math.max(2, Math.round((d.tokens / max) * 100)) + 'px';
    bar.title = `${d.date}: ${fmtTokens(d.tokens)} — ${fmtUSD(d.cost_usd)}`;

    const label = document.createElement('div');
    label.className = 'bar-label' + (i % 5 === 0 ? '' : ' hidden');
    label.textContent = d.date ? d.date.slice(5) : '';

    col.appendChild(bar);
    col.appendChild(label);
    container.appendChild(col);
  });
}

// ── Restaurants table ─────────────────────────────────────────────────────────
let _restaurants = [];
let _sortCol     = 'estimated_cost_usd';
let _sortAsc     = false;
let _expandedId  = null;

function planClass(plan) {
  const p = (plan || '').toLowerCase();
  if (p === 'enterprise') return 'plan-enterprise';
  if (p === 'pro')        return 'plan-pro';
  if (p === 'basic')      return 'plan-basic';
  return 'plan-free';
}

function fmtMargin(cop, pct) {
  if (cop == null) return '—';
  const sign = cop >= 0 ? '+' : '';
  const copStr = sign + '$' + Math.round(Math.abs(cop)).toLocaleString('es-CO') + ' COP';
  const pctStr = pct != null ? ` (${pct > 0 ? '+' : ''}${pct}%)` : '';
  return copStr + pctStr;
}

function renderRestTable() {
  const tbody = document.getElementById('rest-tbody');
  tbody.innerHTML = '';

  if (!_restaurants.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8;
    td.className = 'loading-text';
    td.style.textAlign = 'center';
    td.style.padding = '2rem';
    td.textContent = 'No spend data for this range.';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  const sorted = [..._restaurants].sort((a, b) => {
    let va = a[_sortCol], vb = b[_sortCol];
    if (va == null) va = _sortAsc ? Infinity : -Infinity;
    if (vb == null) vb = _sortAsc ? Infinity : -Infinity;
    if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb || '').toLowerCase(); }
    return _sortAsc ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1);
  });

  sorted.forEach((rest, idx) => {
    const tr = document.createElement('tr');
    if (_expandedId === rest.org_id) tr.classList.add('expanded');

    const rankTd = document.createElement('td');
    rankTd.textContent = String(idx + 1);
    rankTd.style.color = 'var(--text-4)';
    rankTd.style.fontSize = '0.78rem';

    const nameTd = document.createElement('td');
    nameTd.textContent = rest.org_name || '—';

    const planTd = document.createElement('td');
    const pill = document.createElement('span');
    pill.className = 'plan-pill ' + planClass(rest.plan_code);
    pill.textContent = rest.plan_code || 'free';
    planTd.appendChild(pill);

    const tokensTd = document.createElement('td');
    tokensTd.className = 'num-right';
    tokensTd.textContent = fmtTokens(rest.total_tokens);

    const usdTd = document.createElement('td');
    usdTd.className = 'num-right';
    usdTd.textContent = fmtUSD(rest.estimated_cost_usd);

    const copTd = document.createElement('td');
    copTd.className = 'num-right';
    copTd.textContent = fmtCOP(rest.estimated_cost_cop);

    const marginTd = document.createElement('td');
    marginTd.className = 'num-right';
    if (rest.margin_cop == null) {
      marginTd.textContent = '—';
      marginTd.style.color = 'var(--text-4)';
    } else {
      marginTd.textContent = fmtMargin(rest.margin_cop, rest.margin_pct);
      marginTd.style.color = rest.margin_cop >= 0 ? 'var(--success)' : 'var(--danger)';
    }

    const ordersTd = document.createElement('td');
    ordersTd.className = 'num-right';
    ordersTd.textContent = rest.orders_count != null ? rest.orders_count.toLocaleString() : '—';

    tr.append(rankTd, nameTd, planTd, tokensTd, usdTd, copTd, marginTd, ordersTd);

    tr.addEventListener('click', () => {
      _expandedId = _expandedId === rest.org_id ? null : rest.org_id;
      if (_expandedId) loadDetail(rest.org_id);
      else renderRestTable();
    });

    tbody.appendChild(tr);

    // Expanded detail row
    if (_expandedId === rest.org_id) {
      const dtr = document.createElement('tr');
      dtr.className = 'detail-row';
      const dtd = document.createElement('td');
      dtd.colSpan = 8;
      dtd.id = `detail-${rest.org_id}`;
      dtd.textContent = 'Loading daily breakdown…';
      dtr.appendChild(dtd);
      tbody.appendChild(dtr);
    }
  });

  // Avg cost per restaurant
  if (window._summaryData && _restaurants.length > 0) {
    const avgUsd = (window._summaryData.estimated_cost_usd || 0) / _restaurants.length;
    setKpi('kpi-avg-rest', fmtUSD(avgUsd));
    const subEl = document.getElementById('kpi-avg-sub');
    if (subEl) subEl.textContent = `across ${_restaurants.length} active restaurants`;
  }
}

function sortTable(col) {
  if (_sortCol === col) _sortAsc = !_sortAsc;
  else { _sortCol = col; _sortAsc = false; }
  renderRestTable();
}

function loadRestaurants() {
  return apiGet(`/api/internal/costs/restaurants?start=${_start}&end=${_end}&limit=50`)
    .then(data => {
      _restaurants = data.restaurants || [];
      renderRestTable();
    });
}

// ── Detail panel (inside expanded row) ───────────────────────────────────────
function loadDetail(orgId) {
  apiGet(`/api/internal/costs/restaurants/${orgId}?start=${_start}&end=${_end}`)
    .then(data => {
      const el = document.getElementById(`detail-${orgId}`);
      if (!el) return;

      const t = data.totals || {};
      const header = document.createElement('div');
      header.style.cssText = 'display:flex;gap:2rem;margin-bottom:.75rem;flex-wrap:wrap;font-size:.82rem;color:#9ca3af';

      [
        ['Org', data.org_name],
        ['Plan', (data.plan_code || 'free').toUpperCase()],
        ['Total tokens', fmtTokens(t.tokens)],
        ['Cost USD', fmtUSD(t.cost_usd)],
        ['Cost COP', fmtCOP(t.cost_cop)],
        ['Orders', t.orders_count != null ? t.orders_count.toLocaleString() : '—'],
      ].forEach(([label, val]) => {
        const span = document.createElement('span');
        const strong = document.createElement('strong');
        strong.style.color = '#fff';
        strong.textContent = val;
        span.textContent = label + ': ';
        span.appendChild(strong);
        header.appendChild(span);
      });

      // Mini daily chart
      const chartTitle = document.createElement('div');
      chartTitle.style.cssText = 'font-size:.78rem;color:#6b7280;margin-bottom:.5rem;';
      chartTitle.textContent = 'Daily tokens (this period)';

      const miniChart = document.createElement('div');
      miniChart.style.cssText = 'display:flex;align-items:flex-end;gap:2px;height:48px;overflow:hidden;';
      const byDay = data.by_day || [];
      const maxT = Math.max(...byDay.map(d => d.tokens), 1);
      byDay.forEach(d => {
        const bar = document.createElement('div');
        bar.style.cssText = `flex:1;min-width:4px;background:var(--brand);border-radius:2px 2px 0 0;opacity:.7;`;
        bar.style.height = Math.max(2, Math.round((d.tokens / maxT) * 44)) + 'px';
        bar.title = `${d.date}: ${fmtTokens(d.tokens)}`;
        miniChart.appendChild(bar);
      });

      el.innerHTML = '';
      el.appendChild(header);
      el.appendChild(chartTitle);
      el.appendChild(miniChart);
    })
    .catch(() => {
      const el = document.getElementById(`detail-${orgId}`);
      if (el) el.textContent = 'Failed to load detail.';
    });
}

// ── Outliers ──────────────────────────────────────────────────────────────────
function loadOutliers() {
  return apiGet(`/api/internal/costs/outliers?start=${_start}&end=${_end}&threshold_pct=200`)
    .then(data => {
      const container = document.getElementById('outliers-container');
      container.innerHTML = '';
      const outliers = data.outliers || [];

      if (!outliers.length) {
        const p = document.createElement('p');
        p.className = 'loading-text';
        p.textContent = 'No outliers detected — all restaurants within 2x plan limit.';
        container.appendChild(p);
        return;
      }

      const banner = document.createElement('div');
      banner.className = 'outlier-banner';

      const title = document.createElement('div');
      title.className = 'outlier-title';
      title.textContent = `${outliers.length} restaurant${outliers.length > 1 ? 's' : ''} exceeding 2x plan token limit`;
      banner.appendChild(title);

      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      const tbl = document.createElement('table');
      tbl.className = 'data-table';
      tbl.innerHTML = `<thead><tr>
        <th>Restaurant</th><th>Plan</th>
        <th class="num-right">Plan Limit/day</th>
        <th class="num-right">Avg Daily Tokens</th>
        <th class="num-right">% of Limit</th>
        <th class="num-right">Total Tokens</th>
        <th class="num-right">Cost USD</th>
      </tr></thead>`;
      const tbody = document.createElement('tbody');

      outliers.forEach(row => {
        const tr = document.createElement('tr');
        const pct = row.pct_of_limit || 0;
        const pctClass = pct >= 500 ? 'outlier-danger' : 'outlier-warning';

        const cells = [
          row.org_name || '—',
          row.plan_code || 'free',
          fmtTokens(row.plan_daily_token_limit),
          fmtTokens(row.avg_daily_tokens),
          null, // pct pill rendered separately
          fmtTokens(row.total_tokens),
          fmtUSD(row.estimated_cost_usd),
        ];

        cells.forEach((val, i) => {
          const td = document.createElement('td');
          if (i >= 2) td.className = 'num-right';
          if (i === 4) {
            const span = document.createElement('span');
            span.className = 'outlier-pct ' + pctClass;
            span.textContent = pct.toFixed(0) + '%';
            td.appendChild(span);
          } else {
            td.textContent = val;
          }
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });

      tbl.appendChild(tbody);
      wrap.appendChild(tbl);
      banner.appendChild(wrap);
      container.appendChild(banner);
    });
}

// ── Timestamp ─────────────────────────────────────────────────────────────────
function updateTimestamp() {
  const el = document.getElementById('last-update');
  if (el) el.textContent = 'Updated ' + new Date().toLocaleTimeString();
}

// ── Platform margin KPIs ──────────────────────────────────────────────────────
function loadMargin() {
  return apiGet(`/api/internal/costs/margin?start=${_start}&end=${_end}`)
    .then(data => {
      const isPos = n => n != null && n >= 0;
      setKpi('kpi-revenue-cop', fmtCOP(data.total_revenue_cop), 'brand');
      const subEl = document.getElementById('kpi-revenue-sub');
      if (subEl && data.paying_orgs != null) {
        subEl.textContent = `${data.paying_orgs} paying orgs`;
      }
      setKpi('kpi-margin-cost', fmtCOP(data.total_cost_cop));
      setKpi('kpi-net-margin', fmtCOP(data.net_margin_cop),
        isPos(data.net_margin_cop) ? 'text-ok' : 'text-danger');
      setKpi('kpi-margin-pct',
        data.margin_pct != null ? (data.margin_pct > 0 ? '+' : '') + data.margin_pct + '%' : '—',
        isPos(data.margin_pct) ? 'text-ok' : 'text-danger');
    })
    .catch(() => {
      ['kpi-revenue-cop','kpi-margin-cost','kpi-net-margin','kpi-margin-pct'].forEach(id => {
        setKpi(id, '—');
      });
    });
}

// ── Load all ──────────────────────────────────────────────────────────────────
function loadAll() {
  _expandedId = null;
  Promise.allSettled([
    loadSummary(),
    loadRestaurants(),
    loadOutliers(),
    loadMargin(),
  ]).then(() => updateTimestamp());
}

// Auto-refresh every 60 seconds
setInterval(() => {
  if (_token) loadAll();
}, 60_000);
