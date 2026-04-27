# PRODUCT_CONTEXT.md — Visión y reglas de producto Mesio

**Generado:** 2026-04-27
**PM:** Miguel (solo founder, no programador, 1 mes en el proyecto)
**Audiencia:** todo agente IA (Claude Code u otro) que entre a trabajar en este repo.
**Prioridad:** **leer este archivo PRIMERO**, antes de CLAUDE.md, antes del audit, antes de cualquier sesión.

---

## ¿Qué es Mesio?

Mesio es un **agente personal en WhatsApp para experiencias gastronómicas**. El cliente final escribe al WhatsApp del restaurante como si le escribiera a un mesero virtual: pide, paga, reserva, pregunta. Atrás de ese chat hay un sistema completo de POS, salón, delivery, fiscal DIAN, nómina, etc., pero **el cliente final NUNCA ve eso**. Solo ve un chat fluido.

**El bot ES el producto.** Sin el bot, Mesio sería un POS tradicional más en un mercado saturado. Con el bot, es la única opción donde "tu mesero te conoce y está siempre disponible en WhatsApp".

> **Visión a futuro (NO para construir hoy, solo para evitar drift):** PM piensa el producto como agente para experiencias gastronómicas y de entretenimiento — bares, discotecas, eventos, conciertos. **Hoy: solo restaurantes.** Si en sesiones futuras un agente IA ve código creep hacia eventos/conciertos, debe preguntar "¿esto es para hoy o estás abriendo el horizonte?".

---

## Cliente objetivo

**No hay vertical:** Mesio es accesible a cualquier restaurante, en cualquier ciudad de Colombia con infraestructura digital (urbana o intermedia, no rural). Tamaños desde **1 dark kitchen / 1 empleado** hasta **cadena con 20+ empleados / 5+ sucursales**.

**Lo que hace esto posible:** **modularidad**. El cliente activa solo los módulos que necesita HOY, y los demás esperan. Cuando crece, los enciende sin migrar de sistema.

**Casos típicos del cliente:**
- Dark kitchen: solo bot delivery activo. Salón apagado, DIAN apagado.
- Restaurante chico (1 sede, 5 empleados): bot + salón + caja. Nómina apagada.
- Cadena pequeña (3 sedes, 25 empleados): todo activo + multi-sucursal + DIAN.

**Lo que esto IMPLICA en código:**
- Las **feature flags** (`module_reservations`, `module_loyalty`, `module_payroll`, etc.) NO son trabajo a medias — son la **columna vertebral del producto**. Cada flag es un módulo facturable independiente.
- **Activar un módulo NO debe requerir activar otro.** Las dependencias entre módulos son enemigas del modelo modular.
- **Apagar un módulo NO debe romper los demás.** Si `module_loyalty=false`, ningún flujo del bot ni del POS puede asumir que loyalty está activo.

---

## ¿Por qué un restaurante elige Mesio sobre alternativas?

**El wedge (entrada al cliente):** el bot WhatsApp con IA real. Loyverse no lo tiene. Aleph no lo tiene. Square no lo tiene. Es la propuesta diferencial.

**El anchor (no se va):** modularidad + sistema completo end-to-end. Después de 1 año el cliente activó 5 módulos, tiene su DIAN integrado, tiene staff con biometría, su loyalty con campañas. Cambiarse a otro sistema = empezar de cero.

**Reto real con el mercado (información de PM, abr-2026):** los restaurantes ya tienen sistemas integrados. La objeción más común es "cambiar es un dolor de cabeza, aunque Mesio sea mejor". Mesio NO compite por reemplazar el sistema actual — gana porque el bot es algo que el sistema actual no tiene y por eso el cliente paga **por encima** de lo que ya paga.

---

## Estado del producto (abril 2026)

