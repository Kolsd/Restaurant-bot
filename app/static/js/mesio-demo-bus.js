/* ════════════════════════════════════════════════════
   mesio-demo-bus.js · Mock state + EventBus
   - Mantiene contrato: window.MesioDemo.state, fetch monkey-patch (no-op aquí)
   - Añade: pub/sub vía window.MesioBus
   ════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ─── EventBus ─────────────────────────────────────
  var listeners = {};
  var Bus = {
    on: function (evt, fn) {
      (listeners[evt] = listeners[evt] || []).push(fn);
      return function off() {
        listeners[evt] = (listeners[evt] || []).filter(function (x) { return x !== fn; });
      };
    },
    emit: function (evt, payload) {
      var arr = (listeners[evt] || []).slice();
      for (var i = 0; i < arr.length; i++) {
        try { arr[i](payload, evt); } catch (e) { console.error('Bus listener error', evt, e); }
      }
      // Wildcard
      var any = (listeners['*'] || []).slice();
      for (var j = 0; j < any.length; j++) {
        try { any[j](payload, evt); } catch (e) {}
      }
    }
  };
  window.MesioBus = Bus;

  // ─── Initial state ────────────────────────────────
  // Restaurante: Sazón Caribe (escena demo coherente)
  var state = {
    restaurant: {
      id: 'sazon-caribe',
      name: 'Sazón Caribe',
      city: 'Bogotá Centro',
      currency: 'COP'
    },
    metrics: {
      revenue: 412000,
      orders: 27,
      activeTables: 4,
      ticket: 38500
    },
    floor: (function () {
      // 8 mesas; ocupadas: 2, 4, 7, 8 → 4 ocupadas / 4 libres
      var occupiedSet = { 2: true, 4: true, 7: true, 8: true };
      var arr = [];
      for (var i = 1; i <= 8; i++) arr.push({ num: i, occupied: !!occupiedSet[i] });
      return arr;
    })(),
    kds: [
      { mesa: 4, items: ['3× Ajiaco Bogotano', '3× Cerveza Club'], elapsed: 3 },
      { mesa: 8, items: ['1× Cazuela de Mariscos'], elapsed: 1 }
    ],
    inventory: {
      frijoles: { name: 'Frijoles', stock: 3.0, min: 5.0, unit: 'kg' },
      maiz:     { name: 'Maíz tierno', stock: 2.0, min: 3.0, unit: 'kg' },
      pollo:    { name: 'Pollo', stock: 15.5, min: 5.0, unit: 'kg' },
      papa:     { name: 'Papa', stock: 40, min: 10, unit: 'kg' }
    }
  };

  // ─── Mutators (each emits) ────────────────────────
  function setMetric(key, value, opts) {
    var prev = state.metrics[key];
    state.metrics[key] = value;
    Bus.emit('metric:change', { key: key, value: value, prev: prev, delta: value - prev, opts: opts || {} });
  }

  function bumpMetric(key, by) {
    setMetric(key, state.metrics[key] + by);
  }

  function occupyTable(num) {
    var t = state.floor.find(function (x) { return x.num === num; });
    if (!t || t.occupied) return;
    t.occupied = true;
    Bus.emit('floor:occupy', { num: num });
  }

  function releaseTable(num) {
    var t = state.floor.find(function (x) { return x.num === num; });
    if (!t || !t.occupied) return;
    t.occupied = false;
    Bus.emit('floor:release', { num: num });
  }

  function pushKds(ticket) {
    state.kds.push(ticket);
    Bus.emit('kds:add', { ticket: ticket });
  }

  function consumeInventory(key, amount) {
    var item = state.inventory[key];
    if (!item) return;
    var prev = item.stock;
    item.stock = Math.max(0, +(item.stock - amount).toFixed(1));
    Bus.emit('inventory:change', { key: key, prev: prev, stock: item.stock, item: item });
  }

  // ─── Public API (preserves "MesioDemo" surface) ───
  window.MesioDemo = {
    state: state,
    setMetric: setMetric,
    bumpMetric: bumpMetric,
    occupyTable: occupyTable,
    releaseTable: releaseTable,
    pushKds: pushKds,
    consumeInventory: consumeInventory
  };
})();
