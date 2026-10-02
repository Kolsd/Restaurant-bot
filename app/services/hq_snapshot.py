"""
Mesio HQ — one organization's control-center snapshot.

`build_org_snapshot(org_id)` gathers business (plan, billing, MRR, LLM cost),
and per sede: operation, adoption, staff and HEALTH FLAGS. Every flag carries
what to look at and how to fix it, so whoever answers a restaurant's call
knows where to go (docs: docs/claude/internal-hq.md, "Ficha").

Must be called inside bypass_tenant_scope (cross-tenant, read-only).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.repositories.internal import hq_repo
from app.services import plans
from app.services.logging import get_logger

log = get_logger(__name__)

DEFAULT_TZ = "America/Bogota"

# code → (severity, title, where to look, how to fix). Severity: critical |
# warning | info. Wording is for the Mesio team, in Spanish like the HQ UI.
RUNBOOK: dict[str, tuple[str, str, str, str]] = {
    "suspended": (
        "critical", "La cuenta está suspendida: los comensales no pueden pedir",
        "Ficha › Negocio: estado de pago, fin del trial y último pago registrado.",
        "Si ya pagó: Superadmin › Registrar pago (+1 o +12 meses). Si no, contactar al dueño antes de que pierda clientes.",
    ),
    "paused": (
        "critical", "El dueño pausó el restaurante (no recibe pedidos)",
        "Configuración del restaurante › 'Pausar restaurante' (features.bot_active = false).",
        "Confirmar con el dueño si fue a propósito. Si no, pedirle que lo reactive en Configuración.",
    ),
    "overdue": (
        "warning", "Pago vencido: se suspende al terminar los días de gracia",
        "Ficha › Negocio: 'Pagado hasta' y 'Se pausa el'.",
        "Recordar el pago al dueño; al recibirlo, Superadmin › Registrar pago.",
    ),
    "trial_ending": (
        "warning", "El trial termina en 3 días o menos",
        "Ficha › Negocio: fin del trial, uso de las últimas semanas.",
        "Llamar al dueño: mostrar lo que usó, cerrar el plan y registrar el pago.",
    ),
    "stuck_rounds": (
        "critical", "Pedidos de mesa sin pasar a 'listo' hace más de 45 min",
        "Staff App › Cocina (y Bar) de la sede: ¿la pantalla está abierta? ¿la ronda fue a la estación correcta (Configurar operación › Bar)?",
        "Si la cocina no usa la pantalla, enseñarles a marcar 'listo'. Si la ronda está en la estación equivocada, revisar las categorías del bar en Configurar operación.",
    ),
    "stuck_orders": (
        "critical", "Domicilios / para recoger abiertos hace más de 90 min",
        "Staff App › Domicilios de la sede: estado de cada pedido y si alguien lo aceptó.",
        "Pedir a caja que acepte, despache o cancele con motivo. Si el cliente ya recibió, marcar entregado y pagado.",
    ),
    "waiting_acceptance": (
        "warning", "Pedidos web esperando que caja los acepte",
        "Staff App › Domicilios › Por aceptar.",
        "Avisar a la sede: un pedido sin aceptar no llega a cocina y el cliente queda esperando.",
    ),
    "sittings_over_6h": (
        "warning", "Mesas abiertas hace más de 6 horas",
        "Panel › Salón de la sede: mesas ocupadas sin movimiento.",
        "Casi siempre es una mesa que no se cerró al cobrar. La caja la cierra desde Salón o Caja.",
    ),
    "checks_open": (
        "warning", "Cuentas abiertas sin cobrar hace más de 3 horas",
        "Staff App › Caja de la sede.",
        "Cobrar o anular la cuenta. Si el cliente ya pagó, registrar el pago para que cuadre la venta.",
    ),
    "proofs_to_review": (
        "warning", "Comprobantes de transferencia sin revisar",
        "Staff App › Caja › Comprobantes.",
        "La caja revisa el comprobante y confirma el pago; mientras tanto la mesa queda 'esperando confirmación'.",
    ),
    "delivered_unpaid": (
        "info", "Domicilios entregados que no se marcaron pagados",
        "Staff App › Domicilios › Entregados.",
        "Marcar pagado (efectivo, tarjeta o transferencia); si no, esas ventas no salen en el dashboard.",
    ),
    "quiet_during_hours": (
        "critical", "Sede abierta sin pedidos hace 3 horas (normalmente vende)",
        "Ficha › sede: último pedido, último comensal. Probar el QR de una mesa y /pedir desde el celular.",
        "Llamar a la sede YA: ¿caída de internet, QR dañado, cerraron sin avisar, el personal dejó de usar Mesio? Si es la app, revisar Ficha › Errores.",
    ),
    "no_activity_7d": (
        "warning", "La sede no ha tenido pedidos en 7 días",
        "Ficha › sede: último pedido, último comensal, últimos inicios de sesión del staff.",
        "Llamar al dueño: ¿dejaron de usar Mesio en esta sede? ¿problema con los QR o con el personal? Riesgo de churn.",
    ),
    "no_tables": (
        "info", "La sede no tiene mesas creadas",
        "Panel › Mesas & QR de la sede.",
        "Guiar al dueño a crear las mesas e imprimir la hoja de QR.",
    ),
    "kitchen_slow": (
        "info", "La cocina tarda más de 30 min en el 90% de las rondas",
        "Ficha › sede: tiempo de cocina p50/p90 (7 días).",
        "Puede ser que marquen 'listo' tarde y no que cocinen lento. Revisar con el jefe de cocina cómo usan la pantalla.",
    ),
    "bot_failing": (
        "critical", "El bot está fallando: los comensales reciben 'tengo un problema técnico'",
        "Ficha › Errores: fuente 'bot', tipo y mensaje (crédito de Anthropic, timeouts, límite de uso).",
        "Si es crédito o API key: recargar/rotar en Anthropic y Railway. Si es un error de código: abrir el error, reproducir con su request id en los logs de Railway y corregir.",
    ),
    "errors_repeated": (
        "warning", "Errores del servidor repetidos en las últimas 24 h",
        "Ficha › Errores: ruta, tipo y request id; buscar ese id en los logs de Railway para ver el traceback.",
        "Reproducir en local con la misma ruta; si bloquea al restaurante, avisarle mientras se corrige.",
    ),
    "inventory_low": (
        "info", "Insumos por debajo del mínimo",
        "Panel › Inventario de la sede.",
        "Avisar al dueño; si no cocinan, se agotan platos en la carta.",
    ),
    "unhappy_guests": (
        "warning", "NPS negativo en los últimos 30 días",
        "Panel › NPS de la sede: comentarios de los detractores.",
        "Compartir los comentarios con el dueño; es lo primero que mira antes de cancelar.",
    ),
}


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def local_day_start_utc(tz_name: str | None, now: datetime | None = None) -> datetime:
    """Midnight of the sede's local today, as naive UTC (column shape)."""
    tz = _tz(tz_name)
    now = now or datetime.now(timezone.utc)
    local_midnight = datetime.combine(now.astimezone(tz).date(), time.min, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def _num(v) -> float:
    """JSON boundary: numeric DB values (Decimal) to float."""
    if v is None:
        return 0.0
    return float(v) if isinstance(v, (Decimal, int, float)) else 0.0


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:  # naive columns hold UTC
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _json(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v:
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except ValueError:
            return {}
    return {}


def _flag(code: str, count: int | None = None) -> dict:
    severity, title, where, fix = RUNBOOK[code]
    return {"code": code, "severity": severity, "title": title, "count": count, "where": where, "fix": fix}


_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _parse_hhmm(v) -> time | None:
    try:
        h, mi = str(v).split(":")[:2]
        return time(int(h), int(mi))
    except (ValueError, TypeError):
        return None


def open_long_enough(opening_hours, tz_name: str | None, now: datetime | None = None,
                     hours: int = 3) -> bool:
    """True when the sede's posted hours say it has been open at least
    `hours` today (and is still open). Unknown/absent hours → False: the
    rule never fires on a guess."""
    hours_cfg = _json(opening_hours)
    tz = _tz(tz_name)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    day = hours_cfg.get(_DAYS[local.weekday()]) or {}
    if not isinstance(day, dict) or day.get("closed"):
        return False
    start, end = _parse_hhmm(day.get("open")), _parse_hhmm(day.get("close"))
    if not start or not end:
        return False
    opened = datetime.combine(local.date(), start, tzinfo=tz)
    closes = datetime.combine(local.date(), end, tzinfo=tz)
    if closes <= opened:  # closes after midnight
        closes += timedelta(days=1)
    return opened + timedelta(hours=hours) <= local < closes


def _sede_flags(m: dict, active: bool, loc: dict | None = None) -> list[dict]:
    if not active:
        return []
    t, w, fl, ck, nps, mod = m["table"], m["web"], m["floor"], m["checks"], m["nps"], m["modules"]
    flags = []
    if t["stuck_rounds"]:
        flags.append(_flag("stuck_rounds", t["stuck_rounds"]))
    if w["stuck_orders"]:
        flags.append(_flag("stuck_orders", w["stuck_orders"]))
    if w["waiting_acceptance"]:
        flags.append(_flag("waiting_acceptance", w["waiting_acceptance"]))
    if fl["sittings_over_6h"]:
        flags.append(_flag("sittings_over_6h", fl["sittings_over_6h"]))
    if ck["checks_open_over_3h"]:
        flags.append(_flag("checks_open", ck["checks_open_over_3h"]))
    if ck["proofs_to_review"]:
        flags.append(_flag("proofs_to_review", ck["proofs_to_review"]))
    if w["delivered_unpaid"]:
        flags.append(_flag("delivered_unpaid", w["delivered_unpaid"]))
    if not fl["tables"]:
        flags.append(_flag("no_tables"))
    orders_7d = (t["rounds_7d"] or 0) + (w.get("orders_7d") or 0)
    last = max([x for x in (t["last_round_at"], w["last_order_at"]) if x], default=None)
    if (loc and orders_7d >= 10 and open_long_enough(loc.get("opening_hours"), loc.get("timezone"))
            and (last is None or last < datetime.utcnow() - timedelta(hours=3))):  # noqa: DTZ003 — naive UTC columns
        flags.append(_flag("quiet_during_hours"))
    # A sede opened this week has had no chance to sell for 7 days: flagging
    # it put every new signup in the HQ queue as a churn risk on day one.
    created = loc.get("created_at") if loc else None
    if created is not None and created.tzinfo is not None:
        created = created.astimezone(timezone.utc).replace(tzinfo=None)
    old_enough = created is None or created <= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    if (old_enough and not t["rounds_7d"]
            and not (w["last_order_at"] and w["last_order_at"] > datetime.utcnow() - timedelta(days=7))):  # noqa: DTZ003 — naive UTC column
        flags.append(_flag("no_activity_7d"))
    if t["kitchen_samples"] >= 5 and _num(t["kitchen_p90_min"]) > 30:
        flags.append(_flag("kitchen_slow"))
    if mod["inventory_low"]:
        flags.append(_flag("inventory_low", mod["inventory_low"]))
    if nps["responses"] >= 3 and nps["detractors"] > nps["promoters"]:
        flags.append(_flag("unhappy_guests", nps["detractors"]))
    return sorted(flags, key=lambda f: _SEVERITY_ORDER[f["severity"]])


def _nps_score(n: dict) -> int | None:
    return round(100 * (n["promoters"] - n["detractors"]) / n["responses"]) if n["responses"] else None


def _sede_view(loc: dict, m: dict) -> dict:
    t, w, fl, ck, d, nps, mod = m["table"], m["web"], m["floor"], m["checks"], m["diners"], m["nps"], m["modules"]
    sales_30d = _num(t["sales_30d"]) + _num(w["sales_30d"])
    tickets_30d = (t["bills_30d"] or 0) + (w["orders_30d"] or 0)
    last = [x for x in (t["last_round_at"], w["last_order_at"]) if x]
    ops = _json(loc.get("ops_config"))
    delivery_cfg = _json(loc.get("delivery_config"))
    return {
        "id": loc["id"],
        "name": loc["name"],
        "active": bool(loc["active"]),
        "timezone": loc.get("timezone") or DEFAULT_TZ,
        "address": loc.get("address") or "",
        "phone": loc.get("phone") or "",
        "flags": _sede_flags(m, bool(loc["active"]), loc),
        "operation": {
            "orders_today": (t["rounds_today"] or 0) + (w["orders_today"] or 0),
            "sales_today": _num(t["sales_today"]) + _num(w["sales_today"]),       # JSON boundary
            "sales_7d": _num(t["sales_7d"]) + _num(w["sales_7d"]),
            "sales_30d": sales_30d,
            "avg_ticket_30d": round(sales_30d / tickets_30d) if tickets_30d else 0,
            "table_rounds_7d": t["rounds_7d"] or 0,
            "table_rounds_30d": t["rounds_30d"] or 0,
            "web_orders_30d": w["orders_30d"] or 0,
            "delivery_30d": w["delivery_30d"] or 0,
            "pickup_30d": w["pickup_30d"] or 0,
            "tables": fl["tables"] or 0,
            "tables_open": fl["tables_open"] or 0,
            "open_waiter_alerts": fl["open_waiter_alerts"] or 0,
            "kitchen_p50_min": round(_num(t["kitchen_p50_min"]), 1) if t["kitchen_samples"] else None,
            "kitchen_p90_min": round(_num(t["kitchen_p90_min"]), 1) if t["kitchen_samples"] else None,
            "kitchen_samples_7d": t["kitchen_samples"] or 0,
            "last_order_at": _iso(max(last)) if last else None,
        },
        "adoption": {
            "diner_sessions_7d": d["sessions_7d"] or 0,
            "table_diner_sessions_7d": d["table_sessions_7d"] or 0,
            "remembered_diners_7d": d["remembered_7d"] or 0,
            "chat_conversations_7d": m["chat"]["conversations_7d"] or 0,
            "last_diner_at": _iso(d["last_diner_at"]),
            "reservations_30d": mod["reservations_30d"] or 0,
            "inventory_items": mod["inventory_items"] or 0,
            "ops_configured": bool(ops.get("configured")),
            "ops": {k: ops.get(k) for k in ("bar", "delivery", "courier", "waiter") if k in ops},
            "delivery_enabled": bool(delivery_cfg.get("delivery_enabled")),
            "pickup_enabled": bool(delivery_cfg.get("pickup_enabled")),
            "payment_methods": delivery_cfg.get("payment_methods") or [],
        },
        "nps": {
            "responses_30d": nps["responses"] or 0,
            "score_30d": _nps_score(nps),
            "detractor_comments_30d": nps["detractor_comments"] or 0,
        },
        "staff": [
            {
                "id": str(s["id"]), "name": s["name"], "username": s["username"], "active": bool(s["active"]),
                "roles": s["roles"] or s["role"], "last_login": _iso(s["last_login"]),
            }
            for s in m["staff"]
        ],
    }


async def build_org_snapshot(org_id: int, *, llm_cost: dict | None = None) -> dict | None:
    base = await hq_repo.db_hq_org(org_id)
    if base is None:
        return None
    org = base["org"]
    features = _json(org.get("features"))
    status = plans.account_status(dict(org))
    active_sedes = [loc for loc in base["locations"] if loc["active"]]
    price = plans.monthly_price_per_sede(org["plan_code"], org["founder_price_cop"])
    billed = org["plan_code"] in plans.PAYING_PLANS and status in (plans.ACTIVE, plans.OVERDUE)

    org_flags = []
    if status == plans.SUSPENDED:
        org_flags.append(_flag("suspended"))
    elif status == plans.OVERDUE:
        org_flags.append(_flag("overdue"))
    if features.get("bot_active") is False:
        org_flags.append(_flag("paused"))
    comp = org["comp_until"]
    if status == plans.TRIAL and comp and comp - datetime.now(timezone.utc) <= timedelta(days=3):
        org_flags.append(_flag("trial_ending"))

    from app.repositories.internal import errors_repo  # noqa: PLC0415
    error_groups = await errors_repo.db_error_groups(org_id=org_id, days=7)
    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = [g for g in error_groups if g["last_at"] and g["last_at"] >= day_ago]
    bot_24h = sum(g["count"] for g in recent if g["source"] == "bot")
    http_24h = sum(g["count"] for g in recent if g["source"] != "bot")
    if bot_24h >= 3:
        org_flags.append(_flag("bot_failing", bot_24h))
    if http_24h >= 5:
        org_flags.append(_flag("errors_repeated", http_24h))

    sedes = []
    for loc in base["locations"]:
        metrics = await hq_repo.db_hq_location_metrics(
            org_id, int(loc["id"]), local_day_start_utc(loc.get("timezone")),
        )
        sedes.append(_sede_view(loc, metrics))

    owners = [u for u in base["users"] if "owner" in (u["role"] or "")]
    logins = [u["last_login"] for u in base["users"] if u["last_login"]]
    cost_cop = _num(((llm_cost or {}).get("totals") or {}).get("cost_cop"))
    mrr = price * max(1, len(active_sedes)) if billed else 0
    all_flags = org_flags + [dict(f, sede=s["name"], sede_id=s["id"]) for s in sedes for f in s["flags"]]
    return {
        "org": {
            "id": org["id"],
            "name": org["name"],
            "slug": org["slug"] or "",
            "order_link": f"/pedir/{org['slug']}" if org["slug"] else None,
            "created_at": _iso(org["created_at"]),
            "timezone": features.get("timezone") or DEFAULT_TZ,
        },
        "business": {
            "plan_code": plans.normalize_plan(org["plan_code"]),
            "plan_name": plans.PLAN_NAMES.get(plans.normalize_plan(org["plan_code"]), org["plan_code"]),
            "billing_status": status,
            "comp_until": _iso(org["comp_until"]),
            "paid_until": _iso(org["paid_until"]),
            "pauses_on": _iso(plans.pauses_on(org["paid_until"])),
            "founder_price_cop": org["founder_price_cop"],
            "price_per_sede_cop": price,
            "active_sedes": len(active_sedes),
            "mrr_cop": mrr,
            "paused_by_owner": features.get("bot_active") is False,
            "llm_cost_30d_cop": round(cost_cop),
            "llm_tokens_30d": int(base["usage"].get("tokens_30d") or 0),
            "margin_30d_cop": round(mrr - cost_cop) if mrr else None,
        },
        "people": {
            "owners": [u["username"] for u in owners],
            "users": [
                {"username": u["username"], "role": u["role"], "name": u["display_name"] or "",
                 "location_id": u["location_id"], "last_login": _iso(u["last_login"])}
                for u in base["users"]
            ],
            "last_login": _iso(max(logins)) if logins else None,
        },
        "flags": sorted(all_flags, key=lambda f: _SEVERITY_ORDER[f["severity"]]),
        "errors": [
            {
                "source": g["source"], "error_type": g["error_type"], "message": g["message"] or "",
                "route": g["route"] or "", "method": g["method"], "status": g["status"],
                "count": g["count"], "first_at": _iso(g["first_at"]), "last_at": _iso(g["last_at"]),
                "location_id": g["location_id"], "request_id": g["last_request_id"],
            }
            for g in error_groups
        ],
        "sedes": sedes,
    }
