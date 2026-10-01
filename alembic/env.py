import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from alembic import context

# ── Alembic Config object ─────────────────────────────────────────────────────
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Database URL ──────────────────────────────────────────────────────────────
# Railway provides DATABASE_URL as postgres://... which psycopg2 understands
# after replacing the scheme.  asyncpg handles this at runtime separately.
#
# Migrations need SUPERUSER privileges (CREATE ROLE, ALTER SYSTEM, etc.) — prefer
# DATABASE_URL_ADMIN if set.  Runtime (app) connects as non-superuser via
# DATABASE_URL so Row-Level Security actually enforces.  If DATABASE_URL_ADMIN
# is not set we fall back to DATABASE_URL for backward compatibility.
_database_url = (
    os.environ.get("DATABASE_URL_ADMIN")
    or os.environ.get("DATABASE_URL")
    or os.environ.get("PROD_DATABASE_URL", "")
)
if not _database_url:
    raise RuntimeError(
        "No database URL found. Set one of: DATABASE_URL_ADMIN, DATABASE_URL, "
        "or PROD_DATABASE_URL environment variable."
    )
# Normalize to postgresql:// (required by SQLAlchemy / psycopg2)
if _database_url.startswith("postgres://"):
    _database_url = _database_url.replace("postgres://", "postgresql://", 1)
# Force psycopg2 driver for synchronous migration runner
if "+asyncpg" in _database_url:
    _database_url = _database_url.replace("+asyncpg", "+psycopg2", 1)
elif "postgresql://" in _database_url and "+psycopg2" not in _database_url:
    _database_url = _database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

config.set_main_option("sqlalchemy.url", _database_url)

# No SQLAlchemy metadata — Mesio uses raw asyncpg at runtime.
# Migrations are written as plain SQL via op.execute().
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations using a URL string (no live connection needed)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using a live synchronous connection.

    transaction_per_migration=True: each migration commits in its own
    transaction instead of the whole run (0001..head) sharing one outer
    transaction. Two reasons this matters here:

      1. Fail-fast, not fail-everything. Without this, one broken migration
         at revision N rolled back N-1 good migrations too — a fresh
         `alembic upgrade head` had to restart from scratch after any fix
         instead of resuming from N.
      2. SET LOCAL ROLE containment. Some migrations (e.g. 0071_demo_seed)
         do `SET LOCAL ROLE mesio_superadmin` to pass RLS WITH CHECK on
         INSERTs. `SET LOCAL` is scoped to the current transaction — with a
         single outer transaction for the entire run, that role leaked into
         every subsequent migration for the rest of the batch (observed:
         0070/later CREATE TABLE statements failing because they
         unexpectedly ran as mesio_superadmin, which lacks CREATE on schema
         public). Per-migration transactions mean the role reverts the
         moment 0071's transaction commits, regardless of whether the
         migration itself remembers to RESET ROLE.

    No CREATE INDEX ... CONCURRENTLY migration exists in this repo (0032 and
    0073 both explicitly avoid it — see their docstrings — because
    CONCURRENTLY cannot run inside any transaction block, per-migration or
    not). So there is nothing here that this change could break on that
    front; verified by grepping alembic/versions for CONCURRENTLY.
    """
    connectable = create_engine(_database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
