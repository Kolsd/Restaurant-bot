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
import json


# Lazy accessors — break circular import with app.services.database.
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


def _normalize_phone(n: str) -> str:
    from app.services.database import _normalize_phone as _np  # noqa: PLC0415
    return _np(n)


from app.services.logging import get_logger  # noqa: E402
log = get_logger(__name__)


# ── Catálogo v2 — dish shape helpers ─────────────────────────────────────────

def normalize_dish_shape(dish: dict) -> dict:
    """
    Apply defaults for all extended dish fields introduced in catálogo v2.

    Called on every dish before returning from db_get_menu / db_get_public_menu_data,
    and before saving in db_update_menu.  Acts as a safety net so downstream code
    never sees missing keys regardless of how old the stored JSONB is.

    Required fields (validated upstream, not coerced here):
        name (str), price (Decimal/int)

    Extended fields (with defaults):
        description (str, ""), image_url (str|None), image_public_id (str|None),
        tags (list[str], []), badges (list[str], []), allergens (list[str], []),
        featured (bool, False), sort_order (int, 999),
        calories (int|None), prep_time_min (int|None), active (bool, True)
    """
    return {
        "name":             dish.get("name", ""),
        "description":      dish.get("description", ""),
        "price":            dish.get("price", 0),
        "image_url":        dish.get("image_url"),        # None = no image
        "image_public_id":  dish.get("image_public_id"),  # None = no Cloudinary asset
        "tags":             dish.get("tags") if isinstance(dish.get("tags"), list) else [],
        "badges":           dish.get("badges") if isinstance(dish.get("badges"), list) else [],
        "allergens":        dish.get("allergens") if isinstance(dish.get("allergens"), list) else [],
        "featured":         bool(dish.get("featured", False)),
        "sort_order":       int(dish.get("sort_order", 999)),
        "calories":         dish.get("calories"),         # None = unknown
        "prep_time_min":    dish.get("prep_time_min"),    # None = unknown
        "active":           bool(dish.get("active", True)),
    }


def validate_dish_image_ownership(dish: dict, restaurant_id: int) -> bool:
    """
    Return True if the dish's image_public_id belongs to *restaurant_id*.

    A dish without an image (image_public_id is None/empty) always passes.
    Prevents cross-tenant image references from being saved.
    """
    public_id = dish.get("image_public_id")
    if not public_id:
        return True
    expected_prefix = f"mesio/r_{restaurant_id}/"
    return str(public_id).startswith(expected_prefix)


def _normalize_menu_dishes(menu: dict) -> dict:
    """
    Walk a {category: [dish, ...]} menu and run normalize_dish_shape on every dish.
    Returns a new dict; does not mutate the input.
    """
    if not isinstance(menu, dict):
        return menu
    result: dict = {}
    for category, dishes in menu.items():
        if isinstance(dishes, list):
            result[category] = [
                normalize_dish_shape(d) if isinstance(d, dict) else d
                for d in dishes
            ]
        else:
            result[category] = dishes
    return result


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


async def db_update_restaurant_owner_phone(
    restaurant_id: int,
    phone: str | None,
) -> None:
    """Set or clear the owner_phone column for weekly reports delivery.

    Pass None or empty string to clear the phone (set NULL).
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurants SET owner_phone = $1 WHERE id = $2",
            phone or None,
            restaurant_id,
        )


async def db_update_restaurant_timezone(
    restaurant_id: int,
    timezone: str,
) -> None:
    """Set the IANA timezone for the restaurant (used by scheduler and weekly reports)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurants SET timezone = $1 WHERE id = $2",
            timezone,
            restaurant_id,
        )


