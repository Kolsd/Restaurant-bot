"""
Restaurant repository — superadmin and restaurant-level SQL operations.

Covers:
  - Admin stats (global counts)
  - User management (delete)
  - Restaurant credential/settings updates (wa_phone_id, wa_access_token, name, address, etc.)
  - Billing stats (fiscal_invoices aggregate)
  - Maintenance utilities (fix-branch-ids, fix-conversations)
  - Restaurant detail stats for superadmin dashboard
"""

from __future__ import annotations

import json as _json


# Lazy accessors — break circular import with app.services.database.
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


# ── Superadmin global stats ───────────────────────────────────────────────────

async def db_get_admin_stats() -> dict:
    """Return global platform counts: restaurants, users, orders, MRR."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        total_rest   = await conn.fetchval("SELECT COUNT(*) FROM restaurants")
        active_rest  = await conn.fetchval("SELECT COUNT(*) FROM restaurants WHERE subscription_status='active'")
        total_users  = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
    mrr = (active_rest or 0) * 99
    return {
        "total_restaurants":  int(total_rest or 0),
        "active_restaurants": int(active_rest or 0),
        "total_orders":       int(total_orders or 0),
        "mrr":                mrr,
    }


# ── User management ──────────────────────────────────────────────────────────

async def db_delete_user(username: str) -> None:
    """Hard-delete a user by username."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username=$1", username.lower().strip())


# ── Restaurant credential update ─────────────────────────────────────────────

async def db_set_restaurant_wa_credentials(
    whatsapp_number: str, wa_phone_id: str, wa_access_token: str
) -> None:
    """Persist wa_phone_id + wa_access_token right after restaurant creation."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE restaurants
               SET wa_phone_id = $1, wa_access_token = $2
               WHERE whatsapp_number = $3""",
            wa_phone_id, wa_access_token, whatsapp_number,
        )


async def db_update_restaurant_fields(
    restaurant_id: int,
    *,
    name: str | None = None,
    address: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    whatsapp_number: str | None = None,
    wa_phone_id: str | None = None,
    wa_access_token: str | None = None,
    features: dict | None = None,
    menu: dict | None = None,
) -> None:
    """
    Update any subset of restaurant fields atomically.
    Each non-None kwarg is applied as a separate UPDATE statement within one connection.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if name is not None:
            await conn.execute("UPDATE restaurants SET name=$1 WHERE id=$2", name, restaurant_id)
        if address is not None and latitude is not None and longitude is not None:
            await conn.execute(
                "UPDATE restaurants SET address=$1, latitude=$2, longitude=$3 WHERE id=$4",
                address, latitude, longitude, restaurant_id,
            )
        if whatsapp_number is not None:
            await conn.execute("UPDATE restaurants SET whatsapp_number=$1 WHERE id=$2", whatsapp_number, restaurant_id)
        if wa_phone_id is not None:
            await conn.execute("UPDATE restaurants SET wa_phone_id=$1 WHERE id=$2", wa_phone_id, restaurant_id)
        if wa_access_token is not None:
            await conn.execute("UPDATE restaurants SET wa_access_token=$1 WHERE id=$2", wa_access_token, restaurant_id)
        if features is not None:
            await conn.execute(
                "UPDATE restaurants SET features=$1::jsonb WHERE id=$2",
                features, restaurant_id,
            )
        if menu is not None:
            await conn.execute("UPDATE restaurants SET menu=$1::jsonb WHERE id=$2", menu, restaurant_id)


# ── Restaurant detail stats (superadmin dashboard) ───────────────────────────

async def db_get_restaurant_detail_stats(restaurant_id: int, wa: str) -> dict:
    """
    Return 30-day and today order counts, table orders, conversation count,
    user count, fiscal invoice counts for a given restaurant.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        orders_30d   = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(total),0) AS rev "
            "FROM orders WHERE bot_number=$1 AND created_at >= NOW()-INTERVAL '30 days'",
            wa,
        )
        orders_today = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM orders WHERE bot_number=$1 AND created_at >= CURRENT_DATE",
            wa,
        )
        table_30d    = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM table_orders "
            "WHERE created_at >= NOW()-INTERVAL '30 days' AND status NOT IN ('cancelado') "
            "AND (SELECT whatsapp_number FROM restaurants WHERE id=table_orders.branch_id OR id=$1 LIMIT 1)=$1",
            restaurant_id,
        )
        convs        = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE bot_number=$1", wa)
        users_cnt    = await conn.fetchval("SELECT COUNT(*) FROM users WHERE branch_id=$1", restaurant_id)

        has_invoices = await conn.fetchval("SELECT to_regclass('fiscal_invoices')")
        if has_invoices:
            invoices_30d = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(total_cents),0) AS total "
                "FROM fiscal_invoices WHERE restaurant_id=$1 AND created_at >= NOW()-INTERVAL '30 days'",
                restaurant_id,
            )
            invoices_all = await conn.fetchval(
                "SELECT COUNT(*) FROM fiscal_invoices WHERE restaurant_id=$1", restaurant_id
            )
        else:
            invoices_30d = None
            invoices_all = 0

        last_order = await conn.fetchval("SELECT MAX(created_at) FROM orders WHERE bot_number=$1", wa)

    return {
        "orders_30d":       int(orders_30d["cnt"])  if orders_30d else 0,
        "revenue_30d":      float(orders_30d["rev"]) if orders_30d else 0.0,
        "orders_today":     int(orders_today["cnt"]) if orders_today else 0,
        "table_orders_30d": int(table_30d["cnt"])   if table_30d else 0,
        "active_convs":     int(convs or 0),
        "users":            int(users_cnt or 0),
        "invoices_30d":     int(invoices_30d["cnt"]) if invoices_30d else 0,
        "invoices_all":     int(invoices_all or 0),
        "last_order":       last_order.isoformat() if last_order else None,
    }


