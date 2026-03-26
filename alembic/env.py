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
_database_url = os.environ.get("DATABASE_URL", "")
if not _database_url:
    raise RuntimeError("DATABASE_URL environment variable is not set.")
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
    """Run migrations using a live synchronous connection."""
    connectable = create_engine(_database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
