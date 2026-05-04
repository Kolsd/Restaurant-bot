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
from decimal import Decimal


# Lazy accessors — break circular import with app.services.database.
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


def _tenant_connection():
    """Return tenant_connection() async-ctx-manager (lazy import to break cycle)."""
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


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
        calories (int|None), prep_time_min (int|None), active (bool, True),
        combo_suggestions (list[{dish_name, extra_price}], [])
    """
    # combo_suggestions — strict per-entry validation (Fase 5d)
    raw_combos = dish.get("combo_suggestions")
    validated_combos: list[dict] = []
    if isinstance(raw_combos, list):
        for entry in raw_combos:
            if not isinstance(entry, dict):
                continue
            dish_name = entry.get("dish_name")
            extra_price = entry.get("extra_price")
            if not isinstance(dish_name, str) or not dish_name.strip():
                continue
            if not isinstance(extra_price, (int, float, Decimal)) or extra_price < 0:
                continue
            validated_combos.append({
                "dish_name":   dish_name.strip(),
                "extra_price": int(extra_price),  # store as int (no float arith)
            })

    return {
        "name":               dish.get("name", ""),
        "description":        dish.get("description", ""),
        "price":              dish.get("price", 0),
        "image_url":          dish.get("image_url"),        # None = no image
        "image_public_id":    dish.get("image_public_id"),  # None = no Cloudinary asset
        "tags":               dish.get("tags") if isinstance(dish.get("tags"), list) else [],
        "badges":             dish.get("badges") if isinstance(dish.get("badges"), list) else [],
        "allergens":          dish.get("allergens") if isinstance(dish.get("allergens"), list) else [],
        "featured":           bool(dish.get("featured", False)),
        "sort_order":         int(dish.get("sort_order", 999)),
        "calories":           dish.get("calories"),         # None = unknown
        "prep_time_min":      dish.get("prep_time_min"),    # None = unknown
        "active":             bool(dish.get("active", True)),
        "combo_suggestions":  validated_combos,
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

    Also handles the legacy nested format {categories: [{name, items}]} produced by
    setup_demo.py and some older imports.  Detected when the only key is "categories"
    and its value is a list of objects that each have "name" and "items" keys.
    Those are converted on-the-fly to the flat {category_name: [dishes]} format so
    find_dish, _build_compact_menu, and the dashboard all see consistent data.
    """
    if not isinstance(menu, dict):
        return menu

    # Auto-convert {categories: [{name, items}]} → {category_name: [dishes]}
    raw_cats = menu.get("categories")
    if (
        len(menu) == 1
        and isinstance(raw_cats, list)
        and raw_cats
        and isinstance(raw_cats[0], dict)
        and "items" in raw_cats[0]
    ):
        flat: dict = {}
        for cat_obj in raw_cats:
            cat_name = cat_obj.get("name", "Sin categoría")
            items = cat_obj.get("items", [])
            if isinstance(items, list):
                flat[cat_name] = [
                    normalize_dish_shape(d) if isinstance(d, dict) else d
                    for d in items
                ]
        return flat

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


# Public alias used by tests and future call-sites (Fase 5d).
# Wraps _normalize_menu_dishes with None → {} coercion so frontends can trust the shape.
def normalize_menu_shape(menu):
    if menu is None:
        return {}
    return _normalize_menu_dishes(menu)


