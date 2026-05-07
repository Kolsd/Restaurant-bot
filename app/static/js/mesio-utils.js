/* ═══════════════════════════════════════════════════
   Mesio — Shared Utilities
   Loaded before page-specific scripts.
   ═══════════════════════════════════════════════════ */

// ── XSS prevention ───────────────────────────────
function _escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Org/Location storage keys (Wave 1 S5) ────────
var MESIO_ORG_KEY              = 'rb_org';
var MESIO_LOCATIONS_KEY        = 'rb_locations';
var MESIO_CURRENT_LOCATION_KEY = 'rb_current_location_id';

// ── Org/Location helpers ─────────────────────────
function mesioSetOrg(org, locations, defaultLocationId) {
  try {
    localStorage.setItem(MESIO_ORG_KEY, JSON.stringify(org || {}));
    localStorage.setItem(MESIO_LOCATIONS_KEY, JSON.stringify(locations || []));
    if (defaultLocationId != null) {
      localStorage.setItem(MESIO_CURRENT_LOCATION_KEY, String(defaultLocationId));
    }
  } catch (e) { console.warn('mesioSetOrg failed', e); }
}

function mesioGetOrg() {
  try { return JSON.parse(localStorage.getItem(MESIO_ORG_KEY) || 'null'); } catch { return null; }
}

function mesioGetLocations() {
  try { return JSON.parse(localStorage.getItem(MESIO_LOCATIONS_KEY) || '[]'); } catch { return []; }
}

function mesioGetCurrentLocationId() {
  var v = localStorage.getItem(MESIO_CURRENT_LOCATION_KEY);
  if (!v || v === 'all') return null;
  var n = parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
}

function mesioSetCurrentLocationId(id) {
  if (id === null || id === undefined || id === 'all') {
    localStorage.setItem(MESIO_CURRENT_LOCATION_KEY, 'all');
  } else {
    localStorage.setItem(MESIO_CURRENT_LOCATION_KEY, String(id));
  }
}

// ── Currency formatter ───────────────────────────
var _fmtCache = {};
function mesioFmt(n) {
  try {
    // Prefer new Org key (Wave 1 S5); fall back to legacy rb_restaurant
    var src = mesioGetOrg() || JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    var locale   = src.locale   || 'es-CO';
    var currency = src.currency || 'COP';
    var key = locale + ':' + currency;
    if (!_fmtCache[key]) {
      _fmtCache[key] = new Intl.NumberFormat(locale, {
        style: 'currency', currency: currency,
        minimumFractionDigits: ['COP','CLP','JPY','KRW','VND','PYG','ISK'].includes(currency) ? 0 : 2,
        maximumFractionDigits: ['COP','CLP','JPY','KRW','VND','PYG','ISK'].includes(currency) ? 0 : 2,
      });
    }
    return _fmtCache[key].format(n || 0);
  } catch {
    return '$' + Number(n || 0).toLocaleString();
  }
}

// ── Auth headers ─────────────────────────────────
function mesioHeaders() {
  var h = { 'Content-Type': 'application/json' };
  var t = localStorage.getItem('rb_token');
  if (t) h['Authorization'] = 'Bearer ' + t;
  // Legacy branch header — preserved for backward compat (Wave 1)
  var b = localStorage.getItem('rb_branch_id');
  if (b) h['X-Branch-ID'] = b;
  // New location header (Wave 1 S5) — backend prefers X-Location-ID when present
  var locId = localStorage.getItem(MESIO_CURRENT_LOCATION_KEY);
  h['X-Location-ID'] = (locId && locId !== '') ? locId : 'all';
  return h;
}

// ── Centralized logout ───────────────────────────
function mesioLogout() {
  var restId = localStorage.getItem('rb_staff_restaurant_id') || localStorage.getItem('rb_restaurant_id');
  var isStaff = !!localStorage.getItem('rb_staff_token');
  localStorage.clear();
  // New Org/Location keys are inside localStorage so localStorage.clear() above already
  // removes them. The explicit removes below are a safety net in case clear() is ever
  // replaced with targeted removal in a future refactor.
  localStorage.removeItem(MESIO_ORG_KEY);
  localStorage.removeItem(MESIO_LOCATIONS_KEY);
  localStorage.removeItem(MESIO_CURRENT_LOCATION_KEY);
  if (isStaff && restId) {
    window.location.href = '/login?r=' + restId;
  } else {
    window.location.href = '/login';
  }
}

// ── Toast notification ───────────────────────────
function mesioToast(message, type = 'success', duration = 3000) {
  const existing = document.querySelector('.m-toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = `m-toast m-toast--${type}`;
  toast.setAttribute('role', 'alert');
  toast.setAttribute('aria-live', 'polite');
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, duration);
}

