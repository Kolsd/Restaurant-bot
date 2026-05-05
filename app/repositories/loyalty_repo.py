"""
app/repositories/loyalty_repo.py

Repository for loyalty/fidelización functions.
Extracted from database.py — Fase 6 Repository Pattern.
Migrated to tenant_connection() — RLS pilot Step 2.
"""
import json
from decimal import Decimal
from typing import Union

from app.services.logging import get_logger
from app.services.money import to_decimal, quantize_money, ZERO
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize
    return _serialize(d)


def _normalize_phone(number: str) -> str:
    if not number: return ""
    return number.replace(" ", "").replace("+", "")


async def _ensure_loyalty_tables() -> None:
    """No-op: loyalty_customers and loyalty_ledger managed by Alembic (0020_missing_runtime_tables.py)."""
    pass


async def _loyalty_cfg(conn, restaurant_id: int) -> dict:
    """
    Lee loyalty_points_per_1k y loyalty_point_value_cop de restaurants.features.
    Devuelve defaults seguros si no están configurados.
    """
    row = await conn.fetchrow(
        "SELECT features FROM restaurants WHERE id=$1", restaurant_id
    )
    feats = (row["features"] or {}) if row else {}
    if isinstance(feats, str):
        try:
            feats = json.loads(feats)
        except Exception:
            feats = {}
    return {
        "points_per_1k":   max(1, int(feats.get("loyalty_points_per_1k", 1))),
        "point_value_cop": max(1, int(feats.get("loyalty_point_value_cop", 10))),
    }


async def db_get_loyalty_balance(restaurant_id: int, phone: str) -> dict | None:
    """
    Consulta O(1) del saldo. El bot la consume como herramienta ultra-ligera.
    Retorna {"puntos_actuales": N, "equivalencia_cop": N*point_value} o None si
    el cliente no tiene registro de fidelización.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_loyalty_tables()
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
            restaurant_id, _normalize_phone(phone),
        )
        if not row:
            return None
        cfg = await _loyalty_cfg(conn, restaurant_id)
        pts = row["points_balance"]
    return {"puntos_actuales": pts, "equivalencia_cop": pts * cfg["point_value_cop"]}


async def db_accrue_loyalty_points(
    restaurant_id: int,
    phone: str,
    order_id: str,
    total_cop: Union[Decimal, int, float, str],
) -> int:
    """
    Calcula y acumula puntos por una compra pagada. Idempotente a nivel DB:
    UNIQUE INDEX parcial on (org_id, order_id) WHERE delta > 0 (migración 0055)
    + ON CONFLICT DO NOTHING + RETURNING. Si una segunda llamada concurrente
    con el mismo order_id pierde la carrera, fetchrow() retorna None y la
    función retorna 0 sin tocar loyalty_customers (el contador solo refleja
    el primer accrual).

    Retorna los puntos acumulados, o 0 si ya estaba procesado.

    Type contract:
      total_cop accepts Decimal/int/float/str — coerced internally via
      to_decimal. The previous `: float` typing violated the no-float-money
      rule (CLAUDE.md). float entry is preserved for backward compat with
      legacy callers but the internal math is all Decimal.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_loyalty_tables()
    clean_phone = _normalize_phone(phone)
    total_d = to_decimal(total_cop)
    async with tenant_connection() as conn:
        cfg = await _loyalty_cfg(conn, restaurant_id)
        # int(total_d / 1000) is exact for Decimal — Decimal / Decimal returns
        # Decimal, no float drift. Equivalent to floor(total/1000) when total >= 0.
        points = max(1, int(total_d / Decimal(1000)) * cfg["points_per_1k"])

        # Race-safe ledger insert. RETURNING id tells us whether the row was
        # actually inserted (None = a concurrent caller won the race for this
        # order_id and we MUST NOT bump the customer counter).
        inserted = await conn.fetchrow(
            """INSERT INTO loyalty_ledger (org_id, phone, delta, reason, order_id)
               VALUES ($1, $2, $3, 'purchase', $4)
               ON CONFLICT (org_id, order_id) WHERE delta > 0 AND order_id IS NOT NULL
               DO NOTHING
               RETURNING id""",
            restaurant_id, clean_phone, points, order_id,
        )
        if inserted is None:
            log.info(
                "loyalty.accrual_idempotent_skip",
                org_id=restaurant_id, order_id=order_id,
            )
            return 0

        await conn.execute(
            """INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned)
               VALUES ($1, $2, $3, $3)
               ON CONFLICT (org_id, phone) DO UPDATE
               SET points_balance = loyalty_customers.points_balance + $3,
                   total_earned   = loyalty_customers.total_earned   + $3,
                   updated_at     = NOW()""",
            restaurant_id, clean_phone, points,
        )
    return points


