/* ═══════════════════════════════════════════════════
   Mesio — Shared Utilities
   Loaded before page-specific scripts.
   ═══════════════════════════════════════════════════ */

// ── XSS prevention ───────────────────────────────
function _escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Currency formatter ───────────────────────────
const _fmtCache = {};
function mesioFmt(n) {
  try {
    const r = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    const locale = r.locale || 'es-CO';
    const currency = r.currency || 'COP';
    const key = `${locale}:${currency}`;
    if (!_fmtCache[key]) {
      _fmtCache[key] = new Intl.NumberFormat(locale, {
        style: 'currency', currency,
        minimumFractionDigits: ['COP','CLP','JPY','KRW','VND','PYG','ISK'].includes(currency) ? 0 : 2,
        maximumFractionDigits: ['COP','CLP','JPY','KRW','VND','PYG','ISK'].includes(currency) ? 0 : 2,
      });
    }
    return _fmtCache[key].format(n || 0);
  } catch {
    return `$${Number(n||0).toLocaleString()}`;
  }
}

// ── Auth headers ─────────────────────────────────
function mesioHeaders() {
  const h = { 'Content-Type': 'application/json' };
  const t = localStorage.getItem('rb_token');
  if (t) h['Authorization'] = `Bearer ${t}`;
  const b = localStorage.getItem('rb_branch_id');
  if (b) h['X-Branch-ID'] = b;
  return h;
}

// ── Centralized logout ───────────────────────────
function mesioLogout() {
  const restId = localStorage.getItem('rb_staff_restaurant_id') || localStorage.getItem('rb_restaurant_id');
  const isStaff = !!localStorage.getItem('rb_staff_token');
  localStorage.clear();
  if (isStaff && restId) {
    window.location.href = `/login?r=${restId}`;
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
    const r = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    return new Date(isoStr).toLocaleString(r.locale || 'es-CO', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
    });
  } catch { return isoStr; }
}
