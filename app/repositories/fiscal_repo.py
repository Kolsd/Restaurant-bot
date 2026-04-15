"""
app/repositories/fiscal_repo.py

Repository for fiscal/DIAN functions.
Extracted from database.py — Fase 6 Repository Pattern.
Migrated to tenant_connection() — RLS Wave 2.
"""
import json
from datetime import datetime
from app.services.logging import get_logger
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize
    return _serialize(d)


async def db_init_fiscal_tables():
    """No-op: fiscal_resolution and fiscal_invoices managed by Alembic (0001_initial_schema.py)."""
    pass


async def db_get_fiscal_resolution(restaurant_id: int) -> dict | None:
    """Devuelve la resolución DIAN activa del restaurante, o None si no existe.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM fiscal_resolution WHERE restaurant_id=$1",
            restaurant_id
        )
    return _serialize(dict(row)) if row else None


async def db_upsert_fiscal_resolution(restaurant_id: int, data: dict) -> None:
    """Inserta o actualiza la resolución DIAN de un restaurante.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute(
            """INSERT INTO fiscal_resolution
               (restaurant_id, resolution_number, resolution_date, prefix,
                from_number, to_number, valid_from, valid_to,
                technical_key, current_number, environment, software_id, software_pin)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (restaurant_id) DO UPDATE SET
                 resolution_number = EXCLUDED.resolution_number,
                 resolution_date   = EXCLUDED.resolution_date,
                 prefix            = EXCLUDED.prefix,
                 from_number       = EXCLUDED.from_number,
                 to_number         = EXCLUDED.to_number,
                 valid_from        = EXCLUDED.valid_from,
                 valid_to          = EXCLUDED.valid_to,
                 technical_key     = EXCLUDED.technical_key,
                 environment       = EXCLUDED.environment,
                 software_id       = EXCLUDED.software_id,
                 software_pin      = EXCLUDED.software_pin,
                 updated_at        = NOW()""",
            restaurant_id,
            data["resolution_number"], data["resolution_date"], data.get("prefix", ""),
            data["from_number"], data["to_number"],
            data["valid_from"], data["valid_to"],
            data["technical_key"], data.get("current_number", 0),
            data.get("environment", "test"),
            data.get("software_id", ""), data.get("software_pin", ""),
        )


async def db_claim_next_invoice_number(restaurant_id: int) -> int:
    """
    Incrementa atómicamente el consecutivo de factura y lo devuelve.
    Lanza RuntimeError si la resolución no existe o el rango está agotado.
    La operación es atómica (UPDATE ... RETURNING) — segura con múltiples workers.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE fiscal_resolution
               SET current_number = current_number + 1,
                   updated_at     = NOW()
               WHERE restaurant_id = $1
                 AND current_number + 1 <= to_number
               RETURNING current_number, from_number, to_number,
                         valid_from, valid_to, resolution_number, prefix""",
            restaurant_id
        )
    if not row:
        # Puede ser: no existe resolución, rango agotado, o resolución vencida
        res = await db_get_fiscal_resolution(restaurant_id)
        if not res:
            raise RuntimeError("No hay resolución DIAN configurada para este restaurante")
        if res["current_number"] >= res["to_number"]:
            raise RuntimeError(
                f"Rango de facturación agotado ({res['from_number']}-{res['to_number']}). "
                "Solicita una nueva resolución ante la DIAN."
            )
        raise RuntimeError("Error desconocido al reclamar número de factura")
    return row["current_number"]


async def db_save_fiscal_invoice(data: dict) -> int:
    """Persiste la factura electrónica. Devuelve el ID generado.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """

    # asyncpg espera date/time nativos, no strings
    raw_date = data.get("issue_date")
    issue_date = (
        datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        if isinstance(raw_date, str) else raw_date
    )

    raw_time = data.get("issue_time")
    issue_time = (
        datetime.strptime(raw_time[:8], "%H:%M:%S").time()
        if isinstance(raw_time, str) else raw_time
    )

    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO fiscal_invoices
               (billing_log_id, restaurant_id, order_id,
                resolution_number, prefix, invoice_number,
                issue_date, issue_time,
                subtotal_cents, tax_regime, tax_pct, tax_cents, total_cents,
                cufe, qr_data, uuid_dian, xml_content, pdf_url,
                customer_nit, customer_name, customer_email, customer_id_type,
                payment_method, dian_status, dian_response)
               VALUES
               ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                $19,$20,$21,$22,$23,$24,$25)
               RETURNING id""",
            data.get("billing_log_id"),
            data["restaurant_id"], data["order_id"],
            data["resolution_number"], data.get("prefix", ""), data["invoice_number"],
            issue_date, issue_time,
            data["subtotal_cents"], data.get("tax_regime", "iva"),
            data["tax_pct"], data["tax_cents"], data["total_cents"],
            data.get("cufe", ""), data.get("qr_data", ""), data.get("uuid_dian", ""),
            data.get("xml_content"), data.get("pdf_url"),
            data.get("customer_nit", "222222222"),
            data.get("customer_name", "Consumidor Final"),
            data.get("customer_email", ""),
            data.get("customer_id_type", "13"),
            data.get("payment_method", "cash"),
            data.get("dian_status", "draft"),
            json.dumps(data["dian_response"]) if data.get("dian_response") else None,
        )
    return row["id"]


async def db_get_fiscal_invoices(restaurant_id: int, limit: int = 50) -> list:
    """Lista las facturas electrónicas emitidas por el restaurante.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT id, order_id, prefix, invoice_number, issue_date,
                      subtotal_cents, tax_regime, tax_pct, tax_cents, total_cents,
                      cufe, qr_data, customer_nit, customer_name,
                      payment_method, dian_status, created_at
               FROM fiscal_invoices
               WHERE restaurant_id=$1
               ORDER BY created_at DESC LIMIT $2""",
            restaurant_id, limit
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_next_invoice_number(
    restaurant_id: int,
    prefix: str,
    start_at: int = 5200,
) -> int:
    """
    Retorna MAX(invoice_number)+1 para el restaurante y prefijo indicados.
    Devuelve start_at si no existen facturas previas con ese prefijo.
    NOTA: SELECT no-atómico — apropiado para sandbox. En producción multi-worker
    usar db_claim_next_invoice_number (UPDATE … RETURNING atómico).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        val = await conn.fetchval(
            """SELECT COALESCE(MAX(invoice_number), $3 - 1) + 1
               FROM fiscal_invoices
               WHERE restaurant_id = $1 AND prefix = $2""",
            restaurant_id, prefix, start_at,
        )
    return int(val)


async def db_update_invoice_dian_data(
    fiscal_invoice_id: int,
    cufe: str,
    pdf_url: str,
    qr_data: str,
    dian_response: dict | None = None,
) -> None:
    """
    Almacena los 3 campos DIAN retornados por MATIAS API tras la emisión exitosa:
    CUFE, URL/base64 del PDF y cadena QR. Actualiza dian_status a 'accepted'.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute(
            """UPDATE fiscal_invoices
               SET cufe          = $2,
                   pdf_url       = $3,
                   qr_data       = $4,
                   dian_status   = 'accepted',
                   dian_response = $5::jsonb
               WHERE id = $1""",
            fiscal_invoice_id,
            cufe,
            pdf_url,
            qr_data,
            json.dumps(dian_response) if dian_response else None,
        )