async def db_redeem_loyalty_points(
    restaurant_id: int,
    phone: str,
    points: int,
    order_id: str,
) -> dict:
    """
    Canjea puntos contra una compra. Bloquea la fila con FOR UPDATE para
    evitar race conditions en entornos multi-worker.
    Retorna {"redeemed": N, "cop_discount": N*point_value, "new_balance": M}.
    Lanza ValueError si el saldo es insuficiente.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_loyalty_tables()
    if points <= 0:
        raise ValueError("Los puntos a canjear deben ser positivos")
    clean_phone = _normalize_phone(phone)
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT points_balance FROM loyalty_customers "
            "WHERE org_id=$1 AND phone=$2 FOR UPDATE",
            restaurant_id, clean_phone,
        )
        current = row["points_balance"] if row else 0
        if current < points:
            raise ValueError(
                f"Saldo insuficiente: {current} puntos disponibles, "
                f"se intentaron canjear {points}"
            )
        await conn.execute(
            """INSERT INTO loyalty_ledger (org_id, phone, delta, reason, order_id)
               VALUES ($1, $2, $3, 'redeem', $4)""",
            restaurant_id, clean_phone, -points, order_id,
        )
        new_balance = await conn.fetchval(
            """UPDATE loyalty_customers
               SET points_balance = points_balance  - $3,
                   total_redeemed = total_redeemed  + $3,
                   updated_at     = NOW()
               WHERE org_id=$1 AND phone=$2
               RETURNING points_balance""",
            restaurant_id, clean_phone, points,
        )
        cfg = await _loyalty_cfg(conn, restaurant_id)
    return {
        "redeemed":     points,
        "cop_discount": points * cfg["point_value_cop"],
        "new_balance":  new_balance,
    }


async def db_apply_redemption_to_order(
    order_id: str,
    points: int,
    cop_discount: Union[Decimal, int, float, str],
) -> bool:
    """
    Persist a loyalty redemption on a delivery/pickup order so caja sees the
    discount when validating the receipt and the customer is not charged the
    full amount.

    `points` is an integer (the qty redeemed); `cop_discount` is the COP
    equivalent — coerced to Decimal end-to-end (NUMERIC(14,2) on disk).

    db_redeem_loyalty_points already wrote the ledger + decremented the
    balance. This function ONLY annotates the order row so the till and the
    customer-facing card can render the discount line.

    Returns True if a row was updated, False if the order_id was not found
    (RLS-scoped — the row may exist for another tenant but is invisible).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    if points <= 0:
        raise ValueError("points must be positive")
    cop_d = quantize_money(to_decimal(cop_discount))
    if cop_d < ZERO:
        raise ValueError("cop_discount cannot be negative")
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE orders
                  SET loyalty_redeemed_points = $2,
                      loyalty_discount_cop    = $3
                WHERE id = $1
            RETURNING id""",
            order_id, int(points), cop_d,
        )
    return row is not None


async def db_apply_redemption_to_table_check(
    check_id: str,
    points: int,
    cop_discount: Union[Decimal, int, float, str],
) -> bool:
    """
    Equivalent of db_apply_redemption_to_order but for dine-in table_checks.

    The bot calls this AFTER db_redeem_loyalty_points succeeded so caja sees
    the discount when cobrando the cuenta. We do NOT mutate `total` here —
    caja's UI subtracts the displayed discount when computing what the
    customer actually owes; the original total is preserved for audit / DIAN.

    Returns True if updated, False if the check was not found / not visible
    under the current tenant scope.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    if points <= 0:
        raise ValueError("points must be positive")
    cop_d = quantize_money(to_decimal(cop_discount))
    if cop_d < ZERO:
        raise ValueError("cop_discount cannot be negative")
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE table_checks
                  SET loyalty_redeemed_points = $2,
                      loyalty_discount_cop    = $3
                WHERE id = $1
            RETURNING id""",
            check_id, int(points), cop_d,
        )
    return row is not None


async def db_adjust_loyalty_points(
    restaurant_id: int,
    phone: str,
    delta: int,
    reason: str,
) -> dict:
    """
    Ajuste manual (admin). delta puede ser positivo o negativo.
    No permite dejar el saldo en negativo.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_loyalty_tables()
    clean_phone = _normalize_phone(phone)
    async with tenant_connection() as conn:
        if delta < 0:
            row = await conn.fetchrow(
                "SELECT points_balance FROM loyalty_customers "
                "WHERE org_id=$1 AND phone=$2 FOR UPDATE",
                restaurant_id, clean_phone,
            )
            current = row["points_balance"] if row else 0
            if current + delta < 0:
                raise ValueError(
                    f"El ajuste dejaría el saldo negativo "
                    f"({current} + {delta} = {current + delta})"
                )
        await conn.execute(
            """INSERT INTO loyalty_ledger (org_id, phone, delta, reason)
               VALUES ($1, $2, $3, $4)""",
            restaurant_id, clean_phone, delta, reason[:100],
        )
        new_balance = await conn.fetchval(
            """INSERT INTO loyalty_customers
                   (org_id, phone, points_balance, total_earned, total_redeemed)
               VALUES ($1, $2, GREATEST(0, $3), GREATEST(0, $3), 0)
               ON CONFLICT (org_id, phone) DO UPDATE
               SET points_balance = GREATEST(0, loyalty_customers.points_balance + $3),
                   total_earned   = CASE WHEN $3 > 0
                                    THEN loyalty_customers.total_earned + $3
                                    ELSE loyalty_customers.total_earned END,
                   total_redeemed = CASE WHEN $3 < 0
                                    THEN loyalty_customers.total_redeemed + (-$3)
                                    ELSE loyalty_customers.total_redeemed END,
                   updated_at     = NOW()
               RETURNING points_balance""",
            restaurant_id, clean_phone, delta,
        )
    return {"new_balance": new_balance}


