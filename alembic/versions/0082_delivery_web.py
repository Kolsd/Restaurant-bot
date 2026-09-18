"""Delivery/pickup web wave — Phase A data foundation.

See docs/claude/delivery-web.md (locked spec, 2026-09-17) for the full
product context. This migration is backend-only groundwork: no route, no
frontend. It adds:

1. `locations.delivery_config JSONB NOT NULL DEFAULT '{}'::jsonb` — per-sede
   delivery/pickup settings (precedence resolved by app/services/delivery.py).

2. Delivery-lifecycle columns on `orders`. Before adding anything, the real
   current shape of `orders` was inspected against the test DB. Several
   columns the spec asked for ALREADY EXIST and are reused as-is, not
   duplicated:
     - `address`              (customer/delivery address — already TEXT)
     - `delivery_fee`         (already NUMERIC)
     - `proof_url`            (already TEXT — Nequi/Bancolombia transfer proof)
     - `estimated_minutes` + `eta_communicated`  (already there — used as the
       ETA the cashier sets on acceptance; no separate "ETA minutes" column
       is added)
     - `channel`, `payment_method`, `notes`, `org_id`, `location_id` (already there)
     - `cancelled_at TIMESTAMPTZ` and `cancelled_reason TEXT` (already there,
       added earlier for the WhatsApp-era cancel flow) — reused verbatim for
       the web customer's pre-acceptance cancel; NOT re-added under a new name.
     - `scheduled_pickup_at TIMESTAMPTZ` (0052_orders_scheduled_pickup_at) —
       reused as the same-day scheduled time for both pickup AND delivery;
       no new "scheduled_at" column is added.

   Genuinely new columns:
     - `public_code TEXT` — short customer-facing code for `/pedido/{code}`.
     - `customer_name TEXT`, `customer_phone TEXT` — captured at checkout.
     - `delivery_lat DOUBLE PRECISION`, `delivery_lon DOUBLE PRECISION` — GPS pin.
     - `tip_amount NUMERIC(12,2) NOT NULL DEFAULT 0`.
     - `cash_change_for NUMERIC(12,2)` — "¿con cuánto pagas?" amount.
     - `accepted_at TIMESTAMPTZ`, `rejected_at TIMESTAMPTZ`,
       `rejection_reason TEXT`, `courier_assigned_at TIMESTAMPTZ`,
       `delivered_at TIMESTAMPTZ`.

   Deliberate TYPE deviation from the chunk-1 brief for two columns:
     - Spec asked for `accepted_by_user_id BIGINT` and `courier_user_id BIGINT`.
       The real identity of "who accepted" / "who is the courier" in this
       codebase is a STAFF MEMBER — `staff.id`, which is UUID (see
       app/routes/staff.py, `staff_shifts.staff_id`, `table_orders.
       waiter_staff_id`, `table_sessions.assigned_staff_id` — every existing
       "who did this" column in the schema is `<role>_staff_id UUID
       REFERENCES staff(id)`, never a BIGINT `users.id` — `users` doesn't
       even have a numeric id, its PK is `username TEXT`). Adding a BIGINT
       column named "*_user_id" would reference nothing real and silently
       reintroduce the same kind of id-kind ambiguity this codebase has
       already been burned by once (see memory/ambiguous-restaurant-lookup-p0.md).
       Added instead, following the established convention exactly:
         - `accepted_by_staff_id UUID NULL REFERENCES staff(id) ON DELETE SET NULL`
         - `courier_staff_id UUID NULL REFERENCES staff(id) ON DELETE SET NULL`
       Reported in the chunk-1 summary as required by the brief.

   Also added (not explicitly requested, but needed for the public-code
   lookup path described in the spec):
     - `ux_orders_public_code` — GLOBAL partial unique index on `public_code`.
       `db_get_order_by_public_code()` is a PRE-tenant lookup (the customer's
       status URL carries no org_id), so it can only filter on public_code.
       A per-org unique index would let two orgs mint the same code and leave
       that global lookup returning whichever row Postgres found first — the
       exact cross-tenant ambiguity that caused the `db_get_restaurant_by_id`
       P0. The code space is therefore globally unique, which also makes the
       index the real guarantee behind the generator's retry loop.

3. Backfill `organizations.slug` for any row where it is NULL or empty,
   using the same slugify + numeric-suffix idea as 0025_restaurant_slug.py,
   generalized to also avoid colliding with slugs that already exist on
   OTHER rows (0025 could assume a fully-empty column; this table already
   has a UNIQUE constraint and many non-null slugs today).

Revision ID: 0082_delivery_web
Revises:     0081_users_org_location
Create Date: 2026-09-17
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0082_delivery_web"
down_revision = "0081_users_org_location"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    # ── 1. locations.delivery_config ─────────────────────────────────────
    op.execute(sa.text(
        "ALTER TABLE locations ADD COLUMN IF NOT EXISTS "
        "delivery_config JSONB NOT NULL DEFAULT '{}'::jsonb"
    ))
    logger.info("0082: locations.delivery_config added")

    # ── 2. New orders columns (all IF NOT EXISTS) ────────────────────────
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS public_code TEXT"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name TEXT"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_phone TEXT"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_lat DOUBLE PRECISION"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_lon DOUBLE PRECISION"))
    op.execute(sa.text(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "tip_amount NUMERIC(12,2) NOT NULL DEFAULT 0"
    ))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS cash_change_for NUMERIC(12,2)"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS accepted_by_staff_id UUID"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS rejection_reason TEXT"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS courier_staff_id UUID"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS courier_assigned_at TIMESTAMPTZ"))
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ"))
    logger.info("0082: orders delivery-lifecycle columns added")

    # FK constraints for the two staff-identity columns (idempotent, mirrors
    # the DO-block pattern in 0081_users_org_location.py).
    op.execute(sa.text(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_orders_accepted_by_staff_id' AND conrelid = 'orders'::regclass
            ) THEN
                ALTER TABLE orders
                ADD CONSTRAINT fk_orders_accepted_by_staff_id
                FOREIGN KEY (accepted_by_staff_id) REFERENCES staff(id) ON DELETE SET NULL;
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
                WHERE conname = 'fk_orders_courier_staff_id' AND conrelid = 'orders'::regclass
            ) THEN
                ALTER TABLE orders
                ADD CONSTRAINT fk_orders_courier_staff_id
                FOREIGN KEY (courier_staff_id) REFERENCES staff(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    ))
    logger.info("0082: orders FK constraints added")

    # GLOBALLY unique public code: the customer's status URL carries no
    # org_id, so the code must identify exactly one order across every
    # tenant. See the module docstring.
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_public_code "
        "ON orders (public_code) WHERE public_code IS NOT NULL"
    ))
    logger.info("0082: orders public_code index added")

    # ── 3. Backfill organizations.slug (NULL / empty rows only) ──────────
    # Same slugify idea as 0025_restaurant_slug.py, generalized to also
    # avoid colliding with slugs that already exist on OTHER rows (0025 ran
    # against a brand-new, fully-empty column; this one already has data).
    op.execute(sa.text(
        """
        DO $$
        DECLARE
            r RECORD;
            base_slug TEXT;
            candidate TEXT;
            suffix INT;
        BEGIN
            FOR r IN
                SELECT id, name FROM organizations
                WHERE slug IS NULL OR slug = ''
                ORDER BY id
            LOOP
                base_slug := trim(both '-' from lower(
                    regexp_replace(coalesce(r.name, ''), '[^a-zA-Z0-9]+', '-', 'g')
                ));
                IF base_slug = '' THEN
                    base_slug := 'org-' || r.id::text;
                END IF;

                candidate := base_slug;
                suffix := 1;
                WHILE EXISTS (
                    SELECT 1 FROM organizations WHERE slug = candidate AND id <> r.id
                ) LOOP
                    suffix := suffix + 1;
                    candidate := base_slug || '-' || suffix::text;
                END LOOP;

                UPDATE organizations SET slug = candidate WHERE id = r.id;
            END LOOP;
        END $$;
        """
    ))
    logger.info("0082: organizations.slug backfilled for NULL/empty rows")


def downgrade() -> None:
    # Slug backfill is a data migration — not reversible (same precedent as
    # 0081_users_org_location's backfill), only the schema changes are undone.
    op.execute(sa.text("DROP INDEX IF EXISTS ux_orders_public_code"))
    op.execute(sa.text("ALTER TABLE orders DROP CONSTRAINT IF EXISTS fk_orders_courier_staff_id"))
    op.execute(sa.text("ALTER TABLE orders DROP CONSTRAINT IF EXISTS fk_orders_accepted_by_staff_id"))

    for col in (
        "delivered_at", "courier_assigned_at", "courier_staff_id",
        "rejection_reason", "rejected_at", "accepted_by_staff_id", "accepted_at",
        "cash_change_for", "tip_amount", "delivery_lon", "delivery_lat",
        "customer_phone", "customer_name", "public_code",
    ):
        op.execute(sa.text(f"ALTER TABLE orders DROP COLUMN IF EXISTS {col}"))

    op.execute(sa.text("ALTER TABLE locations DROP COLUMN IF EXISTS delivery_config"))
