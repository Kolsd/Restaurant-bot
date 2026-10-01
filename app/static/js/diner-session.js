/* ═══════════════════════════════════════════════════════════════════
   Mesio — Diner Session (chat pivot)

   Thin API wrapper for the unauthenticated diner-facing chat surface.
   The diner carries a per-table SESSION TOKEN issued by the backend
   (POST /api/diner/session after a QR scan) — not an admin/staff JWT —
   so this intentionally does NOT reuse mesioHeaders()/localStorage
   (those carry rb_token + X-Branch-ID for authenticated dashboard
   users). Session lives in sessionStorage, keyed by a fixed key: a
   fresh tab / new QR scan starts a clean conversation, a reload keeps
   the same one for as long as the tab stays open.

   Depends on nothing (no mesio-utils.js requirement) so it can load
   before or after the shared chrome scripts.

   IMPORTANT — this is NOT a Bearer-token API. app/routes/diner.py's
   request models carry `token` as a JSON body field (POST endpoints)
   or a query param (GET /api/diner/menu) — there is no Authorization
   header check anywhere in that router. dinerFetch() below folds the
   token into the right place per HTTP method so every call site can
   just pass the token positionally without knowing that detail.
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

var DINER_SESSION_KEY = 'mesio_diner_session';

function dinerGetTableToken() {
  var qp = new URLSearchParams(window.location.search);
  var fromQuery = qp.get('t') || qp.get('table') || qp.get('mesa');
  if (fromQuery) return fromQuery;
  // Path-based fallback for the QR URL form `/chat/{table_id}` (mirrors
  // catalog-v2.js's identical fallback for `/menu/{table_id}`). Query
  // params still win when present so a manual `?t=` link for testing
  // never gets shadowed by the path segment.
  var parts = window.location.pathname.split('/').filter(Boolean);
  var last = parts[parts.length - 1] || '';
  return last.toLowerCase() === 'chat' ? '' : last;
}

/**
 * Tells the ONE shared chat page (docs/claude/delivery-web.md chunk 5)
 * which entry point served it: the dine-in QR path `/chat/{table_id}` or
 * the public delivery/pickup link `/pedir/{organizations.slug}`. Query
 * params still win (manual `?t=` testing link), mirroring dinerGetTableToken.
 * Returns {mode: 'dine_in', tableId} or {mode: 'pedir', slug}.
 */
function dinerGetEntryMode() {
  var qp = new URLSearchParams(window.location.search);
  var fromQuery = qp.get('t') || qp.get('table') || qp.get('mesa');
  if (fromQuery) return { mode: 'dine_in', tableId: fromQuery };

  var parts = window.location.pathname.split('/').filter(Boolean);
  if (parts.length >= 2 && parts[0].toLowerCase() === 'pedir') {
    return { mode: 'pedir', slug: parts[1] };
  }
  var last = parts[parts.length - 1] || '';
  return { mode: 'dine_in', tableId: last.toLowerCase() === 'chat' ? '' : last };
}

var DINER_DEVICE_TOKEN_KEY = 'mesio_device_token';

/**
 * An opaque per-browser token, persisted in localStorage (survives across
 * sessions/tabs — unlike the sessionStorage-scoped diner session token
 * above), used ONLY as an additional per-IP-adjacent rate-limit dimension
 * on the public, unauthenticated delivery/pickup entry endpoints
 * (app/routes/diner_delivery.py module docstring: "the frontend is expected
 * to generate and persist its own opaque device token"). Never an identity,
 * never sent anywhere except those endpoints' own `device_token` field.
 */
function dinerGetDeviceToken() {
  try {
    var existing = localStorage.getItem(DINER_DEVICE_TOKEN_KEY);
    if (existing) return existing;
    var fresh = (window.crypto && typeof window.crypto.randomUUID === 'function')
      ? window.crypto.randomUUID()
      : 'dev-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    localStorage.setItem(DINER_DEVICE_TOKEN_KEY, fresh);
    return fresh;
  } catch (e) {
    // Storage unavailable (private mode / quota) — a fresh, unpersisted
    // token per call is still harmless: it only weakens rate-limit dimension,
    // never breaks a request.
    return 'dev-' + Date.now() + '-' + Math.random().toString(16).slice(2);
  }
}

