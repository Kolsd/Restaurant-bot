(async function() {
    const token = localStorage.getItem('rb_token');
    if (!token) { window.location.href = '/login'; return; }

    // Toma el nombre de la página del URL: /mesero → "mesero", /caja → "caja"
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
            const redirect = (body.detail && body.detail.redirect) || '/staff';
            window.location.href = redirect;
            return;
        }
        // 200 = tiene acceso, no hace nada
    } catch (e) {
        console.warn('Error verificando rol:', e);
    }
})();