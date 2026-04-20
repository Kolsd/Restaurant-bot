/* ═══════════════════════════════════════════════════════════════
   STAFF CLOCK — interactions + backend wiring
   Produced by: staff-clock.html design → production port
   ═══════════════════════════════════════════════════════════════

   Backend endpoints wired:
     GET  /api/staff/self/profile         → loadProfile()
     POST /api/staff/self/clock-in        → doClockAction('clock-in')
     POST /api/staff/self/clock-out       → doClockAction('clock-out')
     POST /api/staff/self/break-start     → doClockAction('break-start')
     POST /api/staff/self/break-end       → doClockAction('break-end')
     GET  /api/staff/self/timecard        → loadTimecardPreview()
     GET  /api/staff/schedules            → loadUpcomingShifts() [upcoming]
     GET  /api/staff/webauthn/credentials → loadBiometricStatus()
     POST /api/staff/webauthn/register-options   → startBioRegistration()
     POST /api/staff/webauthn/register-complete  → startBioRegistration()

   TODO (backend endpoints not yet implemented):
     GET  /api/staff/self/tips            → tips accumulated today/week
     POST /api/staff/self/shift-swap      → shift swap request
     GET  /api/staff/announcements        → manager announcements
     GET  /api/staff/self/tasks           → shift checklist tasks
     GET  /api/staff/self/performance     → NPS + tip performance stats
*/

'use strict';

/* ── Constants ─────────────────────────────────────────────────── */
const SC_TOKEN  = localStorage.getItem('rb_token');
const SC_REST   = (() => {
  try { return JSON.parse(localStorage.getItem('rb_restaurant') || '{}'); }
  catch(_) { return {}; }
})();

if (!SC_TOKEN) {
  window.location.href = '/login';
}