// ── Audio alert ──────────────────────────────────
/**
 * Play a short two-tone ding for new-order alerts.
 * Uses WebAudio API (no audio file shipped). Silent fallback if not available
 * (Safari sometimes rejects autoplay until user interaction).
 */
function mesioDing() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const playTone = (freq, start, dur) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = freq;
      osc.type = 'sine';
      gain.gain.setValueAtTime(0, ctx.currentTime + start);
      gain.gain.linearRampToValueAtTime(0.18, ctx.currentTime + start + 0.02);
      gain.gain.linearRampToValueAtTime(0, ctx.currentTime + start + dur);
      osc.start(ctx.currentTime + start);
      osc.stop(ctx.currentTime + start + dur);
    };
    playTone(880,  0,    0.18);  // A5
    playTone(1175, 0.2,  0.22);  // D6
    setTimeout(() => ctx.close().catch(() => {}), 600);
  } catch (e) {
    // silent — audio is best-effort
  }
}
window.mesioDing = mesioDing;

// ── Browser notifications ─────────────────────────
/**
 * Request browser notification permission. Returns the resulting permission
 * state ('granted' | 'denied' | 'default'). Safe to call anywhere — checks
 * browser support and current state internally.
 */
async function mesioRequestNotificationPermission() {
  if (!('Notification' in window)) return 'denied';
  if (Notification.permission !== 'default') return Notification.permission;
  try {
    return await Notification.requestPermission();
  } catch {
    return 'denied';
  }
}

/**
 * Show a browser notification (only if permission was granted).
 * Silent no-op otherwise.
 */
function mesioNotify(title, body, opts = {}) {
  try {
    if (!('Notification' in window)) return;
    if (Notification.permission !== 'granted') return;
    const n = new Notification(title, { body, silent: false, ...opts });
    setTimeout(() => n.close(), 8000);
  } catch {
    // silent
  }
}
window.mesioRequestNotificationPermission = mesioRequestNotificationPermission;
window.mesioNotify = mesioNotify;

// ── Confirm dialog (replaces window.confirm) ─────
function mesioConfirm(message, { confirmText = 'Confirmar', cancelText = 'Cancelar', danger = false } = {}) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'm-confirm-overlay';
    overlay.innerHTML = `
      <div class="m-confirm-box">
        <p>${_escHtml(message)}</p>
        <div class="m-confirm-actions">
          <button class="m-btn m-btn--ghost m-confirm-cancel">${_escHtml(cancelText)}</button>
          <button class="m-btn ${danger ? 'm-btn--danger' : 'm-btn--primary'} m-confirm-ok">${_escHtml(confirmText)}</button>
        </div>
      </div>`;
    const cleanup = (val) => { overlay.remove(); resolve(val); };
    overlay.querySelector('.m-confirm-cancel').onclick = () => cleanup(false);
    overlay.querySelector('.m-confirm-ok').onclick = () => cleanup(true);
    overlay.addEventListener('click', e => { if (e.target === overlay) cleanup(false); });
    document.body.appendChild(overlay);
    overlay.querySelector('.m-confirm-ok').focus();
  });
}

// ── Prompt dialog (replaces window.prompt) ───────
/**
 * Show a modal prompt for text/number input. Replaces window.prompt() which
 * is unreliable on iOS Chrome and Android PWAs in standalone mode.
 *
 * @param {string} message - the prompt question shown to the user
 * @param {object} opts
 * @param {string} [opts.title] - modal title
 * @param {string} [opts.placeholder] - input placeholder
 * @param {string} [opts.defaultValue] - prefilled value
 * @param {'text'|'number'} [opts.type='text'] - input type
 * @param {string} [opts.confirmLabel='Aceptar']
 * @param {string} [opts.cancelLabel='Cancelar']
 * @param {(value: string) => string|null} [opts.validator] - return error string or null if valid
 * @returns {Promise<string|null>} - entered value, or null if cancelled
 */