| Aspecto | Estado |
|---|---|
| Tiempo de desarrollo | ~1 mes |
| Clientes activos | 0 |
| Pruebas reales con clientes | Solo amigos/familiares, sin restaurante real |
| Urgencia primer cliente paga | Media (1-2 meses horizonte) |
| Bloqueador percibido por PM | "Demasiados bugs, los flujos del bot no son completos. Cada vez que pruebo aparece error nuevo." + "Acceso al dueño es difícil — el mesero filtra." |
| Onboarding actual | 100% asistido por PM. Cliente NO se autoconfigura. |
| Bottleneck principal de scale | **Provisioning Meta WhatsApp** — Mesio NO es Meta Tech Provider, así que cada nuevo cliente = 2-4h de trabajo manual de PM en Meta Developers. |

**Implicación de este estado:**

- **El código tiene features 5× más avanzadas que la validación de mercado.** Esto es la "deriva" que PM describe. Hay payroll, loyalty avanzado, NPS, reviews, etc. — features que ningún cliente ha pedido.
- **La sesión correcta hoy NO es agregar features.** Es **estabilizar el bot a nivel obsesivo** y **desactivar via flag las features no validadas** para que un demo no las muestre.
- **Self-service signup NO es prioridad** — es literalmente imposible mientras Meta no te certifique como Tech Provider.

---

## Modelo de negocio (no validado todavía, pero diseñado)

Mesio cobra por **suscripción híbrida**:
- **Planes prearmados** (típicos para restaurantes que prefieren elegir entre opciones).
- **Sistema modular tipo Lego** (cliente arma su mensualidad sumando módulos a un base price).

**La suscripción absorbe TODOS los costos operativos:** Anthropic (LLM), Whisper (audio), Cloudinary (imágenes), Meta WhatsApp Business messaging, Wompi fees. Mesio queda en deuda con sus proveedores cada mes que el cliente "consume".

**Letra pequeña obligatoria:** cada módulo y cada plan tiene **techo de uso** asociado a su precio. Sin esto, un cliente chatty puede generar 4× el costo del plan que paga. Ejemplo: 5M tokens Anthropic / mes, X mensajes WhatsApp, Y GB Cloudinary, Z minutos Whisper.

**Estado actual:** infraestructura existe parcialmente (`subscription_status`, `subscription_plan`, `billing_log`, `subscription_usage` con `token_count`), pero **no hay enforcement runtime ni tabla de límites por módulo/plan**. Esto es un GAP P0 antes del primer cliente paga.

---

## Cómo decidir qué construir (decisión escalonada)

PM es solo founder. Decisiones del producto vienen de él. **Stage actual = Stage A.**

### Stage A — HOY (sin clientes pagos)
- PM intuición + sugerencias de Claude + entusiasmo del momento.
- Claude tiene **licencia para sugerir** lo que crea bueno para un restaurante (Claude tiene más exposición al mercado que PM).
- PM filtra cada sugerencia por **coherencia de visión**: ¿esto fortalece el bot como mesero virtual en WhatsApp, o es adyacente?

### Stage B — Cuando haya restaurantes pagando (~ post-cliente #1)
- ANTES de cualquier feature nueva, requerir input externo concreto: PM habla con dueños/admins reales y valida.
- "Una idea me parece buena" deja de ser razón suficiente. La razón válida es "el dueño X me dijo que necesita Z para resolver W".

### Stage C — Cuando haya volumen suficiente (~ N>20 clientes)
- A/B test al 2% de restaurantes para features nuevas.
- Solo escalar a 100% si valida con métrica clara.

**¿Cómo etiquetar mis sugerencias en sesiones futuras?**

Cada propuesta del agente IA debe llevar:

> **ON-VISION:** *(strengthens bot-as-mesero-virtual)* — el agente puede proponer y construir si PM aprueba.
>
> **OFF-VISION:** *(adyacente, pero no fortalece el bot directamente)* — el agente DEBE flaguear explícitamente. PM evalúa con más cuidado. Default: posponer.
>
> **VISION-CREEP:** *(extiende a verticales no acordados — eventos, conciertos, retail)* — el agente DEBE rechazar la implementación y solo dejar nota como propuesta a futuro.

**El "vendiendo zapatillas filter":** PM dijo literalmente *"yo me encargo que la visión de producto sí vaya por donde tiene que ir. No que en un momento Claude diga que quiere vender zapatillas."*. Si te das cuenta que estás derivando, parate y preguntá.

