# MESA_QR_ARCHITECTURE.md — Diseño de identificación de mesa

**Generado:** 2026-04-28
**Estado:** Diseño aprobado por PM. Implementación en curso (Sesión QR-Phone-Claim).
**Audiencia:** todo agente IA o dev que toque el flujo de mesa.

---

## El problema que se resuelve

Mesio identifica al cliente en una mesa cuando éste escanea un QR físico colocado en la mesa. El flujo actual (pre-fix) tenía 3 fallas:

1. **El bot no podía identificar la mesa post-QR.** El QR pre-llenaba un mensaje "Hola! Estoy en {mesa}" sin marcadores. El bot caía a un path "manual text" bloqueado por seguridad → cliente recibía respuesta genérica.

2. **Cuando se "arregló" con un marker `[t:tbl-X]` en el mensaje** (commit `35cf4e9`, fix interino), el cliente veía un código técnico feo en su WhatsApp. UX pobre.

3. **Race condition:** dos clientes escaneando QRs distintos en 30 segundos podían quedar asignados a mesas equivocadas. La identificación se hacía por "scan más reciente".

4. **Anti-impostor inexistente:** alguien con foto del QR (Instagram, etc.) podía abrir sesiones falsas en mesas vacías o robadas.

5. **Multi-participante bloqueado:** la regla `table_cooldown` impedía que un grupo en la misma mesa abriera múltiples sesiones — solo el primer teléfono en escanear podía pedir.

## Arquitectura aprobada (3 capas)

### Capa 1 — QR-Phone-Claim (resuelve identificación + race)

**Idea central:** vincular el escaneo a un teléfono específico ANTES de que llegue el mensaje al bot. La identificación de mesa pasa de "match por texto/recencia" a "match por igualdad exacta de teléfono".

**Flujo:**
1. Cliente escanea QR de Mesa N.
2. Browser abre `/menu/{table_id}`.
3. **Modal pre-menú** pide:
   - Teléfono WhatsApp (input numérico).
   - Geolocalización (silencioso vía Geolocation API; opcional).
4. Cliente tipea teléfono, da Continuar.
5. Browser → `POST /api/qr-claim`:
   ```json
   {
     "bot_number": "573108187460",
     "table_id": "tbl-8-1",
     "phone": "3001234567",
     "geo_lat": 4.65,
     "geo_lon": -74.05
   }
   ```
6. Server registra `qr_scan_pending(bot, table, phone, geo_verified, expires_at=NOW+10min)`.
7. Browser muestra el menú normal. Cliente puede mirar la carta, agregar items al carrito web.
8. Cuando toca "Pedir por WhatsApp" → wa.me con mensaje **100% limpio** (sin marker).
9. Cliente envía mensaje. Bot recibe. `detect_table_context` busca claim por `phone` con match exacto.
10. Encontrado → bot abre sesión sobre `table_id` del claim. Marca el claim como `claimed_at=NOW`.

**Cero ambigüedad:** dos clientes escaneando mesas distintas tienen claims con teléfonos distintos. El bot encuentra el de cada uno por igualdad exacta.

**Geo-verified semantics:**
- `true` = browser dio permiso, está dentro de 50m de la sede.
- `false` = browser dio permiso, está LEJOS (>50m). Probable impostor.
- `null` = browser negó/no soporta geolocation.

Este flag NO bloquea nada en Capa 1. Lo usa Capa 3 para mostrar señal al mesero.

### Capa 2 — Multi-participante con código de mesa (resuelve grupos)

**Idea central:** la primera sesión abierta en una mesa establece un código de 4 dígitos (`join_code`). Los siguientes participantes que escaneen necesitan tipear el código para unirse a la misma cuenta.

**Flujo del primer participante (host):**
1. Pedro escanea Mesa 8 → claim → bot abre sesión.
2. Bot:
   > ¡Hola! Te tengo en Mesa 8 🍽️ ¿Cómo te llamamos?
3. Pedro: "Pedro"
4. Bot:
   > Listo Pedro 👍 Acabo de abrir tu cuenta.
   >
   > **Código de la mesa: 4729**
   >
   > Si vienes con compañía, pasales este código.

**Flujo del N-ésimo participante:**
1. Mamá escanea misma Mesa 8 → claim por su teléfono → bot recibe mensaje.
2. Bot detecta: Mesa 8 ya tiene sesión activa.
3. Bot:
   > Veo que ya hay una cuenta abierta en Mesa 8. ¿Cuál es el código?
