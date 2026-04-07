"""
Staff repository — Fase 6 extraction from app.services.database.

Covers the staff/HR aggregate:
  - staff CRUD (roster, PIN login helpers)
  - clock-in / clock-out with attendance deductions
  - breaks (start, end, query)
  - schedules / weekly shifts (upsert, bulk, list, delete)
  - shift editing and timecard / overtime / attendance reports
  - tips: db_calculate_tip_pool, db_calculate_tips_by_attendance,
          db_save_tip_distribution, db_get_tip_distributions
  - manual deduction items
  - payroll (calculate, save run, get runs, approve)
  - contract templates (CRUD, assign to staff)
  - overtime requests (list, upsert, review)
  - WebAuthn credentials and challenges

Call sites that import via `app.services.database` continue to work through the
re-export shim added to that module.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.services.money import to_decimal, money_mul, money_sum, quantize_money, ZERO


# Lazy accessors — break circular import with app.services.database.
# database.py re-exports this module at module level, so a top-level import
# of database here would create a cycle. We resolve both helpers at call time.

async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()

def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _record_attendance_deduction(
    conn,
    shift_id: str,
    staff_id: str,
    restaurant_id: int,
    deduction_type: str,
    scheduled_time,
    actual_time,
    hourly_rate,  # Decimal (or anything coercible via to_decimal)
) -> None:
    """
    Insert an attendance_deductions row if the deviation exceeds 5 minutes.
    deduction_type: 'tardiness' | 'early_departure'
    scheduled_time: datetime.time object from asyncpg
    actual_time: timezone-aware datetime from asyncpg
    """
    from datetime import datetime, timedelta
    # Strip timezone for arithmetic (comparisons are done in the DB server's local representation)
    actual_naive = actual_time.replace(tzinfo=None)
    sched_dt = datetime.combine(actual_naive.date(), scheduled_time)

    if deduction_type == "tardiness":
        diff_seconds = (actual_naive - sched_dt).total_seconds()
    else:  # early_departure
        diff_seconds = (sched_dt - actual_naive).total_seconds()

    minutes_diff = int(diff_seconds / 60)
    if minutes_diff <= 5:
        return  # Within tolerance

    deduction_amount = quantize_money(money_mul(Decimal(minutes_diff) / Decimal("60"), hourly_rate))
    await conn.execute(
        """INSERT INTO attendance_deductions
           (shift_id, staff_id, restaurant_id, type, scheduled_time,
            actual_time, minutes_diff, deduction_amount)
           VALUES ($1::uuid, $2::uuid, $3, $4, $5::time, $6::timestamptz, $7, $8)""",
        shift_id, staff_id, restaurant_id, deduction_type,
        str(scheduled_time), actual_time, minutes_diff, deduction_amount,
    )


# ── Staff roster ─────────────────────────────────────────────────────────────

async def db_get_staff(restaurant_id: int) -> list:
    """Return all active (and inactive) staff members for a restaurant."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, restaurant_id, name, role, roles, active, phone, "
            "document_number, created_at, updated_at FROM staff "
            "WHERE restaurant_id=$1 ORDER BY name ASC",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_team_staff_by_branch(restaurant_id: int) -> list:
    """Return staff formatted for the Mi Equipo unified team view."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, name, role, roles, phone, active "
            "FROM staff WHERE restaurant_id=$1 ORDER BY name ASC",
            restaurant_id,
        )
    result = []
    for r in rows:
        d = dict(r)
        roles_list = d.get("roles") or []
        if not roles_list and d.get("role"):
            roles_list = [d["role"]]
        d["roles"] = roles_list
        d["source"] = "staff"
        d["branch_id"] = restaurant_id
        result.append(d)
    return result


async def db_get_staff_for_pin_login(restaurant_id: int, name: str) -> dict | None:
    """Return a staff member's record including pin hash for PIN authentication."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text, restaurant_id, name, role, roles, active, phone, pin, "
            "document_number, hourly_rate, photo_url "
            "FROM staff WHERE restaurant_id=$1 AND LOWER(name)=LOWER($2) AND active=true",
            restaurant_id, name,
        )
    if not row:
        return None
    d = dict(row)
    roles_list = d.get("roles") or []
    if not roles_list and d.get("role"):
        roles_list = [d["role"]]
    d["roles"] = roles_list
    return d


async def db_get_staff_candidates_by_name(name: str) -> list:
    """Retorna todos los staff activos con ese nombre (multi-restaurante).
    El caller verifica el PIN contra cada candidato para resolver colisiones."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, restaurant_id, name, role, roles, active, phone, pin, "
            "document_number, hourly_rate "
            "FROM staff WHERE LOWER(name)=LOWER($1) AND active=true "
            "ORDER BY restaurant_id",
            name,
        )
    result = []
    for row in rows:
        d = dict(row)
        roles_list = d.get("roles") or []
        if not roles_list and d.get("role"):
            roles_list = [d["role"]]
        d["roles"] = roles_list
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
) -> dict:
    """Insert a new staff member. Returns the created row."""
    if roles is None:
        roles = [role] if role else []
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO staff (restaurant_id, name, role, pin, phone, roles, document_number)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
               RETURNING id::text, restaurant_id, name, role, roles, active, phone,
                         document_number, created_at, updated_at""",
            restaurant_id, name, role, pin_hash, phone, json.dumps(roles), document_number,
        )
    return _serialize(dict(row))


