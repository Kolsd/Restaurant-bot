/* ═══════════════════════════════════════════════════
   Mesio — Realtime (SSE invalidation events)

   Thin client for the /api/staff/stream and /api/diner/stream endpoints
   (see docs/claude/frontend.md + the shared SSE contract). Events carry
   only ids/topic — never personal data — so every handler here just
   re-runs the loader it already had; this file never renders anything
   itself.

   EventSource can't send custom headers, and both endpoints require the
   caller's token in the Authorization header (never the URL), so this
   uses fetch() + a streamed ReadableStream reader instead of the native
   EventSource API.

   Usage:
     MesioRealtime.connect('/api/staff/stream', mesioHeaders);
     // mesioHeaders() already returns a full {Authorization, ...} object.
     var off = MesioRealtime.on('table_order.updated', loadOrders);
     var offResync = MesioRealtime.on('resync', loadOrders);
     // ...later, on unmount:
     off(); offResync();

     // Diner side — getToken here returns the raw session token string,
     // not a headers object; connect() wraps it as `Authorization: Bearer`.
     MesioRealtime.connect('/api/diner/stream', getToken);

   Plain script, no modules — exposes a single global `MesioRealtime`.
   One connection per page (staff-shell.js / diner-chat.js each call
   connect() once for the page's lifetime); sections/panels just
   on()/unsubscribe() against that single stream.
   ═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var MIN_BACKOFF_MS   = 1000;
  var MAX_BACKOFF_MS    = 30000;
  var HIDDEN_PAUSE_MS   = 60000;  // pause the stream after this long hidden
  var STALE_MS          = 45000;  // 3x the server's 15s heartbeat comment
  var COALESCE_MS       = 500;    // trailing debounce per handler function

  var _url = null;
  var _getToken = null;
  var _abortCtrl = null;
  var _reconnectTimer = null;
  var _hiddenTimer = null;
  var _staleTimer = null;
  var _backoff = MIN_BACKOFF_MS;
  var _hadFirstReady = false;   // becomes true after the very first "ready" frame
  var _stopped = true;          // true until connect() is called; true forever after disconnect()
  var _paused = false;          // true while hidden >60s — resumed on visibilitychange
  var _connected = false;
  var _connectGen = 0;          // bumped on every reconnect/stop to invalidate stale async loops
  var _handlers = Object.create(null); // topic -> Array<{raw, wrapped}>
  var _debounceMap = (typeof WeakMap !== 'undefined') ? new WeakMap() : null;

  // ── Trailing debounce, shared per raw handler fn (across every topic it's
  // subscribed to) so a burst of different-topic events for the same
  // section loader still only calls it once per 500ms window. ────────────
  function _debounced(fn) {
    if (!_debounceMap) return fn; // no WeakMap (very old browser) — no coalescing
    var existing = _debounceMap.get(fn);
    if (existing) return existing;
    var timer = null;
    var lastArg;
    var wrapped = function (evt) {
      lastArg = evt;
      if (timer) return;
      timer = setTimeout(function () {
        timer = null;
        fn(lastArg);
      }, COALESCE_MS);
    };
    _debounceMap.set(fn, wrapped);
    return wrapped;
  }

  function on(topic, fn) {
    if (!_handlers[topic]) _handlers[topic] = [];
    var entry = { raw: fn, wrapped: _debounced(fn) };
    _handlers[topic].push(entry);
    return function unsubscribe() {
      var list = _handlers[topic];
      if (!list) return;
      var idx = list.indexOf(entry);
      if (idx !== -1) list.splice(idx, 1);
    };
  }

  function _dispatch(topic, data) {
    var list = _handlers[topic];
    if (list) {
      list.slice().forEach(function (h) {
        try { h.wrapped(data); } catch (e) { console.error('MesioRealtime: handler error for', topic, e); }
      });
    }
    if (topic !== '*') {
      var star = _handlers['*'];
      if (star) {
        star.slice().forEach(function (h) {
          try { h.wrapped(data); } catch (e) { console.error('MesioRealtime: handler error for *', e); }
        });
      }
    }
  }

  // ── Live indicator DOM (opt-in via [data-mesio-live] so this doesn't
  // fight with per-section fetch-health indicators that reuse the same
  // .m-live-status class, e.g. kitchen/bar KDS footers). ─────────────────
  function _setLiveDom(online) {
    document.querySelectorAll('.m-live-status[data-mesio-live]').forEach(function (el) {
      el.classList.toggle('online', online);
      el.classList.toggle('offline', !online);
      var label = el.querySelector('.label');
      if (label) label.textContent = online ? 'En vivo' : 'Sin conexión';
    });
  }

  function _setConnected(v) {
    if (_connected === v) return;
    _connected = v;
    _setLiveDom(v);
  }

  // ── Auth headers — accepts either a full headers object (staff:
  // mesioHeaders()) or a plain bearer-token string (diner: getToken()). ──
  function _resolveHeaders() {
    var v = null;
    try { v = _getToken(); } catch (e) { v = null; }
    if (v && typeof v === 'object') return v;
    var h = {};
    if (v) h['Authorization'] = 'Bearer ' + v;
    return h;
  }

  function _clearStaleTimer() {
    if (_staleTimer) { clearTimeout(_staleTimer); _staleTimer = null; }
  }

  function _armStaleTimer() {
    _clearStaleTimer();
    _staleTimer = setTimeout(function () {
      // No frame/heartbeat for too long — the connection is likely dead
      // even though the browser hasn't noticed yet. Force a reconnect.
      if (_abortCtrl) { try { _abortCtrl.abort(); } catch (e) { /* already closed */ } }
    }, STALE_MS);
  }

  function _scheduleReconnect() {
    if (_stopped || _paused || _reconnectTimer) return;
    var jitter = Math.random() * 400;
    var delay = _backoff + jitter;
    _reconnectTimer = setTimeout(function () {
      _reconnectTimer = null;
      _backoff = Math.min(_backoff * 2, MAX_BACKOFF_MS);
      _open();
    }, delay);
  }

  function _handleFrame(raw) {
    if (!raw) return;
    var lines = raw.split('\n');
    var event = null;
    var dataStr = '';
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (!line || line.charAt(0) === ':') continue; // heartbeat comment (": ping")
      if (line.indexOf('event:') === 0) event = line.slice(6).trim();
      else if (line.indexOf('data:') === 0) dataStr += line.slice(5).trim();
    }
    if (!event) return;

    if (event === 'ready') {
      _backoff = MIN_BACKOFF_MS;
      _setConnected(true);
      if (_hadFirstReady) _dispatch('resync', {});
      _hadFirstReady = true;
      return;
    }

    var data = {};
    if (dataStr) {
      try { data = JSON.parse(dataStr); } catch (e) { data = {}; }
    }
    _dispatch(event, data); // covers real topics AND server-sent 'resync' (queue overflow)
  }

  async function _open() {
    if (_stopped || _paused) return;
    var myGen = ++_connectGen;
    var headers = _resolveHeaders();
    _abortCtrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;

    var res;
    try {
      res = await fetch(_url, {
        method: 'GET',
        headers: headers,
        signal: _abortCtrl ? _abortCtrl.signal : undefined,
        cache: 'no-store',
      });
    } catch (e) {
      if (myGen !== _connectGen) return;
      _setConnected(false);
      _scheduleReconnect();
      return;
    }
    if (myGen !== _connectGen) return;
    if (!res.ok || !res.body) {
      _setConnected(false);
      _scheduleReconnect();
      return;
    }

    var reader = res.body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buf = '';
    _armStaleTimer();

    try {
      for (;;) {
        var chunk = await reader.read();
        if (myGen !== _connectGen) { try { reader.cancel(); } catch (e) { /* already gone */ } return; }
        if (chunk.done) break;
        _armStaleTimer();
        buf += decoder.decode(chunk.value, { stream: true });
        var frames = buf.split('\n\n');
        buf = frames.pop(); // last (possibly incomplete) frame stays buffered
        for (var i = 0; i < frames.length; i++) _handleFrame(frames[i]);
      }
    } catch (e) {
      // aborted (stale/hidden/disconnect) or network drop — reconnect below
    }

    _clearStaleTimer();
    if (myGen !== _connectGen) return;
    _setConnected(false);
    _scheduleReconnect();
  }

  function _onVisibilityChange() {
    if (document.visibilityState === 'hidden') {
      if (_hiddenTimer || _stopped) return;
      _hiddenTimer = setTimeout(function () {
        _hiddenTimer = null;
        _paused = true;
        _connectGen++; // invalidate the in-flight reader loop, if any
        if (_abortCtrl) { try { _abortCtrl.abort(); } catch (e) { /* already closed */ } }
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
        _setConnected(false);
      }, HIDDEN_PAUSE_MS);
      return;
    }
    if (_hiddenTimer) { clearTimeout(_hiddenTimer); _hiddenTimer = null; }
    if (_paused && !_stopped) {
      _paused = false;
      _backoff = MIN_BACKOFF_MS;
      _open(); // reconnect; the resulting "ready" frame emits a synthetic resync
    }
  }

  function connect(url, getToken) {
    _url = url;
    _getToken = getToken;
    _stopped = false;
    _paused = false;
    _hadFirstReady = false;
    _backoff = MIN_BACKOFF_MS;
    document.addEventListener('visibilitychange', _onVisibilityChange);
    _open();
    return window.MesioRealtime;
  }

  function disconnect() {
    _stopped = true;
    _connectGen++;
    if (_abortCtrl) { try { _abortCtrl.abort(); } catch (e) { /* already closed */ } }
    if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
    if (_hiddenTimer) { clearTimeout(_hiddenTimer); _hiddenTimer = null; }
    _clearStaleTimer();
    document.removeEventListener('visibilitychange', _onVisibilityChange);
    _setConnected(false);
  }

  window.MesioRealtime = {
    connect: connect,
    disconnect: disconnect,
    on: on,
    get connected() { return _connected; },
  };
})();
