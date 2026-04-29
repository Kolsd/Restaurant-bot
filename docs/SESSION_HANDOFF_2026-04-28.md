# Session Handoff — 2026-04-28 (final del día)

**Para el próximo agente IA que entre en este repo:** leé este archivo PRIMERO, antes de PRODUCT_CONTEXT.md, antes de CLAUDE.md, antes de cualquier otra cosa. Tiene el contexto operativo más reciente y te dice exactamente por dónde arrancar.

---

## En 30 segundos

Hoy se hicieron **34 commits en main**. La sesión arrancó cubriendo audit técnico (10 dudas + 12 bugs TOP del audit), siguió a la implementación completa de **QR-Phone-Claim Capa 1**, y terminó con PM testeando en producción descubriendo que **el flujo de mesa end-to-end NO está conectado**.

El producto pasa 1170+ tests unitarios. El flujo end-to-end NO tiene tests. PM está agotado y frustrado (con razón). La próxima sesión NO debe arrancar agregando features — debe arrancar construyendo el E2E test que destape los 10 disconnects entre módulos.

Si lográs SOLO una cosa en la próxima sesión, que sea: `tests/test_e2e_table_flow.py` con un único test que ejecute QR scan → bot → cocina → mesero → caja → factura → NPS, donde los failures se mapeen 1:1 con la tabla de disconnects en PRODUCT_CONTEXT.md regla #13.

---

## Estado de producción (Railway) al cierre

| Aspecto | Estado |
|---|---|
| Última commit en main | `2fdc124` (fix tables filter por location_id, sync legacy branch_id) |
| Migración head DB | `0057_sync_table_branch_id` |
| Bot WhatsApp | ✅ Responde (Anthropic API key resuelta tras saga ANTROPHIC_API_KEY) |
| QR scan → menú web | ✅ Funciona (modal pide teléfono, registra claim) |
| Bot abre table_session | ⚠️ Funciona SI cliente entra teléfono correcto en modal |
| Items del menú web → table_orders | ❌ Si bot no abre sesión, items quedan huérfanos en delivery flow |
| Cocina recibe órdenes | ✅ via /api/table-orders?station=kitchen |
| Mesero ve mesas en /mesero | ✅ post-fix `2fdc124` |
| Mesero marca "entregada" | ❌ NO HAY UI BUTTON (gap del redesign) |
| Caja procesa pago | ✅ via pay_check |
| NPS post-factura | ✅ existe, dispara en factura_entregada |
| Multi-participante mesa | ❌ no implementado (Capa 2 pendiente) |
| Anti-impostor | ❌ no implementado (Capa 3 pendiente) |

---

## Los 10 disconnects descubiertos hoy (PRIORIDAD)

PM testó en vivo el flujo desde QR hasta factura. Estos son los puntos donde un módulo pasa la pelota a otro y la pelota cae:

1. **Bot ↔ Mesa**: si falla el QR-claim (phone mismatch, etc.), bot crea orden de delivery — items NO migran a `table_orders`. Caja ve mesa abierta sin productos.
2. **Mesa ↔ Mesero**: `table_sessions.assigned_staff_id` existe en schema pero ningún UI lo asigna. Sin asignación, no hay mesero responsable.
3. **Cocina ↔ Mesero**: cuando cocina marca `status=listo`, NO se dispara alerta al mesero asignado. La comida queda en el pass sin que nadie sepa que está lista.
4. **Mesero ↔ Cliente**: no hay botón "marcar entregada" en `/mesero` ni `/caja`. Backend acepta `entregado` con role mesero, pero falta UI.
5. **Caja ↔ Bot**: mesero/caja no ve los chats activos del bot. Si cliente le pide algo al bot ("traer servilletas"), nadie lee.
6. **Bot ↔ Cocina (multi-curso)**: si cliente pide entrada + plato fuerte, ambos van juntos a cocina. Plato fuerte llega frío esperando que termine la entrada. Falta secuenciación.
7. **Mesero ↔ Caja**: redesign de `/mesero` quitó tab "Pedidos activos". Mesero tiene que clickear mesa por mesa para ver órdenes pendientes.
8. **Mesa ↔ NPS**: NPS dispara en `factura_entregada` (caja). Para entonces el cliente ya se fue. NPS llega tarde, baja la tasa de respuesta.
9. **Reservas ↔ Salón**: reserva confirmada con depósito no auto-asigna mesa al llegar el cliente. Mesero abre mesa manual.
10. **Multi-orden por mesa**: cuando 4 personas en una mesa quieren pedir cada uno por separado, no hay manera (Capa 2 multi-participante con join_code soluciona esto).