async def db_update_staff(staff_id: str, restaurant_id: int, fields: dict) -> dict | None:
    """
    Update mutable staff fields (name, role, roles, pin, phone, active).
    Ignores unknown keys. Returns updated row or None if not found.
    Only updates columns that are explicitly passed in fields.
    All values are passed as parameters — no f-string SQL.
    """
    allowed = {"name", "role", "roles", "pin", "phone", "active", "document_number"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None

    # Serialize roles list to JSON string for JSONB column
    if "roles" in updates and isinstance(updates["roles"], list):
        updates["roles"] = json.dumps(updates["roles"])

    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Build SET clause with positional params starting at $3
        set_parts = []
        values = []
        for i, (col, val) in enumerate(updates.items(), start=3):
            cast = "::jsonb" if col == "roles" else ""
            set_parts.append(f"{col}=${i}{cast}")
            values.append(val)

        sql = (
            f"UPDATE staff SET {', '.join(set_parts)}, updated_at=NOW() "
            f"WHERE id=$1::uuid AND restaurant_id=$2 "
            f"RETURNING id::text, restaurant_id, name, role, roles, active, phone, "
            f"document_number, created_at, updated_at"
        )
        row = await conn.fetchrow(sql, staff_id, restaurant_id, *values)
    return _serialize(dict(row)) if row else None


async def db_delete_staff(staff_id: str, restaurant_id: int) -> bool:
    """Elimina permanentemente un miembro de staff. Retorna True si se eliminó."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM staff WHERE id=$1::uuid AND restaurant_id=$2",
            staff_id, restaurant_id,
        )
    return result.split()[-1] != "0"  # "DELETE N" → True si N > 0


# ── Clock-in / Clock-out ─────────────────────────────────────────────────────

async def db_clock_in(staff_id: str, restaurant_id: int) -> dict:
    """
    Open a new shift for staff_id.
    Raises ValueError if the employee already has an open shift.
    After inserting, checks staff_schedules for today's day and records
    a tardiness attendance_deduction if clock_in is > 5 min late.
    Returns the new shift dict.
    """
    import asyncpg as _asyncpg
    from datetime import datetime, timedelta
    pool = await _get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO staff_shifts (staff_id, restaurant_id)
                   VALUES ($1::uuid, $2)
                   RETURNING id::text, staff_id::text, restaurant_id,
                             clock_in, clock_out, notes, created_at""",
                staff_id, restaurant_id,
            )
        except _asyncpg.UniqueViolationError:
            raise ValueError("El empleado ya tiene un turno abierto.")

        shift = _serialize(dict(row))
        clock_in_dt = row["clock_in"]
        # 0=Monday in our system; Python weekday() also gives 0=Monday
        dow = clock_in_dt.weekday()

        sched = await conn.fetchrow(
            "SELECT start_time FROM staff_schedules "
            "WHERE staff_id=$1::uuid AND day_of_week=$2 AND active=true LIMIT 1",
            staff_id, dow,
        )
        if sched:
            staff_row = await conn.fetchrow(
                "SELECT hourly_rate FROM staff WHERE id=$1::uuid", staff_id
            )
            hourly_rate = to_decimal(staff_row["hourly_rate"] or 0) if staff_row else ZERO
            await _record_attendance_deduction(
                conn,
                shift_id=str(row["id"]),
                staff_id=staff_id,
                restaurant_id=restaurant_id,
                deduction_type="tardiness",
                scheduled_time=sched["start_time"],
                actual_time=clock_in_dt,
                hourly_rate=hourly_rate,
            )
    return shift


async def db_clock_out(staff_id: str, restaurant_id: int) -> dict | None:
    """
    Close the open shift for staff_id.
    After updating, checks staff_schedules for today's day and records
    an early_departure attendance_deduction if clock_out is > 5 min early.
    Returns the updated shift, or None if no open shift was found.
    """
    from datetime import datetime, timedelta
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE staff_shifts
               SET clock_out = NOW()
               WHERE staff_id = $1::uuid
                 AND restaurant_id = $2
                 AND clock_out IS NULL
               RETURNING id::text, staff_id::text, restaurant_id,
                         clock_in, clock_out, notes, created_at""",
            staff_id, restaurant_id,
        )
        if not row:
            return None

        shift = _serialize(dict(row))
        clock_out_dt = row["clock_out"]
        dow = clock_out_dt.weekday()

        sched = await conn.fetchrow(
            "SELECT end_time FROM staff_schedules "
            "WHERE staff_id=$1::uuid AND day_of_week=$2 AND active=true LIMIT 1",
            staff_id, dow,
        )
        if sched:
            staff_row = await conn.fetchrow(
                "SELECT hourly_rate FROM staff WHERE id=$1::uuid", staff_id
            )
            hourly_rate = to_decimal(staff_row["hourly_rate"] or 0) if staff_row else ZERO
            await _record_attendance_deduction(
                conn,
                shift_id=str(row["id"]),
                staff_id=staff_id,
                restaurant_id=restaurant_id,
                deduction_type="early_departure",
                scheduled_time=sched["end_time"],
                actual_time=clock_out_dt,
                hourly_rate=hourly_rate,
            )
    return shift


async def db_get_open_shifts(restaurant_id: int) -> list:
    """Return all currently open shifts for a restaurant, joined with staff info."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ss.id::text, ss.staff_id::text, ss.clock_in,
                      s.name AS staff_name, s.role AS staff_role
               FROM staff_shifts ss
               JOIN staff s ON ss.staff_id = s.id
               WHERE ss.restaurant_id=$1 AND ss.clock_out IS NULL
               ORDER BY ss.clock_in ASC""",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_shifts(
    restaurant_id: int,
    date_from: str,
    date_to: str,
) -> list:
    """Return closed and open shifts in [date_from, date_to] with staff name/role."""
    from datetime import datetime, timezone
    def _parse_dt(s: str):
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)

    dt_from = _parse_dt(date_from)
    dt_to   = _parse_dt(date_to)

    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ss.id::text, ss.staff_id::text, ss.clock_in, ss.clock_out,
                      ss.notes,
                      s.name AS staff_name, s.role AS staff_role,
                      EXTRACT(EPOCH FROM (
                          COALESCE(ss.clock_out, NOW()) - ss.clock_in
                      )) / 3600.0 AS hours_worked
               FROM staff_shifts ss
               JOIN staff s ON ss.staff_id = s.id
               WHERE ss.restaurant_id=$1
                 AND ss.clock_in >= $2
                 AND ss.clock_in <  $3
               ORDER BY ss.clock_in DESC""",
            restaurant_id, dt_from, dt_to,
        )
    return [_serialize(dict(r)) for r in rows]


# ── Tip pool calculation ─────────────────────────────────────────────────────