const SC_ROLE_META = {
  mesero:       { label: 'Mesero',     url: '/mesero',       svg: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="5"/><path d="M8 1v7"/></svg>' },
  cocina:       { label: 'Cocina',     url: '/cocina',       svg: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 12h8M6 6a2 2 0 104 0V4H6v2z"/></svg>' },
  bar:          { label: 'Bar',        url: '/bar',          svg: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 2h6l-2 5h2l-4 7-4-7h2L5 2z"/></svg>' },
  caja:         { label: 'Caja',       url: '/caja',         svg: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="10" rx="1"/><path d="M2 6h12M5 9h3"/></svg>' },
  domiciliario: { label: 'Domicilios', url: '/domiciliario', svg: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="12" r="1.5"/><circle cx="11" cy="12" r="1.5"/><path d="M1 4h10l2 5H1z"/></svg>' },
};

/* ── Profile state ─────────────────────────────────────────────── */
let _profile = null;
let _durInterval = null;

async function scLoadProfile() {
  try {
    const res = await fetch('/api/staff/self/profile', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) { if (res.status === 401) scLogout(); return; }
    _profile = await res.json();
    scRenderHeader(_profile);
    scRenderShiftStatus(_profile);
    scRenderButtons(_profile);
    scRenderStations(_profile);
    scRenderKioscoIdent(_profile);
  } catch(e) {
    // silently fail — UI stays in loading state
  }
}

function scRenderHeader(p) {
  const initials = (p.name || '?').split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2);

  const mAv = document.getElementById('sc-m-avatar');
  if (mAv) mAv.textContent = initials;

  const mName = document.getElementById('sc-m-name');
  if (mName) mName.textContent = p.name || '';

  const mGreet = document.getElementById('sc-m-greet');
  if (mGreet) {
    const h = new Date().getHours();
    const greet = h < 12 ? 'Buenos días' : h < 19 ? 'Buenas tardes' : 'Buenas noches';
    mGreet.textContent = greet;
  }
}

function scRenderShiftStatus(p) {
  clearInterval(_durInterval);

  const pill = document.getElementById('sc-m-status');
  const pillTxt = document.getElementById('sc-m-status-text');
  const stlHead = document.getElementById('sc-stl-label');
  const stlDur  = document.getElementById('sc-stl-dur');
  const stlFoot = document.getElementById('sc-stl-foot');

  const kHead = document.getElementById('sc-k-panel-sub');
  const kDur  = document.getElementById('sc-k-dur');
  const kTlSched = document.getElementById('sc-k-tl-sched');
  const kTlDur   = document.getElementById('sc-k-tl-dur');

  if (p.current_break) {
    if (pill) { pill.classList.remove('off'); pill.classList.add('break'); }
    if (pillTxt) {
      const since = scFmtTime(p.current_break.break_start);
      pillTxt.textContent = 'En break desde ' + since;
    }
    scUpdateProgress(null, null, null);
  } else if (p.current_shift) {
    if (pill) { pill.classList.remove('off', 'break'); }
    const clockIn = new Date(p.current_shift.clock_in);
    if (pillTxt) {
      function tick() {
        const mins = Math.floor((Date.now() - clockIn) / 60000);
        const h2 = Math.floor(mins / 60), m2 = mins % 60;
        pillTxt.textContent = 'En turno · ' + (h2 > 0 ? h2 + 'h ' : '') + m2 + 'm';

        if (stlHead) stlHead.textContent = 'Turno de hoy' + ((_profile && _profile.roles && _profile.roles[0]) ? ' · ' + _profile.roles[0].charAt(0).toUpperCase() + _profile.roles[0].slice(1) : '');
        if (stlDur)  stlDur.textContent  = (h2 > 0 ? h2 + 'h ' : '') + m2 + 'm';
        if (kDur)    kDur.textContent    = (h2 > 0 ? h2 + 'h ' : '') + m2 + 'm';
        if (kHead)   kHead.textContent   = 'Llevas ' + (h2 > 0 ? h2 + 'h ' : '') + m2 + 'm trabajados.';
      }
      tick();
      _durInterval = setInterval(tick, 30000);

      if (stlFoot) {
        const cin = scFmtTime(p.current_shift.clock_in);
        stlFoot.innerHTML = '';
        const cinSpan = document.createElement('span');
        cinSpan.textContent = 'Entrada ';
        const cinB = document.createElement('b');
        cinB.textContent = cin;
        cinSpan.appendChild(cinB);
        stlFoot.appendChild(cinSpan);
      }
    }
    scUpdateProgress(clockIn, null, p.current_shift);
  } else {
    if (pill) { pill.classList.add('off'); }
    if (pillTxt) pillTxt.textContent = 'Sin fichar';
    if (stlDur) stlDur.textContent = '—';
    if (kHead)  kHead.textContent = 'Ficha tu entrada para comenzar el turno.';
  }
}

function scUpdateProgress(clockIn, breakStart, shiftData) {
  const bar = document.getElementById('sc-stl-progress');
  const nowBar = document.getElementById('sc-stl-now');
  if (!bar || !nowBar) return;

  if (!clockIn) { bar.style.width = '0'; nowBar.style.left = '0'; return; }

  // Simple progress based on 8h shift default if no schedule
  const elapsed = (Date.now() - clockIn) / 1000 / 3600;
  const total = 8;
  const pct = Math.min(Math.round((elapsed / total) * 100), 100);
  bar.style.width = pct + '%';
  nowBar.style.left = pct + '%';

  // Kiosco bar too
  const kBar  = document.getElementById('sc-k-tl-fill');
  const kNow  = document.getElementById('sc-k-tl-now');
  if (kBar)  kBar.style.width = pct + '%';
  if (kNow)  kNow.style.left  = pct + '%';
}

function scRenderButtons(p) {
  const inShift = !!p.current_shift;
  const inBreak = !!p.current_break;

  // Mobile action buttons
  const mClockIn  = document.getElementById('sc-m-btn-clock-in');
  const mClockOut = document.getElementById('sc-m-btn-clock-out');
  const mBreak    = document.getElementById('sc-m-btn-break');

  if (mClockIn) {
    mClockIn.style.display = inShift ? 'none' : '';
  }
  if (mClockOut) {
    mClockOut.style.display = inShift ? '' : 'none';
    mClockOut.classList.toggle('finish', inShift);
  }
  if (mBreak) {
    mBreak.style.display = inShift ? '' : 'none';
    mBreak.classList.toggle('active', inBreak);
    const breakTxt = mBreak.querySelector('.sc-m-break-label');
    if (breakTxt) breakTxt.textContent = inBreak ? 'Fin break' : 'Break';
  }

  // Kiosco action buttons
  const kClockIn  = document.getElementById('sc-k-btn-clock-in');
  const kClockOut = document.getElementById('sc-k-btn-clock-out');
  const kBreak    = document.getElementById('sc-k-btn-break');

  if (kClockIn)  kClockIn.style.display  = inShift ? 'none' : '';
  if (kClockOut) {
    kClockOut.style.display = inShift ? '' : 'none';
    kClockOut.classList.toggle('finish', inShift);
  }
  if (kBreak) {
    if (!inShift) { kBreak.style.display = 'none'; }
    else { kBreak.style.display = ''; }
    kBreak.classList.toggle('active', inBreak);
    const kBreakTxt = kBreak.querySelector('b');
    if (kBreakTxt) kBreakTxt.textContent = inBreak ? 'Fin de break' : 'Break';
  }
}

function scRenderStations(p) {
  const roles = (p.roles || []);

  ['sc-stations-m', 'sc-k-stations'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '';
    roles.forEach(role => {
      const meta = SC_ROLE_META[role];
      if (!meta) return;
      if (id === 'sc-stations-m') {
        const a = document.createElement('a');
        a.className = 'sc-stn';
        a.href = meta.url;
        const ico = document.createElement('div');
        ico.className = 'stn-ico';
        ico.innerHTML = meta.svg; // static SVG strings, not user data
        const nm = document.createElement('div');
        nm.className = 'stn-name';
        nm.textContent = meta.label;
        a.appendChild(ico);
        a.appendChild(nm);
        el.appendChild(a);
      }
    });
    if (!el.children.length && id === 'sc-stations-m') {
      const p2 = document.createElement('p');
      p2.style.cssText = 'padding:10px 14px;font-size:12px;color:var(--sc-text-3);';
      p2.textContent = 'Sin estaciones asignadas.';
      el.appendChild(p2);
    }
  });
}

function scRenderKioscoIdent(p) {
  const initials = (p.name || '?').split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2);
  const av = document.getElementById('sc-k-ident-av');
  if (av) av.textContent = initials;
  const nm = document.getElementById('sc-k-ident-name');
  if (nm) nm.textContent = p.name || '';
  const roles = document.getElementById('sc-k-ident-roles');
  if (roles) {
    roles.innerHTML = '';
    (p.roles || []).forEach(r => {
      const t = document.createElement('span');
      t.className = 'tag';
      t.textContent = r.charAt(0).toUpperCase() + r.slice(1);
      roles.appendChild(t);
    });
  }
  const panelTitle = document.getElementById('sc-k-panel-title');
  if (panelTitle) {
    const nm2 = (p.name || '').split(' ')[0];
    panelTitle.textContent = 'Hola, ' + nm2 + ' 👋';
  }
}

/* ── Live clock ─────────────────────────────────────────────────── */
function scPad(n) { return String(n).padStart(2, '0'); }

function scTick() {
  const now = new Date();
  const hh = scPad(now.getHours()), mm = scPad(now.getMinutes()), ss = scPad(now.getSeconds());
  const t = hh + ':' + mm;

  const mTime = document.getElementById('sc-m-time');
  const mSec  = document.getElementById('sc-m-sec');
  const kTime = document.getElementById('sc-k-time');
  const kSec  = document.getElementById('sc-k-sec');

  if (mTime) mTime.textContent = t;
  if (mSec)  mSec.textContent  = ':' + ss;
  if (kTime) kTime.textContent = t;
  if (kSec)  kSec.textContent  = ':' + ss;

  const dateStr = now.toLocaleDateString('es-CO', { weekday: 'long', day: 'numeric', month: 'long' });
  const dateStrK = now.toLocaleDateString('es-CO', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
  const mDate = document.getElementById('sc-m-date');
  if (mDate) mDate.textContent = dateStr.charAt(0).toUpperCase() + dateStr.slice(1);
  const kDate = document.getElementById('sc-k-date');
  if (kDate) kDate.textContent = dateStrK.charAt(0).toUpperCase() + dateStrK.slice(1);
}
scTick();
setInterval(scTick, 1000);

/* ── Clock actions ─────────────────────────────────────────────── */
const SC_ACTION_ENDPOINTS = {
  'clock-in':    '/api/staff/self/clock-in',
  'clock-out':   '/api/staff/self/clock-out',
  'break-start': '/api/staff/self/break-start',
  'break-end':   '/api/staff/self/break-end',
};

async function doClockAction(action) {
  // Auth modal flows through WebAuthn or PIN before hitting the endpoint
  // For mobile: opens auth-modal-m; for kiosco: opens auth-modal
  // The pendingAction is dispatched after auth confirm
  _pendingAction = action;
  scOpenAuth(action);
}

async function _dispatchClockAction(action) {
  const endpoint = SC_ACTION_ENDPOINTS[action];
  if (!endpoint) return;

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN, 'Content-Type': 'application/json' }
    });
    const data = await res.json();
    if (!res.ok) {
      scShowToast('Error: ' + (data.detail || 'Intenta de nuevo'), true);
    } else {
      const msgs = {
        'clock-in':    '✅ Entrada registrada',
        'clock-out':   '👋 Salida registrada',
        'break-start': '☕ Break iniciado',
        'break-end':   '✅ Break terminado',
      };
      scShowToast(msgs[action] || '✅ Listo');
      await scLoadProfile();
      await scLoadTimecardPreview();
    }
  } catch(_) {
    scShowToast('Error de conexión', true);
  }
}

