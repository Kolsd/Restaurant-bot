/**
 * Mesio Demo Mode — fetch interceptor + mock data
 * app/static/js/demo-data.js
 *
 * Load this BEFORE any dashboard scripts.
 * Sets window.MESIO_DEMO_MODE = true and monkey-patches window.fetch
 * so every /api/ call returns realistic mock data without hitting the server.
 */

window.MESIO_DEMO_MODE = true;

// ── Seed localStorage so dashboard-core.js security guard passes ──────────────
localStorage.setItem('rb_token',      'demo-token');
localStorage.setItem('rb_role',       'owner');
localStorage.setItem('rb_restaurant', JSON.stringify({
  id:             1,
  name:           'Restaurante Demo',
  currency:       'COP',
  locale:         'es-CO',
  whatsapp_number:'573001234567',
  role:           'owner',
  features: {
    module_orders:       true,
    module_reservations: true,
    module_tables:       true,
    module_pos:          true,
    module_inventory:    true,
    module_nps:          true,
    staff_tips:          true,
    loyalty:             true,
  },
}));

// ── Helpers ────────────────────────────────────────────────────────────────────
const _now  = () => new Date();
const _iso  = (d) => d.toISOString();
const _ago  = (h) => { const d = _now(); d.setHours(d.getHours() - h); return _iso(d); };
const _date = (offsetDays = 0) => {
  const d = _now();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
};
const _weekStart = () => {
  const d = _now();
  const day = (d.getDay() + 6) % 7; // Mon=0
  d.setDate(d.getDate() - day);
  d.setHours(0, 0, 0, 0);
  return d;
};

// ── MOCK DATA ──────────────────────────────────────────────────────────────────

const _staff = [
  { id:1,  name:'Carlos Ramírez',         role:'mesero',       roles:['mesero'],       document_number:'1020304050', hourly_rate:8500,  active:true,  in_shift:true  },
  { id:2,  name:'María Gómez',            role:'mesero',       roles:['mesero'],       document_number:'1020304051', hourly_rate:8500,  active:true,  in_shift:true  },
  { id:3,  name:'Juan Sebastián López',   role:'caja',         roles:['caja'],         document_number:'1020304052', hourly_rate:9000,  active:true,  in_shift:false },
  { id:4,  name:'Andrea Castro',          role:'cocina',       roles:['cocina'],       document_number:'1020304053', hourly_rate:8000,  active:true,  in_shift:true  },
  { id:5,  name:'Diego Morales',          role:'bar',          roles:['bar'],          document_number:'1020304054', hourly_rate:8200,  active:true,  in_shift:false },
  { id:6,  name:'Valentina Ruiz',         role:'gerente',      roles:['gerente'],      document_number:'1020304055', hourly_rate:14000, active:true,  in_shift:true  },
  { id:7,  name:'Santiago Herrera',       role:'domiciliario', roles:['domiciliario'], document_number:'1020304056', hourly_rate:7800,  active:true,  in_shift:false },
  { id:8,  name:'Camila Torres',          role:'admin',        roles:['admin'],        document_number:'1020304057', hourly_rate:15000, active:true,  in_shift:false },
];

// Schedules: Mon-Fri for each staff member, stored as day_of_week 0=Mon..6=Sun
const _schedules = [];
let _schedId = 1;
_staff.slice(0, 7).forEach(emp => {
  for (let dow = 0; dow < 5; dow++) {
    _schedules.push({
      id: _schedId++,
      staff_id: emp.id,
      staff_name: emp.name,
      day_of_week: dow,
      start_time: emp.role === 'cocina' ? '07:00' : '08:00',
      end_time:   emp.role === 'cocina' ? '16:00' : '17:00',
    });
  }
});

const _ws  = _weekStart();
const _mkDT = (dow, hour, min = 0) => {
  const d = new Date(_ws);
  d.setDate(d.getDate() + dow);
  d.setHours(hour, min, 0, 0);
  return _iso(d);
};

