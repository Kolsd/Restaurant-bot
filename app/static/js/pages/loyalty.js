/* ── Fidelización page ───────────────────────────────────────────── */
(function () {
  'use strict';

  const token = localStorage.getItem('rb_token');
  if (!token) { location.href = '/login'; return; }

  const fmt = typeof mesioFmt === 'function' ? mesioFmt : function (n) { return '$' + n; };

  // ── Aggregates ────────────────────────────────────────────────────

  function renderAggregates(data) {
    if (!data) return;
    const metrics = document.querySelectorAll('.metrics-row .metric-value');
    // Metric order in HTML: total_customers, avg_frequency, avg_ticket_member, roi_multiple, redemption_pct
    if (metrics[0] !== undefined && data.total_customers !== undefined) {
      metrics[0].textContent = Number(data.total_customers).toLocaleString();
    }
    if (metrics[1] !== undefined && data.avg_frequency !== undefined) {
      metrics[1].textContent = (data.avg_frequency !== null
        ? Number(data.avg_frequency).toFixed(1) + 'x'
        : '—');
    }
    if (metrics[2] !== undefined && data.avg_ticket_member !== undefined) {
      metrics[2].textContent = data.avg_ticket_member !== null
        ? fmt(data.avg_ticket_member)
        : '—';
    }
    if (metrics[3] !== undefined) {
      metrics[3].textContent = data.roi_multiple !== null && data.roi_multiple !== undefined
        ? Number(data.roi_multiple).toFixed(1) + 'x'
        : '—';
    }
    if (metrics[4] !== undefined && data.redemption_pct !== undefined) {
      metrics[4].textContent = data.redemption_pct !== null
        ? Number(data.redemption_pct).toFixed(1) + '%'
        : '—';
    }
  }

  async function loadAggregates() {
    try {
      const res = await fetch('/api/loyalty/aggregates', { headers: mesioHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      renderAggregates(data);
    } catch (e) {
      console.error('fidelizacion: aggregates error', e);
    }
  }

  // ── Segments ──────────────────────────────────────────────────────

  function renderSegments(segments) {
    if (!Array.isArray(segments)) return;
    segments.forEach(function (seg) {
      const card = document.querySelector('[data-segment="' + (seg.id || seg.segment_id) + '"]');
      if (!card) return;
      const countEl = card.querySelector('.mono');
      if (countEl) countEl.textContent = Number(seg.count).toLocaleString();
      const revenueEl = card.querySelector('.seg-revenue');
      if (revenueEl && seg.potential_revenue !== null && seg.potential_revenue !== undefined) {
        revenueEl.textContent = fmt(seg.potential_revenue);
      }
    });
  }

  async function loadSegments() {
    try {
      const res = await fetch('/api/loyalty/segments', { headers: mesioHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      renderSegments(data.segments || []);
    } catch (e) {
      console.error('fidelizacion: segments error', e);
    }
  }

  // ── Funnel ────────────────────────────────────────────────────────

  function renderFunnel(steps) {
    if (!Array.isArray(steps)) return;
    steps.forEach(function (step, i) {
      const idx = i + 1;
      const numEl = document.querySelector('.funnel-step:nth-child(' + idx + ') .funnel-num');
      const pctEl = document.querySelector('.funnel-step:nth-child(' + idx + ') .funnel-pct');
      const fillEl = document.querySelector('.funnel-step:nth-child(' + idx + ') .funnel-fill');
      if (numEl) numEl.textContent = Number(step.count).toLocaleString();
      if (pctEl) pctEl.textContent = step.pct !== null && step.pct !== undefined
        ? Number(step.pct).toFixed(0) + '%'
        : '—';
      if (fillEl) fillEl.style.width = (step.pct !== null && step.pct !== undefined
        ? Math.min(100, Math.max(0, Number(step.pct))) + '%'
        : '0%');
    });
  }

  async function loadFunnel() {
    try {
      const res = await fetch('/api/loyalty/funnel', { headers: mesioHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      renderFunnel(data.steps || []);
    } catch (e) {
      console.error('fidelizacion: funnel error', e);
    }
  }

  // ── Tier thresholds ───────────────────────────────────────────────

  function getTierThresholds() {
    try {
      const rest = localStorage.getItem('rb_restaurant');
      if (rest) {
        const r = JSON.parse(rest);
        if (r && r.features && r.features.loyalty_tiers) return r.features.loyalty_tiers;
      }
    } catch (e) { /* ignore */ }
    return { bronce: [0, 20], plata: [21, 80], oro: [81, 200], platino: [201, Infinity] };
  }

  function computeTierCounts(customers) {
    const t = getTierThresholds();
    const counts = { bronce: 0, plata: 0, oro: 0, platino: 0 };
    customers.forEach(function (c) {
      const pts = c.points_balance || 0;
      if (pts >= t.platino[0]) counts.platino++;
      else if (pts >= t.oro[0]) counts.oro++;
      else if (pts >= t.plata[0]) counts.plata++;
      else counts.bronce++;
    });
    return counts;
  }

  function renderTierCounts(counts) {
    const bronce  = document.getElementById('tier-bronce-count');
    const plata   = document.getElementById('tier-plata-count');
    const oro     = document.getElementById('tier-oro-count');
    const platino = document.getElementById('tier-platino-count');
    if (bronce)  bronce.textContent  = counts.bronce.toLocaleString();
    if (plata)   plata.textContent   = counts.plata.toLocaleString();
    if (oro)     oro.textContent     = counts.oro.toLocaleString();
    if (platino) platino.textContent = counts.platino.toLocaleString();
  }

  // ── Loyalty customers ─────────────────────────────────────────────

  function renderLoyaltyCustomers(customers) {
    let container = document.getElementById('loyalty-customers-body');
    if (!container) {
      const cards = document.querySelectorAll('.card.flush');
      let targetCard = null;
      cards.forEach(function (c) {
        if (c.textContent.includes('Clientes') || c.textContent.includes('ranking')) {
          targetCard = c;
        }
      });
      if (!targetCard) return;
      container = document.createElement('div');
      container.id = 'loyalty-customers-body';
      targetCard.appendChild(container);
    }

    if (!customers || !customers.length) {
      container.innerHTML = '<div style="padding:18px;color:var(--text-3);font-size:13px;">(sin clientes de fidelización)</div>';
      return;
    }

    container.innerHTML = customers.slice(0, 20).map(function (c) {
      const name     = _escHtml(c.customer_name || c.name || c.phone || 'Cliente');
      const points   = c.points_balance || c.current_points || 0;
      const total    = c.total_spent || c.lifetime_value || 0;
      const tier     = _escHtml(c.tier || c.level || 'Bronce');
      const initials = (c.customer_name || c.name || 'C').split(' ')
        .map(function (w) { return w[0] || ''; }).slice(0, 2).join('').toUpperCase();
      return '<div style="display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:0.5px solid var(--border);">' +
        '<div style="width:32px;height:32px;border-radius:50%;background:var(--brand-light);color:var(--brand);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;flex-shrink:0;">' + _escHtml(initials) + '</div>' +
        '<div style="flex:1;">' +
          '<div style="font-weight:500;font-size:13px;">' + name + '</div>' +
          '<div style="font-size:11.5px;color:var(--text-3);">' + tier + ' · ' + points + ' pts</div>' +
        '</div>' +
        '<div style="font-size:12px;color:var(--text-2);">' + fmt(total) + '</div>' +
        '</div>';
    }).join('');
  }

  async function loadLoyaltyCustomers() {
    try {
      const res = await fetch('/api/loyalty/customers', { headers: mesioHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      const list = Array.isArray(data.customers) ? data.customers : (Array.isArray(data) ? data : []);
      renderLoyaltyCustomers(list);
      renderTierCounts(computeTierCounts(list));
    } catch (e) {
      console.error('fidelizacion: customers error', e);
      renderLoyaltyCustomers(null);
    }
  }

  // ── Campaigns ─────────────────────────────────────────────────────

  const STATUS_LABELS = { draft: 'Borrador', active: 'Activa', paused: 'Pausada' };
  const STATUS_BADGE  = { draft: 'muted', active: 'success', paused: 'warning' };

  function renderCampaigns(campaigns) {
    const container = document.getElementById('campaigns-list');
    if (!container) return;

    if (!campaigns || !campaigns.length) {
      container.innerHTML = '<div style="padding:18px;color:var(--text-3);font-size:13px;">(no hay campañas)</div>';
      return;
    }

    container.innerHTML = campaigns.map(function (c) {
      const statusLabel = STATUS_LABELS[c.status] || c.status;
      const badgeClass  = STATUS_BADGE[c.status] || 'muted';
      const canActivate = c.status === 'draft' || c.status === 'paused';
      const canPause    = c.status === 'active';
      return '<div class="camp-row" data-id="' + _escHtml(String(c.id)) + '" style="display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:0.5px solid var(--border);">' +
        '<div style="flex:1;">' +
          '<div style="font-weight:500;font-size:13px;">' + _escHtml(c.name) + '</div>' +
          '<div style="font-size:11.5px;color:var(--text-3);margin-top:2px;">' + _escHtml(c.trigger_type || '') + (c.segment ? ' · ' + _escHtml(c.segment) : '') + '</div>' +
        '</div>' +
        '<span class="badge ' + badgeClass + '">' + statusLabel + '</span>' +
        (canActivate ? '<button class="m-btn sm ghost camp-activate" data-id="' + _escHtml(String(c.id)) + '">Activar</button>' : '') +
        (canPause    ? '<button class="m-btn sm ghost camp-pause"    data-id="' + _escHtml(String(c.id)) + '">Pausar</button>'   : '') +
        '<button class="m-btn sm ghost camp-delete" data-id="' + _escHtml(String(c.id)) + '" style="color:var(--danger);">Eliminar</button>' +
        '</div>';
    }).join('');

    container.querySelectorAll('.camp-activate').forEach(function (btn) {
      btn.addEventListener('click', function () { toggleCampaignStatus(btn.dataset.id, 'active'); });
    });
    container.querySelectorAll('.camp-pause').forEach(function (btn) {
      btn.addEventListener('click', function () { toggleCampaignStatus(btn.dataset.id, 'paused'); });
    });
    container.querySelectorAll('.camp-delete').forEach(function (btn) {
      btn.addEventListener('click', function () { deleteCampaign(btn.dataset.id); });
    });
  }

  let _activeCampFilter = 'active';

  async function loadCampaigns(filter) {
    _activeCampFilter = filter || _activeCampFilter;
    try {
      const qs  = _activeCampFilter ? '?status=' + encodeURIComponent(_activeCampFilter) : '';
      const res = await fetch('/api/loyalty/campaigns' + qs, { headers: mesioHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      renderCampaigns(data.campaigns || []);
    } catch (e) {
      console.error('fidelizacion: campaigns error', e);
    }
  }

  async function toggleCampaignStatus(id, newStatus) {
    try {
      const res = await fetch('/api/loyalty/campaigns/' + encodeURIComponent(id) + '/toggle', {
        method:  'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
        body:    JSON.stringify({ status: newStatus }),
      });
      if (res.status === 409) {
        const err = await res.json().catch(function () { return {}; });
        mesioToast(err.detail || 'Transición no permitida', 'error');
        return;
      }
      if (!res.ok) {
        mesioToast('Error al cambiar estado', 'error');
        return;
      }
      mesioToast('Estado actualizado', 'success', 2000);
      loadCampaigns(_activeCampFilter);
    } catch (e) {
      console.error('fidelizacion: toggle error', e);
      mesioToast('Error de red', 'error');
    }
  }

  async function deleteCampaign(id) {
    const ok = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Eliminar esta campaña?')
      : window.confirm('¿Eliminar esta campaña?');
    if (!ok) return;
    try {
      const res = await fetch('/api/loyalty/campaigns/' + encodeURIComponent(id), {
        method:  'DELETE',
        headers: mesioHeaders(),
      });
      if (res.status === 204 || res.ok) {
        mesioToast('Campaña eliminada', 'success', 2000);
        loadCampaigns(_activeCampFilter);
      } else {
        mesioToast('Error al eliminar', 'error');
      }
    } catch (e) {
      console.error('fidelizacion: delete error', e);
      mesioToast('Error de red', 'error');
    }
  }

  // ── New campaign modal ────────────────────────────────────────────

  function openNewCampaignModal() {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML =
      '<div class="modal" style="max-width:480px;">' +
        '<div class="modal-header"><h3>Nueva campaña</h3><button class="modal-close" id="camp-modal-close">&#x2715;</button></div>' +
        '<div class="modal-body" style="display:flex;flex-direction:column;gap:14px;">' +
          '<div><label style="font-size:12px;color:var(--text-2);">Nombre</label>' +
            '<input id="camp-name" class="input" type="text" maxlength="200" placeholder="Ej. Reactivar VIP dormidos" style="width:100%;margin-top:4px;"></div>' +
          '<div><label style="font-size:12px;color:var(--text-2);">Tipo de disparo</label>' +
            '<select id="camp-trigger" class="input" style="width:100%;margin-top:4px;">' +
              '<option value="manual">Manual</option>' +
              '<option value="birthday">Cumpleaños</option>' +
              '<option value="inactivity_30d">Inactividad 30d</option>' +
              '<option value="first_purchase">Primera compra</option>' +
              '<option value="points_milestone">Hito de puntos</option>' +
            '</select></div>' +
          '<div><label style="font-size:12px;color:var(--text-2);">Segmento (opcional)</label>' +
            '<input id="camp-segment" class="input" type="text" maxlength="100" placeholder="vip-dormant, new-month, ..." style="width:100%;margin-top:4px;"></div>' +
          '<div><label style="font-size:12px;color:var(--text-2);">Mensaje WhatsApp</label>' +
            '<textarea id="camp-msg" class="input" maxlength="4096" rows="4" placeholder="Hola {{nombre}}, tienes {{puntos}} puntos..." style="width:100%;margin-top:4px;resize:vertical;"></textarea></div>' +
        '</div>' +
        '<div class="modal-footer" style="display:flex;gap:8px;justify-content:flex-end;">' +
          '<button class="m-btn ghost" id="camp-modal-cancel">Cancelar</button>' +
          '<button class="m-btn primary" id="camp-modal-save">Crear campaña</button>' +
        '</div>' +
      '</div>';

    document.body.appendChild(overlay);

    overlay.querySelector('#camp-modal-close').addEventListener('click', function () { overlay.remove(); });
    overlay.querySelector('#camp-modal-cancel').addEventListener('click', function () { overlay.remove(); });

    overlay.querySelector('#camp-modal-save').addEventListener('click', async function () {
      const name     = (overlay.querySelector('#camp-name').value || '').trim();
      const trigger  = overlay.querySelector('#camp-trigger').value;
      const segment  = (overlay.querySelector('#camp-segment').value || '').trim() || null;
      const template = (overlay.querySelector('#camp-msg').value || '').trim();

      if (!name) { mesioToast('El nombre es requerido', 'error'); return; }
      if (!template) { mesioToast('El mensaje es requerido', 'error'); return; }

      const btn = overlay.querySelector('#camp-modal-save');
      btn.disabled = true;
      btn.textContent = 'Creando...';

      try {
        const res = await fetch('/api/loyalty/campaigns', {
          method:  'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, mesioHeaders()),
          body:    JSON.stringify({
            name:             name,
            trigger_type:     trigger,
            segment:          segment,
            message_template: template,
            description:      '',
          }),
        });
        if (res.status === 201 || res.ok) {
          mesioToast('Campaña creada', 'success', 2500);
          overlay.remove();
          loadCampaigns('draft');
          // Switch filter tab to draft
          document.querySelectorAll('.seg-btn[data-camp-filter]').forEach(function (b) {
            b.classList.toggle('active', b.dataset.campFilter === 'draft');
          });
          _activeCampFilter = 'draft';
        } else {
          const err = await res.json().catch(function () { return {}; });
          mesioToast(err.detail || 'Error al crear campaña', 'error');
          btn.disabled = false;
          btn.textContent = 'Crear campaña';
        }
      } catch (e) {
        console.error('fidelizacion: create campaign error', e);
        mesioToast('Error de red', 'error');
        btn.disabled = false;
        btn.textContent = 'Crear campaña';
      }
    });
  }

  // ── Campaign filter buttons ───────────────────────────────────────

  document.querySelectorAll('.seg-btn[data-camp-filter]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.seg-btn[data-camp-filter]').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      loadCampaigns(btn.dataset.campFilter);
    });
  });

  // ── New campaign button ───────────────────────────────────────────

  const newCampBtn = document.getElementById('btn-new-campaign');
  if (newCampBtn) {
    newCampBtn.addEventListener('click', openNewCampaignModal);
  }

  // ── Configure program button ──────────────────────────────────────

  const configBtn = document.getElementById('btn-configure');
  if (configBtn) {
    configBtn.addEventListener('click', function () {
      mesioToast('Configuración de programa disponible próximamente', 'info');
    });
  }

  // ── Segment cards (drill-down) ────────────────────────────────────

  document.querySelectorAll('.seg-card').forEach(function (card) {
    card.addEventListener('click', function () {
      mesioToast('Detalle de segmento disponible próximamente', 'info');
    });
  });

  // ── Ver todos segments ────────────────────────────────────────────

  const viewAllBtn = document.getElementById('btn-view-all-segments');
  if (viewAllBtn) {
    viewAllBtn.addEventListener('click', function () {
      mesioToast('Vista completa de segmentos disponible próximamente', 'info');
    });
  }

  // ── Initial data load ─────────────────────────────────────────────

  loadAggregates();
  loadLoyaltyCustomers();
  loadSegments();
  loadFunnel();
  loadCampaigns('active');
})();