async def db_calculate_tip_pool(
    restaurant_id: int,
    period_start: str,
    period_end: str,
    total_tips: float,
) -> dict:
    """
    Calculate tip distribution for [period_start, period_end].

    Algorithm:
      1. Read pct_config from restaurants.features->'tip_distribution'.
         Expected format: {"mesero": 50, "cocina": 30, "bar": 20}
      2. Find all shifts that overlap the period and compute effective hours
         within the window (PostgreSQL handles all timestamp math).
      3. For each configured role: distribute role_pct% of total_tips
         proportionally to hours worked among employees in that role.

    Returns:
      {
        "pct_config": {"mesero": 50, ...},
        "entries":    [{"staff_id", "name", "role", "hours", "amount", "pct"}],
        "total_allocated":   float,
        "total_unallocated": float
      }
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # 1. Read tip_distribution config — single parametrized query
        pct_val = await conn.fetchval(
            "SELECT features->'tip_distribution' FROM restaurants WHERE id=$1",
            restaurant_id,
        )
        pct_config: dict = pct_val if isinstance(pct_val, dict) else {}

        if not pct_config:
            return {
                "pct_config": {},
                "entries": [],
                "total_allocated": 0.0,
                "total_unallocated": float(quantize_money(total_tips)),
            }

        # 2. Compute effective hours per employee within the period.
        # LEAST/GREATEST/COALESCE handle partial overlap and open shifts cleanly.
        rows = await conn.fetch(
            """
            SELECT
                ss.staff_id::text,
                s.name,
                s.role,
                ROUND(
                    CAST(SUM(
                        EXTRACT(EPOCH FROM (
                            LEAST(COALESCE(ss.clock_out, $3::timestamptz), $3::timestamptz)
                            - GREATEST(ss.clock_in, $2::timestamptz)
                        ))
                    ) / 3600.0 AS numeric), 2
                ) AS effective_hours
            FROM staff_shifts ss
            JOIN staff s ON ss.staff_id = s.id
            WHERE ss.restaurant_id = $1
              AND ss.clock_in  < $3::timestamptz
              AND (ss.clock_out > $2::timestamptz OR ss.clock_out IS NULL)
            GROUP BY ss.staff_id, s.name, s.role
            HAVING SUM(
                EXTRACT(EPOCH FROM (
                    LEAST(COALESCE(ss.clock_out, $3::timestamptz), $3::timestamptz)
                    - GREATEST(ss.clock_in, $2::timestamptz)
                ))
            ) > 0
            """,
            restaurant_id, period_start, period_end,
        )

        # 3. Distribute tips by role
        entries = []
        total_allocated = ZERO
        total_tips_d = to_decimal(total_tips)

        for role, pct in pct_config.items():
            role_emps = [r for r in rows if r["role"] == role]
            if not role_emps:
                continue  # role has no hours in this period — skip

            role_pool = money_mul(total_tips_d, to_decimal(pct) / Decimal("100"))
            total_role_hours = money_sum(r["effective_hours"] for r in role_emps)

            for emp in role_emps:
                h = to_decimal(emp["effective_hours"])
                if total_role_hours > ZERO:
                    amount = money_mul(role_pool, h / total_role_hours)
                else:
                    amount = role_pool / Decimal(len(role_emps))

                entries.append({
                    "staff_id": emp["staff_id"],
                    "name":     emp["name"],
                    "role":     role,
                    "hours":    float(emp["effective_hours"]),  # display only
                    "amount":   float(quantize_money(amount)),  # JSON boundary
                    "pct":      pct,
                })
                total_allocated += amount

    return {
        "pct_config":        pct_config,
        "entries":           entries,
        "total_allocated":   float(quantize_money(total_allocated)),   # JSON boundary
        "total_unallocated": float(quantize_money(total_tips_d - total_allocated)),  # JSON boundary
    }


async def db_calculate_tips_by_attendance(
    restaurant_id: int,
    period_start: str,
    period_end: str,
    branch_id: int | None = None,
) -> dict:
    """
    Distribute tips from table_checks to staff based on who was clocked-in
    when each ticket was paid. Uses features.tip_distribution % config.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Get tip distribution config
        rest = await conn.fetchrow(
            "SELECT features FROM restaurants WHERE id=$1", restaurant_id
        )
        features = rest["features"] or {} if rest else {}
        if isinstance(features, str):
            import json as _j
            try:
                features = _j.loads(features)
            except Exception:
                features = {}
        pct_config = features.get("tip_distribution", {})
        if not pct_config:
            return {"entries": [], "total_tips": 0, "unallocated": 0, "pct_config": {}}

        # Resolve restaurant scope (branch or matrix + branches)
        if branch_id:
            rest_ids = [branch_id]
        else:
            branches = await conn.fetch(
                "SELECT id FROM restaurants WHERE id=$1 OR parent_restaurant_id=$1",
                restaurant_id,
            )
            rest_ids = [r["id"] for r in branches]

        # Fetch all paid checks in period with tip_amount > 0
        checks = await conn.fetch(
            """SELECT tc.id, tc.tip_amount, tc.paid_at
               FROM table_checks tc
               JOIN table_orders to2 ON to2.id = tc.base_order_id
               WHERE tc.paid_at >= $1::timestamptz
                 AND tc.paid_at < $2::timestamptz
                 AND tc.tip_amount > 0
                 AND tc.status = 'invoiced'
                 AND COALESCE(to2.branch_id, $3) = ANY($4::int[])""",
            period_start, period_end, restaurant_id, rest_ids,
        )

        if not checks:
            return {"entries": [], "total_tips": 0, "unallocated": 0, "pct_config": pct_config}

        # For each check, find staff on shift at paid_at
        totals: dict[str, dict] = {}  # staff_id -> {name, role, tickets, amount}
        total_tips = ZERO
        unallocated = ZERO

        for chk in checks:
            tip = to_decimal(chk["tip_amount"])
            total_tips += tip
            paid_at = chk["paid_at"]

            on_shift = await conn.fetch(
                """SELECT ss.staff_id::text, s.name, s.role
                   FROM staff_shifts ss
                   JOIN staff s ON s.id = ss.staff_id
                   WHERE ss.restaurant_id = ANY($1::int[])
                     AND ss.clock_in <= $2
                     AND (ss.clock_out IS NULL OR ss.clock_out >= $2)
                     AND s.role = ANY($3::text[])""",
                rest_ids, paid_at, list(pct_config.keys()),
            )

            if not on_shift:
                unallocated += tip
                continue

            # Group by role
            role_staff: dict[str, list[dict]] = {}
            for s in on_shift:
                role_staff.setdefault(s["role"], []).append(dict(s))

            # Redistribute % among present roles only
            present_roles = list(role_staff.keys())
            total_pct = to_decimal(sum(pct_config.get(r, 0) for r in present_roles))
            if total_pct == ZERO:
                unallocated += tip
                continue

            for role, members in role_staff.items():
                role_pct = to_decimal(pct_config.get(role, 0))
                role_share = money_mul(tip, role_pct / total_pct)
                per_person = role_share / Decimal(len(members))
                for m in members:
                    sid = m["staff_id"]
                    if sid not in totals:
                        totals[sid] = {
                            "staff_id": sid,
                            "name": m["name"],
                            "role": role,
                            "tickets_contributed": 0,
                            "total_tips": ZERO,
                        }
                    totals[sid]["tickets_contributed"] += 1
                    totals[sid]["total_tips"] += per_person

        # Quantize at JSON boundary
        for entry in totals.values():
            entry["total_tips"] = float(quantize_money(entry["total_tips"]))

        entries = sorted(totals.values(), key=lambda x: x["total_tips"], reverse=True)
        return {
            "entries": entries,
            "total_tips": float(quantize_money(total_tips)),
            "unallocated": float(quantize_money(unallocated)),
            "pct_config": pct_config,
        }


