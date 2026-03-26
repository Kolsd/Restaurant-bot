"""Initial schema — captures all tables created by db_init_* functions.

On existing production databases run:
    alembic stamp 0001

On a fresh database run:
    alembic upgrade head

Revision ID: 0001
Revises:
Create Date: 2026-03-25
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All statements use IF NOT EXISTS / IF EXISTS so the migration is safe
    # to apply against databases that already have these tables.

    # ── Core tables ──────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS restaurants (
            id                    SERIAL PRIMARY KEY,
            name                  TEXT NOT NULL,
            whatsapp_number       TEXT NOT NULL UNIQUE,
            address               TEXT NOT NULL DEFAULT '',
            menu                  JSONB NOT NULL DEFAULT '{}'::jsonb,
            subscription_status   TEXT NOT NULL DEFAULT 'active',
            features              JSONB NOT NULL DEFAULT '{}'::jsonb,
            billing_config        JSONB DEFAULT NULL,
            created_at            TIMESTAMP DEFAULT NOW(),
            parent_restaurant_id  INTEGER,
            latitude              NUMERIC(10,7),
            longitude             NUMERIC(10,7),
            google_maps_url       TEXT DEFAULT '',
            wa_phone_id           TEXT DEFAULT '',
            wa_access_token       TEXT DEFAULT ''
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS billing_log (
            id            SERIAL PRIMARY KEY,
            restaurant_id INTEGER NOT NULL,
            order_id      TEXT    NOT NULL DEFAULT '',
            provider      TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL DEFAULT 'pending',
            external_id   TEXT    NOT NULL DEFAULT '',
            error_message TEXT    NOT NULL DEFAULT '',
            created_at    TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id         SERIAL PRIMARY KEY,
            name       TEXT NOT NULL,
            date       TEXT NOT NULL,
            time       TEXT NOT NULL,
            guests     INTEGER NOT NULL,
            phone      TEXT NOT NULL,
            bot_number TEXT NOT NULL DEFAULT '',
            notes      TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id             TEXT PRIMARY KEY,
            phone          TEXT NOT NULL,
            items          JSONB NOT NULL,
            order_type     TEXT NOT NULL,
            address        TEXT DEFAULT '',
            notes          TEXT DEFAULT '',
            subtotal       INTEGER NOT NULL,
            delivery_fee   INTEGER DEFAULT 0,
            total          INTEGER NOT NULL,
            status         TEXT DEFAULT 'pendiente_pago',
            paid           BOOLEAN DEFAULT FALSE,
            payment_url    TEXT DEFAULT '',
            transaction_id TEXT DEFAULT '',
            bot_number     TEXT NOT NULL DEFAULT '',
            created_at     TIMESTAMP DEFAULT NOW(),
            paid_at        TIMESTAMP,
            payment_method TEXT DEFAULT '',
            base_order_id  TEXT,
            sub_number     INTEGER DEFAULT 1
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            phone      TEXT NOT NULL,
            bot_number TEXT NOT NULL DEFAULT '',
            history    JSONB NOT NULL DEFAULT '[]',
            bot_paused BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT NOW(),
            created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (phone, bot_number)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username        TEXT PRIMARY KEY,
            password_hash   TEXT NOT NULL,
            restaurant_name TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'owner',
            branch_id       INTEGER,
            parent_user     TEXT,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP DEFAULT NOW() + INTERVAL '72 hours'
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS carts (
            phone      TEXT NOT NULL,
            bot_number TEXT NOT NULL,
            cart_data  JSONB NOT NULL DEFAULT '{"items": [], "order_type": null, "address": null, "notes": ""}'::jsonb,
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (phone, bot_number)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS meta_rate_limits (
            id         SERIAL PRIMARY KEY,
            phone      TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS menu_availability (
            dish_name  TEXT PRIMARY KEY,
            available  BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # ── NPS & Inventory ──────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS nps_responses (
            id         SERIAL PRIMARY KEY,
            phone      TEXT NOT NULL,
            bot_number TEXT NOT NULL DEFAULT '',
            score      INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
            comment    TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id            SERIAL PRIMARY KEY,
            restaurant_id INTEGER NOT NULL,
            name          TEXT NOT NULL,
            unit          TEXT NOT NULL DEFAULT 'unidades',
            current_stock NUMERIC(10,2) NOT NULL DEFAULT 0,
            min_stock     NUMERIC(10,2) NOT NULL DEFAULT 0,
            linked_dishes JSONB NOT NULL DEFAULT '[]'::jsonb,
            cost_per_unit NUMERIC(10,2) DEFAULT 0,
            created_at    TIMESTAMP DEFAULT NOW(),
            updated_at    TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS inventory_history (
            id             SERIAL PRIMARY KEY,
            inventory_id   INTEGER NOT NULL,
            quantity_delta NUMERIC(10,2) NOT NULL,
            stock_after    NUMERIC(10,2) NOT NULL,
            reason         TEXT NOT NULL DEFAULT 'ajuste_manual',
            created_at     TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS nps_waiting (
            phone      TEXT NOT NULL,
            bot_number TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (phone, bot_number)
        )
    """)

    # ── Tables & Kitchen ─────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS restaurant_tables (
            id         TEXT PRIMARY KEY,
            number     INTEGER NOT NULL,
            name       TEXT NOT NULL,
            branch_id  INTEGER,
            active     BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS table_orders (
            id            TEXT PRIMARY KEY,
            table_id      TEXT NOT NULL,
            table_name    TEXT NOT NULL,
            phone         TEXT NOT NULL,
            items         JSONB NOT NULL DEFAULT '[]',
            status        TEXT DEFAULT 'recibido',
            notes         TEXT DEFAULT '',
            total         INTEGER DEFAULT 0,
            created_at    TIMESTAMP DEFAULT NOW(),
            updated_at    TIMESTAMP DEFAULT NOW(),
            base_order_id TEXT DEFAULT NULL,
            sub_number    INTEGER DEFAULT 1,
            station       TEXT NOT NULL DEFAULT 'all'
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS waiter_alerts (
            id         SERIAL PRIMARY KEY,
            table_id   TEXT    NOT NULL DEFAULT '',
            table_name TEXT    NOT NULL DEFAULT '',
            phone      TEXT    NOT NULL,
            bot_number TEXT    NOT NULL DEFAULT '',
            alert_type TEXT    NOT NULL DEFAULT 'waiter',
            message    TEXT    NOT NULL DEFAULT '',
            dismissed  BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS table_sessions (
            id                 SERIAL PRIMARY KEY,
            table_id           TEXT    NOT NULL DEFAULT '',
            table_name         TEXT    NOT NULL DEFAULT '',
            phone              TEXT    NOT NULL,
            bot_number         TEXT    NOT NULL DEFAULT '',
            status             TEXT    NOT NULL DEFAULT 'active',
            has_order          BOOLEAN DEFAULT FALSE,
            order_delivered    BOOLEAN DEFAULT FALSE,
            inactivity_warned  BOOLEAN DEFAULT FALSE,
            last_activity      TIMESTAMP DEFAULT NOW(),
            started_at         TIMESTAMP DEFAULT NOW(),
            closed_at          TIMESTAMP,
            total_spent        INTEGER DEFAULT 0,
            closed_by          TEXT    DEFAULT '',
            closed_by_username TEXT    DEFAULT '',
            meta_phone_id      TEXT    DEFAULT '',
            summary            JSONB   DEFAULT '{}'::jsonb
        )
    """)

    # ── Fiscal / DIAN ────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS fiscal_resolution (
            id                SERIAL PRIMARY KEY,
            restaurant_id     INTEGER NOT NULL UNIQUE,
            resolution_number TEXT    NOT NULL,
            resolution_date   DATE    NOT NULL,
            prefix            TEXT    NOT NULL DEFAULT '',
            from_number       INTEGER NOT NULL,
            to_number         INTEGER NOT NULL,
            valid_from        DATE    NOT NULL,
            valid_to          DATE    NOT NULL,
            technical_key     TEXT    NOT NULL,
            current_number    INTEGER NOT NULL DEFAULT 0,
            environment       TEXT    NOT NULL DEFAULT 'test',
            software_id       TEXT    NOT NULL DEFAULT '',
            software_pin      TEXT    NOT NULL DEFAULT '',
            updated_at        TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS fiscal_invoices (
            id                  SERIAL PRIMARY KEY,
            billing_log_id      INTEGER REFERENCES billing_log(id) ON DELETE SET NULL,
            restaurant_id       INTEGER NOT NULL,
            order_id            TEXT    NOT NULL,
            resolution_number   TEXT    NOT NULL,
            prefix              TEXT    NOT NULL DEFAULT '',
            invoice_number      INTEGER NOT NULL,
            issue_date          DATE      NOT NULL DEFAULT CURRENT_DATE,
            issue_time          TIME      NOT NULL DEFAULT CURRENT_TIME,
            subtotal_cents      BIGINT    NOT NULL DEFAULT 0,
            tax_regime          TEXT      NOT NULL DEFAULT 'iva',
            tax_pct             NUMERIC(5,2) NOT NULL DEFAULT 19.00,
            tax_cents           BIGINT    NOT NULL DEFAULT 0,
            total_cents         BIGINT    NOT NULL DEFAULT 0,
            cufe                TEXT      NOT NULL DEFAULT '',
            qr_data             TEXT      NOT NULL DEFAULT '',
            uuid_dian           TEXT      NOT NULL DEFAULT '',
            xml_content         TEXT,
            pdf_url             TEXT,
            customer_nit        TEXT      NOT NULL DEFAULT '222222222',
            customer_name       TEXT      NOT NULL DEFAULT 'Consumidor Final',
            customer_email      TEXT      NOT NULL DEFAULT '',
            customer_id_type    TEXT      NOT NULL DEFAULT '13',
            payment_method      TEXT      NOT NULL DEFAULT 'cash',
            dian_status         TEXT      NOT NULL DEFAULT 'draft',
            dian_response       JSONB     DEFAULT NULL,
            created_at          TIMESTAMP DEFAULT NOW(),
            UNIQUE (restaurant_id, resolution_number, invoice_number)
        )
    """)

    # ── Escandallos (Dish Recipes) ───────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS dish_recipes (
            id            SERIAL PRIMARY KEY,
            restaurant_id INTEGER       NOT NULL,
            dish_name     TEXT          NOT NULL,
            ingredient_id INTEGER       NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
            quantity      NUMERIC(10,4) NOT NULL CHECK (quantity > 0),
            created_at    TIMESTAMP DEFAULT NOW(),
            updated_at    TIMESTAMP DEFAULT NOW(),
            UNIQUE (restaurant_id, dish_name, ingredient_id)
        )
    """)

    # ── Split Checks ─────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS table_checks (
            id                TEXT PRIMARY KEY,
            base_order_id     TEXT NOT NULL,
            check_number      SMALLINT NOT NULL,
            items             JSONB NOT NULL DEFAULT '[]',
            subtotal          NUMERIC(10,2) NOT NULL DEFAULT 0,
            tax_amount        NUMERIC(10,2) NOT NULL DEFAULT 0,
            total             NUMERIC(10,2) NOT NULL DEFAULT 0,
            payments          JSONB NOT NULL DEFAULT '[]',
            change_amount     NUMERIC(10,2) NOT NULL DEFAULT 0,
            status            TEXT NOT NULL DEFAULT 'open',
            fiscal_invoice_id INTEGER REFERENCES fiscal_invoices(id),
            customer_name     TEXT,
            customer_nit      TEXT,
            customer_email    TEXT,
            created_at        TIMESTAMP DEFAULT NOW(),
            paid_at           TIMESTAMP,
            UNIQUE (base_order_id, check_number)
        )
    """)

    # ── Indexes ──────────────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS idx_orders_bot_date       ON orders(bot_number, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_orders_phone          ON orders(phone)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_orders_paid_date      ON orders(paid, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_table_orders_table    ON table_orders(table_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_table_orders_phone    ON table_orders(phone)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_table_orders_base     ON table_orders(base_order_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_table_orders_station  ON table_orders(station)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_waiter_alerts_bot     ON waiter_alerts(bot_number, dismissed, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires      ON sessions(expires_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reservations_bot      ON reservations(bot_number, date ASC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_convs_updated         ON conversations(bot_number, updated_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_restaurants_wa        ON restaurants(whatsapp_number)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_rate_phone            ON meta_rate_limits(phone)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_table_sessions_active ON table_sessions(phone, bot_number, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_table_sessions_closed ON table_sessions(bot_number, closed_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_nps_bot_number        ON nps_responses(bot_number, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_inventory_restaurant  ON inventory(restaurant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_inv_history           ON inventory_history(inventory_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_nps_waiting_phone     ON nps_waiting(phone, bot_number)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_fiscal_invoices_rest  ON fiscal_invoices(restaurant_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_fiscal_invoices_order ON fiscal_invoices(order_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fiscal_invoices_cufe ON fiscal_invoices(cufe) WHERE cufe != ''")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dish_recipes_lookup   ON dish_recipes(restaurant_id, dish_name)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dish_recipes_ingred   ON dish_recipes(ingredient_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_table_checks_base     ON table_checks(base_order_id)")


def downgrade() -> None:
    # Intentionally left as pass to prevent accidental data loss in production.
    # To rollback, create a dedicated migration that drops only the intended tables.
    pass