var DINER_MEMORY_KEY = 'mesio_diner_memory_key';
var DINER_MEMORY_DECLINED_KEY = 'mesio_diner_memory_declined';

/**
 * "Recuérdame" (app/services/diner_memory.py): a random secret that exists
 * only once the diner said yes. The server keeps its hash as the profile key,
 * so whoever holds it sees that diner's past orders — hence crypto-random
 * only, never the Math.random fallback the rate-limit token above accepts,
 * and a separate key from that token (which lands in Redis key names).
 * create=false (every scan) returns '' for a browser that never consented.
 */
function dinerGetMemoryKey(create) {
  try {
    var existing = localStorage.getItem(DINER_MEMORY_KEY);
    if (existing) return existing;
    if (!create || !window.crypto || typeof window.crypto.getRandomValues !== 'function') return '';
    var bytes = new Uint8Array(24);
    window.crypto.getRandomValues(bytes);
    var fresh = Array.prototype.map.call(bytes, function (b) {
      return ('0' + b.toString(16)).slice(-2);
    }).join('');
    localStorage.setItem(DINER_MEMORY_KEY, fresh);
    return fresh;
  } catch (e) {
    // No storage, no memory: the diner stays anonymous, nothing breaks.
    return '';
  }
}

function dinerForgetMemoryKey() {
  try { localStorage.removeItem(DINER_MEMORY_KEY); } catch (e) { /* nothing stored */ }
}

/* "No, gracias" is remembered per restaurant (org) so the offer isn't repeated. */
function _dinerDeclinedOrgs() {
  try {
    var list = JSON.parse(localStorage.getItem(DINER_MEMORY_DECLINED_KEY) || '[]');
    return Array.isArray(list) ? list : [];
  } catch (e) {
    return [];
  }
}

function dinerMemoryDeclined(orgId) {
  return _dinerDeclinedOrgs().indexOf(String(orgId)) !== -1;
}

function dinerSetMemoryDeclined(orgId) {
  try {
    var list = _dinerDeclinedOrgs();
    if (list.indexOf(String(orgId)) === -1) list.push(String(orgId));
    localStorage.setItem(DINER_MEMORY_DECLINED_KEY, JSON.stringify(list.slice(-50)));
  } catch (e) {
    // Storage unavailable — the offer may show again; harmless.
  }
}

var DINER_CHECKOUT_PROFILE_KEY = 'mesio_delivery_checkout_profile';

/**
 * Remembers name/phone/address between visits (docs/claude/delivery-web.md
 * chunk 5: "Name, phone and address are remembered in localStorage for the
 * next visit"). Every read/write wrapped in try/catch per the same spec
 * ("localStorage read/write is wrapped in try/catch, because it can throw").
 */
function dinerLoadCheckoutProfile() {
  try {
    var raw = localStorage.getItem(DINER_CHECKOUT_PROFILE_KEY);
    if (!raw) return {};
    var parsed = JSON.parse(raw);
    return (parsed && typeof parsed === 'object') ? parsed : {};
  } catch (e) {
    return {};
  }
}

function dinerSaveCheckoutProfile(profile) {
  try {
    localStorage.setItem(DINER_CHECKOUT_PROFILE_KEY, JSON.stringify(profile || {}));
  } catch (e) {
    // Non-fatal — the next visit just won't be pre-filled.
  }
}

var DINER_LAST_ORDER_CODE_KEY = 'mesio_last_delivery_order_code';

function dinerSaveLastOrderCode(code) {
  try {
    localStorage.setItem(DINER_LAST_ORDER_CODE_KEY, code || '');
  } catch (e) {
    // Non-fatal.
  }
}

function dinerLoadLastOrderCode() {
  try {
    return localStorage.getItem(DINER_LAST_ORDER_CODE_KEY) || '';
  } catch (e) {
    return '';
  }
}

/**
 * Wraps navigator.geolocation.getCurrentPosition in a Promise that ALWAYS
 * resolves (never rejects) with one of:
 *   {ok: true, lat, lon}
 *   {ok: false, reason: 'unsupported' | 'denied' | 'timeout' | 'unavailable'}
 * so every caller handles every outcome explicitly (docs/claude/delivery-web.md
 * chunk 5: "Geolocation: handle every outcome ... without hanging the page").
 */
