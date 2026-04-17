/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Weekly Report Section
   app/static/js/dashboard-weekly-report.js
   Depends on: mesio-utils.js, dashboard-core.js
═══════════════════════════════════════════════════ */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
const _wr = {
  settings: { enabled: true, owner_phone: null, timezone: 'America/Bogota' },
  reports: [],
  saving: false,
  loaded: false,
};

// ── Constants ─────────────────────────────────────────────────────────────────
const _WR_TIMEZONES = [
  { value: 'America/Bogota',        label: 'América/Bogotá (COT, UTC−5)' },
  { value: 'America/Mexico_City',   label: 'América/Ciudad de México (CST, UTC−6)' },
  { value: 'America/Buenos_Aires',  label: 'América/Buenos Aires (ART, UTC−3)' },
  { value: 'America/Santiago',      label: 'América/Santiago (CLT, UTC−3/−4)' },
  { value: 'America/Lima',          label: 'América/Lima (PET, UTC−5)' },
  { value: 'Europe/Madrid',         label: 'Europa/Madrid (CET, UTC+1/+2)' },
  { value: 'America/New_York',      label: 'América/Nueva York (ET, UTC−5/−4)' },
];

const _PHONE_RE = /^\+?\d{10,15}$/;

// ── Entry point (called by showSection in dashboard-core.js) ──────────────────
window.loadWeeklyReportSection = async function (forceRefresh = false) {
  if (_wr.loaded && !forceRefresh) return;
  _wr.loaded = false;
  _wrRenderSkeleton();
  try {
    const resp = await fetch('/api/weekly-reports', { headers: mesioHeaders() });
    mesioTrackFetch(resp.ok);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    _wr.settings = data.settings || _wr.settings;
    _wr.reports  = data.reports  || [];
    _wr.loaded   = true;
    _wrRender();
  } catch (e) {
    _wrRenderError();
  }
};

// ── Render helpers ─────────────────────────────────────────────────────────────

function _wrRenderSkeleton() {
  const el = document.getElementById('weekly-report-component');
  if (!el) return;
  el.innerHTML = `
    <div class="card" style="padding:1.5rem;">
      <div class="skeleton" style="height:20px;width:40%;border-radius:8px;margin-bottom:12px;background:#e0e0d8;animation:pulse 1.4s ease-in-out infinite;"></div>
      <div class="skeleton" style="height:14px;width:70%;border-radius:8px;margin-bottom:8px;background:#e0e0d8;animation:pulse 1.4s ease-in-out infinite;"></div>
      <div class="skeleton" style="height:14px;width:55%;border-radius:8px;background:#e0e0d8;animation:pulse 1.4s ease-in-out infinite;"></div>
    </div>`;
}

function _wrRenderError() {
  const el = document.getElementById('weekly-report-component');
  if (!el) return;
  el.innerHTML = `<div class="empty-state" style="padding:2rem;text-align:center;color:#ef4444;">
    Error al cargar el reporte semanal. Intenta recargar la página.
  </div>`;
}

