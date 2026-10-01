"""
Staff repository — Fase 6 extraction from app.services.database.
Migrated to tenant_connection() — RLS rollout Step 3.

Covers the staff roster: CRUD and the PIN-login helpers. (Payroll, shifts,
breaks, schedules, tips, deductions, contracts, overtime and WebAuthn were
deleted in the 2026-09 cleanup; their tables stay until a later migration.)

Call sites that import via `app.services.database` continue to work through the
re-export shim added to that module.

Tenant isolation notes
----------------------
• Functions with a `restaurant_id` parameter use ``tenant_connection()``.
• Functions that look up data by staff/credential/shift UUID only (no tenant
  parameter) use ``bypass_tenant_scope("staff_cross_tenant_<reason>")``.
• ``_generate_username`` is an internal helper that must scan across tenants;
  it also uses bypass.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timezone

from app.services.tenant_db import tenant_connection
from app.services.tenant_context import bypass_tenant_scope


def _normalize_roles(d: dict) -> None:
    """Normalize the JSONB 'roles' field to always be a plain Python list.

    asyncpg may return JSONB as a Python list, a JSON string, or None depending
    on codec registration. Fallback to [role] when roles is empty.
    """
    raw = d.get("roles")
    if isinstance(raw, list):
        roles = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            roles = parsed if isinstance(parsed, list) else []
        except Exception:
            roles = []
    else:
        roles = []
    if not roles and d.get("role"):
        roles = [d["role"]]
    d["roles"] = roles


def _ensure_datetime(val) -> datetime:
    """Coerce date strings to datetime objects for asyncpg TIMESTAMPTZ binding."""
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return datetime.strptime(val, "%Y-%m-%d")
    return val


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


# ── Internal helpers ─────────────────────────────────────────────────────────

# ── Username generation ──────────────────────────────────────────────────────

def _normalize_for_username(text: str) -> str:
    """Remove accents, lowercase, keep only a-z0-9."""
    nfkd = unicodedata.normalize('NFKD', text.lower().strip())
    ascii_text = nfkd.encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]', '', ascii_text)


async def _generate_username(name: str, exclude_id: str | None = None) -> str:
    """Generate unique username as firstname.lastname from full name.
    If a duplicate exists, appends an incrementing number: juan.perez1, juan.perez2...

    Uses get_pool() + SET LOCAL ROLE mesio_superadmin directly to bypass RLS and
    scan globally — avoids TenantContextConflict when called from within an active
    tenant_scope (e.g. create_staff via get_current_restaurant_scoped dependency).
    """
    from app.services.database import get_pool

    parts = name.strip().split()
    fname = _normalize_for_username(parts[0]) if parts else 'user'
    lname = _normalize_for_username(parts[1]) if len(parts) > 1 else ''

    base = f"{fname}.{lname}" if lname else fname

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE mesio_superadmin")
            # The employee being edited doesn't collide with themselves: without
            # this, every save of the Team form renamed their login
            # (juan.perez → juan.perez1 → ...) and locked them out.
            rows = await conn.fetch(
                "SELECT username FROM staff WHERE username LIKE $1 || '%' "
                "AND ($2::uuid IS NULL OR id <> $2::uuid)",
                base, exclude_id,
            )
    existing = {r['username'] for r in rows}

    candidate = base
    counter = 0
    while candidate in existing:
        counter += 1
        candidate = f"{base}{counter}"

    return candidate


# ── Staff roster ─────────────────────────────────────────────────────────────

async def db_get_staff(restaurant_id: int, location_id: int | None = None) -> list:
    """Return all active (and inactive) staff members for a restaurant.

    Includes location_id + location_name (LEFT JOIN — a staff member with no
    sede assigned yet, location_id IS NULL, still comes back rather than
    being silently dropped) so the Team admin UI can flag unassigned staff
    ("Sin sede") in a multi-sede org (docs/claude/delivery-web.md chunk 8).

    `location_id` narrows the roster to ONE sede (PM 2026-09-20: an employee
    of one sede does not see another's). Unassigned staff stay visible in a
    narrowed view too — they are the rows the Team UI exists to flag, and
    hiding them from every sede would leave nobody able to notice them.
    None = the whole org, which only owner/admin get.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            "SELECT s.id::text, s.org_id, s.name, s.username, s.role, s.roles, s.active, "
            "s.phone, s.document_number, s.location_id, l.name AS location_name, "
            "s.created_at, s.updated_at "
            "FROM staff s LEFT JOIN locations l ON l.id = s.location_id "
            "WHERE s.org_id=$1 "
            "  AND ($2::bigint IS NULL OR s.location_id = $2 OR s.location_id IS NULL) "
            "ORDER BY s.name ASC",
            restaurant_id, location_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_staff_for_pin_login(restaurant_id: int, name: str) -> dict | None:
    """Return a staff member's record including pin hash for PIN authentication.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id::text, org_id, name, username, role, roles, active, phone, pin, "
            "document_number, hourly_rate, photo_url "
            "FROM staff WHERE org_id=$1 "
            "AND (LOWER(username)=LOWER($2) OR LOWER(name)=LOWER($2)) AND active=true",
            restaurant_id, name,
        )
    if not row:
        return None
    d = dict(row)
    _normalize_roles(d)
    return d


async def db_get_staff_candidates_by_name(name: str) -> list:
    """Returns all active staff with that name (multi-restaurant).
    The caller verifies the PIN against each candidate to resolve collisions.

    # Cross-tenant lookup — always uses bypass_tenant_scope internally.
    """
    with bypass_tenant_scope("staff_pin_login_cross_tenant: name/username may exist in multiple tenants; caller in auth.py resolves tenant after PIN verification"):
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                "SELECT id::text, org_id, name, username, role, roles, active, phone, pin, "
                "document_number, hourly_rate "
                "FROM staff WHERE (LOWER(name)=LOWER($1) OR LOWER(username)=LOWER($1)) AND active=true "
                "ORDER BY org_id",
                name,
            )
    result = []
    for row in rows:
        d = dict(row)
        _normalize_roles(d)
        result.append(d)
    return result


async def db_create_staff(
    restaurant_id: int,
    name: str,
    role: str,
    pin_hash: str,
    phone: str = "",
    roles: list = None,
    document_number: str = "",
    username: str = "",
    location_id: int | None = None,
) -> dict:
    """Insert a new staff member. Returns the created row.
    username is auto-generated from name if not provided.

    `location_id` is the sede the staff member works at. It is what scopes
    them to their own sede's orders (the staff JWT carries it); leaving it
    NULL makes sede-scoped sections refuse them. Callers resolve and
    validate it — this function never guesses one.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    if roles is None:
        roles = [role] if role else []
    if not username:
        username = await _generate_username(name)
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO staff (org_id, name, username, role, pin, phone, roles, document_number, location_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
               RETURNING id::text, org_id, location_id, name, username, role, roles, active, phone,
                         document_number, created_at, updated_at""",
            restaurant_id, name, username, role, pin_hash, phone, json.dumps(roles), document_number,
            location_id,
        )
    return _serialize(dict(row))


async def db_update_staff_pin_hash(staff_id: str, org_id: int, new_pin_hash: str) -> bool:
    """Update only staff.pin (bcrypt hash) for a single staff row.

    Used by auth.login() to upgrade legacy sha256 PINs to bcrypt on first
    successful login. Cross-tenant by design — login resolves the tenant
    AFTER PIN verification, so we bypass scope here and constrain by both
    staff_id and org_id (caller already authenticated the row).

    Returns True if a row was updated.
    """
    with bypass_tenant_scope("staff_pin_legacy_upgrade: post-login rehash before tenant_scope is established"):
        async with tenant_connection() as conn:
            result = await conn.execute(
                "UPDATE staff SET pin=$1, updated_at=NOW() WHERE id=$2::uuid AND org_id=$3",
                new_pin_hash, staff_id, org_id,
            )
    return result.endswith(" 1")


async def db_update_staff(staff_id: str, restaurant_id: int, fields: dict) -> dict | None:
    """
    Update mutable staff fields (name, role, roles, pin, phone, active).
    Ignores unknown keys. Returns updated row or None if not found.
    Only updates columns that are explicitly passed in fields.
    All values are passed as parameters — no f-string SQL.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    allowed = {"name", "username", "role", "roles", "pin", "phone", "active", "document_number", "location_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None

    # When name changes, regenerate username unless caller explicitly provides one
    if "name" in updates and "username" not in updates:
        updates["username"] = await _generate_username(updates["name"], exclude_id=staff_id)

    # Serialize roles list to JSON string for JSONB column
    if "roles" in updates and isinstance(updates["roles"], list):
        updates["roles"] = json.dumps(updates["roles"])

    async with tenant_connection() as conn:
        # Build SET clause with positional params starting at $3
        set_parts = []
        values = []
        for i, (col, val) in enumerate(updates.items(), start=3):
            cast = "::jsonb" if col == "roles" else ""
            set_parts.append(f"{col}=${i}{cast}")
            values.append(val)

        sql = (
            f"UPDATE staff SET {', '.join(set_parts)}, updated_at=NOW() "
            f"WHERE id=$1::uuid AND org_id=$2 "
            f"RETURNING id::text, org_id, name, username, role, roles, active, phone, "
            f"document_number, location_id, created_at, updated_at"
        )
        row = await conn.fetchrow(sql, staff_id, restaurant_id, *values)
    return _serialize(dict(row)) if row else None


async def db_delete_staff(staff_id: str, restaurant_id: int) -> bool:
    """Permanently deletes a staff member. Returns True if deleted.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        result = await conn.execute(
            "DELETE FROM staff WHERE id=$1::uuid AND org_id=$2",
            staff_id, restaurant_id,
        )
    return result.split()[-1] != "0"  # "DELETE N" → True si N > 0


# ── Self-service helpers (Staff HQ) ──────────────────────────────────────────

async def db_get_staff_profile(staff_id: str) -> dict | None:
    """Return full staff profile row for the Staff HQ self-service view.

    # Cross-tenant self-service lookup — uses bypass_tenant_scope internally.
    """
    with bypass_tenant_scope("staff_self_service_profile: UUID from verified staff JWT in sessions_repo; staff can only read their own row"):
        async with tenant_connection() as conn:
            row = await conn.fetchrow(
                "SELECT id::text, org_id, name, username, role, roles, active, phone, "
                "document_number, hourly_rate, photo_url FROM staff WHERE id=$1::uuid",
                staff_id,
            )
    if not row:
        return None
    d = dict(row)
    d["id"] = str(d["id"])
    _normalize_roles(d)
    return d


# ── Self-service: tips for a specific staff member ────────────────────────────

# ── Self-service: performance aggregate ──────────────────────────────────────

# ── Self-service: upcoming scheduled shifts ───────────────────────────────────