# ── Billing stats ─────────────────────────────────────────────────────────────

async def db_get_billing_stats() -> list[dict]:
    """Return per-restaurant fiscal invoice aggregates for the superadmin billing page."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        table_exists = await conn.fetchval("SELECT to_regclass('fiscal_invoices')")
        if not table_exists:
            return []
        rows = await conn.fetch(
            """
            SELECT fi.restaurant_id, r.name AS restaurant_name,
                   COUNT(fi.id) AS total_invoices,
                   COUNT(fi.id) FILTER (WHERE fi.created_at >= NOW()-INTERVAL '30 days') AS invoices_30d,
                   COUNT(fi.id) FILTER (WHERE fi.dian_status='accepted') AS accepted,
                   COUNT(fi.id) FILTER (WHERE fi.dian_status='pending')  AS pending,
                   COALESCE(SUM(fi.total_cents) FILTER (WHERE fi.dian_status='accepted'),0) AS total_billed_cents,
                   MAX(fi.created_at) AS last_invoice_at
            FROM fiscal_invoices fi
            JOIN restaurants r ON r.id = fi.restaurant_id
            GROUP BY fi.restaurant_id, r.name
            ORDER BY total_invoices DESC
            """
        )
    return [dict(r) for r in rows]


# ── Maintenance utilities ─────────────────────────────────────────────────────

async def db_fix_branch_ids() -> list[dict]:
    """
    Assign branch_id + role='owner' to users whose branch_id is NULL
    by matching restaurant_name. Returns list of fixed records.
    """
    pool = await _get_pool()
    fixed = []
    async with pool.acquire() as conn:
        restaurants = await conn.fetch("SELECT id, name, whatsapp_number FROM restaurants")
        rest_map    = {r["name"].lower().strip(): dict(r) for r in restaurants}
        users       = await conn.fetch(
            "SELECT username, restaurant_name, role FROM users WHERE branch_id IS NULL"
        )
        for user in users:
            rname = user["restaurant_name"].lower().strip()
            if rname in rest_map:
                rest = rest_map[rname]
                await conn.execute(
                    "UPDATE users SET branch_id=$1, role='owner' WHERE username=$2",
                    rest["id"], user["username"],
                )
                fixed.append({"username": user["username"], "branch_id": rest["id"]})
    return fixed


async def db_fix_conversations_bot_number(bot_number: str) -> None:
    """Backfill empty bot_number in conversations rows."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversations SET bot_number=$1 WHERE bot_number='' OR bot_number IS NULL",
            bot_number,
        )