function _wrRender() {
  const el = document.getElementById('weekly-report-component');
  if (!el) return;

  // ── Settings card ──────────────────────────────────────────────────────────
  const s = _wr.settings;
  const tzOptions = _WR_TIMEZONES.map(tz => {
    const sel = tz.value === s.timezone ? ' selected' : '';
    // textContent via DOM, safe — no user data in option labels
    return `<option value="${_escHtml(tz.value)}"${sel}>${_escHtml(tz.label)}</option>`;
  }).join('');

  const checkedAttr = s.enabled ? 'checked' : '';
  const phoneVal    = _escHtml(s.owner_phone || '');

  const settingsHtml = `
    <div class="card" style="margin-bottom:1.25rem;">
      <div style="font-size:16px;font-weight:600;margin-bottom:4px;">Reporte Semanal por WhatsApp</div>
      <div style="font-size:13px;color:#888;margin-bottom:1.25rem;">
        Recibirás un resumen cada lunes a las 9am hora local con ventas, pedidos, NPS y clientes dormidos.
      </div>

      <!-- Toggle enabled -->
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:1.25rem;">
        <label class="wr-toggle" aria-label="Activar reporte semanal">
          <input type="checkbox" id="wr-enabled" ${checkedAttr} onchange="wrHandleToggle(this.checked)">
          <span class="wr-track"><span class="wr-thumb"></span></span>
        </label>
        <div>
          <div style="font-size:14px;font-weight:500;" id="wr-enabled-label">${s.enabled ? 'Reporte semanal activo' : 'Reporte semanal desactivado'}</div>
          <div style="font-size:12px;color:#aaa;">Se envía cada lunes a las 9am (hora local configurada)</div>
        </div>
      </div>

      <!-- Phone input -->
      <div style="margin-bottom:1rem;">
        <label style="font-size:12px;color:#888;display:block;margin-bottom:4px;" for="wr-phone">
          Teléfono del dueño (E.164)
        </label>
        <input
          id="wr-phone"
          type="tel"
          value="${phoneVal}"
          placeholder="+573001234567"
          autocomplete="tel"
          oninput="wrValidatePhone(this)"
          style="width:100%;padding:9px 12px;border:1px solid #e0e0d8;border-radius:8px;font-size:13px;outline:none;box-sizing:border-box;"
        >
        <div id="wr-phone-error" style="display:none;font-size:12px;color:#ef4444;margin-top:4px;">
          Formato inválido. Usa E.164: +573001234567 (10–15 dígitos, opcional prefijo +)
        </div>
      </div>

      <!-- Timezone selector -->
      <div style="margin-bottom:1.5rem;">
        <label style="font-size:12px;color:#888;display:block;margin-bottom:4px;" for="wr-timezone">
          Zona horaria del reporte
        </label>
        <select id="wr-timezone" style="width:100%;padding:9px 12px;border:1px solid #e0e0d8;border-radius:8px;font-size:13px;outline:none;background:#fff;cursor:pointer;">
          ${tzOptions}
        </select>
      </div>

      <!-- Save button -->
      <div style="display:flex;align-items:center;gap:12px;">
        <button
          id="wr-save-btn"
          onclick="wrSaveSettings()"
          style="background:#1D9E75;color:#fff;border:none;padding:10px 24px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity 0.2s;"
        >
          Guardar configuración
        </button>
        <span id="wr-save-status" style="font-size:12px;color:#888;"></span>
      </div>
    </div>`;

  // ── Reports history card ──────────────────────────────────────────────────
  let historyContent;
  if (!_wr.reports.length) {
    historyContent = `
      <div style="text-align:center;padding:2.5rem 1rem;color:#aaa;">
        <div style="font-size:2rem;margin-bottom:.75rem;">📭</div>
        <div style="font-size:14px;font-weight:500;color:#666;margin-bottom:4px;">
          Aún no has recibido ningún reporte
        </div>
        <div style="font-size:12px;">Te llegará el próximo lunes.</div>
      </div>`;
  } else {
    const rows = _wr.reports.map(r => _wrReportRow(r)).join('');
    historyContent = `
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead>
            <tr style="border-bottom:1.5px solid #e0e0d8;text-align:left;">
              <th style="padding:8px 12px;color:#888;font-weight:600;white-space:nowrap;">Semana</th>
              <th style="padding:8px 12px;color:#888;font-weight:600;">Estado</th>
              <th style="padding:8px 12px;color:#888;font-weight:600;white-space:nowrap;">Enviado</th>
              <th style="padding:8px 12px;color:#888;font-weight:600;text-align:right;"></th>
            </tr>
          </thead>
          <tbody id="wr-history-body">
            ${rows}
          </tbody>
        </table>
      </div>`;
  }

  const historyHtml = `
    <div class="card">
      <div style="font-size:15px;font-weight:600;margin-bottom:1rem;">Historial de reportes enviados</div>
      ${historyContent}
    </div>`;

  el.innerHTML = settingsHtml + historyHtml;
  // Set week/sent cells via textContent (XSS safety — no user data in innerHTML)
  requestAnimationFrame(_wrFillRowText);
}