function mesioPrompt(message, opts = {}) {
  const {
    title = '',
    placeholder = '',
    defaultValue = '',
    type = 'text',
    confirmLabel = 'Aceptar',
    cancelLabel = 'Cancelar',
    validator = null,
  } = opts;

  return new Promise(resolve => {
    const titleId = 'm-prompt-title-' + Date.now();

    const overlay = document.createElement('div');
    overlay.className = 'm-confirm-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');

    const box = document.createElement('div');
    box.className = 'm-confirm-box';

    if (title) {
      const h = document.createElement('p');
      h.id = titleId;
      h.style.cssText = 'font-weight:600;margin:0 0 6px;';
      h.textContent = title;
      overlay.setAttribute('aria-labelledby', titleId);
      box.appendChild(h);
    } else {
      overlay.setAttribute('aria-label', message);
    }

    const msg = document.createElement('p');
    msg.style.cssText = title ? 'margin:0 0 12px;' : 'font-weight:600;margin:0 0 12px;';
    msg.textContent = message;
    box.appendChild(msg);

    const input = document.createElement('input');
    input.type = type;
    input.placeholder = placeholder;
    input.value = defaultValue;
    input.setAttribute('aria-label', message);
    input.style.cssText = 'width:100%;box-sizing:border-box;padding:8px 10px;border-radius:6px;border:1px solid var(--border,#334155);background:var(--surface-3,#1e293b);color:var(--text-1,#f1f5f9);font-size:14px;font-family:inherit;margin-bottom:4px;';
    box.appendChild(input);

    const errorEl = document.createElement('p');
    errorEl.style.cssText = 'color:#f87171;font-size:12px;min-height:16px;margin:0 0 10px;';
    errorEl.setAttribute('aria-live', 'polite');
    box.appendChild(errorEl);

    const actions = document.createElement('div');
    actions.className = 'm-confirm-actions';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'm-btn m-btn--ghost m-confirm-cancel';
    cancelBtn.textContent = cancelLabel;

    const confirmBtn = document.createElement('button');
    confirmBtn.className = 'm-btn m-btn--primary m-confirm-ok';
    confirmBtn.textContent = confirmLabel;

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(actions);
    overlay.appendChild(box);

    const cleanup = (val) => { overlay.remove(); resolve(val); };

    const submit = () => {
      const val = input.value;
      if (validator) {
        const err = validator(val);
        if (err) {
          errorEl.textContent = err;
          input.focus();
          return;
        }
      }
      cleanup(val);
    };

    cancelBtn.onclick = () => cleanup(null);
    confirmBtn.onclick = submit;
    overlay.addEventListener('click', e => { if (e.target === overlay) cleanup(null); });

    overlay.addEventListener('keydown', e => {
      if (e.key === 'Escape') { cleanup(null); return; }
      if (e.key === 'Enter') { e.preventDefault(); submit(); return; }
      // Focus trap: Tab/Shift+Tab cycles input → confirmBtn → cancelBtn → input
      if (e.key === 'Tab') {
        const focusable = [input, confirmBtn, cancelBtn];
        const idx = focusable.indexOf(document.activeElement);
        e.preventDefault();
        focusable[e.shiftKey
          ? (idx - 1 + focusable.length) % focusable.length
          : (idx + 1) % focusable.length
        ].focus();
      }
    });

    document.body.appendChild(overlay);
    // Auto-focus after paint so the overlay is visible before focus fires
    requestAnimationFrame(() => input.focus());
  });
}
window.mesioPrompt = mesioPrompt;

// ── Focus trap for accessible modals ────────────
/**
 * Activate accessibility-correct modal behavior on a custom modal:
 *  - sets role="dialog", aria-modal="true"
 *  - traps Tab/Shift+Tab cycling between the first and last focusable elements
 *  - ESC key triggers opts.onEscape (default: noop — caller handles close)
 *  - Auto-focuses the first focusable element (or opts.initialFocus)
 *  - Returns a deactivate() function that restores prior focus and removes listeners
 *
 * Usage:
 *   const trap = mesioFocusTrap(myModalEl, { onEscape: closeFn, labelledBy: 'my-title-id' });
 *   // ... when closing the modal:
 *   trap.deactivate();
 *
 * @param {HTMLElement} modalEl - the modal container element
 * @param {object} opts
 * @param {() => void} [opts.onEscape] - called when ESC is pressed inside the modal
 * @param {string} [opts.labelledBy] - id of element that labels the modal (sets aria-labelledby)
 * @param {HTMLElement} [opts.initialFocus] - element to focus first (default: first focusable)
 * @returns {{ deactivate: () => void }}
 */
