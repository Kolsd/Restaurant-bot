# Reglas del bot — NO ROMPER (17 reglas)

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## Reglas del Bot — NO ROMPER (aprendidas de 79 bugs en 4 auditorías)

Estas reglas protegen los flujos críticos del bot de WhatsApp. Toda modificación a `agent.py`, `agent_salon.py`, `agent_external.py`, `orders.py`, `orders_repo.py`, `inbox_worker.py`, `state_store.py`, o `chat.py` DEBE cumplir TODAS estas reglas.

### 1. Serialización: Decimal NUNCA en state_store
- `state_store` serializa con `json.dumps`. `Decimal` no es JSON-serializable.
- ANTES de guardar cualquier valor en `checkout_set`, `nps_set`, o cualquier `state_store.*_set`: convertir a `float(quantize_money(valor))` con comentario `# JSON boundary`.
- Verificar: buscar `state["` + `Decimal` en la misma función = bug.

### 2. Tool Use: Validar ANTES de ejecutar
- `_validate_tool_call()` es la barrera entre el LLM y la cocina/DB. Toda tool call pasa por ahí.
- `tool_input` DEBE ser `dict` (guardia `isinstance`). Claude puede devolver SDK objects.
- `qty` DEBE parsearse con try/except, default a 1. Claude puede devolver `"dos"` o `null`.
- `items` DEBE validarse como `list` antes de iterar.
- `guests` en reservas DEBE ser `int > 0`.
- Dedup guard DEBE cubrir `place_order`, `create_delivery_order` Y `create_pickup_order`.
- Cuando el dedup bloquea, retornar mensaje NEUTRAL ("ya está siendo procesado"), NUNCA el reply del LLM (que dice "pedido confirmado").

### 3. Checkout Flow: State machine completa
- Todos los steps del checkout (`asking_split`, `asking_tip`, `asking_tip_custom`, `asking_factura`, `asking_payment_N`, `confirming`) DEBEN tener un branch en `handle_checkout_flow`.
- Si falta un branch, el mensaje cae al LLM y el checkout se pierde.
- `requires_proof` DEBE persistirse ANTES de `checkout_set`, no después.
- Check `total` en `_save_checkout_proposal` es el subtotal por check. El pago en `_auto_confirm_checks` DEBE incluir `tip_per_check` en el amount.

### 4. Conexiones DB: NUNCA retener durante dispatch
- El inbox worker usa patrón claim-then-ack en 3 fases:
  1. **Claim** (ms): `fetch_batch` + `claim_rows` dentro de transacción corta → liberar conexión
  2. **Dispatch** (hasta 120s): sin conexión DB abierta, `asyncio.wait_for(timeout=120)`
  3. **Ack** (ms): nueva conexión corta para `mark_processed` o `mark_failed`
- PROHIBIDO meter el dispatch dentro de un `async with conn.transaction()`. Esto causa deadlock de pool bajo carga.
- Si `mark_processed` o `mark_failed` fallan en fase 3, loguear y continuar — el row se reintentará cuando expire el claim (3 min).

### 5. Cart Locks: Ownership obligatoria
- `cart_lock_acquire` retorna un UUID token. `cart_lock_release` DEBE recibir ese token.
- Release sin token (`token=None`) DEBE rechazarse (early return + log error).
- TODAS las funciones que usan `_cart_lock` DEBEN manejar `RuntimeError("cart_lock_contention")`.
- `migrate_cart` DEBE lockear AMBOS bot_numbers (source Y destination) en orden determinístico para evitar deadlock.
- Fallback cart lock timeout: 5 segundos máximo (no 30s, bloquea el event loop).

### 6. Webhook Meta: Nunca perder mensajes
- Retornar 200 a Meta = "mensaje recibido, no reenviar". Retornar 503 = "reenviar todo el batch".
- Si `enqueue` falla para UN mensaje del batch, NO retornar 503 inmediatamente — procesar los demás y retornar 503 al final.
- `changes: []` o `messages: []` (lista vacía, no key ausente) DEBE manejarse con `if not list: continue`, no con `[0]` directo.
- Firma Meta inválida → retornar 200 (no 401). 401 causa retry flood infinito.
- Mensajes sin `wam_id` DEBEN tener un `external_id` sintético (`synth_sha256(phone:text:bot:epoch//10)`) para que el índice de dedup funcione.
- NUNCA almacenar `access_token` de Meta en el payload del inbox. El token se busca de la DB en dispatch time.