async def db_save_tip_distribution(
    restaurant_id: int,
    period_start: str,
    period_end: str,
    total_tips: float,
    distribution: list,
    pct_config: dict,
    created_by: str,
) -> dict:
    """Persist a tip distribution cut. Returns the saved row."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO tip_distributions
               (restaurant_id, period_start, period_end,
                total_tips, distribution, pct_config, created_by)
               VALUES ($1, $2::timestamptz, $3::timestamptz,
                       $4, $5::jsonb, $6::jsonb, $7)
               RETURNING id::text, restaurant_id, period_start, period_end,
                         total_tips, distribution, pct_config, created_by, created_at""",
            restaurant_id,
            period_start,
            period_end,
            to_decimal(total_tips),
            json.dumps(distribution),
            json.dumps(pct_config),
            created_by,
        )
    return _serialize(dict(row))


async def db_get_tip_distributions(restaurant_id: int, limit: int = 20) -> list:
    """Return recent tip distribution cuts for a restaurant."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, restaurant_id, period_start, period_end,
                      total_tips, distribution, pct_config, created_by, created_at
               FROM tip_distributions
               WHERE restaurant_id=$1
               ORDER BY created_at DESC
               LIMIT $2""",
            restaurant_id, limit,
        )
    return [_serialize(dict(r)) for r in rows]


# ── WebAuthn ─────────────────────────────────────────────────────────────────

async def db_save_webauthn_credential(
    staff_id: str,
    credential_id: str,
    public_key: str,
    sign_count: int,
    transports: list,
    device_name: str = "",
) -> dict:
    """Store a WebAuthn credential for a staff member."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO webauthn_credentials
                   (staff_id, credential_id, public_key, sign_count, transports, device_name)
               VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6)
               RETURNING id::text, staff_id::text, credential_id,
                         sign_count, transports, device_name, created_at""",
            staff_id, credential_id, public_key, sign_count,
            json.dumps(transports), device_name,
        )
        return _serialize(dict(row))


async def db_get_webauthn_credentials_by_staff(staff_id: str) -> list:
    """Return all WebAuthn credentials registered for a staff member."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, staff_id::text, credential_id,
                      sign_count, transports, device_name, created_at
               FROM webauthn_credentials
               WHERE staff_id = $1::uuid
               ORDER BY created_at DESC""",
            staff_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_webauthn_credentials_by_restaurant(restaurant_id: int) -> list:
    """Get all WebAuthn credentials for a restaurant (for authentication ceremony)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT wc.id::text, wc.staff_id::text, wc.credential_id, wc.public_key,
                      wc.sign_count, wc.transports, wc.device_name, s.name AS staff_name
               FROM webauthn_credentials wc
               JOIN staff s ON wc.staff_id = s.id
               WHERE s.restaurant_id = $1 AND s.active = TRUE
               ORDER BY wc.created_at DESC""",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_webauthn_credential(credential_id: str) -> dict | None:
    """Get a single credential by its credential_id (base64url string)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT wc.id::text, wc.staff_id::text, wc.credential_id, wc.public_key,
                      wc.sign_count, wc.transports, s.restaurant_id, s.name AS staff_name
               FROM webauthn_credentials wc
               JOIN staff s ON wc.staff_id = s.id
               WHERE wc.credential_id = $1""",
            credential_id,
        )
    return _serialize(dict(row)) if row else None


async def db_update_webauthn_sign_count(credential_id: str, new_count: int) -> None:
    """Update the sign counter after a successful assertion."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE webauthn_credentials SET sign_count = $1 WHERE credential_id = $2",
            new_count, credential_id,
        )


async def db_delete_webauthn_credential(credential_id: str) -> bool:
    """Delete a credential. Returns True if a row was deleted."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM webauthn_credentials WHERE credential_id = $1", credential_id
        )
    return "DELETE 1" in result


async def db_save_webauthn_challenge(
    challenge: str,
    staff_id: str | None,
    challenge_type: str,
    restaurant_id: int,
) -> None:
    """Persist a WebAuthn challenge (registration or authentication)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO webauthn_challenges (challenge, staff_id, type, restaurant_id)
               VALUES ($1, $2, $3, $4)""",
            challenge, staff_id, challenge_type, restaurant_id,
        )


async def db_consume_webauthn_challenge(challenge: str) -> dict | None:
    """Atomically get and delete a challenge. Returns None if expired (>5 min) or not found."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """DELETE FROM webauthn_challenges
               WHERE challenge = $1
                 AND created_at > NOW() - INTERVAL '5 minutes'
               RETURNING id::text, challenge, staff_id::text, type, restaurant_id""",
            challenge,
        )
    return _serialize(dict(row)) if row else None


async def db_cleanup_expired_challenges() -> None:
    """Remove all WebAuthn challenges older than 5 minutes."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM webauthn_challenges WHERE created_at < NOW() - INTERVAL '5 minutes'"
        )


# ── Breaks ────────────────────────────────────────────────────────────────────

