"""Repair jsonb values that were stored as JSON strings.

The app pool's jsonb codec serialized every parameter with `json.dumps`, and
many writers passed an already-serialized `json.dumps(x)` to `$n::jsonb`: the
value was serialized twice and stored as a JSON *string* holding the object
(`'"{\\"a\\": 1}"'` instead of `{"a": 1}`). Readers coped because they all
`json.loads` a str, but SQL over those columns (`->`, `jsonb_array_elements`,
`@>`) saw a scalar string — e.g. stats over order items or the NPS/kitchen
queries that look inside carts and checks.

The codec now leaves a str alone (it is JSON text already); this revision
unwraps what was stored before, in every jsonb column of every table: a
string whose text parses as a JSON object or array becomes that object or
array. Repeated a few times in case a value was wrapped more than once. Any
other string is left untouched.

Revision ID: 0100_repair_double_encoded_jsonb
Revises:     0099_drop_whatsapp_columns
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0100_repair_double_encoded_jsonb"
down_revision = "0099_drop_whatsapp_columns"
branch_labels = None
depends_on = None


_REPAIR = r"""
CREATE OR REPLACE FUNCTION pg_temp._mesio_try_jsonb(t text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $f$
BEGIN
    RETURN t::jsonb;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $f$;

DO $$
DECLARE
    col record;
    pass int;
BEGIN
    FOR pass IN 1..3 LOOP
        FOR col IN
            SELECT c.table_name, c.column_name
              FROM information_schema.columns c
              JOIN information_schema.tables t
                ON t.table_schema = c.table_schema AND t.table_name = c.table_name
             WHERE c.table_schema = 'public'
               AND c.data_type = 'jsonb'
               AND t.table_type = 'BASE TABLE'
        LOOP
            EXECUTE format(
                'UPDATE %I SET %I = pg_temp._mesio_try_jsonb(%I #>> ''{}'')
                  WHERE jsonb_typeof(%I) = ''string''
                    AND jsonb_typeof(pg_temp._mesio_try_jsonb(%I #>> ''{}'')) IN (''object'', ''array'')',
                col.table_name, col.column_name, col.column_name,
                col.column_name, col.column_name
            );
        END LOOP;
    END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(sa.text(_REPAIR))


def downgrade() -> None:
    # Data repair: the old values were wrong (JSON strings wrapping objects).
    pass