# ── GLOBAL functions (not tenant-scoped) — call sites must provide their own scope if needed ──
# These functions enumerate ALL restaurants, handle auth/login paths that query users
# cross-tenant, or are called from inbox_worker dispatch resolution BEFORE a tenant is pinned.
# They MUST keep _get_pool() and must NOT use tenant_connection().

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
    """Persist wa_phone_id + wa_access_token right after restaurant creation.

    Targets organizations first (default number), then falls back to locations
    (override number for multi-number chains).
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Try organization-level number first
        updated_org = await conn.execute(
            """UPDATE organizations
               SET wa_phone_id = $1, wa_access_token = $2
               WHERE replace(replace(whatsapp_number, '+', ''), ' ', '') =
                     replace(replace($3, '+', ''), ' ', '')""",
            wa_phone_id, wa_access_token, whatsapp_number,
        )
        # Also update location-level override if this number is a location override
        await conn.execute(
            """UPDATE locations
               SET wa_phone_id = $1, wa_access_token = $2
               WHERE replace(replace(whatsapp_number, '+', ''), ' ', '') =
                     replace(replace($3, '+', ''), ' ', '')""",
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
    Each non-None kwarg is routed to organizations or locations depending on column ownership.
    restaurant_id is a Location id (via the VIEW); org-level fields resolve via subquery.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        # --- Location-level fields ---
        if address is not None and latitude is not None and longitude is not None:
            await conn.execute(
                "UPDATE locations SET address=$1, latitude=$2, longitude=$3 WHERE id=$4",
                address, latitude, longitude, restaurant_id,
            )
        # --- Org-level name (update both: org.name for Matriz, loc.name for branches) ---
        if name is not None:
            await conn.execute("UPDATE locations SET name=$1 WHERE id=$2", name, restaurant_id)
            await conn.execute(
                """UPDATE organizations SET name=$1
                   WHERE id = (SELECT org_id FROM locations WHERE id=$2)""",
                name, restaurant_id,
            )
        # --- Org-level default credential fields ---
        if whatsapp_number is not None:
            await conn.execute(
                """UPDATE organizations SET whatsapp_number=$1
                   WHERE id = (SELECT org_id FROM locations WHERE id=$2)""",
                whatsapp_number, restaurant_id,
            )
        if wa_phone_id is not None:
            await conn.execute(
                """UPDATE organizations SET wa_phone_id=$1
                   WHERE id = (SELECT org_id FROM locations WHERE id=$2)""",
                wa_phone_id, restaurant_id,
            )
        if wa_access_token is not None:
            await conn.execute(
                """UPDATE organizations SET wa_access_token=$1
                   WHERE id = (SELECT org_id FROM locations WHERE id=$2)""",
                wa_access_token, restaurant_id,
            )
        if features is not None:
            await conn.execute(
                """UPDATE organizations SET features=$1::jsonb
                   WHERE id = (SELECT org_id FROM locations WHERE id=$2)""",
                _json.dumps(features) if isinstance(features, dict) else features,
                restaurant_id,
            )
        if menu is not None:
            await conn.execute(
                """UPDATE organizations SET menu=$1::jsonb
                   WHERE id = (SELECT org_id FROM locations WHERE id=$2)""",
                _json.dumps(menu) if isinstance(menu, dict) else menu,
                restaurant_id,
            )


# ── Restaurant detail stats (superadmin dashboard) ───────────────────────────

async def db_get_restaurant_detail_stats(restaurant_id: int, wa: str) -> dict:
    """
    Return 30-day and today order counts, table orders, conversation count,
    user count, fiscal invoice counts for a given restaurant.

    Cross-tenant by design — called from internal/admin under bypass_tenant_scope.
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    with bypass_tenant_scope("db_get_restaurant_detail_stats: superadmin cross-tenant stats"):
        async with _tenant_connection() as conn:
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
                    "FROM fiscal_invoices WHERE org_id=$1 AND created_at >= NOW()-INTERVAL '30 days'",
                    restaurant_id,
                )
                invoices_all = await conn.fetchval(
                    "SELECT COUNT(*) FROM fiscal_invoices WHERE org_id=$1", restaurant_id
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
            SELECT fi.org_id AS restaurant_id, o.name AS restaurant_name,
                   COUNT(fi.id) AS total_invoices,
                   COUNT(fi.id) FILTER (WHERE fi.created_at >= NOW()-INTERVAL '30 days') AS invoices_30d,
                   COUNT(fi.id) FILTER (WHERE fi.dian_status='accepted') AS accepted,
                   COUNT(fi.id) FILTER (WHERE fi.dian_status='pending')  AS pending,
                   COALESCE(SUM(fi.total_cents) FILTER (WHERE fi.dian_status='accepted'),0) AS total_billed_cents,
                   MAX(fi.created_at) AS last_invoice_at
            FROM fiscal_invoices fi
            JOIN organizations o ON o.id = fi.org_id
            GROUP BY fi.org_id, o.name
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

    features → organizations (org-level config).
    lat/lon  → locations (physical address of the location).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        await conn.execute(
            """UPDATE organizations SET features = $1::jsonb
               WHERE id = (SELECT org_id FROM locations WHERE id = $2)""",
            _json.dumps(features), restaurant_id,
        )
        try:
            if latitude is not None:
                await conn.execute(
                    "UPDATE locations SET latitude=$1 WHERE id=$2", latitude, restaurant_id
                )
            if longitude is not None:
                await conn.execute(
                    "UPDATE locations SET longitude=$1 WHERE id=$2", longitude, restaurant_id
                )
        except (ValueError, TypeError):
            pass


async def db_update_restaurant_owner_phone(
    restaurant_id: int,
    phone: str | None,
) -> None:
    """Set or clear the owner_phone for weekly reports delivery.

    owner_phone is not a column in the VIEW (it was a legacy restaurants column).
    It is stored inside organizations.features as features->>'owner_phone' so that
    scheduler.py can read it via restaurant.get("features", {}).get("owner_phone").

    Pass None or empty string to clear the phone (removes the key from features).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        if phone:
            # Merge the owner_phone key into the existing features JSONB
            await conn.execute(
                """UPDATE organizations
                   SET features = COALESCE(features, '{}'::jsonb) || jsonb_build_object('owner_phone', $1::text)
                   WHERE id = (SELECT org_id FROM locations WHERE id = $2)""",
                phone,
                restaurant_id,
            )
        else:
            # Remove the key to clear it
            await conn.execute(
                """UPDATE organizations
                   SET features = COALESCE(features, '{}'::jsonb) - 'owner_phone'
                   WHERE id = (SELECT org_id FROM locations WHERE id = $1)""",
                restaurant_id,
            )


async def db_update_restaurant_timezone(
    restaurant_id: int,
    timezone: str,
) -> None:
    """Set the IANA timezone for the restaurant (used by scheduler and weekly reports).

    timezone lives in locations (physical timezone of the location).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        await conn.execute(
            "UPDATE locations SET timezone = $1 WHERE id = $2",
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

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE organizations
               SET features = COALESCE(features, '{}'::jsonb) || $2::jsonb
             WHERE id = (SELECT org_id FROM locations WHERE id = $1)
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

    RLS active — caller must run inside tenant_scope() or bypass_tenant_scope().
    Dashboard routes run inside bypass (no scoped dep wired yet).
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    with bypass_tenant_scope("db_get_dashboard_orders: admin dashboard cross-tenant read"):
        async with _tenant_connection() as conn:
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
    """Return reservations for the dashboard in the given date window.

    RLS active — runs under bypass (dashboard route has no scoped dep wired yet).
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    with bypass_tenant_scope("db_get_dashboard_reservations: admin dashboard cross-tenant read"):
        async with _tenant_connection() as conn:
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
    """Return conversations for the dashboard filtered by branch/bot_number.

    Filter semantics post-Wave-2:
      - branch_id = 'all' → no sede filter (relies on bot_number for tenant
        scoping; admin owner viewing 'Casa Matriz' sees all of their org's
        conversations across every sede)
      - branch_id = digit AND looks like a location_id → filter location_id
      - bot_number always applied when present (defense in depth — unique
        per restaurant, ensures no cross-tenant leak under bypass)

    Pre-2026-04-29 the SQL filtered `WHERE branch_id = $1`. Post-migration
    0057 (sync_table_branch_id) the conversations.branch_id column carries
    location_id, NOT org_id. The dashboard route used to default branch_id
    to user.branch_id (= org_id) and the filter matched zero rows for every
    Herradura-style multi-sede org. Same bug family as floor_plan.

    RLS active — runs under bypass (dashboard route has no scoped dep wired yet).
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    with bypass_tenant_scope("db_get_dashboard_conversations: admin dashboard cross-tenant read"):
        async with _tenant_connection() as conn:
            query = "SELECT * FROM conversations"
            conditions: list[str] = []
            params: list = []
            idx = 1

            # bot_number is the strongest tenant filter we have under bypass —
            # apply unconditionally when known, regardless of branch selection.
            if bot_number:
                conditions.append(f"bot_number = ${idx}")
                params.append(bot_number)
                idx += 1

            # location_id filter only when caller explicitly picked a sede.
            # 'all' or None → cross-sede view of the same org (bot_number filter
            # already scopes to the org).
            if branch_id and branch_id != "all":
                conditions.append(f"location_id = ${idx}")
                params.append(branch_id)
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
                l.org_id,
                r.name,
                r.menu,
                o.menu AS parent_menu,
                r.features,
                o.features AS parent_features
            FROM restaurants r
            JOIN locations l ON l.id = r.id
            JOIN organizations o ON o.id = l.org_id
            WHERE replace(replace(r.whatsapp_number, '+', ''), ' ', '') = $1
            """,
            normalized_bot_number,
        )
        if not rest:
            return None
        inv_rows = await conn.fetch(
            "SELECT dish_name, available FROM menu_availability WHERE org_id = $1",
            rest["org_id"],
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

async def db_get_branches(org_id: int) -> list[dict]:
    """Return all locations (peers) for a given org.

    Post-Wave-2: caller passes org_id (the tenant key). Every location in the
    org is returned — no "matriz" vs "sucursal" distinction (Wave-2 model:
    all sedes are peers). The caller decides whether to exclude the current
    location from the dropdown UI.

    Historical implementation assumed the param was a location_id and did a
    subquery (SELECT org_id FROM locations WHERE id=$1). That returned 0
    rows whenever the caller passed an org_id that was not also a location
    id — common for orgs created after Wave-2 where org_id and location_id
    sequences diverged. Fixed by filtering on l.org_id directly.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*
            FROM restaurants r
            JOIN locations l ON l.id = r.id
            WHERE l.org_id = $1
            ORDER BY l.id ASC
            """,
            org_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_matriz_details(restaurant_id: int) -> dict | None:
    """Return whatsapp_number, menu, features, wa_phone_id, wa_access_token for a restaurant.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
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
    """Link a newly created location to its org (branch creation flow).

    The second arg is a location.id used to resolve the org_id for the new branch.
    The new location (looked up by whatsapp_number) is assigned to the same org.
    wa_phone_id and wa_access_token are branch-level overrides stored on locations.

    Cross-tenant: resolves org_id from one location, then updates another.
    Uses bypass_tenant_scope since this operates across location boundaries.
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    with bypass_tenant_scope("db_set_branch_parent: link new location to org across tenant boundary"):
        async with _tenant_connection() as conn:
            # Resolve the org_id from the parent location id
            org_id = await conn.fetchval(
                "SELECT org_id FROM locations WHERE id = $1",
                parent_restaurant_id,
            )
            if org_id is None:
                log.warning(
                    "db_set_branch_parent.parent_not_found",
                    parent_restaurant_id=parent_restaurant_id,
                    whatsapp_number=whatsapp_number,
                )
                return
            # Update the branch location: assign to same org, set wa credentials
            await conn.execute(
                """UPDATE locations
                   SET org_id = $1, wa_phone_id = $2, wa_access_token = $3
                   WHERE replace(replace(whatsapp_number, '+', ''), ' ', '') =
                         replace(replace($4, '+', ''), ' ', '')""",
                org_id,
                wa_phone_id,
                wa_access_token,
                whatsapp_number,
            )


async def db_delete_branch(branch_id: int, parent_restaurant_id: int) -> bool:
    """
    Verify the branch belongs to the same org as the parent, then delete its location row.
    Users linked to this branch (by branch_id) have their branch_id nulled automatically
    via CASCADE if the FK is set, or are updated explicitly here to avoid orphans.
    Returns False if not found / not owned.

    Any location can be deleted this way; the route-level guard is responsible
    for any policy on protecting the last remaining location of an org.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Verify branch belongs to the same org as parent, and is NOT primary
            branch_row = await conn.fetchrow(
                """SELECT l.id, l.org_id
                   FROM locations l
                   WHERE l.id = $1
                     AND l.org_id = (SELECT org_id FROM locations WHERE id = $2)""",
                branch_id, parent_restaurant_id,
            )
            if not branch_row:
                return False
            # Nullify users referencing this branch to avoid FK errors
            await conn.execute("UPDATE users SET branch_id=NULL WHERE branch_id=$1", branch_id)
            # Delete the non-primary location
            await conn.execute("DELETE FROM locations WHERE id=$1", branch_id)
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
    """Delete a staff row by UUID. Returns True if found and deleted.

    Caller should run inside tenant_scope(org_id) for proper RLS enforcement.
    Falls back to bypass for the legacy no-scope call path in team_routes.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415
    with bypass_tenant_scope_if_unset("db_delete_staff_by_id: tenant scope preferred; bypass for legacy call path"):
        async with _tenant_connection() as conn:
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


async def db_update_user_password(username: str, password_hash: str) -> bool:
    """Update users.password_hash for a given username.

    Returns True if a row was updated, False otherwise. GLOBAL table — no
    tenant_scope required.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET password_hash=$1 WHERE username=$2",
            password_hash, username.lower().strip(),
        )
    return result.endswith(" 1")


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
    """Return restaurant row for bot runtime.

    Post-Wave-2: `id` is overridden with `org_id` so downstream code using
    `restaurant_obj["id"]` as the tenant key (for FKs into organizations, RLS,
    etc.) works correctly. The original location_id is preserved under
    `location_id`. Callers needing the sede identifier should use that.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT r.*, l.org_id, l.id AS location_id
            FROM restaurants r
            JOIN locations l ON l.id = r.id
            WHERE r.whatsapp_number=$1
            ORDER BY (l.whatsapp_number = $1) DESC NULLS LAST, l.id ASC
            LIMIT 1
            """,
            _normalize_phone(whatsapp_number.strip()),
        )
        if not row:
            return None
        d = _serialize(dict(row))
        d["id"] = d["org_id"]  # bot runtime expects tenant key here
        return d


