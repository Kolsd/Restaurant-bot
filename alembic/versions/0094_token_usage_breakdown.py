"""subscription_usage: record what the LLM actually bills, not a half of it.

Anthropic reports four token counters per response, priced very differently:

  input_tokens               — uncached input          ($1.00 /MTok on Haiku 4.5)
  output_tokens              — generated               ($5.00 /MTok)
  cache_creation_input_tokens— written to prompt cache (~$1.25 /MTok)
  cache_read_input_tokens    — served from cache       (~$0.10 /MTok)

`agent.py` recorded `input_tokens + output_tokens` into a single
`total_tokens` column, and `cost_estimator` priced that sum at one blended
$0.60/MTok. Both halves of the margin dashboard were therefore wrong in the
same direction — too cheap: the cached input (the BULK of every bot turn,
since the system prompt, the tool list and the carta are all cached) was
counted nowhere at all, and the output was valued at roughly an eighth of
its real rate. Pricing a flat per-sede plan off that number is how a SaaS
sells below cost without noticing.

These four columns are the truth. They are additive and default to 0, so
rows written before this migration simply have zeros in them and the cost
repo keeps pricing those with the legacy blended rate (see
`cost_metrics_repo._estimate_row`) instead of pretending we know a split we
never recorded.

`total_tokens` is deliberately LEFT ALONE, still incremented with exactly
what it was incremented with before (uncached input + output). It feeds the
legacy per-day cap in `db_check_usage_limits` (`features.plan_limits.
daily_tokens`), and folding cache reads into it would silently tighten that
cap by ~7x for any org that has one configured. It is widened to BIGINT
here only so it cannot overflow alongside its new neighbours.

Revision ID: 0094_token_usage_breakdown
Revises:     0093_sede_menu_overrides
Create Date: 2026-09-23
"""

from alembic import op

revision = "0094_token_usage_breakdown"
down_revision = "0093_sede_menu_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Spelled out rather than looped over a column list, so no SQL in this
    # file is built with an f-string (project rule) even from a constant.
    op.execute("ALTER TABLE subscription_usage "
               "ADD COLUMN IF NOT EXISTS input_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE subscription_usage "
               "ADD COLUMN IF NOT EXISTS output_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE subscription_usage "
               "ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE subscription_usage "
               "ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE subscription_usage ALTER COLUMN total_tokens TYPE BIGINT")


def downgrade() -> None:
    op.execute("ALTER TABLE subscription_usage ALTER COLUMN total_tokens TYPE INTEGER")
    op.execute("ALTER TABLE subscription_usage DROP COLUMN IF EXISTS cache_write_tokens")
    op.execute("ALTER TABLE subscription_usage DROP COLUMN IF EXISTS cache_read_tokens")
    op.execute("ALTER TABLE subscription_usage DROP COLUMN IF EXISTS output_tokens")
    op.execute("ALTER TABLE subscription_usage DROP COLUMN IF EXISTS input_tokens")
