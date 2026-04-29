/* ════════════════════════════════════════════════════
   scenarios.js · Narrativa declarativa
   5 escenarios = 75-90s totales (Loom-ready)
   Cada step: { at: ms, type, ...payload }
   Tipos: msg-in | msg-out | typing | quick | dash-event
   ════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var SCENARIOS = {

    // ════════════════════════════════════════════════
    // 1. MESA — cliente escanea QR, ordena y paga
    // ════════════════════════════════════════════════
    mesa: {
      id: 'mesa',
      title: 'Mesa con QR',
      label: 'Cliente escanea QR, ordena y paga',
      duration: 14000,
      steps: [
        { at: 0,    type: 'msg-out', text: '¡Hola! Escaneé el QR de la mesa 5 🪑', time: '12:42' },
        { at: 600,  type: 'dash-event', label: 'QR escaneado · Mesa 5', emoji: '📲' },

        { at: 1400, type: 'typing', duration: 900 },
        { at: 2300, type: 'msg-in',  text: '¡Bienvenido a <b>Sazón Caribe</b>! Te ubico en <b>Mesa 5</b>. ¿Querés ver la carta o ya sabés qué pedir?', time: '12:42' },
        { at: 2400, type: 'dash-event', label: 'Mesa 5 ocupada', emoji: '🪑', floor: 5 },

        { at: 3200, type: 'quick', chips: [
          { text: 'Ver carta' },
          { text: 'Ya sé qué pedir' }
        ]},

        { at: 4200, type: 'msg-out', text: '2 ajiacos y 2 cervezas Club, por favor', time: '12:43' },

        { at: 5200, type: 'typing', duration: 800 },
        { at: 6000, type: 'msg-in',  text: 'Perfecto. Te confirmo:<br><span class="wa-li">• 2× Ajiaco Bogotano — $44.000</span><span class="wa-li">• 2× Cerveza Club — $14.000</span><br><b>Total: $58.000</b>', time: '12:43' },
        { at: 6100, type: 'dash-event', label: 'Pedido enviado a cocina · Mesa 5', emoji: '🍽️',
          kds: { mesa: 5, items: ['2× Ajiaco Bogotano', '2× Cerveza Club'], elapsed: 0 },
          inventory: [ { key: 'frijoles', amount: 0.4 } ]
        },

        { at: 7800, type: 'msg-in', text: '¿Pagás ahora con Wompi (Nequi/PSE/tarjeta) o al final con el mesero?', time: '12:43' },
        { at: 8400, type: 'quick', chips: [
          { text: 'Pagar ahora con Nequi' },
          { text: 'Al final' }
        ]},

        { at: 9600,  type: 'msg-out', text: 'Pagar con Nequi 📲', time: '12:44' },
        { at: 10300, type: 'typing', duration: 700 },
        { at: 11000, type: 'msg-in',  text: '✅ <b>Pago confirmado</b> · Wompi · Ref. WP-8821<br>Te llegó la <b>factura DIAN</b> al WhatsApp.', time: '12:44' },
        { at: 11100, type: 'dash-event', label: 'Pago Wompi · $58.000 · Mesa 5', emoji: '💰',
          metrics: { revenue: +58000, orders: +1 }
        },
        { at: 11600, type: 'dash-event', label: 'Factura DIAN emitida · CUFE válido', emoji: '🧾' },

        { at: 13000, type: 'msg-in', text: 'Gracias 🙌 Tu cocina ya recibió el pedido. Te avisamos cuando esté listo.', time: '12:44' }
      ]
    },

    // ════════════════════════════════════════════════
    // 2. DELIVERY — cliente pide a domicilio
    // ════════════════════════════════════════════════
    delivery: {
      id: 'delivery',
      title: 'Domicilio',
      label: 'Pedido a domicilio con seguimiento',
      duration: 15000,
      steps: [
        { at: 0,     type: 'msg-out', text: 'Hola, ¿hacen domicilios al barrio Chapinero?', time: '13:18' },

        { at: 800,   type: 'typing', duration: 800 },
        { at: 1600,  type: 'msg-in',  text: 'Sí 🛵 Tarda <b>25-35 min</b>. ¿Qué te llevamos?', time: '13:18' },

        { at: 2600,  type: 'msg-out', text: '1 bandeja paisa y 2 jugos de lulo', time: '13:19' },

        { at: 3500,  type: 'typing', duration: 900 },
        { at: 4400,  type: 'msg-in',  text: 'Listo:<br><span class="wa-li">• 1× Bandeja Paisa — $32.000</span><span class="wa-li">• 2× Jugo de lulo — $12.000</span><br><b>Subtotal: $44.000</b><br>+ Domicilio: $5.000<br><b>Total: $49.000</b>', time: '13:19' },
        { at: 4500,  type: 'dash-event', label: 'Pedido domicilio recibido', emoji: '🛵',
          metrics: { orders: +1 },
          kds: { mesa: 'Domicilio', items: ['1× Bandeja Paisa', '2× Jugo de lulo'], elapsed: 0 }
        },

        { at: 6200,  type: 'msg-in', text: '¿Confirmas dirección? <b>Cra 13 # 53-20, Apto 402</b>', time: '13:19' },
        { at: 6800,  type: 'quick', chips: [
          { text: 'Sí, confirmar' },
          { text: 'Cambiar dirección' }
        ]},

        { at: 7800,  type: 'msg-out', text: 'Sí, confirmar', time: '13:20' },

        { at: 8500,  type: 'typing', duration: 700 },
        { at: 9200,  type: 'msg-in', text: '✅ Pedido confirmado · pago contra entrega.<br>Te aviso cuando salga del local.', time: '13:20' },
        { at: 9300,  type: 'dash-event', label: 'Domicilio asignado · Carlos M.', emoji: '🛵' },

        { at: 11000, type: 'msg-in', text: '🛵 Tu pedido <b>salió hace 2 min</b>. Llega ~13:42.<br>Seguilo acá: <span style="color:#1D9E75;text-decoration:underline">mesio.app/seguir/8h2k</span>', time: '13:21' },
        { at: 11100, type: 'dash-event', label: 'En ruta · ETA 13:42', emoji: '📍' },

        { at: 13000, type: 'msg-in', text: '✅ <b>Entregado</b> · Pago $49.000 recibido<br>Factura DIAN: <span style="color:#1D9E75;text-decoration:underline">descargar</span>', time: '13:42' },
        { at: 13100, type: 'dash-event', label: 'Entregado · $49.000', emoji: '✅',
          metrics: { revenue: +49000 }
        }
      ]
    },

    // ════════════════════════════════════════════════
    // 3. NPS — feedback post-visita
    // ════════════════════════════════════════════════
    nps: {
      id: 'nps',
      title: 'NPS post-visita',
      label: 'Encuesta NPS automática + alerta',
      duration: 14000,
      steps: [
        { at: 0,    type: 'dash-event', label: 'Mesa 4 cerró cuenta · enviando NPS', emoji: '✦' },
        { at: 200,  type: 'msg-in', text: 'Hola Camila 👋 Soy Mesio, el asistente de <b>Sazón Caribe</b>. ¿Cómo estuvo tu visita hoy?', time: '14:08' },

        { at: 1200, type: 'msg-in', text: 'En una escala del 0 al 10, ¿qué tan probable es que nos recomendés a un amigo?', time: '14:08' },
        { at: 1800, type: 'quick', chips: [
          { text: '10' }, { text: '9' }, { text: '8' }, { text: '7' }, { text: 'Otro' }
        ]},

        { at: 3200, type: 'msg-out', text: '7', time: '14:09' },
        { at: 3300, type: 'dash-event', label: 'NPS recibido · Camila R. · score 7', emoji: '⭐' },

        { at: 4200, type: 'typing', duration: 900 },
        { at: 5100, type: 'msg-in', text: 'Gracias 🙏 Un 7 nos cuenta que algo se puede mejorar. ¿Qué fue lo que no estuvo a la altura?', time: '14:09' },

        { at: 6800, type: 'msg-out', text: 'La sopa llegó tibia y el mesero tardó en venir', time: '14:10' },

        { at: 7700, type: 'typing', duration: 1000 },
        { at: 8700, type: 'msg-in', text: 'Lamento mucho eso, Camila. Ya le avisé al dueño. Como compensación tenés <b>15% off</b> en tu próxima visita con código <b>VOLVE15</b>.', time: '14:10' },
        { at: 8800, type: 'dash-event', label: 'Alerta enviada al dueño · cupón generado', emoji: '🚨' },

        { at: 10500, type: 'msg-out', text: 'Gracias 🙏', time: '14:11' },

        { at: 11500, type: 'msg-in', text: 'A vos. Te esperamos pronto.', time: '14:11' },

        { at: 13000, type: 'dash-event', label: 'Insight ✦ "3 quejas sobre temperatura esta semana"', emoji: '✦' }
      ]
    },

    // ════════════════════════════════════════════════
    // 4. RESERVA — anticipada, equipo recibe
    // ════════════════════════════════════════════════
    reserva: {
      id: 'reserva',
      title: 'Reserva anticipada',
      label: 'Cliente reserva mesa para mañana',
      duration: 13000,
      steps: [
        { at: 0,    type: 'msg-out', text: 'Hola, ¿tienen mesa para 6 personas mañana 8 pm?', time: '17:30' },

        { at: 800,  type: 'typing', duration: 800 },
        { at: 1600, type: 'msg-in', text: 'Hola 🙌 Déjame revisar la disponibilidad…', time: '17:30' },

        { at: 2800, type: 'typing', duration: 900 },
        { at: 3700, type: 'msg-in', text: 'Sí, tengo:<br><span class="wa-li">• Mesa 9 (terraza, 6p) — 8:00 pm</span><span class="wa-li">• Mesa 12 (interior, 6p) — 8:30 pm</span>', time: '17:30' },
        { at: 4400, type: 'quick', chips: [
          { text: 'Mesa 9 - 8:00 pm' },
          { text: 'Mesa 12 - 8:30 pm' }
        ]},

        { at: 5400, type: 'msg-out', text: 'Mesa 9 está perfecta', time: '17:31' },

        { at: 6200, type: 'typing', duration: 700 },
        { at: 6900, type: 'msg-in', text: '¿A nombre de quién la dejo?', time: '17:31' },

        { at: 7800, type: 'msg-out', text: 'Andrés Gómez · 3001234567', time: '17:31' },

        { at: 8600, type: 'typing', duration: 800 },
        { at: 9400, type: 'msg-in', text: '✅ <b>Reserva confirmada</b><br><span class="wa-li">• Mesa 9 · Terraza</span><span class="wa-li">• Mañana, 8:00 pm</span><span class="wa-li">• 6 personas · Andrés</span><br>Te recordamos 2 h antes 🕗', time: '17:31' },
        { at: 9500, type: 'dash-event', label: 'Reserva creada · Mesa 9 · mañana 8 pm', emoji: '📅' },

        { at: 11000, type: 'dash-event', label: '✦ Insight: 87% ocupación mañana 8-10 pm', emoji: '✦' },
        { at: 12000, type: 'msg-in', text: 'Gracias Andrés, te esperamos 🙌', time: '17:32' }
      ]
    },

    // ════════════════════════════════════════════════
    // 5. NÓMINA — clock-in biométrico del staff
    // ════════════════════════════════════════════════
    nomina: {
      id: 'nomina',
      title: 'Clock-in del equipo',
      label: 'Mesera marca turno con huella · nómina automática',
      duration: 12000,
      steps: [
        { at: 0,    type: 'dash-event', label: 'Yulieth A. · marcando turno…', emoji: '👤' },
        { at: 200,  type: 'msg-in', text: '👋 Hola Yulieth, vi que ibas a entrar al turno de <b>cena (5–11 pm)</b>. Confirmá con tu huella desde la tablet del bar.', time: '17:02' },

        { at: 2000, type: 'msg-out', text: '✓ Huella registrada', time: '17:02' },
        { at: 2100, type: 'dash-event', label: '✓ Clock-in · Yulieth A. · 17:02', emoji: '🔐' },

        { at: 3000, type: 'typing', duration: 800 },
        { at: 3800, type: 'msg-in', text: '¡Listo! Tu turno arrancó. Hoy te toca:<br><span class="wa-li">• Mesas 1–6 (terraza)</span><span class="wa-li">• Cierre de caja al final</span>', time: '17:02' },
        { at: 3900, type: 'dash-event', label: 'Mesas 1–6 asignadas a Yulieth', emoji: '👥' },

        { at: 5800, type: 'msg-in', text: 'Tu acumulado de la quincena: <b>$612.000</b> (78 h trabajadas)', time: '17:02' },
        { at: 5900, type: 'dash-event', label: 'Nómina actualizada · 78 h · $612.000', emoji: '💼' },

        { at: 7800, type: 'msg-in', text: '✦ Recordatorio: este viernes pago de quincena automático vía Nequi.', time: '17:03' },
        { at: 8000, type: 'dash-event', label: '✦ Pago automático Nequi · viernes', emoji: '✦' },

        { at: 10000, type: 'dash-event', label: 'Insight ✦ "Equipo completo · 0 ausencias hoy"', emoji: '✦' }
      ]
    }
  };

  // Order = the auto-play sequence (Loom-ready)
  var ORDER = ['mesa', 'delivery', 'nps', 'reserva', 'nomina'];

  window.MesioScenarios = SCENARIOS;
  window.MesioScenarioOrder = ORDER;
})();
