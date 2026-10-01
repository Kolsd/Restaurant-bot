"""Add users.org_id / users.location_id — fixes the ambiguous branch_id contract.

P0 (2026-09): `users.branch_id` has NO fixed id-kind contract. Different
writers stored DIFFERENT kinds of id in the same column:
  - CRM prospect-convert (routes/internal/crm.py) writes an ORG id.
  - team_routes.py invite writes a LOCATION id (from an owner's branch
    picker, itself populated from db_get_branches which returns locations).
  - db_fix_branch_ids matches by restaurant_name against the `restaurants`
    VIEW, whose `id` column IS a location id.

Downstream code (deps.get_current_user / get_current_restaurant and many
routes) used to feed this ambiguous value into `db_get_restaurant_by_id`,
a lookup that accepted EITHER kind and, on ambiguity, silently preferred
the ORG match. Because org ids and location ids are independent sequences
over the same integer range, a location id can collide with an unrelated
org's real id for any org created after Wave 2 — resolving a user (or a
diner at a table) into a COMPLETELY DIFFERENT tenant. See
memory/ambiguous-restaurant-lookup-p0.md for the full incident.

This migration does NOT fix the guess in code (that is done separately by
splitting db_get_restaurant_by_id into db_get_restaurant_by_org_id /
db_get_restaurant_by_location_id, and by updating every writer to set the
columns added here explicitly going forward). It gives auth resolution an
UNAMBIGUOUS place to read from:

  users.org_id       BIGINT NULL REFERENCES organizations(id) ON DELETE SET NULL
  users.location_id  BIGINT NULL REFERENCES locations(id)     ON DELETE SET NULL

BACKFILL ALGORITHM (per user row with branch_id NOT NULL):
  candidates = {org whose id == branch_id} UNION {org that owns the
               location whose id == branch_id}
  - Exactly one distinct candidate  -> org_id = that org; location_id =
    branch_id IFF branch_id is also a real location id (Matriz-invariant
    tenants get both fields, non-Matriz tenants only whichever matched).
  - Zero candidates                 -> leave NULL, log the username.
  - Two candidates (a genuine id collision) -> disambiguate first by
    matching users.restaurant_name against each candidate org's name,
    then (if still ambiguous) by the org already resolved for
    users.parent_user. Still ambiguous after both -> leave NULL, log the
    username. NEVER guess.

`branch_id` is left untouched (still read by legacy code during the
transition) — NOT NULL is deliberately NOT added to the new columns, and
`branch_id` is NOT dropped, per the fix's explicit scope.

Revision ID: 0081_users_org_location
Revises:     0080_diner_sessions
Create Date: 2026-09-11
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0081_users_org_location"
down_revision = "0080_diner_sessions"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()

    # ── Phase 1: add the columns (nullable — no guessing, no NOT NULL) ───────
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS org_id BIGINT NULL"))
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS location_id BIGINT NULL"))
    logger.info("0081: users.org_id / users.location_id columns added")

    # ── Phase 2: FK constraints (idempotent — DO block checks pg_constraint) ─
    op.execute(sa.text(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_users_org_id' AND conrelid = 'users'::regclass
            ) THEN
                ALTER TABLE users
                ADD CONSTRAINT fk_users_org_id
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    ))
    op.execute(sa.text(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_users_location_id' AND conrelid = 'users'::regclass
            ) THEN
                ALTER TABLE users
                ADD CONSTRAINT fk_users_location_id
                FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    ))
    logger.info("0081: FK constraints added")

    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_users_org_id ON users (org_id) WHERE org_id IS NOT NULL"
    ))
    logger.info("0081: index added")

    # ── Phase 3: backfill (idempotent — only touches rows with org_id still NULL) ─
    rows = conn.execute(sa.text(
        "SELECT username, restaurant_name, branch_id, parent_user "
        "FROM users WHERE branch_id IS NOT NULL AND org_id IS NULL"
    )).fetchall()

    if not rows:
        logger.info("0081: no users need backfill (0 rows with branch_id set and org_id NULL)")
        return

    org_name_by_id = {
        r.id: (r.name or "")
        for r in conn.execute(sa.text("SELECT id, name FROM organizations")).fetchall()
    }
    loc_org_by_id = {
        r.id: r.org_id
        for r in conn.execute(sa.text("SELECT id, org_id FROM locations")).fetchall()
    }

    # resolved[username] = (org_id | None, location_id | None)
    # pending[username]  = (candidate_org_ids: set, parent_user, branch_id)  — needs pass 2
    resolved: dict[str, tuple] = {}
    pending: dict[str, tuple] = {}
    unresolved_reasons: list[tuple[str, str]] = []

    for r in rows:
        branch_id = r.branch_id
        candidates: set[int] = set()
        if branch_id in org_name_by_id:
            candidates.add(branch_id)
        loc_org = loc_org_by_id.get(branch_id)
        if loc_org is not None:
            candidates.add(loc_org)

        if len(candidates) == 1:
            org_id = next(iter(candidates))
            location_id = branch_id if loc_org_by_id.get(branch_id) == org_id else None
            resolved[r.username] = (org_id, location_id)
        elif len(candidates) == 0:
            unresolved_reasons.append((r.username, f"branch_id={branch_id} matches no org id and no location id"))
            resolved[r.username] = (None, None)
        else:
            # Try disambiguating by restaurant_name immediately — this
            # doesn't depend on parent_user resolution order.
            target_name = (r.restaurant_name or "").strip().lower()
            name_matches = [
                c for c in candidates
                if org_name_by_id.get(c, "").strip().lower() == target_name and target_name
            ]
            if len(name_matches) == 1:
                org_id = name_matches[0]
                location_id = branch_id if loc_org_by_id.get(branch_id) == org_id else None
                resolved[r.username] = (org_id, location_id)
            else:
                pending[r.username] = (candidates, r.parent_user, branch_id)

    # Pass 2: disambiguate remaining collisions via the parent_user's
    # already-resolved org (covers team-invited admins/gerentes whose own
    # branch_id collided, but whose creator's org is unambiguous).
    for username, (candidates, parent_user, branch_id) in pending.items():
        parent_org = None
        if parent_user and parent_user in resolved:
            parent_org = resolved[parent_user][0]
        if parent_org is not None and parent_org in candidates:
            location_id = branch_id if loc_org_by_id.get(branch_id) == parent_org else None
            resolved[username] = (parent_org, location_id)
        else:
            unresolved_reasons.append((
                username,
                f"branch_id={branch_id} collides between orgs {sorted(candidates)} — "
                f"restaurant_name and parent_user ({parent_user!r}) disambiguation both failed",
            ))
            resolved[username] = (None, None)

    # ── Apply ─────────────────────────────────────────────────────────────
    resolved_count = 0
    for username, (org_id, location_id) in resolved.items():
        conn.execute(
            sa.text(
                "UPDATE users SET org_id = :org_id, location_id = :location_id "
                "WHERE username = :username"
            ),
            {"org_id": org_id, "location_id": location_id, "username": username},
        )
        if org_id is not None:
            resolved_count += 1

    logger.info(
        "0081: backfill complete — %d/%d users resolved, %d left NULL (see warnings)",
        resolved_count, len(rows), len(unresolved_reasons),
    )
    for username, reason in unresolved_reasons:
        logger.warning("0081: could not resolve org_id for user %r: %s", username, reason)


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE users DROP CONSTRAINT IF EXISTS fk_users_org_id"))
    op.execute(sa.text("ALTER TABLE users DROP CONSTRAINT IF EXISTS fk_users_location_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_users_org_id"))
    op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS org_id"))
    op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS location_id"))