async def db_start_break(staff_id: str, shift_id: str) -> dict:
    """Start a break for an employee. Raises ValueError if a break is already open."""
    import asyncpg as _asyncpg
    pool = await _get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO staff_breaks (staff_id, shift_id)
                   VALUES ($1::uuid, $2::uuid)
                   RETURNING id::text, staff_id::text, shift_id::text,
                             break_start, break_end, notes, created_at""",
                staff_id, shift_id,
            )
            return _serialize(dict(row))
        except _asyncpg.UniqueViolationError:
            raise ValueError("El empleado ya tiene un break abierto.")


async def db_end_break(staff_id: str) -> dict | None:
    """End the current open break. Returns None if no open break exists."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE staff_breaks SET break_end = NOW()
               WHERE staff_id = $1::uuid AND break_end IS NULL
               RETURNING id::text, staff_id::text, shift_id::text,
                         break_start, break_end, notes""",
            staff_id,
        )
    return _serialize(dict(row)) if row else None


async def db_get_breaks_for_shift(shift_id: str) -> list:
    """Return all breaks for a given shift ordered chronologically."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, staff_id::text, shift_id::text,
                      break_start, break_end, notes
               FROM staff_breaks
               WHERE shift_id = $1::uuid
               ORDER BY break_start""",
            shift_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_open_break(staff_id: str) -> dict | None:
    """Return the currently open break for a staff member, or None."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id::text, staff_id::text, shift_id::text, break_start, notes
               FROM staff_breaks
               WHERE staff_id = $1::uuid AND break_end IS NULL""",
            staff_id,
        )
    return _serialize(dict(row)) if row else None


# ── Schedules ─────────────────────────────────────────────────────────────────

async def db_upsert_schedule(
    staff_id: str,
    restaurant_id: int,
    day_of_week: int,
    start_time: str,
    end_time: str,
) -> dict:
    """Create or replace the schedule for a staff member on a specific day."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM staff_schedules WHERE staff_id = $1::uuid AND day_of_week = $2",
            staff_id, day_of_week,
        )
        row = await conn.fetchrow(
            """INSERT INTO staff_schedules
                   (staff_id, restaurant_id, day_of_week, start_time, end_time)
               VALUES ($1::uuid, $2, $3, $4, $5)
               RETURNING id::text, staff_id::text, restaurant_id,
                         day_of_week, start_time::text, end_time::text, active""",
            staff_id, restaurant_id, day_of_week, start_time, end_time,
        )
        return _serialize(dict(row))


async def db_bulk_upsert_schedules(
    entries: list[dict],
    restaurant_id: int,
) -> list[dict]:
    """Bulk create/update schedules. Each entry: {staff_id, day_of_week, start_time, end_time}."""
    pool = await _get_pool()
    results = []
    async with pool.acquire() as conn:
        for entry in entries:
            await conn.execute(
                "DELETE FROM staff_schedules WHERE staff_id = $1::uuid AND day_of_week = $2",
                entry["staff_id"], entry["day_of_week"],
            )
            row = await conn.fetchrow(
                """INSERT INTO staff_schedules (staff_id, restaurant_id, day_of_week, start_time, end_time)
                   VALUES ($1::uuid, $2, $3, $4, $5)
                   RETURNING id::text, staff_id::text, restaurant_id, day_of_week,
                             start_time::text, end_time::text""",
                entry["staff_id"], restaurant_id, entry["day_of_week"],
                entry["start_time"], entry["end_time"],
            )
            if row:
                results.append(_serialize(dict(row)))
    return results