---

## Reglas de comportamiento del agente IA

Aprendidas de los errores de Claude Code en el primer mes (no técnicos — operativos):

### 1. El bot es sagrado. Todo lo demás es andamiaje.

> **Regla #0:** *Si hay tradeoff entre calidad del bot y calidad de cualquier otro módulo (POS, payroll, loyalty, NPS, etc.), siempre gana el bot.*

Las 17 "Reglas del Bot — NO ROMPER" en CLAUDE.md son cicatrices de bugs reales. Cada regla existe porque un cliente quedó colgado, perdió un mensaje, vio un cobro duplicado, etc. **Romper una regla del bot = pecado mortal.**

### 2. Trace-the-flow obligatorio

El error más caro de Claude pasado: **construir piezas correctamente pero las fugas estaban en las conexiones**. Ejemplos vividos por PM: orden duplicada (race), NPS no cierra (state machine incompleta), pedido no aparece en cocina (channel attribution mal), pickup aparece en vista del domiciliario (filtro frontend mal).

**Regla:** si toco una pieza de un flujo cerrado del bot, **debo trazar el flujo entero** leyendo los archivos relevantes. Orden → cocina → caja → propina → NPS → billing. No asumir.

### 3. "Done" significa puedo describir el flujo end-to-end

No "los tests pasaron". No "la función retorna lo que pidieron". **Done = puedo dictarle al PM qué pasa en cada paso del flujo que toqué, con asserts.** Si no puedo hacerlo, no terminé.

### 4. Sesiones paralelas con múltiples agentes son anti-patrón para flujos cerrados del bot

El "No-v2 sprint" usó 3 agentes Sonnet en worktrees paralelos. Eso aceleró el resultado pero **introdujo fugas exactamente del tipo que destruyó la confianza de PM**. Para flujos del bot: **un solo agente, secuencial, traza completa.**

Para tareas independientes (frontend visual, repo extraction, dead code) — paralelo está bien.

### 5. Smoke E2E del bot antes de cerrar sesión

Si el cambio toca el bot (`agent.py`, `agent_salon.py`, `agent_external.py`, `agent_tools.py`, `inbox_worker.py`, `state_store.py`, `chat.py`, `orders.py`):
- Correr `python run_ai_sim.py --smoke` antes de cerrar.
- Si no corre por entorno, **decirlo explícitamente** al PM: "no validé E2E porque X".

### 6. Pre-launch posture: feature faltante > feature buggy

Postura operativa hasta que haya tracción de N>5 clientes:
- Si una feature nueva no se puede shippear con smoke test E2E completo → no se shippea. Se queda apagada.
- Mejor que el cliente vea "esta feature aún no está disponible" a que la vea funcionando a medias.
- Mesio es **misión crítica para el restaurante**. Cuando activan Mesio, apuestan su operación. Bug en prod = restaurante pierde la noche.

### 7. Reputational risk domina la priorización

PM dijo: *"Cualquier bug que llegue a prod con un restaurante real va a doler en la credibilidad de lo que vendemos."* Por lo tanto:
- **Todos los bugs ALTOS/CRÍTICOS del audit son P0.** No negociable.
- Ordering por dependencia + esfuerzo, no por "qué urgencia".
- Bugs en módulos desactivados (loyalty/payroll/etc.) son P1 — el flag los oculta. **Pero deben fix antes de re-activar el flag.**

### 8. Modular es invariante. Borrar features queridas = pecado.

Cuando vea una feature "nice-to-have" que no se está usando hoy:
- ❌ NO borrar (sería romper la promesa modular).
- ✅ **Desactivar via flag**, gate UI, mantener código.
- ✅ Sólo borrar lo TRULY muerto: cero callers, cero flag posible que la reactive.

### 9. Documentar el "WHY" en cada commit, no el "WHAT"

PM lee commits para entender qué pasó en su producto. Un mensaje "fix typo" no le dice nada. Un mensaje "fix: race en pay_check causaba doble factura DIAN cuando 2 cajeros pagaban el mismo check en <10s" le dice todo.

### 10. Hablar el lenguaje del PM, no el lenguaje del programador