async def db_merge_restaurant_features(
    restaurant_id: int,
    patch: dict,
) -> dict:
    """Shallow-merge *patch* into the restaurant's features JSONB and return the result.

    Uses the Postgres || operator so only the provided keys are overwritten —
    existing keys not in *patch* are preserved.  Returns the final features dict.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE restaurants
               SET features = COALESCE(features, '{}'::jsonb) || $2::jsonb
             WHERE id = $1
            RETURNING features
            """,
            restaurant_id,
            _json.dumps(patch),
        )
    if row is None:
        return patch
    raw = row["features"]
    if isinstance(raw, str):
        try:
            return _json.loads(raw)
        except Exception:
            return patch
    return dict(raw) if raw else patch


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

    # Catálogo v2: normalize dish shapes on read so consumers always see all fields.
    def _parse_and_normalize(raw) -> dict | None:
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
            except Exception:
                return {}
        else:
            parsed = raw
        return _normalize_menu_dishes(parsed)

    return {
        "restaurant_id":  rest["restaurant_id"],
        "name":           rest["name"],
        "menu":           _parse_and_normalize(rest["menu"]),
        "parent_menu":    _parse_and_normalize(rest["parent_menu"]),
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


async def db_find_restaurant_id_by_name(name: str) -> int | None:
    """Lookup restaurant ID by case-insensitive name match. Legacy fallback."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name FROM restaurants")
        name_lower = name.lower().strip()
        for r in rows:
            if r["name"].lower().strip() == name_lower:
                return r["id"]
    return None


# ── User functions ────────────────────────────────────────────────────────────

async def db_get_user(username: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE username=$1", username.lower().strip())
        return dict(row) if row else None


async def db_create_user(username: str, password_hash: str, restaurant_name: str,
                          role: str = "owner", branch_id: int = None, parent_user: str = None):
    import asyncpg  # noqa: PLC0415
    pool = await _get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO users (username, password_hash, restaurant_name, role, branch_id, parent_user)
                VALUES ($1,$2,$3,$4,$5,$6)
            """, username.lower().strip(), password_hash, restaurant_name, role, branch_id, parent_user)
            return True
        except asyncpg.UniqueViolationError:
            return False


async def db_get_all_users():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, restaurant_name, role, branch_id, parent_user FROM users")
        return [dict(r) for r in rows]


# ── Restaurant lookup functions ───────────────────────────────────────────────