async def db_get_schedules(restaurant_id: int) -> list:
    """Return all active schedules for a restaurant with staff info."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ss.id::text, ss.staff_id::text, s.name AS staff_name,
                      s.role AS staff_role, ss.day_of_week,
                      ss.start_time::text, ss.end_time::text, ss.active
               FROM staff_schedules ss
               JOIN staff s ON ss.staff_id = s.id
               WHERE ss.restaurant_id = $1 AND ss.active = TRUE
               ORDER BY s.name, ss.day_of_week""",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_delete_schedule(schedule_id: str) -> bool:
    """Delete a schedule entry. Returns True if a row was deleted."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM staff_schedules WHERE id = $1::uuid", schedule_id
        )
    return "DELETE 1" in result


# ── Shift editing ─────────────────────────────────────────────────────────────

async def db_edit_shift(
    shift_id: str,
    restaurant_id: int,
    clock_in=None,
    clock_out=None,
    notes=None,
) -> dict | None:
    """Admin edit of shift times/notes. Only updates provided fields."""
    pool = await _get_pool()
    parts: list[str] = []
    params: list = []
    idx = 1
    if clock_in is not None:
        parts.append(f"clock_in = ${idx}::timestamptz")
        params.append(clock_in)
        idx += 1
    if clock_out is not None:
        parts.append(f"clock_out = ${idx}::timestamptz")
        params.append(clock_out)
        idx += 1
    if notes is not None:
        parts.append(f"notes = ${idx}")
        params.append(notes)
        idx += 1
    if not parts:
        return None
    params.append(shift_id)
    params.append(restaurant_id)
    query = (
        f"UPDATE staff_shifts SET {', '.join(parts)} "
        f"WHERE id = ${idx}::uuid AND restaurant_id = ${idx + 1} "
        "RETURNING id::text, staff_id::text, restaurant_id, clock_in, clock_out, notes"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)
    return _serialize(dict(row)) if row else None


# ── Timecard & Overtime ───────────────────────────────────────────────────────

async def db_get_timecard(
    restaurant_id: int,
    week_start: str,
    week_end: str,
) -> list:
    """Weekly timecard: per employee per day — gross hours minus break time."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                 ss.staff_id::text,
                 s.name  AS staff_name,
                 s.role  AS staff_role,
                 DATE(ss.clock_in) AS work_date,
                 ROUND(SUM(
                   EXTRACT(EPOCH FROM (COALESCE(ss.clock_out, NOW()) - ss.clock_in))
                   / 3600.0
                 )::numeric, 2) AS gross_hours,
                 COALESCE((
                   SELECT ROUND(SUM(
                     EXTRACT(EPOCH FROM (COALESCE(sb.break_end, NOW()) - sb.break_start))
                     / 3600.0
                   )::numeric, 2)
                   FROM staff_breaks sb
                   WHERE sb.shift_id = ANY(ARRAY_AGG(ss.id))
                 ), 0) AS break_hours
               FROM staff_shifts ss
               JOIN staff s ON ss.staff_id = s.id
               WHERE ss.restaurant_id = $1
                 AND ss.clock_in >= $2::timestamptz
                 AND ss.clock_in <  $3::timestamptz
               GROUP BY ss.staff_id, s.name, s.role, DATE(ss.clock_in)
               ORDER BY s.name, work_date""",
            restaurant_id, week_start, week_end,
        )
    result = []
    for r in rows:
        d = _serialize(dict(r))
        gross = float(d.get("gross_hours", 0))
        brk   = float(d.get("break_hours", 0))
        d["net_hours"] = round(gross - brk, 2)  # hours: float is fine here
        result.append(d)
    return result


async def db_get_overtime_report(
    restaurant_id: int,
    date_from: str,
    date_to: str,
    daily_threshold: float = 8.0,
    weekly_threshold: float = 40.0,
) -> list:
    """Overtime report: identifies daily and weekly overtime per employee."""
    timecard = await db_get_timecard(restaurant_id, date_from, date_to)

    by_staff: dict = {}
    for entry in timecard:
        sid = entry["staff_id"]
        if sid not in by_staff:
            by_staff[sid] = {
                "staff_id":    sid,
                "staff_name":  entry["staff_name"],
                "staff_role":  entry["staff_role"],
                "days":        [],
                "total_hours": 0.0,
                "regular_hours":  0.0,
                "overtime_hours": 0.0,
            }
        by_staff[sid]["days"].append(entry)
        by_staff[sid]["total_hours"] += entry["net_hours"]

    result = []
    for sid, data in by_staff.items():
        daily_ot  = sum(max(0.0, d["net_hours"] - daily_threshold) for d in data["days"])
        weekly_ot = max(0.0, data["total_hours"] - weekly_threshold)
        ot = max(daily_ot, weekly_ot)
        data["overtime_hours"] = round(ot, 2)
        data["regular_hours"]  = round(data["total_hours"] - ot, 2)
        result.append(data)
    return result


async def db_get_attendance_report(
    restaurant_id: int,
    date_from: str,
    date_to: str,
) -> list:
    """Compare actual clock-in times with scheduled times to identify lateness."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                 ss.staff_id::text,
                 s.name  AS staff_name,
                 s.role  AS staff_role,
                 ss.clock_in,
                 ss.clock_out,
                 sch.start_time AS scheduled_start,
                 sch.end_time   AS scheduled_end,
                 EXTRACT(DOW FROM ss.clock_in) AS dow
               FROM staff_shifts ss
               JOIN staff s ON ss.staff_id = s.id
               LEFT JOIN staff_schedules sch
                 ON sch.staff_id    = ss.staff_id
                AND sch.day_of_week = EXTRACT(ISODOW FROM ss.clock_in)::int - 1
                AND sch.active      = TRUE
               WHERE ss.restaurant_id = $1
                 AND ss.clock_in >= $2::timestamptz
                 AND ss.clock_in <  $3::timestamptz
               ORDER BY ss.clock_in DESC""",
            restaurant_id, date_from, date_to,
        )
    result = []
    for r in rows:
        d = _serialize(dict(r))
        clock_in = d.get("clock_in")
        sched    = d.get("scheduled_start")
        if clock_in and sched:
            if hasattr(clock_in, "time"):
                from datetime import time as _dt_time
                actual_time = clock_in.time()
                if hasattr(sched, "hour"):
                    diff_minutes = (
                        actual_time.hour * 60 + actual_time.minute
                        - sched.hour * 60 - sched.minute
                    )
                    d["late_minutes"] = max(0, diff_minutes)
                    d["status"] = "late" if diff_minutes > 5 else "on_time"
                else:
                    d["late_minutes"] = 0
                    d["status"] = "no_schedule"
            else:
                d["late_minutes"] = 0
                d["status"] = "no_schedule"
        else:
            d["late_minutes"] = 0
            d["status"] = "no_schedule"
        result.append(d)
    return result


# ── Deduction items (manual) ─────────────────────────────────────────────────

async def db_list_deduction_items(staff_id: str, restaurant_id: int) -> list:
    """List all deduction items for a staff member."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, staff_id::text, restaurant_id, category, label,
                      type, amount, active, created_at
               FROM staff_deduction_items
               WHERE staff_id=$1::uuid AND restaurant_id=$2
               ORDER BY created_at ASC""",
            staff_id, restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_create_deduction_item(
    staff_id: str,
    restaurant_id: int,
    category: str,
    label: str,
    item_type: str,
    amount: float,
) -> dict:
    """Create a manual deduction item."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO staff_deduction_items
               (staff_id, restaurant_id, category, label, type, amount)
               VALUES ($1::uuid, $2, $3, $4, $5, $6)
               RETURNING id::text, staff_id::text, restaurant_id, category, label,
                         type, amount, active, created_at""",
            staff_id, restaurant_id, category, label, item_type, to_decimal(amount),
        )
    return _serialize(dict(row))


async def db_update_deduction_item(
    item_id: str,
    restaurant_id: int,
    patch: dict,
) -> dict | None:
    """Update a deduction item. patch keys: label, category, type, amount, active."""
    allowed = {"label", "category", "type", "amount", "active"}
    updates = {k: v for k, v in patch.items() if k in allowed}
    if not updates:
        return None
    pool = await _get_pool()
    set_parts = []
    values: list = [item_id, restaurant_id]
    idx = 3
    for col, val in updates.items():
        set_parts.append(f"{col} = ${idx}")
        values.append(val)
        idx += 1
    sql = (
        f"UPDATE staff_deduction_items SET {', '.join(set_parts)} "
        f"WHERE id=$1::uuid AND restaurant_id=$2 "
        f"RETURNING id::text, staff_id::text, restaurant_id, category, label, "
        f"type, amount, active, created_at"
    )
    async with (await _get_pool()).acquire() as conn:
        row = await conn.fetchrow(sql, *values)
    return _serialize(dict(row)) if row else None


async def db_delete_deduction_item(item_id: str, restaurant_id: int) -> bool:
    """Delete a deduction item. Returns True if deleted."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM staff_deduction_items WHERE id=$1::uuid AND restaurant_id=$2",
            item_id, restaurant_id,
        )
    return result.split()[-1] != "0"