const _shifts = [
  { id:101, staff_id:1, staff_name:'Carlos Ramírez',       clock_in: _mkDT(0, 8, 3),  clock_out: _mkDT(0, 17, 10) },
  { id:102, staff_id:2, staff_name:'María Gómez',          clock_in: _mkDT(0, 8, 0),  clock_out: _mkDT(0, 17, 0)  },
  { id:103, staff_id:4, staff_name:'Andrea Castro',        clock_in: _mkDT(0, 7, 5),  clock_out: _mkDT(0, 16, 2)  },
  { id:104, staff_id:6, staff_name:'Valentina Ruiz',       clock_in: _mkDT(0, 8, 0),  clock_out: null             },
  { id:105, staff_id:1, staff_name:'Carlos Ramírez',       clock_in: _mkDT(1, 8, 10), clock_out: _mkDT(1, 17, 5)  },
  { id:106, staff_id:3, staff_name:'Juan Sebastián López', clock_in: _mkDT(1, 8, 0),  clock_out: _mkDT(1, 17, 0)  },
];

const _openShifts = _shifts.filter(s => !s.clock_out);

const _payroll = _staff.map(emp => {
  const hours     = emp.role === 'admin' || emp.role === 'gerente' ? 192 : 176;
  const baseSal   = emp.hourly_rate * hours;
  const tips      = Math.round((40000 + Math.random() * 160000) / 1000) * 1000;
  const autoDed   = emp.role === 'mesero' ? Math.round((15000 + Math.random() * 30000) / 1000) * 1000 : 0;
  const manualDed = 0;
  return {
    staff_id:          emp.id,
    name:              emp.name,
    document:          emp.document_number,
    hours:             hours,
    base_salary:       baseSal,
    tips:              tips,
    auto_deductions:   autoDed,
    manual_deductions: manualDed,
    net_pay:           baseSal + tips - autoDed - manualDed,
  };
});

const _tipsAuto = {
  period_start: _date(-30),
  period_end:   _date(0),
  total_tips:   _staff.reduce((s, _, i) => s + (i < 6 ? 40000 + i * 25000 : 0), 0),
  unallocated:  12000,
  distribution: _staff.slice(0, 6).map((emp, i) => ({
    staff_id:   emp.id,
    name:       emp.name,
    role:       emp.role,
    tip_amount: 40000 + i * 25000,
    shifts_count: 5 + i,
  })),
};

const _contracts = [
  {
    id: 1, name: 'Mesero TC',
    weekly_hours: 46, monthly_salary: 1160000, pay_period: 'monthly',
    transport_subsidy: 140606, arl_pct: 0.522, health_pct: 4.0, pension_pct: 4.0,
    breaks_billable: false, lunch_billable: false, lunch_minutes: 60,
  },
  {
    id: 2, name: 'Cocinero',
    weekly_hours: 46, monthly_salary: 1300000, pay_period: 'monthly',
    transport_subsidy: 140606, arl_pct: 1.044, health_pct: 4.0, pension_pct: 4.0,
    breaks_billable: true, lunch_billable: true, lunch_minutes: 30,
  },
  {
    id: 3, name: 'Cajero MT',
    weekly_hours: 24, monthly_salary: 700000, pay_period: 'biweekly',
    transport_subsidy: 0, arl_pct: 0.522, health_pct: 4.0, pension_pct: 4.0,
    breaks_billable: false, lunch_billable: false, lunch_minutes: 45,
  },
];

const _overtimeRequests = [
  {
    id: 1, staff_id: 1, staff_name: 'Carlos Ramírez',
    week_start: _date(-7), extra_hours: 4.5, status: 'pending',
    notes: 'Cubrí turno del compañero el sábado.',
  },
  {
    id: 2, staff_id: 4, staff_name: 'Andrea Castro',
    week_start: _date(-7), extra_hours: 3.0, status: 'pending',
    notes: 'Evento privado, quede hasta las 10pm.',
  },
];

