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
  if (!res.ok) {
    var detail = (data && (data.detail || data.message)) || ('HTTP ' + res.status);
    throw new Error(detail);
  }
  return data;
}

window.DinerSession = {
  getTableToken: dinerGetTableToken,
  load: dinerLoadSession,
  save: dinerSaveSession,
  clear: dinerClearSession,
  headers: dinerHeaders,
  fetch: dinerFetch,
};