Tabla completa con archivos owner y estimados en [REMEDIATION_PLAN.md](REMEDIATION_PLAN.md) — sección "ACTUALIZACIÓN POST-2026-04-28".

---

## Cómo arrancar la PRÓXIMA sesión (orden exacto)

### Paso 1: Lectura previa (10 min)

Lee en este orden:
1. **Este archivo (`SESSION_HANDOFF_2026-04-28.md`)** — contexto operativo.
2. **`docs/PRODUCT_CONTEXT.md`** — visión, reglas, regla nueva #12 (E2E discipline) y #13 (disconnect inventory).
3. **`docs/MESA_QR_ARCHITECTURE.md`** — diseño de Capa 1/2/3 + status post-deploy.
4. **`docs/REMEDIATION_PLAN.md`** — sección "ACTUALIZACIÓN POST-2026-04-28" tiene el plan re-priorizado.
5. **`docs/PM_ANSWERS.md`** — las 10 dudas técnicas resueltas con PM en sesión 2026-04-27.
6. **`CLAUDE.md`** — reglas del repo (las 17 Reglas del Bot, RLS, Decimal money, etc.).

### Paso 2: Confirmar contexto con PM antes de tocar código

Mensaje sugerido:
> "Leí los docs. Mi entendimiento: vamos a construir el E2E test del flujo de mesa primero, hacer que falle meaningfully, después arreglar los 10 disconnects en orden. Antes de empezar — ¿algo cambió desde anoche? ¿Tu prioridad sigue siendo el flujo de mesa o querés cambiar el orden?"

NO empieces a tirar código sin esta confirmación. PM puede haber pensado durante la noche y querer otro ángulo.

### Paso 3: Sesión 0 — E2E Flow Test

**Entregable de esta sesión**: `tests/test_e2e_table_flow.py` con un test integration que ejecute el camino feliz:

```
1. POST /api/qr-claim (cliente entra phone) → claim creado
2. Mock webhook Meta entrega mensaje del bot al inbox
3. inbox_worker._handle_meta_whatsapp procesa
4. detect_table_context Path 0 → consume claim → crea table_session
5. Bot recibe "quiero pedir hamburguesa" → tool place_order
6. table_orders row creado con location_id correcto
7. GET /api/table-orders?station=kitchen → muestra orden
8. POST /api/table-orders/{id}/status status=listo (role cocina)
9. ASSERT: waiter_alert auto-creado para mesa.assigned_staff_id
10. POST /api/table-orders/{id}/status status=entregado (role mesero)
11. POST /api/table-orders/{id}/checks (caja crea check)
12. POST /api/table-orders/{base}/checks/{id}/pay (caja cobra)
13. ASSERT: fiscal_invoice creado, table_session verified, factura_entregada
14. ASSERT: NPS request enqueueado para el phone
```

Cada paso del test va a fallar en alguno de los 10 disconnects. Eso es esperado y deseable. El test ROJO con failures específicos es el roadmap concreto para las 10 sub-sesiones siguientes.

**Mocks permitidos**: solo Anthropic API (`call_claude`), Meta WhatsApp (`meta_api.send_text`), Wompi (`record_wompi_event`). El RESTO (DB, repos, routes, agent.detect_table_context, qr_claims_repo, etc.) debe ser real, contra TEST_DATABASE_URL.

**Fixture pattern**: usar el `_ConnProxy` + `_PoolShim` de `tests/test_loyalty_aggregates.py` o el real_pool de `tests/test_qr_claim_flow.py` — ambos validados en producción.

### Paso 4: Iterar 10 sub-sesiones

Por cada disconnect en PRODUCT_CONTEXT.md regla #13:
1. Reproducir el disconnect en aislamiento (mini-test).
2. Implementar el fix.
3. Hacer pasar la pieza correspondiente del E2E test.
4. Smoke E2E completo (asegurar que no rompí otro disconnect ya verde).
5. Commit con mensaje claro del WHY.
6. Push.
7. PM verifica en prod (manda mensaje al bot, escanea QR, etc.).
8. Siguiente disconnect.