# ── Payroll ───────────────────────────────────────────────────────────────────

async def db_calculate_payroll(
    restaurant_id: int,
    period_start: str,
    period_end: str,
    config: dict | None = None,
) -> list:
    """Calculate payroll for all active staff in a period.

    config keys (all optional):
      overtime_daily_threshold  — default 8.0 h
      overtime_weekly_threshold — default 40.0 h
      overtime_multiplier       — default 1.5
    """
    if config is None:
        config = {}
    daily_threshold  = config.get("overtime_daily_threshold", 8.0)
    weekly_threshold = config.get("overtime_weekly_threshold", 40.0)
    ot_multiplier    = config.get("overtime_multiplier", 1.5)

    pool = await _get_pool()
    async with pool.acquire() as conn:
        staff_rows = await conn.fetch(
            """SELECT id::text, name, role, hourly_rate, deductions
               FROM staff WHERE restaurant_id = $1 AND active = TRUE""",
            restaurant_id,
        )

    overtime_data  = await db_get_overtime_report(
        restaurant_id, period_start, period_end, daily_threshold, weekly_threshold
    )
    hours_by_staff = {d["staff_id"]: d for d in overtime_data}

    tips_result = await db_calculate_tips_by_attendance(
        restaurant_id, period_start, period_end
    )
    tips_by_staff: dict = {
        e["staff_id"]: to_decimal(e["total_tips"])
        for e in tips_result.get("entries", [])
    }
    ot_multiplier_d = to_decimal(ot_multiplier)

    entries = []
    for sr in staff_rows:
        s    = _serialize(dict(sr))
        sid  = s["id"]
        rate = to_decimal(s.get("hourly_rate") or 0)

        hours_data = hours_by_staff.get(sid, {})
        regular    = to_decimal(hours_data.get("regular_hours", 0))
        overtime   = to_decimal(hours_data.get("overtime_hours", 0))

        gross        = money_mul(regular, rate) + money_mul(money_mul(overtime, rate), ot_multiplier_d)
        tip_earnings = tips_by_staff.get(sid, ZERO)
        total_comp   = gross + tip_earnings

        deductions_cfg = s.get("deductions") or {}
        if isinstance(deductions_cfg, str):
            deductions_cfg = json.loads(deductions_cfg)

        deductions       = {}
        total_deductions = ZERO
        for ded_name, ded_pct in deductions_cfg.items():
            amt = quantize_money(money_mul(total_comp, to_decimal(ded_pct) / Decimal("100")))
            deductions[ded_name] = float(amt)  # JSON boundary
            total_deductions    += amt

        net_pay = quantize_money(total_comp - total_deductions)
        entries.append({
            "staff_id":           sid,
            "name":               s["name"],
            "role":               s["role"],
            "regular_hours":      float(regular),   # display only
            "overtime_hours":     float(overtime),  # display only
            "hourly_rate":        float(rate),       # display only
            "gross_pay":          float(quantize_money(gross)),
            "tip_earnings":       float(quantize_money(tip_earnings)),
            "total_compensation": float(quantize_money(total_comp)),
            "deductions":         deductions,
            "total_deductions":   float(quantize_money(total_deductions)),
            "net_pay":            float(net_pay),
        })
    return entries


async def db_save_payroll_run(
    restaurant_id: int,
    period_start: str,
    period_end: str,
    snapshot: list,
    config: dict,
    created_by: str = "",
) -> dict:
    """Persist a payroll run snapshot. Returns the saved row."""
    total_gross = money_sum(e.get("gross_pay", 0) for e in snapshot)
    total_net   = money_sum(e.get("net_pay",   0) for e in snapshot)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO payroll_runs
                   (restaurant_id, period_start, period_end,
                    snapshot, config, total_gross, total_net, created_by)
               VALUES ($1, $2::date, $3::date, $4::jsonb, $5::jsonb, $6, $7, $8)
               RETURNING id::text, restaurant_id, period_start, period_end,
                         status, total_gross, total_net, created_by, created_at""",
            restaurant_id, period_start, period_end,
            json.dumps(snapshot), json.dumps(config),
            total_gross, total_net, created_by,
        )
    return _serialize(dict(row))


async def db_get_payroll_runs(restaurant_id: int, limit: int = 20) -> list:
    """Return recent payroll runs for a restaurant."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, restaurant_id, period_start, period_end,
                      status, total_gross, total_net, created_by, created_at, approved_at
               FROM payroll_runs
               WHERE restaurant_id = $1
               ORDER BY created_at DESC
               LIMIT $2""",
            restaurant_id, limit,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_payroll_run(run_id: str, restaurant_id: int) -> dict | None:
    """Return a single payroll run including its snapshot."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id::text, restaurant_id, period_start, period_end,
                      status, snapshot, config, total_gross, total_net,
                      created_by, created_at, approved_at
               FROM payroll_runs
               WHERE id = $1::uuid AND restaurant_id = $2""",
            run_id, restaurant_id,
        )
    return _serialize(dict(row)) if row else None


async def db_approve_payroll_run(run_id: str, restaurant_id: int) -> dict | None:
    """Approve a draft payroll run. Returns None if not found or already approved."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE payroll_runs
               SET status = 'approved', approved_at = NOW()
               WHERE id = $1::uuid AND restaurant_id = $2 AND status = 'draft'
               RETURNING id::text, status, approved_at""",
            run_id, restaurant_id,
        )
    return _serialize(dict(row)) if row else None


# ── Contract templates ────────────────────────────────────────────────────────

async def db_list_contract_templates(restaurant_id: int) -> list:
    """Return all contract templates for a restaurant."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, restaurant_id, name, weekly_hours, monthly_salary,
                      pay_period, transport_subsidy, arl_pct, health_pct, pension_pct,
                      other_benefits, breaks_billable, lunch_billable, lunch_minutes,
                      active, created_at
               FROM contract_templates
               WHERE restaurant_id = $1
               ORDER BY active DESC, name""",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_create_contract_template(restaurant_id: int, data: dict) -> dict:
    """Create a new contract template."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO contract_templates
               (restaurant_id, name, weekly_hours, monthly_salary, pay_period,
                transport_subsidy, arl_pct, health_pct, pension_pct,
                other_benefits, breaks_billable, lunch_billable, lunch_minutes)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               RETURNING id::text, restaurant_id, name, weekly_hours, monthly_salary,
                         pay_period, transport_subsidy, arl_pct, health_pct, pension_pct,
                         other_benefits, breaks_billable, lunch_billable, lunch_minutes,
                         active, created_at""",
            restaurant_id,
            data["name"],
            float(data.get("weekly_hours", 44)),          # hours — float ok
            to_decimal(data.get("monthly_salary", 0)),
            data.get("pay_period", "biweekly"),
            to_decimal(data.get("transport_subsidy", 0)),
            to_decimal(data.get("arl_pct", "0.00522")),   # pct fraction stored as NUMERIC
            to_decimal(data.get("health_pct", "0.04")),
            to_decimal(data.get("pension_pct", "0.04")),
            json.dumps(data.get("other_benefits", {})),
            bool(data.get("breaks_billable", True)),
            bool(data.get("lunch_billable", False)),
            int(data.get("lunch_minutes", 60)),
        )
    return _serialize(dict(row))


async def db_update_contract_template(template_id: str, restaurant_id: int, data: dict) -> dict | None:
    """Update allowed fields of a contract template."""
    allowed = {
        "name", "weekly_hours", "monthly_salary", "pay_period",
        "transport_subsidy", "arl_pct", "health_pct", "pension_pct",
        "other_benefits", "breaks_billable", "lunch_billable", "lunch_minutes", "active",
    }
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return None
    pool = await _get_pool()
    set_parts = []
    values: list = [template_id, restaurant_id]
    idx = 3
    for col, val in updates.items():
        set_parts.append(f"{col} = ${idx}")
        values.append(val)
        idx += 1
    sql = (
        f"UPDATE contract_templates SET {', '.join(set_parts)} "
        f"WHERE id=$1::uuid AND restaurant_id=$2 "
        f"RETURNING id::text, restaurant_id, name, weekly_hours, monthly_salary, pay_period, "
        f"transport_subsidy, arl_pct, health_pct, pension_pct, other_benefits, "
        f"breaks_billable, lunch_billable, lunch_minutes, active, created_at"
    )
    async with (await _get_pool()).acquire() as conn:
        row = await conn.fetchrow(sql, *values)
    return _serialize(dict(row)) if row else None


async def db_delete_contract_template(template_id: str, restaurant_id: int) -> bool:
    """Delete a contract template (only if no staff assigned)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        in_use = await conn.fetchval(
            "SELECT COUNT(*) FROM staff WHERE contract_template_id=$1::uuid",
            template_id,
        )
        if in_use:
            return False
        result = await conn.execute(
            "DELETE FROM contract_templates WHERE id=$1::uuid AND restaurant_id=$2",
            template_id, restaurant_id,
        )
    return result.split()[-1] != "0"


