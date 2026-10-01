/* ══ Settings page — admin configuration ════════════════════════════
   Loads restaurant config and saves changes via PATCH /api/settings.
   Zero inline onclick — all handlers attached via addEventListener.
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

// ── Auth guard ────────────────────────────────────────────────────
(function () {
  var token = localStorage.getItem('rb_token');
  if (!token) { window.location.href = '/login'; }
})();

// ── DOM refs ──────────────────────────────────────────────────────
var restName, restNIT, restAddress, restCity, restCuisine, restCurrency;
var saveBtn, saveStatusEl;
var DAYS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'];
var DAYS_EN = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];

// ── Restaurant data ───────────────────────────────────────────────
var _restaurant = null;

// ── Fetch restaurant config ───────────────────────────────────────
async function loadSettings() {
  try {
    var res = await fetch('/api/settings', { headers: mesioHeaders() });
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    _restaurant = await res.json();
    renderSettings(_restaurant);
  } catch (e) {
    mesioToast('No se pudo cargar la configuración: ' + e.message, 'error');
  }
}

// ── Populate form ─────────────────────────────────────────────────
function renderSettings(r) {
  // Header
  var nameEls = document.querySelectorAll('.js-rest-name');
  nameEls.forEach(function (el) { el.textContent = r.name || 'Tu Restaurante'; });

  // Restaurant section
  setVal('inputName', r.name);
  setVal('inputNIT', r.nit);
  setVal('inputAddress', r.address);
  setVal('inputCity', r.city);
  setVal('inputCuisine', r.cuisine_type);

  // Currency select
  var curr = r.currency || 'COP';
  var selCurr = document.getElementById('selCurrency');
  if (selCurr) {
    for (var i = 0; i < selCurr.options.length; i++) {
      if (selCurr.options[i].value === curr) { selCurr.selectedIndex = i; break; }
    }
  }

  // Hours
  // Hours are the sede's (r.opening_hours); old saves only ever reached features.
  var hours = (r.opening_hours && Object.keys(r.opening_hours).length) ? r.opening_hours
    : ((r.features && r.features.opening_hours) ? r.features.opening_hours : {});
  renderHours(hours);

  // Address and hours are per sede: say which one is being edited.
  var hoursSub = document.querySelector('#horarios .s-sub');
  if (hoursSub) {
    hoursSub.textContent = r.location_name
      ? 'Cuándo la sede ' + r.location_name + ' acepta pedidos por QR y a domicilio. Cada sede tiene su propio horario.'
      : 'Elige una sede en el selector de la barra lateral para ver y cambiar su horario.';
  }

  // Payment instructions (text per digital method)
  var pi = (r.features && r.features.payment_instructions) ? r.features.payment_instructions : {};
  renderPaymentInstructions(pi);

  // Commerce features — DIAN toggle
  renderCommerceFeatures(r);

  // DIAN — read-only display
  renderDIAN(r.features || {});


  // Sidebar user
  var avatarEl = document.getElementById('sbAvatar');
  var userNameEl = document.getElementById('sbUserName');
  if (avatarEl && r.name) { avatarEl.textContent = r.name.slice(0, 2).toUpperCase(); }
  if (userNameEl) { userNameEl.textContent = r.name || ''; }
}

function setVal(id, val) {
  var el = document.getElementById(id);
  if (el && val != null) el.value = val;
}

// ── Hours grid ────────────────────────────────────────────────────
function renderHours(hours) {
  DAYS.forEach(function (day, i) {
    var key = DAYS_EN[i];
    var info = hours[key] || {};
    var openEl = document.getElementById('h-open-' + i);
    var closeEl = document.getElementById('h-close-' + i);
    var switchEl = document.getElementById('h-switch-' + i);
    if (openEl) { openEl.value = info.open || '12:00'; }
    if (closeEl) { closeEl.value = info.close || '22:00'; }
    var isOpen = info.hasOwnProperty('open') ? true : (info.open !== null);
    if (info.closed) isOpen = false;
    if (switchEl) {
      if (isOpen) {
        switchEl.classList.add('on');
        if (openEl) openEl.disabled = false;
        if (closeEl) closeEl.disabled = false;
      } else {
        switchEl.classList.remove('on');
        if (openEl) { openEl.disabled = true; }
        if (closeEl) { closeEl.disabled = true; }
      }
    }
  });
}

// ── Payment method toggles ────────────────────────────────────────
// The transfer methods a sede can accept (app/services/delivery.ALLOWED_PAYMENT_METHODS).
var PAYMENT_INSTRUCTION_KEYS = ['nequi', 'bancolombia'];
function renderPaymentInstructions(pi) {
  PAYMENT_INSTRUCTION_KEYS.forEach(function (key) {
    var ta = document.getElementById('pay-inst-' + key);
    if (!ta) return;
    // Tolerate both lower and capitalized keys (agent_external looks up both)
    var val = pi[key] || pi[key.charAt(0).toUpperCase() + key.slice(1)] || '';
    ta.value = val;
  });
}

// ── Commerce features toggles ─────────────────────────────────────
function renderCommerceFeatures(features) {
  var dianSw = document.getElementById('commerce-sw-dian');
  if (dianSw) {
    if (features.dian_enabled) { dianSw.classList.add('on'); } else { dianSw.classList.remove('on'); }
  }
}

// ── DIAN display (read-only) ──────────────────────────────────────
function renderDIAN(features) {
  var provEl = document.getElementById('dianProvider');
  var rangeEl = document.getElementById('dianRange');
  var autoEl = document.getElementById('dianAutoSwitch');
  if (provEl && features.dian_provider) { provEl.textContent = features.dian_provider; }
  if (rangeEl && features.dian_numeracion) { rangeEl.textContent = features.dian_numeracion; }
  if (autoEl) {
    if (features.dian_auto_invoice !== false) { autoEl.classList.add('on'); } else { autoEl.classList.remove('on'); }
  }
}

// ── Gather form data ──────────────────────────────────────────────
function collectFormData() {
  // Hours
  var opening_hours = {};
  DAYS.forEach(function (_, i) {
    var key = DAYS_EN[i];
    var switchEl = document.getElementById('h-switch-' + i);
    var openEl = document.getElementById('h-open-' + i);
    var closeEl = document.getElementById('h-close-' + i);
    var isOpen = switchEl && switchEl.classList.contains('on');
    opening_hours[key] = {
      open: isOpen ? (openEl ? openEl.value : '12:00') : null,
      close: isOpen ? (closeEl ? closeEl.value : '22:00') : null,
      closed: !isOpen
    };
  });

  // Payment instructions (free-text per digital method)
  var payment_instructions = {};
  PAYMENT_INSTRUCTION_KEYS.forEach(function (key) {
    var ta = document.getElementById('pay-inst-' + key);
    if (ta) payment_instructions[key] = (ta.value || '').trim();
  });

  // Commerce features
  var dianSw = document.getElementById('commerce-sw-dian');
  var commerceFeatures = {
    dian_enabled: dianSw ? dianSw.classList.contains('on') : false,
  };


  return {
    name: getVal('inputName'),
    nit: getVal('inputNIT'),
    address: getVal('inputAddress'),
    city: getVal('inputCity'),
    cuisine_type: getVal('inputCuisine'),
    // commerce feature flags
    dian_enabled: commerceFeatures.dian_enabled,
    // Top level, where POST /api/settings reads them. They used to be nested
    // inside `features`, which the server ignores — hours and transfer
    // instructions were never saved.
    opening_hours: opening_hours,
    payment_instructions: payment_instructions
  };
}

function getVal(id) {
  var el = document.getElementById(id);
  return el ? el.value.trim() : '';
}

// ── Save ──────────────────────────────────────────────────────────
async function saveSettings() {
  var btn = document.getElementById('saveBtnTop');
  if (btn) { btn.disabled = true; btn.textContent = 'Guardando…'; }
  try {
    var payload = collectFormData();
    var res = await fetch('/api/settings', {
      method: 'POST',
      headers: mesioHeaders(),
      body: JSON.stringify(payload)
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    _restaurant = await res.json();
    mesioToast('Cambios guardados', 'success');
    var statusEl = document.getElementById('saveStatus');
    if (statusEl) { statusEl.textContent = 'Guardado ahora'; }
  } catch (e) {
    mesioToast('No se pudo guardar: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Guardar cambios'; }
  }
}

// ── Danger zone actions ───────────────────────────────────────────
async function handlePause() {
  var currentlyPaused = _restaurant && _restaurant.features && _restaurant.features.bot_active === false;

  if (currentlyPaused) {
    // Unpause
    var ok = await mesioConfirm(
      '¿Reanudar la operación del restaurante? El bot volverá a responder clientes.',
      { confirmText: 'Reanudar' }
    );
    if (!ok) return;
    await _doPauseRequest(false);
  } else {
    // Pause
    var ok = await mesioConfirm(
      '¿Pausar operación del restaurante? El bot dejará de responder y el dashboard mostrará aviso. Podés reanudar en cualquier momento.',
      { confirmText: 'Pausar', danger: true }
    );
    if (!ok) return;
    await _doPauseRequest(true);
  }
}

async function _doPauseRequest(paused) {
  var btn = document.getElementById('btnPause');
  if (btn) { btn.disabled = true; }
  try {
    var res = await fetch('/api/settings/pause', {
      method: 'POST',
      headers: mesioHeaders(),
      body: JSON.stringify({ paused: paused })
    });
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    var data = await res.json();
    mesioToast(paused ? 'Restaurante pausado' : 'Restaurante reanudado', paused ? 'warn' : 'success');
    // Reload settings to reflect new state
    await loadSettings();
    _updatePauseButton();
  } catch (e) {
    mesioToast('Error: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; }
  }
}

function _updatePauseButton() {
  var btn = document.getElementById('btnPause');
  if (!btn) return;
  var currentlyPaused = _restaurant && _restaurant.features && _restaurant.features.bot_active === false;
  btn.textContent = currentlyPaused ? 'Reanudar restaurante' : 'Pausar restaurante';
}

// ── Sidenav scroll spy ────────────────────────────────────────────
function initScrollSpy() {
  var sections = document.querySelectorAll('.set-sec[id]');
  var navItems = document.querySelectorAll('.set-nav-item[href^="#"]');
  if (!sections.length || !navItems.length) return;

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      navItems.forEach(function (item) { item.classList.remove('active'); });
      var target = document.querySelector('.set-nav-item[href="#' + entry.target.id + '"]');
      if (target) target.classList.add('active');
    });
  }, { rootMargin: '-60px 0px -60% 0px' });

  sections.forEach(function (sec) { observer.observe(sec); });
}

// ── Toggle switch helper ──────────────────────────────────────────
function bindSwitches() {
  document.querySelectorAll('.switch[data-toggleable]').forEach(function (sw) {
    sw.addEventListener('click', function () {
      sw.classList.toggle('on');
      // If it's an hours switch, toggle the sibling inputs
      var dayIdx = sw.dataset.day;
      if (dayIdx != null) {
        var isOpen = sw.classList.contains('on');
        var openEl = document.getElementById('h-open-' + dayIdx);
        var closeEl = document.getElementById('h-close-' + dayIdx);
        if (openEl) openEl.disabled = !isOpen;
        if (closeEl) closeEl.disabled = !isOpen;
      }
    });
  });
}

// ── Sidenav smooth scroll ─────────────────────────────────────────
function bindNavLinks() {
  document.querySelectorAll('.set-nav-item[href^="#"]').forEach(function (link) {
    link.addEventListener('click', function (e) {
      e.preventDefault();
      var target = document.querySelector(link.getAttribute('href'));
      if (target) { target.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    });
  });
}

// ── Logout ────────────────────────────────────────────────────────
function bindLogout() {
  var btn = document.getElementById('logoutBtn');
  if (btn) { btn.addEventListener('click', mesioLogout); }
}


// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  // Save buttons
  document.querySelectorAll('.js-save-btn').forEach(function (btn) {
    btn.addEventListener('click', saveSettings);
  });

  // Danger zone
  var btnPause = document.getElementById('btnPause');
  if (btnPause) btnPause.addEventListener('click', handlePause);

  bindSwitches();
  bindNavLinks();
  bindLogout();
  initScrollSpy();
  loadSettings().then(function () { _updatePauseButton(); });
});