4. Mamá: "4729"
5. Bot abre sesión adicional para Mamá, vinculada a la misma `table_id`. Bot:
   > ¡Bienvenida! ¿Cómo te llamamos?
6. Mamá: "Carmen"
7. Bot:
   > Listo Carmen 🌸 Te uní a la cuenta de Pedro. ¿Qué te apetece?

**Carrito compartido:** todos los participantes contribuyen al mismo `table_orders.id`. Caja ve un solo ticket de mesa con la opción de split-checks al pagar.

**Quitamos el bloqueo `table_cooldown.blocked` cuando es la MISMA mesa** (lo mantenemos solo cuando es mesa distinta del mismo restaurante — vector "robo de sesión cruzada" legítimo).

### Capa 3 — Anti-impostor (validación primer pedido)

**Idea central:** el primer pedido de una sesión nueva NO se manda a cocina hasta que el mesero confirma físicamente la mesa. A partir del segundo pedido (o tras la primera validación) el flujo es self-service total.

**Flujo:**
1. Cliente abre sesión, conversa con bot, hace pedido.
2. Bot confirma con cliente ("¿Confirmas el pedido?"), guarda como `table_orders.status='pending_table_validation'`.
3. Bot al cliente:
   > Listo, ya envié tu pedido. El mesero pasará en un momento a confirmar.
4. **El pedido NO va a cocina.** Aparece en `/mesero` y `/floorplan` con badge:
   - 🟢 verde si `geo_verified=true`
   - 🟡 amarillo si `geo_verified=null`
   - 🔴 rojo si `geo_verified=false`
5. Mesero ve el panel, pasa por la mesa físicamente:
   - Tap "**Confirmar mesa**" → `pending_table_validation` → `'confirmed'`, pedido a cocina, sesión queda `verified=true`. Siguientes pedidos van directo.
   - Tap "**Mesa fantasma**" → sesión cerrada, pedido cancelado, teléfono **bloqueado por 24 horas** + log `session.imposter_blocked`.

**Conversación natural:** el cliente no sabe que está siendo "validado" — recibe "ya envié tu pedido al mesero, en un momento te lo confirma" como cualquier saludo normal.

## Schema changes

### Tabla nueva: `qr_scan_pending`

```sql
CREATE TABLE qr_scan_pending (
    id              BIGSERIAL PRIMARY KEY,
    bot_number      TEXT NOT NULL,
    org_id          BIGINT NOT NULL,
    location_id     BIGINT NULL,
    table_id        TEXT NOT NULL,
    phone           TEXT NOT NULL,
    geo_verified    BOOLEAN NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '10 minutes',
    claimed_at      TIMESTAMPTZ NULL
);

-- Lookup por bot al recibir mensaje del cliente.
CREATE INDEX ix_qr_scan_pending_lookup
    ON qr_scan_pending (phone, bot_number)
    WHERE claimed_at IS NULL;

-- Cleanup periódico (scheduler).
CREATE INDEX ix_qr_scan_pending_expiry
    ON qr_scan_pending (expires_at)
    WHERE claimed_at IS NULL;

-- RLS por org_id.
ALTER TABLE qr_scan_pending ENABLE ROW LEVEL SECURITY;
ALTER TABLE qr_scan_pending FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON qr_scan_pending
    USING (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint)
    WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint);
```

Migración: `0056_qr_scan_pending.py`.

### Columnas nuevas

```sql
-- Capa 2: código compartido por mesa.
ALTER TABLE table_sessions ADD COLUMN join_code TEXT NULL;
CREATE INDEX ix_table_sessions_join_code ON table_sessions (join_code) WHERE join_code IS NOT NULL;

-- Capa 3: validación primer pedido.
ALTER TABLE table_orders ADD COLUMN pending_table_validation BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX ix_table_orders_pending_validation
    ON table_orders (pending_table_validation, created_at)
    WHERE pending_table_validation = true;

-- Capa 3: bloqueo de teléfonos impostores.
CREATE TABLE phone_blocklist (
    id              BIGSERIAL PRIMARY KEY,
    phone           TEXT NOT NULL,
    blocked_until   TIMESTAMPTZ NOT NULL,
    reason          TEXT NOT NULL,
    org_id          BIGINT NULL,
    blocked_by      TEXT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX ux_phone_blocklist_active
    ON phone_blocklist (phone)
    WHERE blocked_until > NOW();
```

Migraciones (en orden): `0056_qr_scan_pending.py`, `0057_table_join_code.py`, `0058_pending_table_validation.py`, `0059_phone_blocklist.py`.