# ── Settings ─────────────────────────────────────────────────────────────────

async def db_save_restaurant_settings(
    restaurant_id: int,
    features: dict,
    latitude: float | None = None,
    longitude: float | None = None,
) -> None:
    """
    Persist restaurant features JSONB and optionally update lat/lon.
    Called from POST /api/settings.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurants SET features = $1::jsonb WHERE id = $2",
            _json.dumps(features), restaurant_id,
        )
        try:
            if latitude is not None:
                await conn.execute(
                    "UPDATE restaurants SET latitude=$1 WHERE id=$2", latitude, restaurant_id
                )
            if longitude is not None:
                await conn.execute(
                    "UPDATE restaurants SET longitude=$1 WHERE id=$2", longitude, restaurant_id
                )
        except (ValueError, TypeError):
            pass


# ── Dashboard data queries ────────────────────────────────────────────────────

async def db_get_dashboard_orders(
    start_date,
    end_date,
    branch_id,
    bot_number: str | None,
) -> tuple[list, list]:
    """
    Return (delivery_rows, table_rows) for the dashboard orders page.
    Both are raw dicts; post-processing happens in the route.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Delivery / pickup orders (orders table)
        q_wa = "SELECT * FROM orders WHERE created_at >= $1 AND created_at < $2"
        p_wa: list = [start_date, end_date]
        if bot_number:
            if branch_id == "all":
                q_wa += " AND bot_number LIKE $3"
                p_wa.append(f"{bot_number}%")
            else:
                q_wa += " AND bot_number = $3"
                p_wa.append(bot_number)
        q_wa += " ORDER BY created_at DESC"
        rows_wa = await conn.fetch(q_wa, *p_wa)

        # Table orders (mesa)
        if branch_id and branch_id != "all":
            q_mesa = """
                SELECT o.* FROM table_orders o
                LEFT JOIN restaurant_tables t ON o.table_id = t.id
                WHERE o.created_at >= $1 AND o.created_at < $2
                AND t.branch_id = $3
                ORDER BY o.created_at DESC
            """
            p_mesa = [start_date, end_date, branch_id]
        else:
            q_mesa = """
                SELECT * FROM table_orders
                WHERE created_at >= $1 AND created_at < $2
                ORDER BY created_at DESC
            """
            p_mesa = [start_date, end_date]
        rows_mesa = await conn.fetch(q_mesa, *p_mesa)

    return ([dict(r) for r in rows_wa], [dict(r) for r in rows_mesa])


async def db_get_dashboard_reservations(
    start_date,
    end_date,
    bot_number: str | None,
) -> list[dict]:
    """Return reservations for the dashboard in the given date window."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        query = "SELECT * FROM reservations WHERE created_at >= $1 AND created_at < $2"
        params: list = [start_date, end_date]
        if bot_number:
            query += " AND bot_number = $3"
            params.append(bot_number)
        query += " ORDER BY date ASC, time ASC"
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def db_get_dashboard_conversations(
    branch_id,
    bot_number: str | None,
) -> list[dict]:
    """Return conversations for the dashboard filtered by branch/bot_number."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        query = "SELECT * FROM conversations"
        conditions: list[str] = []
        params: list = []
        idx = 1

        if branch_id == "all":
            pass
        elif branch_id:
            conditions.append(f"branch_id = ${idx}")
            params.append(branch_id)
            idx += 1
        elif bot_number:
            conditions.append(f"bot_number = ${idx}")
            params.append(bot_number)
            idx += 1

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC"
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