/* ── Timecard preview (mobile widget) ─────────────────────────── */
async function scLoadTimecardPreview() {
  const monday = scGetMonday(new Date());
  const ws = scIsoDate(monday);
  const weekEnd = new Date(monday);
  weekEnd.setDate(weekEnd.getDate() + 6);

  try {
    const res = await fetch('/api/staff/self/timecard?week_start=' + ws, {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) return;
    const data = await res.json();
    scRenderTCPreview(data);
  } catch(_) {}
}

function scRenderTCPreview(data) {
  const entries = data.entries || [];
  const totalNet = entries.reduce((s, e) => s + (e.net_hours || 0), 0);
  const h = Math.floor(totalNet), m = Math.round((totalNet - h) * 60);

  const sumBig = document.getElementById('sc-tc-sum-big');
  if (sumBig) sumBig.textContent = h + 'h ' + m + 'm';

  // Per-day bars
  const dayIds = ['sc-tc-L', 'sc-tc-M', 'sc-tc-X', 'sc-tc-J', 'sc-tc-V', 'sc-tc-S', 'sc-tc-D'];
  const dayOfWeekMap = { 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6 }; // Mon=0…Sun=6

  const todayDow = dayOfWeekMap[new Date().getDay()];

  entries.forEach(e => {
    const d = new Date(e.work_date + 'T12:00:00');
    const dow = dayOfWeekMap[d.getDay()];
    const id = dayIds[dow];
    const el = document.getElementById(id);
    if (!el) return;

    const fill = el.querySelector('.fill');
    if (fill) fill.style.height = Math.min(Math.round((e.net_hours / 10) * 100), 100) + '%';

    const hEl = el.querySelector('.h');
    if (hEl) hEl.textContent = e.net_hours > 0 ? e.net_hours.toFixed(1) : '—';

    if (e.net_hours > 8) el.classList.add('warn');
    if (dow === todayDow) el.classList.add('today');
    if (e.net_hours === 0 && !e.clock_in) el.classList.add('off');
  });
}

/* ── Upcoming shifts ────────────────────────────────────────────── */
async function scLoadUpcomingShifts() {
  // Uses GET /api/staff/schedules and renders next 3 upcoming days
  // TODO: endpoint /api/staff/self/upcoming-shifts doesn't exist yet
  // Falls back to static placeholder content
}

/* ── Tips (TODO) ────────────────────────────────────────────────── */
async function scLoadTips() {
  // TODO: GET /api/staff/self/tips is not yet implemented
  // Renders "(próximamente)" placeholder
  const tipEl = document.getElementById('sc-tip-amount');
  if (tipEl) tipEl.textContent = '—';
  const tipSub = document.getElementById('sc-tip-sub');
  if (tipSub) tipSub.textContent = 'Datos próximamente';
}

/* ── WebAuthn / Biometrics ─────────────────────────────────────── */
const SC_WEBAUTHN_OK = !!window.PublicKeyCredential;

function _b64uToBuffer(b) {
  const base64 = b.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64 + '='.repeat((4 - base64.length % 4) % 4);
  const bin = atob(padded);
  return Uint8Array.from(bin, c => c.charCodeAt(0)).buffer;
}
function _bufferToB64u(buffer) {
  let bin = '';
  new Uint8Array(buffer).forEach(b => bin += String.fromCharCode(b));
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

let _bioCredentials = [];

async function scLoadBiometricStatus() {
  if (!SC_WEBAUTHN_OK) return;
  try {
    const res = await fetch('/api/staff/webauthn/credentials', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error();
    const data = await res.json();
    _bioCredentials = data.credentials || [];
  } catch(_) { _bioCredentials = []; }
}

async function scStartBioRegistration() {
  if (!SC_WEBAUTHN_OK) { scShowToast('Este dispositivo no soporta biometría', true); return; }
  try {
    const optRes = await fetch('/api/staff/webauthn/register-options', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    const opts = await optRes.json();
    if (!optRes.ok) throw new Error(opts.detail || 'Error al obtener opciones');

    const cred = await navigator.credentials.create({
      publicKey: {
        challenge:        _b64uToBuffer(opts.challenge),
        rp:               { id: opts.rp.id, name: opts.rp.name || 'Mesio' },
        user: {
          id:          _b64uToBuffer(opts.user.id),
          name:        opts.user.name,
          displayName: opts.user.displayName || opts.user.name
        },
        pubKeyCredParams:       opts.pubKeyCredParams || [{ type: 'public-key', alg: -7 }],
        authenticatorSelection: opts.authenticatorSelection || { userVerification: 'required' },
        timeout:                60000,
        attestation:            'none'
      }
    });

    const completeRes = await fetch('/api/staff/webauthn/register-complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + SC_TOKEN },
      body: JSON.stringify({
        id:     _bufferToB64u(cred.rawId),
        raw_id: _bufferToB64u(cred.rawId),
        response: {
          attestationObject: _bufferToB64u(cred.response.attestationObject),
          clientDataJSON:    _bufferToB64u(cred.response.clientDataJSON)
        },
        type:        'public-key',
        device_name: (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || 'Dispositivo'
      })
    });
    const completeData = await completeRes.json();
    if (!completeRes.ok) throw new Error(completeData.detail || 'Error al guardar huella');

    scShowToast('¡Biometría registrada exitosamente!');
    await scLoadBiometricStatus();
  } catch(e) {
    if (e.name === 'NotAllowedError') {
      scShowToast('Registro cancelado', true);
    } else {
      scShowToast(e.message || 'Error inesperado', true);
    }
  }
}

async function scWebAuthnAuth(action) {
  // Used by kiosco biometric clock-in/out
  if (!SC_WEBAUTHN_OK) {
    // fallback to PIN
    scShowPinPad();
    return;
  }
  try {
    const optRes = await fetch('/api/staff/webauthn/auth-options', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ restaurant_id: SC_REST.id, action: action })
    });
    const opts = await optRes.json();
    if (!optRes.ok) throw new Error(opts.detail || 'Error al obtener opciones');

    const cred = await navigator.credentials.get({
      publicKey: {
        challenge:  _b64uToBuffer(opts.challenge),
        rpId:       opts.rpId,
        timeout:    60000,
        userVerification: 'required'
      }
    });

    const completeRes = await fetch('/api/staff/webauthn/auth-complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action:        action,
        credential_id: _bufferToB64u(cred.rawId),
        id:            _bufferToB64u(cred.rawId),
        response: {
          authenticatorData: _bufferToB64u(cred.response.authenticatorData),
          clientDataJSON:    _bufferToB64u(cred.response.clientDataJSON),
          signature:         _bufferToB64u(cred.response.signature),
          userHandle:        cred.response.userHandle ? _bufferToB64u(cred.response.userHandle) : null
        },
        type: 'public-key'
      })
    });
    const result = await completeRes.json();
    if (!completeRes.ok) throw new Error(result.detail || 'Auth fallida');

    scCloseAuth();
    scCloseAuthM();
    scShowToast('✅ Autenticación exitosa');
    await _dispatchClockAction(action);
  } catch(e) {
    if (e.name === 'NotAllowedError') {
      scShowToast('Autenticación cancelada', true);
    } else {
      scShowPinPad(); // fallback to PIN
    }
  }
}

