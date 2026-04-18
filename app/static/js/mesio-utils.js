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

// ── Legacy → Org migration (runs once on utils load) ─
// If rb_org is absent but rb_restaurant exists, populate minimal Org shape
// from legacy data so downstream helpers work before the user re-logs in.
function mesioMigrateLegacyStorageIfNeeded() {
  if (mesioGetOrg()) return; // already migrated
  var legacy = localStorage.getItem('rb_restaurant');
  if (!legacy) return;
  try {
    var r = JSON.parse(legacy);
    mesioSetOrg(
      { id: r.id, name: r.name, whatsapp_number: r.whatsapp_number || null,
        features: r.features || {}, locale: r.locale, currency: r.currency },
      r.branch_id ? [{ id: r.branch_id, name: r.name, is_primary: true }] : [],
      r.branch_id || r.id
    );
  } catch (e) { console.warn('legacy storage migration failed', e); }
}
mesioMigrateLegacyStorageIfNeeded();

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

// ── Date formatter ───────────────────────────────
function mesioDate(isoStr) {
  if (!isoStr) return '—';
  try {
    // Prefer new Org key (Wave 1 S5); fall back to legacy rb_restaurant
    var src = mesioGetOrg() || JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    return new Date(isoStr).toLocaleString(src.locale || 'es-CO', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
    });
  } catch { return isoStr; }
}