const _tables = [
  { id:1,  name:'Mesa 1',  status:'libre'   },
  { id:2,  name:'Mesa 2',  status:'ocupada' },
  { id:3,  name:'Mesa 3',  status:'libre'   },
  { id:4,  name:'Mesa 4',  status:'ocupada' },
  { id:5,  name:'Mesa 5',  status:'cuenta'  },
  { id:6,  name:'Mesa 6',  status:'libre'   },
  { id:7,  name:'Mesa 7',  status:'libre'   },
  { id:8,  name:'Mesa 8',  status:'ocupada' },
  { id:9,  name:'Mesa 9',  status:'libre'   },
  { id:10, name:'Mesa 10', status:'libre'   },
  { id:11, name:'Mesa 11', status:'libre'   },
  { id:12, name:'Mesa 12', status:'libre'   },
];

const _tableOrders = {
  2: { id: 'to-2', table_id: 2, status: 'en_proceso', items: [
    { name:'Bandeja Paisa', quantity:2, price:32000, total:64000 },
    { name:'Limonada Natural', quantity:2, price:9000,  total:18000 },
  ], subtotal:82000, tip_amount:8200, total:90200, created_at: _ago(1) },
  4: { id: 'to-4', table_id: 4, status: 'en_proceso', items: [
    { name:'Ajiaco Bogotano', quantity:3, price:28000, total:84000 },
    { name:'Cerveza Club Colombia', quantity:3, price:8500, total:25500 },
    { name:'Arroz con Leche', quantity:2, price:9500, total:19000 },
  ], subtotal:128500, tip_amount:13000, total:141500, created_at: _ago(2) },
  8: { id: 'to-8', table_id: 8, status: 'pagando', items: [
    { name:'Cazuela de Mariscos', quantity:1, price:45000, total:45000 },
    { name:'Agua Mineral', quantity:2, price:5000, total:10000 },
  ], subtotal:55000, tip_amount:5500, total:60500, created_at: _ago(0.5) },
};

const _tableChecks = {
  'to-2': [
    { id:'chk-1', table_order_id:'to-2', status:'open',  subtotal:41000, tip_amount:4100, paid_at:null },
    { id:'chk-2', table_order_id:'to-2', status:'open',  subtotal:41000, tip_amount:4100, paid_at:null },
  ],
  'to-4': [
    { id:'chk-3', table_order_id:'to-4', status:'paid',  subtotal:128500, tip_amount:13000, paid_at: _ago(0.25) },
  ],
};

const _inventory = [
  { id:1,  name:'Pollo entero',         unit:'kg',       stock:15.5,  min_stock:5,    cost_per_unit:9800  },
  { id:2,  name:'Papa pastusa',         unit:'kg',       stock:40,    min_stock:10,   cost_per_unit:2200  },
  { id:3,  name:'Tomate chonto',        unit:'kg',       stock:8,     min_stock:5,    cost_per_unit:3500  },
  { id:4,  name:'Cebolla cabezona',     unit:'kg',       stock:6,     min_stock:4,    cost_per_unit:2800  },
  { id:5,  name:'Arroz',               unit:'kg',       stock:25,    min_stock:10,   cost_per_unit:2100  },
  { id:6,  name:'Frijoles',            unit:'kg',       stock:3,     min_stock:5,    cost_per_unit:5200  },  // alert
  { id:7,  name:'Maíz tierno',         unit:'kg',       stock:2,     min_stock:3,    cost_per_unit:3100  },  // alert
  { id:8,  name:'Leche entera',        unit:'litros',   stock:12,    min_stock:5,    cost_per_unit:3200  },
  { id:9,  name:'Huevos',             unit:'unidades', stock:60,    min_stock:24,   cost_per_unit:450   },
  { id:10, name:'Aceite vegetal',      unit:'litros',   stock:4,     min_stock:2,    cost_per_unit:7500  },
  { id:11, name:'Plátano maduro',      unit:'unidades', stock:30,    min_stock:10,   cost_per_unit:900   },
  { id:12, name:'Cilantro',           unit:'kg',       stock:1.5,   min_stock:0.5,  cost_per_unit:6000  },
  { id:13, name:'Limones',            unit:'kg',       stock:5,     min_stock:2,    cost_per_unit:2500  },
  { id:14, name:'Crema de leche',     unit:'litros',   stock:3,     min_stock:1,    cost_per_unit:5800  },
  { id:15, name:'Chorizo',            unit:'kg',       stock:4.5,   min_stock:2,    cost_per_unit:16000 },
];

