/* ════════════════════════════════════════════════════
   orchestrator.js · Runtime
   - Reproduce escenarios con timeline absoluto
   - Pausa/resume/restart
   - Render reactivo a MesioBus events
   - Expone window.loadScenario(key) (compat)
   ════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var Bus = window.MesioBus;
  var Demo = window.MesioDemo;
  var SCENARIOS = window.MesioScenarios;

  // ─── DOM refs ─────────────────────────────────────
  var $chat = document.getElementById('wa-chat');
  var $typing = document.getElementById('wa-typing');
  var $ticker = document.getElementById('dash-ticker');
  var $floor = document.getElementById('floor');
  var $kds = document.getElementById('kds');
  var $kdsCount = document.getElementById('kds-count');
  var $floorActive = document.getElementById('floor-active');
  var $floorFree = document.getElementById('floor-free');
  var $connectorPulse = document.getElementById('connector-pulse');
  var $btnPlay = document.getElementById('btn-play');
  var $btnRestart = document.getElementById('btn-restart');
  var $btnMode = document.getElementById('btn-mode');
  var $modeLabel = document.getElementById('mode-label');
  var $icoPlay = document.getElementById('ico-play');
  var $icoPause = document.getElementById('ico-pause');
  var $tScenario = document.getElementById('t-scenario');
  var $tProgress = document.getElementById('t-progress-fill');
  var $tElapsed = document.getElementById('t-elapsed');
  var $tTotal = document.getElementById('t-total');
  var $clock = document.getElementById('dash-clock');
  var $ctaDock = document.getElementById('cta-dock');
  var $ctaClose = document.getElementById('cta-dock-close');
  var $btnShare = document.getElementById('btn-share');
  var $ctaShare = document.getElementById('cta-share');
  var $toast = document.getElementById('toast');

  // ─── State ────────────────────────────────────────
  var current = null;        // scenario object
  var currentIdx = 0;        // index in ORDER
  var startWall = 0;         // wall-clock ms when (re)started
  var elapsedAcc = 0;        // accumulated ms before pause
  var playing = false;
  var raf = null;
  var stepIdx = 0;
  var ctaShown = false;
  var mode = 'auto';         // 'auto' | 'loop'
  var SCENE_ORDER = window.MesioScenarioOrder || ['mesa'];

  // ─── Initial render ───────────────────────────────
  renderFloor();

  // ─── Bus listeners (declarative reactive UI) ──────
  Bus.on('metric:change', function (e) {
    var elId = ({
      revenue: 'm-revenue',
      orders: 'm-orders',
      activeTables: 'm-tables',
      ticket: 'm-ticket'
    })[e.key];
    if (!elId) return;
    var $el = document.getElementById(elId);
    if (!$el) return;
    $el.textContent = formatNum(e.key, e.value);
    $el.classList.remove('tick');
    void $el.offsetWidth; // restart anim
    $el.classList.add('tick');
    var $tile = $el.closest('.metric');
    if ($tile) {
      $tile.classList.add('flash');
      setTimeout(function () { $tile.classList.remove('flash'); }, 900);
    }
    if (e.key === 'orders') {
      var $d = document.getElementById('m-orders-delta');
      if ($d) $d.textContent = '+' + (e.value - 27 + 3) + ' hoy';
    }
    if (e.key === 'activeTables') {
      var $td = document.getElementById('m-tables-delta');
      if ($td) $td.textContent = (8 - e.value) + ' libres';
    }
  });

  Bus.on('floor:occupy', function (e) {
    var $t = $floor.querySelector('[data-num="' + e.num + '"]');
    if (!$t) return;
    $t.classList.add('occupied', 'flash');
    setTimeout(function () { $t.classList.remove('flash'); }, 900);
    var active = Demo.state.floor.filter(function (x) { return x.occupied; }).length;
    $floorActive.textContent = active;
    $floorFree.textContent = 8 - active;
    Demo.setMetric('activeTables', active);
  });

  Bus.on('floor:release', function (e) {
    var $t = $floor.querySelector('[data-num="' + e.num + '"]');
    if (!$t) return;
    $t.classList.remove('occupied');
    var active = Demo.state.floor.filter(function (x) { return x.occupied; }).length;
    $floorActive.textContent = active;
    $floorFree.textContent = 8 - active;
  });

  Bus.on('kds:add', function (e) {
    var t = e.ticket;
    var $card = document.createElement('div');
    $card.className = 'kds-ticket new';
    var lines = t.items.map(function (it) { return '<div class="kds-line">' + it + '</div>'; }).join('');
    $card.innerHTML =
      '<div class="kds-ticket-head">' +
        '<span class="kds-mesa">Mesa ' + t.mesa + '</span>' +
        '<span class="kds-time">ahora</span>' +
      '</div>' + lines;
    $kds.insertBefore($card, $kds.firstChild);
    setTimeout(function () { $card.classList.remove('new'); }, 600);
    $kdsCount.textContent = $kds.querySelectorAll('.kds-ticket').length;
  });

  Bus.on('inventory:change', function (e) {
    if (e.key === 'frijoles') {
      var $f = document.getElementById('inv-frijoles');
      var $bar = document.getElementById('inv-frijoles-fill');
      if ($f) $f.textContent = e.stock.toFixed(1);
      if ($bar) {
        var pct = Math.min(100, (e.stock / e.item.min) * 50); // 100% = 2x min
        $bar.style.width = Math.max(8, pct) + '%';
      }
    }
  });

  // ─── Floor render ─────────────────────────────────
  function renderFloor() {
    $floor.innerHTML = '';
    Demo.state.floor.forEach(function (t) {
      var $t = document.createElement('div');
      $t.className = 'floor-table' + (t.occupied ? ' occupied' : '');
      $t.setAttribute('data-num', t.num);
      $t.innerHTML = '<div class="ft-num">' + t.num + '</div><div>mesa</div>';
      $floor.appendChild($t);
    });
  }

  // ─── Helpers ──────────────────────────────────────
  function formatNum(key, val) {
    if (key === 'revenue' || key === 'ticket') {
      return val.toLocaleString('es-CO');
    }
    return val.toString();
  }
  function formatTime(ms) {
    var s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }

  function pulseConnector() {
    $connectorPulse.classList.remove('active');
    void $connectorPulse.offsetWidth;
    $connectorPulse.classList.add('active');
  }

  function showTicker(emoji, label) {
    $ticker.innerHTML =
      '<div class="ticker-event">' +
        '<span class="ticker-emoji">' + emoji + '</span>' +
        '<span><b>' + label + '</b></span>' +
        '<span class="ticker-time">justo ahora</span>' +
      '</div>';
  }

  function showToast(msg) {
    $toast.textContent = msg;
    $toast.hidden = false;
    clearTimeout($toast._t);
    $toast._t = setTimeout(function () { $toast.hidden = true; }, 2200);
  }

  // ─── Step executor ────────────────────────────────
  function runStep(step) {
    if (step.type === 'msg-out' || step.type === 'msg-in') {
      $typing.hidden = true;
      var $m = document.createElement('div');
      $m.className = 'wa-msg ' + (step.type === 'msg-in' ? 'wa-msg-in' : 'wa-msg-out');
      var ticks = step.type === 'msg-out' ? '<span class="ticks">✓✓</span>' : '';
      $m.innerHTML = step.text +
        '<span class="wa-msg-time">' + (step.time || '') + ' ' + ticks + '</span>';
      $chat.appendChild($m);
      $chat.scrollTop = $chat.scrollHeight;
    }
    else if (step.type === 'typing') {
      $typing.hidden = false;
      $chat.scrollTop = $chat.scrollHeight;
      setTimeout(function () { $typing.hidden = true; }, step.duration || 800);
    }
    else if (step.type === 'quick') {
      var $q = document.createElement('div');
      $q.className = 'wa-quick';
      $q.innerHTML = step.chips.map(function (c) {
        return '<button type="button">' + c.text + '</button>';
      }).join('');
      $chat.appendChild($q);
      $chat.scrollTop = $chat.scrollHeight;
    }
    else if (step.type === 'dash-event') {
      pulseConnector();
      showTicker(step.emoji || '✦', step.label || '');
      if (step.floor) Demo.occupyTable(step.floor);
      if (step.kds) Demo.pushKds(step.kds);
      if (step.metrics) {
        Object.keys(step.metrics).forEach(function (k) {
          Demo.bumpMetric(k, step.metrics[k]);
        });
      }
      if (step.inventory) {
        step.inventory.forEach(function (i) { Demo.consumeInventory(i.key, i.amount); });
      }
    }
  }

  // ─── Reset (between loops / restart) ──────────────
  function resetView() {
    // Clear chat (keep "hoy" divider)
    var divider = $chat.querySelector('.wa-day');
    $chat.innerHTML = '';
    if (divider) $chat.appendChild(divider);
    $typing.hidden = true;

    // Reset state
    Demo.state.metrics = { revenue: 412000, orders: 27, activeTables: 4, ticket: 38500 };
    document.getElementById('m-revenue').textContent = '412.000';
    document.getElementById('m-orders').textContent = '27';
    document.getElementById('m-tables').textContent = '4';
    document.getElementById('m-ticket').textContent = '38.500';
    document.getElementById('m-orders-delta').textContent = '+3 hoy';
    document.getElementById('m-tables-delta').textContent = '4 libres';

    Demo.state.floor.forEach(function (t) {
      t.occupied = (t.num === 2 || t.num === 4 || t.num === 7 || t.num === 8);
    });
    renderFloor();
    $floorActive.textContent = '4';
    $floorFree.textContent = '8';

    // Reset KDS
    $kds.innerHTML =
      '<div class="kds-ticket"><div class="kds-ticket-head"><span class="kds-mesa">Mesa 4</span><span class="kds-time">3 min</span></div><div class="kds-line">3× Ajiaco Bogotano</div><div class="kds-line">3× Cerveza Club</div></div>' +
      '<div class="kds-ticket"><div class="kds-ticket-head"><span class="kds-mesa">Mesa 8</span><span class="kds-time">1 min</span></div><div class="kds-line">1× Cazuela de Mariscos</div></div>';
    $kdsCount.textContent = '2';

    // Reset inventory
    Demo.state.inventory.frijoles.stock = 3.0;
    document.getElementById('inv-frijoles').textContent = '3.0';
    document.getElementById('inv-frijoles-fill').style.width = '60%';

    $ticker.innerHTML = '<div class="ticker-empty">Esperando actividad…</div>';
    stepIdx = 0;
    elapsedAcc = 0;
  }

  // ─── Transport ────────────────────────────────────
  function loadScenario(key) {
    var idx = SCENE_ORDER.indexOf(key);
    currentIdx = idx >= 0 ? idx : 0;
    current = SCENARIOS[key] || SCENARIOS[SCENE_ORDER[0]];
    if (!current) return;
    $tScenario.textContent = (currentIdx + 1) + ' · ' + current.label;
    $tTotal.textContent = formatTime(current.duration);
    $tElapsed.textContent = '0:00';
    $tProgress.style.width = '0%';
    resetView();
    updateSceneDots();
  }

  function loadNext() {
    var nextIdx = (currentIdx + 1) % SCENE_ORDER.length;
    loadScenario(SCENE_ORDER[nextIdx]);
  }

  function updateSceneDots() {
    var $dots = document.getElementById('t-dots');
    if (!$dots) return;
    var nodes = $dots.querySelectorAll('.t-dot');
    nodes.forEach(function (n, i) {
      n.classList.toggle('active', i === currentIdx);
      n.classList.toggle('done', i < currentIdx);
    });
  }

  function play() {
    if (!current) return;
    if (stepIdx >= current.steps.length && elapsedAcc >= current.duration) {
      // restart from beginning
      resetView();
    }
    playing = true;
    startWall = performance.now();
    $icoPlay.hidden = true;
    $icoPause.hidden = false;
    // Single timing source: setInterval. Reliable in iframes, background tabs, etc.
    if (tickFallback) clearInterval(tickFallback);
    tickFallback = setInterval(tick, 50);
    tick(); // immediate first tick to fire at:0 step
  }

  function pause() {
    if (!playing) return;
    elapsedAcc += performance.now() - startWall;
    playing = false;
    if (tickFallback) { clearInterval(tickFallback); tickFallback = null; }
    $icoPlay.hidden = false;
    $icoPause.hidden = true;
  }

  function togglePlay() {
    if (playing) pause(); else play();
  }

  function restart() {
    pause();
    resetView();
    setTimeout(play, 100);
  }

  var tickFallback = null;
  function tick() {
    if (!playing) return;
    var elapsed = elapsedAcc + (performance.now() - startWall);

    // Fire any due steps
    while (stepIdx < current.steps.length && current.steps[stepIdx].at <= elapsed) {
      runStep(current.steps[stepIdx]);
      stepIdx++;
    }

    // Progress
    var pct = Math.min(100, (elapsed / current.duration) * 100);
    $tProgress.style.width = pct + '%';
    $tElapsed.textContent = formatTime(Math.min(elapsed, current.duration));

    if (elapsed >= current.duration) {
      pause();
      var isLast = (currentIdx === SCENE_ORDER.length - 1);
      // Show CTA after last scene completes (one-shot)
      if (isLast && !ctaShown) {
        ctaShown = true;
        setTimeout(function () { $ctaDock.hidden = false; }, 600);
        // Reveal the live-chat FAB so prospects can talk to the real bot.
        // demo-chat.js listens for this event.
        window.dispatchEvent(new CustomEvent('demo:scenes-complete'));
      }
      // Auto: chain to next scene; Loop: restart current; Last+Auto: stop
      if (mode === 'loop') {
        setTimeout(restart, 1200);
      } else if (!isLast) {
        setTimeout(function () {
          loadNext();
          play();
        }, 900);
      }
      return;
    }
  }

  // ─── Wire controls ────────────────────────────────
  $btnPlay.addEventListener('click', togglePlay);
  $btnRestart.addEventListener('click', restart);
  $btnMode.addEventListener('click', function () {
    mode = (mode === 'auto') ? 'loop' : 'auto';
    $modeLabel.textContent = (mode === 'auto') ? 'Auto' : 'Loop';
  });

  // Scene picker dots
  var $dots = document.getElementById('t-dots');
  if ($dots) {
    $dots.addEventListener('click', function (e) {
      var btn = e.target.closest('.t-dot');
      if (!btn) return;
      var key = btn.getAttribute('data-key');
      var wasPlaying = playing;
      pause();
      loadScenario(key);
      if (wasPlaying) setTimeout(play, 100); else play();
    });
  }

  $ctaClose.addEventListener('click', function () { $ctaDock.hidden = true; });

  function copyShareUrl() {
    var url = location.origin + location.pathname + '?scenario=mesa&autoplay=1';
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () {
        showToast('Link copiado · listo para compartir');
      }).catch(function () { showToast(url); });
    } else {
      showToast(url);
    }
  }
  $btnShare.addEventListener('click', copyShareUrl);
  $ctaShare.addEventListener('click', copyShareUrl);

  // Keyboard: space toggles play (avoid when input focused — none here)
  document.addEventListener('keydown', function (e) {
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    if (e.key === 'r' || e.key === 'R') restart();
  });

  // ─── Clock (cosmetic) ─────────────────────────────
  function updateClock() {
    var d = new Date();
    var h = d.getHours(), m = d.getMinutes();
    $clock.textContent = h + ':' + ('0' + m).slice(-2);
  }
  updateClock();
  setInterval(updateClock, 30000);

  // ─── Public compat ────────────────────────────────
  window.loadScenario = loadScenario;

  // ─── Boot ─────────────────────────────────────────
  loadScenario('mesa');

  // Try immediate autoplay
  setTimeout(play, 200);

  // Defensive: some embed/iframe contexts throttle timers until first user gesture.
  // Wake the timeline on ANY interaction signal if it hasn't visibly progressed yet.
  var bootGuard = function () {
    if (!playing) return; // paused by user — respect that
    var elapsed = elapsedAcc + (playing ? performance.now() - startWall : 0);
    if (elapsed < 200 && stepIdx < 2) {
      // Re-prime: pause then re-start (resets interval in current execution context)
      pause();
      play();
    }
  };
  ['pointerdown', 'keydown', 'scroll', 'visibilitychange', 'focus', 'touchstart'].forEach(function (ev) {
    window.addEventListener(ev, bootGuard, { once: true, passive: true });
    document.addEventListener(ev, bootGuard, { once: true, passive: true });
  });
})();
