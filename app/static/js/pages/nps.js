/* ── NPS page: survey score + read-only feed of answers ───────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  // /api/nps/responses takes a named period, the buttons carry days.
  const _PERIOD_BY_DAYS = { '7': 'week', '30': 'month', '90': 'semester', '365': 'year' };
  let _feedPeriod = 'month';

  // Period filter
  document.querySelectorAll('.page-head .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.page-head .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      const days = btn.dataset.days || '30';
      _feedPeriod = _PERIOD_BY_DAYS[days] || 'month';
      loadNPS(days);
      loadReviews();
    });
  });

  // Feed filter
  document.querySelectorAll('.card.flush .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.card.flush .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      filterFeed(btn.dataset.feedFilter || btn.textContent.trim());
    });
  });

  function filterFeed(filter) {
    const feed = document.getElementById('reviews-feed');
    if (!feed) return;
    feed.querySelectorAll('.review').forEach(function (rev) {
      if (filter === 'all' || filter === 'Todas') { rev.style.display = ''; return; }
      const visible = filter === '5star' || filter === '5★' ? rev.dataset.stars === '5'
        : filter === 'low' || filter === '1-3★' ? parseInt(rev.dataset.stars || '5', 10) <= 3
        : true;
      rev.style.display = visible ? '' : 'none';
    });
    // Say so when the filter leaves nothing, instead of a blank card.
    let empty = document.getElementById('reviews-feed-empty');
    const anyVisible = [...feed.querySelectorAll('.review')].some(function (r) { return r.style.display !== 'none'; });
    if (!anyVisible && feed.querySelector('.review')) {
      if (!empty) {
        empty = document.createElement('div');
        empty.id = 'reviews-feed-empty';
        empty.style.cssText = 'padding:24px;color:var(--text-3);';
        empty.textContent = 'Ninguna respuesta con ese filtro en este período.';
        feed.appendChild(empty);
      }
    } else if (empty) {
      empty.remove();
    }
  }

  // ── NPS stats render ──────────────────────────────────────────────

  function renderNPSStats(data) {
    const scoreEl = document.getElementById('nps-score');
    const totalLabelEl = document.getElementById('nps-total-label');
    const ringEl = document.getElementById('nps-ring');
    const ringLabelsEl = document.getElementById('nps-ring-labels');
    const promoEl = document.getElementById('nps-promoters');
    const passEl = document.getElementById('nps-passives');
    const detrEl = document.getElementById('nps-detractors');
    const ringPromo = document.getElementById('nps-ring-promo');
    const ringPass = document.getElementById('nps-ring-pass');
    const ringDetr = document.getElementById('nps-ring-detr');

    if (!data || !scoreEl) return;

    const total = data.total_responses || 0;

    if (total === 0) {
      scoreEl.textContent = '—';
      if (totalLabelEl) {
        totalLabelEl.textContent = 'Sin datos suficientes aún';
        totalLabelEl.style.color = 'var(--text-3)';
      }
      if (ringEl) ringEl.style.display = 'none';
      if (ringLabelsEl) ringLabelsEl.style.display = 'none';
      return;
    }

    const score = typeof data.nps_score === 'number' ? Math.round(data.nps_score) : '—';
    scoreEl.textContent = score;

    if (totalLabelEl) {
      totalLabelEl.textContent = total + ' respuesta' + (total === 1 ? '' : 's');
      totalLabelEl.style.color = 'var(--text-2)';
    }

    // Distribution ring — only show when there's enough data to be meaningful
    const promoters = data.promoters || 0;
    const passives = data.passives || 0;
    const detractors = data.detractors || 0;
    const promoPct = total > 0 ? Math.round((promoters / total) * 100) : 0;
    const passPct = total > 0 ? Math.round((passives / total) * 100) : 0;
    const detrPct = total > 0 ? Math.round((detractors / total) * 100) : 0;

    if (ringEl) {
      ringEl.style.display = 'flex';
      if (ringPromo) ringPromo.style.flex = '0 0 ' + promoPct + '%';
      if (ringPass) ringPass.style.flex = '0 0 ' + passPct + '%';
      if (ringDetr) ringDetr.style.flex = '0 0 ' + detrPct + '%';
    }
    if (ringLabelsEl) {
      ringLabelsEl.style.display = '';
      if (promoEl) promoEl.textContent = promoPct + '% promo.';
      if (passEl) passEl.textContent = passPct + '% pas.';
      if (detrEl) detrEl.textContent = detrPct + '% detr.';
    }
  }

  async function loadNPS(days) {
    const scoreEl = document.getElementById('nps-score');
    if (scoreEl) scoreEl.textContent = '…';
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const d = parseInt(days, 10) || 30;
      const res = await fetch('/api/nps/stats?days=' + d, { headers });
      if (!res.ok) {
        if (scoreEl) scoreEl.textContent = '—';
        return;
      }
      const data = await res.json();
      renderNPSStats(data);
    } catch (e) {
      console.error('nps: stats error', e);
      if (scoreEl) scoreEl.textContent = '—';
    }
  }

  // ── Reviews render ────────────────────────────────────────────────

  function starsHtml(rating) {
    const r = Math.round(rating || 0);
    let s = '';
    for (let i = 1; i <= 5; i++) {
      s += i <= r ? '★' : '<span class="off">★</span>';
    }
    return s;
  }

  function sourceChip(source) {
    const map = { google: 'source-google', tripadvisor: 'source-tripadvisor', whatsapp: 'source-wa', internal: 'source-internal' };
    const cls = map[(source || '').toLowerCase()] || 'source-internal';
    const label = cls === 'source-internal' ? 'Encuesta' : source;
    return '<span class="source-chip ' + cls + '">' + _escHtml(label) + '</span>';
  }

  function renderReviews(reviews) {
    const feed = document.getElementById('reviews-feed');
    if (!feed) return;

    if (!reviews || !reviews.length) {
      feed.innerHTML = '<div style="padding:24px;color:var(--text-3);">Sin datos suficientes aún</div>';
      feed.setAttribute('data-loaded', 'true');
      return;
    }

    feed.innerHTML = reviews.map(function (rev) {
      const name = rev.customer_name || 'Cliente';
      const initials = name.split(' ').map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase();
      const score = rev.score || rev.rating || 0;
      const comment = rev.comment || rev.review_text || '';
      const date = typeof mesioDate === 'function' ? mesioDate(rev.created_at || '') : (rev.created_at || '');
      const stars = parseInt(score, 10);

      return '<div class="review" data-stars="' + stars + '" data-review-id="' + rev.id + '">' +
        '<div class="rev-avatar" style="background:var(--brand-light);color:var(--brand);">' + _escHtml(initials) + '</div>' +
        '<div style="flex:1;">' +
        '<div class="rev-name">' + _escHtml(name) + '</div>' +
        '<div class="rev-sub">' +
        sourceChip(rev.source || 'internal') +
        '<span class="stars">' + starsHtml(score) + '</span>' +
        '<span>· ' + _escHtml(date) + '</span>' +
        '</div>' +
        '<div class="rev-body">' + _escHtml(comment) + '</div>' +
        '</div>' +
        (stars <= 3 ? '<span class="badge danger">' + stars + '★</span>' : '') +
        '</div>';
    }).join('');

    feed.setAttribute('data-loaded', 'true');
  }

  async function loadReviews() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/nps/responses?period=' + _feedPeriod + '&limit=100', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const reviews = data.responses || [];
      renderReviews(Array.isArray(reviews) ? reviews : []);
    } catch (e) {
      console.error('nps: responses error', e);
    }
  }

  loadNPS('30');
  loadReviews();

  // Auto-refresh every 30s
  if (typeof mesioInterval === 'function') {
    mesioInterval(loadReviews, 30000);
  }
})();