// ── Report table row ──────────────────────────────────────────────────────────
function _wrReportRow(r) {
  const weekLabel = _wrFormatWeek(r.week_start);
  const badgeHtml = _wrStatusBadge(r.delivery_status, r.error_message);
  const sentAt    = r.sent_at ? _wrFormatDateTime(r.sent_at) : '—';

  // "Ver mensaje" button — only if payload has message text accessible
  // We store a generated_at but message_preview isn't in the list shape;
  // we show a button that triggers a fetch of the preview via the modal.
  const viewBtn = `
    <button
      onclick="wrOpenPreviewModal(${r.id})"
      style="font-size:12px;padding:5px 12px;background:#f0fdf7;color:#0F6E56;border:1px solid #1D9E75;border-radius:6px;cursor:pointer;white-space:nowrap;"
    >
      Ver mensaje
    </button>`;

  // Week and sent-at cells are left empty here; _wrFillRowText sets them via
  // textContent after innerHTML insertion (XSS safety convention).
  return `
    <tr style="border-bottom:0.5px solid #e0e0d8;">
      <td style="padding:10px 12px;white-space:nowrap;font-weight:500;" id="wr-week-${r.id}"></td>
      <td style="padding:10px 12px;">${badgeHtml}</td>
      <td style="padding:10px 12px;color:#666;white-space:nowrap;" id="wr-sent-${r.id}"></td>
      <td style="padding:10px 12px;text-align:right;">${viewBtn}</td>
    </tr>`;
}

// Fill text content of rows safely after DOM insertion (called by _wrRender)
function _wrFillRowText() {
  (_wr.reports || []).forEach(r => {
    const weekCell = document.getElementById('wr-week-' + r.id);
    const sentCell = document.getElementById('wr-sent-' + r.id);
    if (weekCell) weekCell.textContent = _wrFormatWeek(r.week_start);
    if (sentCell) sentCell.textContent = r.sent_at ? _wrFormatDateTime(r.sent_at) : '—';
  });
}

// ── Status badge ──────────────────────────────────────────────────────────────
function _wrStatusBadge(status, errorMessage) {
  // errorMessage is used in title — set via textContent trick via data attribute
  const errData = errorMessage ? ` data-err="${_escHtml(errorMessage)}"` : '';
  switch (status) {
    case 'sent':
      return `<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 10px;background:#dcfce7;color:#15803d;border-radius:20px;font-size:12px;font-weight:600;">Enviado</span>`;
    case 'pending':
      return `<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 10px;background:#fef9c3;color:#854d0e;border-radius:20px;font-size:12px;font-weight:600;">Pendiente</span>`;
    case 'failed':
      return `<span${errData} style="display:inline-flex;align-items:center;gap:4px;padding:3px 10px;background:#fee2e2;color:#dc2626;border-radius:20px;font-size:12px;font-weight:600;cursor:default;" class="wr-badge-failed">Falló</span>`;
    default:
      return `<span style="padding:3px 10px;background:#f3f4f6;color:#6b7280;border-radius:20px;font-size:12px;">${_escHtml(status || '—')}</span>`;
  }
}

// ── Date helpers ──────────────────────────────────────────────────────────────
const _MONTHS_SHORT_ES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];

function _wrFormatWeek(weekStartIso) {
  if (!weekStartIso) return '—';
  try {
    // week_start is a date string like "2026-03-16"
    const [year, month, day] = weekStartIso.split('-').map(Number);
    const start = new Date(year, month - 1, day);
    const end   = new Date(year, month - 1, day + 6);
    const startDay  = start.getDate();
    const endDay    = end.getDate();
    const endMonth  = _MONTHS_SHORT_ES[end.getMonth()];
    const startMonth = _MONTHS_SHORT_ES[start.getMonth()];
    if (start.getMonth() === end.getMonth()) {
      return `${startDay}–${endDay} ${endMonth}`;
    }
    return `${startDay} ${startMonth} – ${endDay} ${endMonth}`;
  } catch {
    return weekStartIso;
  }
}

function _wrFormatDateTime(isoStr) {
  if (!isoStr) return '—';
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('es-CO', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  } catch {
    return isoStr;
  }
}

// ── Toggle handler ────────────────────────────────────────────────────────────
window.wrHandleToggle = function (checked) {
  _wr.settings.enabled = checked;
  const label = document.getElementById('wr-enabled-label');
  if (label) label.textContent = checked ? 'Reporte semanal activo' : 'Reporte semanal desactivado';
};

// ── Phone validation ──────────────────────────────────────────────────────────
window.wrValidatePhone = function (input) {
  const val = input.value.trim();
  const errEl = document.getElementById('wr-phone-error');
  const borderColor = '#e0e0d8';
  if (!val) {
    // Empty = clear phone, valid
    input.style.borderColor = borderColor;
    if (errEl) errEl.style.display = 'none';
    return true;
  }
  const valid = _PHONE_RE.test(val);
  input.style.borderColor = valid ? '#1D9E75' : '#ef4444';
  if (errEl) errEl.style.display = valid ? 'none' : 'block';
  return valid;
};

