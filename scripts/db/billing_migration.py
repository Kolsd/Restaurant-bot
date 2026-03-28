"""
Mesio — Migración de base de datos para módulo de Billing
Ejecuta esto en startup o como script independiente.
"""

import asyncio
import os
import asyncpg


async def run_billing_migrations():
    """Añade las tablas y columnas necesarias para el módulo de billing."""
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL no configurada")
    database_url = database_url.replace("postgres://", "postgresql://", 1)

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:
        # 1. Columna billing_config en restaurants
        try:
            await conn.execute(
                "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS billing_config JSONB DEFAULT NULL"
            )
            print("✅ Columna billing_config añadida a restaurants")
        except Exception as e:
            print(f"  ⚠️  billing_config: {e}")

        # 2. Tabla billing_log
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS billing_log (
                id            SERIAL PRIMARY KEY,
                restaurant_id INTEGER NOT NULL,
                order_id      TEXT    NOT NULL DEFAULT '',
                provider      TEXT    NOT NULL DEFAULT '',
                status        TEXT    NOT NULL DEFAULT 'pending',
                external_id   TEXT    NOT NULL DEFAULT '',
                error_message TEXT    NOT NULL DEFAULT '',
                created_at    TIMESTAMP DEFAULT NOW()
            );
        """)
        print("✅ Tabla billing_log lista")

        # Índices
        indices = [
            "CREATE INDEX IF NOT EXISTS idx_billing_log_restaurant ON billing_log(restaurant_id)",
            "CREATE INDEX IF NOT EXISTS idx_billing_log_order ON billing_log(order_id)",
            "CREATE INDEX IF NOT EXISTS idx_billing_log_created ON billing_log(created_at DESC)",
        ]
        for idx in indices:
            try:
                await conn.execute(idx)
            except Exception:
                pass

    await pool.close()
    print("✅ Migraciones de billing completadas")


if __name__ == "__main__":
    asyncio.run(run_billing_migrations())