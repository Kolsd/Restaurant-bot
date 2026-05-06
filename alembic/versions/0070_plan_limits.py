"""Subscription plans foundation: plan_limits, addon_modules, usage_packs tables.

Adds:
  - plan_limits: global lookup table of plan definitions (no RLS — public read).
  - addon_modules: global lookup table of available add-on modules (no RLS).
  - usage_packs: per-org auto-recharge credit packs (RLS org_isolation).
  - ALTER organizations: plan_code FK, active_addons[], auto-recharge config,
      current_period tracking columns, comp_until, annual_billing.

Seeds the 4 canonical plans (pulso, restaurante, pro, cadena) and 7 add-on
modules with ON CONFLICT DO NOTHING for idempotency.

RLS: usage_packs gets ENABLE + FORCE + org_isolation policy (mirrors 0049
loyalty_campaigns pattern). plan_limits and addon_modules are global lookup
tables — intentionally NO RLS.

GUC used by policy: app.org_id (Wave-2 canonical tenant key).

Revision ID: 0070_plan_limits
Revises: 0069_password_reset_tokens
"""

import logging

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision = "0070_plan_limits"
down_revision = "0069_password_reset_tokens"
branch_labels = None
depends_on = None

_ORG_ISOLATION_EXPR = (
    "org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)


def upgrade() -> None:
    # ── 1. plan_limits (global lookup — no RLS) ─────────────────────────────
    logger.info("0050 upgrade: creating plan_limits table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS plan_limits (
            plan_code           TEXT        PRIMARY KEY,
            display_name        TEXT        NOT NULL,
            monthly_price_cop   INTEGER     NOT NULL,
            conv_cap            INTEGER     NOT NULL,
            audio_min_cap       INTEGER     NOT NULL,
            storage_mb_cap      INTEGER     NOT NULL,
            locations_included  INTEGER     NOT NULL DEFAULT 1,
            staff_cap           INTEGER     NOT NULL,
            sku_cap             INTEGER     NOT NULL,
            marketing_msg_cap   INTEGER     NOT NULL,
            description         TEXT,
            sort_order          SMALLINT    NOT NULL DEFAULT 0,
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # ── 2. addon_modules (global lookup — no RLS) ───────────────────────────
    logger.info("0050 upgrade: creating addon_modules table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS addon_modules (
            module_code         TEXT        PRIMARY KEY,
            display_name        TEXT        NOT NULL,
            monthly_price_cop   INTEGER     NOT NULL,
            description         TEXT,
            sort_order          SMALLINT    NOT NULL DEFAULT 0,
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # ── 3. usage_packs (tenant-scoped — RLS org_isolation) ──────────────────
    logger.info("0050 upgrade: creating usage_packs table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS usage_packs (
            id                  BIGSERIAL   PRIMARY KEY,
            org_id              BIGINT      NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            credits_total       INTEGER     NOT NULL,
            credits_remaining   INTEGER     NOT NULL,
            amount_paid_cop     INTEGER     NOT NULL,
            fired_at            TIMESTAMPTZ DEFAULT NOW(),
            expires_at          TIMESTAMPTZ NOT NULL,
            fired_automatically BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at          TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    logger.info("0050 upgrade: creating index ix_usage_packs_org_expires")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_usage_packs_org_expires
        ON usage_packs (org_id, expires_at)
    """)

    logger.info("0050 upgrade: enabling RLS on usage_packs")
    op.execute("ALTER TABLE usage_packs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_packs FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        DROP POLICY IF EXISTS org_isolation ON usage_packs;
        CREATE POLICY org_isolation ON usage_packs
        USING ({_ORG_ISOLATION_EXPR})
        WITH CHECK ({_ORG_ISOLATION_EXPR})
    """)

    logger.info("0050 upgrade: granting DML on usage_packs to mesio_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON usage_packs TO mesio_app"
    )
    op.execute("GRANT USAGE, SELECT ON SEQUENCE usage_packs_id_seq TO mesio_app")

    # ── 4. Grant read access on lookup tables to mesio_app ──────────────────
    logger.info("0050 upgrade: granting SELECT on plan_limits and addon_modules to mesio_app")
    op.execute("GRANT SELECT ON plan_limits TO mesio_app")
    op.execute("GRANT SELECT ON addon_modules TO mesio_app")

    # ── 5. ALTER organizations — subscription columns ────────────────────────
    logger.info("0050 upgrade: adding subscription columns to organizations")

    # plan_code FK — default 'pulso' so existing orgs land on the base plan
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS plan_code TEXT NOT NULL DEFAULT 'pulso'
    """)

    # active_addons TEXT[] — list of enabled addon module_codes
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS active_addons TEXT[] NOT NULL DEFAULT '{}'
    """)

    # auto-recharge config
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS auto_recharge_enabled BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS auto_recharge_max_packs_per_month SMALLINT NOT NULL DEFAULT 5
            CONSTRAINT chk_orgs_max_packs CHECK (auto_recharge_max_packs_per_month BETWEEN 0 AND 5)
    """)

    # current billing period tracking
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS current_period_start DATE NOT NULL DEFAULT CURRENT_DATE
    """)
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS current_period_convs_used INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS current_period_audio_min_used NUMERIC(10,2) NOT NULL DEFAULT 0
    """)

    # comp_until — friends & family free access override
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS comp_until TIMESTAMPTZ NULL
    """)

    # annual_billing — flag for 16.7% discount calculation downstream
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS annual_billing BOOLEAN NOT NULL DEFAULT FALSE
    """)

    # Now add the FK constraint for plan_code (after the seed below)
    # We add it after seeding so the reference exists.

    # ── 6. Seed plan_limits ──────────────────────────────────────────────────
    logger.info("0050 upgrade: seeding plan_limits")
    op.execute("""
        INSERT INTO plan_limits
            (plan_code, display_name, monthly_price_cop,
             conv_cap, audio_min_cap, storage_mb_cap,
             locations_included, staff_cap, sku_cap, marketing_msg_cap,
             description, sort_order)
        VALUES
            ('pulso',       'Pulso',       149000,
             250,  60,   200,    1, 3,      50,      0,
             'Para dark kitchens y restaurantes unipersonales', 1),
            ('restaurante', 'Restaurante', 299000,
             700,  200,  1024,   1, 10,     200,     100,
             'Para restaurantes con equipo y menú completo', 2),
            ('pro',         'Pro',         549000,
             1500, 500,  3072,   1, 20,     500,     500,
             'Para restaurantes con alto volumen y múltiples módulos', 3),
            ('cadena',      'Cadena',      899000,
             3000, 1000, 10240,  1, 50,     999999,  1000,
             'Para cadenas con múltiples sedes — pool de conversaciones compartido', 4)
        ON CONFLICT (plan_code) DO NOTHING
    """)

    # ── 7. Seed addon_modules ────────────────────────────────────────────────
    logger.info("0050 upgrade: seeding addon_modules")
    op.execute("""
        INSERT INTO addon_modules
            (module_code, display_name, monthly_price_cop, description, sort_order)
        VALUES
            ('extra_location',      'Sede adicional',              199000, 'Agrega una sede adicional a tu plan', 1),
            ('dian',                'Facturación DIAN',             99000, 'Factura electrónica DIAN Colombia', 2),
            ('payroll',             'Nómina y contratos',          129000, 'Gestión de nómina, contratos y deducciones', 3),
            ('loyalty',             'Fidelización',                 79000, 'Programa de puntos y campañas de lealtad', 4),
            ('inventory',           'Inventario',                   79000, 'Control de inventario y escandallos', 5),
            ('reservations_deposit','Depósito de reservas',         59000, 'Cobro anticipado via Wompi al reservar', 6),
            ('marketing_extra',     'Marketing extra (1000 msgs)', 99000, '1000 mensajes de marketing adicionales por mes', 7)
        ON CONFLICT (module_code) DO NOTHING
    """)

    # ── 8. Add FK constraint for plan_code after seeding ────────────────────
    logger.info("0050 upgrade: adding FK constraint organizations.plan_code -> plan_limits")
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_orgs_plan_code'
                  AND table_name = 'organizations'
            ) THEN
                ALTER TABLE organizations
                ADD CONSTRAINT fk_orgs_plan_code
                FOREIGN KEY (plan_code) REFERENCES plan_limits(plan_code);
            END IF;
        END;
        $$
    """)

    logger.info("0050 upgrade: done")


def downgrade() -> None:
    logger.info("0050 downgrade: removing FK constraint and columns from organizations")

    # Drop FK first so we can drop plan_limits
    op.execute("""
        ALTER TABLE organizations
        DROP CONSTRAINT IF EXISTS fk_orgs_plan_code
    """)

    # Drop the added columns
    for col in [
        "plan_code",
        "active_addons",
        "auto_recharge_enabled",
        "auto_recharge_max_packs_per_month",
        "current_period_start",
        "current_period_convs_used",
        "current_period_audio_min_used",
        "comp_until",
        "annual_billing",
    ]:
        op.execute(f"ALTER TABLE organizations DROP COLUMN IF EXISTS {col}")  # noqa: S608

    logger.info("0050 downgrade: dropping usage_packs table")
    op.execute("DROP POLICY IF EXISTS org_isolation ON usage_packs")
    op.execute("DROP TABLE IF EXISTS usage_packs CASCADE")

    logger.info("0050 downgrade: dropping addon_modules and plan_limits tables")
    op.execute("DROP TABLE IF EXISTS addon_modules CASCADE")
    op.execute("DROP TABLE IF EXISTS plan_limits CASCADE")

    logger.info("0050 downgrade: done")