# ── Public menu ───────────────────────────────────────────────────────────────

async def db_get_public_menu_data(normalized_bot_number: str) -> dict | None:
    """
    Return restaurant menu + availability + features for the public menu endpoint.
    Normalizes by stripping + and spaces from bot_number.
    Returns None if not found.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rest = await conn.fetchrow(
            """
            SELECT
                r.id AS restaurant_id,
                r.name,
                r.menu,
                p.menu AS parent_menu,
                r.features,
                p.features AS parent_features
            FROM restaurants r
            LEFT JOIN restaurants p ON r.parent_restaurant_id = p.id
            WHERE replace(replace(r.whatsapp_number, '+', ''), ' ', '') = $1
            """,
            normalized_bot_number,
        )
        if not rest:
            return None
        inv_rows = await conn.fetch(
            "SELECT dish_name, available FROM menu_availability WHERE restaurant_id = $1",
            rest["restaurant_id"],
        )
    availability = {r["dish_name"]: r["available"] for r in inv_rows}
    return {
        "restaurant_id": rest["restaurant_id"],
        "name":           rest["name"],
        "menu":           rest["menu"],
        "parent_menu":    rest["parent_menu"],
        "features":       rest["features"],
        "parent_features": rest["parent_features"],
        "availability":   availability,
    }


# ── Team / Branch management ──────────────────────────────────────────────────

async def db_get_branches(parent_restaurant_id: int) -> list[dict]:
    """Return all child branches for a given parent restaurant."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM restaurants WHERE parent_restaurant_id = $1 ORDER BY id ASC",
            parent_restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_matriz_details(restaurant_id: int) -> dict | None:
    """Return whatsapp_number, menu, features, wa_phone_id, wa_access_token for a restaurant."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT whatsapp_number, menu, features, wa_phone_id, wa_access_token "
            "FROM restaurants WHERE id = $1",
            restaurant_id,
        )
    return dict(row) if row else None


async def db_set_branch_parent(
    whatsapp_number: str,
    parent_restaurant_id: int,
    wa_phone_id: str,
    wa_access_token: str,
) -> None:
    """Link a newly created restaurant to its parent (branch creation flow)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE restaurants
            SET parent_restaurant_id = $1,
                wa_phone_id = $3,
                wa_access_token = $4
            WHERE whatsapp_number = $2
            """,
            parent_restaurant_id,
            whatsapp_number,
            wa_phone_id,
            wa_access_token,
        )


async def db_delete_branch(branch_id: int, parent_restaurant_id: int) -> bool:
    """
    Verify the branch belongs to the parent, then delete its users and restaurant row.
    Returns False if not found / not owned.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        is_my_branch = await conn.fetchval(
            "SELECT id FROM restaurants WHERE id = $1 AND parent_restaurant_id = $2",
            branch_id, parent_restaurant_id,
        )
        if not is_my_branch:
            return False
        await conn.execute("DELETE FROM users WHERE branch_id=$1", branch_id)
        await conn.execute("DELETE FROM restaurants WHERE id=$1", branch_id)
    return True


async def db_get_team_users(branch_id: int) -> list[dict]:
    """Return users list with branch name for a given branch."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.username, u.role, u.branch_id, r.name as branch_name
            FROM users u
            LEFT JOIN restaurants r ON u.branch_id = r.id
            WHERE u.branch_id = $1
            ORDER BY u.created_at DESC
            """,
            branch_id,
        )
    return [dict(r) for r in rows]


async def db_delete_user_by_username(username: str) -> None:
    """Delete a user row by username."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username=$1", username.lower().strip())


async def db_delete_staff_by_id(staff_id: str) -> bool:
    """Delete a staff row by UUID. Returns True if found and deleted."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM staff WHERE id=$1::uuid RETURNING id", staff_id
        )
    return deleted is not None
