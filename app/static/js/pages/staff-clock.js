/* ═══════════════════════════════════════════════════════════════
   STAFF CLOCK — interactions + backend wiring
   Produced by: staff-clock.html design → production port
   ═══════════════════════════════════════════════════════════════

   Backend endpoints wired:
     GET  /api/staff/self/profile              → loadProfile()
     POST /api/staff/self/clock-in             → doClockAction('clock-in')
     POST /api/staff/self/clock-out            → doClockAction('clock-out')
     POST /api/staff/self/break-start          → doClockAction('break-start')
     POST /api/staff/self/break-end            → doClockAction('break-end')
     GET  /api/staff/self/timecard             → loadTimecardPreview()
     GET  /api/staff/webauthn/credentials       → scLoadBiometricStatus()
     POST /api/staff/webauthn/register-options  → scStartBioRegistration()
     POST /api/staff/webauthn/register-complete → scStartBioRegistration()
     POST /api/staff/webauthn/auth-options      → scWebAuthnAuth() [public, kiosco]
     POST /api/staff/webauthn/auth-complete     → scWebAuthnAuth() [public, kiosco]

   Wired in this file:
     GET  /api/staff/self/announcements        → scLoadAnnouncements()
     GET  /api/staff/self/tasks                → scLoadTasks()
     POST /api/staff/self/tasks/{id}/complete  → scCompleteTask()
     GET  /api/staff/self/tips                 → scLoadTips()    (Sprint Y)
     GET  /api/staff/self/upcoming-shifts      → scLoadUpcomingShifts() (Sprint Y)

   Wired in Sprint Z:
     GET  /api/staff/self/performance          → scLoadPerformance()

   Wired in Sprint W:
     POST /api/staff/self/shift-swap                  → scSubmitShiftSwap()
     GET  /api/staff/self/shift-swap/incoming         → scLoadSwapRequests()
     GET  /api/staff/self/shift-swap/outgoing         → scLoadOutgoingSwaps()
     POST /api/staff/self/shift-swap/{id}/{accept|reject} → scHandleSwap()
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
    if (_scKioscoMode && _currentLayout !== 'kiosco') _scKioscoEnter();
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

const SC_DAY_ABBR = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom'];

async function scLoadUpcomingShifts() {
  const container = document.getElementById('sc-shifts-container');
  if (!container || !SC_TOKEN) return;

  try {
    const res = await fetch('/api/staff/self/upcoming-shifts?limit=3', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _scRenderUpcomingShifts(container, data.shifts || []);
  } catch(_) {
    // Non-critical — leave container with minimal fallback.
    container.innerHTML = '';
    const fallback = document.createElement('div');
    fallback.style.cssText = 'padding:14px;font-size:12px;color:var(--sc-text-3);';
    fallback.textContent = 'Sin turnos programados.';
    container.appendChild(fallback);
  }
}

function _scRenderUpcomingShifts(container, shifts) {
  container.innerHTML = '';

  if (!shifts.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'padding:14px;font-size:12px;color:var(--sc-text-3);';
    empty.textContent = 'Sin turnos programados.';
    container.appendChild(empty);
    return;
  }

  const todayStr    = scIsoDate(new Date());
  const tomorrowStr = scIsoDate(new Date(Date.now() + 86400000));

  shifts.forEach(shift => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:12px;padding:12px 14px;border-bottom:1px solid var(--sc-border,#E5E7EB);';

    // Left: date badge
    const dateObj = new Date(shift.date + 'T12:00:00');
    const dayNum  = dateObj.getDate();
    const dayAbbr = SC_DAY_ABBR[shift.day_of_week] || '';

    // Determine badge label: Hoy / Mañana / date
    let badgeLabel;
    if (shift.date === todayStr)    { badgeLabel = 'Hoy'; }
    else if (shift.date === tomorrowStr) { badgeLabel = 'Mañana'; }
    else { badgeLabel = dayAbbr + ' ' + dayNum; }

    const badge = document.createElement('div');
    badge.style.cssText = 'min-width:42px;text-align:center;';
    const badgeTop = document.createElement('div');
    badgeTop.style.cssText = 'font-family:var(--font-display,inherit);font-weight:700;font-size:18px;line-height:1;color:var(--sc-brand,#1D9E75);';
    badgeTop.textContent = String(dayNum);
    const badgeBot = document.createElement('div');
    badgeBot.style.cssText = 'font-size:10px;color:var(--sc-text-3,#9CA3AF);text-transform:uppercase;letter-spacing:.04em;';
    badgeBot.textContent = shift.date === todayStr ? 'Hoy' : (shift.date === tomorrowStr ? 'Mañ' : dayAbbr);
    badge.appendChild(badgeTop);
    badge.appendChild(badgeBot);

    // Right: hours + duration
    const info = document.createElement('div');
    info.style.cssText = 'flex:1;';
    const timeEl = document.createElement('div');
    timeEl.style.cssText = 'font-weight:600;font-size:13px;';
    const durationEl = document.createElement('span');
    durationEl.style.cssText = 'color:var(--sc-text-3,#9CA3AF);font-size:11px;font-weight:400;margin-left:6px;';
    durationEl.textContent = '· ' + shift.duration_hours + 'h';
    timeEl.textContent = shift.start_time + ' – ' + shift.end_time;
    timeEl.appendChild(durationEl);

    const badgeRow = document.createElement('div');
    if (shift.date === todayStr || shift.date === tomorrowStr) {
      badgeRow.style.cssText = 'display:inline-block;margin-top:3px;padding:1px 6px;border-radius:999px;font-size:10px;font-weight:600;background:rgba(29,158,117,.12);color:var(--sc-brand,#1D9E75);';
      badgeRow.textContent = badgeLabel;
    }

    info.appendChild(timeEl);
    if (badgeRow.textContent) info.appendChild(badgeRow);

    // "Cambiar" button — wires to shift swap modal
    const swapBtn = document.createElement('button');
    swapBtn.style.cssText = 'background:none;border:1px solid var(--sc-brand,#1D9E75);color:var(--sc-brand,#1D9E75);border-radius:8px;padding:4px 10px;font-size:11px;font-weight:600;cursor:pointer;flex-shrink:0;';
    swapBtn.textContent = 'Cambiar';
    // Store shift data on the button for the event listener
    swapBtn.dataset.shiftDate  = shift.date;
    swapBtn.dataset.shiftStart = shift.start_time || '';
    swapBtn.dataset.shiftEnd   = shift.end_time   || '';
    swapBtn.addEventListener('click', function() {
      scOpenSwap(this.dataset.shiftDate, this.dataset.shiftStart || null, this.dataset.shiftEnd || null);
    });

    row.appendChild(badge);
    row.appendChild(info);
    row.appendChild(swapBtn);
    container.appendChild(row);
  });
}

/* ── Tips ───────────────────────────────────────────────────────── */