PM no es programador. Cuando explico:
- Evitar jargon innecesario. "Hash legacy" → "contraseña vieja en formato inseguro".
- Cuando el detalle técnico es necesario, dar la versión simple primero, técnica después.
- Si PM dice "no entendí, es muy técnico", parar y reescribir desde cero.

### 11. Antes de proponer features nuevas, preguntar 3 razones para NO hacerlo

Auto-disciplina del agente:
- ¿Esta feature ya existe parcialmente y solo necesito conectarla?
- ¿Esta feature compite con otra por atención del usuario?
- ¿Esta feature complica el modelo modular (introduce dependencia entre módulos)?
- ¿Esta feature requiere mantenimiento ongoing que PM no puede hacer solo?
- ¿Hay un cliente real que la pidió, o es mi entusiasmo?

Si ≥2 respuestas son "sí preocupante" → recomendar NO construir, dejar solo como nota de backlog.

---

## Mapa de zonas: sagradas, optimizables, borrables

| Zona | Estado | Implicación |
|---|---|---|
| Bot logic (`agent.py`, `agent_salon.py`, `agent_external.py`, `agent_tools.py`) | 🔴 SAGRADA | Tocar sin preguntar = pecado mortal. |
| 17 Reglas del Bot — NO ROMPER (CLAUDE.md) | 🔴 SAGRADA | Cada regla es una cicatriz de bug real. No relajar. |
| Inbox worker claim-then-ack (3 fases) | 🔴 SAGRADA | Romper = pool deadlock = bot caído. |
| Multi-tenant RLS infrastructure | 🔴 SAGRADA | Romper = data leak cross-tenant = fin de Mesio. |
| Decimal money handling (`money.py`) | 🔴 SAGRADA | Romper = cobros mal = cliente furioso. |
| `commit_order_transaction` | 🔴 SAGRADA | Romper = órdenes/inventario huérfanos. |
| Floor plan editor | 🔴 SAGRADA | "Corazón del sistema de salón" (PM literal). |
| DIAN/fiscal billing | 🔴 SAGRADA | Liability legal aún para clientes que NO la usen, porque es módulo activable. |
| Subscriptions infra | 🟡 ESTABLE-CONTRATO | Sagrada salvo cambio explícito de pricing model. Nunca refactor por estética. |
| Auth + sessions (JWT, bcrypt) | 🟢 OPTIMIZABLE | Mejorable libremente. **Login staff operativo es invariante.** |
| Wompi integration | 🟢 OPTIMIZABLE | Cliente puede o no usarlo (puede aceptar comprobantes WhatsApp en su lugar). Tratable como módulo. |
| Scheduler (`scheduler.py`) | 🟢 EVOLUTIVO | Refactor según features evolucionen. |
| Redis state_store | 🟢 EVOLUTIVO | Refactor según features evolucionen. |
| Truly dead code | ⚫ BORRABLE | Permiso explícito para eliminar. Identificar con cero callers + cero flag posible. |
| Features en flag-off list | ⚫ DESACTIVABLE | Gate via flag, no borrar. |

### Truly dead code identificado (permiso para eliminar)

- `app/routes/legacy_redirects.py` (89 LOC)
- `db_save_tip_distribution` + tabla `tip_distributions` (zero writers)
- Legacy paths de routing en `agent_external.py` (líneas 339-371 + 433-507)
- Bloque try/except SHA-256 fallback en `auth.py` (post wipe_test_users.py)
- Tests Wave-2 transicionales obsoletos (17 tests skip-marked)

### Features a desactivar via flag (PM confirmó abr-2026)

- Reservation deposits (`reservation_deposits` flag ya off por default)
- Dynamic discounts (`dynamic_discounts` flag ya off por default)
- Reviews públicas (`module_reviews` flag ya off por default)
- Loyalty (puntos + ledger + campañas + segmentos + funnel) — **CREAR `module_loyalty` flag**
- Payroll (contratos + nómina + deducciones + overtime) — **CREAR `module_payroll` flag**
- WebAuthn biométrico — **CREAR `module_webauthn` flag** (PIN sigue siendo default)
- Marketing module + prospects — **CREAR `module_marketing` flag**