async def db_get_loyalty_ledger(
    restaurant_id: int,
    phone: str,
    limit: int = 50,
) -> list[dict]:
    """Historial de movimientos de un cliente (para dashboard / POS).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_loyalty_tables()
    limit = min(limit, 200)
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT id, delta, reason, order_id, created_at
               FROM loyalty_ledger
               WHERE org_id=$1 AND phone=$2
               ORDER BY created_at DESC
               LIMIT $3""",
            restaurant_id, _normalize_phone(phone), limit,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_loyalty_stats(restaurant_id: int, limit: int = 100) -> list[dict]:
    """Top clientes ordenados por saldo (para dashboard de fidelización).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_loyalty_tables()
    limit = min(limit, 500)
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT phone, points_balance, total_earned, total_redeemed, updated_at
               FROM loyalty_customers
               WHERE org_id=$1
               ORDER BY points_balance DESC
               LIMIT $2""",
            restaurant_id, limit,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_loyalty_customers(
    org_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List loyalty members ordered by most recent activity.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_loyalty_tables()
    limit  = min(max(1, limit),  200)
    offset = max(0, offset)
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT phone, points_balance, total_earned, total_redeemed,
                      updated_at AS last_activity, created_at
               FROM loyalty_customers
               WHERE org_id = $1
               ORDER BY updated_at DESC NULLS LAST, total_earned DESC
               LIMIT $2 OFFSET $3""",
            org_id, limit, offset,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_count_loyalty_customers(org_id: int) -> int:
    """Return total loyalty customer count for an org.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_loyalty_tables()
    async with tenant_connection() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM loyalty_customers WHERE org_id = $1",
            org_id,
        )
    return int(total or 0)


