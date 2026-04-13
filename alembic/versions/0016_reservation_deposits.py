"""Reservation deposits table for Wompi prepayment guarantees

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-12
"""
from alembic import op

revision      = "0016"
down_revision = "0015"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS reservation_deposits (
            id SERIAL PRIMARY KEY,
            reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
            amount NUMERIC(12,2) NOT NULL,
            currency TEXT DEFAULT 'COP',
            status TEXT DEFAULT 'pending',
            payment_url TEXT DEFAULT '',
            transaction_id TEXT DEFAULT '',
            refunded BOOLEAN DEFAULT FALSE,
            refund_reason TEXT DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            paid_at TIMESTAMPTZ DEFAULT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reservation_deposits_reservation "
        "ON reservation_deposits(reservation_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reservation_deposits_status "
        "ON reservation_deposits(status)"
    )


def downgrade() -> None:
    pass
