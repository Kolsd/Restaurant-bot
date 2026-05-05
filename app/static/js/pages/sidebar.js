/* Mesio — Shared sidebar renderer
 * Introduced: 2026-04-19 (design-review-v2 port)
 * Purpose: single source of truth for the admin sidebar across all pages.
 *
 * Usage:
 *   1. Add <aside class="sidebar"></aside> wherever the sidebar should appear.
 *   2. Add data-active="<page-key>" to <body> to highlight the current item.
 *   3. Include this script before </body>.
 *
 * Valid page keys (data-active values):
 *   resumen | pedidos | reservaciones | whatsapp | salon | menu | menu-eng
 *   nps | fidelizacion | riesgo | staff | nomina | sucursales
 *   settings | billing
 *
 * Route mapping (.html → production URL):
 *   dashboard.html          → /dashboard
 *   pedidos.html            → /caja
 *   reservaciones.html      → /reservaciones
 *   floorplan.html          → /floorplan
 *   menu-admin.html         → /menu-admin
 *   menu-engineering.html   → /menu-engineering
 *   nps.html                → /nps
 *   fidelizacion.html       → /fidelizacion
 *   clientes-riesgo.html    → /clientes-riesgo
 *   staff-hq.html           → /staff-hq   (employee portal — Agent B)
 *   nomina.html             → /nomina
 *   sucursales.html         → /sucursales
 *   settings.html           → /settings
 *   billing.html            → /billing
 */
