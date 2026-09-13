# Estado actual, decisiones de producto cerradas y siguiente sesión

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## ▶ ESTADO ACTUAL Y SIGUIENTE SESIÓN — leer primero (actualizado 2026-09-12)

### Qué pasó (sesiones 2026-09-10 → 12, 20 commits `0ab1641..ad8dca1`)
- **La suite mentía.** Los "1368 tests verdes" venían de un subconjunto mockeado: `alembic upgrade head` no construía una DB desde cero, nadie corría los tests con DB y 260 se saltaban en silencio. Arreglado: la cadena de migraciones construye desde vacío (0079), los tests con DB corren y pasan, dependencias de test pinneadas.
- **Pivote de producto: canal web propio en lugar de WhatsApp.** El bot sigue siendo el producto (chat-first). QR → `/chat/{table_id}` → el bot da la carta como tarjetas.
- **Flujo de mesa completo, sin WhatsApp y sin LLM:** el escaneo abre la mesa y muestra un código → los amigos se unen con ese código → carrito determinista con nota por plato → "Enviar pedido" llega a cocina con la nota en la comanda → vista de mesa ("Tú" / "Otro comensal") → "Pedir la cuenta" (lo mío / toda la mesa, tarjeta / efectivo) → el mesero cobra con su datáfono → caja `pay_check` → NPS de 1 a 5 en el chat.
- **P0 cerrados:** el cobro de mesa daba 500 siempre; `db_get_restaurant_by_id` devolvía OTRO restaurante cuando un id de sede coincidía con un id de organización (eliminada; `users.org_id`/`location_id` en 0081); el superadmin escribía sobre el cliente equivocado (endpoints legacy eliminados); "descartar aviso" hacía DELETE; el control de stock nunca funcionaba (doble `json.dumps`); cinco endpoints con RLS mal cableado; tres tests que fallaban solo de noche por zona horaria. El test estrella e2e ya no es intermitente: su "intermitencia" era la colisión de ids.
- **Email transaccional** (`EMAIL_BACKEND=console|resend`) para reset de clave, reporte semanal y bienvenida del CRM, para poder retirar WhatsApp sin dejar dueños bloqueados.

### Decisiones de producto cerradas (no re-discutir)
1. Comensal anónimo (`web:<uuid4>`); nombre y teléfono opcionales, solo al pagar.
2. El pedido va directo a cocina; el mesero recibe aviso, no aprueba.
3. Botón de mesero flotante, siempre visible, que pregunta el motivo (cuenta / cubiertos / servilletas / otra cosa).
4. Chat-first: el bot da la carta en tarjetas con foto y "+"; el primer mensaje saluda y muestra categorías; "Ver carta completa" abre un panel.
5. Nota libre por plato, sin modificadores con precio.
6. Mesa compartida: quien llega después se une con el código que ve el primero.
7. Cobro al lanzamiento POR EL MESERO, sin pasarela: "pago lo mío" o "pago toda la mesa" (= saldo restante).
8. NPS al terminar el servicio (al cobrar), dentro del chat.
9. Wompi OFF (webhook roto, ver sección Wompi). Bold diferido como primera pasarela (ver sección Bold).
10. WhatsApp se retira. Push web fuera de alcance hasta construir domicilios.
11. El superadmin edita solo datos del negocio (organización); los datos de cada sede los edita el restaurante.
12. Enganche comercial = 8 días gratis sobre el plan básico usando `comp_until` (NO un `plan_code='free'`). NO implementado; falta confirmar si es Pulso o Restaurante.

