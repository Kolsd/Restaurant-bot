"""The carta per sede — the organization's menu plus each sede's changes.

PM 2026-09-20: each sede is its own restaurant. The carta stays ONE menu
owned by the organization (`organizations.menu`), and each sede may, on top
of it (PM 2026-09-21):

  - charge its own price for a dish,
  - hide a dish it does not serve,
  - add dishes of its own that no other sede sells.

Two tables, both keyed by sede and matched to the base menu by dish NAME,
case-insensitively — the same identity `menu_availability` (0091) and the
per-sede inventory (0090) already use, since most dishes carry no `sku`.

`location_menu_overrides` — one row per (sede, base dish) that the sede
changed. `price` NULL means "the base price"; a sede price survives later
changes to the base price until someone removes it (PM 2026-09-21).

`location_menu_dishes` — dishes only this sede sells, stored in the same
normalized shape as a dish inside `organizations.menu`.

Revision ID: 0093_sede_menu_overrides
Revises:     0092_restaurants_display_name
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0093_sede_menu_overrides"
down_revision = "0092_restaurants_display_name"
branch_labels = None
depends_on = None


_POLICY = """
    CREATE POLICY org_isolation ON {table}
        USING (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint)
        WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint)
"""


def _protect(table: str, *, has_sequence: bool) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS org_isolation ON {table}"))
    op.execute(sa.text(_POLICY.format(table=table)))
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO mesio_app"))
    op.execute(sa.text(f"GRANT SELECT ON {table} TO mesio_superadmin"))
    if has_sequence:
        op.execute(sa.text(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO mesio_app"))


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS location_menu_overrides (
            org_id       BIGINT        NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            location_id  BIGINT        NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            dish_name    TEXT          NOT NULL CHECK (btrim(dish_name) <> ''),
            price        NUMERIC(12,2) NULL CHECK (price IS NULL OR price >= 0),
            hidden       BOOLEAN       NOT NULL DEFAULT FALSE,
            updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        )
    """))
    op.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_location_menu_overrides_dish
            ON location_menu_overrides (org_id, location_id, lower(dish_name))
    """))
    _protect("location_menu_overrides", has_sequence=False)

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS location_menu_dishes (
            id           BIGSERIAL     PRIMARY KEY,
            org_id       BIGINT        NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            location_id  BIGINT        NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            category     TEXT          NOT NULL CHECK (btrim(category) <> ''),
            dish_name    TEXT          NOT NULL CHECK (btrim(dish_name) <> ''),
            dish         JSONB         NOT NULL,
            updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        )
    """))
    op.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_location_menu_dishes_dish
            ON location_menu_dishes (org_id, location_id, lower(dish_name))
    """))
    _protect("location_menu_dishes", has_sequence=True)


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS location_menu_dishes"))
    op.execute(sa.text("DROP TABLE IF EXISTS location_menu_overrides"))