// ── Save settings ─────────────────────────────────────────────────────────────
window.wrSaveSettings = async function () {
  if (_wr.saving) return;

  const enabledEl  = document.getElementById('wr-enabled');
  const phoneEl    = document.getElementById('wr-phone');
  const tzEl       = document.getElementById('wr-timezone');
  const saveBtn    = document.getElementById('wr-save-btn');
  const saveStatus = document.getElementById('wr-save-status');

  // Validate phone before submitting
  if (phoneEl && !wrValidatePhone(phoneEl)) {
    mesioToast('Corrige el teléfono antes de guardar', 'error');
    return;
  }

  const body = {};
  if (enabledEl)  body.enabled      = enabledEl.checked;
  if (phoneEl)    body.owner_phone  = phoneEl.value.trim() || null;
  if (tzEl)       body.timezone     = tzEl.value;

  _wr.saving = true;
  if (saveBtn)    { saveBtn.disabled = true; saveBtn.style.opacity = '0.6'; }
  if (saveStatus) saveStatus.textContent = 'Guardando...';

  try {
    const resp = await fetch('/api/weekly-reports/settings', {
      method:  'PATCH',
      headers: mesioHeaders(),
      body:    JSON.stringify(body),
    });
    mesioTrackFetch(resp.ok);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      const msg = (typeof err.detail === 'string') ? err.detail : 'Error al guardar';
      mesioToast(msg, 'error');
      if (saveStatus) saveStatus.textContent = '';
      return;
    }
    const updated = await resp.json();
    _wr.settings = updated;
    mesioToast('Configuracion guardada', 'success');
    if (saveStatus) saveStatus.textContent = 'Guardado';
    setTimeout(() => { if (saveStatus) saveStatus.textContent = ''; }, 3000);
  } catch (e) {
    mesioToast('Error de conexion al guardar', 'error');
    if (saveStatus) saveStatus.textContent = '';
  } finally {
    _wr.saving = false;
    if (saveBtn) { saveBtn.disabled = false; saveBtn.style.opacity = '1'; }
  }
};

// ── Preview modal ─────────────────────────────────────────────────────────────
window.wrOpenPreviewModal = function (reportId) {
  // Find the report in local state
  const report = (_wr.reports || []).find(r => r.id === reportId);
  const modal  = document.getElementById('wr-preview-modal');
  const body   = document.getElementById('wr-preview-body');
  const title  = document.getElementById('wr-preview-title');
  if (!modal || !body) return;

  if (title && report) {
    title.textContent = 'Reporte — ' + _wrFormatWeek(report.week_start);
  }

  if (!report || !report.payload) {
    body.textContent = 'Vista previa no disponible para este reporte.';
  } else {
    // Build a human-readable preview from the payload stats
    const p = report.payload;
    const lines = [];

    if (p.revenue_current != null) {
      lines.push('Ingresos: ' + mesioFmt(p.revenue_current));
    }
    if (p.order_count_current != null) {
      lines.push('Pedidos: ' + p.order_count_current +
        (p.delivery_count != null ? ' (' + p.delivery_count + ' domicilio · ' + (p.table_count || 0) + ' mesa)' : ''));
    }
    if (p.nps_avg != null && p.nps_count) {
      lines.push('NPS promedio: ' + p.nps_avg + '/10 (' + p.nps_count + ' respuestas)');
    }
    if (p.dormant_frequent_count) {
      lines.push('Clientes frecuentes inactivos: ' + p.dormant_frequent_count);
    }
    if (p.revenue_delta_pct != null) {
      const sign = p.revenue_delta_pct >= 0 ? '+' : '';
      lines.push('Variacion vs semana anterior: ' + sign + p.revenue_delta_pct + '%');
    }

    body.textContent = lines.length ? lines.join('\n') : 'Sin datos disponibles.';
  }

  modal.style.display = 'flex';
};

window.wrClosePreviewModal = function () {
  const modal = document.getElementById('wr-preview-modal');
  if (modal) modal.style.display = 'none';
};

// ── Tooltip for failed badge ──────────────────────────────────────────────────
// Delegate click on .wr-badge-failed to show the error_message as a toast
document.addEventListener('click', function (e) {
  const badge = e.target.closest('.wr-badge-failed');
  if (!badge) return;
  const err = badge.getAttribute('data-err');
  if (err) mesioToast('Error: ' + err, 'error', 6000);
});
