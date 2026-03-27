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

    // ── Widget de turno (clock-in / clock-out) para operativos ──────────────
    const staffId = localStorage.getItem('rb_staff_id');
    if (staffId) {
        const token = localStorage.getItem('rb_token') || '';
        const hdr   = { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };

        const widget = document.createElement('div');
        widget.style.cssText = 'display:flex;gap:6px;align-items:center;margin-left:8px;';

        const ciBtn = document.createElement('button');
        ciBtn.textContent = '▶ Entrada';
        ciBtn.style.cssText = 'background:#1D9E75;color:#fff;border:none;padding:5px 11px;border-radius:6px;font-size:12px;cursor:pointer;font-weight:600;';

        const coBtn = document.createElement('button');
        coBtn.textContent = '■ Salida';
        coBtn.style.cssText = 'background:none;color:#555;border:1px solid #ccc;padding:5px 11px;border-radius:6px;font-size:12px;cursor:pointer;font-weight:600;';

        const _clockAction = async (btn, endpoint, successText) => {
            btn.disabled = true;
            try {
                const r = await fetch(endpoint, { method: 'POST', headers: hdr });
                if (r.ok) {
                    const prev = btn.textContent;
                    btn.textContent = '✓ ' + successText;
                    setTimeout(() => { btn.textContent = prev; btn.disabled = false; }, 2000);
                } else {
                    const e = await r.json().catch(() => ({}));
                    alert(e.detail || 'Error al registrar turno');
                    btn.disabled = false;
                }
            } catch { btn.disabled = false; }
        };

        ciBtn.addEventListener('click', () => _clockAction(ciBtn, '/api/staff/self/clock-in',  'Entrada OK'));
        coBtn.addEventListener('click', () => _clockAction(coBtn, '/api/staff/self/clock-out', 'Salida OK'));

        widget.appendChild(ciBtn);
        widget.appendChild(coBtn);

        // Insertar junto a la barra de roles (o al final del header si no la hay)
        const anchor = roleNavContainer.parentNode || document.body;
        anchor.appendChild(widget);
    }
});