### Features que se quedan ON (andamiaje del bot)

- Reservas (`module_reservations` ON por default — el cliente puede reservar via bot).
- NPS basic (sin reviews públicas).
- DIAN/fiscal (para clientes que facturan).
- Voice notes (audio Whisper).
- Catálogo visual v2 (Cloudinary).
- Floor plan editor.
- POS de mesa, caja, kitchen, bar, mesero.
- Multi-sucursal.
- Auth + staff operativo.
- Subscriptions infra.

---

## Lo que asusta a PM (señal de prioridad)

PM dijo: *"Todos [los bugs CRÍTICOS] me quitan el sueño. Cualquier bug que llegue a prod con un restaurante real va a doler en la credibilidad."*

Implicación operativa:
- Los 22 bugs ALTOS/CRÍTICOS del audit son P0 uniforme.
- Sequencing por dependencia técnica + esfuerzo, no por "qué importa más".
- Antes del primer cliente paga: cero bugs ALTOS/CRÍTICOS sin resolver.

---

## Demo strategy (propuesta del agente, abr-2026)

PM aún no ha llegado a hacer demo a un restaurante. Cuando llegue ese momento, propuesta:

1. **🥇 Número WhatsApp de demo público** — el prospect toca link en landing, su WhatsApp abre con bot real, restaurant ficticio. 90 segundos, zero setup. **Esto es la demo.**
2. **🥈 Loom de 2min del lado dueño** — después de tocar el bot. Muestra orden cayendo en cocina, propina distribuyéndose, NPS llegando al cliente.
3. **🥉 Landing con UN solo CTA** — "Probá el bot ahora". Loom embebido. Módulos como Lego con precio. Form "Hablemos".
4. **Dashboard sembrado del demo** — 80 órdenes ficticias, staff, sucursales mock. Cuando PM screenshare, se ve VIVO.
5. **`/demo-chat.html` como fallback** — para prospects que no quieren compartir su WhatsApp.

**Pre-condición no negociable:** las "Reglas del Bot" verificadas con `run_ai_sim.py --smoke` corriendo verde de manera reproducible. Bot buggy en demo público = matás la marca.

---

## Cómo trabajar con este PM

- PM es **solo founder**. No hay equipo. No hay cofounder. No hay advisors involucrados en decisiones del producto.
- PM ve a Claude como **cofounder** (no como junior dev). Valora ideas porque tiene más exposición al mercado.
- PM **no es programador**. Hablar simple, evitar jargon innecesario.
- PM tiene **alta tolerancia a sugerencias del agente**, baja tolerancia a **deriva de visión**.
- PM **valora honestidad por encima de optimismo**. Si una sesión no terminó bien, decírselo. Si un test no se pudo correr, decírselo. No prometer cosas no validadas.
- PM **tolera deuda técnica** mientras no comprometa la credibilidad del bot. Pero **deuda en el bot = miedo existencial** para él.

---

## Checklist para cualquier sesión nueva en este repo

Antes de empezar a editar, el agente debe responder mentalmente:

- [ ] ¿Leí PRODUCT_CONTEXT.md (este archivo)?
- [ ] ¿Leí CLAUDE.md (reglas técnicas)?
- [ ] ¿Leí PM_ANSWERS.md (resoluciones de las 10 dudas)?
- [ ] ¿Esta tarea está en el REMEDIATION_PLAN, o es nueva?
- [ ] Si es nueva: ¿está ON-VISION? ¿flagueé OFF-VISION o VISION-CREEP si aplica?
- [ ] ¿Toco el bot? Si sí: ¿corro `run_ai_sim.py --smoke` al final?
- [ ] ¿El cambio implica deshabilitar una feature? Usar flag, no borrar código.
- [ ] ¿Todo lo que voy a hacer cabe en una sesión secuencial, o estoy disparando agentes paralelos sobre flujos cerrados del bot? (Anti-patrón.)
- [ ] ¿Puedo describir el flujo end-to-end al final, con asserts en cada paso? Si no, no terminé.
