/* Mesio Landing — landing.js (vanilla, no deps) */
(function () {
  'use strict';

  var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ── INTERSECTION OBSERVER: reveal .rv elements ── */
  function initReveal() {
    var els = document.querySelectorAll('.rv');
    if (!els.length) return;
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('in');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 });
    els.forEach(function (el) { observer.observe(el); });
  }

  /* ── ANIMATED COUNTERS ── */
  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  function animateCounter(el) {
    var target = parseFloat(el.dataset.target) || 0;
    var prefix = el.dataset.prefix || '';
    var suffix = el.dataset.suffix || '';
    var duration = 1600;
    var start = null;

    if (reducedMotion) {
      el.textContent = prefix + target + suffix;
      return;
    }

    function step(ts) {
      if (!start) start = ts;
      var progress = Math.min((ts - start) / duration, 1);
      var value = Math.round(easeOutCubic(progress) * target);
      el.textContent = prefix + value + suffix;
      if (progress < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function initCounters() {
    var counters = document.querySelectorAll('.stat-num[data-target]');
    if (!counters.length) return;
    var triggered = new Set();
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting && !triggered.has(entry.target)) {
          triggered.add(entry.target);
          animateCounter(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.4 });
    counters.forEach(function (el) { observer.observe(el); });
  }

  /* ── MOBILE NAV ── */
  function initMobileNav() {
    var btn = document.querySelector('.hamburger');
    var nav = document.getElementById('mobile-nav');
    if (!btn || !nav) return;

    function open() {
      document.body.classList.add('nav-open');
      btn.setAttribute('aria-expanded', 'true');
      nav.setAttribute('aria-hidden', 'false');
    }
    function close() {
      document.body.classList.remove('nav-open');
      btn.setAttribute('aria-expanded', 'false');
      nav.setAttribute('aria-hidden', 'true');
    }
    function toggle() {
      document.body.classList.contains('nav-open') ? close() : open();
    }

    btn.addEventListener('click', toggle);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') close();
    });

    document.addEventListener('click', function (e) {
      if (
        document.body.classList.contains('nav-open') &&
        !nav.contains(e.target) &&
        !btn.contains(e.target)
      ) {
        close();
      }
    });

    nav.querySelectorAll('a').forEach(function (a) {
      a.addEventListener('click', close);
    });
  }

  /* ── PRICING TOGGLE ── */
  function initPricingToggle() {
    var btnMonthly = document.getElementById('toggle-monthly');
    var btnAnnual = document.getElementById('toggle-annual');
    var grid = document.getElementById('pricing-grid');
    if (!btnMonthly || !btnAnnual || !grid) return;

    function setMonthly() {
      btnMonthly.classList.add('active');
      btnAnnual.classList.remove('active');
      btnMonthly.setAttribute('aria-pressed', 'true');
      btnAnnual.setAttribute('aria-pressed', 'false');
      grid.querySelectorAll('.price-m').forEach(function (el) { el.hidden = false; });
      grid.querySelectorAll('.price-a').forEach(function (el) { el.hidden = true; });
    }
    function setAnnual() {
      btnAnnual.classList.add('active');
      btnMonthly.classList.remove('active');
      btnAnnual.setAttribute('aria-pressed', 'true');
      btnMonthly.setAttribute('aria-pressed', 'false');
      grid.querySelectorAll('.price-a').forEach(function (el) { el.hidden = false; });
      grid.querySelectorAll('.price-m').forEach(function (el) { el.hidden = true; });
    }

    btnMonthly.addEventListener('click', setMonthly);
    btnAnnual.addEventListener('click', setAnnual);
    setMonthly();
  }

  /* ── DEMO FORM ── */
  function initDemoForm() {
    var form = document.getElementById('demo-form');
    if (!form) return;

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var name = (form.querySelector('#f-name') || {}).value || '';
      var restaurant = (form.querySelector('#f-restaurant') || {}).value || '';
      var whatsapp = (form.querySelector('#f-whatsapp') || {}).value || '';
      var branches = (form.querySelector('#f-branches') || {}).value || '1';

      var text = [
        'Hola, quiero solicitar una demo de Mesio.',
        'Nombre: ' + name,
        'Restaurante: ' + restaurant,
        'WhatsApp: ' + whatsapp,
        'Sucursales: ' + branches
      ].join('\n');

      var url = 'https://wa.me/573144914554?text=' + encodeURIComponent(text);
      window.open(url, '_blank', 'noopener');
    });
  }

  /* ── INIT ── */
  document.addEventListener('DOMContentLoaded', function () {
    initReveal();
    initCounters();
    initMobileNav();
    initPricingToggle();
    initDemoForm();
  });
})();