async def db_get_loyalty_aggregates(org_id: int) -> dict:
    """
    Compute headline aggregate metrics for the loyalty dashboard.

    Metrics:
      total_customers       — COUNT(*) of loyalty_customers for this org.
      avg_frequency         — average purchase ledger entries per member in
                              last 30 days (each 'purchase' entry = one order).
      avg_ticket_member     — AVG(orders.total) for orders from loyalty members.
      avg_ticket_nonmember  — AVG(orders.total) for orders from non-members.
      roi_multiple          — null for now (no campaign spend tracking yet).
                              # TODO: calculate once loyalty_campaigns.revenue_numeric
                              #        and a corresponding campaign_spend column are used.
      redemption_pct        — SUM(abs(delta)) WHERE delta<0 / SUM(delta) WHERE delta>0
                              × 100.  Returns 0.0 when no accruals exist.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        total_customers = await conn.fetchval(
            "SELECT COUNT(*) FROM loyalty_customers WHERE org_id = $1",
            org_id,
        ) or 0

        # avg_frequency + redemption_pct both scan loyalty_ledger for this org —
        # batched into one query to save 1 round-trip.
        # avg_frequency uses a 30-day window (FILTER) while redemption_pct is
        # all-time; FILTER aggregate handles this cleanly in one pass.
        ledger_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE reason = 'purchase'
                      AND created_at >= NOW() - INTERVAL '30 days'
                )                                                          AS freq_count,
                COUNT(DISTINCT phone) FILTER (
                    WHERE reason = 'purchase'
                      AND created_at >= NOW() - INTERVAL '30 days'
                )                                                          AS freq_members,
                COALESCE(SUM(CASE WHEN delta > 0 THEN  delta ELSE 0 END), 0) AS accrued,
                COALESCE(SUM(CASE WHEN delta < 0 THEN -delta ELSE 0 END), 0) AS redeemed
            FROM loyalty_ledger
            WHERE org_id = $1
            """,
            org_id,
        )
        if ledger_row and int(ledger_row["freq_members"] or 0) > 0:
            avg_frequency = float(
                round(int(ledger_row["freq_count"]) / int(ledger_row["freq_members"]), 1)
            )
        else:
            avg_frequency = 0.0
        accrued  = int(ledger_row["accrued"])  if ledger_row else 0
        redeemed = int(ledger_row["redeemed"]) if ledger_row else 0
        redemption_pct = round(redeemed / accrued * 100, 1) if accrued > 0 else 0.0

        # avg ticket split: member vs non-member.
        # Member phones are those in loyalty_customers for this org.
        ticket_row = await conn.fetchrow(
            """
            SELECT
                AVG(CASE WHEN lc.phone IS NOT NULL THEN o.total END)
                    AS member_avg,
                AVG(CASE WHEN lc.phone IS NULL THEN o.total END)
                    AS nonmember_avg
            FROM orders o
            LEFT JOIN loyalty_customers lc
                   ON lc.org_id = o.org_id
                  AND lc.phone  = o.phone
            WHERE o.org_id = $1
              AND o.total IS NOT NULL
              AND o.status NOT IN ('cancelled', 'pendiente')
            """,
            org_id,
        )
        avg_ticket_member = (
            float(ticket_row["member_avg"])
            if ticket_row and ticket_row["member_avg"] is not None
            else None
        )
        avg_ticket_nonmember = (
            float(ticket_row["nonmember_avg"])
            if ticket_row and ticket_row["nonmember_avg"] is not None
            else None
        )

    return {
        "total_customers":      int(total_customers),
        "avg_frequency":        avg_frequency,
        "avg_ticket_member":    avg_ticket_member,      # float or None
        "avg_ticket_nonmember": avg_ticket_nonmember,   # float or None
        "roi_multiple":         None,                   # TODO: campaign spend tracking
        "redemption_pct":       redemption_pct,
    }