async def db_get_restaurant_by_phone(whatsapp_number: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurants WHERE whatsapp_number=$1", _normalize_phone(whatsapp_number.strip()))
        return _serialize(dict(row)) if row else None


async def db_get_restaurant_by_bot_number(whatsapp_number: str):
    return await db_get_restaurant_by_phone(whatsapp_number)


async def db_get_restaurant_by_name(name: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurants WHERE name=$1", name)
        return _serialize(dict(row)) if row else None


async def db_get_restaurant_by_id(restaurant_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurants WHERE id=$1", restaurant_id)
        return _serialize(dict(row)) if row else None


async def db_get_all_restaurants(parent_id: int = None):
    """
    Si se pasa parent_id, solo devuelve las sucursales de ese padre.
    Si no, devuelve todos los restaurantes principales.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if parent_id:
            # Solo devuelve hijos reales
            rows = await conn.fetch(
                "SELECT * FROM restaurants WHERE parent_restaurant_id = $1 ORDER BY name ASC",
                parent_id
            )
        else:
            # Devuelve solo los que no tienen padre (Matrices)
            rows = await conn.fetch(
                "SELECT * FROM restaurants WHERE parent_restaurant_id IS NULL ORDER BY id ASC"
            )
        return [_serialize(dict(r)) for r in rows]


async def db_find_nearest_branch(customer_lat: float, customer_lon: float, parent_id: int) -> dict | None:
    """Finds the nearest branch to the customer's location within its delivery_radius_km.
    Uses the Haversine formula via PostgreSQL.
    Returns the nearest branch within coverage, or None if no branch covers that area."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, whatsapp_number, latitude, longitude,
                   features->>'delivery_radius_km' AS radius_km,
                   (
                     6371 * acos(
                       cos(radians($1)) * cos(radians(latitude::float))
                       * cos(radians(longitude::float) - radians($2))
                       + sin(radians($1)) * sin(radians(latitude::float))
                     )
                   ) AS distance_km
            FROM restaurants
            WHERE parent_restaurant_id = $3
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
            ORDER BY distance_km ASC
        """, customer_lat, customer_lon, parent_id)
        for r in rows:
            radius = float(r["radius_km"] or 5.0)
            if r["distance_km"] is not None and r["distance_km"] <= radius:
                return _serialize(dict(r))
    return None


async def db_find_nearest_branch_any(customer_lat: float, customer_lon: float, parent_id: int) -> dict | None:
    """Finds the geographically nearest branch with no delivery-radius constraint.
    Used for pickup routing where any branch is reachable by the customer."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, name, address, whatsapp_number,
                   (6371 * acos(
                     cos(radians($1)) * cos(radians(latitude::float))
                     * cos(radians(longitude::float) - radians($2))
                     + sin(radians($1)) * sin(radians(latitude::float))
                   )) AS distance_km
            FROM restaurants
            WHERE parent_restaurant_id = $3
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
            ORDER BY distance_km ASC
            LIMIT 1
        """, customer_lat, customer_lon, parent_id)
        return _serialize(dict(row)) if row else None


async def db_check_module(bot_number: str, module_name: str) -> bool:
    """
    Return True if module_name is explicitly enabled (true) in the restaurant's
    features JSONB column.

    Returns False for:
      - Restaurant not found for bot_number
      - Key not present in features
      - Key present but value is not the boolean true (e.g. false, null, string)

    Query is fully parametrized ($1, $2) — no f-strings, no injection risk.

    Example features structure:
        {"staff_tips": true, "reservations": true, "delivery": false}
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # ->> extracts the key as TEXT; comparing to 'true' safely handles any
        # non-boolean value stored in the JSONB without risking a cast error.
        # COALESCE turns NULL (restaurant not found, or key absent) into false.
        val = await conn.fetchval(
            "SELECT COALESCE((features->>$2) = 'true', false) "
            "FROM restaurants WHERE whatsapp_number=$1",
            _normalize_phone(bot_number),
            module_name,
        )
    # fetchval returns None when no rows match; bool(None) == False
    return bool(val)


async def db_create_restaurant(name: str, whatsapp_number: str, address: str, menu: dict,
                                latitude: float = None, longitude: float = None, features: dict = None):
    if features is None:
        features = {}
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # 🛡️ FIX: Los diccionarios entran directo a asyncpg (sin json.dumps)
        await conn.execute("""
            INSERT INTO restaurants (name, whatsapp_number, address, menu, latitude, longitude, features)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
            ON CONFLICT (whatsapp_number) DO UPDATE
            SET name=EXCLUDED.name, address=EXCLUDED.address, menu=EXCLUDED.menu,
                latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude, features=EXCLUDED.features
        """, name, whatsapp_number, address, menu, latitude, longitude, features)


async def db_sync_menu_to_branches(parent_restaurant_id: int) -> int:
    """
    Sincroniza (sobrescribe) la columna 'menu' de todas las sucursales hijas
    con el JSON exacto de la Casa Matriz.
    Devuelve el número de sucursales actualizadas.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # 1. Obtener el menú de la matriz
        parent = await conn.fetchrow("SELECT menu FROM restaurants WHERE id = $1", parent_restaurant_id)
        if not parent or not parent["menu"]:
            return 0

        menu_jsonb = parent["menu"]

        # 2. Hacer UPDATE masivo en las sucursales hijas
        result = await conn.execute(
            "UPDATE restaurants SET menu = $1 WHERE parent_restaurant_id = $2",
            menu_jsonb, parent_restaurant_id
        )

        # El result de execute suele ser un string como "UPDATE 3"
        try:
            count = int(result.split()[-1])
        except Exception:
            count = 0

        return count


async def db_update_menu(restaurant_id: int, menu_data: dict) -> bool:
    """
    Sobrescribe el JSON del menú para un restaurante específico.

    Catálogo v2: antes de guardar, cada plato pasa por normalize_dish_shape y se
    valida que image_public_id pertenezca a este restaurante.  Lanza ValueError si
    algún plato tiene una imagen de otro tenant.
    """
    # ── Validate + normalize dishes ───────────────────────────────────────────
    if isinstance(menu_data, dict):
        for category, dishes in menu_data.items():
            if not isinstance(dishes, list):
                continue
            normalized: list = []
            for dish in dishes:
                if not isinstance(dish, dict):
                    normalized.append(dish)
                    continue
                if not validate_dish_image_ownership(dish, restaurant_id):
                    bad_pid = dish.get("image_public_id")
                    log.warning(
                        "restaurant_repo.db_update_menu.cross_tenant_image",
                        restaurant_id=restaurant_id,
                        category=category,
                        dish_name=dish.get("name"),
                        image_public_id=bad_pid,
                    )
                    raise ValueError(
                        f"Plato '{dish.get('name')}' tiene una imagen que no pertenece a "
                        f"este restaurante (public_id='{bad_pid}'). "
                        "Solo se permiten imágenes bajo mesio/r_{restaurant_id}/."
                    )
                normalized.append(normalize_dish_shape(dish))
            menu_data[category] = normalized

    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE restaurants SET menu = $1::jsonb WHERE id = $2",
            json.dumps(menu_data), restaurant_id
        )
        return result == "UPDATE 1"


# ── Offline Sync Batch ────────────────────────────────────────────────────────
# Dispatch table for POST /api/sync operations.
# Keys are the `type` field sent by offline-sync.js.
# Each handler receives (conn, restaurant_id, op_data) and performs an upsert.
_SYNC_HANDLERS: dict = {}


def _register_sync_handler(type_name: str):
    """Decorator to register a sync handler function."""
    def decorator(fn):
        _SYNC_HANDLERS[type_name] = fn
        return fn
    return decorator


@_register_sync_handler("staff_shift")
async def _sync_staff_shift(conn, restaurant_id: int, data: dict):
    """Upsert a staff_shifts record by its client-generated UUID."""
    await conn.execute(
        """
        INSERT INTO staff_shifts
            (id, staff_id, restaurant_id, clock_in, clock_out, notes)
        VALUES ($1, $2::uuid, $3, $4::timestamptz, $5::timestamptz, $6)
        ON CONFLICT (id) DO UPDATE
            SET clock_out = EXCLUDED.clock_out,
                notes     = EXCLUDED.notes
        """,
        data.get("id"),
        data.get("staff_id"),
        restaurant_id,
        data.get("clock_in"),
        data.get("clock_out"),
        data.get("notes", ""),
    )


@_register_sync_handler("staff")
async def _sync_staff(conn, restaurant_id: int, data: dict):
    """Upsert a staff record by its client-generated UUID."""
    await conn.execute(
        """
        INSERT INTO staff
            (id, restaurant_id, name, role, pin, active)
        VALUES ($1::uuid, $2, $3, $4, $5, $6)
        ON CONFLICT (id) DO UPDATE
            SET name   = EXCLUDED.name,
                role   = EXCLUDED.role,
                pin    = EXCLUDED.pin,
                active = EXCLUDED.active
        """,
        data.get("id"),
        restaurant_id,
        data.get("name", ""),
        data.get("role", "staff"),
        data.get("pin", ""),
        data.get("active", True),
    )


async def db_sync_batch(restaurant_id: int, operations: list) -> list:
    """
    Process a batch of offline operations.
    Each operation: {id, type, action, data, client_ts}.
    Returns [{id, status: 'ok'|'error'|'unsupported_type', error?}].
    All operations use fully parametrized upserts — no f-string SQL.
    """
    pool = await _get_pool()
    results = []
    async with pool.acquire() as conn:
        for op in operations:
            op_id   = op.get("id", "unknown")
            op_type = op.get("type", "")
            handler = _SYNC_HANDLERS.get(op_type)
            if handler is None:
                results.append({
                    "id":     op_id,
                    "status": "unsupported_type",
                    "error":  f"No sync handler registered for type '{op_type}'",
                })
                continue
            try:
                async with conn.transaction():
                    await handler(conn, restaurant_id, op.get("data", {}))
                results.append({"id": op_id, "status": "ok"})
            except Exception as exc:
                results.append({"id": op_id, "status": "error", "error": str(exc)})
    return results


# ── Menu functions ────────────────────────────────────────────────────────────

async def db_get_menu(whatsapp_number: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                r.menu,
                p.menu AS parent_menu
            FROM restaurants r
            LEFT JOIN restaurants p ON r.parent_restaurant_id = p.id
            WHERE r.whatsapp_number = $1
        """, whatsapp_number)

        if not row:
            return None

        menu_data = row['menu']

        if (not menu_data or menu_data == '{}' or menu_data == "{}") and row['parent_menu']:
            menu_data = row['parent_menu']

        # 🛡️ AUTO-SANADOR: Repara cadenas doblemente codificadas al vuelo
        if menu_data:
            if isinstance(menu_data, str):
                try:
                    parsed = json.loads(menu_data)
                    if isinstance(parsed, str):
                        parsed = json.loads(parsed)
                    return _normalize_menu_dishes(parsed)
                except Exception:
                    return {}
            return _normalize_menu_dishes(menu_data)
        return {}


async def db_get_top_dishes(whatsapp_number: str, top_n: int = 5):
    menu = await db_get_menu(whatsapp_number)
    if not menu:
        return []
    all_dishes = []
    if isinstance(menu, dict):
        for cat, dishes in menu.items():
            if isinstance(dishes, list):
                all_dishes.extend(dishes)
    return all_dishes[:top_n]


async def db_update_subscription(restaurant_id: int, new_status: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE restaurants SET subscription_status=$2 WHERE id=$1", restaurant_id, new_status)


# ── Menu availability ─────────────────────────────────────────────────────────

async def db_get_menu_availability(restaurant_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT dish_name, available FROM menu_availability WHERE restaurant_id = $1", restaurant_id)
        return {r['dish_name']: r['available'] for r in rows}


async def db_set_dish_availability(restaurant_id: int, dish_name: str, available: bool):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO menu_availability (dish_name, restaurant_id, available, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (dish_name, restaurant_id) DO UPDATE SET available=EXCLUDED.available, updated_at=NOW()
        """, dish_name, restaurant_id, available)


# ── Settings wrapper ──────────────────────────────────────────────────────────

async def db_get_restaurant_settings() -> dict:
    all_r = await db_get_all_restaurants()
    return all_r[0] if all_r else {}


# ── NPS analytics ─────────────────────────────────────────────────────────────

async def db_get_nps_stats(bot_number: str, period: str = "month", branch_id: int | str = None) -> dict:
    pool = await _get_pool()
    period_map = {"today": "1 day", "week": "7 days", "month": "30 days", "semester": "180 days", "year": "365 days"}
    interval_str = period_map.get(period, "30 days")

    async with pool.acquire() as conn:
        conditions = ["bot_number = $1", f"created_at >= NOW() - INTERVAL '{interval_str}'"]
        params = [bot_number]

        # 🛡️ LA MAGIA DEL "ALL"
        if branch_id == "all":
            pass
        elif branch_id is not None:
            conditions.append("branch_id = $2")
            params.append(branch_id)

        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT COUNT(*) as total_responses, COALESCE(AVG(score), 0) as average_score,
            COUNT(*) FILTER (WHERE score = 5) as promoters, COUNT(*) FILTER (WHERE score = 4) as passives,
            COUNT(*) FILTER (WHERE score <= 3) as detractors
            FROM nps_responses WHERE {where_clause}
        """
        row = await conn.fetchrow(query, *params)

        total = row["total_responses"]
        nps_score = round(((row["promoters"] / total) - (row["detractors"] / total)) * 100) if total > 0 else 0

        return {
            "total_responses": total, "average_score": round(row["average_score"], 1),
            "nps_score": nps_score, "promoters": row["promoters"], "passives": row["passives"], "detractors": row["detractors"]
        }


async def db_get_nps_responses(bot_number: str, period: str = "month", limit: int = 50, branch_id: int | str = None) -> list:
    pool = await _get_pool()
    period_map = {"today": "1 day", "week": "7 days", "month": "30 days", "semester": "180 days", "year": "365 days"}
    interval_str = period_map.get(period, "30 days")

    async with pool.acquire() as conn:
        conditions = ["bot_number = $1", f"created_at >= NOW() - INTERVAL '{interval_str}'"]
        params = [bot_number]

        # 🛡️ LA MAGIA DEL "ALL"
        if branch_id == "all":
            pass
        elif branch_id is not None:
            conditions.append("branch_id = $2")
            params.append(branch_id)

        where_clause = " AND ".join(conditions)
        limit_idx = len(params) + 1
        params.append(limit)

        query = f"SELECT * FROM nps_responses WHERE {where_clause} ORDER BY created_at DESC LIMIT ${limit_idx}"
        rows = await conn.fetch(query, *params)

        result = []
        for r in rows:
            d = dict(r)
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat() + "Z"
            result.append(d)
        return result


# ── Subscription usage ────────────────────────────────────────────────────────

async def _ensure_usage_table() -> None:
    """No-op: subscription_usage managed by Alembic (0020_missing_runtime_tables.py)."""
    pass


async def db_increment_token_usage(restaurant_id: int, tokens: int) -> None:
    """Suma `tokens` al contador diario del restaurante (upsert atómico)."""
    if tokens <= 0:
        return
    await _ensure_usage_table()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO subscription_usage (restaurant_id, usage_date, total_tokens)
               VALUES ($1, CURRENT_DATE, $2)
               ON CONFLICT (restaurant_id, usage_date) DO UPDATE
               SET total_tokens = subscription_usage.total_tokens + $2,
                   updated_at   = NOW()""",
            restaurant_id, tokens,
        )


async def db_increment_invoice_usage(restaurant_id: int) -> None:
    """Incrementa en 1 el contador de facturas diarias del restaurante (upsert atómico)."""
    await _ensure_usage_table()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO subscription_usage (restaurant_id, usage_date, total_invoices)
               VALUES ($1, CURRENT_DATE, 1)
               ON CONFLICT (restaurant_id, usage_date) DO UPDATE
               SET total_invoices = subscription_usage.total_invoices + 1,
                   updated_at     = NOW()""",
            restaurant_id,
        )


async def db_check_usage_limits(restaurant_id: int) -> None:
    """
    Verifica que el restaurante no haya superado sus límites diarios.
    Lee restaurants.features.plan_limits → { daily_tokens, daily_invoices }.
    Si plan_limits está ausente, no se aplica ningún límite.
    Lanza UsageLimitExceeded si se superó algún límite.
    """
    from app.services.database import UsageLimitExceeded  # noqa: PLC0415
    await _ensure_usage_table()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Leer límites del plan desde features
        row = await conn.fetchrow(
            "SELECT features FROM restaurants WHERE id = $1", restaurant_id
        )
        if not row:
            return
        feats = row["features"] or {}
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except Exception:
                feats = {}
        limits = feats.get("plan_limits") if isinstance(feats, dict) else None
        if not limits:
            return  # sin límites configurados → acceso libre

        # Leer consumo del día actual
        usage = await conn.fetchrow(
            """SELECT total_tokens, total_invoices
               FROM subscription_usage
               WHERE restaurant_id = $1 AND usage_date = CURRENT_DATE""",
            restaurant_id,
        )
        used_tokens   = usage["total_tokens"]   if usage else 0
        used_invoices = usage["total_invoices"] if usage else 0

        token_limit   = limits.get("daily_tokens")
        invoice_limit = limits.get("daily_invoices")

        if token_limit and used_tokens >= int(token_limit):
            raise UsageLimitExceeded("tokens", used_tokens, int(token_limit))
        if invoice_limit and used_invoices >= int(invoice_limit):
            raise UsageLimitExceeded("facturas", used_invoices, int(invoice_limit))


async def db_has_orders_by_bot_number(bot_number: str) -> bool:
    """Return True if any order exists for this bot_number. Uses EXISTS for efficiency."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM orders WHERE bot_number = $1 LIMIT 1)",
            bot_number,
        )
    return bool(exists)


# ── Slug helpers (Catálogo v2 Fase 6 — SEO routes) ───────────────────────────

import re as _re


def _slugify(name: str) -> str:
    """Convert a restaurant or dish name to a URL-safe slug."""
    s = _re.sub(r'[^a-zA-Z0-9]+', '-', (name or '').lower()).strip('-')
    return s or 'restaurant'


async def db_get_restaurant_by_slug(slug: str) -> dict | None:
    """Return a restaurant row by its unique slug, or None if not found."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, slug, whatsapp_number, menu, features, address
            FROM restaurants
            WHERE slug = $1
            """,
            slug,
        )
    if not row:
        return None
    return dict(row)


async def db_slug_exists(slug: str) -> bool:
    """Return True if a restaurant with this slug already exists."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT 1 FROM restaurants WHERE slug = $1", slug
        )
    return val is not None


async def db_generate_unique_slug(name: str) -> str:
    """
    Generate a unique slug for a new restaurant.
    Tries base slug first; appends -2, -3, … until unique.
    Uses ON CONFLICT strategy: check-then-insert is acceptable here
    because slug assignment is infrequent (restaurant creation).
    """
    base = _slugify(name)
    candidate = base
    suffix = 2
    while await db_slug_exists(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
