// ── Función global de logout — disponible para todos los HTMLs ──
function doStaffLogout() {
    const staffRestaurantId = localStorage.getItem('rb_staff_restaurant_id');
    localStorage.clear();
    if (staffRestaurantId) {
        window.location.href = '/staff?r=' + staffRestaurantId;
    } else {
        window.location.href = '/staff';
    }
}

// ── Verificación de rol + construcción de barra de navegación ──
(async function() {
    const token = localStorage.getItem('rb_token');
    if (!token) { window.location.href = '/login'; return; }

    const page = window.location.pathname.replace('/', '').split('/')[0] || 'dashboard';

    try {
        const res = await fetch('/api/auth/verify-role?page=' + encodeURIComponent(page), {
            headers: { 'Authorization': 'Bearer ' + token }
        });

        const body = await res.json();

        if (res.status === 401) {
            localStorage.clear();
            window.location.href = '/login';
            return;
        }

        if (res.status === 403) {
            const redirect = (body.detail && body.detail.redirect) || '/staff';
            window.location.href = redirect;
            return;
        }

        // 200 — acceso permitido
        console.log('rb_restaurant:', localStorage.getItem('rb_restaurant'));
        console.log('rb_role:', localStorage.getItem('rb_role'));
        _buildRoleNav();

    } catch (e) {
        console.warn('Error verificando rol:', e);
    }

    function _buildRoleNav() {
        const ROLE_META = {
            mesero:       { icon: '🍽️', label: 'Mesero',    url: '/mesero'       },
            cocina:       { icon: '👨‍🍳', label: 'Cocina',     url: '/cocina'       },
            bar:          { icon: '🍹', label: 'Bar',         url: '/bar'          },
            caja:         { icon: '💰', label: 'Caja',        url: '/caja'         },
            domiciliario: { icon: '🛵', label: 'Domicilios',  url: '/domiciliario' },
        };
    
        const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
        let roleStr = restaurant.role || localStorage.getItem('rb_role') || '';
    
        // ✅ FIX: si el role es un JSON array string, parsearlo
        let roles = [];
        try {
            const parsed = JSON.parse(roleStr);
            if (Array.isArray(parsed)) {
                roles = parsed;
            } else {
                roles = roleStr.split(',').map(r => r.trim());
            }
        } catch(e) {
            roles = roleStr.split(',').map(r => r.trim());
        }
    
        // Limpiar comillas y espacios que puedan venir de Postgres
        roles = roles
            .map(r => r.replace(/["\[\]\s]/g, '').trim())
            .filter(r => ROLE_META[r]);
    
        if (roles.length <= 1) return;
    
        const navEl = document.getElementById('dynamic-role-nav');
        if (!navEl) return;
    
        navEl.style.display = 'flex';
        navEl.innerHTML = '';
    
        const currentPath = window.location.pathname;
    
        roles.forEach(role => {
            const meta = ROLE_META[role];
            const a = document.createElement('a');
            a.href = meta.url;
            a.className = 'role-btn' + (currentPath === meta.url ? ' active' : '');
            a.textContent = meta.icon + ' ' + meta.label;
            navEl.appendChild(a);
        });
    }
})();