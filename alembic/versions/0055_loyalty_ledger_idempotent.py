"""
0055 — Bullet-proof loyalty accrual idempotency at the DB level.

Background:
  db_accrue_loyalty_points used a SELECT-then-INSERT idempotency check,
  which is NOT race-safe. Two webhooks (Wompi callback + manual cashier
  validation, or two retries of the same Wompi event) firing within
  milliseconds could both pass the SELECT (existing=None) and both
  INSERT into loyalty_ledger → double points.

  The ON CONFLICT in the existing code only protected the loyalty_customers
  counter, NOT the ledger itself. Two ledger rows + one customer row
  (correct sum due to ON CONFLICT) but ledger has bad audit trail.

Schema change:
  CREATE UNIQUE INDEX ux_loyalty_ledger_accrual_idempotent
    ON loyalty_ledger (org_id, order_id) WHERE delta > 0;

  Partial index — only accrual rows (delta > 0). Redemptions (delta < 0)
  may legitimately have multiple ledger entries per order if the customer
  redeems in multiple batches.

  WHERE order_id IS NOT NULL is implicit because the column is NOT NULL
  in practice for accrual rows.

Idempotency contract:
  After this migration, attempting to INSERT a second accrual row with
  the same (org_id, order_id) raises UniqueViolationError. The repo
  upgrades the INSERT to ON CONFLICT DO NOTHING and detects the race
  via a missing RETURNING.

Cleanup of pre-existing duplicates:
  Pre-launch DBs are unlikely to have duplicates because loyalty has been
  off via flag. Defensive: this migration first deduplicates any existing
  rows by keeping the smallest id per (org_id, order_id) accrual pair.
  Counter rows in loyalty_customers may be slightly inflated — they are
  recomputed from ledger via a backfill subquery.

Revision ID: 0055
Revises: 0054
Create Date: 2026-04-27
"""

from alembic import op


revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. De-dup any existing accrual rows. Keep the row with the smallest id.
    op.execute(
        """
        DELETE FROM loyalty_ledger
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY org_id, order_id
                    ORDER BY id ASC
                ) AS rn
                FROM loyalty_ledger
                WHERE delta > 0 AND order_id IS NOT NULL
            ) t
            WHERE rn > 1
        );
        """
    )

    # 2. Recompute loyalty_customers counters from the deduped ledger so the
    #    public-facing balances stay accurate. Skip if loyalty_customers row
    #    doesn't exist (no data for that customer at all).
    op.execute(
        """
        UPDATE loyalty_customers lc
        SET points_balance = COALESCE(s.balance, 0),
            total_earned   = COALESCE(s.earned, 0),
            total_redeemed = COALESCE(s.redeemed, 0),
            updated_at     = NOW()
        FROM (
            SELECT
                org_id,
                phone,
                SUM(delta) FILTER (WHERE delta > 0)        AS earned,
                -COALESCE(SUM(delta) FILTER (WHERE delta < 0), 0) AS redeemed,
                SUM(delta)                                  AS balance
            FROM loyalty_ledger
            GROUP BY org_id, phone
        ) s
        WHERE lc.org_id = s.org_id AND lc.phone = s.phone;
        """
    )

    # 3. Add the unique partial index.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_loyalty_ledger_accrual_idempotent
            ON loyalty_ledger (org_id, order_id)
            WHERE delta > 0 AND order_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_loyalty_ledger_accrual_idempotent;")
    # No de-dup rollback — once duplicates are removed, recreating them is
    # not possible without a separate audit log.
