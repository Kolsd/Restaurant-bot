/* ═══════════════════════════════════════════════════
   Mesio Dashboard — NPS & Inventario
   app/static/dashboard-nps-inventory.js
═══════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════
// NPS
// ══════════════════════════════════════════════════

let npsPeriod = 'month';
let npsChart  = null;

async function loadNPS() {
  const h = window._dashHeaders;
  // 🛡️ Limpiamos el score actual para dar feedback visual de carga
  const scoreEl = document.getElementById('nps-score-display');
  if (scoreEl) scoreEl.innerHTML = '<div style="text-align:center;color:#888;padding:2rem;">Cargando sucursal...</div>';
  
  try {
    const [rStats, rResponses] = await Promise.all([
      fetch(`/api/nps/stats?period=${npsPeriod}`, { headers: h }),
      fetch(`/api/nps/responses?period=${npsPeriod}&limit=50`, { headers: h })
    ]);

    if (!rStats.ok || !rResponses.ok) {
      document.getElementById('nps-container').innerHTML =
        '<div class="empty-state">Error cargando datos NPS.</div>';
      return;
    }

    const stats     = await rStats.json();
    const { responses } = await rResponses.json();

    renderNPSStats(stats);
    renderNPSChart(stats.distribution || {});
    renderNPSResponses(responses || []);
    loadGoogleMapsURL();
  } catch(e) {
    console.error('loadNPS:', e);
  }
}

function renderNPSStats(s) {
  const score = s.nps_score || 0;
  const color = score >= 50 ? '#1D9E75' : score >= 0 ? '#FAC775' : '#E24B4A';

  const el = document.getElementById('nps-score-display');
  if (el) {
    el.innerHTML = `
      <div style="text-align:center;">
        <div style="font-size:56px;font-weight:700;color:${color};line-height:1;">${score}</div>
        <div style="font-size:12px;color:#888;margin-top:4px;">NPS Score</div>
        <div style="margin-top:12px;display:flex;gap:16px;justify-content:center;">
          <div style="text-align:center;">
            <div style="font-size:22px;font-weight:600;color:#1D9E75;">${s.promoters||0}</div>
            <div style="font-size:11px;color:#888;">Promotores</div>
          </div>
          <div style="width:1px;background:#e0e0d8;"></div>
          <div style="text-align:center;">
            <div style="font-size:22px;font-weight:600;color:#E24B4A;">${s.detractors||0}</div>
            <div style="font-size:11px;color:#888;">Detractores</div>
          </div>
          <div style="width:1px;background:#e0e0d8;"></div>
          <div style="text-align:center;">
            <div style="font-size:22px;font-weight:600;">${s.total||0}</div>
            <div style="font-size:11px;color:#888;">Total</div>
          </div>
        </div>
        <div style="margin-top:12px;font-size:13px;color:#555;">
          Promedio: <strong>${(s.avg_score||0).toFixed(1)} / 5</strong>
        </div>
      </div>`;
  }
}

function renderNPSChart(dist) {
  const ctx = document.getElementById('nps-chart');
  if (!ctx) return;
  if (npsChart) npsChart.destroy();

  const labels = ['⭐ 1', '⭐⭐ 2', '⭐⭐⭐ 3', '⭐⭐⭐⭐ 4', '⭐⭐⭐⭐⭐ 5'];
  const data   = [dist[1]||0, dist[2]||0, dist[3]||0, dist[4]||0, dist[5]||0];
  const colors = ['#E24B4A','#FA8C75','#FAC775','#86C38A','#1D9E75'];

  npsChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors,
        borderRadius: 6,
        borderWidth: 0
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, ticks: { stepSize: 1, font: { size: 11 } }, grid: { color: '#f0f0e8' } },
        x: { ticks: { font: { size: 11 } }, grid: { display: false } }
      }
    }
  });
}

function renderNPSResponses(responses) {
  const c = document.getElementById('nps-responses-list');
  if (!c) return;
  if (!responses.length) {
    c.innerHTML = '<div class="empty-state">Sin respuestas en este período.</div>';
    return;
  }

  const stars = n => '⭐'.repeat(n) + '☆'.repeat(5 - n);
  const typeLabel = score => score >= 4
    ? '<span style="background:#E1F5EE;color:#0F6E56;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600;">Promotor</span>'
    : '<span style="background:#FDE8E8;color:#C0392B;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600;">Detractor</span>';

  let html = `<table>
    <thead><tr>
      <th>Teléfono</th><th>Puntuación</th><th>Tipo</th><th>Comentario</th><th>Fecha</th>
    </tr></thead><tbody>`;

  responses.forEach(r => {
    const date = new Date((r.created_at||'') + 'Z').toLocaleDateString('es-CO',
      { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' });
    // Extract comment from either field name; treat '__pending__' as empty
    const rawComment = r.comment || r.feedback || '';
    const displayComment = (rawComment && rawComment !== '__pending__')
      ? `<span>${rawComment}</span>`
      : '<span style="color:#bbb;">—</span>';
    html += `<tr>
      <td style="font-size:12px;color:#888;">${r.phone}</td>
      <td>${stars(r.score)}</td>
      <td>${typeLabel(r.score)}</td>
      <td style="font-size:12px;color:#555;max-width:280px;">${displayComment}</td>
      <td style="font-size:11px;color:#aaa;">${date}</td>
    </tr>`;
  });

  html += '</tbody></table>';
  c.innerHTML = html;
}

function setNPSPeriod(p, btn) {
  npsPeriod = p;
  document.querySelectorAll('#nps .nps-period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadNPS();
}

async function loadGoogleMapsURL() {
  const h = window._dashHeaders;
  try {
    const r = await fetch('/api/nps/google-maps-url', { headers: h });
    if (r.ok) {
      const { url } = await r.json();
      const input = document.getElementById('maps-url-input');
      if (input) input.value = url || '';
    }
  } catch(e) {}
}

async function saveGoogleMapsURL() {
  const h   = window._dashHeaders;
  const url = document.getElementById('maps-url-input').value.trim();
  const btn = document.getElementById('maps-url-btn');
  btn.textContent = 'Guardando...';
  btn.disabled = true;
  try {
    const r = await fetch('/api/nps/google-maps-url', {
      method: 'POST',
      headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    if (r.ok) {
      btn.textContent = '✓ Guardado';
      setTimeout(() => { btn.textContent = 'Guardar'; btn.disabled = false; }, 2000);
    } else {
      btn.textContent = 'Error';
      btn.disabled = false;
    }
  } catch(e) {
    btn.textContent = 'Error';
    btn.disabled = false;
  }
}


// ══════════════════════════════════════════════════
// INVENTARIO
// ══════════════════════════════════════════════════

let inventoryItems   = [];
let menuDishesCache  = [];
let selectedDishes   = new Set();
let editingItemId    = null;
let historyItemId    = null;

async function loadInventory() {
  const h = window._dashHeaders;
  const container = document.getElementById('inventory-table-container');
  if (container) container.innerHTML = '<div class="empty-state">Cargando...</div>';

  try {
    const [rItems, rAlerts] = await Promise.all([
      fetch('/api/inventory', { headers: h }),
      fetch('/api/inventory/alerts', { headers: h })
    ]);

    inventoryItems = rItems.ok ? ((await rItems.json()).items || []) : [];
    const alerts   = rAlerts.ok ? ((await rAlerts.json()).alerts || []) : [];

    renderInventoryAlerts(alerts);
    renderInventoryTable(inventoryItems);
  } catch(e) {
    console.error('loadInventory:', e);
    if (container) container.innerHTML = '<div class="empty-state">Error de conexión.</div>';
  }
}

function renderInventoryAlerts(alerts) {
  const banner = document.getElementById('inventory-alerts-banner');
  const list   = document.getElementById('inventory-alerts-list');
  if (!banner || !list) return;

  if (!alerts.length) {
    banner.style.display = 'none';
    return;
  }

  banner.style.display = 'flex';
  list.innerHTML = alerts.map(a => {
    const isOut = parseFloat(a.current_stock) <= 0;
    return `<span style="
      display:inline-flex;align-items:center;gap:4px;
      background:${isOut ? '#FDE8E8' : '#FFF8E6'};
      color:${isOut ? '#C0392B' : '#7A4F00'};
      border:1px solid ${isOut ? '#FCA5A5' : '#FDE68A'};
      padding:3px 10px;border-radius:20px;font-size:12px;font-weight:500;">
      ${isOut ? '🔴' : '🟡'} ${a.name}: ${a.current_stock} ${a.unit}
    </span>`;
  }).join('');
}

function renderInventoryTable(items) {
  const c = document.getElementById('inventory-table-container');
  if (!c) return;

  if (!items.length) {
    c.innerHTML = '<div class="empty-state">No hay productos en el inventario. ¡Agrega el primero!</div>';
    return;
  }

  let html = `<table>
    <thead><tr>
      <th>Producto</th><th>Stock actual</th><th>Mínimo</th>
      <th>Platos vinculados</th><th>Estado</th><th>Acciones</th>
    </tr></thead><tbody>`;

  items.forEach(item => {
    const stock   = parseFloat(item.current_stock);
    const minSt   = parseFloat(item.min_stock);
    const dishes  = Array.isArray(item.linked_dishes) ? item.linked_dishes : JSON.parse(item.linked_dishes || '[]');
    const isOut   = stock <= 0;
    const isLow   = !isOut && stock <= minSt;

    const statusBadge = isOut
      ? '<span style="background:#FDE8E8;color:#C0392B;padding:3px 8px;border-radius:6px;font-size:11px;font-weight:600;">Agotado</span>'
      : isLow
        ? '<span style="background:#FFF8E6;color:#7A4F00;padding:3px 8px;border-radius:6px;font-size:11px;font-weight:600;">Stock bajo</span>'
        : '<span style="background:#E1F5EE;color:#0F6E56;padding:3px 8px;border-radius:6px;font-size:11px;font-weight:600;">OK</span>';

    const dishTags = dishes.map(d =>
      `<span style="background:#f0f0e8;color:#555;padding:2px 7px;border-radius:5px;font-size:10px;margin:1px;">${d}</span>`
    ).join('') || '<span style="color:#bbb;font-size:11px;">Sin vincular</span>';

    html += `<tr style="${isOut ? 'background:#fff8f8;' : isLow ? 'background:#fffdf0;' : ''}">
      <td style="font-weight:500;">${item.name}</td>
      <td>
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:15px;font-weight:600;color:${isOut?'#C0392B':isLow?'#BA7517':'#111'};">
            ${stock} ${item.unit}
          </span>
        </div>
      </td>
      <td style="color:#888;font-size:13px;">${minSt} ${item.unit}</td>
      <td style="max-width:200px;line-height:1.8;">${dishTags}</td>
      <td>${statusBadge}</td>
      <td>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <button onclick="openAdjustModal(${item.id},'${item.name.replace(/'/g,"\\'")}',${stock},'${item.unit}')"
            style="font-size:11px;padding:4px 9px;background:#E6F1FB;color:#185FA5;border:none;border-radius:6px;cursor:pointer;font-weight:500;">
            ± Ajustar
          </button>
          <button onclick="openEditModal(${item.id})"
            style="font-size:11px;padding:4px 9px;background:#f0f0e8;color:#555;border:none;border-radius:6px;cursor:pointer;">
            ✏️ Editar
          </button>
          <button onclick="openHistoryModal(${item.id},'${item.name.replace(/'/g,"\\'")}' )"
            style="font-size:11px;padding:4px 9px;background:#f0f0e8;color:#555;border:none;border-radius:6px;cursor:pointer;">
            📋 Historial
          </button>
          <button onclick="deleteInventoryItem(${item.id},'${item.name.replace(/'/g,"\\'")}' )"
            style="font-size:11px;padding:4px 9px;background:#FDE8E8;color:#C0392B;border:none;border-radius:6px;cursor:pointer;">
            🗑️
          </button>
        </div>
      </td>
    </tr>`;
  });

  html += '</tbody></table>';
  c.innerHTML = html;
}

// ── Cargar platos del menú para el selector ──
async function loadMenuDishes() {
  const h = window._dashHeaders;
  try {
    const r = await fetch('/api/inventory/menu-items', { headers: h });
    if (r.ok) menuDishesCache = ((await r.json()).dishes || []);
  } catch(e) {}
}

function renderDishSelector(containerId, preSelected = []) {
  const c = document.getElementById(containerId);
  if (!c) return;

  selectedDishes = new Set(preSelected);

  if (!menuDishesCache.length) {
    c.innerHTML = '<div style="color:#aaa;font-size:12px;">No hay platos en el menú.</div>';
    return;
  }

  // Agrupar por categoría
  const byCategory = {};
  menuDishesCache.forEach(d => {
    if (!byCategory[d.category]) byCategory[d.category] = [];
    byCategory[d.category].push(d.name);
  });

  let html = '';
  Object.entries(byCategory).forEach(([cat, dishes]) => {
    html += `<div style="margin-bottom:8px;">
      <div style="font-size:11px;color:#888;font-weight:600;text-transform:uppercase;margin-bottom:4px;">${cat}</div>
      <div style="display:flex;flex-wrap:wrap;gap:4px;">`;
    dishes.forEach(name => {
      const safe = name.replace(/'/g, "\\'");
      const isSelected = selectedDishes.has(name);
      html += `<button
        id="dish-tag-${containerId}-${safe}"
        onclick="toggleDishTag('${safe}', '${containerId}')"
        style="font-size:11px;padding:4px 10px;border-radius:20px;cursor:pointer;border:1px solid;
               ${isSelected
                 ? 'background:#E1F5EE;color:#0F6E56;border-color:#1D9E75;font-weight:600;'
                 : 'background:#fff;color:#555;border-color:#e0e0d8;'
               }">${name}</button>`;
    });
    html += '</div></div>';
  });

  c.innerHTML = html;
}

function toggleDishTag(name, containerId) {
  const id  = `dish-tag-${containerId}-${name}`;
  const btn = document.getElementById(id);
  if (!btn) return;

  if (selectedDishes.has(name)) {
    selectedDishes.delete(name);
    btn.style.background    = '#fff';
    btn.style.color         = '#555';
    btn.style.borderColor   = '#e0e0d8';
    btn.style.fontWeight    = '400';
  } else {
    selectedDishes.add(name);
    btn.style.background    = '#E1F5EE';
    btn.style.color         = '#0F6E56';
    btn.style.borderColor   = '#1D9E75';
    btn.style.fontWeight    = '600';
  }
}

// ── Modal: Crear producto ──
async function openCreateInventoryModal() {
  await loadMenuDishes();
  selectedDishes = new Set();
  document.getElementById('inv-create-name').value   = '';
  document.getElementById('inv-create-unit').value   = 'unidades';
  document.getElementById('inv-create-stock').value  = '';
  document.getElementById('inv-create-min').value    = '0';
  document.getElementById('inv-create-cost').value   = '0';
  renderDishSelector('inv-dish-selector-create');
  document.getElementById('modal-inv-create').style.display = 'flex';
}

function closeCreateInventoryModal() {
  document.getElementById('modal-inv-create').style.display = 'none';
}

async function submitCreateInventory() {
  const h    = window._dashHeaders;
  const name = document.getElementById('inv-create-name').value.trim();
  const unit = document.getElementById('inv-create-unit').value.trim() || 'unidades';
  const stock = parseFloat(document.getElementById('inv-create-stock').value);
  const min   = parseFloat(document.getElementById('inv-create-min').value || '0');
  const cost  = parseFloat(document.getElementById('inv-create-cost').value || '0');

  if (!name) return alert('El nombre es obligatorio');
  if (isNaN(stock)) return alert('El stock debe ser un número');

  const btn = document.getElementById('btn-inv-create-submit');
  btn.textContent = 'Guardando...'; btn.disabled = true;

  try {
    const r = await fetch('/api/inventory', {
      method: 'POST',
      headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, unit,
        current_stock: stock,
        min_stock: min,
        cost_per_unit: cost,
        linked_dishes: Array.from(selectedDishes)
      })
    });
    if (r.ok) {
      closeCreateInventoryModal();
      loadInventory();
    } else {
      const e = await r.json();
      alert('Error: ' + (e.detail || 'No se pudo crear'));
    }
  } catch(e) { alert('Error de conexión'); }
  btn.textContent = '+ Agregar producto'; btn.disabled = false;
}

// ── Modal: Editar producto ──
async function openEditModal(itemId) {
  await loadMenuDishes();
  editingItemId = itemId;
  const item = inventoryItems.find(i => i.id === itemId);
  if (!item) return;

  document.getElementById('inv-edit-name').value  = item.name;
  document.getElementById('inv-edit-unit').value  = item.unit;
  document.getElementById('inv-edit-stock').value = item.current_stock;
  document.getElementById('inv-edit-min').value   = item.min_stock;
  document.getElementById('inv-edit-cost').value  = item.cost_per_unit || 0;

  const dishes = Array.isArray(item.linked_dishes)
    ? item.linked_dishes
    : JSON.parse(item.linked_dishes || '[]');

  renderDishSelector('inv-dish-selector-edit', dishes);
  document.getElementById('modal-inv-edit').style.display = 'flex';
}

function closeEditModal() {
  document.getElementById('modal-inv-edit').style.display = 'none';
  editingItemId = null;
}

async function submitEditInventory() {
  if (!editingItemId) return;
  const h = window._dashHeaders;

  const body = {
    name:          document.getElementById('inv-edit-name').value.trim(),
    unit:          document.getElementById('inv-edit-unit').value.trim(),
    current_stock: parseFloat(document.getElementById('inv-edit-stock').value),
    min_stock:     parseFloat(document.getElementById('inv-edit-min').value || '0'),
    cost_per_unit: parseFloat(document.getElementById('inv-edit-cost').value || '0'),
    linked_dishes: Array.from(selectedDishes)
  };

  const btn = document.getElementById('btn-inv-edit-submit');
  btn.textContent = 'Guardando...'; btn.disabled = true;

  try {
    const r = await fetch(`/api/inventory/${editingItemId}`, {
      method: 'PUT',
      headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (r.ok) {
      closeEditModal();
      loadInventory();
    } else {
      const e = await r.json();
      alert('Error: ' + (e.detail || 'No se pudo actualizar'));
    }
  } catch(e) { alert('Error de conexión'); }
  btn.textContent = 'Guardar cambios'; btn.disabled = false;
}

// ── Modal: Ajustar stock ──
let adjustItemId = null;

function openAdjustModal(itemId, name, currentStock, unit) {
  adjustItemId = itemId;
  document.getElementById('adjust-item-name').textContent    = name;
  document.getElementById('adjust-current-stock').textContent = `${currentStock} ${unit}`;
  document.getElementById('adjust-quantity').value           = '';
  document.getElementById('adjust-reason').value             = 'compra';
  document.getElementById('adjust-feedback').style.display   = 'none';
  document.getElementById('modal-inv-adjust').style.display  = 'flex';
}

function closeAdjustModal() {
  document.getElementById('modal-inv-adjust').style.display = 'none';
  adjustItemId = null;
}

async function submitAdjustStock() {
  if (!adjustItemId) return;
  const h        = window._dashHeaders;
  const qty      = parseFloat(document.getElementById('adjust-quantity').value);
  const reason   = document.getElementById('adjust-reason').value;
  const feedback = document.getElementById('adjust-feedback');

  if (isNaN(qty)) { alert('Ingresa una cantidad válida'); return; }

  const btn = document.getElementById('btn-adjust-submit');
  btn.textContent = 'Ajustando...'; btn.disabled = true;

  try {
    const r = await fetch(`/api/inventory/${adjustItemId}/adjust`, {
      method: 'POST',
      headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ quantity: qty, reason })
    });
    if (r.ok) {
      const { item } = await r.json();
      feedback.style.display  = 'block';
      feedback.style.background = '#E1F5EE';
      feedback.style.color    = '#0F6E56';
      feedback.textContent    = `✓ Stock actualizado: ${item.current_stock} ${item.unit}`;
      setTimeout(() => { closeAdjustModal(); loadInventory(); }, 1200);
    } else {
      const e = await r.json();
      feedback.style.display   = 'block';
      feedback.style.background = '#FDE8E8';
      feedback.style.color     = '#C0392B';
      feedback.textContent     = 'Error: ' + (e.detail || 'No se pudo ajustar');
    }
  } catch(e) {
    feedback.style.display = 'block';
    feedback.textContent = 'Error de conexión';
  }
  btn.textContent = 'Aplicar'; btn.disabled = false;
}

// ── Modal: Historial de movimientos ──
async function openHistoryModal(itemId, name) {
  historyItemId = itemId;
  document.getElementById('history-item-name').textContent = name;
  document.getElementById('history-list').innerHTML        = '<div class="empty-state">Cargando...</div>';
  document.getElementById('modal-inv-history').style.display = 'flex';

  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/inventory/${itemId}/history`, { headers: h });
    if (!r.ok) throw new Error('Error');
    const { history } = await r.json();

    if (!history.length) {
      document.getElementById('history-list').innerHTML =
        '<div class="empty-state">Sin movimientos registrados.</div>';
      return;
    }

    const reasonLabel = {
      orden_confirmada: '🛒 Orden',
      compra:           '📦 Compra',
      merma:            '⚠️ Merma',
      ajuste_manual:    '✏️ Ajuste'
    };

    let html = `<table>
      <thead><tr><th>Fecha</th><th>Movimiento</th><th>Razón</th><th>Stock tras movimiento</th></tr></thead><tbody>`;
    history.forEach(h => {
      const date  = new Date((h.created_at||'') + 'Z').toLocaleString('es-CO',
        { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' });
      const delta = parseFloat(h.quantity_delta);
      const color = delta >= 0 ? '#1D9E75' : '#E24B4A';
      const sign  = delta >= 0 ? '+' : '';
      html += `<tr>
        <td style="font-size:11px;color:#888;">${date}</td>
        <td style="font-weight:600;color:${color};">${sign}${delta}</td>
        <td style="font-size:12px;">${reasonLabel[h.reason] || h.reason}</td>
        <td style="font-size:13px;">${h.stock_after}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('history-list').innerHTML = html;
  } catch(e) {
    document.getElementById('history-list').innerHTML =
      '<div class="empty-state">Error al cargar historial.</div>';
  }
}

function closeHistoryModal() {
  document.getElementById('modal-inv-history').style.display = 'none';
  historyItemId = null;
}

async function deleteInventoryItem(itemId, name) {
  if (!confirm(`¿Eliminar "${name}" del inventario?`)) return;
  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/inventory/${itemId}`, { method: 'DELETE', headers: h });
    if (r.ok) loadInventory();
    else { const e = await r.json(); alert('Error: ' + (e.detail || 'No se pudo eliminar')); }
  } catch(e) { alert('Error de conexión'); }
}

function filterInventory() {
  const q = document.getElementById('inv-search').value.toLowerCase();
  const filtered = inventoryItems.filter(i =>
    i.name.toLowerCase().includes(q)
  );
  renderInventoryTable(filtered);
}


// ══════════════════════════════════════════════════════════════════
// ESCANDALLOS / FOOD COST (FASE 4)
// ══════════════════════════════════════════════════════════════════

async function loadFoodCosts() {
  const h = window._dashHeaders;
  const c = document.getElementById('food-costs-container');
  if (!c) return;
  try {
    const r = await fetch('/api/inventory/food-costs', { headers: h });
    if (!r.ok) { c.innerHTML = '<div class="empty-state">Error al cargar escandallos.</div>'; return; }
    const { food_costs } = await r.json();
    renderFoodCostTable(food_costs || []);
  } catch(e) {
    c.innerHTML = '<div class="empty-state">Error de conexión.</div>';
  }
}

function renderFoodCostTable(costs) {
  const c = document.getElementById('food-costs-container');
  if (!c) return;
  if (!costs.length) {
    c.innerHTML = '<div class="empty-state">No hay escandallos definidos. Crea uno con "+ Nuevo Escandallo".</div>';
    return;
  }
  let html = `<table>
    <thead><tr>
      <th>Plato</th><th>Food Cost</th><th>Ingredientes</th><th>Acciones</th>
    </tr></thead><tbody>`;
  costs.forEach(row => {
    const breakdown = Array.isArray(row.breakdown) ? row.breakdown : [];
    const ingredientTags = breakdown.map(b =>
      `<span style="background:#f0f0e8;color:#555;padding:2px 7px;border-radius:5px;font-size:10px;margin:1px;" title="${b.quantity} ${b.unit} × $${b.cost_per_unit}">${b.ingredient}</span>`
    ).join('');
    const dishEsc = (row.dish_name || '').replace(/'/g, "\\'");
    html += `<tr>
      <td style="font-weight:500;">${row.dish_name}</td>
      <td style="font-weight:700;color:#7B5EA7;">$${parseFloat(row.food_cost).toLocaleString('es-CO')}</td>
      <td style="max-width:240px;line-height:1.8;">${ingredientTags}</td>
      <td>
        <div style="display:flex;gap:6px;">
          <button onclick="openRecipeModal('${dishEsc}')"
            style="font-size:11px;padding:4px 9px;background:#F0EBF8;color:#7B5EA7;border:none;border-radius:6px;cursor:pointer;font-weight:500;">
            ✏️ Editar
          </button>
          <button onclick="deleteRecipe('${dishEsc}')"
            style="font-size:11px;padding:4px 9px;background:#FDE8E8;color:#C0392B;border:none;border-radius:6px;cursor:pointer;">
            🗑️
          </button>
        </div>
      </td>
    </tr>`;
  });
  html += '</tbody></table>';
  c.innerHTML = html;
}

async function openRecipeModal(dishName) {
  // Ensure dishes and inventory are loaded
  if (!menuDishesCache.length) await loadMenuDishes();
  if (!inventoryItems.length) await loadInventory();

  // Populate dish selector
  const sel = document.getElementById('recipe-dish-select');
  sel.innerHTML = '<option value="">— Selecciona un plato —</option>';
  menuDishesCache.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.name;
    opt.textContent = d.name + (d.category ? ` (${d.category})` : '');
    sel.appendChild(opt);
  });

  // Clear lines
  document.getElementById('recipe-lines').innerHTML = '';
  document.getElementById('recipe-food-cost-preview').style.display = 'none';
  const fb = document.getElementById('recipe-feedback');
  fb.style.display = 'none';

  if (dishName) {
    sel.value = dishName;
    sel.disabled = true;  // can't change dish when editing existing
    // Load existing lines
    const h = window._dashHeaders;
    try {
      const r = await fetch(`/api/inventory/recipes/${encodeURIComponent(dishName)}`, { headers: h });
      if (r.ok) {
        const { lines } = await r.json();
        lines.forEach(l => addRecipeLine(l.ingredient_id, l.quantity));
      }
    } catch(e) {}
  } else {
    sel.disabled = false;
  }

  _updateRecipeLinesEmpty();
  document.getElementById('modal-recipe').style.display = 'flex';
}

function closeRecipeModal() {
  document.getElementById('modal-recipe').style.display = 'none';
  document.getElementById('recipe-dish-select').disabled = false;
}

function addRecipeLine(ingredientId, quantity) {
  const container = document.getElementById('recipe-lines');
  const lineId = 'rl-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);

  const options = inventoryItems.map(i =>
    `<option value="${i.id}" data-unit="${i.unit}" data-cost="${i.cost_per_unit || 0}" ${String(i.id) === String(ingredientId) ? 'selected' : ''}>${i.name} (${i.unit})</option>`
  ).join('');

  const div = document.createElement('div');
  div.id = lineId;
  div.style.cssText = 'display:flex;align-items:center;gap:8px;background:#fafaf7;border:1px solid #e0e0d8;border-radius:8px;padding:8px 10px;';
  div.innerHTML = `
    <select onchange="_recalcFoodCost()" style="flex:1;padding:6px 8px;border:1px solid #e0e0d8;border-radius:6px;font-size:12px;outline:none;">
      <option value="">— Ingrediente —</option>${options}
    </select>
    <input type="number" min="0.001" step="0.001" placeholder="Cantidad"
      value="${quantity != null ? quantity : ''}"
      onchange="_recalcFoodCost()"
      style="width:90px;padding:6px 8px;border:1px solid #e0e0d8;border-radius:6px;font-size:12px;outline:none;">
    <span class="rl-unit" style="font-size:11px;color:#888;min-width:32px;"></span>
    <button onclick="document.getElementById('${lineId}').remove();_updateRecipeLinesEmpty();_recalcFoodCost();"
      style="background:#FDE8E8;color:#C0392B;border:none;border-radius:6px;padding:4px 8px;cursor:pointer;font-size:13px;">✕</button>
  `;

  // Update unit label when ingredient changes
  const sel = div.querySelector('select');
  const unitSpan = div.querySelector('.rl-unit');
  const updateUnit = () => {
    const opt = sel.options[sel.selectedIndex];
    unitSpan.textContent = opt ? (opt.dataset.unit || '') : '';
  };
  sel.addEventListener('change', updateUnit);
  updateUnit();

  container.appendChild(div);
  _updateRecipeLinesEmpty();
  _recalcFoodCost();
}

function _updateRecipeLinesEmpty() {
  const lines = document.getElementById('recipe-lines');
  const empty = document.getElementById('recipe-lines-empty');
  if (!lines || !empty) return;
  empty.style.display = lines.children.length ? 'none' : 'block';
}

function _recalcFoodCost() {
  const lines = document.getElementById('recipe-lines');
  const preview = document.getElementById('recipe-food-cost-preview');
  if (!lines || !preview) return;
  let total = 0;
  let valid = true;
  Array.from(lines.children).forEach(div => {
    const sel = div.querySelector('select');
    const inp = div.querySelector('input[type=number]');
    if (!sel || !inp) return;
    const opt = sel.options[sel.selectedIndex];
    const cost = parseFloat(opt ? opt.dataset.cost || 0 : 0);
    const qty  = parseFloat(inp.value) || 0;
    if (!sel.value || qty <= 0) { valid = false; return; }
    total += cost * qty;
  });
  if (lines.children.length && valid && total > 0) {
    preview.style.display = 'block';
    preview.textContent = `Food Cost estimado: $${total.toLocaleString('es-CO', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  } else {
    preview.style.display = 'none';
  }
}

async function submitRecipe() {
  const h = window._dashHeaders;
  const dishName = document.getElementById('recipe-dish-select').value.trim();
  const fb = document.getElementById('recipe-feedback');
  const btn = document.getElementById('btn-recipe-submit');

  if (!dishName) {
    fb.style.display = 'block';
    fb.style.background = '#FDE8E8'; fb.style.color = '#C0392B';
    fb.textContent = 'Selecciona un plato del menú.';
    return;
  }

  const lines = [];
  const lineEls = document.getElementById('recipe-lines').children;
  for (const div of lineEls) {
    const sel = div.querySelector('select');
    const inp = div.querySelector('input[type=number]');
    const ingId = parseInt(sel ? sel.value : 0);
    const qty   = parseFloat(inp ? inp.value : 0);
    if (!ingId || qty <= 0) {
      fb.style.display = 'block';
      fb.style.background = '#FDE8E8'; fb.style.color = '#C0392B';
      fb.textContent = 'Completa todos los ingredientes con cantidad válida.';
      return;
    }
    lines.push({ ingredient_id: ingId, quantity: qty });
  }

  btn.textContent = 'Guardando...'; btn.disabled = true;
  fb.style.display = 'none';

  try {
    const r = await fetch('/api/inventory/recipes', {
      method: 'POST',
      headers: { ...h, 'Content-Type': 'application/json' },
      body: JSON.stringify({ dish_name: dishName, lines })
    });
    if (r.ok) {
      fb.style.display = 'block';
      fb.style.background = '#E1F5EE'; fb.style.color = '#0F6E56';
      fb.textContent = '✓ Escandallo guardado correctamente.';
      setTimeout(() => { closeRecipeModal(); loadFoodCosts(); }, 1000);
    } else {
      const e = await r.json();
      fb.style.display = 'block';
      fb.style.background = '#FDE8E8'; fb.style.color = '#C0392B';
      fb.textContent = 'Error: ' + (e.detail || 'No se pudo guardar');
    }
  } catch(e) {
    fb.style.display = 'block';
    fb.textContent = 'Error de conexión';
  }
  btn.textContent = 'Guardar Escandallo'; btn.disabled = false;
}

async function deleteRecipe(dishName) {
  if (!confirm(`¿Eliminar el escandallo de "${dishName}"?`)) return;
  const h = window._dashHeaders;
  try {
    const r = await fetch(`/api/inventory/recipes/${encodeURIComponent(dishName)}`, { method: 'DELETE', headers: h });
    if (r.ok) loadFoodCosts();
    else { const e = await r.json(); alert('Error: ' + (e.detail || 'No se pudo eliminar')); }
  } catch(e) { alert('Error de conexión'); }
}

// ── Init ──
(function() {
    // Esperar a que showSection esté definido (viene de dashboard-core.js que carga antes)
    const origShowSection = window.showSection;
    window.showSection = function(id, btn) {
      if (typeof origShowSection === 'function') origShowSection(id, btn);
      if (id === 'nps')        loadNPS();
      if (id === 'inventario') { loadInventory(); loadFoodCosts(); }
    };

    // Si la sección activa al cargar ya es nps o inventario, cargar datos
    document.addEventListener('DOMContentLoaded', () => {
      const active = document.querySelector('.section.active');
      if (active) {
        if (active.id === 'nps')        loadNPS();
        if (active.id === 'inventario') { loadInventory(); loadFoodCosts(); }
      }
    });
  })();