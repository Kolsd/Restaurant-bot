# Commands and environment variables

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Environment and Commands

```bash
Server:  uvicorn app.main:app --reload --port 8000
Migrate: alembic upgrade head          # ALWAYS before starting in production
Tests:   pytest | pytest tests/test_file.py -v
Sim:     python run_ai_sim.py          # Real E2E: real Postgres + Anthropic, 20 multi-turn scenarios
Deploy:  Railway — railway.toml conditional start (web vs inbox worker)
Worker:  WORKER_MODE=inbox → python scripts/run_inbox_worker.py (Railway separate service)

Critical environment variables:
  DATABASE_URL,                 # RUNTIME: connects as mesio_app (non-superuser). RLS enforced automatically.
  DATABASE_URL_ADMIN,           # (RLS Phase 1) Superuser URL, ONLY for Alembic migrations.
                                #   If unset, alembic falls back to DATABASE_URL (backward-compat).
                                #   In prod: NEVER point the app here.
  ANTHROPIC_API_KEY, META_APP_SECRET, ADMIN_KEY,
  META_ACCESS_TOKEN,
  WOMPI_PUBLIC_KEY,             # LEGACY/FALLBACK — see "Per-restaurant Wompi configuration" below
  WOMPI_INTEGRITY_SECRET,       # LEGACY/FALLBACK — see "Per-restaurant Wompi configuration" below
  APP_DOMAIN,
  REDIS_URL,                    # Shared state across the 4 workers (NPS, checkout, cooldowns)
  ALERT_WEBHOOK_URL,            # (optional) Webhook for operational alerts (Slack/Discord)
  DISABLE_EMBEDDED_WORKER,      # "1" to disable the inbox worker embedded in the web service
  WORKER_MODE,                  # "inbox" for a separate Railway worker service
  INBOX_BATCH_SIZE,             # (optional, default 10) rows per inbox worker poll
  INBOX_POLL_INTERVAL_EMPTY,    # (optional, default 1.0) sleep in seconds when a batch comes back empty
  INBOX_DISPATCH_TIMEOUT_S,     # (optional, default 120) timeout per dispatched message
  INBOX_BACKOFF_SECONDS,        # (optional, default "30,120,600,3600,21600") CSV retry schedule
  INBOX_CLAIM_WINDOW_MINUTES,   # (optional, default 3) window before a claimed row becomes visible again if the worker crashes
  BOT_MAX_TOKENS,               # (optional, default 2048) max_tokens ceiling for LLM responses
  BOT_MAX_TOKENS_SHORT,         # (optional, default 768) max_tokens for normal replies
  BOT_MODEL_FAST,               # (optional) override for Anthropic's fast model
  BOT_MODEL_PRECISE,            # (optional) override for Anthropic's precise model
  OPENAI_API_KEY,               # (optional) For voice note transcription (Whisper API). Without it, audio gets a friendly fallback.
  SENTRY_DSN,                   # (optional) Enables Sentry error tracking. Without it, init is a silent no-op.
  SENTRY_ENVIRONMENT,           # (optional, default "production") Distinguishes prod/staging/dev in the Sentry UI.
  SENTRY_RELEASE,               # (optional) Version/commit SHA. Useful for tracking deploys.
  SENTRY_TRACES_SAMPLE_RATE,    # (optional, default 0.1) APM transaction sampling. 1.0 = 100%, 0 = off.
  CLOUDINARY_CLOUD_NAME,        # Visual catalog v2 — Phase 1. MVP uses the free 25 GB tier.
  CLOUDINARY_API_KEY,           # Visual catalog v2 — Phase 1. MVP uses the free 25 GB tier.
  CLOUDINARY_API_SECRET,        # Visual catalog v2 — Phase 1. MVP uses the free 25 GB tier.
  CRM_PHONE_NUMBER_ID,          # Meta Cloud API phone ID for the CRM support number (3144914554). Without it, inbound WA messages on the support number are NOT auto-captured into prospects. Startup logs WARN if unset.
  OTP_PEPPER,                   # Server-side pepper prepended to the OTP before SHA-256. Without it, password-reset OTPs are brute-forceable offline if the DB leaks. Must be 32+ random chars.
  APP_DOMAIN,                   # Used as the WebAuthn RP_ID. Production startup logs CRITICAL if unset (WebAuthn fallback to the Host header is spoofable).
  ADMIN_KEY,                    # Internal /api/internal/* gate. Must be ≥ 32 chars (startup warns if shorter).
  EMAIL_BACKEND,                # (optional, default console) console|resend. console logs without sending; resend without RESEND_API_KEY falls back to console with a warning.
  RESEND_API_KEY,               # (optional) Transactional email provider (password reset, weekly report, CRM welcome).
  EMAIL_FROM,                   # (optional) Sender for transactional emails.
  TURNSTILE_SECRET,             # (optional) Cloudflare Turnstile server-side secret, verified on POST /api/diner/session for order_mode delivery/pickup only (never the dine-in QR path). Unset = verification is a no-op (local/test); logged once at startup, not per request.
  TURNSTILE_SITE_KEY,           # (optional) Cloudflare Turnstile site key — used by the (not-yet-built) delivery/pickup checkout frontend to render the widget. Unused by the backend itself.
```