const _inventoryAlerts = _inventory.filter(i => i.stock <= i.min_stock).map(i => ({
  ...i, alert: true, shortage: i.min_stock - i.stock,
}));

const _recipes = [
  { dish_name:'Bandeja Paisa',     cost:18500, ingredients:[ {name:'Frijoles',qty:0.2}, {name:'Arroz',qty:0.2}, {name:'Chorizo',qty:0.15}, {name:'Huevos',qty:1}, {name:'Plátano maduro',qty:0.5} ] },
  { dish_name:'Ajiaco Bogotano',   cost:12000, ingredients:[ {name:'Pollo entero',qty:0.3}, {name:'Papa pastusa',qty:0.4}, {name:'Maíz tierno',qty:0.1}, {name:'Crema de leche',qty:0.05} ] },
  { dish_name:'Limonada Natural',  cost:1500,  ingredients:[ {name:'Limones',qty:0.15}, {name:'Leche entera',qty:0.1} ] },
  { dish_name:'Cazuela de Mariscos', cost:24000, ingredients:[ {name:'Crema de leche',qty:0.1}, {name:'Cebolla cabezona',qty:0.1}, {name:'Tomate chonto',qty:0.1}, {name:'Aceite vegetal',qty:0.02} ] },
  { dish_name:'Arroz con Leche',   cost:2200,  ingredients:[ {name:'Arroz',qty:0.1}, {name:'Leche entera',qty:0.2} ] },
];

// Revenue data points for chart — last 14 days
const _revenueData = Array.from({length:14}, (_, i) => {
  const d = new Date(_now());
  d.setDate(d.getDate() - (13 - i));
  const count = Math.round(8 + Math.random() * 20);
  const avg   = 28000 + Math.random() * 20000;
  return { date: d.toISOString().slice(0,10), orders: count, revenue: Math.round(count * avg / 1000) * 1000 };
});

// Build flat orders array for dashboard resumen
const _orders = [];
let _orderId = 1;
_revenueData.forEach(dp => {
  for (let i = 0; i < dp.orders; i++) {
    const h  = Math.floor(Math.random() * 12) + 8;
    const mn = Math.floor(Math.random() * 60);
    const dt = new Date(dp.date + 'T' + String(h).padStart(2,'0') + ':' + String(mn).padStart(2,'0') + ':00Z');
    const type = ['domicilio','recoger','mesa'][Math.floor(Math.random()*3)];
    const items = [
      { name:'Bandeja Paisa',  quantity:1, price:32000 },
      { name:'Ajiaco',         quantity:1, price:28000 },
      { name:'Cazuela',        quantity:1, price:45000 },
      { name:'Limonada',       quantity:2, price: 9000 },
    ].slice(0, 1 + Math.floor(Math.random()*3));
    const total = items.reduce((s,it) => s + it.price * it.quantity, 0);
    _orders.push({
      id:         String(_orderId++).padStart(8,'0') + '-demo',
      type,
      status:     Math.random() > 0.15 ? 'entregado' : 'pendiente',
      paid:       Math.random() > 0.15,
      items,
      total,
      created_at: dt.toISOString(),
    });
  }
});

const _reservations = [
  { id:1, name:'Familia Martínez', date: _date(0), time:'12:30', guests:4, status:'confirmada' },
  { id:2, name:'Santiago Peña',    date: _date(0), time:'19:00', guests:2, status:'pendiente'  },
  { id:3, name:'Empresa Acme',     date: _date(1), time:'13:00', guests:10, status:'confirmada'},
  { id:4, name:'Laura Vargas',     date: _date(2), time:'20:00', guests:3, status:'confirmada' },
];

