"""Add username column to staff for unique login identity.

Auto-generates username as firstname.lastname from existing name column.
Duplicates get a numeric suffix (juan.perez1, juan.perez2).

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-13
"""

from alembic import op

revision      = "0019"
down_revision = "0018"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE staff ADD COLUMN IF NOT EXISTS username TEXT;
    """)

    # Backfill existing staff with auto-generated usernames.
    # Logic: take first word as firstname, second word as lastname.
    # Normalize: lowercase, remove accents (basic transliteration).
    # Handle duplicates with numeric suffix.
    op.execute("""
        CREATE OR REPLACE FUNCTION _generate_staff_username(full_name TEXT) RETURNS TEXT AS $$
        DECLARE
            parts TEXT[];
            fname TEXT;
            lname TEXT;
            base_username TEXT;
            candidate TEXT;
            counter INT := 0;
        BEGIN
            -- Split name, normalize
            parts := string_to_array(LOWER(TRIM(full_name)), ' ');
            fname := COALESCE(parts[1], 'user');
            lname := COALESCE(parts[2], '');

            -- Remove accents (basic transliteration)
            fname := TRANSLATE(fname, 'áéíóúüñàèìòùâêîôû', 'aeiouunaeiouaeiou');
            lname := TRANSLATE(lname, 'áéíóúüñàèìòùâêîôû', 'aeiouunaeiouaeiou');

            -- Remove non-alphanumeric
            fname := REGEXP_REPLACE(fname, '[^a-z0-9]', '', 'g');
            lname := REGEXP_REPLACE(lname, '[^a-z0-9]', '', 'g');

            IF lname = '' THEN
                base_username := fname;
            ELSE
                base_username := fname || '.' || lname;
            END IF;

            -- Find unique username
            candidate := base_username;
            WHILE EXISTS (SELECT 1 FROM staff WHERE username = candidate) LOOP
                counter := counter + 1;
                candidate := base_username || counter::TEXT;
            END LOOP;

            RETURN candidate;
        END;
        $$ LANGUAGE plpgsql;
    """)

    # Backfill all existing staff
    op.execute("""
        UPDATE staff SET username = _generate_staff_username(name) WHERE username IS NULL;
    """)

    # Now make it NOT NULL and UNIQUE
    op.execute("""
        ALTER TABLE staff ALTER COLUMN username SET NOT NULL;
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_staff_username ON staff (username);
    """)

    # Drop the helper function
    op.execute("DROP FUNCTION IF EXISTS _generate_staff_username(TEXT);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_staff_username;")
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS username;")
