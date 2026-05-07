"""Performance indexes (usage_packs FIFO, staff_shifts open shift) and
phone_blocklist WITH CHECK tightening.

Changes:
  1. ix_usage_packs_org_active — partial index on usage_packs excluding
     exhausted packs (credits_remaining > 0), ordered by fired_at ASC for
     FIFO consumption. db_check_caps and db_consume_pack_credit query
     WHERE org_id=$1 AND credits_remaining>0 AND expires_at>NOW() ORDER BY
     fired_at ASC — this index covers the first two predicates and the sort,
     letting Postgres avoid a full scan of exhausted/all packs.

  2. ix_staff_shifts_open — partial index on staff_shifts covering only open
     shifts (clock_out IS NULL), ordered by clock_in DESC per staff_id.
     db_get_open_shifts and clock-in checks hit WHERE clock_out IS NULL
     repeatedly; this partial index is ~100x smaller than the full covering
     index ix_staff_shifts_staff_clock added in 0048.

  3. phone_blocklist org_isolation policy — the original WITH CHECK allowed
     any tenant to insert a row with org_id=NULL, creating a "global block"
     that silences phones across ALL tenants. The new policy tightens WITH
     CHECK to require org_id = current scope (no nulls on write). USING still
     allows reading global blocks (org_id IS NULL) so the inbox_worker's
     is_phone_blocked check continues to work. mesio_superadmin (BYPASSRLS)
     can still insert global blocks via bypass_tenant_scope from internal
     admin tools.

  Note on CONCURRENTLY: skipped (pre-launch tables are small; plain CREATE
  INDEX inside Alembic's transaction is correct). Mirrors 0048 pattern.

  Note on ix_webhook_inbox_claim: already added in 0048 as
  (next_attempt_at, id) WHERE processed_at IS NULL — not duplicated here.

Revision ID: 0073_perf_indexes_and_blocklist_check
Revises: 0072_merge_plan_limits_demo_seed
Create Date: 2026-05-07
"""

import logging

from alembic import op

revision = "0073_perf_indexes_and_blocklist_check"
down_revision = "0072_merge_plan_limits_demo_seed"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Original phone_blocklist WITH CHECK expression (for downgrade restore)
_BLOCKLIST_POLICY_ORIGINAL_WITH_CHECK = (
    "org_id IS NULL OR org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)
# Tightened WITH CHECK: no nulls on write (tenant can only insert own org_id)
_BLOCKLIST_POLICY_USING = (
    "org_id IS NULL OR org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)
_BLOCKLIST_POLICY_WITH_CHECK_TIGHT = (
    "org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)


def upgrade() -> None:
    # ── 1. usage_packs FIFO partial index ────────────────────────────────────
    # Covers: WHERE org_id = $1 AND credits_remaining > 0
    # Includes expires_at in the index columns so the planner can also
    # filter by expires_at > NOW() without a heap fetch on those columns.
    # Partial predicate credits_remaining > 0 eliminates exhausted packs,
    # making this index much smaller than the full ix_usage_packs_org_expires.
    # Note: expires_at > NOW() cannot be used in a partial index predicate
    # (NOW() is STABLE, not IMMUTABLE — Postgres rejects it). It is instead
    # included as an index column so Postgres can apply it as an index-only
    # condition after using the partial predicate.
    logger.info(
        "0073 upgrade: creating partial index ix_usage_packs_org_active"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_usage_packs_org_active "
        "ON usage_packs (org_id, fired_at ASC, expires_at) "
        "WHERE credits_remaining > 0"
    )

    # ── 2. staff_shifts open-shift partial index ──────────────────────────────
    # Covers: WHERE clock_out IS NULL (at most one row per staff due to the
    # partial unique index). Ordered by clock_in DESC so the most-recent open
    # shift surfaces first. This index is ~100x smaller than the full
    # ix_staff_shifts_staff_clock (0048) because it excludes all closed shifts.
    logger.info(
        "0073 upgrade: creating partial index ix_staff_shifts_open"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_staff_shifts_open "
        "ON staff_shifts (staff_id, clock_in DESC) "
        "WHERE clock_out IS NULL"
    )

    # ── 3. Tighten phone_blocklist WITH CHECK ─────────────────────────────────
    # DROP the existing policy and recreate with a stricter WITH CHECK that
    # forbids tenant-side inserts of org_id=NULL (which would create global
    # blocks affecting ALL tenants). USING clause is unchanged — reads still
    # see global blocks so is_phone_blocked works correctly in bypass scope.
    logger.info(
        "0073 upgrade: tightening phone_blocklist org_isolation WITH CHECK"
    )
    op.execute("DROP POLICY IF EXISTS org_isolation ON phone_blocklist")
    op.execute(
        f"CREATE POLICY org_isolation ON phone_blocklist "
        f"USING ({_BLOCKLIST_POLICY_USING}) "
        f"WITH CHECK ({_BLOCKLIST_POLICY_WITH_CHECK_TIGHT})"
    )

    logger.info("0073 upgrade: done")


def downgrade() -> None:
    # ── Restore original phone_blocklist policy (WITH CHECK allows org_id NULL) ─
    logger.info(
        "0073 downgrade: restoring original phone_blocklist org_isolation policy"
    )
    op.execute("DROP POLICY IF EXISTS org_isolation ON phone_blocklist")
    op.execute(
        f"CREATE POLICY org_isolation ON phone_blocklist "
        f"USING ({_BLOCKLIST_POLICY_ORIGINAL_WITH_CHECK}) "
        f"WITH CHECK ({_BLOCKLIST_POLICY_ORIGINAL_WITH_CHECK})"
    )

    # ── Drop the two new indexes ───────────────────────────────────────────────
    logger.info("0073 downgrade: dropping ix_staff_shifts_open")
    op.execute("DROP INDEX IF EXISTS ix_staff_shifts_open")

    logger.info("0073 downgrade: dropping ix_usage_packs_org_active")
    op.execute("DROP INDEX IF EXISTS ix_usage_packs_org_active")

    logger.info("0073 downgrade: done")
