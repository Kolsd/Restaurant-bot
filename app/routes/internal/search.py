"""
app/routes/internal/search.py

Global search endpoint for Mesio HQ Cmd+K palette.
Returns up to 20 results across tenants, prospects, and hardcoded quick actions.

Endpoint:
  GET /api/internal/search?q=<query>   — requires superadmin session
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query
from app.routes.deps import verify_superadmin
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope
from app.services.database import get_pool

log = get_logger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal-search"])

# ── Quick actions (always shown, filtered by query) ─────────────────────────
_QUICK_ACTIONS = [
    {
        "type": "action",
        "title": "Crear nuevo cliente",
        "subtitle": "Nuevo tenant en 30s",
        "url": "/internal/superadmin?action=new",
        "keywords": ["crear", "nuevo", "cliente", "tenant", "restaurante", "new", "create"],
    },
    {
        "type": "action",
        "title": "Ver pipeline CRM",
        "subtitle": "Prospectos y seguimiento",
        "url": "/internal/crm",
        "keywords": ["crm", "pipeline", "prospecto", "ventas", "lead"],
    },
    {
        "type": "action",
        "title": "Ver MRR",
        "subtitle": "Ingresos recurrentes mensuales",
        "url": "/internal/analytics#mrr",
        "keywords": ["mrr", "analytics", "ingresos", "revenue", "facturación", "billing"],
    },
    {
        "type": "action",
        "title": "Ver alertas",
        "subtitle": "Sentry + cost runaway + churn",
        "url": "/internal/monitoring",
        "keywords": ["alerta", "alert", "monitoring", "error", "sentry", "churn"],
    },
    {
        "type": "action",
        "title": "Ver costos",
        "subtitle": "Consumo LLM y costos por tenant",
        "url": "/internal/costs",
        "keywords": ["costo", "cost", "llm", "consumo", "gasto", "token"],
    },
]


def _relative_time(dt: datetime | None) -> str:
    """Return a human-readable relative time string (e.g. 'hace 2h')."""
    if dt is None:
        return "nunca"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return "hace un momento"
    if total_seconds < 3600:
        m = total_seconds // 60
        return f"hace {m}m"
    if total_seconds < 86400:
        h = total_seconds // 3600
        return f"hace {h}h"
    d = total_seconds // 86400
    return f"hace {d}d"


def _filter_actions(q: str) -> list[dict]:
    """Return actions that match the query (case-insensitive substring on title+keywords)."""
    if not q:
        return [{"type": a["type"], "title": a["title"], "subtitle": a["subtitle"], "url": a["url"]}
                for a in _QUICK_ACTIONS]
    q_lower = q.lower()
    out = []
    for a in _QUICK_ACTIONS:
        title_match = q_lower in a["title"].lower()
        keyword_match = any(q_lower in kw for kw in a["keywords"])
        if title_match or keyword_match:
            out.append({
                "type": a["type"],
                "title": a["title"],
                "subtitle": a["subtitle"],
                "url": a["url"],
            })
    return out


@router.get("/search")
async def hq_search(
    q: str = Query(default="", max_length=100),
    _: None = Depends(verify_superadmin),
) -> dict:
    """
    Global Cmd+K search for Mesio HQ.

    Returns up to 20 results:
      - Quick actions (always, filtered by query)
      - Tenant orgs (if q >= 2 chars)
      - CRM prospects (if q >= 2 chars)
    """
    results: list[dict] = []

    q = q.strip()
    actions = _filter_actions(q)

    if len(q) < 2:
        return {"results": actions}

    # Cross-tenant queries — superadmin sees all
    with bypass_tenant_scope("internal_search_cross_tenant"):
        pool = await get_pool()
        async with pool.acquire() as conn:
            pattern = f"%{q}%"

            # ── Tenant search ────────────────────────────────────────────────
            tenant_rows = await conn.fetch(
                """
                SELECT
                    o.id,
                    o.name,
                    o.plan_code,
                    o.whatsapp_number,
                    (
                        SELECT MAX(ord.created_at)
                        FROM orders ord
                        INNER JOIN locations loc ON loc.org_id = o.id
                        WHERE ord.org_id = o.id
                    ) AS last_order_at
                FROM organizations o
                WHERE o.name ILIKE $1 OR o.whatsapp_number LIKE $1
                ORDER BY o.name
                LIMIT 10
                """,
                pattern,
            )

            for row in tenant_rows:
                last_order = row["last_order_at"]
                if last_order is not None:
                    activity = f"activo {_relative_time(last_order)}"
                else:
                    activity = "sin actividad"

                plan = (row["plan_code"] or "sin plan").capitalize()
                wa = row["whatsapp_number"] or ""
                subtitle_parts = [plan]
                if wa:
                    subtitle_parts.append(wa)
                subtitle_parts.append(activity)
                subtitle = " · ".join(subtitle_parts)

                results.append({
                    "type": "tenant",
                    "id": row["id"],
                    "title": row["name"] or f"Org #{row['id']}",
                    "subtitle": subtitle,
                    "url": f"/internal/superadmin#org={row['id']}",
                })

            # ── Prospect search ──────────────────────────────────────────────
            prospect_rows = await conn.fetch(
                """
                SELECT
                    id,
                    owner_name,
                    phone,
                    restaurant_name,
                    stage,
                    city,
                    last_contact_at
                FROM prospects
                WHERE
                    archived = FALSE
                    AND (owner_name ILIKE $1 OR restaurant_name ILIKE $1 OR phone LIKE $1)
                ORDER BY last_contact_at DESC NULLS LAST
                LIMIT 10
                """,
                pattern,
            )

            for row in prospect_rows:
                name = row["owner_name"] or row["restaurant_name"] or f"Prospecto #{row['id']}"
                stage = (row["stage"] or "nuevo").replace("_", " ").capitalize()
                city = row["city"] or ""
                last_contact = _relative_time(row["last_contact_at"])
                subtitle_parts = [stage]
                if city:
                    subtitle_parts.append(city)
                subtitle_parts.append(f"contactado {last_contact}")
                subtitle = " · ".join(subtitle_parts)

                results.append({
                    "type": "prospect",
                    "id": row["id"],
                    "title": name,
                    "subtitle": subtitle,
                    "url": f"/internal/crm#prospect={row['id']}",
                })

    # Combine: actions first, then db results (capped at 20 total)
    combined = actions + results
    return {"results": combined[:20]}
