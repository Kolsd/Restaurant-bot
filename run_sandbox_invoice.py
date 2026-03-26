#!/usr/bin/env python3
"""
run_sandbox_invoice.py
======================
Prueba de fuego del adaptador MATIAS API contra el Sandbox DIAN.

Uso rápido:
    python run_sandbox_invoice.py

Variables de entorno obligatorias (en .env o en el shell):
    DATABASE_URL            PostgreSQL connection string
    MATIAS_API_URL          https://api-v2.matias-api.com/api/ubl2.1
    MATIAS_API_TOKEN        token estatico del panel de MATIAS (recomendado)
    DIAN_RESOLUTION         numero de resolucion  (ej. 18764074347312)
    DIAN_PREFIX             prefijo de factura    (ej. LZT)

Alternativa al token estatico (login dinamico):
    MATIAS_API_USER         email de la cuenta sandbox MATIAS
    MATIAS_API_PASS         password de la cuenta sandbox MATIAS
    MATIAS_AUTH_URL         https://api-v2.matias-api.com/api/login

Variables opcionales:
    SANDBOX_RESTAURANT_ID   ID del restaurante en la BD   (default: 1)
    SANDBOX_TAX_REGIME      iva | ico                     (default: iva)
    SANDBOX_TAX_PCT         porcentaje de impuesto        (default: 19.0)
    DIAN_TECHNICAL_KEY      clave técnica de la resolución
    DIAN_SOFTWARE_ID        software_id del PT certificado
    DIAN_SOFTWARE_PIN       PIN del software
"""

import asyncio
import os
import sys
import time
import json


# ── Cargar .env si existe ─────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("  .env cargado")
except ImportError:
    pass  # python-dotenv no instalado; se usan las vars del entorno

# ── Añadir raíz del proyecto al PYTHONPATH ────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services import database as db
from app.services.billing import MesioNativeAdapter, _get_matias_token
from datetime import date

# ══════════════════════════════════════════════════════════════════════════════
# Configuración del test
# ══════════════════════════════════════════════════════════════════════════════

RESTAURANT_ID  = int(os.getenv("SANDBOX_RESTAURANT_ID", "1"))
TAX_REGIME     = os.getenv("SANDBOX_TAX_REGIME", "iva")          # "iva" | "ico"
TAX_PCT        = float(os.getenv("SANDBOX_TAX_PCT", "19.0"))
INVOICE_NUMBER = int(os.getenv("SANDBOX_INVOICE_NUMBER", "5210"))

# order_id único por ejecución para evitar llave duplicada en fiscal_invoices
_TS = int(time.time())

# Orden falsa — dos productos, total calculado manualmente
# Precios incluyen IVA/INC (Colombia: precio menú ya lleva impuesto)
FAKE_ORDER = {
    "id":             f"SANDBOX-TEST-{INVOICE_NUMBER}-{_TS}",
    "order_type":     "mesa",
    "total":          190.0,     # Límite sandbox MATIAS: máx 224 pesos
    "payment_method": "cash",
    "notes":          "Factura de prueba Sandbox DIAN — Mesio",
    "items": [
        {
            "id":       "P001",
            "name":     "Hamburguesa Clasica",
            "quantity": 1,
            "price":    100.0,
        },
        {
            "id":       "P002",
            "name":     "Coca-Cola 350 ml",
            "quantity": 1,
            "price":    90.0,
        },
    ],
    "customer": {
        "nit":     "222222222222",
        "name":    "Consumidor Final",
        "email":   "cf@email.com",
        "id_type": "13",
    },
}

CONFIG = {
    "_restaurant_id": RESTAURANT_ID,
    "tax_regime":     TAX_REGIME,
    "tax_percentage": TAX_PCT,
}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _hr(char="-", width=62):
    print(char * width)