async def db_get_loyalty_segments(org_id: int) -> list[dict]:
    """
    Compute the 4 IA-curated customer segments for the dashboard.

    Segment definitions:
      vip-active    — members with ≥4 purchase ledger entries in last 30d AND
                      cumulative order spend ≥ 200 000 COP in the same window.
                      potential_revenue = count × avg_ticket × 4 visits/month.
      vip-dormant   — was vip-active in a 30d window ending >60d ago, but has
                      zero orders in the last 30d.
                      potential_revenue = count × avg_ticket × 2 (recovery).
      new-month     — first-ever purchase entry within last 30d.
                      potential_revenue = null (unknown lifetime value).
      birthdays     — count=0 placeholder.
                      # TODO: ADD COLUMN birthday_month SMALLINT NULL to
                      #        loyalty_customers, then query:
                      #        WHERE org_id=$1 AND birthday_month =
                      #              EXTRACT(MONTH FROM NOW())::int

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        # ── VIP active ─────────────────────────────────────────────────────────
        vip_active_row = await conn.fetchrow(
            """
            WITH purchase_freq AS (
                SELECT phone
                FROM loyalty_ledger
                WHERE org_id = $1
                  AND reason = 'purchase'
                  AND created_at >= NOW() - INTERVAL '30 days'
                GROUP BY phone
                HAVING COUNT(*) >= 4
            ),
            high_spend AS (
                SELECT o.phone
                FROM orders o
                INNER JOIN purchase_freq pf ON pf.phone = o.phone
                WHERE o.org_id = $1
                  AND o.created_at >= NOW() - INTERVAL '30 days'
                  AND o.status NOT IN ('cancelled', 'pendiente')
                GROUP BY o.phone
                HAVING SUM(o.total) >= 200000
            )
            SELECT
                COUNT(DISTINCT hs.phone)          AS cnt,
                AVG(o.total)                      AS avg_ticket
            FROM orders o
            INNER JOIN high_spend hs ON hs.phone = o.phone
            WHERE o.org_id = $1
              AND o.created_at >= NOW() - INTERVAL '30 days'
              AND o.status NOT IN ('cancelled', 'pendiente')
            """,
            org_id,
        )
        vip_active_count = int(vip_active_row["cnt"]) if vip_active_row else 0
        vip_avg_ticket = (
            float(vip_active_row["avg_ticket"])
            if vip_active_row and vip_active_row["avg_ticket"]
            else 0.0
        )
        # potential_revenue: 4 visits/month going forward
        vip_active_rev = (
            round(vip_active_count * vip_avg_ticket * 4, 2)
            if vip_avg_ticket and vip_active_count
            else None
        )

        # ── VIP dormant ────────────────────────────────────────────────────────
        vip_dormant_row = await conn.fetchrow(
            """
            WITH was_vip AS (
                SELECT phone
                FROM loyalty_ledger
                WHERE org_id = $1
                  AND reason = 'purchase'
                  AND created_at >= NOW() - INTERVAL '90 days'
                  AND created_at <  NOW() - INTERVAL '60 days'
                GROUP BY phone
                HAVING COUNT(*) >= 4
            ),
            recent_buyers AS (
                SELECT DISTINCT phone
                FROM orders
                WHERE org_id = $1
                  AND created_at >= NOW() - INTERVAL '30 days'
                  AND status NOT IN ('cancelled', 'pendiente')
            )
            SELECT COUNT(*) AS cnt
            FROM was_vip wv
            WHERE NOT EXISTS (
                SELECT 1 FROM recent_buyers rb WHERE rb.phone = wv.phone
            )
            """,
            org_id,
        )
        vip_dormant_count = int(vip_dormant_row["cnt"]) if vip_dormant_row else 0
        # recovery scenario: 2 visits/month potential
        vip_dormant_rev = (
            round(vip_dormant_count * vip_avg_ticket * 2, 2)
            if vip_avg_ticket and vip_dormant_count
            else None
        )

        # ── New this month ─────────────────────────────────────────────────────
        new_month_row = await conn.fetchrow(
            """
            WITH first_purchase AS (
                SELECT phone, MIN(created_at) AS first_at
                FROM loyalty_ledger
                WHERE org_id = $1
                  AND reason = 'purchase'
                GROUP BY phone
            )
            SELECT COUNT(*) AS cnt
            FROM first_purchase
            WHERE first_at >= NOW() - INTERVAL '30 days'
            """,
            org_id,
        )
        new_month_count = int(new_month_row["cnt"]) if new_month_row else 0

        # ── Birthdays ──────────────────────────────────────────────────────────
        # TODO: ADD COLUMN birthday_month SMALLINT NULL to loyalty_customers
        birthday_count = 0

    return [
        {
            "id":                "vip-active",
            "name":              "VIPs activos",
            "description":       "Gastan >$200k/mes con frecuencia ≥4× en los últimos 30 días",
            "count":             vip_active_count,
            "potential_revenue": vip_active_rev,   # float or None  # JSON boundary
            "tone":              "success",
        },
        {
            "id":                "vip-dormant",
            "name":              "VIPs dormidos",
            "description":       "Fueron VIP hace 60+ días, sin visita reciente",
            "count":             vip_dormant_count,
            "potential_revenue": vip_dormant_rev,  # float or None  # JSON boundary
            "tone":              "warn",
        },
        {
            "id":                "new-month",
            "name":              "Nuevos del mes",
            "description":       "Primera visita en los últimos 30 días",
            "count":             new_month_count,
            "potential_revenue": None,
            "tone":              "info",
        },
        {
            "id":                "birthdays",
            "name":              "Cumpleaños del mes",
            "description":       "Postre gratis + 500 pts · automático",
            "count":             birthday_count,
            "potential_revenue": None,
            "tone":              "purple",
        },
    ]


async def db_get_loyalty_funnel(org_id: int) -> list[dict]:
    """
    Compute the 5-step conversion funnel for the loyalty dashboard.

    Cohort: ALL loyalty members (registered customers in loyalty_customers).
    Tier thresholds read from restaurants.features.loyalty_tiers or defaults.

    Steps:
      1. Primera visita  — total loyalty_customers rows.
      2. Segunda visita  — customers with ≥2 'purchase' ledger entries.
      3. Suben a Plata   — points_balance ≥ plata_min (default 21).
      4. Suben a Oro     — points_balance ≥ oro_min (default 81).
      5. Suben a Platino — points_balance ≥ platino_min (default 201).

    All percentages are relative to step 1.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    import json as _json

    async with tenant_connection() as conn:
        feats_row = await conn.fetchrow(
            "SELECT features FROM restaurants WHERE id = $1", org_id
        )
        feats: dict = {}
        if feats_row and feats_row["features"]:
            f = feats_row["features"]
            if isinstance(f, str):
                try:
                    feats = _json.loads(f)
                except Exception:
                    feats = {}
            elif isinstance(f, dict):
                feats = f

        tiers = feats.get("loyalty_tiers") or {}
        plata_min   = int((tiers.get("plata")   or [21,   80])[0])
        oro_min     = int((tiers.get("oro")     or [81,  200])[0])
        platino_min = int((tiers.get("platino") or [201, 9999])[0])

        # Steps 1, 3, 4, 5 all scan loyalty_customers for the same org —
        # batched into one aggregate query to save 3 round-trips.
        tier_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                                AS step1_total,
                COUNT(*) FILTER (WHERE points_balance >= $2)           AS step3_plata,
                COUNT(*) FILTER (WHERE points_balance >= $3)           AS step4_oro,
                COUNT(*) FILTER (WHERE points_balance >= $4)           AS step5_platino
            FROM loyalty_customers
            WHERE org_id = $1
            """,
            org_id, plata_min, oro_min, platino_min,
        )
        step1 = int(tier_row["step1_total"] or 0)
        step3 = int(tier_row["step3_plata"] or 0)
        step4 = int(tier_row["step4_oro"] or 0)
        step5 = int(tier_row["step5_platino"] or 0)

        # Step 2 is on loyalty_ledger — separate table, stays as its own query.
        step2 = int(await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM (
                SELECT phone
                FROM loyalty_ledger
                WHERE org_id = $1 AND reason = 'purchase'
                GROUP BY phone
                HAVING COUNT(*) >= 2
            ) sub
            """,
            org_id,
        ) or 0)

    def _pct(n: int) -> float:
        return round(n / step1 * 100, 1) if step1 > 0 else 0.0

    return [
        {
            "label":       "Primera visita",
            "description": "Registro vía WhatsApp",
            "count":       step1,
            "pct":         100.0 if step1 > 0 else 0.0,
        },
        {
            "label":       "Segunda visita",
            "description": "≤30 días después",
            "count":       step2,
            "pct":         _pct(step2),
        },
        {
            "label":       "Suben a Plata",
            "description": f"≥{plata_min} puntos",
            "count":       step3,
            "pct":         _pct(step3),
        },
        {
            "label":       "Suben a Oro",
            "description": f"≥{oro_min} puntos",
            "count":       step4,
            "pct":         _pct(step4),
        },
        {
            "label":       "Suben a Platino",
            "description": f"≥{platino_min} puntos",
            "count":       step5,
            "pct":         _pct(step5),
        },
    ]


async def db_get_phone_for_base_order(base_order_id: str) -> str | None:
    """
    Obtiene el teléfono del cliente asociado a un ticket de mesa.
    Busca en table_orders por id directo o por base_order_id de sub-órdenes.
    Usado por el background task de acumulación de loyalty en caja.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        phone = await conn.fetchval(
            "SELECT phone FROM table_orders WHERE id=$1 OR base_order_id=$1 LIMIT 1",
            base_order_id,
        )
    return phone
