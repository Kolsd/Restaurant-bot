"""Merge parallel heads: 0070_plan_limits + 0071_demo_seed.

Both branches were authored against 0069_password_reset_tokens in parallel
sprints (Pricing v1 + /demo live chat). Production deploy crashed on
`alembic upgrade head` with "Multiple head revisions are present".

This is a no-op merge — schema unchanged. It only joins the migration graph
so Alembic has a single head again.

Revision ID: 0072_merge_plan_limits_demo_seed
Revises: 0070_plan_limits, 0071_demo_seed
"""

revision = "0072_merge_plan_limits_demo_seed"
down_revision = ("0070_plan_limits", "0071_demo_seed")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