async def db_get_restaurant_by_bot_number(whatsapp_number: str):
    return await db_get_restaurant_by_phone(whatsapp_number)


async def db_get_restaurant_by_name(name: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurants WHERE name=$1", name)
        return _serialize(dict(row)) if row else None


async def db_get_restaurant_by_id(restaurant_id: int):
    """Lookup restaurant by location_id (VIEW id) OR org_id.

    Post-Wave-2: accepts either a location_id or an org_id. The returned
    dict's `id` field is normalized to org_id (tenant key) for consistency
    with db_get_restaurant_by_phone. `location_id` is preserved.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT r.*, l.org_id, l.id AS location_id
            FROM restaurants r
            JOIN locations l ON l.id = r.id
            WHERE r.id = $1 OR l.org_id = $1
            ORDER BY (l.org_id = $1) DESC, l.id ASC
            LIMIT 1
            """,
            restaurant_id,
        )
        if not row:
            return None
        d = _serialize(dict(row))
        d["id"] = d["org_id"]
        return d


async def db_get_all_restaurants(parent_id: int = None):
    """
    Post-Wave-2: every restaurant is a row in `locations` (the `restaurants`
    table is a VIEW over locations JOIN organizations). All locations are peers
    — there is no special "primary" / "matriz" entity anymore.

    Behaviour:
      parent_id passed     → return all OTHER locations of the same org
                             (the historical "branches of this matriz")
      parent_id omitted    → return one deterministic location per org
                             ordered by org_id (the historical "all matrices")

    Each returned dict is enriched with explicit `org_id` and `location_id`
    so callers do NOT have to depend on the 0034 backfill Matriz invariant
    (org_id == matriz_location_id), which only holds for orgs that existed
    at Wave-2 deploy time. NEW orgs created after deploy have independent
    org_id and location_id integers.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if parent_id:
            # Historical "branches of matriz X" → all OTHER locations of
            # the same org. Resolve via the locations table directly so the
            # query semantics survive when parent_restaurant_id is dropped.
            rows = await conn.fetch(
                """
                SELECT r.*, l.org_id, l.id AS location_id
                FROM restaurants r
                JOIN locations  l ON l.id = r.id
                WHERE l.org_id = (SELECT org_id FROM locations WHERE id = $1)
                  AND l.id != $1
                ORDER BY r.name ASC
                """,
                parent_id,
            )
        else:
            # Historical "all matrices" → one deterministic location per org
            # (lowest id), ordered by org_id.
            rows = await conn.fetch(
                """
                SELECT r.*, l.org_id, l.id AS location_id
                FROM restaurants r
                JOIN locations  l ON l.id = r.id
                WHERE l.id = (
                    SELECT MIN(id) FROM locations l2 WHERE l2.org_id = l.org_id
                )
                ORDER BY l.org_id ASC
                """
            )
        return [_serialize(dict(r)) for r in rows]


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
    """Create a new restaurant (organization + primary location).

    Preserves legacy signature; internally creates an organization row and
    a primary location row.  ON CONFLICT: if the whatsapp_number already belongs
    to an organization, updates that org and its primary location instead.
    """
    if features is None:
        features = {}
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            menu_json = _json.dumps(menu) if isinstance(menu, dict) else (menu or "[]")
            features_json = _json.dumps(features) if isinstance(features, dict) else (features or "{}")
            normalized_wa = _normalize_phone(whatsapp_number) if whatsapp_number else None

            # Upsert organization
            org_row = await conn.fetchrow(
                """INSERT INTO organizations (name, whatsapp_number, menu, features)
                   VALUES ($1, $2, $3::jsonb, $4::jsonb)
                   ON CONFLICT (whatsapp_number) DO UPDATE
                   SET name = EXCLUDED.name,
                       menu = EXCLUDED.menu,
                       features = EXCLUDED.features,
                       updated_at = NOW()
                   WHERE organizations.whatsapp_number IS NOT NULL
                   RETURNING id""",
                name, normalized_wa, menu_json, features_json,
            )
            if org_row is None:
                # whatsapp_number is NULL — no conflict clause applies; just fetch
                org_row = await conn.fetchrow(
                    "SELECT id FROM organizations WHERE name=$1 ORDER BY id DESC LIMIT 1", name
                )

            org_id = org_row["id"]

            # Insert default location (no conflict clause — partial unique index
            # on is_primary is dropped in migration 0038; callers must not
            # call this function twice for the same org).
            await conn.execute(
                """INSERT INTO locations
                       (org_id, name, code, address, latitude, longitude, active, timezone)
                   VALUES ($1, $2, 'principal', $3, $4, $5, true, 'America/Bogota')""",
                org_id, name, address, latitude, longitude,
            )


async def db_sync_menu_to_branches(parent_restaurant_id: int) -> int:
    """
    In the Org/Location model, menu lives on organizations and is shared by all
    locations via the VIEW JOIN — there is no per-branch menu column to sync.

    This function is retained for backward compatibility.  It returns the number
    of sibling locations (branch count) so the caller can display a meaningful
    count, but no database write is performed (the org menu was already updated
    by db_update_menu or db_update_restaurant_fields).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        # Count sibling locations in the same org (all OTHER locations).
        # Post-Wave-2: no is_primary column. Count all locations except the one passed in.
        count = await conn.fetchval(
            """SELECT COUNT(*)
               FROM locations
               WHERE org_id = (SELECT org_id FROM locations WHERE id = $1)
                 AND id != $1""",
            parent_restaurant_id,
        )
    return int(count or 0)