const _conversations = Array.from({length:18}, (_, i) => ({
  phone:      '57300' + String(1000000 + i * 111111),
  name:       ['Ana','Pedro','Laura','Luis','Sofía','Camilo'][i % 6] + ' (Demo)',
  last_message: 'Hola, quisiera hacer un pedido por favor.',
  updated_at: _ago(i * 0.5),
  unread:     i < 3,
}));

const _menuData = {
  menu: {
    'Platos Fuertes': [
      { name:'Bandeja Paisa',    price:32000, description:'Frijoles, arroz, chicharrón, carne molida, huevo, plátano, chorizo', available:true },
      { name:'Ajiaco Bogotano',  price:28000, description:'Papa criolla, pollo, mazorca, crema de leche',                       available:true },
      { name:'Cazuela de Mariscos', price:45000, description:'Mezcla de mariscos en salsa criolla',                            available:true },
    ],
    'Sopas': [
      { name:'Sopa de Mondongo', price:22000, description:'Mondongo con papa, zanahoria, cilantro',  available:true },
      { name:'Caldo de Costilla', price:18000, description:'Costilla de res, papa, cilantro',        available:true },
    ],
    'Bebidas': [
      { name:'Limonada Natural',  price:9000,  description:'Limones frescos, agua, azúcar',         available:true },
      { name:'Cerveza Club',      price:8500,  description:'Club Colombia 330ml',                    available:true },
      { name:'Agua Mineral',      price:5000,  description:'Agua sin gas 500ml',                    available:true },
    ],
    'Postres': [
      { name:'Arroz con Leche',  price:9500,  description:'Arroz, leche, canela, azúcar',           available:true },
      { name:'Tres Leches',      price:12000, description:'Bizcocho bañado en tres leches',          available:true },
    ],
  },
};

const _branches = {
  branches: [
    { id:2, name:'Centro', parent_restaurant_id:1, address:'Carrera 7 #32-12, Bogotá' },
    { id:3, name:'Norte',  parent_restaurant_id:1, address:'Calle 100 #15-30, Bogotá' },
  ],
};

const _settings = {
  id: 1, name: 'Restaurante Demo', currency: 'COP', locale: 'es-CO',
  features: {
    module_orders: true, module_reservations: true, module_tables: true,
    module_pos: true, module_inventory: true, module_nps: true,
    staff_tips: true, loyalty: true,
  },
};

const _statsToday = {
  revenue_today:  _revenueData.slice(-1)[0].revenue,
  orders_today:   _revenueData.slice(-1)[0].orders,
  avg_ticket:     28500,
  top_dishes:     [
    { name:'Bandeja Paisa',    count:34 },
    { name:'Ajiaco Bogotano',  count:29 },
    { name:'Cazuela de Mariscos', count:18 },
    { name:'Limonada Natural', count:52 },
    { name:'Arroz con Leche',  count:22 },
  ],
  top_waiters: [
    { name:'Carlos Ramírez', orders:21 },
    { name:'María Gómez',    orders:18 },
    { name:'Diego Morales',  orders:15 },
  ],
};

const _loyaltyStats = {
  total_members:  142,
  active_members: 87,
  points_issued:  48500,
  points_redeemed:12300,
  top_customers: [
    { phone:'573001111111', name:'Ana Rodríguez', points:1250 },
    { phone:'573002222222', name:'Pedro Martínez', points:980 },
    { phone:'573003333333', name:'Laura Sánchez', points:760 },
  ],
};

const _npsStats = {
  avg_score:     8.4,
  total:         63,
  promoters:     41,
  passives:      14,
  detractors:    8,
  responses: Array.from({length:10}, (_, i) => ({
    id: i+1, score: 6 + Math.floor(Math.random()*5),
    comment: ['Excelente servicio!', 'Muy buen ambiente', 'La comida estuvo deliciosa',
              'El mesero fue muy amable', 'Volveré pronto'][i%5],
    created_at: _ago(i * 8),
  })),
};

const _payrollRuns = [];