async def db_assign_staff_contract(
    staff_id: str,
    restaurant_id: int,
    template_id: str | None,
    overrides: dict | None = None,
    contract_start: str | None = None,
) -> dict | None:
    """Assign (or clear) a contract template for a staff member."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE staff
               SET contract_template_id = $1::uuid,
                   contract_overrides   = $2::jsonb,
                   contract_start       = $3::date
               WHERE id = $4::uuid AND restaurant_id = $5
               RETURNING id::text, name, role, contract_template_id::text,
                         contract_overrides, contract_start""",
            template_id,
            json.dumps(overrides or {}),
            contract_start,
            staff_id,
            restaurant_id,
        )
    return _serialize(dict(row)) if row else None


# ── Overtime requests ─────────────────────────────────────────────────────────

async def db_list_overtime_requests(
    restaurant_id: int,
    week_start: str | None = None,
    status: str | None = None,
) -> list:
    """Return overtime requests, optionally filtered by week and status."""
    pool = await _get_pool()
    clauses = ["o.restaurant_id = $1"]
    values: list = [restaurant_id]
    idx = 2
    if week_start:
        clauses.append(f"o.week_start = ${idx}::date")
        values.append(week_start)
        idx += 1
    if status:
        clauses.append(f"o.status = ${idx}")
        values.append(status)
        idx += 1
    where = " AND ".join(clauses)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT o.id::text, o.staff_id::text, s.name AS staff_name, s.role,
                       o.restaurant_id, o.week_start, o.regular_minutes,
                       o.overtime_minutes, o.overtime_rate, o.status,
                       o.approved_by::text, o.approved_at, o.notes, o.created_at
                FROM overtime_requests o
                JOIN staff s ON s.id = o.staff_id
                WHERE {where}
                ORDER BY o.week_start DESC, s.name""",
            *values,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_upsert_overtime_request(
    staff_id: str,
    restaurant_id: int,
    week_start: str,
    regular_minutes: int,
    overtime_minutes: int,
    overtime_rate: float = 1.25,
) -> dict:
    """Insert or update an overtime request for a staff member's week."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO overtime_requests
               (staff_id, restaurant_id, week_start, regular_minutes, overtime_minutes, overtime_rate)
               VALUES ($1::uuid, $2, $3::date, $4, $5, $6)
               ON CONFLICT (staff_id, week_start) DO UPDATE
                 SET regular_minutes  = EXCLUDED.regular_minutes,
                     overtime_minutes = EXCLUDED.overtime_minutes,
                     overtime_rate    = EXCLUDED.overtime_rate,
                     status           = 'pending'
               RETURNING id::text, staff_id::text, restaurant_id, week_start,
                         regular_minutes, overtime_minutes, overtime_rate, status, created_at""",
            staff_id, restaurant_id, week_start,
            regular_minutes, overtime_minutes, to_decimal(overtime_rate),
        )
    return _serialize(dict(row))


async def db_review_overtime_request(
    request_id: str,
    restaurant_id: int,
    status: str,
    approved_by: str | None,
    notes: str = "",
) -> dict | None:
    """Approve or reject an overtime request."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE overtime_requests
               SET status=$3, approved_by=$4::uuid, approved_at=NOW(), notes=$5
               WHERE id=$1::uuid AND restaurant_id=$2
               RETURNING id::text, staff_id::text, restaurant_id, week_start,
                         regular_minutes, overtime_minutes, overtime_rate,
                         status, approved_by::text, approved_at, notes""",
            request_id, restaurant_id, status,
            approved_by, notes,
        )
    return _serialize(dict(row)) if row else None