async def db_update_menu(restaurant_id: int, menu_data: dict) -> bool:
    """
    Sobrescribe el JSON del menú para un restaurante específico.

    Catálogo v2: antes de guardar, cada plato pasa por normalize_dish_shape y se
    valida que image_public_id pertenezca a este restaurante.  Lanza ValueError si
    algún plato tiene una imagen de otro tenant.

    # Requires active tenant_scope() or bypass_tenant_scope().
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
                        f"Solo se permiten imágenes bajo mesio/r_{restaurant_id}/."
                    )
                normalized.append(normalize_dish_shape(dish))
            menu_data[category] = normalized

    # Post-Wave-2: callers pass org_id (restaurant["id"] is normalized to
    # org_id in db_get_restaurant_by_phone). The historical subquery
    # (SELECT org_id FROM locations WHERE id = $2) assumed $2 was a
    # location_id and returned NULL for orgs whose id isn't also a location
    # id — the UPDATE then affected 0 rows and the route 500'd.
    async with _tenant_connection() as conn:
        result = await conn.execute(
            """UPDATE organizations SET menu = $1::jsonb WHERE id = $2""",
            json.dumps(menu_data, default=lambda o: float(o) if isinstance(o, Decimal) else str(o)),  # JSON boundary
            restaurant_id,
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
    """Upsert a staff_shifts record by its client-generated UUID.

    Wave-2: the `restaurant_id` parameter name is the legacy interface
    (kept for backwards compat with sync route signature) — the value is
    the canonical tenant key (org_id), so we write it to the `org_id`
    column. `location_id` is left NULL (post-0037d nullable) because the
    offline client only knows its tenant, not which sede the shift was
    clocked at; the staff_id FK still ties the row back to a sede.
    """
    await conn.execute(
        """
        INSERT INTO staff_shifts
            (id, staff_id, org_id, clock_in, clock_out, notes)
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
    """Upsert a staff record by its client-generated UUID.

    Wave-2: same convention as _sync_staff_shift — the legacy
    restaurant_id param name carries the tenant key (org_id) value.
    location_id is nullable post-0037d.
    """
    await conn.execute(
        """
        INSERT INTO staff
            (id, org_id, name, role, pin, active)
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

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    results = []
    async with _tenant_connection() as conn:
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
                o.menu AS parent_menu
            FROM restaurants r
            JOIN locations l ON l.id = r.id
            JOIN organizations o ON o.id = l.org_id
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
    """Update subscription_status on the organizations table.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        await conn.execute(
            """UPDATE organizations SET subscription_status=$1
               WHERE id = (SELECT org_id FROM locations WHERE id=$2)""",
            new_status, restaurant_id,
        )


# ── Menu availability ─────────────────────────────────────────────────────────

async def db_get_menu_availability(restaurant_id: int):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch("SELECT dish_name, available FROM menu_availability WHERE org_id = $1", restaurant_id)
        return {r['dish_name']: r['available'] for r in rows}


async def db_set_dish_availability(restaurant_id: int, dish_name: str, available: bool):
    """# Requires active tenant_scope() or bypass_tenant_scope().

    Wave-2: `restaurant_id` param name kept for the legacy interface,
    value is the org_id tenant key. menu_availability lost the
    restaurant_id column in 0037 (the unique constraint was recreated
    as (dish_name, org_id) — see inventory_repo._sync_dish_availability_conn
    for the canonical pattern).
    """
    async with _tenant_connection() as conn:
        await conn.execute("""
            INSERT INTO menu_availability (dish_name, org_id, available, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (dish_name, org_id) DO UPDATE SET available=EXCLUDED.available, updated_at=NOW()
        """, dish_name, restaurant_id, available)


# ── NPS analytics ─────────────────────────────────────────────────────────────

async def db_get_nps_stats(bot_number: str, period: str = "month", branch_id: int | str = None, days: int = None) -> dict:
    """Return NPS aggregate stats. bot_number used as tenant discriminator.

    RLS active — runs under bypass (nps.py route uses get_current_restaurant, not _scoped).
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    period_map = {"today": "1 day", "week": "7 days", "month": "30 days", "semester": "180 days", "year": "365 days"}
    if days is not None and days > 0:
        interval_str = f"{int(days)} days"
    else:
        interval_str = period_map.get(period, "30 days")

    with bypass_tenant_scope("db_get_nps_stats: NPS dashboard cross-tenant read via bot_number"):
        async with _tenant_connection() as conn:
            conditions = ["bot_number = $1", f"created_at >= NOW() - INTERVAL '{interval_str}'"]
            params = [bot_number]

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
    """Return paginated NPS responses. bot_number used as tenant discriminator.

    RLS active — runs under bypass (nps.py route uses get_current_restaurant, not _scoped).
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    period_map = {"today": "1 day", "week": "7 days", "month": "30 days", "semester": "180 days", "year": "365 days"}
    interval_str = period_map.get(period, "30 days")

    with bypass_tenant_scope("db_get_nps_responses: NPS dashboard cross-tenant read via bot_number"):
        async with _tenant_connection() as conn:
            conditions = ["bot_number = $1", f"created_at >= NOW() - INTERVAL '{interval_str}'"]
            params = [bot_number]

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


async def db_get_recent_nps_for_caja(limit: int = 10) -> list[dict]:
    """Return the most recent NPS responses for the current tenant.

    RLS-scoped via app.org_id GUC — caller must be inside tenant_scope().
    Phone is anonymized to "***1234" (last 4 digits) so caja staff can
    correlate with a recent customer without exposing full PII on screen.

    The widget polls this every 30s; an order-by-created_at LIMIT keeps
    it cheap even on busy tenants.
    """
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT phone, score, COALESCE(comment, '') AS comment, created_at
               FROM nps_responses
               WHERE org_id = NULLIF(current_setting('app.org_id', true), '')::int
               ORDER BY created_at DESC
               LIMIT $1""",
            limit,
        )
    out = []
    for r in rows:
        raw_phone = r["phone"] or ""
        if raw_phone:
            anon = "***" + raw_phone[-4:]
        else:
            anon = "***"
        out.append({
            "phone": anon,
            "score": r["score"],
            "comment": r["comment"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return out


# ── Subscription usage ────────────────────────────────────────────────────────

async def _ensure_usage_table() -> None:
    """No-op: subscription_usage managed by Alembic (0020_missing_runtime_tables.py)."""
    pass


async def db_increment_token_usage(restaurant_id: int, tokens: int) -> None:
    """Suma `tokens` al contador diario del restaurante (upsert atómico).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    if tokens <= 0:
        return
    await _ensure_usage_table()
    async with _tenant_connection() as conn:
        # Insert both restaurant_id and org_id (same value during Wave 1).
        # ON CONFLICT uses the new org_id-based constraint which is NOT NULL and
        # guaranteed to exist after migration 0037b.  The legacy restaurant_id
        # constraint (also restored by 0037b) keeps backward-compat for any
        # concurrent code path that reads by restaurant_id.
        await conn.execute(
            """INSERT INTO subscription_usage (org_id, usage_date, total_tokens)
               VALUES ($1, CURRENT_DATE, $2)
               ON CONFLICT (org_id, usage_date) DO UPDATE
               SET total_tokens = subscription_usage.total_tokens + $2,
                   updated_at   = NOW()""",
            restaurant_id, tokens,
        )


async def db_increment_invoice_usage(restaurant_id: int) -> None:
    """Incrementa en 1 el contador de facturas diarias del restaurante (upsert atómico).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    await _ensure_usage_table()
    async with _tenant_connection() as conn:
        await conn.execute(
            """INSERT INTO subscription_usage (org_id, usage_date, total_invoices)
               VALUES ($1, CURRENT_DATE, 1)
               ON CONFLICT (org_id, usage_date) DO UPDATE
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

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    from app.services.database import UsageLimitExceeded  # noqa: PLC0415
    await _ensure_usage_table()
    async with _tenant_connection() as conn:
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
               WHERE org_id = $1 AND usage_date = CURRENT_DATE""",
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
            SELECT r.id, r.name, r.slug, r.whatsapp_number, r.menu, r.features, r.address,
                   o.menu AS parent_menu
            FROM restaurants r
            JOIN locations l ON l.id = r.id
            JOIN organizations o ON o.id = l.org_id
            WHERE r.slug = $1
            """,
            slug,
        )
    if not row:
        return None
    return dict(row)


