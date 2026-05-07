# Sentry Setup — Mesio

## DSN env var

Set `SENTRY_DSN` in Railway (both the web service and the inbox worker service).
The value comes from: Sentry UI → mesio org → python-fastapi project → Settings → Client Keys (DSN).

Optional tuning vars (already have sensible defaults in code):

| Var | Default | Notes |
|-----|---------|-------|
| `SENTRY_ENVIRONMENT` | `production` | Change to `staging` on a staging service |
| `SENTRY_RELEASE` | auto (git SHA) | Railway injects `RAILWAY_GIT_COMMIT_SHA` — set `SENTRY_RELEASE=$RAILWAY_GIT_COMMIT_SHA` for deploy tracking |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.1` (10 %) | Raise to `0.5` if you want more APM coverage; lower to `0.0` to disable tracing entirely |

## Component tagging

`init_sentry` is called in both services with a `component` argument:

- `app/main.py` → `init_sentry("web")`
- `scripts/run_inbox_worker.py` → `init_sentry("inbox_worker")`

Every Sentry event gets these tags automatically (via `before_send`):

| Tag | Value |
|-----|-------|
| `component` | `web` or `inbox_worker` |
| `org_id` | integer tenant ID when a tenant is active (blank on cross-tenant ops) |

These tags let the CEO filter alerts by component or by restaurant in the Sentry Issues UI.

## Configuring email alerts to miango950@gmail.com

1. Go to <https://sentry.io> → sign in to the Mesio org.
2. Select the **python-fastapi** project.
3. Navigate to **Settings → Alerts**.
4. Click **Create Alert Rule**.
5. Choose **Issues** as the alert type → **Set Conditions**.
6. Condition: "An issue is first seen" (or "An issue occurs more than N times in Y minutes" for volume-based alerting).
7. Action: **Send a notification via Email** → add `miango950@gmail.com`.
8. Name the rule "CEO Error Alerts" and save.

To add a second rule for high-frequency errors (spike alert):
- Condition: "The issue is seen more than 5 times in 10 minutes"
- Same email action

## Verifying after deploy

After deploying with `SENTRY_DSN` set, confirm the SDK initialized by checking Railway logs for:

```
sentry.initialized  environment=production  component=web
sentry.initialized  environment=production  component=inbox_worker
```

Both lines must appear (one from each service). If only one appears, `SENTRY_DSN` is missing from the other service's env vars.

To trigger a real test event without code changes: in the Sentry UI go to
**Settings → Client Keys (DSN) → Send a test event**. That fires a synthetic error and
confirms the email routing works end-to-end.

## Expected alert volume

Steady state: fewer than 5 alerts/day on a healthy deployment. Signals to watch:

| Pattern | Likely cause |
|---------|-------------|
| Spike of `TenantNotSetError` | A new endpoint missing `tenant_scope` |
| Spike of `asyncpg` errors | DB connectivity / pool exhaustion |
| Spike of `httpx.HTTPStatusError` | MATIAS API down or DIAN outage |
| `billing.matias_auth_failed` breadcrumbs | `MATIAS_API_TOKEN` / `MATIAS_API_USER` wrong in Railway |

If volume exceeds ~20/day in steady state, increase the `SENTRY_TRACES_SAMPLE_RATE`
threshold or add an issue-frequency filter to the alert rule (e.g. "only alert if seen > 3 times").
