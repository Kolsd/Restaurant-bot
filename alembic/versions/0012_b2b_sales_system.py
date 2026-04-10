"""B2B Sales System — AI-powered sales agent tables.

Creates:
  - sales_inbox: durable inbox for inbound sales channel messages
  - sales_knowledge_base: product knowledge for the sales agent
  - sales_conversations: per-prospect conversation state
  - sales_escalations: human-handoff queue
  - ALTER prospects: add enrichment columns for lead scoring

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-10
"""
from alembic import op

revision      = "0012"
down_revision = "0011"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── Sales inbox (durable queue, mirrors webhook_inbox pattern) ────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS sales_inbox (
        id              BIGSERIAL PRIMARY KEY,
        external_id     TEXT,
        payload         JSONB NOT NULL,
        received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        processed_at    TIMESTAMPTZ,
        attempts        INT NOT NULL DEFAULT 0,
        last_error      TEXT,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)

    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_inbox_pending
        ON sales_inbox (next_attempt_at)
        WHERE processed_at IS NULL
    """)

    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_inbox_dedup
        ON sales_inbox (external_id)
        WHERE external_id IS NOT NULL
    """)

    # ── Sales knowledge base ──────────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS sales_knowledge_base (
        id         SERIAL PRIMARY KEY,
        category   TEXT NOT NULL,
        title      TEXT NOT NULL,
        content    TEXT NOT NULL,
        priority   INT  NOT NULL DEFAULT 0,
        active     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)

    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_skb_category_active
        ON sales_knowledge_base (category, active)
    """)

    # ── Sales conversations ───────────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS sales_conversations (
        id              BIGSERIAL PRIMARY KEY,
        prospect_id     INT REFERENCES prospects(id) ON DELETE SET NULL,
        phone           TEXT NOT NULL,
        channel         TEXT NOT NULL DEFAULT 'whatsapp',
        messages        JSONB NOT NULL DEFAULT '[]'::jsonb,
        agent_state     TEXT NOT NULL DEFAULT 'greeting',
        context         JSONB NOT NULL DEFAULT '{}'::jsonb,
        escalation      TEXT,
        escalated_at    TIMESTAMPTZ,
        resolved_at     TIMESTAMPTZ,
        token_count     INT NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)

    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_conv_phone
        ON sales_conversations (phone)
    """)

    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_conv_prospect
        ON sales_conversations (prospect_id)
        WHERE prospect_id IS NOT NULL
    """)

    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_conv_escalated
        ON sales_conversations (escalated_at)
        WHERE escalation IS NOT NULL AND resolved_at IS NULL
    """)

    # ── Sales escalations ─────────────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS sales_escalations (
        id               SERIAL PRIMARY KEY,
        conversation_id  BIGINT NOT NULL REFERENCES sales_conversations(id) ON DELETE CASCADE,
        prospect_id      INT REFERENCES prospects(id) ON DELETE SET NULL,
        reason           TEXT NOT NULL,
        agent_summary    TEXT NOT NULL,
        suggested_action TEXT,
        status           TEXT NOT NULL DEFAULT 'pending',
        assigned_to      TEXT,
        resolution_note  TEXT,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at      TIMESTAMPTZ
    )
    """)

    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_esc_pending
        ON sales_escalations (created_at)
        WHERE status = 'pending'
    """)

    # ── Enrich prospects table for lead scoring ───────────────────────────
    op.execute("""
    ALTER TABLE prospects
        ADD COLUMN IF NOT EXISTS email              TEXT,
        ADD COLUMN IF NOT EXISTS website            TEXT,
        ADD COLUMN IF NOT EXISTS employee_count     INT,
        ADD COLUMN IF NOT EXISTS monthly_orders_est INT,
        ADD COLUMN IF NOT EXISTS current_solution   TEXT,
        ADD COLUMN IF NOT EXISTS lead_score         INT NOT NULL DEFAULT 0
    """)

    # ── Seed knowledge base ───────────────────────────────────────────────
    op.execute("""
    INSERT INTO sales_knowledge_base (category, title, content, priority) VALUES
    ('product', 'Qué es Mesio',
     'Mesio es un sistema operativo para restaurantes impulsado por IA. Combina WhatsApp bot para pedidos y atención al cliente, POS táctil para mesas, gestión de domicilios, inventario en tiempo real, nómina y propinas automáticas, facturación electrónica DIAN y analytics. Todo en una sola plataforma multi-sucursal.',
     100),

    ('product', 'Módulos principales',
     'Módulos incluidos: (1) Bot WhatsApp con IA para pedidos, preguntas frecuentes y NPS; (2) POS de mesas con split-check y propinas; (3) Gestión de domicilios con triangulación GPS; (4) Inventario y escandallos (recetas); (5) Staff HQ: turnos, nómina, contratos, biometría FIDO2; (6) Facturación electrónica DIAN; (7) CRM y campañas; (8) Dashboard analytics multi-sucursal.',
     90),

    ('pricing', 'Planes y precios',
     'Mesio ofrece tres planes: Starter (1 sucursal, hasta 3 empleados, bot + POS básico), Growth (hasta 5 sucursales, empleados ilimitados, todos los módulos) y Enterprise (sucursales ilimitadas, SLA 99.9%, onboarding dedicado, integraciones custom). Los precios se cotizan según el número de sucursales y volumen de pedidos mensuales. Solicitar demo para cotización personalizada.',
     80),

    ('objection', 'Ya tenemos un sistema POS',
     'Mesio no es solo un POS — es la capa de IA encima de las operaciones. Muchos clientes usan Mesio junto a su POS actual para agregar el bot de WhatsApp, gestión de domicilios con GPS y analytics centralizado. La integración toma menos de una semana. El ROI promedio es 3x en reducción de llamadas y errores de pedidos.',
     70),

    ('objection', 'Es muy caro / no tenemos presupuesto',
     'Mesio se paga solo: el bot reduce el tiempo de atención por pedido de 8 min a menos de 1 min, y la nómina automática elimina 4-6 horas de trabajo manual por semana. Ofrecemos un período de prueba de 30 días sin costo. Después del piloto, el 94% de los restaurantes renuevan porque el ahorro operativo supera el costo de la suscripción.',
     70),

    ('objection', 'No tenemos tiempo para implementar',
     'El onboarding estándar toma 3 días: Día 1 configuración del bot y menú, Día 2 capacitación del equipo, Día 3 go-live con soporte en vivo. Nuestro equipo migra el menú existente y configura los números de WhatsApp. No se requiere hardware adicional — corre en cualquier tablet o computador.',
     65),

    ('competitor', 'vs. sistemas POS tradicionales (e.g. Siigo, Revel)',
     'Los POS tradicionales no tienen IA conversacional ni bot de WhatsApp nativo. Mesio agrega una capa de automatización encima: el bot atiende pedidos 24/7, el inventario se descuenta automáticamente por cada pedido del bot o del POS, y los reportes consolidan ambos canales. No es reemplazo — es potenciador.',
     60),

    ('competitor', 'vs. plataformas de delivery (Rappi, iFood)',
     'Rappi cobra entre 25-35% de comisión por pedido. Con Mesio, el canal de WhatsApp propio cobra 0% de comisión — el restaurante es dueño del cliente. Un restaurante con 200 pedidos/mes a $40.000 COP promedio ahorra $2-2.8M COP/mes en comisiones versus Rappi. Mesio y Rappi pueden coexistir.',
     60),

    ('feature', 'Biometría y control de asistencia',
     'Mesio incluye reloj biométrico FIDO2 (huella dactilar o reconocimiento facial según el dispositivo) para clock-in/out del personal. Las deducciones por tardanza o salida temprana se calculan automáticamente. Los turnos planeados vs reales se comparan en el dashboard con badges de cumplimiento. Todo integrado con la nómina.',
     50),

    ('feature', 'Facturación electrónica DIAN',
     'Mesio genera facturas electrónicas válidas ante la DIAN directamente desde el POS o desde el bot de WhatsApp. Soporta resoluciones de facturación propias o por habilitación. Los documentos se envían automáticamente al correo del cliente y al portal DIAN. Compatible con régimen simplificado y común.',
     50)
    ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sales_esc_pending")
    op.execute("DROP TABLE IF EXISTS sales_escalations CASCADE")

    op.execute("DROP INDEX IF EXISTS ix_sales_conv_escalated")
    op.execute("DROP INDEX IF EXISTS ix_sales_conv_prospect")
    op.execute("DROP INDEX IF EXISTS ix_sales_conv_phone")
    op.execute("DROP TABLE IF EXISTS sales_conversations CASCADE")

    op.execute("DROP INDEX IF EXISTS ix_skb_category_active")
    op.execute("DROP TABLE IF EXISTS sales_knowledge_base CASCADE")

    op.execute("DROP INDEX IF EXISTS ux_sales_inbox_dedup")
    op.execute("DROP INDEX IF EXISTS ix_sales_inbox_pending")
    op.execute("DROP TABLE IF EXISTS sales_inbox CASCADE")

    op.execute("""
    ALTER TABLE prospects
        DROP COLUMN IF EXISTS email,
        DROP COLUMN IF EXISTS website,
        DROP COLUMN IF EXISTS employee_count,
        DROP COLUMN IF EXISTS monthly_orders_est,
        DROP COLUMN IF EXISTS current_solution,
        DROP COLUMN IF EXISTS lead_score
    """)
