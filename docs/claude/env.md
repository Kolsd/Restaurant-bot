# Comandos y variables de entorno

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## Entorno y Comandos

```bash
Server:  uvicorn app.main:app --reload --port 8000
Migrate: alembic upgrade head          # SIEMPRE antes de arrancar en producción
Tests:   pytest | pytest tests/test_file.py -v
Sim:     python run_ai_sim.py          # Real E2E: Postgres + Anthropic reales, 20 escenarios multi-turno
Deploy:  Railway — railway.toml conditional start (web vs inbox worker)
Worker:  WORKER_MODE=inbox → python scripts/run_inbox_worker.py (Railway separate service)

Variables de entorno críticas:
  DATABASE_URL,                 # RUNTIME: conecta como mesio_app (non-superuser). RLS enforce automático.
  DATABASE_URL_ADMIN,           # (Fase 1 RLS) URL superuser SOLO para migraciones Alembic.
                                #   Si no se setea, alembic cae a DATABASE_URL (backward-compat).
                                #   En prod: NUNCA apuntar la app aquí.
  ANTHROPIC_API_KEY, META_APP_SECRET, ADMIN_KEY,
  META_ACCESS_TOKEN,
  WOMPI_PUBLIC_KEY,             # LEGACY/FALLBACK — see "Configuración Wompi per-restaurant" below
  WOMPI_INTEGRITY_SECRET,       # LEGACY/FALLBACK — see "Configuración Wompi per-restaurant" below
  APP_DOMAIN,
  REDIS_URL,                    # Estado compartido entre 4 workers (NPS, checkout, cooldowns)
  ALERT_WEBHOOK_URL,            # (opcional) Webhook para alertas operativas (Slack/Discord)
  DISABLE_EMBEDDED_WORKER,      # "1" para desactivar inbox worker embebido en web service
  WORKER_MODE,                  # "inbox" para Railway worker service separado
  INBOX_BATCH_SIZE,             # (opcional, default 10) filas por poll del inbox worker
  INBOX_POLL_INTERVAL_EMPTY,    # (opcional, default 1.0) sleep en segundos cuando batch viene vacío
  INBOX_DISPATCH_TIMEOUT_S,     # (opcional, default 120) timeout por mensaje dispatcheado
  INBOX_BACKOFF_SECONDS,        # (opcional, default "30,120,600,3600,21600") schedule de retries CSV
  INBOX_CLAIM_WINDOW_MINUTES,   # (opcional, default 3) ventana antes de que un row claimed sea visible de nuevo si crashea el worker
  BOT_MAX_TOKENS,               # (opcional, default 2048) max_tokens ceiling para respuestas del LLM
  BOT_MAX_TOKENS_SHORT,         # (opcional, default 768) max_tokens para replies normales
  BOT_MODEL_FAST,               # (opcional) override modelo rápido de Anthropic
  BOT_MODEL_PRECISE,            # (opcional) override modelo preciso de Anthropic
  OPENAI_API_KEY,               # (opcional) Para transcripción de voice notes (Whisper API). Sin esto, audios reciben fallback amigable.
  SENTRY_DSN,                   # (opcional) Activa Sentry error tracking. Sin esto, init es no-op silencioso.
  SENTRY_ENVIRONMENT,           # (opcional, default "production") Distingue prod/staging/dev en Sentry UI.
  SENTRY_RELEASE,               # (opcional) Versión/commit SHA. Útil para tracking de deploys.
  SENTRY_TRACES_SAMPLE_RATE,    # (opcional, default 0.1) Sampling de transacciones APM. 1.0 = 100%, 0 = off.
  CLOUDINARY_CLOUD_NAME,        # Catálogo visual v2 — Fase 1. MVP usa free tier 25 GB.
  CLOUDINARY_API_KEY,           # Catálogo visual v2 — Fase 1. MVP usa free tier 25 GB.
  CLOUDINARY_API_SECRET,        # Catálogo visual v2 — Fase 1. MVP usa free tier 25 GB.
  CRM_PHONE_NUMBER_ID,          # Meta Cloud API phone ID for the CRM support number (3144914554). Without it, inbound WA messages on the support number are NOT auto-captured into prospects. Startup logs WARN if unset.
  OTP_PEPPER,                   # Server-side pepper prepended to OTP before SHA-256. Without it, password-reset OTPs are brute-forceable offline if the DB leaks. Must be 32+ chars random.
  APP_DOMAIN,                   # Used as WebAuthn RP_ID. Production startup logs CRITICAL if unset (WebAuthn fallback to Host header is spoofable).
  ADMIN_KEY,                    # Internal /api/internal/* gate. Must be ≥ 32 chars (startup warns if shorter).
  EMAIL_BACKEND,                # (opcional, default console) console|resend. console loguea sin enviar; resend sin RESEND_API_KEY cae a console con warning.
  RESEND_API_KEY,               # (opcional) Proveedor de email transaccional (reset de clave, reporte semanal, bienvenida CRM).
  EMAIL_FROM,                   # (opcional) Remitente de los emails transaccionales.
```

