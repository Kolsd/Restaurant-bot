"""Bootstrap table/sequence/schema privileges for mesio_app and mesio_superadmin.

WHY (P0 — fresh deploy cannot boot):
  Migration 0029 CREATEs the mesio_superadmin role and grants ROLE MEMBERSHIP
  to current_user (so `SET LOCAL ROLE mesio_superadmin` succeeds), but never
  grants TABLE or SEQUENCE privileges to it. BYPASSRLS only skips the RLS
  policy filter — Postgres still enforces the normal GRANT system underneath.
  Without explicit grants, `SET LOCAL ROLE mesio_superadmin` followed by any
  SELECT/INSERT/UPDATE/DELETE fails with "permission denied for table ...".

  Only two tables (qr_scan_pending in 0056, phone_blocklist in 0060) ever
  received a GRANT ... TO mesio_app statement in version control. Every other
  table — all ~34 RLS tables from 0029 plus everything from 0001-0028 — has
  never had a GRANT applied by a migration. In the existing prod/dev database
  this "works" only because someone ran manual GRANT statements by hand
  outside of git history (see CLAUDE.md "Roles Postgres (Fase 1 RLS)"). A
  brand-new database built via `alembic upgrade head` alone has never had
  those manual grants and is broken from the first `bypass_tenant_scope()`
  call onward (0071_demo_seed is the first migration that actually exercises
  the bypass role, which is why the break surfaces there).

WHAT THIS DOES:
  1. GRANT USAGE ON SCHEMA public to both roles (needed to even see objects
     in the schema).
  2. GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public to
     both roles — catches up every table created by migrations 0001-0078.
  3. GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public to both roles —
     nextval()/currval() on every serial/bigserial PK.
  4. ALTER DEFAULT PRIVILEGES IN SCHEMA public for both roles, so tables and
     sequences created by FUTURE migrations (which all run as the same
     DATABASE_URL_ADMIN role) automatically grant these privileges without
     needing a manual GRANT in every new migration going forward. This does
     NOT retroactively fix a forgotten per-table grant in a future migration
     for a role other than the one running migrations, but it means the
     common case (new RLS table, same admin/migration role) is covered
     without anyone remembering to add GRANT statements.

  Both roles are treated identically here: mesio_app is the runtime
  non-superuser role (RLS enforces per-tenant reads), mesio_superadmin is the
  BYPASSRLS role used only via bypass_tenant_scope() for genuinely
  cross-tenant operations. Table-level privileges are the same for both; the
  RLS policies (or their absence, for BYPASSRLS) are what differ.

IDEMPOTENT + GUARDED:
  - GRANT is naturally idempotent in Postgres (re-granting an already-held
    privilege is a no-op, not an error).
  - Both DO blocks check pg_roles first so this migration does not fail in
    an environment where one of the two roles does not exist (e.g. a role
    was renamed/dropped, or this is applied out of the usual order in a
    throwaway test database).

Revision ID: 0079_bootstrap_role_grants
Revises:     0078_crm_lost_reason
Create Date: 2026-05-08
"""

import logging

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision = "0079_bootstrap_role_grants"
down_revision = "0078_crm_lost_reason"
branch_labels = None
depends_on = None

_ROLES = ("mesio_app", "mesio_superadmin")


def upgrade() -> None:
    for role in _ROLES:
        logger.info("0079 upgrade: granting schema/table/sequence privileges to %s (if role exists)", role)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'GRANT USAGE ON SCHEMA public TO {role}';
                    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}';
                    EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}';
                    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}';
                    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {role}';
                ELSE
                    RAISE NOTICE '0079: role % does not exist, skipping grants', '{role}';
                END IF;
            END $$;
            """
        )

    logger.info("0079 upgrade: done")


def downgrade() -> None:
    for role in reversed(_ROLES):
        logger.info("0079 downgrade: revoking schema/table/sequence privileges from %s (if role exists)", role)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {role}';
                    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {role}';
                    EXECUTE 'REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {role}';
                    EXECUTE 'REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM {role}';
                    EXECUTE 'REVOKE USAGE ON SCHEMA public FROM {role}';
                END IF;
            END $$;
            """
        )

    logger.info("0079 downgrade: done")