# ── Org/Location lookups (Bloque S3) ─────────────────────────────────────────
#
# These functions operate on the `organizations` and `locations` tables
# introduced by migration 0034.  Neither table is in _RLS_TABLES (only
# tenant-content tables have RLS policies).  However:
#   - Functions called PRE-SCOPE (webhook resolution, public lookups) use
#     _get_pool() directly — GLOBAL pattern, same as db_get_restaurant_by_phone.
#   - Functions called WITHIN an authenticated scope use _tenant_connection() or
#     _get_pool() depending on whether they are tenant-specific operations.
#     For org/location tables without RLS, _get_pool() is fine; we keep
#     _tenant_connection() only where the operation is logically tenant-bound
#     (e.g. update org settings from an authenticated admin request).
#
# Bypass is still used in pre-scope helpers for audit-log consistency (all
# cross-tenant reads should produce a bypass log entry).


async def db_get_org_by_id(org_id: int) -> dict | None:
    """Fetch an Organization by PK.

    organizations has no RLS policy (it IS the tenant container), so we use
    bypass_tenant_scope for audit-log consistency.  Safe from any call site.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_get_org_by_id_lookup"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, slug, whatsapp_number, wa_phone_id, wa_access_token,
                       menu, features, subscription_plan, subscription_status,
                       created_at, updated_at
                FROM organizations
                WHERE id = $1
                """,
                org_id,
            )
    if not row:
        return None
    d = _serialize(dict(row))
    # Deserialize JSONB fields that asyncpg may return as dicts or strings
    for field in ("menu", "features"):
        val = d.get(field)
        if isinstance(val, str):
            try:
                d[field] = _json.loads(val)
            except Exception:
                d[field] = {} if field == "features" else []
        elif val is None:
            d[field] = {} if field == "features" else []
    return d


