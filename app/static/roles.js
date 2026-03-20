/* app/static/roles.js */
document.addEventListener('DOMContentLoaded', () => {
    const token = localStorage.getItem('rb_token');
    const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    
    if (!token || !restaurant) {
        window.location.href = '/login';
        return;
    }

    // Los roles vienen del backend (ej: "owner", "admin", "waiter", "waiter,cashier")
    const userRoles = (restaurant.role || '').toLowerCase();
    const isAdmin = userRoles.includes('owner') || userRoles.includes('admin');
    
    const path = window.location.pathname;
    let hasAccess = false;

    // 1. VERIFICAR ACCESO
    if (isAdmin) {
        hasAccess = true;
    } else if (path.includes('/mesero') && userRoles.includes('waiter')) {
        hasAccess = true;
    } else if (path.includes('/cocina') && userRoles.includes('cook')) {
        hasAccess = true;
    } else if (path.includes('/caja') && userRoles.includes('cashier')) {
        hasAccess = true;
    }

    // Si no tiene acceso, lo pateamos a su vista principal o al login
    if (!hasAccess && !path.includes('/dashboard')) {
        alert('🔒 No tienes permisos para acceder a esta vista.');
        if (userRoles.includes('cashier')) window.location.href = '/caja';
        else if (userRoles.includes('waiter')) window.location.href = '/mesero';
        else if (userRoles.includes('cook')) window.location.href = '/cocina';
        else window.location.href = '/login';
        return;
    }

    // 2. RENDERIZAR BARRA DE NAVEGACIÓN DINÁMICA
    const navContainer = document.getElementById('dynamic-role-nav');
    if (navContainer) {
        let navHtml = '';
        
        if (isAdmin || userRoles.includes('waiter')) {
            navHtml += `<a href="/mesero" class="role-btn ${path.includes('/mesero') ? 'active' : ''}">Mesero</a>`;
        }
        if (isAdmin || userRoles.includes('cook')) {
            navHtml += `<a href="/cocina" class="role-btn ${path.includes('/cocina') ? 'active' : ''}">Cocina</a>`;
        }
        if (isAdmin || userRoles.includes('cashier')) {
            navHtml += `<a href="/caja" class="role-btn ${path.includes('/caja') ? 'active' : ''}">Caja</a>`;
        }
        if (isAdmin) {
            navHtml += `<a href="/dashboard" class="role-btn">Admin</a>`;
        }

        // Solo mostramos la barra si el usuario tiene más de 1 vista disponible (o es admin)
        if (navHtml.split('</a>').length > 2 || isAdmin) {
            navContainer.innerHTML = navHtml;
            navContainer.style.display = 'flex';
        } else {
            navContainer.style.display = 'none'; // Ocultar si solo tiene 1 rol
        }
    }
});