(function () {
  const SIDEBAR_HTML = `
    <div class="sb-brand">
      <div class="mark">M</div>
      <div>
        <div class="sb-brand-name">Mesio</div>
        <div class="sb-brand-sub">Panel de control</div>
      </div>
    </div>

    <div class="sb-switcher" id="sb-switcher">
      <div id="sb-org-avatar" style="width:20px;height:20px;border-radius:5px;background:#e0e0d8;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#555;flex-shrink:0;"></div>
      <div class="grow">
        <div class="sb-switcher-name" id="sb-org-name">Cargando…</div>
        <div class="sb-switcher-sub" id="sb-org-sub">Panel de control</div>
      </div>
      <span class="chev">⌄</span>
    </div>

    <div class="sb-group">
      <div class="sb-group-label">Operaciones</div>
      <a class="sb-item" data-key="resumen" href="/dashboard">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>
        Resumen
        <span class="kbd">R</span>
      </a>
      <a class="sb-item" data-key="pedidos" href="/pedidos">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 5l5-3 5 3v6l-5 3-5-3V5z"/><path d="M3 5l5 3 5-3M8 8v6"/></svg>
        Pedidos
        <span class="badge" id="sb-live-orders-badge" style="display:none;"></span>
      </a>
      <a class="sb-item" data-key="reservaciones" href="/reservaciones">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="10" rx="1"/><path d="M5 3V1M11 3V1M2 6h12"/></svg>
        Reservaciones
      </a>
      <a class="sb-item" data-key="whatsapp" href="/dashboard">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8c0-3 2.5-5.5 6-5.5S14 5 14 8s-2.5 5.5-6 5.5c-1 0-2-.2-2.8-.6L3 14l.6-2.2C2.6 11 2 9.5 2 8z"/></svg>
        WhatsApp
        <span class="dot live" style="margin-left:auto;"></span>
      </a>
    </div>

    <div class="sb-group">
      <div class="sb-group-label">Restaurante</div>
      <a class="sb-item" data-key="salon" href="/floorplan">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="12" height="12" rx="1"/><path d="M2 6h12M6 2v12"/></svg>
        Salón
      </a>
      <a class="sb-item" data-key="menu" href="/menu-admin">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M3 8c0-2.8 2.2-5 5-5"/></svg>
        Menú
      </a>
      <a class="sb-item" data-key="menu-eng" href="/menu-engineering">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 13V8l3-4h6l3 4v5"/><path d="M2 13h12M5 13v-3h6v3"/></svg>
        Menu Engineering
      </a>
    </div>

    <div class="sb-group">
      <div class="sb-group-label">Clientes</div>
      <a class="sb-item" data-key="nps" href="/nps">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1.5l1.8 4.2 4.5.4-3.4 3 1 4.4L8 11.2 4.1 13.5l1-4.4-3.4-3 4.5-.4z"/></svg>
        NPS &amp; Reseñas
      </a>
      <a class="sb-item" data-key="fidelizacion" href="/fidelizacion">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="5" width="12" height="9" rx="1"/><path d="M2 8h12M8 5V2M5 5v-.5a1 1 0 011-1h4a1 1 0 011 1V5"/></svg>
        Fidelización
      </a>
      <a class="sb-item" data-key="riesgo" href="/clientes-riesgo">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5.5" cy="5" r="2.5"/><path d="M1.5 13c0-2.2 1.8-4 4-4s4 1.8 4 4"/><circle cx="11.5" cy="6" r="2"/><path d="M9.5 13c0-1.6 1-3 2-3"/></svg>
        Clientes en riesgo
        <span class="badge" id="sb-risk-badge" style="display:none;background:var(--warning-light,#fef3c7);color:var(--warning-text,#92400e);"></span>
      </a>
    </div>

    <div class="sb-group">
      <div class="sb-group-label">Equipo</div>
      <a class="sb-item" data-key="staff" href="/equipo">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="5" r="2.5"/><path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5"/></svg>
        Equipo
      </a>
      <a class="sb-item" data-key="nomina" href="/nomina">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l2.5 1.5"/></svg>
        Nómina
      </a>
      <a class="sb-item" data-key="sucursales" href="/sucursales">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 13V6l6-4 6 4v7"/><path d="M2 13h12M6 13V9h4v4"/></svg>
        Sucursales
      </a>
    </div>

    <div class="sb-group" id="sb-ops-group" style="display:none;">
      <div class="sb-group-label">Mi área</div>
      <a class="sb-item" data-key="ops-caja" href="/pedidos" style="display:none;">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="12" height="9" rx="1"/><path d="M2 7h12M5 10h2M9 10h2"/></svg>
        Caja
      </a>
      <a class="sb-item" data-key="ops-mesero" href="/mesero" style="display:none;">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="5" r="2.5"/><path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5"/></svg>
        Mesero
      </a>
      <a class="sb-item" data-key="ops-cocina" href="/kitchen" style="display:none;">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 6a4 4 0 018 0v2H4V6z"/><path d="M3 8h10v6H3z"/><path d="M6 11h4"/></svg>
        Cocina
      </a>
      <a class="sb-item" data-key="ops-bar" href="/bar" style="display:none;">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 2h6l2 5H3L5 2z"/><path d="M3 7v7h10V7"/><path d="M7 10v4M9 10v4"/></svg>
        Bar
      </a>
      <a class="sb-item" data-key="ops-domiciliario" href="/domiciliario" style="display:none;">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="13" r="1.5"/><circle cx="12" cy="13" r="1.5"/><path d="M1 3h2l2 7h6l2-5H5"/></svg>
        Domicilios
      </a>
    </div>

    <div class="sb-bottom">
      <a class="sb-item" data-key="settings" href="/settings">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3"/></svg>
        Configuración
      </a>
      <a class="sb-item" data-key="billing" href="/billing">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="12" height="9" rx="1"/><path d="M2 7h12"/></svg>
        Facturación
      </a>
      <div class="sb-user" id="sb-user" style="margin-top:8px;cursor:pointer;">
        <div class="sb-avatar" id="sb-user-avatar" style="flex-shrink:0;"></div>
        <div class="grow">
          <div class="sb-user-name" id="sb-user-name">—</div>
          <div class="sb-user-role" id="sb-user-role"></div>
        </div>
        <span class="chev" style="color:var(--text-4);font-size:11px;">⌄</span>
      </div>
    </div>
  `;

  function initMobileSidebar() {
    var sidebar = document.querySelector('.sidebar');
    var topbar = document.querySelector('.topbar');
    if (!sidebar || !topbar) return;

    // Inject hamburger if not already present
    if (!document.querySelector('.sb-hamburger')) {
      var ham = document.createElement('button');
      ham.className = 'sb-hamburger';
      ham.setAttribute('aria-label', 'Abrir menú');
      ham.setAttribute('aria-expanded', 'false');
      ham.innerHTML = '☰';
      topbar.insertBefore(ham, topbar.firstChild);
    }

    // Inject overlay if not already present
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

    // Close drawer when a nav link inside is clicked
    sidebar.addEventListener('click', function (e) {
      if (e.target.closest('a, .sb-item')) closeDrawer();
    });

    // Close on Escape
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && sidebar.classList.contains('open')) closeDrawer();
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    const sb = document.querySelector('.sidebar');
    if (!sb) return;

    sb.innerHTML = SIDEBAR_HTML;

    // Highlight active item via body[data-active] or sidebar[data-active]
    const active = document.body.dataset.active || sb.dataset.active;
    if (active) {
      const el = sb.querySelector('[data-key="' + active + '"]');
      if (el) el.classList.add('active');
    }

    // Populate org info from localStorage (set by dashboard.js / auth flow)
    const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    if (restaurant.name) {
      const orgName = sb.querySelector('#sb-org-name');
      const orgSub  = sb.querySelector('#sb-org-sub');
      const orgAvatar = sb.querySelector('#sb-org-avatar');
      if (orgName) orgName.textContent = restaurant.name;
      if (orgSub && restaurant.plan) orgSub.textContent = restaurant.plan;
      if (orgAvatar) {
        const initials = restaurant.name
          .split(' ')
          .slice(0, 2)
          .map(function(w) { return w[0]; })
          .join('')
          .toUpperCase();
        orgAvatar.textContent = initials;
        orgAvatar.style.background = '#FDE8CE';
        orgAvatar.style.color = '#BA7517';
      }
    }

    // Populate user info from localStorage
    const token = localStorage.getItem('rb_token');
    const userEl = sb.querySelector('#sb-user');
    if (!token && userEl) {
      userEl.style.display = 'none';
    } else if (restaurant.email || restaurant.name) {
      const nameEl   = sb.querySelector('#sb-user-name');
      const roleEl   = sb.querySelector('#sb-user-role');
      const avatarEl = sb.querySelector('#sb-user-avatar');
      if (nameEl && restaurant.email) nameEl.textContent = restaurant.email;
      if (roleEl) roleEl.textContent = restaurant.role || 'Owner';
      if (avatarEl && restaurant.email) {
        avatarEl.textContent = restaurant.email.slice(0, 2).toUpperCase();
      }
    }

    // Role-based and feature-flag filtering
    const role = localStorage.getItem('rb_role') || restaurant.role || 'owner';
    const features = restaurant.features || {};
    const operationalRoles = ['caja', 'mesero', 'cocina', 'bar', 'domiciliario'];

    function hide(key) {
      const el = sb.querySelector('[data-key="' + key + '"]');
      if (el) el.style.display = 'none';
    }

    // Always hide the WhatsApp nav item (no standalone page)
    hide('whatsapp');

    if (operationalRoles.includes(role)) {
      // Operational staff: hide all admin sections, show only their page link
      const adminKeys = ['resumen', 'pedidos', 'reservaciones', 'salon', 'menu', 'menu-eng',
        'nps', 'fidelizacion', 'riesgo', 'staff', 'nomina', 'sucursales', 'settings', 'billing'];
      adminKeys.forEach(function(k) { hide(k); });

      const opsGroup = sb.querySelector('#sb-ops-group');
      if (opsGroup) opsGroup.style.display = '';
      const opsKeyMap = {
        'caja': 'ops-caja',
        'mesero': 'ops-mesero',
        'cocina': 'ops-cocina',
        'bar': 'ops-bar',
        'domiciliario': 'ops-domiciliario',
      };
      const opsKey = opsKeyMap[role];
      if (opsKey) {
        const opsEl = sb.querySelector('[data-key="' + opsKey + '"]');
        if (opsEl) opsEl.style.display = '';
      }
    } else if (role === 'gerente') {
      hide('billing');
      const locations = restaurant.locations || [];
      if (locations.length <= 1) hide('sucursales');
    }
    // owner / admin: show everything (already rendered)

    // Feature-flag gating
    if (features.module_loyalty === false) hide('fidelizacion');
    if (features.module_reservations === false) hide('reservaciones');

    // Fix 3: initialize mobile hamburger + drawer after sidebar is fully rendered
    initMobileSidebar();

    // Sede switcher: wire the sb-switcher element to a popover that lists
    // the org's locations and lets the user filter the dashboard to a sede.
    initSedeSwitcher(sb);
  });

  // ── Sede switcher (multi-sucursal navigation) ───────────────────────────
  // Shows a popover under #sb-switcher with the org's locations + Casa Matriz.
  // Only visible to owner/admin/gerente roles. On select, persists the choice
  // to localStorage (rb_branch_id for legacy header, rb_current_location_id
  // for X-Location-ID) and reloads the page so all data refetches.
  function initSedeSwitcher(sb) {
    const switcherEl = sb.querySelector('#sb-switcher');
    if (!switcherEl) return;

    const role = (localStorage.getItem('rb_role') || 'owner').toLowerCase();
    const operationalRoles = ['caja', 'mesero', 'cocina', 'bar', 'domiciliario'];
    if (operationalRoles.includes(role)) return; // staff can't switch sedes

    switcherEl.style.cursor = 'pointer';
    switcherEl.setAttribute('role', 'button');
    switcherEl.setAttribute('aria-haspopup', 'listbox');
    switcherEl.setAttribute('aria-expanded', 'false');

    let popover = null;
    let isOpen = false;

    function close() {
      if (popover) { popover.remove(); popover = null; }
      isOpen = false;
      switcherEl.setAttribute('aria-expanded', 'false');
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onKey);
    }

    function onDocClick(e) {
      if (!popover) return;
      if (popover.contains(e.target) || switcherEl.contains(e.target)) return;
      close();
    }

    function onKey(e) {
      if (e.key === 'Escape') close();
    }

    async function fetchBranches() {
      try {
        const headers = (typeof mesioHeaders === 'function')
          ? mesioHeaders()
          : { 'Authorization': 'Bearer ' + (localStorage.getItem('rb_token') || '') };
        const r = await fetch('/api/team/branches', { headers });
        if (!r.ok) return [];
        const data = await r.json();
        return Array.isArray(data.branches) ? data.branches : [];
      } catch (e) { console.warn('sede switcher: fetchBranches failed', e); return []; }
    }

    function applyAndReload(value) {
      try {
        if (value === 'matriz' || value === '' || value == null) {
          localStorage.removeItem('rb_branch_id');
          if (typeof mesioSetCurrentLocationId === 'function') mesioSetCurrentLocationId('all');
        } else {
          localStorage.setItem('rb_branch_id', String(value));
          if (typeof mesioSetCurrentLocationId === 'function') mesioSetCurrentLocationId(value);
        }
      } catch (e) { console.warn('sede switcher: persist failed', e); }
      window.location.reload();
    }

    async function open() {
      if (isOpen) return;
      isOpen = true;
      switcherEl.setAttribute('aria-expanded', 'true');

      const branches = await fetchBranches();
      const current = localStorage.getItem('rb_branch_id') || 'matriz';

      popover = document.createElement('div');
      popover.className = 'sb-switcher-popover';
      popover.setAttribute('role', 'listbox');
      popover.style.cssText = [
        'position:absolute',
        'left:8px',
        'right:8px',
        'top:' + (switcherEl.offsetTop + switcherEl.offsetHeight + 4) + 'px',
        'background:#fff',
        'border:1px solid var(--border-2,#e2e2dd)',
        'border-radius:10px',
        'box-shadow:0 8px 24px rgba(0,0,0,.08)',
        'z-index:1000',
        'overflow:hidden',
        'max-height:340px',
        'overflow-y:auto'
      ].join(';');

      // restaurants VIEW exposes BOTH name (org name, same for all peers of an org)
      // AND location_name (per-sede name like "Herradura Norte"). Prefer the
      // location_name in the dropdown so the user can distinguish sedes — falling
      // back to name if the VIEW doesn't carry location_name (older VIEW snapshots).
      const items = [
        { value: 'matriz', name: 'Casa Matriz', sub: 'Todas las sucursales' }
      ].concat(
        branches.map(b => {
          const sedeName = b.location_name || b.name || ('Sede ' + b.id);
          const sub = (b.location_name && b.name && b.location_name !== b.name) ? b.name : '';
          return { value: String(b.id), name: sedeName, sub: sub };
        })
      );

      items.forEach(function(it) {
        const row = document.createElement('div');
        row.setAttribute('role', 'option');
        const isActive = String(current) === String(it.value);
        row.setAttribute('aria-selected', isActive ? 'true' : 'false');
        row.style.cssText = [
          'padding:10px 14px',
          'cursor:pointer',
          'font-size:13px',
          'color:var(--text-1,#1a1a1a)',
          'border-bottom:1px solid var(--border-3,#f1f1ec)',
          'display:flex',
          'align-items:center',
          'gap:8px',
          isActive ? 'background:var(--brand-soft,#e8f5ee)' : ''
        ].join(';');
        const check = document.createElement('span');
        check.textContent = isActive ? '✓' : '';
        check.style.cssText = 'width:14px;color:var(--brand,#1D9E75);font-weight:700;flex-shrink:0;';
        const txt = document.createElement('div');
        txt.style.cssText = 'flex:1;min-width:0;';
        const name = document.createElement('div');
        name.textContent = it.name;
        name.style.cssText = 'font-weight:' + (isActive ? '600' : '500');
        txt.appendChild(name);
        if (it.sub) {
          const sub = document.createElement('div');
          sub.textContent = it.sub;
          sub.style.cssText = 'font-size:11px;color:var(--text-3,#888);margin-top:2px;';
          txt.appendChild(sub);
        }
        row.appendChild(check);
        row.appendChild(txt);
        row.addEventListener('mouseenter', function() {
          if (!isActive) row.style.background = 'var(--bg-2,#f7f7f3)';
        });
        row.addEventListener('mouseleave', function() {
          if (!isActive) row.style.background = '';
        });
        row.addEventListener('click', function() { applyAndReload(it.value); });
        popover.appendChild(row);
      });

      sb.appendChild(popover);
      // defer so this same click doesn't immediately close it
      setTimeout(function() {
        document.addEventListener('click', onDocClick);
        document.addEventListener('keydown', onKey);
      }, 0);
    }

    switcherEl.addEventListener('click', function(e) {
      e.stopPropagation();
      if (isOpen) close(); else open();
    });
  }
})();
