/* ════════════════════════════════════════════════════
   Mesio · Dashboard demo · live ticker + tour + dock
   ════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ───────── State ─────────
  const state = {
    revenue: 438250,
    orders: 31,
    nps: 62,
    ticket: 42300,
    floorOccupied: new Set([2, 4, 7, 8]), // table numbers occupied initially
    floorTotal: 12,
    nextOrderId: 1043,
    dockShown: false,
    tourActive: false,
  };

  // ───────── Floor plan ─────────
  function renderFloor() {
    const floor = document.getElementById('floor');
    if (!floor) return;
    floor.innerHTML = '';
    for (let i = 1; i <= state.floorTotal; i++) {
      const cell = document.createElement('div');
      cell.className = 'table-cell' + (state.floorOccupied.has(i) ? ' occupied' : '');
      cell.dataset.table = String(i);
      cell.innerHTML =
        '<span class="table-num">' + i + '</span>' +
        '<span class="table-status">' + (state.floorOccupied.has(i) ? 'ocupada' : 'libre') + '</span>';
      floor.appendChild(cell);
    }
    document.getElementById('floor-active').textContent = String(state.floorOccupied.size);
  }

  // ───────── Number animation ─────────
  function animateNumber(el, from, to, format) {
    const dur = 700;
    const t0 = performance.now();
    function tick(t) {
      const p = Math.min(1, (t - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      const v = from + (to - from) * eased;
      el.textContent = format(v);
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  const fmtMoney = v => Math.round(v).toLocaleString('es-CO');
  const fmtInt = v => String(Math.round(v));
  const fmtSign = v => (v >= 0 ? '+' : '') + Math.round(v);

  // ───────── Toast ─────────
  let toastTimer = null;
  function toast(msg) {
    const el = document.getElementById('toast');
    el.innerHTML = '<span class="live-dot"></span>' + msg;
    el.hidden = false;
    el.classList.remove('out');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.classList.add('out');
      setTimeout(() => { el.hidden = true; }, 320);
    }, 3200);
  }

  // ───────── Ticker (new orders cascade) ─────────
  const orderTemplates = [
    { ch: 'mesa', where: 'Mesa 11', items: '2× Empanadas · 1× Pollo asado · 2× Limonada', amount: 38900, status: 'nuevo', statusText: 'Nuevo' },
    { ch: 'domicilio', where: 'María L. · Usaquén', items: '1× Sancocho · 1× Arepa con queso', amount: 32500, status: 'nuevo', statusText: 'Nuevo · pagado' },
    { ch: 'mesa', where: 'Mesa 6', items: '4× Cerveza Águila · 1× Picada surtida', amount: 67400, status: 'nuevo', statusText: 'Nuevo' },
    { ch: 'domicilio', where: 'Ricardo P. · Suba', items: '2× Bandeja Paisa · 1× Jugo mango', amount: 71200, status: 'nuevo', statusText: 'Nuevo · pagado' },
    { ch: 'mesa', where: 'Mesa 9', items: '1× Cazuela · 2× Postre · 2× Café', amount: 45800, status: 'nuevo', statusText: 'Nuevo' },
    { ch: 'domicilio', where: 'Camila T. · Cedritos', items: '1× Ajiaco · 1× Mazorcada', amount: 36700, status: 'nuevo', statusText: 'Nuevo · pagado' },
  ];
  let templateIdx = 0;

  function addNewOrder() {
    const tpl = orderTemplates[templateIdx % orderTemplates.length];
    templateIdx++;

    const id = state.nextOrderId++;
    const row = document.createElement('div');
    row.className = 'order-row new-entry';
    row.dataset.id = 'o-' + id;
    row.innerHTML =
      '<div class="order-channel" data-ch="' + tpl.ch + '">' + (tpl.ch === 'mesa' ? '🪑' : '🛵') + '</div>' +
      '<div class="order-main">' +
        '<div class="order-top">' +
          '<span class="order-id">#' + id + '</span>' +
          '<span class="order-where">' + tpl.where + '</span>' +
          '<span class="order-time">recién</span>' +
        '</div>' +
        '<div class="order-items">' + tpl.items + '</div>' +
      '</div>' +
      '<div class="order-amount">$' + tpl.amount.toLocaleString('es-CO') + '</div>' +
      '<div class="order-status ' + tpl.status + '">' + tpl.statusText + '</div>';

    const ordersEl = document.getElementById('orders');
    ordersEl.insertBefore(row, ordersEl.firstChild);

    // Cascade: bump metrics. Ticket promedio drifts subtly with each new
    // order (5% blend) instead of recomputing as revenue/orders, which
    // would cause a jarring jump from the seeded value to a different
    // figure on the very first new order.
    const oldRev = state.revenue;
    const oldOrders = state.orders;
    const oldTicket = state.ticket;
    state.revenue += tpl.amount;
    state.orders += 1;
    state.ticket = Math.round(state.ticket * 0.95 + tpl.amount * 0.05);

    setTimeout(() => {
      flashMetric('revenue');
      animateNumber(document.getElementById('m-revenue'), oldRev, state.revenue, fmtMoney);
    }, 280);
    setTimeout(() => {
      flashMetric('orders');
      animateNumber(document.getElementById('m-orders'), oldOrders, state.orders, fmtInt);
      // Update sidebar badge
      const badge = document.querySelector('.sb-badge');
      if (badge) badge.textContent = String(state.orders);
    }, 480);
    setTimeout(() => {
      flashMetric('ticket');
      animateNumber(document.getElementById('m-ticket'), oldTicket, state.ticket, fmtMoney);
    }, 680);

    // If mesa, occupy a table
    if (tpl.ch === 'mesa') {
      const free = [];
      for (let i = 1; i <= state.floorTotal; i++) if (!state.floorOccupied.has(i)) free.push(i);
      if (free.length) {
        const t = free[Math.floor(Math.random() * free.length)];
        state.floorOccupied.add(t);
        setTimeout(() => {
          renderFloor();
          const cell = document.querySelector('.table-cell[data-table="' + t + '"]');
          if (cell) {
            cell.classList.add('recent-occupy');
            setTimeout(() => cell.classList.remove('recent-occupy'), 1200);
          }
        }, 850);
      }
    }

    // Toast
    setTimeout(() => {
      toast('Pedido #' + id + ' · ' + tpl.where + ' · $' + tpl.amount.toLocaleString('es-CO'));
    }, 100);

    // Settle row background
    setTimeout(() => row.classList.add('settled'), 2200);

    // Trim the list to keep DOM tidy (max 8 rows)
    const rows = ordersEl.querySelectorAll('.order-row');
    if (rows.length > 8) {
      for (let i = 8; i < rows.length; i++) rows[i].remove();
    }

    // Age existing rows' time labels (cosmetic)
    rows.forEach((r, idx) => {
      if (idx === 0) return;
      const t = r.querySelector('.order-time');
      if (!t) return;
      // Only update those still showing "recién" or "hace X min"
      const txt = t.textContent.trim();
      if (txt === 'recién') t.textContent = 'hace 1 min';
    });
  }

  function flashMetric(key) {
    const el = document.querySelector('.metric[data-key="' + key + '"]');
    if (!el) return;
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1500);
  }

  // Schedule new orders every 9–14 s
  function scheduleNext() {
    const delay = 9000 + Math.random() * 5000;
    setTimeout(() => {
      if (!document.hidden && !state.tourActive) addNewOrder();
      scheduleNext();
    }, delay);
  }

  // ───────── Tour ─────────
  const tourSteps = [
    {
      target: '.metrics',
      title: 'Esto es tu negocio en vivo.',
      text: 'Cada tile se actualiza solo cuando entra un pedido nuevo, alguien paga, o tu equipo marca turno. Sin botón de "actualizar".',
    },
    {
      target: '.card-live',
      title: 'Cada pedido entra solo.',
      text: 'WhatsApp, salón, domicilio — todo cae acá ordenado. Quedate mirando 30 segundos: vas a ver entrar uno.',
    },
  ];

  function showTour(stepIdx) {
    if (stepIdx >= tourSteps.length) return endTour();
    state.tourActive = true;
    const step = tourSteps[stepIdx];
    const tour = document.getElementById('tour');
    const tooltip = document.getElementById('tour-tooltip');
    tour.hidden = false;

    document.getElementById('tour-step-num').textContent = String(stepIdx + 1);
    document.getElementById('tour-title').textContent = step.title;
    document.getElementById('tour-text').textContent = step.text;
    document.getElementById('tour-next').textContent = stepIdx === tourSteps.length - 1 ? 'Empezar a explorar' : 'Siguiente →';

    // Highlight target + scroll it into view (account for sticky frame+topbar)
    document.querySelectorAll('.tour-highlight').forEach(el => el.classList.remove('tour-highlight'));
    const target = document.querySelector(step.target);
    if (target) {
      target.classList.add('tour-highlight');
      const r = target.getBoundingClientRect();
      const stickyOffset = 120; // demo-frame (40) + topbar (64) + buffer
      const desiredTop = stickyOffset;
      const scrollDelta = r.top - desiredTop;
      // Only scroll if target is meaningfully out of place (avoid jitter)
      if (Math.abs(scrollDelta) > 24) {
        window.scrollTo({ top: window.scrollY + scrollDelta, behavior: 'smooth' });
      }
    }
    // Tooltip is CSS-positioned (fixed bottom-right) — no JS layout needed.

    document.getElementById('tour-next').onclick = () => showTour(stepIdx + 1);
    document.getElementById('tour-skip').onclick = endTour;
  }

  function endTour() {
    state.tourActive = false;
    document.getElementById('tour').hidden = true;
    document.querySelectorAll('.tour-highlight').forEach(el => el.classList.remove('tour-highlight'));
    try { localStorage.setItem('mesio_tour_v1', '1'); } catch (e) {}
    // Kick first order shortly after tour
    setTimeout(addNewOrder, 4000);
  }

  // ───────── Dock CTA ─────────
  function showDock() {
    if (state.dockShown) return;
    state.dockShown = true;
    const dock = document.getElementById('dock');
    dock.hidden = false;
    requestAnimationFrame(() => dock.classList.add('show'));
  }
  function hideDock() {
    const dock = document.getElementById('dock');
    dock.classList.remove('show');
    setTimeout(() => { dock.hidden = true; }, 500);
  }

  // ───────── Locked sections modal ─────────
  function openLockedModal(name, emoji) {
    const modal = document.getElementById('locked-modal');
    document.getElementById('modal-emoji').textContent = emoji;
    const titles = {
      salon: 'Salón en tiempo real',
      menu: 'Tu menú completo',
      nps: 'NPS y reseñas',
      fidelizacion: 'Programa de fidelización',
      equipo: 'Tu equipo',
      nomina: 'Nómina',
      sucursales: 'Múltiples sucursales',
      config: 'Configuración',
    };
    document.getElementById('modal-title').textContent = (titles[name] || 'Esta sección') + ' · sólo en cuenta real';
    document.getElementById('modal-text').textContent =
      'El demo te muestra el corazón de Mesio. Para explorar ' + (titles[name] || 'esta sección').toLowerCase() +
      ' con tu propia data, te creamos una cuenta de prueba en 48 horas. Sin tarjeta, sin compromiso.';
    modal.hidden = false;
  }
  function closeLockedModal() {
    document.getElementById('locked-modal').hidden = true;
  }

  // ───────── Section nav (resumen / pedidos / whatsapp / locked) ─────────
  function setupNav() {
    document.querySelectorAll('.sb-item').forEach(item => {
      item.addEventListener('click', e => {
        e.preventDefault();
        const sec = item.dataset.section;
        if (item.dataset.locked === '1') {
          openLockedModal(sec, item.querySelector('.sb-emoji').textContent.trim());
          return;
        }
        // For resumen/pedidos/whatsapp, just visually mark active.
        // (Resumen is the only fully-built view; others would be separate routes in real app.)
        document.querySelectorAll('.sb-item').forEach(i => i.classList.remove('active'));
        item.classList.add('active');
        document.getElementById('tb-title').textContent = item.textContent.trim().split(/\s+/).filter(t => !/^\d+$/.test(t)).join(' ').replace(/^[^\w]+/, '').trim() || 'Resumen';
        if (sec === 'whatsapp') {
          // Send to chat-demo
          window.location.href = 'demo.html';
        } else if (sec === 'pedidos') {
          // Scroll to orders card
          document.querySelector('.card-live').scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    });
    // "Ver todos" link
    document.querySelectorAll('.card-link[data-section]').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        const targetItem = document.querySelector('.sb-item[data-section="' + a.dataset.section + '"]');
        if (targetItem) targetItem.click();
      });
    });
  }

  // ───────── Dock buttons ─────────
  function setupDock() {
    document.getElementById('dock-close').addEventListener('click', hideDock);
    document.getElementById('dock-create').addEventListener('click', () => {
      window.open('https://wa.me/573001234567?text=' + encodeURIComponent('Hola Mesio, quiero crear mi cuenta de prueba.'), '_blank');
    });
    document.getElementById('dock-share').addEventListener('click', () => {
      const url = window.location.href;
      if (navigator.share) {
        navigator.share({ title: 'Mesio · Demo', text: 'Mirá esto, creo que nos sirve para el restaurante.', url }).catch(() => {});
      } else {
        navigator.clipboard.writeText(url).then(
          () => toast('Link copiado · pegalo en tu chat con tu socio'),
          () => toast('No pude copiar. Copialo manualmente.')
        );
      }
    });
  }

  // ───────── Modal close ─────────
  function setupModal() {
    document.querySelectorAll('#locked-modal [data-close]').forEach(el => {
      el.addEventListener('click', closeLockedModal);
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') closeLockedModal();
    });
  }

  // ───────── Dock trigger: 30 s OR scroll to trust ─────────
  function setupDockTrigger() {
    setTimeout(showDock, 30000);
    const trust = document.getElementById('trust');
    if (!trust) return;
    const io = new IntersectionObserver((entries) => {
      entries.forEach(en => {
        if (en.isIntersecting) {
          showDock();
          io.disconnect();
        }
      });
    }, { threshold: 0.1 });
    io.observe(trust);
  }

  // ───────── Init ─────────
  function init() {
    renderFloor();
    setupNav();
    setupDock();
    setupModal();
    setupDockTrigger();

    // First-time tour
    let tourSeen = false;
    try { tourSeen = localStorage.getItem('mesio_tour_v1') === '1'; } catch (e) {}
    if (!tourSeen) {
      setTimeout(() => showTour(0), 800);
    } else {
      // No tour: kick a first order soon
      setTimeout(addNewOrder, 5000);
    }

    scheduleNext();

    // Welcome pulse: occasional revenue tick (small) without new order
    // (simulates table closing checks etc.) — every 25-40 s
    function smallTick() {
      const delay = 25000 + Math.random() * 15000;
      setTimeout(() => {
        if (!document.hidden) {
          // pulse welcome counter slightly
          const pulse = document.querySelector('.welcome-pulse');
          if (pulse) {
            pulse.style.transition = 'transform .25s';
            pulse.style.transform = 'scale(1.04)';
            setTimeout(() => { pulse.style.transform = 'scale(1)'; }, 280);
          }
        }
        smallTick();
      }, delay);
    }
    smallTick();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