### 7. Rate Limiting: Redis para cross-worker
- Rate limits globales (webhook flood) DEBEN usar `state_store.rate_limit_check` (Redis INCR), NO contadores module-level (son per-worker, no per-plataforma).
- `rate_limit_check` en Redis: `EXPIRE` solo se setea cuando `count == 1` (primer request). NUNCA resetear el TTL en cada request.
- Fallback in-process: dicts con size cap de 10K entries. Evicción por timestamp más antiguo, NO por orden de inserción.

### 8. LLM: Nunca silencio al cliente
- `call_claude()` DEBE estar en try/except. On failure → reply amigable ("problema técnico, intenta de nuevo").
- Retry: 3 intentos con backoff para errores transientes (429, 503, 529, timeout, connection error).
- Reply vacío o None del LLM → fallback "¿En qué te puedo ayudar?"
- `end_session` bloqueado (pedido activo/cuenta pendiente) → mensaje contextual, NUNCA el farewell del LLM.
- `_INJECTION_RE` se evalúa en `_wrap_user_message` ANTES de enviar al LLM. Patrones bloqueados retornan string vacío.

### 9. NPS: Manejo de carreras y cleanup
- `_handle_nps_flow` puede retornar `None` si la key expiró entre dos reads (race multi-worker). `_try_nps_active_flow` DEBE verificar `if nps_reply is None: return None` antes de enviar el prompt.
- `skip_nps` cuando state es `waiting_comment` DEBE finalizar el record pendiente (`db_update_nps_comment("Sin comentario")`), no dejarlo huérfano con `__pending__`.
- `trigger_nps` DEBE recibir el `restaurant_name` real, no string vacío.

### 10. Concurrencia: Asumir 4 workers siempre
- Todo estado mutable (NPS, checkout, cooldowns, cart locks) va por Redis via `state_store`.
- Fallback in-process es degradado, NO equivalente. Documentar diferencias.
- `decode_responses=True` en Redis client → valores son `str`, NUNCA `bytes`. No poner `.decode()` defensivo.
- `FOR UPDATE SKIP LOCKED` solo protege dentro de una transacción. Al liberar la transacción, el row es visible para otros workers.
- `asyncio.TimeoutError` es subclase de `Exception` en Python 3.11+. Catches deben usar `except (Exception, asyncio.TimeoutError)` para compatibilidad.

### 11. GPS y Branch Routing
- Coordenadas 0,0 son válidas (Golfo de Guinea). Usar `if lat is None` en lugar de `if not lat`.
- Branch con `whatsapp_number = NULL` → fallback al número del parent.
- `restaurant_obj` DEBE actualizarse cuando se hace branch override (no conservar el Matriz ID).
- `_try_checkout_flow` y `db_save_history` DEBEN propagar `branch_id` del table_context.

### 12. find_dish: Matching seguro
- Pass 1: exact match case-insensitive (siempre).
- Pass 2: substring con ratio mínimo 40% (`query_len / item_len >= 0.4`). SIN ratio, una query de 2 letras matchea un nombre de 30.
- `remove_from_cart` DEBE verificar que el item existía antes de confirmar remoción.
- `add_to_cart` DEBE rechazar `qty <= 0`.

### 13. Errores tipados en pipeline de órdenes
- `InsufficientStockError` → mensaje al cliente sobre stock, NO silenciar con `except Exception`.
- `OrderCommitError` → mensaje al cliente sobre error de pedido.
- Ambos DEBEN capturarse ANTES del `except Exception` genérico en `execute_action`.
- `commit_order_transaction` DEBE recibir el cart real con items, NUNCA `cart={}`.

