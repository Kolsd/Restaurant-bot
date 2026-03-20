"""
Mesio — Migración CRM de Prospectos
Tablas: prospects, prospect_notes, prospect_interactions, crm_templates
"""
import asyncio
import os
import asyncpg


async def run_crm_migrations():
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL no configurada")
    database_url = database_url.replace("postgres://", "postgresql://", 1)

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:

        # ── 1. PROSPECTS ──────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prospects (
                id            SERIAL PRIMARY KEY,
                restaurant_name TEXT    NOT NULL,
                owner_name      TEXT    NOT NULL DEFAULT '',
                phone           TEXT    NOT NULL,
                city            TEXT    NOT NULL DEFAULT '',
                neighborhood    TEXT    NOT NULL DEFAULT '',
                category        TEXT    NOT NULL DEFAULT '',
                instagram       TEXT    NOT NULL DEFAULT '',
                google_maps     TEXT    NOT NULL DEFAULT '',
                source          TEXT    NOT NULL DEFAULT 'manual',
                stage           TEXT    NOT NULL DEFAULT 'prospecto',
                priority        TEXT    NOT NULL DEFAULT 'medium',
                assigned_to     TEXT    NOT NULL DEFAULT '',
                last_contact_at TIMESTAMP,
                next_follow_up  TIMESTAMP,
                revenue_est     INTEGER DEFAULT 0,
                tags            TEXT[]  DEFAULT '{}',
                archived        BOOLEAN DEFAULT FALSE,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
        """)
        print("✅ Tabla prospects lista")

        # ── 2. NOTES ──────────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prospect_notes (
                id          SERIAL PRIMARY KEY,
                prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
                author      TEXT    NOT NULL DEFAULT 'miguel',
                content     TEXT    NOT NULL,
                note_type   TEXT    NOT NULL DEFAULT 'note',
                created_at  TIMESTAMP DEFAULT NOW()
            );
        """)
        print("✅ Tabla prospect_notes lista")

        # ── 3. INTERACTIONS (WA messages sent/received) ──────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prospect_interactions (
                id           SERIAL PRIMARY KEY,
                prospect_id  INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
                direction    TEXT    NOT NULL DEFAULT 'outbound',
                channel      TEXT    NOT NULL DEFAULT 'whatsapp',
                content      TEXT    NOT NULL,
                template_name TEXT   NOT NULL DEFAULT '',
                status       TEXT    NOT NULL DEFAULT 'sent',
                wa_message_id TEXT   NOT NULL DEFAULT '',
                created_at   TIMESTAMP DEFAULT NOW()
            );
        """)
        print("✅ Tabla prospect_interactions lista")

        # ── 4. TEMPLATES ──────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS crm_templates (
                id          SERIAL PRIMARY KEY,
                name        TEXT    NOT NULL UNIQUE,
                wa_name     TEXT    NOT NULL DEFAULT '',
                category    TEXT    NOT NULL DEFAULT 'MARKETING',
                language    TEXT    NOT NULL DEFAULT 'es',
                body        TEXT    NOT NULL,
                params      TEXT[]  DEFAULT '{}',
                active      BOOLEAN DEFAULT TRUE,
                created_at  TIMESTAMP DEFAULT NOW()
            );
        """)
        print("✅ Tabla crm_templates lista")

        # ── Índices ────────────────────────────────────────────────────────
        indices = [
            "CREATE INDEX IF NOT EXISTS idx_prospects_stage   ON prospects(stage)",
            "CREATE INDEX IF NOT EXISTS idx_prospects_phone   ON prospects(phone)",
            "CREATE INDEX IF NOT EXISTS idx_prospects_archived ON prospects(archived)",
            "CREATE INDEX IF NOT EXISTS idx_interactions_pid  ON prospect_interactions(prospect_id)",
            "CREATE INDEX IF NOT EXISTS idx_notes_pid         ON prospect_notes(prospect_id)",
        ]
        for idx in indices:
            try:
                await conn.execute(idx)
            except Exception:
                pass

        # ── Template por defecto ───────────────────────────────────────────
        await conn.execute("""
            INSERT INTO crm_templates (name, wa_name, category, body, params)
            VALUES
              ('Prospección inicial',
               'mesio_prospeccion_v1',
               'MARKETING',
               'Hola {{1}}, vi que tienen {{2}} y quería hacerles una pregunta rápida — ¿reciben pedidos por WhatsApp o solo por Rappi? Tenemos algo que podría ahorrarles la comisión. 🙋',
               ARRAY['nombre del dueño', 'nombre del restaurante']),
              ('Follow-up demo',
               'mesio_followup_demo_v1',
               'MARKETING',
               'Hola {{1}}! Les comparto el demo de Mesio para que vean cómo funcionaría para {{2}}: mesioai.com/demo — ¿tienen 15 minutos esta semana para una llamada rápida?',
               ARRAY['nombre', 'restaurante']),
              ('Cierre',
               'mesio_cierre_v1',
               'MARKETING',
               'Hola {{1}}, quería saber si pudieron ver el demo de Mesio. Tenemos el plan Starter desde $49 USD/mes y podemos tenerlo configurado en 48h. ¿Arrancamos esta semana?',
               ARRAY['nombre'])
            ON CONFLICT (name) DO NOTHING;
        """)
        print("✅ Templates por defecto insertados")

    await pool.close()
    print("✅ Migración CRM completada")


if __name__ == "__main__":
    asyncio.run(run_crm_migrations())