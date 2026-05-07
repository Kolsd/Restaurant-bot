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
  var hours = (r.features && r.features.opening_hours) ? r.features.opening_hours : {};
  renderHours(hours);

  // Payment methods
  var pm = (r.features && r.features.payment_methods) ? r.features.payment_methods : {};
  renderPaymentToggles(pm);

  // Payment instructions (text per digital method)
  var pi = (r.features && r.features.payment_instructions) ? r.features.payment_instructions : {};
  renderPaymentInstructions(pi);

  // Wompi per-restaurant credentials (server returns masked secret summary)
  renderWompiCredentials(r.wompi || (r.features && r.features.wompi) || {});

  // Notifications
  var notif = (r.features && r.features.notifications) ? r.features.notifications : {};
  renderNotifToggles(notif);

  // Bot features — voice notes
  renderBotFeatures(r.features || {});

  // DIAN — read-only display
  renderDIAN(r.features || {});

  // Kiosko de personal URL
  renderKioskoUrl(r);

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
var PAYMENT_KEYS = ['cash', 'wompi', 'nequi', 'daviplata', 'bancolombia', 'bold'];
// Methods that require payment_instructions text (cash/wompi/bold use other flows)
var PAYMENT_INSTRUCTION_KEYS = ['nequi', 'daviplata', 'bancolombia'];
function renderPaymentToggles(pm) {
  PAYMENT_KEYS.forEach(function (key) {
    var sw = document.getElementById('pay-sw-' + key);
    if (!sw) return;
    if (pm[key] !== false) { sw.classList.add('on'); } else { sw.classList.remove('on'); }
  });
}
function renderPaymentInstructions(pi) {
  PAYMENT_INSTRUCTION_KEYS.forEach(function (key) {
    var ta = document.getElementById('pay-inst-' + key);
    if (!ta) return;
    // Tolerate both lower and capitalized keys (agent_external looks up both)
    var val = pi[key] || pi[key.charAt(0).toUpperCase() + key.slice(1)] || '';
    ta.value = val;
  });
}

// ── Wompi per-restaurant credentials ─────────────────────────────────
// The server returns a masked summary: { public_key, integrity_secret_set,
// integrity_secret_last4 }. The plaintext integrity_secret is NEVER returned
// — admin only sees it when typing it into the form. On save, if the input
// is empty the backend preserves the previously-stored secret.
function renderWompiCredentials(wompi) {
  var pkInput = document.getElementById('wompiPublicKey');
  var secretInput = document.getElementById('wompiIntegritySecret');
  var hintEl = document.getElementById('wompi-secret-hint');
  var badgeEl = document.getElementById('wompi-status-badge');
  if (!pkInput || !secretInput) return;

  var publicKey = (wompi && wompi.public_key) || '';
  var secretSet = !!(wompi && wompi.integrity_secret_set);
  var last4 = (wompi && wompi.integrity_secret_last4) || '';

  pkInput.value = publicKey;
  // NEVER populate the secret input with the plaintext value — it never leaves
  // the server. Leave it empty; placeholder hints what's stored.
  secretInput.value = '';
  if (secretSet) {
    secretInput.placeholder = last4 ? ('•••• •••• •••• ' + last4) : '•••• Integrity Secret guardado';
  } else {
    secretInput.placeholder = 'Integrity Secret';
  }

  if (hintEl) {
    hintEl.textContent = secretSet
      ? 'Secret guardado. Dejá este campo vacío para conservarlo o escribí uno nuevo para reemplazarlo.'
      : 'Sin secret configurado. Sin él, el bot usará el flujo de comprobante manual.';
  }

  if (badgeEl) {
    var configured = !!publicKey && secretSet;
    badgeEl.textContent = configured ? 'Conectado' : 'Sin configurar';
    badgeEl.classList.toggle('success', configured);
  }

  // Show/hide credentials block based on Wompi toggle state
  _toggleWompiCredentialsBlock();
}

function _toggleWompiCredentialsBlock() {
  var sw = document.getElementById('pay-sw-wompi');
  var block = document.getElementById('wompi-credentials');
  if (!sw || !block) return;
  block.style.display = sw.classList.contains('on') ? '' : 'none';
}

