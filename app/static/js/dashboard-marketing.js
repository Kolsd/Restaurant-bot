/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Marketing / Clientes en Riesgo
   app/static/js/dashboard-marketing.js
═══════════════════════════════════════════════════ */

// ── State ─────────────────────────────────────────────────────────────────────

let _mktStatus       = null;   // last fetched status dict
let _reengagePhone   = null;   // phone in open modal
let _mktLoaded       = false;  // guard: load once per section visit

// ── Load entry point (called from showSection) ────────────────────────────────

async function loadClientesSection() {
  if (_mktLoaded) return;   // already loaded this session
  _mktLoaded = true;
  await Promise.all([
    _loadMarketingStatus(),
    _loadAtRiskCustomers(),
    _loadMarketingHistory(),
  ]);
}

// ── Marketing status card ─────────────────────────────────────────────────────

async function _loadMarketingStatus() {
  try {
    const resp = await fetch('/api/marketing/status', { headers: mesioHeaders() });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    _mktStatus = await resp.json();
    _renderMarketingStatus(_mktStatus);
  } catch (err) {
    const card = document.getElementById('marketing-status-card');
    if (card) card.innerHTML = '<p style="color:#C0392B;font-size:13px;">No se pudo cargar el estado de marketing.</p>';
  }
}

function _renderMarketingStatus(s) {
  // Plan badge
  const badge = document.getElementById('mkt-plan-badge');
  if (badge) badge.textContent = s.plan || 'basic';

  // Toggle
  const toggle = document.getElementById('mkt-enabled-toggle');
  if (toggle) toggle.checked = s.enabled;

  // Progress bar
  const bar   = document.getElementById('mkt-progress-bar');
  const label = document.getElementById('mkt-progress-label');
  const warn  = document.getElementById('mkt-cap-warning');

  if (s.cap === null) {
    // Enterprise — unlimited
    if (bar)   { bar.style.width = '0%'; bar.style.background = '#1D9E75'; }
    if (label) label.textContent = s.sent_this_month + ' / Ilimitado';
    if (warn)  warn.style.display = 'none';
  } else {
    const pct = s.cap > 0 ? Math.min(100, Math.round((s.sent_this_month / s.cap) * 100)) : 0;
    if (bar) {
      bar.style.width = pct + '%';
      bar.style.background = pct >= 100 ? '#C0392B' : pct >= 80 ? '#e67e22' : '#1D9E75';
    }
    if (label) label.textContent = s.sent_this_month + ' / ' + s.cap;
    if (warn) {
      if (pct >= 100) {
        warn.textContent = 'Alcanzaste el limite mensual. Actualiza tu plan para seguir enviando.';
        warn.style.display = 'block';
      } else if (pct >= 80) {
        warn.textContent = 'Estas cerca del limite mensual (' + s.remaining + ' restantes).';
        warn.style.display = 'block';
      } else {
        warn.style.display = 'none';
      }
    }
  }

  // Cost estimate
  const costEl = document.getElementById('mkt-cost-estimate');
  if (costEl) {
    const costUsd = typeof s.cost_estimate_month_usd === 'number'
      ? s.cost_estimate_month_usd.toFixed(2)
      : '0.00';
    costEl.textContent = 'Costo estimado: $' + costUsd + ' USD';
  }
}