async def db_get_all_orgs(active_only: bool = True) -> list[dict]:
    """Return ALL organizations (one row per tenant business).

    This is the canonical Wave-2 primitive for "enumerate every customer".
    Use it whenever the conceptual loop is per-tenant (scheduler ticks,
    Mesio internal admin views, billing aggregations) — NOT
    db_get_all_restaurants() which leans on the legacy "matriz" mental
    model and the vestigial is_primary flag.

    Returned dicts contain only Organization-level fields. If a caller
    ALSO needs sede-level info (whatsapp_number per location, address,
    etc.), follow up with db_get_org_locations(org_id) — locations are
    all peers, none of them is "the primary".

    No RLS on `organizations` (it IS the tenant container), so this works
    from any call site without an active tenant_scope.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    where = ""
    if active_only:
        # subscription_status is the per-org active flag; default 'active'.
        where = "WHERE subscription_status = 'active'"

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_get_all_orgs_enumerate"):
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, name, slug, whatsapp_number, wa_phone_id, wa_access_token,
                       menu, features, subscription_plan, subscription_status,
                       created_at, updated_at
                FROM organizations
                {where}
                ORDER BY id ASC
                """
            )
    result = []
    for row in rows:
        d = _serialize(dict(row))
        for field in ("menu", "features"):
            val = d.get(field)
            if isinstance(val, str):
                try:
                    d[field] = _json.loads(val)
                except Exception:
                    d[field] = {} if field == "features" else []
            elif val is None:
                d[field] = {} if field == "features" else []
        result.append(d)
    return result


async def db_get_org_by_phone(phone: str) -> dict | None:
    """Resolve which Organization owns a given WhatsApp number.

    Checks both:
      1. organizations.whatsapp_number (Org-level default number)
      2. locations.whatsapp_number (Location-level override for multi-number chains)

    Location match is preferred (more specific).  If matched via a Location
    override, the returned dict includes ``matched_location_id`` (else None).

    This is called PRE-SCOPE from inbox_worker dispatch resolution, so it uses
    the GLOBAL pool pattern + bypass_tenant_scope for audit-log consistency.

    Phone normalization is applied before querying (strip +, spaces).
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    normalized = _normalize_phone(phone)
    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_get_org_by_phone_webhook_resolve"):
        async with pool.acquire() as conn:
            # Try Location-level override first (more specific routing)
            loc_row = await conn.fetchrow(
                """
                SELECT l.id AS location_id, o.id, o.name, o.slug,
                       o.whatsapp_number, o.wa_phone_id, o.wa_access_token,
                       o.menu, o.features, o.subscription_plan, o.subscription_status,
                       o.created_at, o.updated_at
                FROM locations l
                JOIN organizations o ON o.id = l.org_id
                WHERE replace(replace(l.whatsapp_number, '+', ''), ' ', '') = $1
                  AND l.active = true
                """,
                normalized,
            )
            if loc_row:
                d = _serialize(dict(loc_row))
                matched_location_id = d.pop("location_id", None)
                for field in ("menu", "features"):
                    val = d.get(field)
                    if isinstance(val, str):
                        try:
                            d[field] = _json.loads(val)
                        except Exception:
                            d[field] = {} if field == "features" else []
                    elif val is None:
                        d[field] = {} if field == "features" else []
                d["matched_location_id"] = matched_location_id
                return d

            # Fall back to Org-level number
            org_row = await conn.fetchrow(
                """
                SELECT id, name, slug, whatsapp_number, wa_phone_id, wa_access_token,
                       menu, features, subscription_plan, subscription_status,
                       created_at, updated_at
                FROM organizations
                WHERE replace(replace(whatsapp_number, '+', ''), ' ', '') = $1
                """,
                normalized,
            )
            if not org_row:
                return None
            d = _serialize(dict(org_row))
            for field in ("menu", "features"):
                val = d.get(field)
                if isinstance(val, str):
                    try:
                        d[field] = _json.loads(val)
                    except Exception:
                        d[field] = {} if field == "features" else []
                elif val is None:
                    d[field] = {} if field == "features" else []
            d["matched_location_id"] = None
            return d


async def db_get_org_locations(org_id: int, active_only: bool = True) -> list[dict]:
    """List Locations for an Org, ordered alphabetically (name ASC, id ASC).

    Caller must be in tenant_scope(org_id) or bypass_tenant_scope; locations
    has no RLS policy so bypass is not strictly required, but we use
    _get_pool() + bypass for consistency with the GLOBAL pattern for
    cross-tenant/admin lookups.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_get_org_locations_list"):
        async with pool.acquire() as conn:
            if active_only:
                rows = await conn.fetch(
                    """
                    SELECT id, org_id, name, code, address, latitude, longitude,
                           whatsapp_number, wa_phone_id, wa_access_token,
                           active, timezone, opening_hours,
                           created_at, updated_at
                    FROM locations
                    WHERE org_id = $1 AND active = true
                    ORDER BY name ASC, id ASC
                    """,
                    org_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, org_id, name, code, address, latitude, longitude,
                           whatsapp_number, wa_phone_id, wa_access_token,
                           active, timezone, opening_hours,
                           created_at, updated_at
                    FROM locations
                    WHERE org_id = $1
                    ORDER BY name ASC, id ASC
                    """,
                    org_id,
                )
    result = []
    for row in rows:
        d = _serialize(dict(row))
        val = d.get("opening_hours")
        if isinstance(val, str):
            try:
                d["opening_hours"] = _json.loads(val)
            except Exception:
                d["opening_hours"] = {}
        elif val is None:
            d["opening_hours"] = {}
        result.append(d)
    return result


async def db_get_default_location(org_id: int) -> dict | None:
    """Return a deterministic default Location for the org (ORDER BY id ASC LIMIT 1).

    Post-Wave-2 all locations are peers — this does NOT return a "primary"
    location (no such concept exists). Use only for legacy fallback cases where
    a single representative location is needed (e.g. credential fallback when a
    branch has no WhatsApp credentials configured).
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_get_default_location_lookup"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, org_id, name, code, address, latitude, longitude,
                       whatsapp_number, wa_phone_id, wa_access_token,
                       active, timezone, opening_hours,
                       created_at, updated_at
                FROM locations
                WHERE org_id = $1
                ORDER BY id ASC
                LIMIT 1
                """,
                org_id,
            )
    if not row:
        return None
    d = _serialize(dict(row))
    val = d.get("opening_hours")
    if isinstance(val, str):
        try:
            d["opening_hours"] = _json.loads(val)
        except Exception:
            d["opening_hours"] = {}
    elif val is None:
        d["opening_hours"] = {}
    return d


# Deprecated alias. Remove after 1-2 sprints.
db_get_primary_location = db_get_default_location


async def db_resolve_org_id_from_location(location_id: int) -> int | None:
    """Resolve the org_id for a given location_id.

    Used where the caller has a location_id (e.g. from `restaurant["id"]` via
    the `restaurants` VIEW) but needs the real org_id for explicit repo queries.
    Returns None if the location is not found.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_resolve_org_id_from_location_lookup"):
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT org_id FROM locations WHERE id = $1",
                location_id,
            )