// ── Notification toggles ──────────────────────────────────────────
var NOTIF_KEYS = ['waiter_call', 'late_order', 'low_stock', 'nps_negative', 'tips_ready'];
function renderNotifToggles(notif) {
  NOTIF_KEYS.forEach(function (key) {
    var sw = document.getElementById('notif-sw-' + key);
    if (!sw) return;
    if (notif[key] !== false) { sw.classList.add('on'); } else { sw.classList.remove('on'); }
  });
}

// ── Bot features toggles ──────────────────────────────────────────
function renderBotFeatures(features) {
  var voiceSw = document.getElementById('bot-sw-voice-notes');
  if (voiceSw) {
    if (features.bot_voice_notes) { voiceSw.classList.add('on'); } else { voiceSw.classList.remove('on'); }
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

  // Payment methods
  var payment_methods = {};
  PAYMENT_KEYS.forEach(function (key) {
    var sw = document.getElementById('pay-sw-' + key);
    payment_methods[key] = sw ? sw.classList.contains('on') : true;
  });

  // Payment instructions (free-text per digital method)
  var payment_instructions = {};
  PAYMENT_INSTRUCTION_KEYS.forEach(function (key) {
    var ta = document.getElementById('pay-inst-' + key);
    if (ta) payment_instructions[key] = (ta.value || '').trim();
  });

  // Notifications
  var notifications = {};
  NOTIF_KEYS.forEach(function (key) {
    var sw = document.getElementById('notif-sw-' + key);
    notifications[key] = sw ? sw.classList.contains('on') : true;
  });

  // Bot features
  var voiceSw = document.getElementById('bot-sw-voice-notes');
  var botFeatures = {
    bot_voice_notes: voiceSw ? voiceSw.classList.contains('on') : false,
  };

  var currentFeatures = (_restaurant && _restaurant.features) ? Object.assign({}, _restaurant.features) : {};

  // Wompi credentials — empty integrity_secret means "preserve existing" on the server.
  var wompiPkEl = document.getElementById('wompiPublicKey');
  var wompiSecretEl = document.getElementById('wompiIntegritySecret');
  var wompiPayload = {
    public_key:       wompiPkEl ? (wompiPkEl.value || '').trim() : '',
    integrity_secret: wompiSecretEl ? (wompiSecretEl.value || '').trim() : ''
  };

  return {
    name: getVal('inputName'),
    nit: getVal('inputNIT'),
    address: getVal('inputAddress'),
    city: getVal('inputCity'),
    cuisine_type: getVal('inputCuisine'),
    wompi: wompiPayload,
    // bot feature flags — sent as top-level keys so _features_updatable in
    // settings_routes.py picks them up and writes them into features JSONB
    bot_voice_notes: botFeatures.bot_voice_notes,
    features: Object.assign(currentFeatures, {
      opening_hours: opening_hours,
      payment_methods: payment_methods,
      payment_instructions: payment_instructions,
      notifications: notifications,
    })
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
async function handleTransfer() {
  var ok = await mesioConfirm(
    'Transferir la propiedad cede el control total a otro usuario. ¿Continuar?',
    { confirmText: 'Transferir', danger: true }
  );
  if (!ok) return;
  mesioToast('Disponible en la próxima versión', 'info');
}

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

async function handleDelete() {
  var ok = await mesioConfirm(
    '¿Eliminar este restaurante? Esta acción borra todo y es IRREVERSIBLE.',
    { confirmText: 'Eliminar', cancelText: 'Cancelar', danger: true }
  );
  if (!ok) return;
  mesioToast('Disponible en la próxima versión', 'info');
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
      // Wompi toggle controls visibility of the credentials sub-block
      if (sw.id === 'pay-sw-wompi') {
        _toggleWompiCredentialsBlock();
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

// ── Kiosko de personal ────────────────────────────────────────────
function renderKioskoUrl(r) {
  var urlInput = document.getElementById('kiosko-url-input');
  if (!urlInput) return;
  // org_id is exposed as restaurant_id in the /api/settings response
  var orgId = (r && (r.restaurant_id || r.id)) || '';
  var kioskUrl = orgId
    ? (window.location.origin + '/staff-hq?kiosko=true&r=' + orgId)
    : '';
  urlInput.value = kioskUrl;
}

function bindKioskoHandlers() {
  var copyBtn = document.getElementById('kiosko-copy-btn');
  if (!copyBtn) return;
  copyBtn.addEventListener('click', function () {
    var urlInput = document.getElementById('kiosko-url-input');
    var url = urlInput ? urlInput.value : '';
    if (!url) {
      mesioToast('No hay URL disponible aún', 'error');
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () {
        mesioToast('URL copiada', 'success');
      }).catch(function () {
        _kioskoCopyFallback(urlInput);
      });
    } else {
      _kioskoCopyFallback(urlInput);
    }
  });
}

function _kioskoCopyFallback(inputEl) {
  if (!inputEl) return;
  inputEl.select();
  try {
    document.execCommand('copy');
    mesioToast('URL copiada', 'success');
  } catch (_) {
    mesioToast('Copiá manualmente la URL del campo', 'info');
  }
}

// ── Phone blocklist (admin) ───────────────────────────────────────
function _formatRelativeUntil(iso) {
  if (!iso) return '';
  var t = Date.parse(iso);
  if (isNaN(t)) return '';
  var diffMs = t - Date.now();
  if (diffMs <= 0) return 'expirado';
  var mins = Math.round(diffMs / 60000);
  if (mins < 60) return 'expira en ' + mins + ' min';
  var hours = Math.round(mins / 60);
  if (hours < 48) return 'expira en ' + hours + ' h';
  var days = Math.round(hours / 24);
  return 'expira en ' + days + ' días';
}

async function loadBlocklist() {
  var listEl = document.getElementById('bl-list');
  var countEl = document.getElementById('bl-count');
  if (!listEl) return;

  // Loading state — innerHTML with static markup only (no user data)
  listEl.innerHTML = '<div style="padding:14px;color:var(--text-3);font-size:13px;">Cargando&hellip;</div>';

  try {
    var res = await fetch('/api/admin/blocklist', { headers: mesioHeaders() });
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    var items = (data && data.items) || [];

    if (countEl) {
      countEl.textContent = items.length === 0
        ? 'No hay bloqueos activos'
        : items.length + ' bloqueo' + (items.length === 1 ? '' : 's') + ' activo' + (items.length === 1 ? '' : 's');
    }

    // Clear and rebuild via DOM (textContent for all user data)
    listEl.textContent = '';
    if (items.length === 0) {
      var empty = document.createElement('div');
      empty.style.cssText = 'padding:14px;color:var(--text-3);font-size:13px;';
      empty.textContent = 'No hay teléfonos bloqueados en este momento.';
      listEl.appendChild(empty);
      return;
    }

    items.forEach(function (item) {
      var row = document.createElement('div');
      row.setAttribute('role', 'listitem');
      row.style.cssText = 'display:grid;grid-template-columns:140px 1fr auto;gap:12px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--border);';

      // Phone (obfuscated)
      var phoneCell = document.createElement('div');
      var phoneSpan = document.createElement('span');
      phoneSpan.className = 'mono';
      phoneSpan.style.cssText = 'font-weight:600;';
      phoneSpan.textContent = item.phone_obf || '***';
      phoneCell.appendChild(phoneSpan);
      var expSpan = document.createElement('div');
      expSpan.style.cssText = 'font-size:11px;color:var(--text-3);margin-top:2px;';
      expSpan.textContent = _formatRelativeUntil(item.blocked_until);
      phoneCell.appendChild(expSpan);
      row.appendChild(phoneCell);

      // Reason + blocker
      var infoCell = document.createElement('div');
      var reasonDiv = document.createElement('div');
      reasonDiv.style.cssText = 'font-size:13px;color:var(--text-1);';
      reasonDiv.textContent = item.reason || '(sin razón)';
      infoCell.appendChild(reasonDiv);
      var byDiv = document.createElement('div');
      byDiv.style.cssText = 'font-size:11px;color:var(--text-3);margin-top:2px;';
      byDiv.textContent = 'Bloqueado por: ' + (item.blocked_by || 'sistema');
      infoCell.appendChild(byDiv);
      row.appendChild(infoCell);

      // Unblock button
      var btnCell = document.createElement('div');
      var btn = document.createElement('button');
      btn.className = 'btn sm';
      btn.type = 'button';
      btn.textContent = 'Desbloquear';
      btn.addEventListener('click', function () {
        handleUnblockPhone(item.phone, item.phone_obf);
      });
      btnCell.appendChild(btn);
      row.appendChild(btnCell);

      listEl.appendChild(row);
    });
  } catch (e) {
    listEl.textContent = '';
    var errDiv = document.createElement('div');
    errDiv.style.cssText = 'padding:14px;color:var(--danger);font-size:13px;';
    errDiv.textContent = 'No se pudieron cargar los bloqueos: ' + e.message;
    listEl.appendChild(errDiv);
  }
}

async function handleBlockPhone() {
  var phoneEl = document.getElementById('bl-phone');
  var reasonEl = document.getElementById('bl-reason');
  var hoursEl = document.getElementById('bl-hours');
  var btn = document.getElementById('bl-block-btn');
  if (!phoneEl || !btn) return;

  var rawPhone = (phoneEl.value || '').trim();
  var phone = rawPhone.replace(/[^\d]/g, '');
  if (phone.length < 7 || phone.length > 15) {
    mesioToast('El teléfono debe tener entre 7 y 15 dígitos', 'error');
    phoneEl.focus();
    return;
  }
  var reason = (reasonEl && reasonEl.value || '').trim();
  if (!reason) {
    mesioToast('Escribí una razón breve para el bloqueo', 'error');
    if (reasonEl) reasonEl.focus();
    return;
  }
  var hours = parseInt(hoursEl && hoursEl.value || '24', 10);
  if (!hours || hours < 1 || hours > 720) hours = 24;

  btn.disabled = true;
  var originalText = btn.textContent;
  btn.textContent = 'Bloqueando…';
  try {
    var res = await fetch('/api/admin/blocklist', {
      method: 'POST',
      headers: mesioHeaders(),
      body: JSON.stringify({ phone: phone, reason: reason, hours: hours })
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (res.status === 403) {
      mesioToast('No tenés permiso para bloquear teléfonos', 'error');
      return;
    }
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(err.detail || ('HTTP ' + res.status));
    }
    mesioToast('Teléfono bloqueado por ' + hours + ' h', 'success');
    phoneEl.value = '';
    if (reasonEl) reasonEl.value = '';
    await loadBlocklist();
  } catch (e) {
    mesioToast('No se pudo bloquear: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function handleUnblockPhone(phone, phoneObf) {
  if (!phone) return;
  var label = phoneObf || ('***' + phone.slice(-4));
  var ok = await mesioConfirm('¿Desbloquear el teléfono ' + label + '? Recibirá mensajes del bot inmediatamente.');
  if (!ok) return;

  try {
    var res = await fetch('/api/admin/blocklist/' + encodeURIComponent(phone), {
      method: 'DELETE',
      headers: mesioHeaders()
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (res.status === 403) {
      mesioToast('No tenés permiso para desbloquear teléfonos', 'error');
      return;
    }
    if (res.status === 404) {
      mesioToast('Ese teléfono ya no estaba bloqueado', 'info');
      await loadBlocklist();
      return;
    }
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(err.detail || ('HTTP ' + res.status));
    }
    mesioToast('Teléfono desbloqueado', 'success');
    await loadBlocklist();
  } catch (e) {
    mesioToast('No se pudo desbloquear: ' + e.message, 'error');
  }
}

function bindBlocklistHandlers() {
  var blockBtn = document.getElementById('bl-block-btn');
  var refreshBtn = document.getElementById('bl-refresh-btn');
  if (blockBtn) blockBtn.addEventListener('click', handleBlockPhone);
  if (refreshBtn) refreshBtn.addEventListener('click', loadBlocklist);
}

// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  // Save buttons
  document.querySelectorAll('.js-save-btn').forEach(function (btn) {
    btn.addEventListener('click', saveSettings);
  });

  // Danger zone
  var btnTransfer = document.getElementById('btnTransfer');
  var btnPause = document.getElementById('btnPause');
  var btnDelete = document.getElementById('btnDelete');
  if (btnTransfer) btnTransfer.addEventListener('click', handleTransfer);
  if (btnPause) btnPause.addEventListener('click', handlePause);
  if (btnDelete) btnDelete.addEventListener('click', handleDelete);

  bindSwitches();
  bindNavLinks();
  bindLogout();
  bindKioskoHandlers();
  bindBlocklistHandlers();
  initScrollSpy();
  loadSettings().then(function () { _updatePauseButton(); });
  loadBlocklist();
});
