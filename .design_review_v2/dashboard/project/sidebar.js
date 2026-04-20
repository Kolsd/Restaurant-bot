/* Mesio — Shared sidebar renderer
   Usage: include on any page with <aside class="sidebar"></aside>
   Set `data-active` on <body> to highlight the current item, e.g.:
     <body data-active="pedidos">
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

    <div class="sb-switcher">
      <div style="width:20px;height:20px;border-radius:5px;background:#FDE8CE;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#BA7517;">LM</div>
      <div class="grow">
        <div class="sb-switcher-name">La Mesa — Chapinero</div>
        <div class="sb-switcher-sub">3 sucursales · Colombia</div>
      </div>
      <span class="chev">⌄</span>
    </div>

    <div class="sb-group">
      <div class="sb-group-label">Operaciones</div>
      <a class="sb-item" data-key="resumen" href="dashboard.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>
        Resumen
        <span class="kbd">R</span>
      </a>
      <a class="sb-item" data-key="pedidos" href="pedidos.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 5l5-3 5 3v6l-5 3-5-3V5z"/><path d="M3 5l5 3 5-3M8 8v6"/></svg>
        Pedidos
        <span class="badge">12</span>
      </a>
      <a class="sb-item" data-key="reservaciones" href="reservaciones.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="10" rx="1"/><path d="M5 3V1M11 3V1M2 6h12"/></svg>
        Reservaciones
      </a>
      <a class="sb-item" data-key="whatsapp" href="demo-chat.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8c0-3 2.5-5.5 6-5.5S14 5 14 8s-2.5 5.5-6 5.5c-1 0-2-.2-2.8-.6L3 14l.6-2.2C2.6 11 2 9.5 2 8z"/></svg>
        WhatsApp
        <span class="dot live" style="margin-left:auto;"></span>
      </a>
    </div>

    <div class="sb-group">
      <div class="sb-group-label">Restaurante</div>
      <a class="sb-item" data-key="salon" href="floorplan.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="12" height="12" rx="1"/><path d="M2 6h12M6 2v12"/></svg>
        Salón
      </a>
      <a class="sb-item" data-key="menu" href="menu-admin.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M3 8c0-2.8 2.2-5 5-5"/></svg>
        Menú
      </a>
      <a class="sb-item" data-key="menu-eng" href="menu-engineering.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 13V8l3-4h6l3 4v5"/><path d="M2 13h12M5 13v-3h6v3"/></svg>
        Menu Engineering
      </a>
    </div>

    <div class="sb-group">
      <div class="sb-group-label">Clientes</div>
      <a class="sb-item" data-key="nps" href="nps.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1.5l1.8 4.2 4.5.4-3.4 3 1 4.4L8 11.2 4.1 13.5l1-4.4-3.4-3 4.5-.4z"/></svg>
        NPS & Reseñas
      </a>
      <a class="sb-item" data-key="fidelizacion" href="fidelizacion.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="5" width="12" height="9" rx="1"/><path d="M2 8h12M8 5V2M5 5v-.5a1 1 0 011-1h4a1 1 0 011 1V5"/></svg>
        Fidelización
      </a>
      <a class="sb-item" data-key="riesgo" href="clientes-riesgo.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5.5" cy="5" r="2.5"/><path d="M1.5 13c0-2.2 1.8-4 4-4s4 1.8 4 4"/><circle cx="11.5" cy="6" r="2"/><path d="M9.5 13c0-1.6 1-3 2-3"/></svg>
        Clientes en riesgo
        <span class="badge" style="background: var(--warning-light); color: var(--warning-text);">8</span>
      </a>
    </div>

    <div class="sb-group">
      <div class="sb-group-label">Equipo</div>
      <a class="sb-item" data-key="staff" href="staff-hq.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="5" r="2.5"/><path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5"/></svg>
        Staff HQ
      </a>
      <a class="sb-item" data-key="nomina" href="nomina.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l2.5 1.5"/></svg>
        Nómina
      </a>
      <a class="sb-item" data-key="sucursales" href="sucursales.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 13V6l6-4 6 4v7"/><path d="M2 13h12M6 13V9h4v4"/></svg>
        Sucursales
      </a>
    </div>

    <div class="sb-bottom">
      <a class="sb-item" data-key="settings" href="settings.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3"/></svg>
        Configuración
      </a>
      <a class="sb-item" data-key="billing" href="billing.html">
        <svg class="sb-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="12" height="9" rx="1"/><path d="M2 7h12"/></svg>
        Facturación
      </a>
      <div class="sb-user" style="margin-top: 8px;">
        <div class="sb-avatar">CR</div>
        <div class="grow">
          <div class="sb-user-name">Carolina R.</div>
          <div class="sb-user-role">Administradora</div>
        </div>
        <span class="chev" style="color:var(--text-4);font-size:11px;">⌄</span>
      </div>
    </div>
  `;

  document.addEventListener('DOMContentLoaded', () => {
    const sb = document.querySelector('.sidebar');
    if (!sb) return;
    sb.innerHTML = SIDEBAR_HTML;
    const active = document.body.dataset.active;
    if (active) {
      const el = sb.querySelector(`[data-key="${active}"]`);
      if (el) el.classList.add('active');
    }
  });
})();