async def _ensure_resolution() -> dict:
    """
    Devuelve la resolución DIAN del restaurante desde DB.
    Si no existe, la crea con los valores de las variables DIAN_* de entorno
    (útil en sandbox donde la BD puede estar vacía).
    """
    resolution = await db.db_get_fiscal_resolution(RESTAURANT_ID)
    if resolution:
        print(
            f"  Resolución en BD  : {resolution['resolution_number']}"
            f"  |  prefijo '{resolution.get('prefix', '')}'"
            f"  |  env '{resolution.get('environment', '')}'"
        )
        return resolution

    print("  No hay resolución en BD — creando desde variables DIAN_* …")
    res_number = os.getenv("DIAN_RESOLUTION", "")
    if not res_number:
        sys.exit(
            "\n  ERROR: DIAN_RESOLUTION no está definida y la BD no tiene resolución.\n"
            "  Define DIAN_RESOLUTION=<número> en tu .env y vuelve a ejecutar.\n"
        )

    await db.db_upsert_fiscal_resolution(RESTAURANT_ID, {
        "resolution_number": res_number,
        "resolution_date":   date(2023, 1, 19),
        "prefix":            os.getenv("DIAN_PREFIX", "LZT"),
        "from_number":       1,
        "to_number":         99999,
        "valid_from":        date(2023, 1, 19),
        "valid_to":          date(2030, 1, 19),
        "technical_key":     os.getenv("DIAN_TECHNICAL_KEY", ""),
        "current_number":    0,
        "environment":       "test",
        "software_id":       os.getenv("DIAN_SOFTWARE_ID", ""),
        "software_pin":      os.getenv("DIAN_SOFTWARE_PIN", ""),
    })
    resolution = await db.db_get_fiscal_resolution(RESTAURANT_ID)
    print(
        f"  Resolución creada : {resolution['resolution_number']}"
        f"  |  prefijo '{resolution.get('prefix', '')}'"
    )
    return resolution


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    _hr("=")
    print("  MATIAS API -- Prueba de Fuego Sandbox DIAN")
    _hr("=")

    # ── 1. Base de datos ──────────────────────────────────────────────────────
    print("\n[1/4] Conectando a la base de datos…")
    await db.init_pool()
    print("  Pool OK")

    # ── 2. Resolución DIAN ───────────────────────────────────────────────────
    print(f"\n[2/4] Resolución DIAN  (restaurant_id={RESTAURANT_ID})…")
    resolution = await _ensure_resolution()

    # ── 3. Token MATIAS (login dinamico) ─────────────────────────────────────
    matias_url = os.getenv("MATIAS_API_URL", "").strip()
    auth_url   = os.getenv("MATIAS_AUTH_URL", "https://api-v2.matias-api.com/api/ubl2.1/login")
    print("\n[3/4] Autenticacion MATIAS API (login dinamico)…")
    print(f"  Auth URL  : {auth_url}")
    print(f"  Email     : {os.getenv('MATIAS_API_USER', '(no definido)')}")
    print(f"  API URL   : {matias_url or '(no definida — modo MOCK)'}")

    if not matias_url:
        print("  -> Modo MOCK activado. Para el sandbox real define MATIAS_API_URL.")
    else:
        t0 = time.perf_counter()
        token = await _get_matias_token()
        ms = (time.perf_counter() - t0) * 1000
        print(f"  Token     : {token[:20]}…  ({ms:.0f} ms)")

    # ── 4. Emisión ────────────────────────────────────────────────────────────
    # Forzar número de factura exacto (soporte MATIAS: rango 5200-5210)
    forced_number = INVOICE_NUMBER

    _original_get_next = db.db_get_next_invoice_number
    async def _fixed_invoice_number(*args, **kwargs):
        return forced_number
    db.db_get_next_invoice_number = _fixed_invoice_number

    print("\n[4/4] Emitiendo factura de prueba…")
    print(f"  Nro. factura : {forced_number}  (SANDBOX_INVOICE_NUMBER)")
    print(f"  Orden        : {FAKE_ORDER['id']}")
    for item in FAKE_ORDER["items"]:
        print(f"  Item         : {item['name']}  x{item['quantity']}  ${item['price']:,.0f}")
    print(
        f"  Total        : ${FAKE_ORDER['total']:,.0f}"
        f"  |  {TAX_REGIME.upper()} {TAX_PCT}%"
    )

    adapter = MesioNativeAdapter()
    t_start = time.perf_counter()

    try:
        result = await adapter._create_invoice_matias(
            order=FAKE_ORDER,
            config=CONFIG,
            resolution=resolution,
        )
    except Exception as exc:
        db.db_get_next_invoice_number = _original_get_next  # restaurar siempre
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        print(f"\n  ERROR tras {elapsed_ms:.0f} ms")
        print(f"  {type(exc).__name__}: {exc}")

        # Imprimir body HTTP si está disponible (ayuda a depurar 400 UBL)
        response = getattr(exc, "response", None)
        if response is not None:
            print(f"\n  HTTP {response.status_code}  —  respuesta del servidor:")
            try:
                body = response.json()
                print(json.dumps(body, indent=2, ensure_ascii=False))
            except Exception:
                print(response.text[:1200])
        raise SystemExit(1)
    finally:
        db.db_get_next_invoice_number = _original_get_next  # restaurar siempre

    elapsed_ms = (time.perf_counter() - t_start) * 1000

    # ── Resultado ──────────────────────────────────────────────────────────
    _hr("─")
    print("  RESULTADO")
    _hr("─")

    cufe    = result.get("cufe", "")
    qr_data = result.get("qr_data", "")
    pdf_url = result.get("pdf_url", "")

    print(f"  Nro. factura      : {result['invoice_number']}")
    print(f"  fiscal_invoice_id : {result['id']}")
    print(f"  DIAN status       : {result['dian_status']}")
    print(f"  Modo mock         : {result['provider_mock']}")
    print()

    # CUFE — línea larga, mostrar completo
    print(f"  CUFE  ({len(cufe)} chars):")
    print(f"    {cufe}")

    # QR — puede ser URL o cadena larga
    print(f"\n  QR data  ({len(qr_data)} chars):")
    print(f"    {qr_data[:120]}{'…' if len(qr_data) > 120 else ''}")

    # PDF — puede ser URL corta o base64 muy largo
    print(f"\n  PDF  ({len(pdf_url)} chars):")
    if len(pdf_url) > 120:
        print(f"    {pdf_url[:80]}…  [base64 truncado]")
    else:
        print(f"    {pdf_url or '(vacío)'}")

    print()
    print(f"  Subtotal          : ${result['subtotal']:,.2f}")
    print(f"  Impuesto          : ${result['tax']:,.2f}  ({result['tax_pct']}%)")
    print(f"  Total             : ${result['total']:,.2f}")
    print()
    print(f"  Tiempo total      : {elapsed_ms:.0f} ms")
    _hr("─")
    print()


if __name__ == "__main__":
    asyncio.run(main())