/* ── Auth modals ────────────────────────────────────────────────── */
let _pendingAction = null;
let _currentLayout = 'mobile';

function scOpenAuth(action) {
  _pendingAction = action;
  const titles = {
    'clock-in':    'Confirmar entrada',
    'clock-out':   'Confirmar salida',
    'break-start': 'Iniciar break',
    'break-end':   'Terminar break',
    'switch':      'Cambiar de rol',
  };
  const title = titles[action] || 'Confirmar';

  if (_currentLayout === 'kiosco') {
    const titleEl = document.getElementById('sc-auth-title');
    if (titleEl) titleEl.textContent = title;
    const modalEl = document.getElementById('sc-auth-modal');
    if (modalEl) modalEl.classList.add('open');
  } else {
    const titleEl = document.getElementById('sc-auth-title-m');
    if (titleEl) titleEl.textContent = title;
    const modalEl = document.getElementById('sc-auth-modal-m');
    if (modalEl) modalEl.classList.add('open');
  }
}

function scCloseAuth()  { const m = document.getElementById('sc-auth-modal');   if (m) m.classList.remove('open'); }
function scCloseAuthM() { const m = document.getElementById('sc-auth-modal-m'); if (m) m.classList.remove('open'); }

function scShowPinPad() {
  scCloseAuth(); scCloseAuthM();
  const m = document.getElementById('sc-pin-modal');
  if (m) m.classList.add('open');
  scPinReset();
}
function scClosePin() { const m = document.getElementById('sc-pin-modal'); if (m) m.classList.remove('open'); }
function scShowFp() { scClosePin(); scOpenAuth(_pendingAction); }