async def db_get_location_by_id(location_id: int) -> dict | None:
    """Fetch a Location by PK.  Returns None if not found.

    Includes org_id so the caller can validate ownership (e.g. in
    get_current_location dep — prevent cross-org location spoofing).
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_get_location_by_id_lookup"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, org_id, name, code, address, latitude, longitude,
                       whatsapp_number, wa_phone_id, wa_access_token,
                       active, timezone, opening_hours,
                       created_at, updated_at
                FROM locations
                WHERE id = $1
                """,
                location_id,
            )
    if not row:
        return None
    d = _serialize(dict(row))
    val = d.get("opening_hours")
    if isinstance(val, str):
        try:
            d["opening_hours"] = _json.loads(val)
        except Exception:
            d["opening_hours"] = {}
    elif val is None:
        d["opening_hours"] = {}
    return d


async def db_resolve_location_by_gps(
    org_id: int,
    lat: float,
    lon: float,
    radius_km: float = 5.0,
) -> dict | None:
    """Find the nearest active Location to (lat, lon) within radius_km.

    Returns None if no active Location of the Org is within range.

    Uses the standard Haversine formula:
      distance_km = 6371 * acos(
        cos(radians(lat1)) * cos(radians(lat2))
        * cos(radians(lon2) - radians(lon1))
        + sin(radians(lat1)) * sin(radians(lat2))
      )

    For orgs with few locations (typical: 1-5), we fetch all active locations
    with GPS and filter in Python — simpler and avoids earthdistance extension
    dependency.
    """
    import math  # noqa: PLC0415

    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_resolve_location_by_gps"):
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, org_id, name, code, address, latitude, longitude,
                       whatsapp_number, wa_phone_id, wa_access_token,
                       active, timezone, opening_hours,
                       created_at, updated_at
                FROM locations
                WHERE org_id = $1
                  AND active = true
                  AND latitude IS NOT NULL
                  AND longitude IS NOT NULL
                """,
                org_id,
            )

    if not rows:
        return None

    # Haversine filter in Python
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        lat1_r = math.radians(lat1)
        lat2_r = math.radians(lat2)
        cos_product = (
            math.cos(lat1_r)
            * math.cos(lat2_r)
            * math.cos(math.radians(lon2) - math.radians(lon1))
            + math.sin(lat1_r) * math.sin(lat2_r)
        )
        # Clamp to [-1, 1] to guard against floating-point drift
        cos_product = max(-1.0, min(1.0, cos_product))
        return 6371.0 * math.acos(cos_product)

    best: dict | None = None
    best_dist = float("inf")
    for row in rows:
        dist = _haversine(lat, lon, float(row["latitude"]), float(row["longitude"]))
        if dist <= radius_km and dist < best_dist:
            best_dist = dist
            best = row

    if best is None:
        return None

    d = _serialize(dict(best))
    val = d.get("opening_hours")
    if isinstance(val, str):
        try:
            d["opening_hours"] = _json.loads(val)
        except Exception:
            d["opening_hours"] = {}
    elif val is None:
        d["opening_hours"] = {}
    d["distance_km"] = round(best_dist, 3)
    return d


async def db_update_organization(org_id: int, **fields) -> dict | None:
    """Update Org fields.  Returns the updated row or None if not found.

    Accepted fields: name, slug, whatsapp_number, wa_phone_id, wa_access_token,
    menu, features, subscription_plan, subscription_status.

    Called from authenticated admin routes; uses bypass_tenant_scope since
    organizations has no RLS and we need to update the container itself.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    _ALLOWED_ORG_FIELDS = {
        "name", "slug", "whatsapp_number", "wa_phone_id", "wa_access_token",
        "menu", "features", "subscription_plan", "subscription_status",
    }
    _JSONB_FIELDS = {"menu", "features"}

    updates = {k: v for k, v in fields.items() if k in _ALLOWED_ORG_FIELDS}
    if not updates:
        log.warning("db_update_organization.no_valid_fields", org_id=org_id)
        return await db_get_org_by_id(org_id)

    set_clauses = []
    params: list = []
    idx = 1
    for col, val in updates.items():
        if col in _JSONB_FIELDS:
            # Serialize dicts to JSON string for the ::jsonb cast
            if isinstance(val, dict):
                val = _json.dumps(val)
            set_clauses.append(f"{col} = ${idx}::jsonb")
        else:
            set_clauses.append(f"{col} = ${idx}")
        params.append(val)
        idx += 1
    # Always update updated_at
    set_clauses.append("updated_at = NOW()")
    params.append(org_id)

    sql = (
        f"UPDATE organizations SET {', '.join(set_clauses)} "  # noqa: S608 — col names are whitelisted above
        f"WHERE id = ${idx} RETURNING id, name, slug, whatsapp_number, wa_phone_id, "
        f"wa_access_token, menu, features, subscription_plan, subscription_status, "
        f"created_at, updated_at"
    )

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_update_organization_admin"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)

    if not row:
        return None
    d = _serialize(dict(row))
    for field in ("menu", "features"):
        val = d.get(field)
        if isinstance(val, str):
            try:
                d[field] = _json.loads(val)
            except Exception:
                d[field] = {} if field == "features" else []
        elif val is None:
            d[field] = {} if field == "features" else []
    return d