function dinerGetGeolocation(timeoutMs) {
  return new Promise(function (resolve) {
    if (!('geolocation' in navigator)) {
      resolve({ ok: false, reason: 'unsupported' });
      return;
    }
    var settled = false;
    var timer = setTimeout(function () {
      if (settled) return;
      settled = true;
      resolve({ ok: false, reason: 'timeout' });
    }, timeoutMs || 8000);

    navigator.geolocation.getCurrentPosition(
      function (pos) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve({ ok: true, lat: pos.coords.latitude, lon: pos.coords.longitude });
      },
      function (err) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        // PERMISSION_DENIED=1, POSITION_UNAVAILABLE=2, TIMEOUT=3
        var reason = (err && err.code === 1) ? 'denied'
          : (err && err.code === 3) ? 'timeout' : 'unavailable';
        resolve({ ok: false, reason: reason });
      },
      { enableHighAccuracy: false, timeout: timeoutMs || 8000, maximumAge: 60000 }
    );
  });
}

function dinerLoadSession() {
  try {
    var raw = sessionStorage.getItem(DINER_SESSION_KEY);
    if (!raw) return null;
    var parsed = JSON.parse(raw);
    if (!parsed || !parsed.token) return null;
    return parsed;
  } catch (e) {
    return null;
  }
}

function dinerSaveSession(session) {
  try {
    sessionStorage.setItem(DINER_SESSION_KEY, JSON.stringify(session));
  } catch (e) {
    // Storage unavailable (private mode / quota) — session stays in-memory
    // only for the life of this page load. Non-fatal.
  }
  // A diner who is actually seated also leaves a seat that outlives the tab.
  if (session && session.tableId && session.token && !session.needsJoin) {
    dinerSaveSeat(session.tableId, session.token);
  }
}

/* ── The seat: "this phone sits at this table" ───────────────────────
 * sessionStorage dies with the tab, so closing the browser — or the camera
 * app opening the QR in a new tab — made the diner a stranger at their own
 * table ("la mesa ya tiene un pedido activo"). The seat is the last token
 * this browser held at a table, kept in localStorage. It is NOT trusted by
 * itself: it is sent as `resume_token` and the server hands the seat back
 * only if that token still holds an ACTIVE session on that same table.
 * The age limit just stops a long-dead token from being sent at all. */
var DINER_SEAT_KEY = 'mesio_table_seat';
var DINER_SEAT_MAX_AGE_MS = 12 * 60 * 60 * 1000;

function dinerSaveSeat(tableId, token) {
  try {
    localStorage.setItem(DINER_SEAT_KEY, JSON.stringify({
      tableId: tableId, token: token, savedAt: Date.now(),
    }));
  } catch (e) {
    // Private mode / quota: the diner just loses the convenience.
  }
}

function dinerLoadSeat(tableId) {
  try {
    var raw = localStorage.getItem(DINER_SEAT_KEY);
    if (!raw) return null;
    var seat = JSON.parse(raw);
    if (!seat || !seat.token || seat.tableId !== tableId) return null;
    if (Date.now() - (Number(seat.savedAt) || 0) > DINER_SEAT_MAX_AGE_MS) return null;
    return seat;
  } catch (e) {
    return null;
  }
}

function dinerClearSession() {
  try {
    sessionStorage.removeItem(DINER_SESSION_KEY);
  } catch (e) {
    // no-op
  }
}

function dinerHeaders() {
  return { 'Content-Type': 'application/json' };
}

/**
 * Turns a non-2xx JSON body into an Error with a HUMAN message, whatever
 * shape `detail` came in as. Most of app/routes/diner.py raises
 * HTTPException(detail="plain string"), but the delivery/pickup checkout
 * refusals (app/routes/diner_delivery.py::_refusal) raise
 * detail={reason, message} on purpose — a machine-readable `reason` for the
 * page's own field-level branching PLUS a Spanish `message` for display
 * (chunk 5/checkout instructions: "shown as a clear Spanish message next to
 * the right field — never a raw error"). Before this fix `new Error(detail)`
 * on an OBJECT stringified to the useless literal "[object Object]" for
 * every one of those refusals — this was a bug in existing code found while
 * wiring the delivery checkout form, fixed here rather than worked around
 * at each call site. `err.reason` is attached (null when detail was a plain
 * string) so callers CAN branch on it without re-parsing the message text.
 */
