"""
0054 — Drop legacy `tip_distributions` table.

Background:
  Tabla legacy del flujo viejo "corte manual de propinas" (POST /tip-cut),
  endpoint que se eliminó hace varios sprints. El cálculo activo de propinas
  hoy usa db_calculate_tips_by_attendance (basado en clock_in/out + table_checks.tip_amount),
  que NO escribe ni lee tip_distributions.

  Estado pre-drop verificado en sesión "Dead Code Cleanup" (2026-04-27):
    - db_save_tip_distribution: cero callers en app/.
    - db_get_tip_distributions: solo el endpoint GET /api/staff/tip-distributions.
    - El endpoint y las dos funciones del repo se eliminan en el mismo commit.
    - El mock de demo-data.js también se elimina.

  PM confirmó "puede borrarse, no hay riesgos" porque ningún cliente
  en producción ha usado este flujo.

Revision ID: 0054
Revises: 0053
Create Date: 2026-04-27
"""

from alembic import op


revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tip_distributions CASCADE;")


def downgrade() -> None:
    # Recreate minimal shape if a rollback is ever needed. Note: the original
    # table was created in migration 0002_staff_tips and had RLS enabled; this
    # downgrade does NOT restore historical data — only the empty schema.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tip_distributions (
            id            BIGSERIAL PRIMARY KEY,
            org_id        INTEGER NOT NULL,
            period_start  TIMESTAMPTZ NOT NULL,
            period_end    TIMESTAMPTZ NOT NULL,
            total_tips    NUMERIC(14,2) NOT NULL DEFAULT 0,
            distribution  JSONB NOT NULL DEFAULT '[]'::jsonb,
            pct_config    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_by    TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE tip_distributions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tip_distributions FORCE ROW LEVEL SECURITY;
        CREATE POLICY org_isolation ON tip_distributions
            USING (org_id = NULLIF(current_setting('app.org_id', true), '')::int)
            WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::int);
        """
    )
