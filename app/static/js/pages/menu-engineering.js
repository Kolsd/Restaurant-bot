/* ── Menu Engineering page ───────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // Period filter buttons
  document.querySelectorAll('.page-head .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.page-head .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      loadEngineering(btn.dataset.days || '30');
    });
  });

  // Table filter buttons
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      filterTableRows(btn.textContent.trim().toLowerCase());
    });
  });

  function filterTableRows(filter) {
    const tbody = document.getElementById('me-tbody');
    if (!tbody) return;
    tbody.querySelectorAll('tr').forEach(function (row) {
      if (filter === 'todos') { row.style.display = ''; return; }
      const badge = row.querySelector('.quad-badge');
      const cls = badge ? badge.className : '';
      const visible = filter.includes('star') ? cls.includes('stars')
        : filter.includes('puzzle') ? cls.includes('puzzles')
        : filter.includes('plowhorse') ? cls.includes('plowhorses')
        : filter.includes('dog') ? cls.includes('dogs')
        : true;
      row.style.display = visible ? '' : 'none';
    });
  }

  // Table column sort
  document.querySelectorAll('.me-tbl th').forEach(function (th) {
    th.addEventListener('click', function () {
      document.querySelectorAll('.me-tbl th').forEach(function (t) { t.classList.remove('active'); });
      th.classList.add('active');
    });
  });

  // Export button
  const exportBtn = document.querySelector('.card.flush .btn.sm.ghost');
  if (exportBtn) {
    exportBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Exportación próximamente disponible', 'info');
      }
    });
  }

  // ── Quadrant helpers ──────────────────────────────────────────────

  function classify(dish) {
    // Quadrant = f(popularity_rank, margin_rank)
    const highPop = dish.is_high_popularity || dish.quadrant === 'star' || dish.quadrant === 'plowhorse';
    const highMgn = dish.is_high_margin || dish.quadrant === 'star' || dish.quadrant === 'puzzle';
    if (dish.quadrant) return dish.quadrant;
    if (highPop && highMgn) return 'star';
    if (!highPop && highMgn) return 'puzzle';
    if (highPop && !highMgn) return 'plowhorse';
    return 'dog';
  }

  const QUAD_META = {
    star:      { label: '⭐ Star',       cls: 'stars',      badgeHtml: '<span class="quad-badge stars">⭐ Star</span>' },
    puzzle:    { label: '🧩 Puzzle',      cls: 'puzzles',    badgeHtml: '<span class="quad-badge puzzles">🧩 Puzzle</span>' },
    plowhorse: { label: '🐎 Plowhorse',  cls: 'plowhorses', badgeHtml: '<span class="quad-badge plowhorses">🐎 Plowhorse</span>' },
    dog:       { label: '🐶 Dog',        cls: 'dogs',       badgeHtml: '<span class="quad-badge dogs">🐶 Dog</span>' },
  };

  // Bubble position: Stars top-right, Puzzles top-left, Plowhorses bottom-right, Dogs bottom-left
  const QUAD_ORIGIN = {
    star:      { top: 10, left: 60 },
    puzzle:    { top: 10, left: 5  },
    plowhorse: { top: 55, left: 60 },
    dog:       { top: 55, left: 5  },
  };

  // ── Render ────────────────────────────────────────────────────────

  function renderStats(grouped) {
    const total = Object.values(grouped).reduce(function (s, arr) { return s + arr.length; }, 0);
    const setVal = function (id, v) { const el = document.getElementById(id); if (el) el.textContent = v; };
    setVal('stat-total', total);
    setVal('stat-stars', (grouped.star || []).length);
    setVal('stat-puzzles', (grouped.puzzle || []).length);
    setVal('stat-plowhorses', (grouped.plowhorse || []).length);
    setVal('stat-dogs', (grouped.dog || []).length);

    // Update quad counts in the matrix
    const quads = document.querySelectorAll('.quad');
    const order = ['puzzle', 'star', 'dog', 'plowhorse'];
    quads.forEach(function (q, i) {
      const cnt = q.querySelector('.quad-count');
      if (cnt && order[i]) cnt.textContent = (grouped[order[i]] || []).length;
    });
  }

  function renderBubbles(grouped) {
    const matrix = document.querySelector('.matrix');
    if (!matrix) return;

    // Remove old bubbles
    matrix.querySelectorAll('.bubble').forEach(function (b) { b.remove(); });

    Object.keys(grouped).forEach(function (quad) {
      const meta = QUAD_META[quad];
      const origin = QUAD_ORIGIN[quad];
      if (!meta || !origin) return;

      const dishes = grouped[quad];
      const maxQty = Math.max.apply(null, dishes.map(function (d) { return d.quantity || d.units_sold || 1; }).concat([1]));

      dishes.slice(0, 6).forEach(function (dish, i) {
        const qty = dish.quantity || dish.units_sold || 0;
        const name = dish.name || dish.dish_name || '';
        const shortName = name.split(' ').slice(0, 2).join(' ');
        const size = Math.max(30, Math.min(70, 30 + Math.round((qty / maxQty) * 40)));

        // Scatter within quadrant: 3 columns × 2 rows
        const col = i % 3;
        const row = Math.floor(i / 3);
        const left = origin.left + col * 13;
        const top = origin.top + row * 20;

        const bubble = document.createElement('div');
        bubble.className = 'bubble ' + meta.cls;
        bubble.style.cssText = 'top:' + top + '%;left:' + left + '%;width:' + size + 'px;height:' + size + 'px;font-size:9px;';
        bubble.title = name + (qty ? ' — ' + qty + ' uds' : '');
        bubble.textContent = shortName + (qty ? ' ' + qty : '');

        bubble.addEventListener('click', function () {
          if (typeof mesioToast === 'function') {
            const margin = dish.margin_pct ? ' · margen ' + Math.round(dish.margin_pct) + '%' : '';
            mesioToast(name + ' · ' + qty + ' uds' + margin, 'info', 2500);
          }
        });

        matrix.appendChild(bubble);
      });
    });
  }

  function renderTable(dishes) {
    const tbody = document.getElementById('me-tbody');
    if (!tbody) return;

    if (!dishes || !dishes.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="padding:16px;color:var(--text-3);">(sin datos)</td></tr>';
      tbody.setAttribute('data-loaded', 'true');
      return;
    }

    const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };

    tbody.innerHTML = dishes.map(function (d) {
      const quad = classify(d);
      const meta = QUAD_META[quad] || QUAD_META.dog;
      const name = d.name || d.dish_name || '';
      const cat = d.category || '';
      const qty = d.quantity || d.units_sold || 0;
      const price = d.price || 0;
      const cost = d.food_cost || d.cost || 0;
      const margin = d.margin_pct !== undefined ? Math.round(d.margin_pct) + '%'
        : (price > 0 ? Math.round((1 - cost / price) * 100) + '%' : '—');
      const revenue = d.revenue || (qty * price) || 0;

      return '<tr>' +
        '<td style="font-weight:500;">' + _escHtml(name) + '</td>' +
        '<td style="color:var(--text-3);">' + _escHtml(cat) + '</td>' +
        '<td class="num">' + qty + '</td>' +
        '<td class="num">' + fmt(price) + '</td>' +
        '<td class="num">' + margin + '</td>' +
        '<td class="num">' + fmt(cost) + '</td>' +
        '<td class="num">' + fmt(revenue) + '</td>' +
        '<td>' + meta.badgeHtml + '</td>' +
        '</tr>';
    }).join('');

    tbody.setAttribute('data-loaded', 'true');
  }

  // ── Load ──────────────────────────────────────────────────────────

  async function loadEngineering(days) {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/menu/analytics?days=' + days, { headers });
      if (!res.ok) { return; }
      const data = await res.json();

      // Response may have pre-classified quadrants or a flat list
      let allDishes = [];
      const grouped = { star: [], puzzle: [], plowhorse: [], dog: [] };

      if (data.stars || data.puzzles || data.plowhorses || data.dogs) {
        // Pre-classified response
        grouped.star      = data.stars      || [];
        grouped.puzzle    = data.puzzles    || [];
        grouped.plowhorse = data.plowhorses || [];
        grouped.dog       = data.dogs       || [];
        allDishes = [].concat(grouped.star, grouped.puzzle, grouped.plowhorse, grouped.dog);
      } else {
        // Flat list — classify client-side
        allDishes = data.dishes || data.items || data;
        if (Array.isArray(allDishes)) {
          allDishes.forEach(function (d) {
            const q = classify(d);
            grouped[q] = grouped[q] || [];
            grouped[q].push(d);
          });
        }
      }

      renderStats(grouped);
      renderBubbles(grouped);
      renderTable(allDishes);

    } catch (e) {
      console.error('menu-engineering: error', e);
    }
  }

  // Load with default period
  loadEngineering('30');
})();