function _scFmtCOP(n) {
  // Format a number as COP using mesioFmt if available, fallback to locale.
  if (typeof mesioFmt === 'function') return mesioFmt(n);
  return '$' + Math.round(n).toLocaleString('es-CO');
}

async function scLoadTips() {
  if (!SC_TOKEN) return;

  try {
    const res = await fetch('/api/staff/self/tips', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _scRenderTips(data);
  } catch(_) {
    // Non-critical — leave DOM in zero state.
    const tipEl = document.getElementById('sc-tip-amount');
    if (tipEl) tipEl.textContent = '—';
    const tipSub = document.getElementById('sc-tip-sub');
    if (tipSub) tipSub.textContent = 'Sin datos disponibles';
    const kTipEl = document.getElementById('sc-k-tip-amount');
    if (kTipEl) kTipEl.textContent = '—';
    const kTipSub = document.getElementById('sc-k-tip-sub');
    if (kTipSub) kTipSub.textContent = 'Sin datos disponibles';
  }
}

function _scRenderTips(data) {
  const todayAmt   = data.today       || 0;
  const todayCount = data.today_count || 0;
  const weekAmt    = data.week        || 0;
  const weekCount  = data.week_count  || 0;

  // Mobile: main tip amount widget
  const tipAmountEl = document.getElementById('sc-tip-amount');
  if (tipAmountEl) tipAmountEl.textContent = _scFmtCOP(todayAmt);

  const tipSubEl = document.getElementById('sc-tip-sub');
  if (tipSubEl) {
    if (todayAmt === 0 && weekAmt === 0) {
      tipSubEl.textContent = 'Sin propinas este turno';
    } else {
      tipSubEl.textContent = todayCount + (todayCount === 1 ? ' mesa hoy' : ' mesas hoy');
    }
  }

  // Mobile grid: mesas + pct indicator (use week data in sub-cells if available)
  const mesasEl = document.getElementById('sc-tip-mesas');
  if (mesasEl) mesasEl.textContent = todayCount > 0 ? String(todayCount) : '—';

  const ventasEl = document.getElementById('sc-tip-ventas');
  if (ventasEl) ventasEl.textContent = weekAmt > 0 ? _scFmtCOP(weekAmt) : '—';

  const pctEl = document.getElementById('sc-tip-pct');
  if (pctEl) {
    // Show week count as a context figure rather than leaving it blank
    pctEl.textContent = weekCount > 0 ? String(weekCount) + ' sem' : '—';
  }

  // Kiosco panel: tip amount
  const kTipEl = document.getElementById('sc-k-tip-amount');
  if (kTipEl) kTipEl.textContent = _scFmtCOP(todayAmt);

  const kTipSubEl = document.getElementById('sc-k-tip-sub');
  if (kTipSubEl) {
    if (todayAmt === 0 && weekAmt === 0) {
      kTipSubEl.textContent = 'Sin propinas este turno';
    } else {
      kTipSubEl.textContent = todayCount + (todayCount === 1 ? ' mesa · Semana: ' : ' mesas · Semana: ') + _scFmtCOP(weekAmt);
    }
  }
}

/* ── Performance ────────────────────────────────────────────────── */

async function scLoadPerformance() {
  const container = document.getElementById('sc-perf-container');
  if (!container || !SC_TOKEN) return;

  try {
    const res = await fetch('/api/staff/self/performance?days=30', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _scRenderPerformance(container, data);
  } catch(_) {
    // Non-critical — leave existing placeholder values in place.
  }
}

function _scRenderPerformance(container, data) {
  const m = (data && data.metrics) ? data.metrics : {};

  // Update the sub-header period label
  const subEl = container.querySelector('.sc-perf-sub');
  if (subEl) {
    subEl.textContent = 'Últimos ' + (data.period_days || 30) + ' días';
  }

  // Locate the three stat rows by their label text and update the value span.
  const rows = container.querySelectorAll('.sc-perf-row');
  rows.forEach(row => {
    const labelEl = row.querySelector('.l');
    const valueEl = row.querySelector('.v');
    if (!labelEl || !valueEl) return;

    const label = labelEl.textContent.trim();

    if (label === 'Propinas promedio / turno') {
      const amt = m.avg_tip_per_shift != null ? m.avg_tip_per_shift : 0;
      valueEl.textContent = _scFmtCOP(amt);

    } else if (label === 'NPS de tus mesas') {
      // Known limitation: nps_average stays null until nps_responses.table_session_id FK lands (CLAUDE.md §Pendientes #7)  // lint-allow: documented deferral
      // migration lands.  Show "—" with muted hint.
      if (m.nps_average != null) {
        valueEl.textContent = Number(m.nps_average).toFixed(1);
      } else {
        valueEl.textContent = '';
        const dash = document.createElement('span');
        dash.textContent = '—';
        const hint = document.createElement('span');
        hint.style.cssText = 'font-size:10px;color:var(--sc-text-3);font-weight:400;margin-left:4px;';
        hint.textContent = 'Sin reseñas en el período';
        valueEl.appendChild(dash);
        valueEl.appendChild(hint);
      }

    } else if (label === 'Ticket promedio') {
      const amt = m.avg_ticket != null ? m.avg_ticket : 0;
      valueEl.textContent = amt > 0 ? _scFmtCOP(amt) : '—';
    }
  });
}

/* ── Auth flow: WebAuthn gate ──────────────────────────────────── */

function _scFpState(modalId, state) {
  const modal   = document.getElementById(modalId);
  if (!modal) return;
  const wrap    = modal.querySelector('.sc-fp-wrap');
  const circle  = modal.querySelector('.sc-fp-circle');
  const label   = modal.querySelector('.sc-fp-label');
  const sub     = modal.querySelector('.sc-fp-sub');
  const confirm = modal.querySelector('.sc-modal-act:not(.ghost)');
  const usePin  = modal.querySelector('.sc-modal-act.ghost');

  if (!wrap) return;

  wrap.classList.remove('sc-fp-state-waiting', 'sc-fp-state-success', 'sc-fp-state-error');

  if (state === 'waiting') {
    wrap.classList.add('sc-fp-state-waiting');
    if (label) label.textContent = 'Esperando huella…';
    if (sub)   sub.textContent   = 'Acerca tu dedo o usa Face ID';
    if (circle) {
      circle.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2"><path d="M8 2C5 2 3 4 3 7v3M8 2c3 0 5 2 5 5v3"/><path d="M5 7c0-2 1-3 3-3s3 1 3 3v4"/><path d="M8 7v5M6.5 10v2M9.5 10v2"/></svg>';
    }
    if (confirm) confirm.style.display = 'none';
    if (usePin)  usePin.style.display  = '';
  } else if (state === 'success') {
    wrap.classList.add('sc-fp-state-success');
    if (label) label.textContent = 'Autenticado';
    if (sub)   sub.textContent   = '';
    if (circle) {
      circle.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 8l4 4 6-6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    }
    if (confirm) confirm.style.display = 'none';
    if (usePin)  usePin.style.display  = 'none';
  } else if (state === 'error') {
    wrap.classList.add('sc-fp-state-error');
    if (label) label.textContent = 'Error de autenticación';
    if (sub)   sub.textContent   = 'Intenta de nuevo o usa PIN';
    if (circle) {
      circle.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4l8 8M12 4l-8 8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    }
    if (confirm) confirm.style.display = 'none';
    if (usePin)  usePin.style.display  = '';
  } else {
    if (label) label.textContent = 'Esperando huella…';
    if (sub)   sub.textContent   = 'También Face ID disponible';
    if (circle) {
      circle.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2"><path d="M8 2C5 2 3 4 3 7v3M8 2c3 0 5 2 5 5v3"/><path d="M5 7c0-2 1-3 3-3s3 1 3 3v4"/><path d="M8 7v5M6.5 10v2M9.5 10v2"/></svg>';
    }
    if (confirm) confirm.style.display = 'none';
    if (usePin)  usePin.style.display  = '';
  }
}

async function _scStartAuthFlow(action) {
  if (!SC_WEBAUTHN_OK) {
    scShowToast('Este dispositivo no soporta biometría. Registra una credencial desde un dispositivo compatible.', true);
    scCloseAuth();
    scCloseAuthM();
    return;
  }
  if (_bioCredentials.length === 0) {
    scShowToast('No tienes huella registrada. Registra tu biometría primero.', true);
    scCloseAuth();
    scCloseAuthM();
    return;
  }
  const modalId = _currentLayout === 'kiosco' ? 'sc-auth-modal' : 'sc-auth-modal-m';
  _scFpState(modalId, 'waiting');
  try {
    await scWebAuthnAuth(action);
  } catch(_) {
    _scFpState(modalId, 'error');
  }
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
  const webAuthnAction = (action === 'break-start' || action === 'break-end') ? 'break' : action.replace('-', '_');

  const optRes = await fetch('/api/staff/webauthn/auth-options', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ restaurant_id: SC_REST.id, action: webAuthnAction })
  });
  const opts = await optRes.json();
  if (!optRes.ok) throw new Error(opts.detail || 'Error al obtener opciones de autenticación');

  const allowCreds = (opts.allowCredentials || []).map(c => ({
    type: c.type || 'public-key',
    id:   _b64uToBuffer(c.id),
    transports: c.transports || []
  }));

  const cred = await navigator.credentials.get({
    publicKey: {
      challenge:        _b64uToBuffer(opts.challenge),
      rpId:             opts.rpId,
      allowCredentials: allowCreds,
      timeout:          60000,
      userVerification: 'required'
    }
  });

  const completeRes = await fetch('/api/staff/webauthn/auth-complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      action:             webAuthnAction,
      credential_id:      _bufferToB64u(cred.rawId),
      authenticator_data: _bufferToB64u(cred.response.authenticatorData),
      client_data_json:   _bufferToB64u(cred.response.clientDataJSON),
      signature:          _bufferToB64u(cred.response.signature),
      user_handle:        cred.response.userHandle ? _bufferToB64u(cred.response.userHandle) : null
    })
  });
  const result = await completeRes.json();
  if (!completeRes.ok) throw new Error(result.detail || 'Verificación biométrica fallida');

  const modalId = _currentLayout === 'kiosco' ? 'sc-auth-modal' : 'sc-auth-modal-m';
  _scFpState(modalId, 'success');

  const staffName = result.staff_name ? _escHtml(result.staff_name) : '';
  const actionLabel = { clock_in: 'Entrada', clock_out: 'Salida', break: 'Break' }[webAuthnAction] || 'Acción';
  scShowToast((staffName ? staffName + ' — ' : '') + actionLabel + ' registrado');

  await new Promise(r => setTimeout(r, 900));

  scCloseAuth();
  scCloseAuthM();

  if (_scKioscoMode) {
    _scKioscoHandleAuthSuccess(action, result);
  } else {
    await _dispatchClockAction(action);
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

  const modalId = _currentLayout === 'kiosco' ? 'sc-auth-modal' : 'sc-auth-modal-m';
  _scFpState(modalId, 'idle');
  setTimeout(() => { _scStartAuthFlow(action); }, 200);
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
      scShowToast('PIN no soportado aún — registra tu huella biométrica para fichar.', true);
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

/* ── Kiosco mode ────────────────────────────────────────────────── */
const _scKioscoMode = new URLSearchParams(window.location.search).get('kiosco') === '1';

let _scKioscoIdleTimer = null;

function _scKioscoEnter() {
  const stageK = document.getElementById('sc-stage-kiosco');
  const stageM = document.getElementById('sc-stage-mobile');
  if (stageK) stageK.classList.add('active');
  if (stageM) stageM.classList.remove('active');
  _currentLayout = 'kiosco';
  document.body.classList.add('sc-kiosco-mode');
  _scKioscoResetIdle();
}

function _scKioscoResetIdle() {
  clearTimeout(_scKioscoIdleTimer);
  _scKioscoIdleTimer = setTimeout(() => {
    scCloseAuth();
    scCloseAuthM();
    scLoadProfile();
  }, 120000);
}

function _scKioscoHandleAuthSuccess(action, result) {
  const overlay = document.getElementById('sc-kiosco-success-overlay');
  if (overlay) {
    const nameEl = overlay.querySelector('.sc-ks-name');
    const actEl  = overlay.querySelector('.sc-ks-action');
    const staffName = result && result.staff_name ? result.staff_name : '';
    if (nameEl) nameEl.textContent = staffName;
    const actionLabels = { 'clock-in': 'Entrada registrada', 'clock_in': 'Entrada registrada', 'clock-out': 'Salida registrada', 'clock_out': 'Salida registrada', 'break-start': 'Break iniciado', 'break-end': 'Break terminado', 'break': 'Break registrado' };
    if (actEl) actEl.textContent = actionLabels[action] || 'Fichaje registrado';
    overlay.classList.add('visible');
    setTimeout(() => {
      overlay.classList.remove('visible');
      _pendingAction = null;
      scLoadProfile();
      _scKioscoResetIdle();
    }, 3000);
  } else {
    scShowToast('Fichaje registrado');
    _pendingAction = null;
    scLoadProfile();
    _scKioscoResetIdle();
  }
}

/* ── Shift swap modal ───────────────────────────────────────────── */
let _swapShiftDate      = null;
let _swapShiftStart     = null;
let _swapShiftEnd       = null;
let _swapCoworkers      = [];

function scOpenSwap(shiftDate, shiftStart, shiftEnd) {
  _swapShiftDate  = shiftDate  || null;
  _swapShiftStart = shiftStart || null;
  _swapShiftEnd   = shiftEnd   || null;

  const modalId = _currentLayout === 'kiosco' ? 'sc-swap-modal' : 'sc-swap-modal-m';
  const m = document.getElementById(modalId);
  if (!m) { return; }

  // Reset form
  const sel = m.querySelector('.sc-swap-target-sel');
  if (sel) sel.innerHTML = '<option value="">Cargando compañeros…</option>';
  const reasonEl = m.querySelector('.sc-swap-reason');
  if (reasonEl) reasonEl.value = '';

  m.classList.add('open');
  _scLoadCoworkersForSwap(m);
}

async function _scLoadCoworkersForSwap(modal) {
  const sel = modal.querySelector('.sc-swap-target-sel');
  if (!sel) return;
  try {
    const res = await fetch('/api/staff', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const staff = (data.staff || data || []).filter(s => {
      // Exclude self — compare by name (we don't have staff_id readily in profile cache)
      if (_profile && _profile.name && s.name === _profile.name) return false;
      return s.active !== false;
    });
    _swapCoworkers = staff;
    sel.innerHTML = '<option value="">— Selecciona compañero —</option>';
    staff.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = s.name + (s.role ? ' (' + s.role + ')' : '');
      sel.appendChild(opt);
    });
  } catch(_) {
    sel.innerHTML = '<option value="">Error al cargar compañeros</option>';
  }
}