function _dinerErrorFromResponse(data, status) {
  var detail = data && data.detail;
  var message;
  var reason = null;
  if (detail && typeof detail === 'object') {
    message = detail.message || detail.reason || ('HTTP ' + status);
    reason = detail.reason || null;
  } else {
    message = detail || (data && data.message) || ('HTTP ' + status);
  }
  var err = new Error(message);
  err.reason = reason;
  err.status = status;
  return err;
}

/**
 * Thin fetch wrapper for /api/diner/* endpoints. Throws Error(detail) on
 * a non-2xx response so callers can show a friendly message instead of a
 * silent failure — mirrors the _staffFetch convention in mesio-utils.js,
 * scoped to the diner's session token instead of an admin JWT.
 *
 * Token placement follows app/routes/diner.py's actual Pydantic models:
 * GET requests (only /api/diner/menu) get `?token=` appended to the query
 * string; every other method gets `token` merged into the JSON body
 * alongside whatever the caller already passed (POST /api/diner/session
 * has no token yet — callers pass token=null there and nothing is added).
 *
 * The four /api/diner/* endpoints are real, registered FastAPI routes
 * (see app/routes/diner.py, included in app/main.py) — no lint-allow
 * suppression needed at call sites; scripts/lint_frontend.py's FETCH
 * check resolves them against the live route table like any other path.
 */
async function dinerFetch(path, method, body, token) {
  var verb = (method || 'GET').toUpperCase();
  var url = path;
  var payload = body;

  if (token) {
    if (verb === 'GET' || verb === 'HEAD') {
      url += (path.indexOf('?') === -1 ? '?' : '&') + 'token=' + encodeURIComponent(token);
    } else {
      payload = Object.assign({}, body || {}, { token: token });
    }
  }

  var opts = { method: verb, headers: dinerHeaders() };
  if (payload !== undefined && payload !== null) opts.body = JSON.stringify(payload);
  var res = await fetch(url, opts);
  var data = null;
  try {
    data = await res.json();
  } catch (e) {
    // Empty or non-JSON body — leave data as null.
  }
  if (!res.ok) throw _dinerErrorFromResponse(data, res.status);
  return data;
}

/**
 * Multipart upload for POST /api/diner/delivery/payment-proof — the ONLY
 * /api/diner/* call that isn't JSON (FastAPI File/Form), so it can't go
 * through dinerFetch's JSON envelope. Same error-shape handling as above.
 */
async function dinerUploadProof(token, file) {
  var form = new FormData();
  form.append('token', token);
  form.append('file', file);
  var res = await fetch('/api/diner/delivery/payment-proof', { method: 'POST', body: form });
  var data = null;
  try {
    data = await res.json();
  } catch (e) {
    // Empty or non-JSON body — leave data as null.
  }
  if (!res.ok) throw _dinerErrorFromResponse(data, res.status);
  return data;
}

window.DinerSession = {
  getTableToken: dinerGetTableToken,
  getEntryMode: dinerGetEntryMode,
  getDeviceToken: dinerGetDeviceToken,
  getMemoryKey: dinerGetMemoryKey,
  forgetMemoryKey: dinerForgetMemoryKey,
  memoryDeclined: dinerMemoryDeclined,
  setMemoryDeclined: dinerSetMemoryDeclined,
  loadCheckoutProfile: dinerLoadCheckoutProfile,
  saveCheckoutProfile: dinerSaveCheckoutProfile,
  saveLastOrderCode: dinerSaveLastOrderCode,
  loadLastOrderCode: dinerLoadLastOrderCode,
  getGeolocation: dinerGetGeolocation,
  load: dinerLoadSession,
  save: dinerSaveSession,
  loadSeat: dinerLoadSeat,
  clear: dinerClearSession,
  headers: dinerHeaders,
  fetch: dinerFetch,
  uploadProof: dinerUploadProof,
};
