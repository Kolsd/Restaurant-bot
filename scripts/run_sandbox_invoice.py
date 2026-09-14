#!/usr/bin/env python3
"""
run_sandbox_invoice.py
======================
Fire test of the MATIAS API adapter against the DIAN Sandbox.

Quick usage:
    python run_sandbox_invoice.py

Required environment variables (in .env or the shell):
    DATABASE_URL            PostgreSQL connection string
    MATIAS_API_URL          https://api-v2.matias-api.com/api/ubl2.1
    MATIAS_API_TOKEN        static token from the MATIAS panel (recommended)
    DIAN_RESOLUTION         resolution number  (e.g. 18764074347312)
    DIAN_PREFIX             invoice prefix     (e.g. LZT)

Alternative to the static token (dynamic login):
    MATIAS_API_USER         MATIAS sandbox account email
    MATIAS_API_PASS         MATIAS sandbox account password
    MATIAS_AUTH_URL         https://api-v2.matias-api.com/api/login

Optional variables:
    SANDBOX_RESTAURANT_ID   Restaurant ID in the DB       (default: 1)
    SANDBOX_TAX_REGIME      iva | ico                     (default: iva)
    SANDBOX_TAX_PCT         tax percentage                (default: 19.0)
    DIAN_TECHNICAL_KEY      resolution's technical key
    DIAN_SOFTWARE_ID        certified Technology Provider's software_id
    DIAN_SOFTWARE_PIN       software PIN
"""

import asyncio
import os
import sys
import time
import json


# ── Load .env if it exists ─────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("  .env loaded")
except ImportError:
    pass  # python-dotenv not installed; environment vars are used instead

# ── Add the project root to PYTHONPATH ────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services import database as db
from app.services.billing import MesioNativeAdapter, _get_matias_token
from datetime import date

# ══════════════════════════════════════════════════════════════════════════════
# Test configuration
# ══════════════════════════════════════════════════════════════════════════════

RESTAURANT_ID  = int(os.getenv("SANDBOX_RESTAURANT_ID", "1"))
TAX_REGIME     = os.getenv("SANDBOX_TAX_REGIME", "iva")          # "iva" | "ico"
TAX_PCT        = float(os.getenv("SANDBOX_TAX_PCT", "19.0"))
INVOICE_NUMBER = int(os.getenv("SANDBOX_INVOICE_NUMBER", "5210"))

# order_id unique per run to avoid a duplicate key in fiscal_invoices
_TS = int(time.time())