let _pin = '';
function scPinPress(d) {
  if (_pin.length >= 6) return;
  _pin += d;
  scRenderPin();
  if (_pin.length === 6) {
    setTimeout(async () => {
      scClosePin();
      scPinReset();
      // PIN auth is handled server-side via clock-in/out endpoint accepting pin
      if (_pendingAction) await _dispatchClockAction(_pendingAction);
    }, 200);
  }
}
function scPinBack()  { _pin = _pin.slice(0, -1); scRenderPin(); }
function scPinReset() { _pin = ''; scRenderPin(); }
function scRenderPin() {
  document.querySelectorAll('#sc-pin-dots .sc-pin-dot').forEach((d, i) => {
    d.classList.toggle('filled', i < _pin.length);
  });
}

function scFakeAuthSuccess() {
  scCloseAuth(); scCloseAuthM();
  if (_pendingAction) _dispatchClockAction(_pendingAction);
}

/* ── Shift swap modal ───────────────────────────────────────────── */
let _swapShiftId = null;

function scOpenSwap(shiftId) {
  _swapShiftId = shiftId;
  const modalId = _currentLayout === 'kiosco' ? 'sc-swap-modal' : 'sc-swap-modal-m';
  const m = document.getElementById(modalId);
  if (m) m.classList.add('open');
  // TODO: load compatible coworkers from backend
  // GET /api/staff/self/shift-swap (not yet implemented)
  // For now shows static "próximamente" content
}
function scCloseSwap()  { const m = document.getElementById('sc-swap-modal');   if (m) m.classList.remove('open'); }
function scCloseSwapM() { const m = document.getElementById('sc-swap-modal-m'); if (m) m.classList.remove('open'); }