function mesioFocusTrap(modalEl, opts) {
  opts = opts || {};
  var FOCUSABLE_SEL = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  // Set ARIA attributes
  modalEl.setAttribute('role', 'dialog');
  modalEl.setAttribute('aria-modal', 'true');
  if (opts.labelledBy) {
    modalEl.setAttribute('aria-labelledby', opts.labelledBy);
  }

  // Save prior focus for restoration on deactivate
  var prevFocus = document.activeElement;

  var onKeyDown = function(e) {
    var focusable = Array.from(modalEl.querySelectorAll(FOCUSABLE_SEL)).filter(function(el) {
      return el.offsetParent !== null || el === document.activeElement;
    });

    if (e.key === 'Escape' || e.key === 'Esc') {
      if (opts.onEscape) opts.onEscape();
      return;
    }

    if (e.key === 'Tab' && focusable.length > 0) {
      var first = focusable[0];
      var last  = focusable[focusable.length - 1];
      if (e.shiftKey) {
        // Shift+Tab on first → wrap to last
        if (document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
      } else {
        // Tab on last → wrap to first
        if (document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
  };

  modalEl.addEventListener('keydown', onKeyDown);

  // Auto-focus after paint so the overlay is visible before focus fires
  requestAnimationFrame(function() {
    var target = opts.initialFocus;
    if (!target) {
      var focusable = modalEl.querySelectorAll(FOCUSABLE_SEL);
      target = focusable.length ? focusable[0] : null;
    }
    if (target) target.focus();
  });

  return {
    deactivate: function() {
      modalEl.removeEventListener('keydown', onKeyDown);
      if (prevFocus && prevFocus.focus) prevFocus.focus();
    }
  };
}
window.mesioFocusTrap = mesioFocusTrap;

// ── Connection monitor ───────────────────────────
const _connState = { failCount: 0, maxFails: 3 };
function mesioTrackFetch(ok) {
  if (ok) {
    _connState.failCount = 0;
  } else {
    _connState.failCount++;
  }
  document.querySelectorAll('.m-live-status').forEach(el => {
    if (_connState.failCount >= _connState.maxFails) {
      el.classList.remove('online');
      el.classList.add('offline');
      const label = el.querySelector('.label');
      if (label) label.textContent = 'Sin conexión';
    } else {
      el.classList.remove('offline');
      el.classList.add('online');
      const label = el.querySelector('.label');
      if (label) label.textContent = 'En vivo';
    }
  });
}

// ── Visibility-aware interval ────────────────────
function mesioInterval(fn, ms) {
  return setInterval(() => {
    if (document.visibilityState !== 'hidden') fn();
  }, ms);
}

// ── Cloudinary image URL transform ───────────────
/**
 * Insert a Cloudinary transform into an image URL. Falls through unchanged
 * if the URL isn't a Cloudinary URL (e.g. external hosting, data URIs).
 *
 * @param {string} url - the original Cloudinary URL
 * @param {'thumb'|'card'|'hero'} variant - target size variant
 * @returns {string} - transformed URL or original if not Cloudinary
 */
function mesioImageUrl(url, variant) {
  if (!url || typeof url !== 'string') return url || '';
  if (!url.includes('res.cloudinary.com')) return url;
  if (!url.includes('/upload/')) return url;
  // Skip if already transformed (idempotent) — detects any transform param after /upload/
  if (/\/upload\/[a-z]_/.test(url)) return url;
  var transforms = {
    thumb: 'c_fill,w_300,h_300,q_auto,f_auto',
    card:  'c_fill,w_600,h_450,q_auto,f_auto',
    hero:  'c_fill,w_1200,h_900,q_auto,f_auto',
  };
  var t = transforms[variant] || transforms.card;
  return url.replace('/upload/', '/upload/' + t + '/');
}
window.mesioImageUrl = mesioImageUrl;

// ── Date formatter ───────────────────────────────
/**
 * Format an ISO date string (or Date object) for display.
 *
 * @param {string|Date} isoStr - ISO date string or Date object
 * @param {object} [opts]
 * @param {'short'|'long'|'compact'|'time'|'datetime'} [opts.format='short']
 *   - 'short':    "27 abr · 14:30"  (day-short-month, hour:minute)  ← original behaviour
 *   - 'long':     "27 abr 2026"     (day-short-month, 4-digit year, no time)
 *   - 'compact':  "27 abr"          (day + short month, no year, no time)
 *   - 'time':     "14:30"           (hour + minute only)
 *   - 'datetime': "27 abr · 14:30"  (alias for 'short', explicit name)
 * @param {string} [opts.locale]  Override locale (default: rb_org.locale or 'es-CO')
 * @returns {string} Formatted date, or '—' for null/undefined, or raw string on error
 */
function mesioDate(isoStr, opts) {
  if (isoStr == null || isoStr === '') return '—';
  opts = opts || {};
  var fmt = opts.format || 'short';
  try {
    var src = mesioGetOrg() || JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    var locale = opts.locale || src.locale || 'es-CO';
    var d = isoStr instanceof Date ? isoStr : new Date(isoStr);
    if (isNaN(d.getTime())) return String(isoStr);
    if (fmt === 'long') {
      return d.toLocaleDateString(locale, { day: '2-digit', month: 'short', year: 'numeric' });
    }
    if (fmt === 'compact') {
      return d.toLocaleDateString(locale, { day: 'numeric', month: 'short' });
    }
    if (fmt === 'time') {
      return d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
    }
    // 'short' and 'datetime': day + short month + hour:minute (original behaviour)
    return d.toLocaleString(locale, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  } catch (_) { return String(isoStr); }
}

/** Thin wrapper — returns only the time portion ("14:30"). */
function mesioTime(isoStr) { return mesioDate(isoStr, { format: 'time' }); }

/** Thin wrapper — returns day + short month + time ("27 abr · 14:30"). */
function mesioDateTime(isoStr) { return mesioDate(isoStr, { format: 'short' }); }

// ── Floating support button ──────────────────────────────────────
/**
 * Injects a fixed bottom-right WhatsApp support button on admin pages.
 * Skipped on operational KDS/POS pages and public pages (landing, menu, login, demo).
 * Opened on DOMContentLoaded — safe to call from any admin page that loads mesio-utils.js.
 */
(function _mesioSupportButton() {
  // URL patterns where the button should NOT appear
  var SKIP_PATHS = [
    '/caja', '/cocina', '/bar', '/mesero', '/domiciliario', '/staff-hq',
    '/landing', '/menu', '/login', '/signup', '/demo', '/dashboard-demo',
    '/r/',   // public QR menu routes
  ];

  function _shouldShow() {
    var p = window.location.pathname;
    for (var i = 0; i < SKIP_PATHS.length; i++) {
      if (p === SKIP_PATHS[i] || p.startsWith(SKIP_PATHS[i] + '/') || p.startsWith(SKIP_PATHS[i] + '?')) {
        return false;
      }
      // Exact match for paths like '/caja' that may not have trailing slash
      if (p.replace(/\/$/, '') === SKIP_PATHS[i].replace(/\/$/, '')) return false;
    }
    // Only show on known admin paths — don't show on public menu.html etc.
    var ADMIN_PATHS = [
      '/dashboard', '/pedidos', '/reservaciones', '/menu-admin', '/menu-engineering',
      '/nps', '/fidelizacion', '/clientes-riesgo', '/nomina', '/sucursales',
      '/floorplan', '/equipo', '/settings', '/billing', '/stats',
      '/internal/analytics', '/internal/monitoring', '/internal/superadmin', '/internal/crm',
    ];
    for (var j = 0; j < ADMIN_PATHS.length; j++) {
      if (p === ADMIN_PATHS[j] || p.startsWith(ADMIN_PATHS[j] + '/')) return true;
    }
    return false;
  }

  function _inject() {
    if (!_shouldShow()) return;
    if (document.getElementById('mesio-support-btn')) return; // idempotent

    // Build WhatsApp deep-link pre-filled with restaurant name
    var restName = '';
    try {
      var rb = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
      restName = (rb.name || rb.org_name || '').trim();
    } catch (_) {}
    var msgText = restName
      ? 'Hola Mesio, necesito ayuda con mi cuenta de ' + restName
      : 'Hola Mesio, necesito ayuda';
    var waUrl = 'https://wa.me/573144914554?text=' + encodeURIComponent(msgText);

    var btn = document.createElement('a');
    btn.id = 'mesio-support-btn';
    btn.href = waUrl;
    btn.target = '_blank';
    btn.rel = 'noopener noreferrer';
    btn.setAttribute('aria-label', 'Soporte por WhatsApp');
    btn.title = 'Soporte por WhatsApp';
    btn.textContent = '💬';
    btn.style.cssText = [
      'position:fixed',
      'bottom:20px',
      'right:20px',
      'width:48px',
      'height:48px',
      'border-radius:50%',
      'background:var(--brand,#1D9E75)',
      'color:#fff',
      'font-size:22px',
      'line-height:48px',
      'text-align:center',
      'text-decoration:none',
      'box-shadow:0 2px 12px rgba(0,0,0,.25)',
      'z-index:50',
      'transition:transform .15s,box-shadow .15s',
      'display:flex',
      'align-items:center',
      'justify-content:center',
    ].join(';');

    btn.addEventListener('mouseenter', function () {
      btn.style.transform = 'scale(1.1)';
      btn.style.boxShadow = '0 4px 18px rgba(0,0,0,.3)';
    });
    btn.addEventListener('mouseleave', function () {
      btn.style.transform = '';
      btn.style.boxShadow = '0 2px 12px rgba(0,0,0,.25)';
    });

    document.body.appendChild(btn);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _inject);
  } else {
    _inject();
  }
})();
