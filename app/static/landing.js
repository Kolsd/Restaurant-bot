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