async function scSubmitSwap(modalId) {
  const modal = document.getElementById(modalId);
  if (!modal) return;

  const sel      = modal.querySelector('.sc-swap-target-sel');
  const reasonEl = modal.querySelector('.sc-swap-reason');
  const targetId = sel ? sel.value : '';
  const reason   = reasonEl ? reasonEl.value.trim() : '';

  if (!targetId) { scShowToast('Selecciona un compañero para el cambio', true); return; }
  if (!_swapShiftDate) { scShowToast('No se encontró la fecha del turno', true); return; }

  const payload = {
    target_staff_id:  targetId,
    shift_date:       _swapShiftDate,
    shift_start_time: _swapShiftStart || undefined,
    shift_end_time:   _swapShiftEnd   || undefined,
    reason:           reason || undefined,
  };

  try {
    const res = await fetch('/api/staff/self/shift-swap', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Error ' + res.status);
    scShowToast('Solicitud enviada — pendiente de respuesta del compañero');
    modal.classList.remove('open');
    scLoadSwapOutgoing();
  } catch (e) {
    scShowToast(e.message || 'Error al enviar la solicitud', true);
  }
}

function scCloseSwap()  { const m = document.getElementById('sc-swap-modal');   if (m) m.classList.remove('open'); }
function scCloseSwapM() { const m = document.getElementById('sc-swap-modal-m'); if (m) m.classList.remove('open'); }

/* ── Incoming swap requests (for me as target) ──────────────────── */

async function scLoadSwapIncoming() {
  const container = document.getElementById('sc-swap-incoming-container');
  if (!container || !SC_TOKEN) return;
  try {
    const res = await fetch('/api/staff/self/shift-swap/incoming', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _scRenderSwapIncoming(container, data.swaps || []);
  } catch(_) {
    container.innerHTML = '';
  }
}

function _scRenderSwapIncoming(container, swaps) {
  container.innerHTML = '';
  const labelEl = document.getElementById('sc-swap-incoming-label');
  if (!swaps.length) {
    if (labelEl) labelEl.style.display = 'none';
    return;
  }
  if (labelEl) labelEl.style.display = '';

  swaps.forEach(s => {
    const card = document.createElement('div');
    card.style.cssText = 'background:var(--sc-surface-2,#F9FAFB);border-radius:10px;padding:10px 12px;margin-bottom:8px;';

    const titleEl = document.createElement('div');
    titleEl.style.cssText = 'font-weight:600;font-size:13px;margin-bottom:4px;';
    titleEl.textContent = (s.requester_name || 'Compañero') + ' quiere cambiar su turno contigo';

    const metaEl = document.createElement('div');
    metaEl.style.cssText = 'font-size:12px;color:var(--sc-text-3,#9CA3AF);margin-bottom:8px;';
    let metaTxt = 'Fecha: ' + (s.shift_date || '—');
    if (s.shift_start_time) metaTxt += '  ·  ' + s.shift_start_time + (s.shift_end_time ? '–' + s.shift_end_time : '');
    if (s.reason) metaTxt += '\nRazón: ' + s.reason;
    metaEl.textContent = metaTxt;

    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:6px;';

    const acceptBtn = document.createElement('button');
    acceptBtn.style.cssText = 'background:var(--sc-brand,#1D9E75);color:#fff;border:none;border-radius:8px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer;';
    acceptBtn.textContent = 'Aceptar';
    acceptBtn.addEventListener('click', () => _scRespondSwap(s.id, 'accept', container));

    const rejectBtn = document.createElement('button');
    rejectBtn.style.cssText = 'background:#FEE2E2;color:#991B1B;border:none;border-radius:8px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer;';
    rejectBtn.textContent = 'Rechazar';
    rejectBtn.addEventListener('click', () => _scRespondSwap(s.id, 'reject', container));

    btns.appendChild(acceptBtn);
    btns.appendChild(rejectBtn);

    card.appendChild(titleEl);
    card.appendChild(metaEl);
    card.appendChild(btns);
    container.appendChild(card);
  });
}

async function _scRespondSwap(swapId, action, container) {
  try {
    const res = await fetch('/api/staff/self/shift-swap/' + swapId + '/' + action, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN, 'Content-Type': 'application/json' },
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Error ' + res.status);
    const msg = action === 'accept'
      ? '✅ Solicitud aceptada — esperando aprobación del admin'
      : 'Solicitud rechazada';
    scShowToast(msg);
    scLoadSwapIncoming();
  } catch (e) {
    scShowToast(e.message || 'Error al responder', true);
  }
}

/* ── Outgoing swap requests (my requests) ───────────────────────── */

const _SC_SWAP_STATUS = {
  pending:          'Pendiente',
  target_accepted:  'Aceptado — esperando admin',
  target_rejected:  'Rechazado por compañero',
  admin_approved:   'Aprobado ✓',
  admin_rejected:   'Rechazado por admin',
  withdrawn:        'Retirado',
};

async function scLoadSwapOutgoing() {
  const container = document.getElementById('sc-swap-outgoing-container');
  if (!container || !SC_TOKEN) return;
  try {
    const res = await fetch('/api/staff/self/shift-swap/outgoing?limit=5', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _scRenderSwapOutgoing(container, data.swaps || []);
  } catch(_) {
    container.innerHTML = '';
  }
}

function _scRenderSwapOutgoing(container, swaps) {
  container.innerHTML = '';
  // Only show active/recent ones — filter out old terminal states
  const visible = swaps.filter(s => !['admin_approved', 'admin_rejected', 'withdrawn', 'target_rejected'].includes(s.status));
  if (!visible.length) return;

  const header = document.createElement('div');
  header.style.cssText = 'font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--sc-text-3,#9CA3AF);margin-bottom:6px;';
  header.textContent = 'Mis solicitudes de cambio';
  container.appendChild(header);

  visible.forEach(s => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--sc-border,#E5E7EB);font-size:12px;gap:8px;';

    const leftEl = document.createElement('div');
    const targetTxt = document.createElement('span');
    targetTxt.style.fontWeight = '600';
    targetTxt.textContent = s.target_name || 'Compañero';
    const dateTxt = document.createElement('span');
    dateTxt.style.cssText = 'color:var(--sc-text-3,#9CA3AF);margin-left:4px;';
    dateTxt.textContent = '· ' + (s.shift_date || '—');
    leftEl.appendChild(targetTxt);
    leftEl.appendChild(dateTxt);

    const statusEl = document.createElement('div');
    statusEl.style.cssText = 'font-size:11px;color:var(--sc-brand,#1D9E75);font-weight:600;white-space:nowrap;';
    statusEl.textContent = _SC_SWAP_STATUS[s.status] || s.status;

    row.appendChild(leftEl);
    row.appendChild(statusEl);
    container.appendChild(row);
  });
}

/* ── Announcements ──────────────────────────────────────────────── */
async function scLoadAnnouncements() {
  const mobileEl = document.getElementById('sc-annc-container');
  // No kiosko equivalent container in current HTML — mobile only.

  if (!SC_TOKEN) return;
  try {
    const res = await fetch('/api/staff/self/announcements', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const items = data.announcements || [];
    _scRenderAnnouncements(mobileEl, items);
  } catch(_) {
    // Non-critical — leave placeholder text in place.
  }
}

function _scRenderAnnouncements(container, items) {
  if (!container) return;
  container.innerHTML = '';
  if (!items.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'font-size:12px;color:var(--sc-text-3);padding:4px 0;';
    empty.textContent = '(no hay anuncios)';
    container.appendChild(empty);
    return;
  }
  items.forEach(ann => {
    const card = document.createElement('div');
    card.style.cssText = 'margin-bottom:8px;';

    const av = document.createElement('div');
    av.className = 'sc-annc-av';
    av.textContent = '📢';

    const body = document.createElement('div');
    body.className = 'sc-annc-body';

    const titleEl = document.createElement('div');
    titleEl.style.cssText = 'font-weight:600;font-size:13px;';
    titleEl.textContent = ann.title;

    const bodyEl = document.createElement('div');
    bodyEl.style.cssText = 'font-size:12px;color:var(--sc-text-3);margin-top:2px;white-space:pre-wrap;';
    bodyEl.textContent = ann.body;

    body.appendChild(titleEl);
    body.appendChild(bodyEl);

    card.style.cssText = 'display:flex;align-items:flex-start;gap:8px;';
    card.appendChild(av);
    card.appendChild(body);
    container.appendChild(card);
  });
}

/* ── Task checklist ─────────────────────────────────────────────── */
async function scLoadTasks() {
  const mobileEl = document.getElementById('sc-tasks-container');
  const kioscoEl = document.getElementById('sc-k-tasks');
  const countEl  = document.getElementById('sc-task-count');

  if (!SC_TOKEN) return;
  try {
    const res = await fetch('/api/staff/self/tasks', {
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN }
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const tasks = data.tasks || [];

    const done  = tasks.filter(t => t.completed_by_me).length;
    const total = tasks.length;
    if (countEl) countEl.textContent = total ? done + '/' + total + ' completadas' : '';

    _scRenderTasks(mobileEl, tasks);
    _scRenderTasks(kioscoEl, tasks, true);
  } catch(_) {
    // Non-critical — leave placeholder.
  }
}

function _scRenderTasks(container, tasks, compact) {
  if (!container) return;
  container.innerHTML = '';
  if (!tasks.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'padding:10px 14px;font-size:12px;color:var(--sc-text-3);';
    empty.textContent = '(no hay tareas pendientes)';
    container.appendChild(empty);
    return;
  }
  tasks.forEach(task => {
    const row = document.createElement('div');
    row.className = 'sc-task-row' + (task.completed_by_me ? ' done' : '');

    const cb = document.createElement('div');
    cb.className = 'sc-task-cb' + (task.completed_by_me ? ' done' : '');
    cb.setAttribute('role', 'checkbox');
    cb.setAttribute('aria-checked', task.completed_by_me ? 'true' : 'false');
    cb.setAttribute('tabindex', '0');
    if (!task.completed_by_me) {
      cb.addEventListener('click', () => scCompleteTask(task.id, cb, row));
      cb.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') scCompleteTask(task.id, cb, row); });
    }

    const label = document.createElement('div');
    label.className = 'sc-task-label';
    label.textContent = task.title;
    if (task.completed_by_me) label.style.textDecoration = 'line-through';

    row.appendChild(cb);
    row.appendChild(label);
    container.appendChild(row);
  });
}

async function scCompleteTask(taskId, cbEl, rowEl) {
  if (!SC_TOKEN) return;
  // Optimistic UI
  cbEl.classList.add('done');
  rowEl.classList.add('done');
  cbEl.setAttribute('aria-checked', 'true');
  const labelEl = rowEl.querySelector('.sc-task-label');
  if (labelEl) labelEl.style.textDecoration = 'line-through';

  try {
    const res = await fetch('/api/staff/self/tasks/' + taskId + '/complete', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + SC_TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    scShowToast('¡Tarea completada!');
    // Refresh counts
    scLoadTasks();
  } catch(_) {
    // Revert optimistic update on failure
    cbEl.classList.remove('done');
    rowEl.classList.remove('done');
    cbEl.setAttribute('aria-checked', 'false');
    if (labelEl) labelEl.style.textDecoration = '';
    scShowToast('Error al guardar. Intenta de nuevo.', true);
  }
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
scLoadUpcomingShifts();
scLoadPerformance();
scLoadBiometricStatus();
scLoadAnnouncements();
scLoadTasks();

// Auto-refresh every 60 seconds (tips + upcoming shifts added in Sprint Y;
// performance added in Sprint Z — refreshed once per minute like tips).
if (typeof mesioInterval === 'function') {
  mesioInterval(scLoadAnnouncements,    60000);
  mesioInterval(scLoadTasks,            60000);
  mesioInterval(scLoadTips,             60000);
  mesioInterval(scLoadUpcomingShifts,   60000);
  mesioInterval(scLoadPerformance,      60000);
} else {
  setInterval(scLoadAnnouncements,    60000);
  setInterval(scLoadTasks,            60000);
  setInterval(scLoadTips,             60000);
  setInterval(scLoadUpcomingShifts,   60000);
  setInterval(scLoadPerformance,      60000);
}
