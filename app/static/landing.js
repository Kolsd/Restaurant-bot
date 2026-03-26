/* ═══════════════════════════════════════════════════
   Mesio Landing — Scripts
   app/static/landing.js
═══════════════════════════════════════════════════ */

// ── SCROLL REVEAL ─────────────────────────────────────────────────
const revealObserver = new IntersectionObserver(entries => {
    entries.forEach(e => { if (e.isIntersecting) e.target.classList.add('on'); });
  }, { threshold: 0.1 });
  document.querySelectorAll('.rv').forEach(el => revealObserver.observe(el));
  
  // ── ANIMATED COUNTERS ─────────────────────────────────────────────
  function animateCounter(el) {
    const prefix = el.dataset.prefix || '';
    const suffix = el.dataset.suffix || '';
    const target = parseFloat(el.dataset.target);
    if (target === 0) { el.textContent = prefix + '0' + suffix; return; }
    let current = 0;
    const step = Math.max(1, Math.ceil(target / 50));
    const interval = setInterval(() => {
      current = Math.min(current + step, target);
      el.textContent = prefix + current + suffix;
      if (current >= target) clearInterval(interval);
    }, 28);
  }
  
  const counterObserver = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        animateCounter(e.target);
        counterObserver.unobserve(e.target);
      }
    });
  }, { threshold: 0.5 });
  document.querySelectorAll('[data-target]').forEach(el => counterObserver.observe(el));
  
  // ── PRIVACY MODAL ─────────────────────────────────────────────────
  function openPrivacy() {
    document.getElementById('privacy-modal').classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closePrivacy() {
    document.getElementById('privacy-modal').classList.remove('open');
    document.body.style.overflow = '';
  }
  
  // Cerrar modal al hacer clic en el overlay
  document.getElementById('privacy-modal').addEventListener('click', function(e) {
    if (e.target === this) closePrivacy();
  });
  
  // Cerrar modal con Escape
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closePrivacy();
  });

  // ── PRICING TOGGLE ────────────────────────────────────────────────
  function switchPricing(view) {
    const viewPacks = document.getElementById('view-packs');
    const viewLego  = document.getElementById('view-lego');
    const tabPacks  = document.getElementById('tab-packs');
    const tabLego   = document.getElementById('tab-lego');

    if (view === 'packs') {
      viewPacks.style.display = '';
      viewLego.style.display  = 'none';
      tabPacks.classList.add('ptab-active');
      tabLego.classList.remove('ptab-active');
    } else {
      viewPacks.style.display = 'none';
      viewLego.style.display  = '';
      tabLego.classList.add('ptab-active');
      tabPacks.classList.remove('ptab-active');
      // Trigger scroll reveal for lego view
      document.querySelectorAll('#view-lego .rv').forEach(el => {
        if (!el.classList.contains('on')) el.classList.add('on');
      });
    }
  }

  // ── LEGO CALCULATOR ───────────────────────────────────────────────
  const BASE_PRICE = 119000;

  function formatCOP(n) {
    return '$' + n.toLocaleString('es-CO');
  }

  function updateLegoTotal() {
    const active = document.querySelectorAll('.lego-mod.lego-mod-active');
    let total = BASE_PRICE;
    active.forEach(mod => { total += parseInt(mod.dataset.price, 10); });

    document.getElementById('lego-total').textContent = formatCOP(total);

    // Rebuild selected list
    const list = document.getElementById('lego-sel-list');
    // Keep base row, remove added module rows
    const addedRows = list.querySelectorAll('.lego-sel-added');
    addedRows.forEach(r => r.remove());

    active.forEach(mod => {
      const row = document.createElement('div');
      row.className = 'lego-sel-row lego-sel-added';
      const price = parseInt(mod.dataset.price, 10);
      row.innerHTML = '<span>' + mod.dataset.name + '</span><span>+$' + (price / 1000) + 'k</span>';
      list.appendChild(row);
    });

    // Update setup note
    const setupNote = document.getElementById('lego-setup-note');
    const hasAdvanced = !!document.querySelector('.lego-mod-active[data-name="Multi-sucursal"]');
    const hasMesa     = !!document.querySelector('.lego-mod-active[data-name="Mesa QR"]');
    let setup = 150000;
    if (hasMesa)     setup = Math.max(setup, 200000);
    if (hasAdvanced) setup = Math.max(setup, 250000);
    setupNote.textContent = '+ ' + formatCOP(setup) + ' setup único';
  }

  document.querySelectorAll('.lego-mod').forEach(mod => {
    mod.addEventListener('click', function() {
      this.classList.toggle('lego-mod-active');
      updateLegoTotal();
    });
  });