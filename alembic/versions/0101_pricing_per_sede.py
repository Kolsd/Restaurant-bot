"""Pricing per sede: Esencial replaces Pulso, new prices, founder price.

The 2026-09-30 price list (docs/claude/status.md #15, app/services/plans.py):
Esencial 119.000, Restaurante 249.000, Pro 349.000, Cadena 299.000 — per sede
per month. `plan_limits` keeps mirroring the prices so SQL (MRR) can join on
them; `conv_cap` becomes the internal soft ceiling (~30% of the price spent on
the LLM at ~$140 COP per conversation), which only alerts Mesio.

Also repairs the plan every self-serve org was actually on: signup wrote the
chosen plan to `subscription_plan` while `plan_code` stayed at its 'pulso'
default, so a restaurant that picked Restaurante was billed as Pulso.
`plan_code` is the one column code reads from now on; `subscription_plan`
is kept equal to it until a later drop. Orgs that never chose a plan (demo,
legacy 'free') land on Restaurante — the plan the trial gives — and so does
the column default: signup, CRM and superadmin always name one.

`founder_price_cop` is the frozen per-sede price of a founder-program org
(40% off list); NULL for everyone else.

Revision ID: 0101_pricing_per_sede
Revises:     0100_repair_double_encoded_jsonb
Create Date: 2026-09-30
"""
from alembic import op

revision = "0101_pricing_per_sede"
down_revision = "0100_repair_double_encoded_jsonb"
branch_labels = None
depends_on = None

_UNLIMITED = 999999


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO plan_limits
            (plan_code, display_name, monthly_price_cop,
             conv_cap, audio_min_cap, storage_mb_cap,
             locations_included, staff_cap, sku_cap, marketing_msg_cap,
             description, sort_order)
        VALUES
            ('esencial', 'Esencial', 119000,
             0, 0, 200, 1, 5, {_UNLIMITED}, 0,
             'Carta QR y pedidos desde la mesa, cocina, caja y panel de ventas', 1)
        ON CONFLICT (plan_code) DO NOTHING
    """)

    # Orgs first, so deleting the pulso row does not trip fk_orgs_plan_code.
    op.execute("""
        UPDATE organizations
           SET plan_code = CASE
                   WHEN subscription_plan IN ('restaurante', 'pro', 'cadena')
                       THEN subscription_plan
                   WHEN subscription_plan = 'pulso' THEN 'esencial'
                   ELSE 'restaurante'
               END
         WHERE plan_code = 'pulso'
    """)
    op.execute("UPDATE organizations SET pending_plan_code = 'esencial' WHERE pending_plan_code = 'pulso'")
    op.execute("UPDATE organizations SET subscription_plan = plan_code")
    op.execute("ALTER TABLE organizations ALTER COLUMN plan_code SET DEFAULT 'restaurante'")
    op.execute("ALTER TABLE organizations ALTER COLUMN subscription_plan SET DEFAULT 'restaurante'")
    op.execute("DELETE FROM plan_limits WHERE plan_code = 'pulso'")

    op.execute(f"""
        UPDATE plan_limits AS pl
           SET monthly_price_cop = v.price,
               conv_cap          = v.conv_cap,
               staff_cap         = {_UNLIMITED},
               sku_cap           = {_UNLIMITED},
               locations_included = 1,
               audio_min_cap     = 0,
               marketing_msg_cap = 0,
               description       = v.description,
               sort_order        = v.sort_order,
               updated_at        = NOW()
          FROM (VALUES
                ('restaurante', 249000, 500,
                 'Asistente con IA en la mesa, domicilios y recogida por link propio', 2),
                ('pro',         349000, 750,
                 'Reservas, inventario por pedido y facturación electrónica DIAN', 3),
                ('cadena',      299000, 650,
                 'Desde 3 sedes: panel de todas las sedes y traslados de inventario', 4)
               ) AS v(plan_code, price, conv_cap, description, sort_order)
         WHERE pl.plan_code = v.plan_code
    """)

    op.execute("""
        ALTER TABLE organizations
            ADD COLUMN IF NOT EXISTS founder_price_cop INTEGER
    """)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'chk_orgs_founder_price_positive'
            ) THEN
                ALTER TABLE organizations
                    ADD CONSTRAINT chk_orgs_founder_price_positive
                    CHECK (founder_price_cop IS NULL OR founder_price_cop > 0);
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP CONSTRAINT IF EXISTS chk_orgs_founder_price_positive")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS founder_price_cop")
    op.execute("""
        INSERT INTO plan_limits
            (plan_code, display_name, monthly_price_cop,
             conv_cap, audio_min_cap, storage_mb_cap,
             locations_included, staff_cap, sku_cap, marketing_msg_cap,
             description, sort_order)
        VALUES
            ('pulso', 'Pulso', 149000, 250, 60, 200, 1, 3, 50, 0,
             'Para dark kitchens y restaurantes unipersonales', 1)
        ON CONFLICT (plan_code) DO NOTHING
    """)
    op.execute("""
        UPDATE plan_limits AS pl
           SET monthly_price_cop = v.price, conv_cap = v.conv_cap,
               staff_cap = v.staff_cap, updated_at = NOW()
          FROM (VALUES ('restaurante', 299000, 700, 10),
                       ('pro',         549000, 1500, 20),
                       ('cadena',      899000, 3000, 50)
               ) AS v(plan_code, price, conv_cap, staff_cap)
         WHERE pl.plan_code = v.plan_code
    """)
    op.execute("UPDATE organizations SET plan_code = 'pulso' WHERE plan_code = 'esencial'")
    op.execute("UPDATE organizations SET pending_plan_code = 'pulso' WHERE pending_plan_code = 'esencial'")
    op.execute("UPDATE organizations SET subscription_plan = 'pulso' WHERE subscription_plan = 'esencial'")
    op.execute("ALTER TABLE organizations ALTER COLUMN plan_code SET DEFAULT 'pulso'")
    op.execute("ALTER TABLE organizations ALTER COLUMN subscription_plan SET DEFAULT 'free'")
    op.execute("DELETE FROM plan_limits WHERE plan_code = 'esencial'")
