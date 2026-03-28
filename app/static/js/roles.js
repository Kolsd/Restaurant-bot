function doStaffLogout() {
    const staffRestaurantId = localStorage.getItem('rb_staff_restaurant_id');
    localStorage.clear();
    if (staffRestaurantId) {
        window.location.href = '/staff?r=' + staffRestaurantId;
    } else {
        window.location.href = '/staff';
    }
}

(async function() {
    const token = localStorage.getItem('rb_token');
    if (!token) { window.location.href = '/login'; return; }

    const page = window.location.pathname.replace('/', '').split('/')[0] || 'dashboard';

    try {
        const res = await fetch('/api/auth/verify-role?page=' + encodeURIComponent(page), {
            headers: { 'Authorization': 'Bearer ' + token }
        });

        if (res.status === 401) {
            localStorage.clear();
            window.location.href = '/login';
            return;
        }

        if (res.status === 403) {
            const body = await res.json();
            window.location.href = (body.detail && body.detail.redirect) || '/staff';
            return;
        }

        _buildRoleNav();
    } catch (e) { console.warn('Error verificando rol:', e); }

    function _buildRoleNav() {
        const ROLE_META = {
            mesero:       { icon: '🍽️', label: 'Mesero',    url: '/mesero'       },
            cocina:       { icon: '👨‍🍳', label: 'Cocina',     url: '/cocina'       },
            bar:          { icon: '🍹', label: 'Bar',         url: '/bar'          },
            caja:         { icon: '💰', label: 'Caja',        url: '/caja'         },
            domiciliario: { icon: '🛵', label: 'Domicilios',  url: '/domiciliario' },
        };
    
        const rawRole = localStorage.getItem('rb_role') || '';
        let roles = [];
        try {
            const parsed = JSON.parse(rawRole);
            roles = Array.isArray(parsed) ? parsed : rawRole.split(',').map(r => r.trim());
        } catch(e) {
            roles = rawRole.split(',').map(r => r.trim());
        }
        roles = roles.map(r => r.replace(/["\[\]\s]/g, '').trim());
    
        const navEl = document.getElementById('dynamic-role-nav');
        if (!navEl) return;
    
        navEl.style.display = 'flex';
        navEl.innerHTML = '';
        const currentPath = window.location.pathname;

        if (roles.includes('owner') || roles.includes('admin')) {
            const active = (p) => currentPath === p ? 'active' : '';
            navEl.innerHTML += `<a href="/dashboard" class="role-btn ${active('/dashboard')}">📊 Dashboard</a>`;
            navEl.innerHTML += `<a href="/mesero" class="role-btn ${active('/mesero')}">🍽️ Mesero</a>`;
            navEl.innerHTML += `<a href="/caja" class="role-btn ${active('/caja')}">💰 Caja</a>`;
            navEl.innerHTML += `<a href="/cocina" class="role-btn ${active('/cocina')}">👨‍🍳 Cocina</a>`;
            navEl.innerHTML += `<a href="/bar" class="role-btn ${active('/bar')}">🍹 Bar</a>`;
            return;
        }
    
        const validRoles = roles.filter(r => ROLE_META[r]);
        if (validRoles.length <= 1) return;

        validRoles.forEach(role => {
            const meta = ROLE_META[role];
            const a = document.createElement('a');
            a.href = meta.url;
            a.className = 'role-btn' + (currentPath === meta.url ? ' active' : '');
            a.textContent = meta.icon + ' ' + meta.label;
            navEl.appendChild(a);
        });
    }
})();