const _tableSessions = [
  { id:1,  table_id:2,  table_name:'Mesa 2',  bot_number:'573001234567', phone:'573011111111', started_at: _ago(3.5), closed_at: _ago(2.0), closed_by:'client_goodbye',    closed_by_username:'Cliente',          total_spent:90200  },
  { id:2,  table_id:4,  table_name:'Mesa 4',  bot_number:'573001234567', phone:'573022222222', started_at: _ago(5.0), closed_at: _ago(3.5), closed_by:'waiter_manual',     closed_by_username:'Carlos Ramírez',   total_spent:141500 },
  { id:3,  table_id:7,  table_name:'Mesa 7',  bot_number:'573001234567', phone:'573033333333', started_at: _ago(6.0), closed_at: _ago(4.8), closed_by:'inactivity_timeout', closed_by_username:'Sistema',         total_spent:null   },
  { id:4,  table_id:1,  table_name:'Mesa 1',  bot_number:'573001234567', phone:'573044444444', started_at: _ago(8.0), closed_at: _ago(7.0), closed_by:'client_goodbye',    closed_by_username:'Cliente',          total_spent:55000  },
  { id:5,  table_id:5,  table_name:'Mesa 5',  bot_number:'573001234567', phone:'573055555555', started_at: _ago(9.5), closed_at: _ago(8.2), closed_by:'waiter_manual',     closed_by_username:'María Gómez',     total_spent:60500  },
  { id:6,  table_id:3,  table_name:'Mesa 3',  bot_number:'573001234567', phone:'573066666666', started_at: _ago(12.0),closed_at: _ago(10.5),closed_by:'client_goodbye',    closed_by_username:'Cliente',          total_spent:112000 },
];

// ── Route matcher ──────────────────────────────────────────────────────────────

