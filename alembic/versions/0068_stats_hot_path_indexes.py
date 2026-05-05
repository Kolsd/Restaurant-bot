"""Stats hot-path indexes for No-v2 sprint queries.

INDEX RATIONALE:
  1. ix_loyalty_ledger_org_reason_created
       Query: db_get_loyalty_aggregates (avg_frequency) and db_get_loyalty_segments
              filter  WHERE org_id=$1 AND reason='purchase' AND created_at >= ...
              Current ix_loyalty_ledger_org_id covers org_id only — Postgres must
              then filter reason + created_at from the full org partition.
              Composite (org_id, reason, created_at DESC) eliminates that secondary
              filter cost at any meaningful ledger size.

  2. ix_customer_profiles_org_last_seen
       Query: db_churn_summary
              filters  WHERE org_id=$1 AND total_orders >= 2 AND last_seen < ...
              and sorts  ORDER BY last_seen ASC.
              Current ix_customer_profiles_org_id is single-column. A composite
              (org_id, last_seen DESC) lets Postgres skip all rows outside the
              recency window in a single index range scan instead of a full
              org-partition seq scan.

  3. ix_reservations_org_created
       Query: db_branches_comparison
              filters  WHERE org_id=$1 AND created_at >= $window GROUP BY branch_id
              Current ix_reservations_org_id is single-column. Composite
              (org_id, created_at DESC) eliminates the secondary created_at filter.

  4. ix_loyalty_customers_org_balance
       Query: db_get_loyalty_funnel steps 3/4/5
              filters  WHERE org_id=$1 AND points_balance >= $tier_threshold
              Current ix_loyalty_customers_org_id is single-column. Composite
              (org_id, points_balance DESC) supports the >= range filter in
              one index scan; also benefits COUNT(*) FILTER (WHERE points_balance >= N)
              after the batching refactor in loyalty_repo.py.

Revision ID: 0068_stats_hot_path_indexes
Revises: 0067_weekly_reports_retry
Create Date: 2026-05-05
"""

import logging

from alembic import op

revision = "0068_stats_hot_path_indexes"
down_revision = "0067_weekly_reports_retry"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# (index_name, create_sql, drop_sql)
_INDEXES = [
    (
        "ix_loyalty_ledger_org_reason_created",
        "CREATE INDEX IF NOT EXISTS ix_loyalty_ledger_org_reason_created "
        "ON loyalty_ledger (org_id, reason, created_at DESC)",
        "DROP INDEX IF EXISTS ix_loyalty_ledger_org_reason_created",
    ),
    (
        "ix_customer_profiles_org_last_seen",
        "CREATE INDEX IF NOT EXISTS ix_customer_profiles_org_last_seen "
        "ON customer_profiles (org_id, last_seen DESC)",
        "DROP INDEX IF EXISTS ix_customer_profiles_org_last_seen",
    ),
    (
        "ix_reservations_org_created",
        "CREATE INDEX IF NOT EXISTS ix_reservations_org_created "
        "ON reservations (org_id, created_at DESC)",
        "DROP INDEX IF EXISTS ix_reservations_org_created",
    ),
    (
        "ix_loyalty_customers_org_balance",
        "CREATE INDEX IF NOT EXISTS ix_loyalty_customers_org_balance "
        "ON loyalty_customers (org_id, points_balance DESC)",
        "DROP INDEX IF EXISTS ix_loyalty_customers_org_balance",
    ),
]


def upgrade() -> None:
    for name, create_sql, _drop_sql in _INDEXES:
        logger.info("0068 upgrade: creating index %s", name)
        op.execute(create_sql)

    logger.info("0068 upgrade: all 4 stats hot-path indexes created")


def downgrade() -> None:
    for name, _create_sql, drop_sql in reversed(_INDEXES):
        logger.info("0068 downgrade: dropping index %s", name)
        op.execute(drop_sql)

    logger.info("0068 downgrade: all 4 stats hot-path indexes dropped")
