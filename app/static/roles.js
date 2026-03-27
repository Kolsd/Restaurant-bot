(async function() {
    const token = localStorage.getItem('rb_token');
    if (!token) { window.location.href = '/login'; return; }

    const page = window.location.pathname.replace('/', '').split('/')[0] || 'dashboard';
    
    console.log('roles.js — page:', page);

    try {
        const res = await fetch('/api/auth/verify-role?page=' + encodeURIComponent(page), {
            headers: { 'Authorization': 'Bearer ' + token }
        });

        console.log('roles.js — status:', res.status);
        const body = await res.json();
        console.log('roles.js — body:', JSON.stringify(body));

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
    } catch (e) {
        console.warn('Error verificando rol:', e);
    }
})();