async function marketingToggleEnabled(enabled) {
  try {
    const resp = await fetch('/api/marketing/settings', {
      method: 'PATCH',
      headers: { ...mesioHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    mesioToast(enabled ? 'Marketing activado' : 'Marketing pausado', 'success');
    if (_mktStatus) {
      _mktStatus.enabled = enabled;
    }
  } catch (err) {
    mesioToast('No se pudo actualizar la configuracion', 'error');
    // Revert toggle visually
    const toggle = document.getElementById('mkt-enabled-toggle');
    if (toggle) toggle.checked = !enabled;
  }
}

// ── At-risk customers table ───────────────────────────────────────────────────

async function _loadAtRiskCustomers() {
  const container = document.getElementById('at-risk-table-body');
  if (!container) return;

  try {
    const resp = await fetch('/api/customers/at-risk?limit=50', { headers: mesioHeaders() });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    _renderAtRiskTable(data.customers || []);
  } catch (err) {
    container.innerHTML = '<p style="color:#C0392B;font-size:13px;padding:1rem;">Error cargando clientes en riesgo.</p>';
  }
}

function _renderAtRiskTable(customers) {
  const container = document.getElementById('at-risk-table-body');
  if (!container) return;

  if (!customers.length) {
    container.innerHTML = `
      <div style="text-align:center;padding:2.5rem;color:#888;">
        <div style="font-size:2rem;margin-bottom:8px;">&#127881;</div>
        <div style="font-size:14px;font-weight:600;color:#555;margin-bottom:4px;">Todos tus clientes frecuentes estan activos</div>
        <div style="font-size:12px;">No hay clientes con mas de 21 dias sin visitar.</div>
      </div>`;
    return;
  }

  // Table header
  let html = `
    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="border-bottom:1px solid #e0e0d8;color:#888;text-align:left;">
          <th style="padding:8px 10px;font-weight:600;">Cliente</th>
          <th style="padding:8px 10px;font-weight:600;">Ultima visita</th>
          <th style="padding:8px 10px;font-weight:600;">Pedidos</th>
          <th style="padding:8px 10px;font-weight:600;">Total gastado</th>
          <th style="padding:8px 10px;font-weight:600;"></th>
        </tr>
      </thead>
      <tbody>`;

  const isDisabled = _mktStatus && !_mktStatus.enabled;
  const isCapReached = _mktStatus && _mktStatus.cap !== null && _mktStatus.remaining === 0;
  const blocked = isDisabled || isCapReached;

  let tooltip = '';
  if (isDisabled) tooltip = 'Marketing pausado. Activalo en la tarjeta superior.';
  else if (isCapReached) tooltip = 'Limite mensual alcanzado. Actualiza tu plan.';

  customers.forEach(c => {
    const name  = c.customer_name || c.customer_phone;
    const days  = c.days_since || 0;
    const spent = typeof c.total_spent === 'number'
      ? new Intl.NumberFormat('es-CO', { style: 'currency', currency: 'COP', minimumFractionDigits: 0 }).format(c.total_spent)
      : '-';

    const daysColor = days >= 45 ? '#C0392B' : days >= 30 ? '#e67e22' : '#888';

    const btnStyle = blocked
      ? 'background:#e0e0d8;color:#aaa;cursor:not-allowed;border:none;padding:6px 14px;border-radius:8px;font-size:12px;font-weight:600;'
      : 'background:#1D9E75;color:#fff;border:none;padding:6px 14px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;';

    const btnAttrs = blocked
      ? `disabled title="${_escHtml(tooltip)}"`
      : `onclick="openReengagementModal('${_escHtml(c.customer_phone)}', '${_escHtml(name)}')"`;

    html += `
      <tr style="border-bottom:1px solid #f5f5f0;">
        <td style="padding:10px 10px;">
          <div style="font-weight:500;color:#222;">${_escHtml(name)}</div>
          <div style="font-size:11px;color:#aaa;margin-top:2px;">${_escHtml(c.customer_phone)}</div>
        </td>
        <td style="padding:10px 10px;color:${daysColor};font-weight:600;">${days} dias</td>
        <td style="padding:10px 10px;color:#555;">${c.total_orders}</td>
        <td style="padding:10px 10px;color:#555;">${spent}</td>
        <td style="padding:10px 10px;text-align:right;">
          <button style="${btnStyle}" ${btnAttrs}>Enviar saludo</button>
        </td>
      </tr>`;
  });

  html += '</tbody></table></div>';
  container.innerHTML = html;
}

// ── Marketing history table ───────────────────────────────────────────────────

async function _loadMarketingHistory() {
  const container = document.getElementById('marketing-history-body');
  if (!container) return;

  try {
    const resp = await fetch('/api/marketing/history?limit=50', { headers: mesioHeaders() });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    _renderHistoryTable(data.messages || []);
  } catch (err) {
    container.innerHTML = '<p style="color:#C0392B;font-size:13px;">Error cargando historial.</p>';
  }
}

function _renderHistoryTable(messages) {
  const container = document.getElementById('marketing-history-body');
  if (!container) return;

  if (!messages.length) {
    container.innerHTML = '<p style="color:#aaa;font-size:13px;text-align:center;padding:1rem;">Sin envios registrados.</p>';
    return;
  }

  const statusColors = {
    sent:             { bg: '#e8f5f0', text: '#1D9E75', label: 'Enviado' },
    failed:           { bg: '#FDE8E8', text: '#C0392B', label: 'Fallido' },
    blocked_cap:      { bg: '#fef3cd', text: '#856404', label: 'Cap alcanzado' },
    blocked_disabled: { bg: '#f0f0e8', text: '#888',    label: 'Desactivado' },
  };

  let html = `
    <table style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead>
        <tr style="border-bottom:1px solid #e0e0d8;color:#888;text-align:left;">
          <th style="padding:6px 8px;font-weight:600;">Fecha</th>
          <th style="padding:6px 8px;font-weight:600;">Cliente</th>
          <th style="padding:6px 8px;font-weight:600;">Estado</th>
          <th style="padding:6px 8px;font-weight:600;">Costo</th>
        </tr>
      </thead>
      <tbody>`;

  messages.forEach(m => {
    const sc    = statusColors[m.status] || { bg: '#f0f0e8', text: '#555', label: m.status };
    const date  = m.sent_at ? new Date(m.sent_at).toLocaleString('es-CO', { dateStyle: 'short', timeStyle: 'short' }) : '-';
    const cost  = typeof m.cost_estimate_usd === 'number' ? '$' + m.cost_estimate_usd.toFixed(4) : '-';

    html += `
      <tr style="border-bottom:1px solid #f5f5f0;">
        <td style="padding:7px 8px;color:#555;">${_escHtml(date)}</td>
        <td style="padding:7px 8px;color:#333;">${_escHtml(m.customer_phone)}</td>
        <td style="padding:7px 8px;">
          <span style="background:${sc.bg};color:${sc.text};padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;">${sc.label}</span>
        </td>
        <td style="padding:7px 8px;color:#888;">${cost}</td>
      </tr>`;
  });

  html += '</tbody></table>';
  container.innerHTML = html;
}

// ── Reengagement modal ────────────────────────────────────────────────────────

function openReengagementModal(phone, name) {
  _reengagePhone = phone;

  const overlay  = document.getElementById('reengagement-modal-overlay');
  const phoneEl  = document.getElementById('reengagement-modal-phone');
  const msgEl    = document.getElementById('reengagement-modal-message');
  const countEl  = document.getElementById('reengagement-char-count');

  if (!overlay || !phoneEl || !msgEl) return;

  const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
  const restName   = restaurant.name || 'tu restaurante';

  phoneEl.textContent = name + ' (' + phone + ')';

  const defaultMsg = 'Hola \uD83D\uDC4B Te extranjamos en ' + restName + '. \u00BFVolvemos a verte pronto? \uD83C\uDF7D\uFE0F';
  msgEl.value = defaultMsg;
  if (countEl) countEl.textContent = defaultMsg.length + ' / 1024';

  msgEl.oninput = () => {
    if (countEl) countEl.textContent = msgEl.value.length + ' / 1024';
  };

  overlay.style.display = 'flex';
}

function closeReengagementModal() {
  const overlay = document.getElementById('reengagement-modal-overlay');
  if (overlay) overlay.style.display = 'none';
  _reengagePhone = null;
}

async function sendReengagement() {
  if (!_reengagePhone) return;

  const msgEl  = document.getElementById('reengagement-modal-message');
  const sendBtn = document.getElementById('reengagement-send-btn');
  const message = msgEl ? msgEl.value.trim() : '';

  if (!message) {
    mesioToast('Escribe un mensaje antes de enviar', 'error');
    return;
  }

  if (sendBtn) {
    sendBtn.disabled = true;
    sendBtn.textContent = 'Enviando...';
  }

  try {
    const resp = await fetch('/api/marketing/send-reengagement', {
      method: 'POST',
      headers: { ...mesioHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ customer_phone: _reengagePhone, message }),
    });

    const data = await resp.json();

    if (!resp.ok) {
      const detail = data.detail || data;
      const msg    = (typeof detail === 'object' ? detail.message : detail) || 'Error al enviar';
      mesioToast(msg, 'error');
      return;
    }

    mesioToast('Mensaje enviado correctamente', 'success');
    closeReengagementModal();

    // Update status counts and re-render
    if (_mktStatus && data.remaining !== undefined) {
      _mktStatus.sent_this_month = (_mktStatus.cap !== null)
        ? _mktStatus.cap - (data.remaining || 0)
        : (_mktStatus.sent_this_month || 0) + 1;
      _mktStatus.remaining = data.remaining;
      _renderMarketingStatus(_mktStatus);
    }

    // Remove customer from at-risk list
    const rows = document.querySelectorAll('#at-risk-table-body tbody tr');
    rows.forEach(row => {
      if (row.textContent.includes(_reengagePhone || '')) {
        row.style.transition = 'opacity 0.3s';
        row.style.opacity = '0';
        setTimeout(() => row.remove(), 300);
      }
    });

    // Refresh history
    _loadMarketingHistory();

  } catch (err) {
    mesioToast('Error de red al enviar el mensaje', 'error');
  } finally {
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.textContent = 'Enviar mensaje';
    }
  }
}

// Close modal on overlay click
document.addEventListener('DOMContentLoaded', () => {
  const overlay = document.getElementById('reengagement-modal-overlay');
  if (overlay) {
    overlay.addEventListener('click', e => {
      if (e.target === overlay) closeReengagementModal();
    });
  }
});