## Endpoints nuevos

### `POST /api/qr-claim` (público)

**Body:**
```json
{
  "bot_number": "string",
  "table_id": "string",
  "phone": "string (normalizado, sin +)",
  "geo_lat": "number | null",
  "geo_lon": "number | null"
}
```

**Behavior:**
- Resolver org/location desde bot_number.
- Calcular geo_verified si lat/lon provistos vs locations.lat/lon (≤50m → true, >50m → false). Si null → null.
- Insertar en qr_scan_pending. Upsert si ya existe claim para mismo (phone, bot, table) — refresh expires_at.
- Rate limit: 30 claims/IP/min para evitar abuso.

**Response:**
```json
{
  "ok": true,
  "claim_id": "...",
  "expires_at": "..."
}
```

### `POST /api/mesero/tables/{table_id}/confirm-real`

Auth: staff con rol mesero/admin.
Effect: `pending_table_validation=false` para todas las órdenes de esa mesa, `table_sessions.verified=true`. Pedido fluye a cocina.

### `POST /api/mesero/tables/{table_id}/mark-ghost`

Auth: staff.
Effect: cierra todas las sesiones activas de la mesa, cancela órdenes con `pending_table_validation=true`, agrega los teléfonos a `phone_blocklist` por 24 horas con reason `'ghost_table_marked'`.

## Cambios en código

### `app/services/agent.py detect_table_context`

Nueva path **0** (antes de los actuales 1, 2, 3):

```python
# Path 0: QR-Phone-Claim (Capa 1 de la arquitectura).
# Buscar claim no consumido por teléfono+bot. Si existe → consumir y abrir sesión.
with _bypass_tenant("agent.detect_table_context: qr_claim lookup pre-tenant"):
    claim = await qr_claims_repo.find_unclaimed_by_phone(phone, bot_number)
    if claim:
        await qr_claims_repo.mark_claimed(claim["id"])
        # Continuar con flujo de creación de sesión usando claim["table_id"]
        # (idéntico al path 1 actual cuando matcheaba [t:X])
        ...
```

El path 1 (`[t:X]` marker) **se mantiene** como respaldo (QRs viejos físicos ya impresos). El path 3 (manual text) **se mantiene** gated por flag.

### `app/static/js/catalog-v2.js`

- Modal pre-menú con input teléfono + botón "Continuar".
- Llamada a `navigator.geolocation.getCurrentPosition()` (best-effort, timeout 3s, falla → null).
- POST a `/api/qr-claim`.
- Después del modal: render menú normal.
- `buildWaMessage`: cuando `state.tableId` está seteado **Y** el claim fue exitoso, NO incluir el marker `[t:tbl-X]` (la identificación va por el claim, el mensaje queda limpio).

### `app/routes/tables.py public_menu_context`

Cambiar `wa_msg = f"Hola! Estoy en {table['name']} [t:{table['id']}]"` por `wa_msg = f"Hola! Quisiera el menú 👋"` cuando hay claim establecido. Si no hay claim, mantener el marker como fallback.

## Estados de sesión

```
table_sessions.verified BOOLEAN DEFAULT false

  false = sesión recién abierta, primer pedido va a pending_table_validation
  true  = sesión confirmada por mesero, pedidos van directo a cocina

table_orders.pending_table_validation BOOLEAN DEFAULT false

  true  = esperando confirmación del mesero. NO mostrar en KDS de cocina.
  false = pedido normal, fluye a cocina.

table_sessions.join_code TEXT NULL
  set when session is opened as host (first scanner of the table).
  remains until session closes (factura entregada).
```

## Decisiones binarias confirmadas por PM

1. **Geo-fencing:** SÍ. 50m radius. Cero fricción al cliente, alta señal al mesero.
2. **Validación primer pedido:** SÍ. Es el corazón del anti-impostor.
3. **Bloqueo automático tras "Mesa fantasma":** SÍ, 24 horas.
4. **Explicación al cliente del retraso:** SÍ, formulación natural ("ya envié tu pedido, en un momento te lo confirma el mesero"). No revelar que es validación anti-impostor.

## Plan de implementación

### Sesión 1 — QR-Phone-Claim (foundation)

Estimado: 1.5 días.

