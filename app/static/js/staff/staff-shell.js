/* ═══════════════════════════════════════════════════
   Mesio — Staff App shell
   One page (/staff) for every operational role: Cashier, Waiter, Kitchen,
   Bar, Courier and "My shift". The shell renders a sidebar (same look as
   the admin dashboard — see app/static/css/shared.css .sb-* classes) with
   only the sections the current user's roles allow
   (GET /api/staff/sections — app/services/staff_sections.py is the single
   source of truth for that mapping), then mounts/unmounts section modules
   from app/static/js/staff/sections/*.js into #staff-section-root WITHOUT
   a full page reload.

   Each section module lazy-loads its own <script> on first visit and
   registers itself on window.MesioStaffSections[key] = { mount, unmount }.
   Switching sections always calls the outgoing section's unmount() before
   mounting the next one, so polling intervals / listeners never leak.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Section metadata: key -> { label, icon, jsFile }. Order here is the
  // sidebar order (mirrors app.services.staff_sections.ALL_SECTIONS).
  var SECTION_META = {
    cashier: { label: 'Caja',       jsFile: 'cashier.js',
      icon: '<svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="12" height="9" rx="1"/><path d="M2 7h12M5 10h2M9 10h2"/></svg>' },
    delivery: { label: 'Domicilios', jsFile: 'delivery.js',
      icon: '<svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h7v6H2z"/><path d="M9 7h3l2 2.5V10h-5z"/><circle cx="4.5" cy="12.5" r="1.5"/><circle cx="11.5" cy="12.5" r="1.5"/></svg>' },
    waiter:  { label: 'Mesero',     jsFile: 'waiter.js',
      icon: '<svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="5" r="2.5"/><path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5"/></svg>' },
    kitchen: { label: 'Cocina',     jsFile: 'kitchen.js',
      icon: '<svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 6a4 4 0 018 0v2H4V6z"/><path d="M3 8h10v6H3z"/><path d="M6 11h4"/></svg>' },
    bar:     { label: 'Bar',        jsFile: 'bar.js',
      icon: '<svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 2h6l2 5H3L5 2z"/><path d="M3 7v7h10V7"/><path d="M7 10v4M9 10v4"/></svg>' },
    courier: { label: 'Mis entregas',   jsFile: 'courier.js',
      icon: '<svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="13" r="1.5"/><circle cx="12" cy="13" r="1.5"/><path d="M1 3h2l2 7h6l2-5H5"/></svg>' },
  };
  // Mirrors app.services.staff_sections.ALL_SECTIONS ordering exactly
  // (cashier, delivery, waiter, kitchen, bar, courier) — "delivery"
  // (Domicilios) sits right after "cashier" since it's granted to the same
  // roles (docs/claude/delivery-web.md, "Cashier UI").
  var SECTION_ORDER = ['cashier', 'delivery', 'waiter', 'kitchen', 'bar', 'courier'];
  var LAST_SECTION_KEY = 'rb_staff_last_section';

  var _currentSection = null;
  var _loadedScripts = {};   // jsFile -> true once its <script> has loaded
  var _loadingPromises = {}; // jsFile -> in-flight load Promise

  function _loadSectionScript(key) {
    var meta = SECTION_META[key];
    if (window.MesioStaffSections && window.MesioStaffSections[key]) {
      return Promise.resolve(); // already registered (e.g. re-visit)
    }
    if (_loadingPromises[meta.jsFile]) return _loadingPromises[meta.jsFile];
    _loadingPromises[meta.jsFile] = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = '/static/js/staff/sections/' + meta.jsFile;
      s.onload = function () { _loadedScripts[meta.jsFile] = true; resolve(); };
      s.onerror = function () { reject(new Error('No se pudo cargar la sección: ' + meta.jsFile)); };
      document.head.appendChild(s);
    });
    return _loadingPromises[meta.jsFile];
  }

  async function switchSection(key, allowedSections) {
    if (allowedSections.indexOf(key) === -1) return;
    if (key === _currentSection) return;

    var root = document.getElementById('staff-section-root');
    if (!root) return;

    // Unmount the outgoing section FIRST so its polling intervals / event
    // listeners are always stopped before the next section starts its own.
    if (_currentSection && window.MesioStaffSections && window.MesioStaffSections[_currentSection]) {
      try { window.MesioStaffSections[_currentSection].unmount(root); }
      catch (e) { console.error('staff-shell: unmount failed for', _currentSection, e); }
    }
    root.innerHTML = '<div style="padding:60px;text-align:center;color:var(--text-3);">Cargando…</div>';

    try {
      await _loadSectionScript(key);
    } catch (e) {
      root.innerHTML = '<div style="padding:40px;text-align:center;color:#ef4444;">No se pudo cargar esta sección. Intenta de nuevo.</div>';
      console.error(e);
      return;
    }

    var mod = window.MesioStaffSections && window.MesioStaffSections[key];
    if (!mod) {
      root.innerHTML = '<div style="padding:40px;text-align:center;color:#ef4444;">Sección no disponible.</div>';
      return;
    }

    _currentSection = key;
    try { localStorage.setItem(LAST_SECTION_KEY, key); } catch (e) { /* private mode etc. */ }

    _updateActiveNav(key);
    _updateTopbar(key);

    try { mod.mount(root); }
    catch (e) { console.error('staff-shell: mount failed for', key, e); }
  }

  function _updateActiveNav(key) {
    document.querySelectorAll('.sidebar [data-section]').forEach(function (el) {
      el.classList.toggle('active', el.dataset.section === key);
    });
  }

  function _updateTopbar(key) {
    var titleEl = document.getElementById('staff-topbar-title');
    if (titleEl) titleEl.textContent = (SECTION_META[key] || {}).label || 'Staff';
    try {
      var url = new URL(window.location.href);
      url.searchParams.set('section', key);
      window.history.replaceState(null, '', url.pathname + '?' + url.searchParams.toString());
    } catch (e) { /* non-critical */ }
  }

  function _renderSidebarNav(allowedSections) {
    var nav = document.getElementById('staff-sidebar-nav');
    if (!nav) return;
    nav.innerHTML = '';

    var opsKeys = SECTION_ORDER.filter(function (k) { return allowedSections.indexOf(k) !== -1; });
    var hasOps = opsKeys.length > 0;

    if (hasOps) {
      var opsGroup = document.createElement('div');
      opsGroup.className = 'sb-group';
      var opsLabel = document.createElement('div');
      opsLabel.className = 'sb-group-label';
      opsLabel.textContent = 'Operación';
      opsGroup.appendChild(opsLabel);
      opsKeys.forEach(function (key) { opsGroup.appendChild(_navItem(key)); });
      nav.appendChild(opsGroup);
    }

  }

  function _navItem(key) {
    var meta = SECTION_META[key];
    var a = document.createElement('a');
    a.className = 'sb-item';
    a.href = '#';
    a.dataset.section = key;
    a.innerHTML = meta.icon; // static, trusted markup defined above — not user data
    var label = document.createElement('span');
    label.textContent = meta.label; // textContent — safe even though this string is static
    a.appendChild(label);
    // Unread-count pill — hidden by default, styling from .sb-item .badge
    // in shared.css (already used by the admin dashboard's sidebar.js).
    // A section fills this in via MesioStaffShell.setBadge(key, n) from its
    // own mount()/checkUpdates() loop; the shell itself never counts anything.
    var badge = document.createElement('span');
    badge.className = 'badge';
    badge.style.display = 'none';
    a.appendChild(badge);
    a.addEventListener('click', function (e) {
      e.preventDefault();
      switchSection(key, _allowedSectionsCache || [key]);
    });
    return a;
  }

  // ── Sidebar unread badges (e.g. Domicilios "por aceptar" count) ────────
  // Sections call this from their own polling/SSE handlers; the shell just
  // owns the DOM element it already renders in _navItem above.
  function setBadge(key, count) {
    var item = document.querySelector('.sidebar [data-section="' + key + '"]');
    var badge = item && item.querySelector('.badge');
    if (!badge) return;
    var n = Number(count) || 0;
    if (n > 0) { badge.textContent = n > 99 ? '99+' : String(n); badge.style.display = ''; }
    else { badge.style.display = 'none'; }
  }

  var _allowedSectionsCache = null;

  function _populateUserChrome() {
    // Mirrors app/static/js/pages/sidebar.js's org/user population so the
    // Staff App sidebar looks identical to the admin dashboard's.
    var restaurant = {};
    try { restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}'); } catch (e) { /* ignore */ }

    var orgName = document.getElementById('sb-org-name');
    var orgSub = document.getElementById('sb-org-sub');
    var orgAvatar = document.getElementById('sb-org-avatar');
    if (restaurant.name) {
      if (orgName) orgName.textContent = restaurant.name;
      if (orgSub) orgSub.textContent = 'Staff App';
      if (orgAvatar) {
        var initials = restaurant.name.split(' ').slice(0, 2).map(function (w) { return w[0]; }).join('').toUpperCase();
        orgAvatar.textContent = initials;
        orgAvatar.style.background = '#FDE8CE';
        orgAvatar.style.color = '#BA7517';
      }
    } else if (orgName) {
      orgName.textContent = 'Mesio';
    }

    var nameEl = document.getElementById('sb-user-name');
    var roleEl = document.getElementById('sb-user-role');
    var avatarEl = document.getElementById('sb-user-avatar');
    var staffName = localStorage.getItem('rb_staff_name') || localStorage.getItem('rb_name') || restaurant.email || '';
    if (nameEl) nameEl.textContent = staffName || '—';
    if (roleEl) roleEl.textContent = localStorage.getItem('rb_role') || '';
    if (avatarEl && staffName) avatarEl.textContent = staffName.slice(0, 2).toUpperCase();

    var logoutBtn = document.getElementById('staff-btn-logout');
    if (logoutBtn) logoutBtn.addEventListener('click', function () { mesioLogout(); });
  }

  function _readInitialSection(allowedSections) {
    var urlParams = new URLSearchParams(window.location.search);
    var fromUrl = urlParams.get('section');
    if (fromUrl && allowedSections.indexOf(fromUrl) !== -1) return fromUrl;

    var fromStorage = null;
    try { fromStorage = localStorage.getItem(LAST_SECTION_KEY); } catch (e) { /* ignore */ }
    if (fromStorage && allowedSections.indexOf(fromStorage) !== -1) return fromStorage;

    return allowedSections[0] || null;
  }

  // ── Mobile drawer ──────────────────────────────────────────────────
  // Same pattern as the admin dashboard (app/static/js/pages/sidebar.js
  // initMobileSidebar) — reuses the identical .sb-hamburger/.sidebar-overlay/
  // .sidebar.open CSS already defined in shared.css, so it looks and behaves
  // like the rest of the app. Waiters/couriers run this on a phone, so the
  // sidebar MUST be reachable at narrow widths — without this there is no
  // way to open it at all below the 768px breakpoint.
  function _initMobileDrawer() {
    var sidebar = document.querySelector('.sidebar');
    var topbar = document.querySelector('.topbar');
    if (!sidebar || !topbar) return;

    if (!document.querySelector('.sb-hamburger')) {
      var ham = document.createElement('button');
      ham.className = 'sb-hamburger';
      ham.setAttribute('aria-label', 'Abrir menú');
      ham.setAttribute('aria-expanded', 'false');
      ham.setAttribute('aria-controls', 'staff-sidebar-nav');
      ham.innerHTML = '☰';
      topbar.insertBefore(ham, topbar.firstChild);
    }

    if (!document.querySelector('.sidebar-overlay')) {
      var ov = document.createElement('div');
      ov.className = 'sidebar-overlay';
      document.body.appendChild(ov);
    }

    var hamBtn = document.querySelector('.sb-hamburger');
    var overlay = document.querySelector('.sidebar-overlay');

    function openDrawer() {
      sidebar.classList.add('open');
      overlay.classList.add('open');
      hamBtn.setAttribute('aria-expanded', 'true');
    }
    function closeDrawer() {
      sidebar.classList.remove('open');
      overlay.classList.remove('open');
      hamBtn.setAttribute('aria-expanded', 'false');
    }

    hamBtn.addEventListener('click', function () {
      if (sidebar.classList.contains('open')) closeDrawer(); else openDrawer();
    });
    overlay.addEventListener('click', closeDrawer);

    // Close the drawer after picking a section — the click also runs
    // switchSection() via the nav item's own listener (_navItem above);
    // this just handles the drawer chrome, same division of concerns as
    // sidebar.js's version.
    sidebar.addEventListener('click', function (e) {
      if (e.target.closest('a, .sb-item')) closeDrawer();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && sidebar.classList.contains('open')) closeDrawer();
    });
  }

  async function boot() {
    var token = localStorage.getItem('rb_token');
    if (!token) { window.location.href = '/login'; return; }

    _populateUserChrome();
    _initMobileDrawer();

    // One SSE connection for the whole page lifetime (real-time invalidation
    // events — see app/static/js/mesio-realtime.js). Sections subscribe to
    // the topics they care about in their own mount()/unmount().
    if (window.MesioRealtime) {
      MesioRealtime.connect('/api/staff/stream', mesioHeaders);
    }

    var sections;
    var opsInfo = { needs_setup: false, can_configure: false };
    try {
      const res = await fetch('/api/staff/sections', { headers: mesioHeaders() });
      if (res.status === 401) { localStorage.clear(); window.location.href = '/login'; return; }
      if (!res.ok) throw new Error('status ' + res.status);
      const data = await res.json();
      sections = Array.isArray(data.sections) ? data.sections : [];
      opsInfo = { needs_setup: !!data.needs_setup, can_configure: !!data.can_configure };
    } catch (e) {
      console.error('staff-shell: /api/staff/sections failed', e);
      sections = [];
    }

    if (!sections.length) {
      // A role that grants no section ("otro"): say so instead of a blank page.
      var emptyRoot = document.getElementById('staff-section-root');
      if (emptyRoot) {
        var msg = document.createElement('p');
        msg.style.cssText = 'padding:32px;color:var(--text-3);font-size:14px;';
        msg.textContent = 'Tu usuario todavía no tiene un rol con pantalla asignada. Pídele al administrador que te asigne uno (mesero, caja, cocina, bar o domiciliario).';
        emptyRoot.appendChild(msg);
      }
      return;
    }

    _allowedSectionsCache = sections;
    _renderSidebarNav(sections);
    _initOpsSetup(opsInfo);

    var initial = _readInitialSection(sections);
    switchSection(initial, sections);

    // "A new order arriving must be impossible to miss" (docs/claude/
    // delivery-web.md, chunk 7) means the Domicilios badge has to update
    // even while the cashier is working the Caja section, not only while
    // Domicilios itself is mounted — a per-section poll alone would freeze
    // the count the moment they switch away. Lives here (not in
    // delivery.js) because this is the one place that already owns a
    // page-lifetime SSE connection; delivery.js still drives the badge
    // directly (and more precisely) whenever it IS the mounted section.
    if (sections.indexOf('delivery') !== -1) {
      _watchDeliveryBadge();
    }
  }

  /* "Configurar operación" (ops-setup.js): asked once per sede the first time
   * an owner/admin/gerente opens Operación, and reachable from the sidebar
   * after. Saving reloads so the sidebar shows only the screens kept. */
  function _initOpsSetup(info) {
    if (!info.can_configure || !window.MesioOpsSetup) return;
    var onSaved = function () { window.location.reload(); };
    var bottom = document.querySelector('.sb-bottom');
    if (bottom && !document.getElementById('staff-btn-ops-setup')) {
      var btn = document.createElement('button');
      btn.id = 'staff-btn-ops-setup';
      btn.type = 'button';
      btn.className = 'btn sm ghost';
      btn.style.cssText = 'width:100%;margin-bottom:8px;';
      btn.textContent = 'Configurar operación';
      btn.addEventListener('click', function () { MesioOpsSetup.open({ onSaved: onSaved }); });
      bottom.insertBefore(btn, bottom.firstChild);
    }
    if (info.needs_setup) MesioOpsSetup.open({ onSaved: onSaved });
  }

  function _watchDeliveryBadge() {
    function refresh() {
      if (_currentSection === 'delivery') return; // delivery.js owns it while mounted
      fetch('/api/staff/delivery/orders?status=pendiente_aceptacion', { headers: mesioHeaders() })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) { if (data) setBadge('delivery', (data.orders || []).length); })
        .catch(function () { /* best-effort — never blocks the rest of the shell */ });
    }
    refresh();
    mesioLiveInterval(refresh, 15000);
    if (window.MesioRealtime) {
      ['order.created', 'order.updated', 'resync'].forEach(function (topic) {
        MesioRealtime.on(topic, refresh);
      });
    }
  }

  window.MesioStaffShell = { setBadge: setBadge };

  document.addEventListener('DOMContentLoaded', boot);
})();
