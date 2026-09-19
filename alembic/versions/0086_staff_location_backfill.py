"""Backfill staff.location_id where the org has exactly one active sede.

Staff created through the app never got a sede: staff_repo.db_create_staff
did not write location_id, and both creation routes dropped the sede they
had resolved. The staff JWT carries staff.location_id, and sede-scoped
sections (the cashier's Domicilios) refuse a staff member without one — so
every real cashier was locked out.

Only the unambiguous case is filled in: an org with exactly ONE active sede.
In a multi-sede org, picking one would be a guess; those rows stay NULL and
are assigned from the team UI.

Revision ID: 0086_staff_location_backfill
Revises:     0085_menu_availability_org_pk
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0086_staff_location_backfill"
down_revision = "0085_menu_availability_org_pk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE staff s
           SET location_id = only_loc.id
          FROM (
                SELECT org_id, min(id) AS id
                  FROM locations
                 WHERE active = true
                 GROUP BY org_id
                HAVING count(*) = 1
               ) AS only_loc
         WHERE s.org_id = only_loc.org_id
           AND s.location_id IS NULL
    """))


def downgrade() -> None:
    # Data backfill: the rows it filled are indistinguishable from staff
    # assigned deliberately afterwards, so there is nothing safe to undo.
    pass
