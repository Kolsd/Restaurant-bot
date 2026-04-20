/* ── NPS & Reseñas page ──────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  let _restaurantName = '';
  try {
    const rb = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    _restaurantName = rb.name || rb.restaurant_name || '';
  } catch (_) {}

  // Period filter
  document.querySelectorAll('.page-head .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.page-head .seg-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      const days = btn.dataset.days || '30';
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
      const status = rev.dataset.status || '';
      const visible = filter === 'pending' || filter === 'Pendientes' ? status === 'pending'
        : filter === 'urgent' || filter === 'Urgentes' ? status === 'urgent'
        : filter === '5star' || filter === '5★' ? rev.dataset.stars === '5'
        : filter === 'low' || filter === '1-3★' ? parseInt(rev.dataset.stars || '5', 10) <= 3
        : true;
      rev.style.display = visible ? '' : 'none';
    });
  }

  // Send survey button
  const surveyBtn = document.getElementById('btn-send-survey');
  if (surveyBtn) {
    surveyBtn.addEventListener('click', function () {
      if (typeof mesioToast === 'function') {
        mesioToast('Envío de encuesta próximamente disponible', 'info');
      }
    });
  }

  // ── NPS stats render ──────────────────────────────────────────────

  function renderNPSStats(data) {
    if (!data) return;
    const scoreEl = document.getElementById('nps-score');
    const promoEl = document.getElementById('nps-promoters');
    const passEl = document.getElementById('nps-passives');
    const detrEl = document.getElementById('nps-detractors');
    if (scoreEl && data.nps_score !== undefined) {
      scoreEl.textContent = Math.round(data.nps_score);
    }
    if (promoEl && data.promoters_pct !== undefined) {
      promoEl.textContent = Math.round(data.promoters_pct) + '% promo.';
    }
    if (passEl && data.passives_pct !== undefined) {
      passEl.textContent = Math.round(data.passives_pct) + '% pas.';
    }
    if (detrEl && data.detractors_pct !== undefined) {
      detrEl.textContent = Math.round(data.detractors_pct) + '% detr.';
    }
  }

  async function loadNPS(days) {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/nps/stats', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      renderNPSStats(data);
    } catch (e) {
      console.error('nps: stats error', e);
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
    return '<span class="source-chip ' + cls + '">' + _escHtml(source || 'Encuesta') + '</span>';
  }

  function reviewStatusBadge(rev) {
    if (rev.owner_reply) return '<span class="badge success">Respondida</span>';
    if (rev.score <= 3) return '<span class="badge danger">Urgente · ' + rev.score + '★</span>';
    return '<span class="badge warn">Pendiente</span>';
  }

  function reviewStatus(rev) {
    if (rev.owner_reply) return 'responded';
    if (rev.score <= 3) return 'urgent';
    return 'pending';
  }

  function renderReplyBlock(rev) {
    if (rev.owner_reply) {
      return '<div class="rev-reply">' +
        '<div class="rev-reply-head">↳ Respuesta del restaurante</div>' +
        _escHtml(rev.owner_reply) +
        '</div>';
    }
    // Show action buttons for pending/urgent
    const idAttr = 'data-review-id="' + rev.id + '"';
    const comment = _escHtml(rev.comment || rev.review_text || '');
    const score = rev.score || 0;
    return '<div class="rev-actions" data-review-id="' + rev.id + '">' +
      '<button class="btn sm primary" data-action="suggest" ' + idAttr + ' data-score="' + score + '" data-comment="' + comment + '">✨ Sugerir respuesta</button>' +
      '<textarea class="rev-reply-textarea" placeholder="Escribe tu respuesta…" style="display:none;width:100%;margin-top:8px;padding:6px;border:1px solid var(--border);border-radius:6px;font-size:13px;resize:vertical;min-height:60px;"></textarea>' +
      '<div style="display:none;gap:6px;margin-top:6px;" class="rev-reply-send-row">' +
      '<button class="btn sm primary" data-action="save-reply" ' + idAttr + '>Responder</button>' +
      '<button class="btn sm ghost" data-action="publish" ' + idAttr + '>Publicar</button>' +
      '</div>' +
      '</div>';
  }

  function renderReviews(reviews) {
    const feed = document.getElementById('reviews-feed');
    if (!feed) return;

    if (!reviews || !reviews.length) {
      feed.innerHTML = '<div style="padding:24px;color:var(--text-3);">(sin reseñas)</div>';
      feed.setAttribute('data-loaded', 'true');
      return;
    }

    feed.innerHTML = reviews.map(function (rev) {
      const name = rev.customer_name || 'Cliente';
      const initials = name.split(' ').map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase();
      const score = rev.score || rev.rating || 0;
      const comment = rev.comment || rev.review_text || '';
      const date = typeof mesioDate === 'function' ? mesioDate(rev.created_at || '') : (rev.created_at || '');
      const status = reviewStatus(rev);
      const stars = parseInt(score, 10);

      return '<div class="review" data-status="' + status + '" data-stars="' + stars + '" data-review-id="' + rev.id + '">' +
        '<div class="rev-avatar" style="background:var(--brand-light);color:var(--brand);">' + _escHtml(initials) + '</div>' +
        '<div style="flex:1;">' +
        '<div class="rev-name">' + _escHtml(name) + '</div>' +
        '<div class="rev-sub">' +
        sourceChip(rev.source || 'internal') +
        '<span class="stars">' + starsHtml(score) + '</span>' +
        '<span>· ' + _escHtml(date) + '</span>' +
        '</div>' +
        '<div class="rev-body">' + _escHtml(comment) + '</div>' +
        renderReplyBlock(rev) +
        '</div>' +
        reviewStatusBadge(rev) +
        '</div>';
    }).join('');

    feed.setAttribute('data-loaded', 'true');
    bindReviewActions(feed);
  }

  // ── Review action wiring (Task 3) ─────────────────────────────────

  function bindReviewActions(container) {
    container.querySelectorAll('[data-action]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        const action = btn.dataset.action;
        const id = btn.dataset.reviewId;
        const actionsBlock = btn.closest('[data-review-id]');
        if (action === 'suggest') suggestReply(btn, actionsBlock);
        else if (action === 'save-reply') saveReply(id, actionsBlock);
        else if (action === 'publish') publishReview(id, btn);
      });
    });
  }

  async function suggestReply(btn, actionsBlock) {
    const id = btn.dataset.reviewId;
    const score = btn.dataset.score || '?';
    const comment = btn.dataset.comment || '';
    const prompt = 'Tipo: review_reply\nRating: ' + score + '/10\nComentario: ' + comment +
      '\nRestaurante: ' + _restaurantName;

    if (typeof mesioToast === 'function') mesioToast('Generando respuesta con IA…', 'info', 2000);

    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/ai/proxy', {
        method: 'POST', headers,
        body: JSON.stringify({ prompt: prompt, max_tokens: 300 })
      });
      if (!res.ok) throw new Error('status ' + res.status);
      const data = await res.json();
      const text = data.text || data.content || data.reply || '';

      // Show textarea with suggestion
      const textarea = actionsBlock ? actionsBlock.querySelector('.rev-reply-textarea') : null;
      const sendRow = actionsBlock ? actionsBlock.querySelector('.rev-reply-send-row') : null;
      if (textarea) {
        textarea.value = text;
        textarea.style.display = '';
      }
      if (sendRow) sendRow.style.display = 'flex';
    } catch (e) {
      console.error('nps: suggest error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al generar respuesta IA', 'error');
    }
  }

  async function saveReply(id, actionsBlock) {
    const textarea = actionsBlock ? actionsBlock.querySelector('.rev-reply-textarea') : null;
    const reply = textarea ? textarea.value.trim() : '';
    if (!reply) {
      if (typeof mesioToast === 'function') mesioToast('Escribe una respuesta primero', 'warning');
      return;
    }
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/reviews/' + id + '/reply', {
        method: 'PUT', headers,
        body: JSON.stringify({ reply: reply })
      });
      if (!res.ok) throw new Error('status ' + res.status);
      if (typeof mesioToast === 'function') mesioToast('Respuesta guardada', 'success');
      // Update DOM inline
      const reviewEl = document.querySelector('.review[data-review-id="' + id + '"]');
      if (reviewEl) {
        const body = reviewEl.querySelector('.rev-body');
        if (body) {
          const replyDiv = document.createElement('div');
          replyDiv.className = 'rev-reply';
          replyDiv.innerHTML = '<div class="rev-reply-head">↳ Tu respuesta</div>' + _escHtml(reply);
          body.insertAdjacentElement('afterend', replyDiv);
        }
        if (actionsBlock) actionsBlock.style.display = 'none';
        reviewEl.dataset.status = 'responded';
        const badge = reviewEl.querySelector('.badge:last-child');
        if (badge) { badge.className = 'badge success'; badge.textContent = 'Respondida'; }
      }
    } catch (e) {
      console.error('nps: save-reply error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al guardar respuesta', 'error');
    }
  }

  async function publishReview(id, btn) {
    try {
      const headers = Object.assign({ 'Content-Type': 'application/json' },
        typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token });
      const res = await fetch('/api/reviews/' + id + '/publish', {
        method: 'PUT', headers,
        body: JSON.stringify({ is_public: true })
      });
      if (!res.ok) throw new Error('status ' + res.status);
      if (typeof mesioToast === 'function') mesioToast('Reseña publicada', 'success');
      if (btn) btn.textContent = '✓ Publicada';
    } catch (e) {
      console.error('nps: publish error', e);
      if (typeof mesioToast === 'function') mesioToast('Error al publicar reseña', 'error');
    }
  }

  async function loadReviews() {
    try {
      const headers = typeof mesioHeaders === 'function' ? mesioHeaders() : { 'Authorization': 'Bearer ' + token };
      const res = await fetch('/api/reviews', { headers });
      if (!res.ok) { return; }
      const data = await res.json();
      const reviews = data.reviews || data;
      renderReviews(Array.isArray(reviews) ? reviews : []);
    } catch (e) {
      console.error('nps: reviews error', e);
    }
  }

  // Also bind existing static review action buttons (before dynamic load)
  bindReviewActions(document.getElementById('reviews-feed') || document.body);

  loadNPS('30d');
  loadReviews();

  // Auto-refresh every 30s
  if (typeof mesioInterval === 'function') {
    mesioInterval(loadReviews, 30000);
  }
})();
