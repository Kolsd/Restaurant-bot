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
  
    // Si es Dueño/Admin, mostrarle todo por defecto
    if (roles.includes('owner') || roles.includes('admin')) {
        html += `<a href="/dashboard" class="role-btn ${currentPath === '/dashboard' ? 'active' : ''}">📊 Dashboard</a>`;
        html += `<a href="/mesero" class="role-btn ${currentPath === '/mesero' ? 'active' : ''}">🍽️ Mesero</a>`;
        html += `<a href="/caja" class="role-btn ${currentPath === '/caja' ? 'active' : ''}">💰 Caja</a>`;
        html += `<a href="/cocina" class="role-btn ${currentPath === '/cocina' ? 'active' : ''}">👨‍🍳 Cocina</a>`;
        html += `<a href="/domiciliario" class="role-btn ${currentPath === '/domiciliario' ? 'active' : ''}">🛵 Domicilios</a>`;
    } 
    else {
        // Dibujar solo los botones a los que tiene acceso
        if (roles.includes('waiter')) {
            html += `<a href="/mesero" class="role-btn ${currentPath === '/mesero' ? 'active' : ''}">🍽️ Mesero</a>`;
        }
        if (roles.includes('cashier')) {
            html += `<a href="/caja" class="role-btn ${currentPath === '/caja' ? 'active' : ''}">💰 Caja</a>`;
        }
        if (roles.includes('cook')) {
            html += `<a href="/cocina" class="role-btn ${currentPath === '/cocina' ? 'active' : ''}">👨‍🍳 Cocina</a>`;
        }
        if (roles.includes('delivery') || roles.includes('domiciliario')) {
            html += `<a href="/domiciliario" class=\"role-btn ${currentPath === '/domiciliario' ? 'active' : ''}\">🛵 Domicilios</a>`;
        }
    }
  
    // Si se generó al menos un botón, mostrar la barra
    if (html !== '') {
        roleNavContainer.innerHTML = html;
        roleNavContainer.style.display = 'flex';
    }
  });