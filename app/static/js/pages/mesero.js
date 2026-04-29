/* ═══════════════════════════════════════════════════
   Mesio — Mesero v2
   Auto-refresh 20s · zone filter · waiter alert banner
   ═══════════════════════════════════════════════════ */

// ── Auth guard ──────────────────────────────────────
const _token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token');
if (!_token) { window.location.href = '/login'; }

const _hdr = mesioHeaders;
const _staffId = localStorage.getItem('rb_staff_id') || null;

// ── State ───────────────────────────────────────────
let _currentZone = 'all';
let _allTables = [];

// ── XSS helper ──────────────────────────────────────
function _esc(s) {
  const el = document.createElement('div');
  el.textContent = String(s == null ? '' : s);
  return el.innerHTML;
}

// ── Elapsed time helper ──────────────────────────────
function _elapsed(isoStr) {
  if (!isoStr) return '';
  const iso = isoStr.endsWith('Z') ? isoStr : isoStr + 'Z';
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${mins} min`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

// ── Zone filter ──────────────────────────────────────
function setZone(zone) {
  _currentZone = zone;
  document.querySelectorAll('.m-zone-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.zone === zone);
    btn.setAttribute('aria-selected', btn.dataset.zone === zone ? 'true' : 'false');
  });
  _applyZoneFilter();
}

function _applyZoneFilter() {
  document.querySelectorAll('.m-tbl').forEach(tile => {
    const z = tile.dataset.zone || '';
    const show = _currentZone === 'all' || z === _currentZone;
    tile.classList.toggle('zone-hidden', !show);
  });
}

// ── Channel badge: origen del pedido (bot WhatsApp vs POS vs QR) ──
function _channelBadge(channel) {
  if (!channel) return '';
  const map = {
    whatsapp_bot: { icon: '💬', title: 'WhatsApp bot' },
    qr_pickup:    { icon: '📱', title: 'QR / pickup' },
    web:          { icon: '🌐', title: 'Web' },
    pos:          { icon: '🧾', title: 'POS' },
    manual:       { icon: '✋', title: 'Manual' },
  };
  const m = map[channel];
  if (!m) return '';
  return `<div class="m-tbl-channel" title="${m.title}" aria-label="${m.title}">${m.icon}</div>`;
}

// ── Status → CSS class + label ───────────────────────
function _tableState(t) {
  if (t.has_waiter_alert ?? false) return { cls: 'alert', label: '🙋 Llamó al mesero' };
  if (!(t.session_active ?? false)) return { cls: '', label: 'Libre' };
  const orders = t.pending_orders || [];
  const anyListo = orders.some(s => s === 'listo');
  const anyBill  = t.has_open_check ?? false;
  if (anyBill)   return { cls: 'fac',  label: 'Facturando' };
  if (anyListo)  return { cls: 'occ',  label: 'Comiendo' };
  if (orders.length) return { cls: 'sent', label: 'Esperando' };
  return { cls: 'sent', label: 'Sentados' };
}

// ── Render tables ─────────────────────────────────────
function _renderTables(tables) {
  _allTables = tables;
  const floor = document.getElementById('mesero-floor');
  if (!floor) return;

  if (!tables.length) {
    floor.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-3);">No hay mesas configuradas.</div>';
    return;
  }

  floor.innerHTML = tables.map(t => {
    const { cls, label } = _tableState(t);
    const name  = _esc(t.name || t.table_name || String(t.id));
    const cap   = _esc(String(t.capacity || ''));
    const zone  = t.zone || '';
    const total = t.current_total != null ? mesioFmt(t.current_total) : '';
    const sinceStr = t.session_started_at ? _elapsed(t.session_started_at) : '';
    const alertLabel = (t.has_waiter_alert ?? false) ? '<div class="m-tbl-alert">Atenci\u00f3n</div>' : '';
    const checkBadge = (t.has_open_check ?? false) ? '<div class="m-tbl-check-badge">Cobrando</div>' : '';
    const capBadge = cap ? `<div class="m-tbl-cap ${cls === 'alert' ? 'alert' : ''}">${cls === 'alert' ? '🙋' : cap + 'p'}</div>` : '';

    // Waiter attribution + channel: show WHO is serving (if known) and HOW the session started.
    const waiterLine = t.waiter_name
      ? `<div class="m-tbl-waiter" title="Atiende">Att: ${_esc(t.waiter_name)}</div>`
      : '';
    const channelBadge = _channelBadge(t.channel);

    const tileAriaLabel = `Mesa ${name}, ${label}`;
    return `<div class="m-tbl ${cls}" role="button" tabindex="0" aria-label="${_esc(tileAriaLabel)}" data-id="${_esc(String(t.id))}" data-zone="${_esc(zone)}">
      ${capBadge}
      ${channelBadge}
      <div class="m-tbl-num">${name}</div>
      <div class="m-tbl-state">${_esc(label)}</div>
      <div class="m-tbl-body">
        ${total ? `<div class="m-tbl-total">${total}</div>` : ''}
        ${sinceStr ? `<div class="m-tbl-time">${sinceStr}</div>` : ''}
        ${waiterLine}
        ${alertLabel}
        ${checkBadge}
      </div>
    </div>`;
  }).join('');

  // Click + keyboard → open POS
  floor.querySelectorAll('.m-tbl').forEach(tile => {
    tile.addEventListener('click', () => {
      const id = tile.dataset.id;
      const t = tables.find(x => String(x.id) === id);
      if (t) openTable(t);
    });
    tile.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        tile.click();
      }
    });
  });

  _applyZoneFilter();
  _updateActionBar();
}

// ── Open table → in-page modal with active orders + delivery actions ──
// Mesero stays on /mesero. Previously this redirected to /caja, but caja
// is a different role's view (cashier) and the mesero couldn't mark
// orders as 'entregado' from there. Backend already accepts mesero role
// for the 'entregado' status transition (see _STATUS_ROLE_MAP in tables.py).
async function openTable(t) {
  const existing = document.getElementById('mesero-table-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'mesero-table-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-label', 'Pedidos de la mesa ' + (t.name || t.id));
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:9999;';

  const card = document.createElement('div');
  card.style.cssText = 'background:var(--surface-2,#1e2535);border-radius:12px;padding:24px;min-width:320px;max-width:520px;width:92%;max-height:85vh;overflow-y:auto;';

  const title = document.createElement('h3');
  title.style.cssText = 'margin:0 0 4px;font-size:18px;color:var(--text-1,#fff);';
  title.textContent = 'Mesa ' + (t.name || t.table_name || t.id);
  card.appendChild(title);

  const subtitle = document.createElement('div');
  subtitle.style.cssText = 'margin:0 0 16px;font-size:12px;color:var(--text-3,#94a3b8);';
  if (t.session_started_at) subtitle.textContent = 'Sesión activa · ' + _elapsed(t.session_started_at);
  else if (t.waiter_name)   subtitle.textContent = 'Atiende: ' + t.waiter_name;
  card.appendChild(subtitle);

  const ordersDiv = document.createElement('div');
  ordersDiv.id = 'mesero-modal-orders';
  ordersDiv.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-3,#94a3b8);">Cargando…</div>';
  card.appendChild(ordersDiv);

  const footer = document.createElement('div');
  footer.style.cssText = 'display:flex;gap:8px;margin-top:16px;';

  // Cobrar mesa: physical-payment trigger by mesero. Picks service %,
  // marks every active order as generar_factura → caja sees them in
  // their queue ready to invoice.
  const cobrarBtn = document.createElement('button');
  cobrarBtn.className = 'm-btn m-btn--sm';
  cobrarBtn.style.cssText = 'flex:2;background:#10b981;color:#fff;font-weight:700;border:none;padding:10px;border-radius:8px;cursor:pointer;font-size:13px;';
  cobrarBtn.textContent = '💰 Cobrar mesa';
  cobrarBtn.addEventListener('click', () => _showCobrarServiceModal(t, modal));
  footer.appendChild(cobrarBtn);

  const closeBtn = document.createElement('button');
  closeBtn.className = 'm-btn m-btn--sm m-btn--ghost';
  closeBtn.style.cssText = 'flex:1;';
  closeBtn.textContent = 'Cerrar';
  closeBtn.addEventListener('click', () => modal.remove());
  footer.appendChild(closeBtn);
  card.appendChild(footer);

  modal.appendChild(card);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.addEventListener('keydown', function onEsc(e) {
    if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', onEsc); }
  });
  document.body.appendChild(modal);

  await _loadTableOrders(t.id, ordersDiv);
}

// ── Cobrar mesa: service % picker + send orders to caja queue ────────
async function _showCobrarServiceModal(table, parentModal) {
  const existing = document.getElementById('mesero-cobrar-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'mesero-cobrar-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:10001;';

  const card = document.createElement('div');
  card.style.cssText = 'background:var(--surface-2,#1e2535);border-radius:12px;padding:24px;min-width:320px;max-width:420px;width:92%;';

  const title = document.createElement('h3');
  title.style.cssText = 'margin:0 0 6px;font-size:17px;color:var(--text-1,#fff);';
  title.textContent = '¿Cobrar Mesa ' + (table.name || table.table_name || table.id) + '?';
  card.appendChild(title);

  const subtitle = document.createElement('div');
  subtitle.style.cssText = 'margin:0 0 18px;font-size:12px;color:var(--text-3,#94a3b8);line-height:1.4;';
  subtitle.textContent = 'El pedido se envía a caja con el % de servicio que elijas. Caja confirma el cobro físico.';
  card.appendChild(subtitle);

  const opts = [
    { pct: 0,   label: 'Sin servicio',    sub: '0%'    },
    { pct: 8,   label: 'Servicio 8%',     sub: 'Sugerido' },
    { pct: 10,  label: 'Servicio 10%',    sub: 'Premium' },
    { pct: -1,  label: 'Otro %',          sub: 'Personalizar' },
  ];
  const optsDiv = document.createElement('div');
  optsDiv.style.cssText = 'display:flex;flex-direction:column;gap:8px;margin-bottom:16px;';
  let chosenPct = 8;
  function highlight() {
    optsDiv.querySelectorAll('[data-opt]').forEach(el => {
      const p = Number(el.dataset.opt);
      const isActive = p === chosenPct;
      el.style.background  = isActive ? 'var(--brand-soft,#1f3d2f)' : 'var(--surface-3,#252d40)';
      el.style.borderColor = isActive ? '#10b981' : 'transparent';
    });
  }
  opts.forEach(o => {
    const row = document.createElement('button');
    row.type = 'button';
    row.dataset.opt = String(o.pct);
    row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-radius:8px;border:2px solid transparent;cursor:pointer;background:var(--surface-3,#252d40);color:var(--text-1,#fff);font-size:13px;font-family:inherit;';
    const left = document.createElement('span');
    left.textContent = o.label;
    left.style.fontWeight = '600';
    const right = document.createElement('span');
    right.textContent = o.sub;
    right.style.cssText = 'font-size:11px;color:var(--text-3,#94a3b8);';
    row.appendChild(left);
    row.appendChild(right);
    row.addEventListener('click', () => {
      if (o.pct === -1) {
        const v = window.prompt('% de servicio (0–25):', '12');
        const num = Math.min(25, Math.max(0, parseInt(v, 10)));
        if (!Number.isFinite(num)) return;
        chosenPct = num;
      } else {
        chosenPct = o.pct;
      }
      highlight();
    });
    optsDiv.appendChild(row);
  });
  card.appendChild(optsDiv);
  highlight();

  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:8px;';
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'm-btn m-btn--sm m-btn--ghost';
  cancelBtn.style.cssText = 'flex:1;';
  cancelBtn.textContent = 'Cancelar';
  cancelBtn.addEventListener('click', () => modal.remove());
  const okBtn = document.createElement('button');
  okBtn.className = 'm-btn m-btn--sm';
  okBtn.style.cssText = 'flex:2;background:#10b981;color:#fff;font-weight:700;border:none;padding:10px;border-radius:8px;cursor:pointer;';
  okBtn.textContent = 'Mandar a caja';
  okBtn.addEventListener('click', async () => {
    okBtn.disabled = true;
    okBtn.textContent = 'Enviando…';
    const ok = await _generateFacturaForTable(table.id, chosenPct);
    if (ok) {
      if (typeof mesioToast === 'function') mesioToast(`Mesa enviada a caja · servicio ${chosenPct}%`, 'success', 2400);
      modal.remove();
      if (parentModal) parentModal.remove();
      loadTables();
    } else {
      okBtn.disabled = false;
      okBtn.textContent = 'Mandar a caja';
      if (typeof mesioToast === 'function') mesioToast('No se pudo enviar a caja', 'error');
    }
  });
  actions.appendChild(cancelBtn);
  actions.appendChild(okBtn);
  card.appendChild(actions);

  modal.appendChild(card);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

async function _generateFacturaForTable(tableId, servicePct) {
  // Find every active table_order on this mesa and transition them to
  // generar_factura. Backend handler (tables.py update_order_status) marks
  // each as factura_generada AND notifies the customer via WhatsApp.
  try {
    const res = await fetch('/api/table-orders?table_id=' + encodeURIComponent(tableId), { headers: _hdr() });
    if (!res.ok) return false;
    const data = await res.json();
    const orders = (data.orders || []).filter(o => !_CLOSED_ORDER_STATUSES.includes(o.status));
    if (!orders.length) {
      if (typeof mesioToast === 'function') mesioToast('No hay pedidos activos en esta mesa', 'warning');
      return false;
    }
    const results = await Promise.all(orders.map(o =>
      fetch('/api/table-orders/' + encodeURIComponent(o.base_order_id || o.id) + '/status', {
        method: 'POST',
        headers: _hdr(),
        body: JSON.stringify({ status: 'generar_factura', suggested_service_pct: servicePct }),
      }).then(r => r.ok).catch(() => false)
    ));
    return results.every(Boolean);
  } catch (e) {
    return false;
  }
}

const _ACTIVE_ORDER_STATUSES = ['recibido', 'en_preparacion', 'listo'];
const _CLOSED_ORDER_STATUSES = ['factura_entregada', 'cancelado', 'closed'];
const _STATUS_LABELS = {
  recibido:        { txt: 'Recibido',     color: '#94a3b8', bg: '#1e2535' },
  en_preparacion:  { txt: 'En cocina',    color: '#fbbf24', bg: '#3d2f1f' },
  listo:           { txt: 'Listo',        color: '#10b981', bg: '#1f3d2f' },
  entregado:       { txt: 'Entregado',    color: '#60a5fa', bg: '#1f2d3d' },
  generar_factura: { txt: 'Cobrando',     color: '#a78bfa', bg: '#2d1f3d' },
};

async function _loadTableOrders(tableId, container) {
  try {
    const url = '/api/table-orders?table_id=' + encodeURIComponent(tableId);
    const res = await fetch(url, { headers: _hdr() });
    if (!res.ok) {
      container.innerHTML = '<div style="padding:20px;color:#ef4444;">No se pudieron cargar los pedidos (HTTP ' + res.status + ').</div>';
      return;
    }
    const data = await res.json();
    const all = data.orders || [];
    const orders = all.filter(o => !_CLOSED_ORDER_STATUSES.includes(o.status));

    if (!orders.length) {
      container.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-3,#94a3b8);">No hay pedidos activos en esta mesa.</div>';
      return;
    }

    container.innerHTML = '';
    orders.forEach(o => {
      const orderCard = document.createElement('div');
      orderCard.style.cssText = 'background:var(--surface-3,#252d40);border-radius:10px;padding:12px;margin-bottom:10px;';

      const head = document.createElement('div');
      head.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:8px;';
      const orderId = document.createElement('span');
      orderId.style.cssText = 'font-size:11px;color:var(--text-3,#94a3b8);font-family:monospace;';
      orderId.textContent = '#' + String(o.id || '').slice(-6);
      const statusInfo = _STATUS_LABELS[o.status] || { txt: o.status, color: '#94a3b8', bg: '#1e2535' };
      const statusBadge = document.createElement('span');
      statusBadge.style.cssText = 'font-size:11px;font-weight:600;padding:3px 8px;border-radius:6px;background:' + statusInfo.bg + ';color:' + statusInfo.color + ';';
      statusBadge.textContent = statusInfo.txt;
      head.appendChild(orderId);
      head.appendChild(statusBadge);
      orderCard.appendChild(head);

      const items = Array.isArray(o.items) ? o.items : [];
      if (items.length) {
        const itemsList = document.createElement('ul');
        itemsList.style.cssText = 'list-style:none;margin:0 0 10px;padding:0;font-size:13px;color:var(--text-2,#cbd5e1);';
        items.forEach(it => {
          const li = document.createElement('li');
          li.style.cssText = 'padding:2px 0;';
          li.textContent = (it.qty || it.quantity || 1) + '× ' + (it.name || '—');
          itemsList.appendChild(li);
        });
        orderCard.appendChild(itemsList);
      }

      if (o.total != null) {
        const totalRow = document.createElement('div');
        totalRow.style.cssText = 'font-size:12px;color:var(--text-3,#94a3b8);margin-bottom:8px;';
        totalRow.textContent = 'Total: ' + mesioFmt(o.total);
        orderCard.appendChild(totalRow);
      }

      // Action: only when the kitchen has finished and the food is ready
      // to deliver, or when the order is sitting at 'recibido' / 'en_preparacion'
      // and the mesero wants to short-circuit (e.g. food directly handed off).
      // Backend already validates the role permission.
      if (_ACTIVE_ORDER_STATUSES.includes(o.status)) {
        const btn = document.createElement('button');
        btn.className = 'm-btn m-btn--sm';
        btn.style.cssText = 'width:100%;background:#10b981;color:#fff;font-weight:600;border:none;padding:10px;border-radius:8px;cursor:pointer;';
        btn.textContent = '✓ Marcar entregado';
        btn.addEventListener('click', async function() {
          btn.disabled = true;
          btn.style.opacity = '0.6';
          btn.textContent = 'Marcando…';
          const ok = await _markOrderDelivered(o.id);
          if (ok) {
            if (typeof mesioToast === 'function') mesioToast('Pedido entregado', 'success', 1800);
            await _loadTableOrders(tableId, container);
            // Refresh the mesa grid in the background so the tile state updates
            loadTables();
          } else {
            btn.disabled = false;
            btn.style.opacity = '';
            btn.textContent = '✓ Marcar entregado';
            if (typeof mesioToast === 'function') mesioToast('No se pudo marcar entregado', 'error');
          }
        });
        orderCard.appendChild(btn);
      }

      container.appendChild(orderCard);
    });
  } catch (e) {
    container.innerHTML = '<div style="padding:20px;color:#ef4444;">Error de conexión.</div>';
  }
}

async function _markOrderDelivered(orderId) {
  try {
    const res = await fetch('/api/table-orders/' + encodeURIComponent(orderId) + '/status', {
      method: 'POST',
      headers: _hdr(),
      body: JSON.stringify({ status: 'entregado' }),
    });
    return res.ok;
  } catch (_) { return false; }
}

// ── Waiter alerts banner ─────────────────────────────
async function _loadAlerts() {
  try {
    const res = await fetch('/api/waiter-alerts?resolved=false', { headers: _hdr() });
    if (!res.ok) return;
    const data = await res.json();
    const alerts = data.alerts || data || [];
    const banner = document.getElementById('mesero-alert-banner');
    if (!banner) return;
    if (!alerts.length) { banner.classList.add('hidden'); return; }
    banner.classList.remove('hidden');
    const first = alerts[0];
    const tableEl = document.createElement('strong');
    tableEl.textContent = first.table_name || `Mesa ${first.table_id}`;
    banner.innerHTML = '';
    const icon = document.createTextNode('🔔 ');
    banner.appendChild(icon);
    banner.appendChild(tableEl);
    const msg = document.createTextNode(` llamó al mesero · ${alerts.length} alerta${alerts.length > 1 ? 's' : ''}`);
    banner.appendChild(msg);
    const btn = document.createElement('button');
    btn.className = 'm-btn m-btn--sm m-btn--ghost';
    btn.style.cssText = 'margin-left:auto;font-size:11px;min-height:28px;';
    btn.textContent = 'Ver todas';
    banner.appendChild(btn);
    btn.addEventListener('click', e => { e.stopPropagation(); _showAllAlerts(alerts); });
  } catch (_) { /* non-critical */ }
}

function _showAllAlerts(alerts) {
  const existing = document.getElementById('mesero-alerts-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'mesero-alerts-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-label', 'Alertas de mesero');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:9999;';

  const card = document.createElement('div');
  card.style.cssText = 'background:var(--surface-2,#1e2535);border-radius:12px;padding:24px;min-width:280px;max-width:380px;width:90%;';

  const title = document.createElement('h3');
  title.style.cssText = 'margin:0 0 16px;font-size:16px;color:var(--text-1,#fff);';
  title.textContent = `${alerts.length} alerta${alerts.length > 1 ? 's' : ''} activa${alerts.length > 1 ? 's' : ''}`;
  card.appendChild(title);

  const list = document.createElement('ul');
  list.style.cssText = 'list-style:none;margin:0 0 16px;padding:0;display:flex;flex-direction:column;gap:8px;';
  alerts.forEach(a => {
    const li = document.createElement('li');
    li.style.cssText = 'background:var(--surface-3,#252d40);border-radius:8px;padding:10px 12px;display:flex;gap:8px;align-items:center;';
    const badge = document.createElement('span');
    badge.style.cssText = 'font-size:11px;font-weight:600;color:var(--warn,#f59e0b);white-space:nowrap;';
    badge.textContent = a.table_name || `Mesa ${a.table_id}`;
    const type = document.createElement('span');
    type.style.cssText = 'font-size:12px;color:var(--text-2,#94a3b8);';
    type.textContent = a.alert_type || '';
    li.appendChild(badge);
    li.appendChild(type);
    list.appendChild(li);
  });
  card.appendChild(list);

  const closeBtn = document.createElement('button');
  closeBtn.className = 'm-btn m-btn--sm';
  closeBtn.style.cssText = 'width:100%;';
  closeBtn.textContent = 'Cerrar';
  closeBtn.addEventListener('click', () => modal.remove());
  card.appendChild(closeBtn);

  modal.appendChild(card);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
  closeBtn.focus();
}

// ── Request help alert for a table ───────────────────
async function _callAlert(tableId, tableName) {
  try {
    const restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    const botNumber = restaurant.whatsapp_number || '';
    const res = await fetch('/api/waiter-alerts/admin-call', {
      method: 'POST',
      headers: _hdr(),
      body: JSON.stringify({ table_name: tableName, bot_number: botNumber, alert_type: 'help' })
    });
    if (res.ok) {
      mesioToast('Alerta enviada', 'success', 2000);
    } else {
      mesioToast('No se pudo enviar la alerta', 'error');
    }
  } catch (_) {
    mesioToast('Error de conexi\u00f3n', 'error');
  }
}

// ── Bottom action bar ─────────────────────────────────
async function _updateActionBar() {
  if (!_staffId) return;
  try {
    // Staff performance for this week
    const res = await fetch(`/api/stats/staff-performance?staff_id=${encodeURIComponent(_staffId)}&weeks=1`, { headers: _hdr() });
    if (!res.ok) return;
    const data = await res.json();

    const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setVal('m-stat-ventas', data.total_sales != null ? mesioFmt(data.total_sales) : '—');
    setVal('m-stat-mesas', data.tables_served != null ? String(data.tables_served) : '—');
    setVal('m-stat-ticket', data.avg_ticket != null ? mesioFmt(data.avg_ticket) : '—');
  } catch (_) { /* non-critical */ }

  try {
    const res2 = await fetch('/api/stats/tips-pool', { headers: _hdr() });
    if (!res2.ok) return;
    const data2 = await res2.json();
    const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setVal('m-stat-propinas', data2.my_pool != null ? mesioFmt(data2.my_pool) : '—');
  } catch (_) { /* non-critical */ }
}

// ── Load tables ───────────────────────────────────────
async function loadTables() {
  try {
    const res = await fetch('/api/pos/tables-status', { headers: _hdr() });
    mesioTrackFetch(res.ok);
    if (!res.ok) { if (res.status === 401) { window.location.href = '/login'; return; } throw new Error(); }
    const data = await res.json();
    const tables = data.tables || data || [];
    _renderTables(tables);
    const subEl = document.getElementById('mesero-sub');
    if (subEl) {
      const n = tables.length;
      const active = tables.filter(t => t.session_active || t.has_open_check).length;
      subEl.textContent = `${n} mesa${n === 1 ? '' : 's'} · ${active} activa${active === 1 ? '' : 's'}`;
    }
    _loadAlerts();
  } catch (_) {
    mesioTrackFetch(false);
  }
}

// ── Active orders modal (cross-table flat list) ────────
// Closes DISCONNECT #7 (mesero tab 'Pedidos activos'). The redesign
// removed this view and forced the user to click each mesa to see
// orders. Now there's a single button → modal with all active orders.
async function openActiveOrdersModal() {
  const existing = document.getElementById('mesero-orders-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'mesero-orders-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:flex-start;justify-content:center;z-index:9999;padding:40px 16px;';

  const card = document.createElement('div');
  card.style.cssText = 'background:var(--surface-2,#1e2535);border-radius:12px;padding:24px;min-width:320px;max-width:680px;width:100%;max-height:85vh;overflow-y:auto;';

  const title = document.createElement('h3');
  title.style.cssText = 'margin:0 0 16px;font-size:18px;color:var(--text-1,#fff);display:flex;align-items:center;gap:8px;';
  title.innerHTML = '📋 Pedidos activos';
  card.appendChild(title);

  const body = document.createElement('div');
  body.id = 'mesero-orders-modal-body';
  body.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-3,#94a3b8);">Cargando…</div>';
  card.appendChild(body);

  const footer = document.createElement('div');
  footer.style.cssText = 'display:flex;gap:8px;margin-top:16px;';
  const closeBtn = document.createElement('button');
  closeBtn.className = 'm-btn m-btn--sm m-btn--ghost';
  closeBtn.style.cssText = 'flex:1;';
  closeBtn.textContent = 'Cerrar';
  closeBtn.addEventListener('click', () => modal.remove());
  footer.appendChild(closeBtn);
  card.appendChild(footer);

  modal.appendChild(card);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.addEventListener('keydown', function onEsc(e) {
    if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', onEsc); }
  });
  document.body.appendChild(modal);

  try {
    const res = await fetch('/api/table-orders', { headers: _hdr() });
    if (!res.ok) {
      body.innerHTML = '<div style="padding:20px;color:#ef4444;">No se pudieron cargar los pedidos (HTTP ' + res.status + ').</div>';
      return;
    }
    const data = await res.json();
    const all = data.orders || [];
    const orders = all.filter(o => !_CLOSED_ORDER_STATUSES.includes(o.status));
    if (!orders.length) {
      body.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-3,#94a3b8);">No hay pedidos activos en ninguna mesa.</div>';
      return;
    }
    // Group by table
    const byTable = {};
    orders.forEach(o => {
      const k = String(o.table_id || 'sin-mesa');
      if (!byTable[k]) byTable[k] = { table_id: o.table_id, table_name: o.table_name, orders: [] };
      byTable[k].orders.push(o);
    });

    body.innerHTML = '';
    Object.values(byTable).forEach(grp => {
      const grpEl = document.createElement('div');
      grpEl.style.cssText = 'margin-bottom:14px;';
      const head = document.createElement('div');
      head.style.cssText = 'font-size:13px;font-weight:600;color:var(--text-1,#fff);margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;';
      const name = document.createElement('span');
      name.textContent = 'Mesa ' + (grp.table_name || grp.table_id || '—');
      const count = document.createElement('span');
      count.style.cssText = 'font-size:11px;color:var(--text-3,#94a3b8);font-weight:500;';
      count.textContent = grp.orders.length + ' pedido' + (grp.orders.length === 1 ? '' : 's');
      head.appendChild(name);
      head.appendChild(count);
      grpEl.appendChild(head);

      grp.orders.forEach(o => {
        const oCard = document.createElement('div');
        oCard.style.cssText = 'background:var(--surface-3,#252d40);border-radius:8px;padding:10px 12px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;gap:8px;cursor:pointer;';
        const left = document.createElement('div');
        left.style.cssText = 'flex:1;min-width:0;';
        const idLine = document.createElement('div');
        idLine.style.cssText = 'font-size:11px;color:var(--text-3,#94a3b8);font-family:monospace;';
        idLine.textContent = '#' + String(o.id || '').slice(-6);
        const itemsLine = document.createElement('div');
        itemsLine.style.cssText = 'font-size:12px;color:var(--text-2,#cbd5e1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
        const items = Array.isArray(o.items) ? o.items : [];
        itemsLine.textContent = items.length
          ? items.map(it => (it.qty || 1) + '× ' + (it.name || '?')).join(', ')
          : '—';
        left.appendChild(idLine);
        left.appendChild(itemsLine);
        const right = document.createElement('div');
        const statusInfo = _STATUS_LABELS[o.status] || { txt: o.status, color: '#94a3b8', bg: '#1e2535' };
        const badge = document.createElement('span');
        badge.style.cssText = 'font-size:11px;font-weight:600;padding:3px 8px;border-radius:6px;background:' + statusInfo.bg + ';color:' + statusInfo.color + ';';
        badge.textContent = statusInfo.txt;
        right.appendChild(badge);
        oCard.appendChild(left);
        oCard.appendChild(right);
        oCard.addEventListener('click', () => {
          modal.remove();
          openTable({ id: grp.table_id, name: grp.table_name });
        });
        grpEl.appendChild(oCard);
      });
      body.appendChild(grpEl);
    });
  } catch (e) {
    body.innerHTML = '<div style="padding:20px;color:#ef4444;">Error de conexión.</div>';
  }
}

// ── Active chats modal (bot conversations visibility) ─
// Closes DISCONNECT #5 (mesero/caja no ve chats activos del bot).
// Lists recent conversations + click row → individual chat history.
async function openChatsModal() {
  const existing = document.getElementById('mesero-chats-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'mesero-chats-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:flex-start;justify-content:center;z-index:9999;padding:40px 16px;';

  const card = document.createElement('div');
  card.style.cssText = 'background:var(--surface-2,#1e2535);border-radius:12px;padding:24px;min-width:320px;max-width:580px;width:100%;max-height:85vh;display:flex;flex-direction:column;';

  const title = document.createElement('h3');
  title.style.cssText = 'margin:0 0 16px;font-size:18px;color:var(--text-1,#fff);display:flex;align-items:center;gap:8px;';
  title.innerHTML = '💬 Chats activos';
  card.appendChild(title);

  const body = document.createElement('div');
  body.id = 'mesero-chats-modal-body';
  body.style.cssText = 'flex:1;overflow-y:auto;';
  body.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-3,#94a3b8);">Cargando…</div>';
  card.appendChild(body);

  const footer = document.createElement('div');
  footer.style.cssText = 'display:flex;gap:8px;margin-top:16px;flex-shrink:0;';
  const closeBtn = document.createElement('button');
  closeBtn.className = 'm-btn m-btn--sm m-btn--ghost';
  closeBtn.style.cssText = 'flex:1;';
  closeBtn.textContent = 'Cerrar';
  closeBtn.addEventListener('click', () => modal.remove());
  footer.appendChild(closeBtn);
  card.appendChild(footer);

  modal.appendChild(card);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.addEventListener('keydown', function onEsc(e) {
    if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', onEsc); }
  });
  document.body.appendChild(modal);

  try {
    const res = await fetch('/api/dashboard/conversations', { headers: _hdr() });
    if (!res.ok) {
      body.innerHTML = '<div style="padding:20px;color:#ef4444;">No se pudieron cargar las conversaciones (HTTP ' + res.status + ').</div>';
      return;
    }
    const data = await res.json();
    const convs = data.conversations || [];
    if (!convs.length) {
      body.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-3,#94a3b8);">No hay conversaciones recientes.</div>';
      return;
    }
    body.innerHTML = '';
    convs.forEach(c => {
      const row = document.createElement('div');
      row.style.cssText = 'background:var(--surface-3,#252d40);border-radius:10px;padding:12px;margin-bottom:8px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:12px;';
      const left = document.createElement('div');
      left.style.cssText = 'flex:1;min-width:0;';
      const phoneEl = document.createElement('div');
      phoneEl.style.cssText = 'font-size:13px;font-weight:600;color:var(--text-1,#fff);';
      phoneEl.textContent = _maskPhone(c.phone);
      const previewEl = document.createElement('div');
      previewEl.style.cssText = 'font-size:11px;color:var(--text-3,#94a3b8);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px;';
      previewEl.textContent = (c.last_message || c.preview || '').slice(0, 80) || '—';
      left.appendChild(phoneEl);
      left.appendChild(previewEl);
      const right = document.createElement('div');
      right.style.cssText = 'font-size:10px;color:var(--text-3,#94a3b8);text-align:right;flex-shrink:0;';
      if (c.last_at || c.updated_at) {
        const iso = (c.last_at || c.updated_at);
        right.textContent = _elapsed(iso);
      }
      row.appendChild(left);
      row.appendChild(right);
      row.addEventListener('click', () => openChatHistoryModal(c.phone));
      body.appendChild(row);
    });
  } catch (e) {
    body.innerHTML = '<div style="padding:20px;color:#ef4444;">Error de conexión.</div>';
  }
}

function _maskPhone(p) {
  if (!p) return '';
  const s = String(p);
  if (s.length <= 4) return s;
  return s.slice(0, -4).replace(/\d/g, '•') + s.slice(-4);
}

async function openChatHistoryModal(phone) {
  const existing = document.getElementById('mesero-chat-history-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'mesero-chat-history-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:flex-start;justify-content:center;z-index:10000;padding:40px 16px;';

  const card = document.createElement('div');
  card.style.cssText = 'background:var(--surface-2,#1e2535);border-radius:12px;padding:20px;min-width:320px;max-width:520px;width:100%;max-height:90vh;display:flex;flex-direction:column;';

  const head = document.createElement('div');
  head.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-shrink:0;';
  const title = document.createElement('h3');
  title.style.cssText = 'margin:0;font-size:15px;color:var(--text-1,#fff);';
  title.textContent = 'Chat ' + _maskPhone(phone);
  const xBtn = document.createElement('button');
  xBtn.className = 'm-btn m-btn--sm m-btn--ghost';
  xBtn.textContent = '✕';
  xBtn.style.cssText = 'min-width:28px;padding:4px 10px;';
  xBtn.addEventListener('click', () => modal.remove());
  head.appendChild(title);
  head.appendChild(xBtn);
  card.appendChild(head);

  const msgs = document.createElement('div');
  msgs.style.cssText = 'flex:1;overflow-y:auto;background:#0e1117;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:6px;';
  msgs.innerHTML = '<div style="color:#94a3b8;text-align:center;padding:12px;font-size:12px;">Cargando historial…</div>';
  card.appendChild(msgs);

  modal.appendChild(card);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);

  try {
    const res = await fetch('/api/conversations/' + encodeURIComponent(phone), { headers: _hdr() });
    if (!res.ok) {
      msgs.innerHTML = '<div style="color:#ef4444;text-align:center;padding:12px;">No se pudo cargar (HTTP ' + res.status + ').</div>';
      return;
    }
    const d = await res.json();
    const history = d.history || [];
    if (!history.length) {
      msgs.innerHTML = '<div style="color:#94a3b8;text-align:center;padding:12px;font-size:12px;">Sin mensajes.</div>';
      return;
    }
    msgs.innerHTML = '';
    history.forEach(m => {
      const isUser = m.role === 'user';
      const bubble = document.createElement('div');
      bubble.style.cssText = 'max-width:80%;padding:8px 12px;border-radius:10px;font-size:13px;line-height:1.4;word-wrap:break-word;align-self:' + (isUser ? 'flex-start' : 'flex-end') + ';background:' + (isUser ? '#252d40' : '#1f3d2f') + ';color:' + (isUser ? '#cbd5e1' : '#d1fae5') + ';';
      const content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
      bubble.textContent = content;
      msgs.appendChild(bubble);
    });
    msgs.scrollTop = msgs.scrollHeight;
  } catch (e) {
    msgs.innerHTML = '<div style="color:#ef4444;text-align:center;padding:12px;">Error de conexión.</div>';
  }
}

// ── Zone tabs setup ───────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.m-zone-btn').forEach(btn => {
    btn.addEventListener('click', () => setZone(btn.dataset.zone || 'all'));
  });

  const ordersBtn = document.getElementById('m-btn-orders');
  if (ordersBtn) ordersBtn.addEventListener('click', openActiveOrdersModal);
  const chatsBtn = document.getElementById('m-btn-chats');
  if (chatsBtn) chatsBtn.addEventListener('click', openChatsModal);

  loadTables();
  mesioInterval(loadTables, 20000);
});