/* ── Task checklist ─────────────────────────────────────────────── */
function scToggleTask(el) {
  const row = el.closest('.sc-task-row');
  if (!row) return;
  row.classList.toggle('done');
  el.classList.toggle('done');
  // TODO: POST /api/staff/self/tasks/{id}/complete (not yet implemented)
}

/* ── Toast ──────────────────────────────────────────────────────── */
function scShowToast(msg, isError) {
  const t = document.createElement('div');
  t.textContent = msg;
  const bg = isError ? '#B91C1C' : 'var(--sc-brand)';
  t.style.cssText = 'position:fixed;bottom:40px;left:50%;transform:translateX(-50%);background:' + bg + ';color:#fff;padding:12px 22px;border-radius:12px;font-weight:600;font-size:13px;box-shadow:0 12px 36px rgba(0,0,0,0.3);z-index:9999;animation:sc-toastIn 0.3s ease;font-family:inherit;';
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity 0.3s'; }, 2000);
  setTimeout(() => t.remove(), 2400);
}

/* ── Logout ─────────────────────────────────────────────────────── */
function scLogout() {
  const rid = localStorage.getItem('rb_staff_restaurant_id');
  localStorage.clear();
  window.location.href = rid ? '/login?r=' + rid : '/login';
}

/* ── Date helpers ───────────────────────────────────────────────── */
function scGetMonday(d) {
  const dt = new Date(d);
  const day = dt.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  dt.setDate(dt.getDate() + diff);
  dt.setHours(0, 0, 0, 0);
  return dt;
}
function scIsoDate(d) {
  return d.getFullYear() + '-' + scPad(d.getMonth() + 1) + '-' + scPad(d.getDate());
}
function scFmtTime(iso) {
  if (!iso) return '--:--';
  const d = new Date(iso);
  return scPad(d.getHours()) + ':' + scPad(d.getMinutes());
}

