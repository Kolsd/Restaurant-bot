/**
 * app/static/js/internal/hq-shell.js
 *
 * Unified HQ shell for all /internal/* pages.
 * Injects top bar + optional sidebar on DOMContentLoaded.
 *
 * Opt-out: set data-hq-shell="skip" on <body> to prevent injection entirely.
 * Sidebar: enabled by default unless data-hq-sidebar="false" on <body>.
 * Active page: set data-page="analytics" (etc.) on <body> to highlight nav item.
 */
(function () {
  'use strict';

  const SESSION_KEY = 'hq_session';

  // ── Auth guard ────────────────────────────────────────────────────────────────
  // If there is no session token, redirect to superadmin (which has the login flow).
  function _getToken() {
    return sessionStorage.getItem(SESSION_KEY) ||
           sessionStorage.getItem('mesio_admin_key') || '';
  }

  function _guardAuth() {
    const token = _getToken();
    if (!token && !window.location.pathname.includes('/superadmin')) {
      window.location.href = '/internal/superadmin';
    }
    return token;
  }

  // ── Nav items ─────────────────────────────────────────────────────────────────
  const NAV_ITEMS = [
    { page: 'index',      href: '/internal',            icon: '🏠', label: 'Resumen'     },
    { page: 'analytics',  href: '/internal/analytics',  icon: '📊', label: 'Analytics'   },
    { page: 'crm',        href: '/internal/crm',        icon: '👥', label: 'CRM'         },
    { page: 'superadmin', href: '/internal/superadmin', icon: '🔧', label: 'Superadmin'  },
    { page: 'costs',      href: '/internal/costs',      icon: '💰', label: 'Costos'      },
    { page: 'monitoring', href: '/internal/monitoring', icon: '🛡️', label: 'Monitoring'  },
  ];

  // Detect current page from data-page attr or pathname
  function _currentPage() {
    const attr = document.body.dataset.page;
    if (attr) return attr;
    const path = window.location.pathname;
    if (path === '/internal' || path === '/internal/') return 'index';
    const match = path.match(/\/internal\/([^/]+)/);
    return match ? match[1] : '';
  }

  // ── Build top bar ─────────────────────────────────────────────────────────────
  function _buildTopbar() {
    const bar = document.createElement('div');
    bar.id = 'hq-topbar';

    // Hamburger (mobile)
    const ham = document.createElement('button');
    ham.id = 'hq-hamburger';
    ham.setAttribute('aria-label', 'Toggle navigation');
    ham.textContent = '☰';
    ham.addEventListener('click', _toggleSidebar);
    bar.appendChild(ham);

    // Logo
    const logo = document.createElement('a');
    logo.id = 'hq-logo';
    logo.href = '/internal';
    logo.innerHTML =
      '<div id="hq-logo-icon">M</div>' +
      '<div id="hq-logo-text">Mesio <span>HQ</span></div>';
    bar.appendChild(logo);

    // Search — clicking/focusing routes through the Cmd+K palette
    const searchWrap = document.createElement('div');
    searchWrap.id = 'hq-search-wrap';
    searchWrap.innerHTML =
      '<span id="hq-search-icon">🔍</span>' +
      '<input id="hq-search" type="search" placeholder="Buscar… (⌘K)" autocomplete="off" readonly>' +
      '<div id="hq-search-dropdown"></div>';
    bar.appendChild(searchWrap);

    // User area
    const userArea = document.createElement('div');
    userArea.id = 'hq-user-area';
    userArea.innerHTML =
      '<span id="hq-user-greeting">Hola, founder</span>' +
      '<button id="hq-logout-btn">Salir</button>';
    bar.appendChild(userArea);

    return bar;
  }

  // ── Build sidebar ─────────────────────────────────────────────────────────────
  function _buildSidebar(activePage) {
    const sb = document.createElement('nav');
    sb.id = 'hq-sidebar';
    sb.setAttribute('aria-label', 'HQ Navigation');

    const label = document.createElement('div');
    label.className = 'hq-nav-label';
    label.textContent = 'Herramientas';
    sb.appendChild(label);

    NAV_ITEMS.forEach(item => {
      const a = document.createElement('a');
      a.className = 'hq-nav-item' + (item.page === activePage ? ' active' : '');
      a.href = item.href;
      a.innerHTML =
        '<span class="hq-nav-icon">' + item.icon + '</span>' +
        '<span>' + item.label + '</span>';
      sb.appendChild(a);
    });

    return sb;
  }

  // ── Sidebar toggle (mobile) ───────────────────────────────────────────────────
  function _toggleSidebar() {
    const sb = document.getElementById('hq-sidebar');
    const overlay = document.getElementById('hq-sidebar-overlay');
    if (!sb) return;
    sb.classList.toggle('open');
    if (overlay) overlay.classList.toggle('open');
  }

  // ── Global search — routes topbar input through Cmd+K palette ────────────────
  function _setupSearch() {
    const input = document.getElementById('hq-search');
    if (!input) return;

    // Focus or click on the topbar search → open the palette
    input.addEventListener('focus', () => {
      input.blur(); // prevent input from keeping focus; palette has its own input
      _openPalette();
    });
    input.addEventListener('click', e => {
      e.preventDefault();
      _openPalette();
    });

    // Any keypress while topbar input is somehow focused → open palette
    input.addEventListener('keydown', e => {
      if (e.key !== 'Tab') e.preventDefault();
      _openPalette();
    });
  }

  function _openPalette() {
    if (window.MesioCmdPalette) {
      window.MesioCmdPalette.open();
    } else {
      // Palette script not yet loaded (should not normally happen since we inject
      // it below, but guard anyway). Dispatch custom event as fallback.
      document.dispatchEvent(new CustomEvent('mesio:palette-open'));
    }
  }

  // Minimal XSS escape (mesio-utils.js may not be loaded yet)
  function _escHtmlShell(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── Logout ────────────────────────────────────────────────────────────────────
  function _setupLogout() {
    const btn = document.getElementById('hq-logout-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      const token = _getToken();
      try {
        await fetch('/api/internal/admin/logout', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer ' + token }
        });
      } catch (_) { /* best-effort */ }
      sessionStorage.removeItem(SESSION_KEY);
      sessionStorage.removeItem('mesio_admin_key');
      window.location.href = '/internal/superadmin';
    });
  }

  // ── Load cmd-palette.js ───────────────────────────────────────────────────────
  function _loadCmdPalette() {
    const s = document.createElement('script');
    s.src = '/static/js/internal/cmd-palette.js';
    s.defer = true;
    // Once loaded, wire the custom fallback event
    s.addEventListener('load', () => {
      document.addEventListener('mesio:palette-open', () => {
        if (window.MesioCmdPalette) window.MesioCmdPalette.open();
      });
    });
    document.head.appendChild(s);
  }

  // ── Main injection ────────────────────────────────────────────────────────────
  function _inject() {
    const body = document.body;

    // Opt-out check
    if (body.dataset.hqShell === 'skip') return;

    // Auth guard — redirect if no token
    _guardAuth();

    const showSidebar = body.dataset.hqSidebar !== 'false';
    const activePage = _currentPage();

    // Add layout classes
    body.classList.add('hq-ready');
    if (!showSidebar) body.classList.add('hq-no-sidebar');

    // Wrap existing body content in #hq-content
    const content = document.createElement('div');
    content.id = 'hq-content';
    // Move all existing children into content div
    while (body.firstChild) {
      content.appendChild(body.firstChild);
    }

    // Build and insert shell elements
    const topbar = _buildTopbar();
    body.appendChild(topbar);

    if (showSidebar) {
      const sidebar = _buildSidebar(activePage);
      body.appendChild(sidebar);

      // Mobile overlay
      const overlay = document.createElement('div');
      overlay.id = 'hq-sidebar-overlay';
      overlay.addEventListener('click', _toggleSidebar);
      body.appendChild(overlay);
    }

    body.appendChild(content);

    // Wire up interactions
    _setupSearch();
    _setupLogout();

    // Load the Cmd+K palette script
    _loadCmdPalette();
  }

  // ── Boot ──────────────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _inject);
  } else {
    _inject();
  }
})();
