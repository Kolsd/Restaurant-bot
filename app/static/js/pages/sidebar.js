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
 *   demo-chat.html          → /dashboard  (WhatsApp tab, no standalone page yet)
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
      // Operational staff: hide all admin sections, keep only staff-hq link
      const adminKeys = ['resumen', 'pedidos', 'reservaciones', 'salon', 'menu', 'menu-eng',
        'nps', 'fidelizacion', 'riesgo', 'staff', 'nomina', 'sucursales', 'settings', 'billing'];
      adminKeys.forEach(function(k) { hide(k); });
    } else if (role === 'gerente') {
      hide('billing');
      const locations = restaurant.locations || [];
      if (locations.length <= 1) hide('sucursales');
    }
    // owner / admin: show everything (already rendered)

    // Feature-flag gating
    if (features.module_loyalty === false) hide('fidelizacion');
    if (features.module_reservations === false) hide('reservaciones');
  });
})();