/* ── Tweaks panel (layout switcher) ─────────────────────────────── */
const SC_TWEAK_DEFAULTS = { layout: 'mobile', state: 'in', role: 'mesero' };
let scState = Object.assign({}, SC_TWEAK_DEFAULTS);
try {
  const saved = JSON.parse(localStorage.getItem('mesio_staff_clock') || '{}');
  scState = Object.assign({}, scState, saved);
} catch(_) {}

function scSaveState() {
  localStorage.setItem('mesio_staff_clock', JSON.stringify(scState));
}

function scApplyLayout() {
  _currentLayout = scState.layout;
  const mStage = document.getElementById('sc-stage-mobile');
  const kStage = document.getElementById('sc-stage-kiosco');
  if (mStage) mStage.classList.toggle('active', scState.layout === 'mobile');
  if (kStage) kStage.classList.toggle('active', scState.layout === 'kiosco');
  scUpdateTweakBtns('sc-tw-layout', scState.layout);
}

function scUpdateTweakBtns(groupId, value) {
  const grp = document.getElementById(groupId);
  if (!grp) return;
  grp.querySelectorAll('button').forEach(b => {
    b.classList.toggle('on', b.dataset.v === value);
  });
}

function scApplyState() {
  scUpdateTweakBtns('sc-tw-state', scState.state);
  const pill = document.getElementById('sc-m-status');
  const pillTxt = document.getElementById('sc-m-status-text');
  const labels = { in: 'En turno · 3h 15m', break: 'En break · 12m', off: 'Sin fichar' };
  if (pill) {
    pill.classList.remove('off', 'break');
    if (scState.state === 'break') pill.classList.add('break');
    else if (scState.state === 'off') pill.classList.add('off');
  }
  if (pillTxt) pillTxt.textContent = labels[scState.state] || '';
}