function _demoMatch(path, method) {
  // Normalize: strip trailing slash
  const p = path.replace(/\/$/, '');

  // ── Settings ──
  if (p === '/settings' && method === 'GET')
    return _settings;

  // ── Dashboard overview ──
  if (p.startsWith('/dashboard/orders') && method === 'GET')
    return { orders: _orders };
  if (p.startsWith('/dashboard/reservations') && method === 'GET')
    return { reservations: _reservations };
  if (p === '/dashboard/conversations' && method === 'GET')
    return { conversations: _conversations };
  if (p.startsWith('/dashboard/menu') && method === 'GET')
    return _menuData;
  if (p.startsWith('/dashboard/stats') && method === 'GET')
    return _statsToday;

  // ── Staff endpoints ──
  if (p === '/staff' && method === 'GET')
    return _staff;
  if (p === '/staff' && (method === 'POST'))
    return { id: 99, name:'Nuevo Empleado', role:'mesero', roles:['mesero'], active:true };
  if (/^\/staff\/\d+$/.test(p) && (method === 'PATCH' || method === 'PUT' || method === 'DELETE'))
    return { ok: true };

  if (p === '/staff/schedules' && method === 'GET')
    return _schedules;
  if (p === '/staff/schedules' && method === 'POST')
    return { id: 999, ...{} };
  if (p === '/staff/schedules/bulk' && method === 'POST')
    return { created: 5 };
  if (/^\/staff\/schedules\/\d+$/.test(p) && method === 'DELETE')
    return { ok: true };

  if (p === '/staff/shifts' && method === 'GET')
    return _shifts;
  if (p === '/staff/open-shifts' && method === 'GET')
    return _openShifts;

  if (p === '/staff/clock-in' && method === 'POST')
    return { id: 200, clock_in: _iso(_now()), clock_out: null };
  if (p === '/staff/clock-out' && method === 'POST')
    return { id: 200, clock_out: _iso(_now()) };

  if (p === '/staff/tips/auto' && method === 'GET')
    return _tipsAuto;
  if (p === '/staff/tip-distribution' && method === 'PATCH')
    return { ok: true };
  if (p === '/staff/tip-distributions' && method === 'GET')
    return [];

  if (/^\/staff\/\d+\/deductions$/.test(p) && method === 'GET')
    return [];
  if (/^\/staff\/\d+\/deductions$/.test(p) && method === 'POST')
    return { id: 88, amount: 50000 };
  if (/^\/staff\/deductions\/\d+$/.test(p))
    return { ok: true };

  if (p === '/staff/payroll/calculate' && method === 'GET')
    return _payroll;
  if (p === '/staff/payroll/runs' && method === 'GET')
    return _payrollRuns;
  if (p === '/staff/payroll/runs' && method === 'POST')
    return { id: 1, status: 'draft' };
  if (/^\/staff\/payroll\/runs\/\d+\/approve$/.test(p) && method === 'PUT')
    return { id: 1, status: 'approved' };
  if (p === '/staff/payroll/overtime' && method === 'GET')
    return _overtimeRequests;
  if (/^\/staff\/payroll\/overtime\/\d+$/.test(p) && method === 'PATCH')
    return { ok: true };
  if (p === '/staff/payroll/contracts' && method === 'GET')
    return _contracts;
  if (p === '/staff/payroll/contracts' && method === 'POST')
    return { id: 99, name: 'Nueva Plantilla' };
  if (/^\/staff\/payroll\/contracts\/\d+$/.test(p) && (method === 'PATCH' || method === 'DELETE'))
    return { ok: true };
  if (/^\/staff\/\d+\/contract$/.test(p) && method === 'PATCH')
    return { ok: true };

  // WebAuthn — just return empty/ok
  if (p.includes('/staff/webauthn'))
    return { challenge: 'demo', ok: true };

  // ── Menu ──
  if (p === '/menu/availability' && method === 'GET')
    return { availability: {} };
  if (p === '/menu/availability' && method === 'POST')
    return { ok: true };
  if (p === '/menu/sync-branches' && method === 'POST')
    return { ok: true };
  if (p === '/menu/update' && (method === 'POST' || method === 'PUT'))
    return { ok: true };

  // ── Tables ──
  if (p === '/tables' && method === 'GET')
    return { tables: _tables };
  if (p === '/tables' && method === 'POST')
    return { id: 13, name:'Mesa 13' };
  if (/^\/tables\/\d+$/.test(p) && method === 'DELETE')
    return { ok: true };

  // ── Table orders ──
  if (p === '/table-orders' && method === 'GET')
    return { orders: Object.values(_tableOrders) };
  const toMatch = p.match(/^\/table-orders\/([^/]+)$/);
  if (toMatch && method === 'GET')
    return _tableOrders[toMatch[1]] || _tableOrders[2];
  const checksMatch = p.match(/^\/table-orders\/([^/]+)\/checks$/);
  if (checksMatch && method === 'GET')
    return _tableChecks[checksMatch[1]] || [];
  if (/\/table-orders\/.*\/status/.test(p) && method === 'PUT')
    return { ok: true };
  if (/\/table-orders\/.*\/adjust/.test(p))
    return { ok: true };

  // ── Orders ──
  if (/\/orders\/.*\/status/.test(p) && method === 'PUT')
    return { ok: true };

  // ── Inventory ──
  if (p === '/inventory' && method === 'GET')
    return _inventory;
  if (p === '/inventory' && method === 'POST')
    return { id: 99, name:'Nuevo Producto' };
  if (p === '/inventory/alerts' && method === 'GET')
    return _inventoryAlerts;
  if (p === '/inventory/menu-items' && method === 'GET')
    return _menuData.menu;
  if (p === '/inventory/food-costs' && method === 'GET')
    return _recipes;
  if (/^\/inventory\/\d+$/.test(p) && (method === 'PUT' || method === 'PATCH' || method === 'DELETE'))
    return { ok: true };
  if (/^\/inventory\/\d+\/adjust$/.test(p) && method === 'POST')
    return { ok: true };
  if (/^\/inventory\/\d+\/history$/.test(p) && method === 'GET')
    return [];

  // ── Recipes ──
  if (p === '/inventory/recipes' && method === 'GET')
    return _recipes;
  if (p === '/inventory/recipes' && method === 'POST')
    return { ok: true };
  if (/^\/inventory\/recipes\//.test(p) && method === 'GET')
    return _recipes[0];
  if (/^\/inventory\/recipes\//.test(p) && method === 'DELETE')
    return { ok: true };

  // ── Team / Branches ──
  if (p === '/team/branches' && method === 'GET')
    return _branches;
  if (p === '/team/branches' && method === 'POST')
    return { id: 99, name:'Nueva Sucursal' };
  if (/^\/team\/branches\/\d+$/.test(p) && method === 'DELETE')
    return { ok: true };
  if (p === '/team/invite' && method === 'POST')
    return { ok: true };
  if (p.startsWith('/team/users') && method === 'GET')
    return { users: [] };
  if (/\/team\/users\//.test(p) && method === 'DELETE')
    return { ok: true };

  // ── Table sessions ──
  if (p.startsWith('/table-sessions/closed') && method === 'GET')
    return { sessions: _tableSessions };
  if (p.startsWith('/table-sessions') && method === 'GET')
    return { sessions: [] };
  if (/\/table-sessions\/.*\/history/.test(p) && method === 'GET')
    return { history: [] };
  if (/\/table-sessions\/.*\/reopen/.test(p) && method === 'POST')
    return { ok: true };
  if (/\/table-sessions\/.*\/send-message/.test(p) && method === 'POST')
    return { ok: true };
  if (/\/table-sessions\/.*\/alert-waiter/.test(p) && method === 'POST')
    return { ok: true };

  // ── Waiter alerts ──
  if (p.includes('/waiter-alerts'))
    return { alerts: [] };

  // ── Conversations ──
  if (p.startsWith('/conversations') && method === 'GET')
    return { messages: [], phone: '' };
  if (p.includes('/conversations') && (method === 'POST' || method === 'DELETE' || method === 'PATCH'))
    return { ok: true };

  // ── NPS ──
  if (p.startsWith('/nps/stats') && method === 'GET')
    return _npsStats;
  if (p.startsWith('/nps/responses') && method === 'GET')
    return { responses: _npsStats.responses };
  if (p.startsWith('/nps/google-maps-url'))
    return { url: 'https://g.page/demo' };

  // ── Loyalty ──
  if (p.startsWith('/loyalty/stats') && method === 'GET')
    return _loyaltyStats;
  if (p.startsWith('/loyalty/balance') && method === 'GET')
    return { balance: 350, name: 'Cliente Demo' };

  // ── Offline sync ──
  if (p === '/sync' && method === 'POST')
    return { ok: true, synced: 0 };

  // ── CRM / billing / misc ──
  if (p.startsWith('/crm'))
    return [];
  if (p.startsWith('/billing') || p.startsWith('/fiscal'))
    return [];
  if (p.startsWith('/reservations') && method === 'GET')
    return { reservations: _reservations };

  return null; // Unmocked — caller will console.warn
}

// ── Fetch Monkey-Patch ─────────────────────────────────────────────────────────
const _origFetch = window.fetch.bind(window);

window.fetch = async function(url, opts = {}) {
  const rawUrl = typeof url === 'string' ? url : (url && url.url) || String(url);

  if (rawUrl.includes('/api/')) {
    // Simulate realistic network latency
    await new Promise(r => setTimeout(r, 120 + Math.random() * 80));

    const path   = rawUrl.replace(/^.*\/api/, '').split('?')[0];
    const method = ((opts && opts.method) || 'GET').toUpperCase();

    const data = _demoMatch(path, method);

    if (data === null) {
      console.warn('[MESIO DEMO] No mock for', method, path, '— returning []');
      return new Response('[]', { status:200, headers:{'Content-Type':'application/json'} });
    }

    return new Response(JSON.stringify(data), {
      status:  200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // Non-API calls (static assets, CDNs, etc.) pass through unchanged
  return _origFetch(url, opts);
};

console.info('[MESIO DEMO] Modo demo activo — todas las llamadas /api/ están interceptadas.');