# Fake order — two products, total calculated manually
# Prices include IVA/INC (Colombia: menu price already includes tax)
FAKE_ORDER = {
    "id":             f"SANDBOX-TEST-{INVOICE_NUMBER}-{_TS}",
    "order_type":     "mesa",
    "total":          190.0,     # MATIAS sandbox limit: max 224 pesos
    "payment_method": "cash",
    "notes":          "DIAN Sandbox test invoice — Mesio",
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
    Returns the restaurant's DIAN resolution from the DB.
    If it doesn't exist, creates it from the DIAN_* environment variables
    (useful in sandbox where the DB may be empty).
    """
    resolution = await db.db_get_fiscal_resolution(RESTAURANT_ID)
    if resolution:
        print(
            f"  Resolution in DB  : {resolution['resolution_number']}"
            f"  |  prefix '{resolution.get('prefix', '')}'"
            f"  |  env '{resolution.get('environment', '')}'"
        )
        return resolution

    print("  No resolution in DB — creating from DIAN_* variables …")
    res_number = os.getenv("DIAN_RESOLUTION", "")
    if not res_number:
        sys.exit(
            "\n  ERROR: DIAN_RESOLUTION is not set and the DB has no resolution.\n"
            "  Set DIAN_RESOLUTION=<number> in your .env and run again.\n"
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
        f"  Resolution created : {resolution['resolution_number']}"
        f"  |  prefix '{resolution.get('prefix', '')}'"
    )
    return resolution


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    _hr("=")
    print("  MATIAS API -- DIAN Sandbox Fire Test")
    _hr("=")

    # ── 1. Database ──────────────────────────────────────────────────────
    print("\n[1/4] Connecting to the database…")
    await db.init_pool()
    print("  Pool OK")

    # ── 2. DIAN Resolution ───────────────────────────────────────────────────
    print(f"\n[2/4] DIAN Resolution  (restaurant_id={RESTAURANT_ID})…")
    resolution = await _ensure_resolution()

    # ── 3. MATIAS Token (dynamic login) ─────────────────────────────────────
    matias_url = os.getenv("MATIAS_API_URL", "").strip()
    auth_url   = os.getenv("MATIAS_AUTH_URL", "https://api-v2.matias-api.com/api/ubl2.1/login")
    print("\n[3/4] MATIAS API Authentication (dynamic login)…")
    print(f"  Auth URL  : {auth_url}")
    print(f"  Email     : {os.getenv('MATIAS_API_USER', '(not set)')}")
    print(f"  API URL   : {matias_url or '(not set — MOCK mode)'}")

    if not matias_url:
        print("  -> MOCK mode active. Set MATIAS_API_URL for the real sandbox.")
    else:
        t0 = time.perf_counter()
        token = await _get_matias_token()
        ms = (time.perf_counter() - t0) * 1000
        print(f"  Token     : {token[:20]}…  ({ms:.0f} ms)")

    # ── 4. Issuance ────────────────────────────────────────────────────────────
    # Force the exact invoice number (MATIAS support: range 5200-5210)
    forced_number = INVOICE_NUMBER

    _original_get_next = db.db_get_next_invoice_number
    async def _fixed_invoice_number(*args, **kwargs):
        return forced_number
    db.db_get_next_invoice_number = _fixed_invoice_number

    print("\n[4/4] Issuing test invoice…")
    print(f"  Invoice No.  : {forced_number}  (SANDBOX_INVOICE_NUMBER)")
    print(f"  Order        : {FAKE_ORDER['id']}")
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
        db.db_get_next_invoice_number = _original_get_next  # always restore
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        print(f"\n  ERROR after {elapsed_ms:.0f} ms")
        print(f"  {type(exc).__name__}: {exc}")

        # Print the HTTP body if available (helps debug 400 UBL errors)
        response = getattr(exc, "response", None)
        if response is not None:
            print(f"\n  HTTP {response.status_code}  —  server response:")
            try:
                body = response.json()
                print(json.dumps(body, indent=2, ensure_ascii=False))
            except Exception:
                print(response.text[:1200])
        raise SystemExit(1)
    finally:
        db.db_get_next_invoice_number = _original_get_next  # always restore

    elapsed_ms = (time.perf_counter() - t_start) * 1000

    # ── Result ──────────────────────────────────────────────────────────
    _hr("─")
    print("  RESULT")
    _hr("─")

    cufe    = result.get("cufe", "")
    qr_data = result.get("qr_data", "")
    pdf_url = result.get("pdf_url", "")

    print(f"  Invoice No.       : {result['invoice_number']}")
    print(f"  fiscal_invoice_id : {result['id']}")
    print(f"  DIAN status       : {result['dian_status']}")
    print(f"  Mock mode         : {result['provider_mock']}")
    print()

    # CUFE — long line, show in full
    print(f"  CUFE  ({len(cufe)} chars):")
    print(f"    {cufe}")

    # QR — may be a URL or a long string
    print(f"\n  QR data  ({len(qr_data)} chars):")
    print(f"    {qr_data[:120]}{'…' if len(qr_data) > 120 else ''}")

    # PDF — may be a short URL or a very long base64 string
    print(f"\n  PDF  ({len(pdf_url)} chars):")
    if len(pdf_url) > 120:
        print(f"    {pdf_url[:80]}…  [base64 truncated]")
    else:
        print(f"    {pdf_url or '(empty)'}")

    print()
    print(f"  Subtotal          : ${result['subtotal']:,.2f}")
    print(f"  Tax               : ${result['tax']:,.2f}  ({result['tax_pct']}%)")
    print(f"  Total             : ${result['total']:,.2f}")
    print()
    print(f"  Total time        : {elapsed_ms:.0f} ms")
    _hr("─")
    print()


if __name__ == "__main__":
    asyncio.run(main())