### 14. Tenant scope en bot runtime (Fase 1 RLS)
- `inbox_worker._handle_meta_whatsapp` DEBE envolver `_process_message(...)` en `with tenant_scope(_tenant_id):` una vez resuelto el restaurante desde `bot_number`. Si no lo hacés, CUALQUIER repo migrado explota con `TenantNotSetError` dentro del flujo del bot.
- `scheduler._scheduler_loop` DEBE entrar en `bypass_tenant_scope("scheduler_leader_tick")` antes del leader tick, Y envolver cada iteración per-restaurant en `tenant_scope(rid)`.
- `chat.py meta_webhook` DEBE entrar en `bypass_tenant_scope("webhook_enqueue_cross_tenant")` durante el enqueue (pre-resolución).
- `agent.py detect_table_context / get_session_state / _handle_nps_guard / _resolve_branch_id` usan `_bypass_tenant` (aliased a `bypass_tenant_scope_if_unset`, **soft-bypass**). En producción corren dentro de `tenant_scope(rid)` activado por `inbox_worker`, así que el helper es un **no-op** — la query corre bajo el scope real. En call sites legacy (`/chat` POST endpoint interno, Twilio webhook) sin scope previo, sí entra a un bypass real para preservar compat. Usar el strict `bypass_tenant_scope` (no el soft) sólo para casos genuinamente cross-tenant (internal admin, scheduler leader, inbox pre-resolución).
- `orders.py process_order_callback` (Wompi) DEBE entrar en `tenant_scope(order["restaurant_id"])` tras cargar la orden.
- NO volver a "silent fail" en el bot runtime. Si un `TenantNotSetError` aparece en producción, es un gap de wiring, NO un caso a suprimir.

### 15. Confirmación de pedido: vocabulario amplio + elongación de vocales
- `_CONFIRM_WORDS` en `agent.py` incluye `vale, bueno, claro, correcto, bien, excelente, genial, sii/siii/siiii` además del set clásico (`sí, ok, dale, perfecto, listo, ...`). PROHIBIDO recortarlo "porque parece largo" — cada palabra está ahí porque un usuario real la usó y el bot ignoró su confirmación.
- `_last_messages_have_confirmation` colapsa elongaciones de vocal (`re.sub(r"([aeiou])\1{1,}", r"\1", lowered)`) ANTES de matchear: `vaaaaale`→`vale`, `siiii`→`si`. NO remover la normalización — los usuarios estiran vocales en WhatsApp constantemente.
- El match union incluye AMBAS formas (lowered + normalized) para no perder tokens donde la elongación accidentalmente cae sobre una palabra válida.

### 16. Single-location restaurants: no preguntar sucursal
- `agent.py` inyecta `[UBICACION_UNICA: ...]` en el contexto cuando `db_get_branches` retorna lista vacía (tenant de una sola sede). Esto es la contraparte explícita del `[SUCURSALES: ...]` del caso multi-sede.
- `agent_external.py` STEP 2 tiene una `CRITICAL — SINGLE-LOCATION RULE` que dice "si NO hay [SUCURSALES] block, hay UNA sola sede; NUNCA preguntes cuál sucursal".
- Sin estos hints el LLM preguntaba "¿de qué sucursal?" en restaurantes de UNA sola sede, rompiendo el flujo pickup/delivery. Si tocás el system prompt o la inyección de contexto, MANTENER ambos hints.

### 17. Bill request fires waiter_alert al instante (no espera al checkout completo)
- `agent_salon.py` checkout flow llama a `db.db_create_waiter_alert(alert_type='bill', ...)` **en el momento que el cliente pide la cuenta**, ANTES de que termine la state machine de checkout (split → tip → método → factura).
- Razón: staff de piso necesita ver "mesa X pidió la cuenta" en el POS al instante, no después de 4 turnos de conversación.
- El alert es un **hint, no un commitment**. El pago real sigue su flujo normal — esto solo notifica.
- Failure de crear el alert se loguea pero NO bloquea el checkout (best-effort). Si removés el try/except, el bot deja de procesar checkouts cuando waiter_alerts esté caído.

