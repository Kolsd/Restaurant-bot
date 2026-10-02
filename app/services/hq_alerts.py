"""
Mesio HQ alert rules (wave 4).

`run_alert_rules()` evaluates every organization's health flags — the same
ones the ficha shows, with the same runbook (services/hq_snapshot.RUNBOOK) —
and keeps hq_alerts in step:

  * a warning/critical flag that appears   → alert opens
  * one that is still there                → last_seen refreshed
  * one that is gone                       → alert resolves on its own

Critical alerts that just opened are emailed to Mesio (HQ_ALERT_EMAIL,
default miguel@mesioai.com) in ONE digest per run, once per alert — never
every tick. A failed send is retried on the next run.

Runs from the scheduler every 5 minutes, inside its bypass_tenant_scope.
Cost: one snapshot per organization (a few queries per sede) — fine for
hundreds of orgs; batch it if it ever shows in the DB load.
"""
from __future__ import annotations

import html
import os

import asyncpg

from app.repositories.internal import hq_alerts_repo as repo
from app.services import hq_snapshot
from app.services.logging import get_logger

log = get_logger(__name__)

DEFAULT_ALERT_EMAIL = "miguel@mesioai.com"
ALERTING_SEVERITIES = ("critical", "warning")


def alert_email() -> str:
    return os.getenv("HQ_ALERT_EMAIL", DEFAULT_ALERT_EMAIL).strip() or DEFAULT_ALERT_EMAIL


def alert_key(code: str, org_id: int, sede_id: int | None) -> str:
    return f"{code}:{org_id}:{sede_id if sede_id else '-'}"


async def run_alert_rules() -> dict:
    opened, kept, failed_orgs = [], 0, []
    evaluated, seen = [], []
    for org_id in await repo.db_org_ids_to_watch():
        try:
            snap = await hq_snapshot.build_org_snapshot(org_id)
        except asyncpg.PostgresError as exc:
            log.warning("hq_alerts.org_eval_failed", org_id=org_id, error=type(exc).__name__)
            failed_orgs.append(org_id)
            continue
        if snap is None:
            continue
        evaluated.append(org_id)
        for flag in snap["flags"]:
            if flag["severity"] not in ALERTING_SEVERITIES:
                continue
            key = alert_key(flag["code"], org_id, flag.get("sede_id"))
            seen.append(key)
            row = await repo.db_upsert_open(
                key=key, code=flag["code"], severity=flag["severity"], org_id=org_id,
                location_id=flag.get("sede_id"), title=flag["title"],
                detail=f"{flag['where']} — {flag['fix']}", count=flag.get("count"),
            )
            if row["opened"]:
                opened.append({**flag, "id": row["id"], "org_id": org_id, "org_name": snap["org"]["name"],
                               "emailed_at": row["emailed_at"]})
            else:
                kept += 1
    resolved = await repo.db_resolve_missing(evaluated, seen)

    # Critical ones that still owe an email: just opened, or a previous send failed.
    pending = [a for a in await repo.db_list_alerts(status="open", org_id=None, limit=500)
               if a["severity"] == "critical" and a["emailed_at"] is None]
    emailed = 0
    if pending:
        from app.services.email import delivers_for_real  # noqa: PLC0415
        if not delivers_for_real():
            # Console backend: the email would only be logged. Keep them
            # pending (emailed_at NULL) so they go out once email is set up.
            log.warning("hq_alerts.email_not_configured", pending=len(pending))
        elif await _send_digest(pending):
            await repo.db_mark_emailed([a["id"] for a in pending])
            emailed = len(pending)

    summary = {"orgs": len(evaluated), "opened": len(opened), "kept": kept, "resolved": resolved,
               "emailed": emailed, "failed_orgs": failed_orgs}
    if opened or resolved or emailed:
        log.info("hq_alerts.run", **summary)
    return summary


def _ficha_url(org_id) -> str:
    domain = os.getenv("APP_DOMAIN", "mesioai.com").strip() or "mesioai.com"
    return f"https://{domain}/internal/org/{org_id}"


async def _send_digest(alerts: list[dict]) -> bool:
    from app.services.email import send_email  # noqa: PLC0415

    by_code = {code: (sev, title, where, fix) for code, (sev, title, where, fix) in hq_snapshot.RUNBOOK.items()}
    orgs = sorted({a.get("org_name") or f"Org #{a['org_id']}" for a in alerts})
    subject = f"[Mesio HQ] {len(alerts)} alerta{'s' if len(alerts) != 1 else ''} crítica{'s' if len(alerts) != 1 else ''}: {', '.join(orgs)[:120]}"
    items_html, items_text = [], []
    for a in alerts:
        _, title, where, fix = by_code.get(a["code"], ("", a["title"], "", ""))
        org = a.get("org_name") or f"Org #{a['org_id']}"
        sede = a.get("location_name")
        url = _ficha_url(a["org_id"])
        head = f"{org}{' · ' + sede if sede else ''}: {title}{' (' + str(a['count']) + ')' if a.get('count') else ''}"
        items_html.append(
            f"<li style='margin-bottom:14px'><b>{html.escape(head)}</b><br>"
            f"<span style='color:#555'>Dónde mirar:</span> {html.escape(where)}<br>"
            f"<span style='color:#555'>Cómo resolverlo:</span> {html.escape(fix)}<br>"
            f"<a href='{html.escape(url)}'>Abrir ficha</a></li>"
        )
        items_text.append(f"- {head}\n  Dónde mirar: {where}\n  Cómo resolverlo: {fix}\n  {url}")
    body_html = (
        "<div style='font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;color:#111'>"
        "<p>Estas alertas críticas acaban de abrirse en Mesio HQ:</p>"
        f"<ul style='padding-left:18px'>{''.join(items_html)}</ul>"
        "<p style='color:#777;font-size:12px'>Se cierran solas cuando el problema desaparece. "
        "Solo llega un correo cuando una alerta se abre.</p></div>"
    )
    body_text = "Alertas críticas en Mesio HQ:\n\n" + "\n\n".join(items_text)
    try:
        sent = await send_email(to=alert_email(), subject=subject, html=body_html, text=body_text)
    except (OSError, ValueError) as exc:
        log.warning("hq_alerts.email_failed", error=type(exc).__name__)
        return False
    if not sent:
        log.warning("hq_alerts.email_not_sent", count=len(alerts))
    return bool(sent)