async def db_update_location(location_id: int, **fields) -> dict | None:
    """Update Location fields.  Returns the updated row or None if not found.

    Accepted fields: name, code, address, latitude, longitude,
    whatsapp_number, wa_phone_id, wa_access_token, active,
    opening_hours, timezone.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    _ALLOWED_LOC_FIELDS = {
        "name", "code", "address", "latitude", "longitude",
        "whatsapp_number", "wa_phone_id", "wa_access_token",
        "active", "opening_hours", "timezone",
    }
    _JSONB_FIELDS = {"opening_hours"}

    updates = {k: v for k, v in fields.items() if k in _ALLOWED_LOC_FIELDS}
    if not updates:
        log.warning("db_update_location.no_valid_fields", location_id=location_id)
        return await db_get_location_by_id(location_id)

    set_clauses = []
    params: list = []
    idx = 1
    for col, val in updates.items():
        if col in _JSONB_FIELDS:
            if isinstance(val, dict):
                val = _json.dumps(val)
            set_clauses.append(f"{col} = ${idx}::jsonb")
        else:
            set_clauses.append(f"{col} = ${idx}")
        params.append(val)
        idx += 1
    set_clauses.append("updated_at = NOW()")
    params.append(location_id)

    sql = (
        f"UPDATE locations SET {', '.join(set_clauses)} "  # noqa: S608 — col names are whitelisted
        f"WHERE id = ${idx} RETURNING id, org_id, name, code, address, latitude, longitude, "
        f"whatsapp_number, wa_phone_id, wa_access_token, active, timezone, "
        f"opening_hours, created_at, updated_at"
    )

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_update_location_admin"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)

    if not row:
        return None
    d = _serialize(dict(row))
    val = d.get("opening_hours")
    if isinstance(val, str):
        try:
            d["opening_hours"] = _json.loads(val)
        except Exception:
            d["opening_hours"] = {}
    elif val is None:
        d["opening_hours"] = {}
    return d


async def db_create_location(org_id: int, name: str, **fields) -> dict:
    """Create a new Location for an Org.  is_primary defaults to false.

    Accepted extra fields: code, address, latitude, longitude,
    whatsapp_number, wa_phone_id, wa_access_token, active,
    opening_hours, timezone.

    Returns the created row as a dict.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    _ALLOWED_CREATE_FIELDS = {
        "code", "address", "latitude", "longitude",
        "whatsapp_number", "wa_phone_id", "wa_access_token",
        "active", "opening_hours", "timezone",
    }

    extra = {k: v for k, v in fields.items() if k in _ALLOWED_CREATE_FIELDS}

    # Build dynamic INSERT
    col_names = ["org_id", "name"]
    col_values: list = [org_id, name]
    placeholders = ["$1", "$2"]
    idx = 3
    for col, val in extra.items():
        col_names.append(col)
        if col == "opening_hours" and isinstance(val, dict):
            val = _json.dumps(val)
            placeholders.append(f"${idx}::jsonb")
        else:
            placeholders.append(f"${idx}")
        col_values.append(val)
        idx += 1

    sql = (
        f"INSERT INTO locations ({', '.join(col_names)}) "  # noqa: S608 — col names are whitelisted
        f"VALUES ({', '.join(placeholders)}) "
        f"RETURNING id, org_id, name, code, address, latitude, longitude, "
        f"whatsapp_number, wa_phone_id, wa_access_token, active, timezone, "
        f"opening_hours, created_at, updated_at"
    )

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_create_location_admin"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *col_values)

    d = _serialize(dict(row))
    val = d.get("opening_hours")
    if isinstance(val, str):
        try:
            d["opening_hours"] = _json.loads(val)
        except Exception:
            d["opening_hours"] = {}
    elif val is None:
        d["opening_hours"] = {}
    return d


async def db_list_organizations() -> list[dict]:
    """List all Organizations with a location_count aggregate.

    Called from internal superadmin; uses bypass + global pool (cross-tenant).
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_list_organizations_superadmin"):
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT o.id, o.name, o.slug, o.whatsapp_number,
                       o.wa_phone_id, o.subscription_plan, o.subscription_status,
                       o.features, o.created_at, o.updated_at,
                       COUNT(l.id)::int AS location_count
                FROM organizations o
                LEFT JOIN locations l ON l.org_id = o.id
                GROUP BY o.id
                ORDER BY o.created_at DESC
                """
            )
    result = []
    for row in rows:
        d = _serialize(dict(row))
        val = d.get("features")
        if isinstance(val, str):
            try:
                d["features"] = _json.loads(val)
            except Exception:
                d["features"] = {}
        elif val is None:
            d["features"] = {}
        result.append(d)
    return result


async def db_create_organization(
    name: str,
    whatsapp_number: str | None = None,
    wa_phone_id: str | None = None,
    wa_access_token: str | None = None,
    slug: str | None = None,
    features: dict | None = None,
    subscription_plan: str = "free",
) -> dict:
    """Insert a new Organization row and return the created record.

    Called from internal superadmin routes (cross-tenant, bypass needed).
    Slug uniqueness is enforced by DB UNIQUE constraint; duplicate slug raises
    asyncpg.UniqueViolationError which the route converts to 409.
    """
    from app.services.tenant_context import bypass_tenant_scope_if_unset  # noqa: PLC0415

    if features is None:
        features = {}

    pool = await _get_pool()
    with bypass_tenant_scope_if_unset("db_create_organization_superadmin"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO organizations
                    (name, slug, whatsapp_number, wa_phone_id, wa_access_token,
                     features, subscription_plan)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                RETURNING id, name, slug, whatsapp_number, wa_phone_id, wa_access_token,
                          menu, features, subscription_plan, subscription_status,
                          created_at, updated_at
                """,
                name,
                slug or None,
                whatsapp_number or None,
                wa_phone_id or None,
                wa_access_token or None,
                _json.dumps(features),
                subscription_plan,
            )
    d = _serialize(dict(row))
    for field in ("menu", "features"):
        val = d.get(field)
        if isinstance(val, str):
            try:
                d[field] = _json.loads(val)
            except Exception:
                d[field] = {} if field == "features" else []
        elif val is None:
            d[field] = {} if field == "features" else []
    return d