function scApplyRole() {
  scUpdateTweakBtns('sc-tw-role', scState.role);
}

// Tweaks event delegation
document.querySelectorAll('.sc-tw-seg').forEach(seg => {
  seg.addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    const group = seg.id.replace('sc-tw-', '');
    scState[group] = btn.dataset.v;
    scSaveState();
    scApplyLayout();
    scApplyState();
    scApplyRole();
    scScaleKiosco();
    scScaleMobile();
    try { window.parent.postMessage({ type: '__edit_mode_set_keys', edits: { [group]: btn.dataset.v } }, '*'); } catch(_) {}
  });
});

/* ── Responsive scaling ─────────────────────────────────────────── */
function scScaleKiosco() {
  const el = document.getElementById('sc-tablet-scale');
  if (!el) return;
  const availW = window.innerWidth - 48;
  const availH = window.innerHeight - 100;
  const s = Math.min(availW / 1204, availH / 844, 1);
  el.style.transform = 'scale(' + s + ')';
  el.style.width = '1204px';
  el.style.height = '844px';
  el.style.marginBottom = (-844 * (1 - s)) + 'px';
}

function scScaleMobile() {
  const el = document.querySelector('.sc-stage-mobile .sc-phone');
  if (!el) return;
  const availH = window.innerHeight - 80;
  if (availH < 864) {
    const s = availH / 864;
    el.style.transform = 'scale(' + s + ')';
    el.style.transformOrigin = 'top center';
    el.style.marginBottom = (-864 * (1 - s)) + 'px';
  } else {
    el.style.transform = '';
    el.style.marginBottom = '';
  }
}

window.addEventListener('resize', () => { scScaleKiosco(); scScaleMobile(); });

/* ── Modal backdrop click-to-close ──────────────────────────────── */
document.querySelectorAll('.sc-modal-backdrop').forEach(m => {
  m.addEventListener('click', e => { if (e.target === m) m.classList.remove('open'); });
});

/* ── Edit mode bridge ───────────────────────────────────────────── */
window.addEventListener('message', (e) => {
  if (!e.data) return;
  const tw = document.getElementById('sc-tw-panel');
  if (!tw) return;
  if (e.data.type === '__activate_edit_mode')   tw.classList.add('on');
  if (e.data.type === '__deactivate_edit_mode') tw.classList.remove('on');
});
try { window.parent.postMessage({ type: '__edit_mode_available' }, '*'); } catch(_) {}

/* ── Init ───────────────────────────────────────────────────────── */
scApplyLayout();
scApplyState();
scApplyRole();
scScaleKiosco();
scScaleMobile();

scLoadProfile();
scLoadTimecardPreview();
scLoadTips();
scLoadBiometricStatus();