### Siguiente sesión — en este orden
1. **Validar el bot con el LLM real — bloqueante.** Todo lo verificado corrió SIN `ANTHROPIC_API_KEY`. El PM carga la clave; correr `pytest tests/e2e` completo (44 tests con LLM) y `python run_ai_sim.py` (~$2-5). Revisar: la tool `add_to_cart` crea líneas con `line_id`, el carrito entra al contexto con notas sanitizadas, NPS y checkout escritos por chat web.
2. **Tiempo real:** SSE (+ Redis pub/sub, por los 4 workers) en kitchen / bar / mesero / caja y en el estado del comensal; sonido en KDS. Hoy todo es polling de 6 a 30 s.
3. **Primer cliente:** trial de 8 días (`comp_until`) en el alta del CRM; verificar que el alta deja menú, mesas, QR y staff listos para operar.
4. **Multi-sede:** la pantalla del mesero no filtra avisos por sede (el login de staff no guarda `location_id`). Obligatorio antes de vender a cadenas.
5. Barrer el patrón `json.dumps()` pasado a `$n::jsonb` (doble codificación) en el resto del código.
6. Apagar WhatsApp: migrar primero `tests/e2e/conftest.py` y `test_happy_path_full_flow` al canal web; nunca borrar antes de tener el harness equivalente.
7. Antes de reactivar Wompi: arreglar el webhook (sección Wompi).
- **Ops (Railway):** confirmar que `WOMPI_*` NO estén seteadas, que `REDIS_URL` y `DATABASE_URL_ADMIN` sí lo estén, y aplicar `alembic upgrade head` (0079-0081).

### Estado verificado al cierre
- Head `0081_users_org_location`, un solo head, construye desde una DB vacía.
- `pytest tests/ --ignore=tests/e2e --ignore=tests/ai_sim`: **1741 passed / 0 failed** con DB · **1400 passed** sin DB.
- `pytest tests/e2e -m e2e_no_llm`: **15/15**, 11 corridas seguidas limpias. `scripts/lint_frontend.py`: 0 violaciones. `sw.js` `CACHE_VERSION = 'v45'`.

### Entorno local (Windows)
- Python 3.12 en `.venv` · Postgres 16 local (superuser `postgres` / `mesio_local_dev`; rol `mesio_app` / `mesio_app_pw`) · sin Redis (fallback in-process).
- DBs: `mesio_tests` (suite), `mesio_test` (e2e), `mesio_fresh` (scratch aislada). Todas con `ALTER DATABASE <db> SET timezone TO 'UTC'` — obligatorio.
- Pins obligatorios: `pytest==8.4.2`, `pytest-asyncio==0.24.0` (la 1.x rompe ~94 tests con "coroutine was never awaited").
- Para correr tests: `TEST_DATABASE_URL`, `DATABASE_URL` y `DATABASE_URL_ADMIN` apuntando a la DB; `DISABLE_META_SIGNATURE_VERIFY=1` para e2e.
- Servidor local: `uvicorn` no está en el PATH y `.claude/launch.json` es un archivo versionado con 3 configuraciones — NO sobrescribirlo; usar un launcher propio que setee `DATABASE_URL`.

### Reglas aprendidas en estas sesiones
- **Un test que se salta en silencio es un test que miente.** Los tests con DB deben correr contra una DB real.
- **Colisión de ids organización/sede:** nunca pasar un id de tipo dudoso a una búsqueda. Todo test de tenant debe sembrar ids que choquen A PROPÓSITO; los tests previos pasaban solo porque no chocaban.
- **Firmas de pasarelas:** probar contra la documentación o un evento real, nunca contra una firma generada por nuestro propio código.
- **Commit con archivos estáticos → subir `CACHE_VERSION` de `sw.js` en ese mismo commit**, y correr `test_sw_cache_version` después de commitear (solo mira el último commit).
- **Fechas en tests:** comparar siempre en el mismo reloj (UTC). `date.today()` contra `utcnow()` rompió tres tests después de las 19:00.
- **Agentes:** darles el porqué, exigir verificación en navegador y en DB, prohibir debilitar tests, y verificar cada reporte antes de commitear — varios diagnósticos iniciales (propios y de agentes) fueron incorrectos.

