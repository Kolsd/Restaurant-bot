/* ═══════════════════════════════════════════════════
   Mesio — Barra de Navegación Dinámica Multirol
   app/static/roles.js
═══════════════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════
   Mesio — Barra de Navegación Dinámica Multirol
   app/static/roles.js
═══════════════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', () => {
    const roleNavContainer = document.getElementById('dynamic-role-nav');
    if (!roleNavContainer) return;
  
    // Leer el rol guardado durante el login
    const rawRole = localStorage.getItem('rb_role') || 'owner';
    const roles = rawRole.toLowerCase().split(',');
    
    // Saber en qué página estamos actualmente
    const currentPath = window.location.pathname;
    
    let html = '';
  
    const has = (...names) => names.some(n => roles.includes(n));
    const active = (path) => currentPath === path ? 'active' : '';

    if (has('owner', 'admin')) {
        // Owner/Admin ven todo
        html += `<a href="/dashboard" class="role-btn ${active('/dashboard')}">📊 Dashboard</a>`;
        html += `<a href="/mesero" class="role-btn ${active('/mesero')}">🍽️ Mesero</a>`;
        html += `<a href="/caja" class="role-btn ${active('/caja')}">💰 Caja</a>`;
        html += `<a href="/cocina" class="role-btn ${active('/cocina')}">👨‍🍳 Cocina</a>`;
        html += `<a href="/domiciliario" class="role-btn ${active('/domiciliario')}">🛵 Domicilios</a>`;
        html += `<a href="/bar" class="role-btn ${active('/bar')}">🍹 Bar</a>`;
    }
    else {
        // Dibujar solo los botones a los que tiene acceso (roles en español e inglés)
        if (has('gerente')) {
            html += `<a href="/dashboard" class="role-btn ${active('/dashboard')}">📊 Dashboard</a>`;
        }
        if (has('waiter', 'mesero')) {
            html += `<a href="/mesero" class="role-btn ${active('/mesero')}">🍽️ Mesero</a>`;
        }
        if (has('cashier', 'cajero', 'caja')) {
            html += `<a href="/caja" class="role-btn ${active('/caja')}">💰 Caja</a>`;
        }
        if (has('cook', 'cocina', 'cocinero')) {
            html += `<a href="/cocina" class="role-btn ${active('/cocina')}">👨‍🍳 Cocina</a>`;
        }
        if (has('delivery', 'domiciliario')) {
            html += `<a href="/domiciliario" class="role-btn ${active('/domiciliario')}">🛵 Domicilios</a>`;
        }
        if (has('bar')) {
            html += `<a href="/bar" class="role-btn ${active('/bar')}">🍹 Bar</a>`;
        }
    }
  
    // Si se generó al menos un botón, mostrar la barra
    if (html !== '') {
        roleNavContainer.innerHTML = html;
        roleNavContainer.style.display = 'flex';
    }
});