---

## Decisiones operativas que YA fueron tomadas (no re-debatir)

Estas decisiones tienen confirmación binaria del PM en sesiones anteriores. Re-discutirlas perdería tiempo:

1. **Modelo modular es invariante**. Features off-flag NO se borran (PRODUCT_CONTEXT.md regla #8).
2. **El bot ES el producto**. Si hay tradeoff, gana el bot (regla #1).
3. **Ship fast and break things NO aplica**. Mesio es misión crítica para el restaurante (regla #6).
4. **Pricing es subscription híbrida absorbiendo costos operativos**. Token tracking + module limits son P0 antes del primer cliente paga (no implementado todavía — backlog).
5. **Onboarding es 100% asistido por PM**. Self-signup NO es prioridad. Provisioning Meta es el bottleneck real.
6. **Decisiones del producto las toma PM solo** (Stage A). Customer feedback entra a partir de Stage B (post-cliente #1).
7. **Capa 1 QR-Phone-Claim deployada hoy**. NO re-discutir el diseño, solo conectar los disconnects.
8. **Capa 2 (multi-participante) y Capa 3 (anti-impostor) DIFERIDAS** hasta E2E verde.
9. **El customer entra phone con código país en modal** — gotcha de UX. El backend canonicaliza ambas formas. Si entra OTRO número distinto al de su WhatsApp, el matching falla — eso es UX problem, no code bug.

---

## Cosas que PM dijo durante el día y deberías recordar

- "Claude es mi cofounder. Si tiene ideas son valiosas porque yo no estoy empapado en el mercado. Yo me encargo que la visión de producto sí vaya por donde tiene que ir." (Texto exacto de PM 2026-04-27)
- PM **no es programador**. Hablar simple. No usar jargon.
- PM va a frustrarse cuando algo "que debería funcionar" no funciona. Es válido. La respuesta correcta es ser HONESTO sobre el gap, no defensivo.
- PM dice "siempre habia funcionado" muy seguido. Generalmente no es cierto al pie de la letra — es que no había probado a fondo. NO contradecirlo, solo investigar con datos.

---

## Si encontrás algo NO documentado en estos docs

Antes de implementar:
1. Pregúntale a PM: "¿esto está documentado y me lo perdí, o es decisión nueva?"
2. Si es decisión nueva, anotala en PRODUCT_CONTEXT.md o en este handoff antes de codificar.
3. NO improvises. Las improvisaciones son lo que metió a Mesio en el estado de hoy.

---

## Check de salud del repo

Al final de la sesión 2026-04-28:

```
git log --oneline | head -10
2fdc124 fix(tables): /mesero shows tables — filter by location_id, sync legacy branch_id
503265f fix(ui): kitchen sidebar link → /cocina (canonical route)
604f0b3 fix(qr-claim): defensive fallback for legacy non-canonical claims
d7ceacc fix(qr-claim): canonicalize phone numbers to country-code form
97fdde1 chore(anthropic): remove typo fallbacks, keep ANTHROPIC_API_KEY only
e257a10 fix(anthropic): accept ANTROPHIC_API_KEY (P-H reversed) as fallback name
c410702 diagnose(anthropic): emit env-var snapshot on every missing-key event
48fad75 diagnose(anthropic): list suspicious env var names when key missing
80719ec fix(anthropic): tolerate ANTROPIC_API_KEY typo in env var name
e2113c1 diagnose(anthropic): boot log + lazy client for env-var changes post-startup
```

Tests:
- 1170+ unit/integration tests, ~9-10 minutos contra TEST_DATABASE_URL.
- 0 E2E flow tests (gap principal a resolver).

Migraciones:
- Head: `0057_sync_table_branch_id`.
- Próximas previstas: `0058_pending_table_validation`, `0059_phone_blocklist` (Capa 3, diferida).

---

## Una última cosa

Si después de hacer pasar el E2E test (Sesión 0) y los 10 disconnects (Sesiones 0a-0j), PM querés celebrar con un test de campo real (cliente piloto, mostrarle a alguien) — esa es la señal de que estamos listos para Capa 2 y 3.

Hasta entonces, NO agregar features. El piso primero.

Buena suerte.

— Claude (sesión 2026-04-28, ~14h en linea, 34 commits, mucho aprendido)