- [ ] Migración 0056 (tabla qr_scan_pending + indexes + RLS).
- [ ] `app/repositories/qr_claims_repo.py`: create_claim, find_unclaimed_by_phone, mark_claimed, cleanup_expired.
- [ ] Endpoint `POST /api/qr-claim` con rate limit por IP.
- [ ] `detect_table_context` path 0 (lookup por phone).
- [ ] Modal teléfono + geo en `/menu` (catalog-v2.js).
- [ ] `public_menu_context` wa_msg limpio cuando hay claim.
- [ ] Tests unit + integration:
  - Phone match exacto = sesión abre en mesa correcta.
  - Phone no encontrado = bot cae a flujo manual (regular).
  - Race con dos teléfonos distintos = cada uno encuentra el suyo.
  - Geo lejos = `geo_verified=false` en el claim.
  - Geo negado = `geo_verified=null`.
  - Claim expirado = no se consume.
- [ ] Scheduler: limpieza periódica de claims expirados.
- [ ] Smoke E2E: simular scan + envío bot.

### Sesión 2 — Multi-participante (código)

Estimado: 1.5 días. Bloqueada por sesión 1.

- [ ] Migración 0057 (table_sessions.join_code).
- [ ] Generación de código 4-dígitos al abrir sesión host.
- [ ] Bot pide código a participantes adicionales que escanean misma mesa.
- [ ] Quitar bloqueo `table_cooldown` para misma mesa (mantenerlo cross-tenant).
- [ ] Cuenta compartida: items van a `table_orders` con `org_id+table_id` clave, no por phone.
- [ ] `/caja` muestra el `join_code` de cada mesa activa para que cajero pueda asistir si cliente pierde el código.
- [ ] Tests:
  - Segundo phone con código correcto = se une.
  - Segundo phone con código incorrecto = bloqueado tras 3 intentos.
  - Mismo phone re-escaneando misma mesa = no necesita código.
  - Mismo phone escaneando mesa DISTINTA = nueva sesión, no se pide código.

### Sesión 3 — Anti-impostor (validación primer pedido)

Estimado: 1.5 días. Bloqueada por sesión 2.

- [ ] Migración 0058 (table_orders.pending_table_validation).
- [ ] Migración 0059 (phone_blocklist).
- [ ] Bot marca primer pedido como `pending_table_validation=true` cuando sesión es nueva (`verified=false`).
- [ ] Endpoint `POST /api/mesero/tables/{id}/confirm-real`.
- [ ] Endpoint `POST /api/mesero/tables/{id}/mark-ghost`.
- [ ] UI en `/mesero` y `/floorplan`:
  - Badge geo (verde/amarillo/rojo).
  - Botones "Confirmar mesa" / "Mesa fantasma".
- [ ] Bot rechaza mensajes de teléfonos en blocklist.
- [ ] Filtro KDS: cocina NO ve órdenes con `pending_table_validation=true`.
- [ ] Tests:
  - Sesión nueva, primer pedido = pending_validation.
  - Confirm-real = pedido a cocina, sesión verified.
  - Mark-ghost = phone blocked, sesión cerrada, pedido cancelado.
  - Phone bloqueado = bot rechaza primer mensaje con "no puedo atenderte ahora".
  - Segundo pedido en sesión verified = directo a cocina sin pending.

### Total

4.5 días end-to-end. PM aprobó proceder en este orden.

## Notas para sesiones futuras

- **No re-introducir el marker `[t:tbl-X]` en el wa.me prefilled** post-Sesión 1. La identificación va por phone-claim. Marker solo permanece como fallback para QRs físicos viejos pre-fix.
- **El path manual_text sigue gated por `allow_manual_table_number=False`** (default). Ese path NO se desbloquea — sigue siendo solo para casos especiales documentados.
- **Cleanup scheduler**: agregar a `scheduler.py` un task que elimine `qr_scan_pending` con `expires_at < NOW() - INTERVAL '1 hour'`. Frecuencia: cada hora.
- **Logs estructurados clave** (para que PM vea patrones en dashboards futuros):
  - `qr_claim.created` (al crear claim)
  - `qr_claim.consumed` (al match en bot)
  - `qr_claim.no_match` (mensaje del bot sin claim — fallback)
  - `qr_claim.geo_far` (geo_verified=false detectado)
  - `session.imposter_blocked` (mesero marcó fantasma)
  - `session.join_code_failed` (3 intentos sin código correcto)

## Cómo retomar si la sesión actual no termina

1. Leer este archivo entero.
2. Ver `git log --oneline | head -15` para entender qué commits están relacionados.
3. Ver TodoWrite list o memoria del agente — el estado de las 9 sub-tareas.
4. Continuar por la primera sub-tarea pendiente.
5. Cualquier duda de diseño que no esté acá → preguntar al PM antes de improvisar.
