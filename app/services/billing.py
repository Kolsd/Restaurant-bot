"""
Mesio — Módulo de Facturación / Billing
Integración con: Siigo, Alegra, Loggro
"""

import os
import json
import httpx
import base64
from datetime import datetime, date
from typing import Optional
from app.services import database as db

# ── HELPERS ──────────────────────────────────────────────────────────

def _today_iso() -> str:
    return date.today().isoformat()

def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════════
# SIIGO
# ══════════════════════════════════════════════════════════════════════

class SiigoClient:
    """
    Cliente Siigo Cloud API v1
    Docs: https://siigonube.siigo.com/docs/
    """
    BASE_URL = "https://siigo.com/api"

    def __init__(self, username: str, access_key: str):
        self.username   = username
        self.access_key = access_key
        self._token: Optional[str] = None

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.BASE_URL}/auth",
                json={"Username": self.username, "AccessKey": self.access_key},
                headers={"Content-Type": "application/json", "Partner-Id": "Mesio"}
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            return self._token

    async def _headers(self) -> dict:
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Partner-Id": "Mesio"
        }

    async def get_document_types(self) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.BASE_URL}/document-types?type=FV",
                headers=await self._headers()
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

    async def get_taxes(self) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.BASE_URL}/taxes",
                headers=await self._headers()
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

    async def create_invoice(self, order: dict, config: dict) -> dict:
        """
        Crea factura de venta en Siigo.
        config espera: document_id, seller_id, cost_center_id,
                       product_code, iva_percentage (opcional)
        """
        items = order.get("items", [])
        if isinstance(items, str):
            items = json.loads(items)

        iva_pct = float(config.get("iva_percentage", 0))

        siigo_items = []
        for it in items:
            price    = float(it.get("price", 0))
            qty      = int(it.get("quantity", 1))
            tax_info = [{"id": config["tax_id"]}] if iva_pct > 0 and config.get("tax_id") else []
            siigo_items.append({
                "code":        config.get("product_code", "CONSUMO"),
                "description": it.get("name", "Restaurant item"),
                "quantity":    qty,
                "price":       price,
                "discount":    0,
                "taxes":       tax_info
            })

        customer = order.get("customer", {})
        payload  = {
            "document": {"id": int(config["document_id"])},
            "date":     _today_iso(),
            "customer": {
                "identification": customer.get("nit", config.get("default_customer_nit", "222222222")),
                "branch_office":  0
            },
            "seller": int(config.get("seller_id", 0)) if config.get("seller_id") else None,
            "observations": f"Pedido Mesio #{order.get('id','')} · {order.get('order_type','')}",
            "items": siigo_items,
            "payments": [{
                "id":     int(config.get("payment_id", 5765)),
                "value":  float(order.get("total", 0)),
                "due_date": _today_iso()
            }]
        }
        if payload["seller"] is None:
            del payload["seller"]

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self.BASE_URL}/v1/invoices",
                json=payload,
                headers=await self._headers()
            )
            resp.raise_for_status()
            return resp.json()


# ══════════════════════════════════════════════════════════════════════
# ALEGRA
# ══════════════════════════════════════════════════════════════════════

class AlegraClient:
    """
    Cliente Alegra REST API
    Docs: https://developer.alegra.com/docs
    """
    BASE_URL = "https://app.alegra.com/api/v1"

    def __init__(self, email: str, token: str):
        raw   = f"{email}:{token}".encode()
        self._basic = base64.b64encode(raw).decode()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Basic {self._basic}",
            "Content-Type": "application/json"
        }

    async def get_payment_methods(self) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.BASE_URL}/payment-types",
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()

    async def get_taxes(self) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.BASE_URL}/taxes",
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()

    async def get_price_lists(self) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.BASE_URL}/price-lists",
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()

    async def create_invoice(self, order: dict, config: dict) -> dict:
        """
        config espera: payment_type_id, warehouse_id, item_id_default,
                       iva_id (opcional), currency (COP/USD)
        """
        items = order.get("items", [])
        if isinstance(items, str):
            items = json.loads(items)

        alegra_items = []
        for it in items:
            price = float(it.get("price", 0))
            qty   = int(it.get("quantity", 1))
            item_entry = {
                "id":       config.get("item_id_default", 1),
                "name":     it.get("name", "Item"),
                "price":    price,
                "quantity": qty,
                "discount": 0,
            }
            if config.get("iva_id"):
                item_entry["tax"] = [{"id": int(config["iva_id"])}]
            alegra_items.append(item_entry)

        customer  = order.get("customer", {})
        c_id      = customer.get("alegra_id") or config.get("default_customer_id", 1)

        payload = {
            "date":     _today_iso(),
            "dueDate":  _today_iso(),
            "client":   {"id": int(c_id)},
            "items":    alegra_items,
            "currency": {"code": config.get("currency", "COP")},
            "notes":    f"Pedido Mesio #{order.get('id','')} · Mesa/Canal: {order.get('order_type','')}",
            "payments": [{
                "id":     int(config.get("payment_type_id", 1)),
                "amount": float(order.get("total", 0)),
                "date":   _today_iso()
            }]
        }
        if config.get("warehouse_id"):
            payload["warehouse"] = {"id": int(config["warehouse_id"])}

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self.BASE_URL}/invoices",
                json=payload,
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()


# ══════════════════════════════════════════════════════════════════════
# LOGGRO
# ══════════════════════════════════════════════════════════════════════

class LoggroClient:
    """
    Cliente Loggro API
    Docs: https://desarrolladores.loggro.com
    """
    BASE_URL = "https://api.loggro.com/api/v1"

    def __init__(self, api_key: str, company_id: str):
        self.api_key    = api_key
        self.company_id = company_id

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
            "X-Company-Id":  self.company_id
        }

    async def get_products(self) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.BASE_URL}/products",
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

    async def get_customers(self) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.BASE_URL}/customers",
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

    async def create_invoice(self, order: dict, config: dict) -> dict:
        """
        config espera: resolution_id, payment_method_code,
                       product_code_default, iva_percentage (opcional),
                       customer_nit_default
        """
        items = order.get("items", [])
        if isinstance(items, str):
            items = json.loads(items)

        iva_pct = float(config.get("iva_percentage", 0))

        loggro_items = []
        for it in items:
            price = float(it.get("price", 0))
            qty   = int(it.get("quantity", 1))
            entry = {
                "productCode": config.get("product_code_default", "CONS001"),
                "description": it.get("name", "Restaurant item"),
                "quantity":    qty,
                "unitPrice":   price,
                "discount":    0,
            }
            if iva_pct > 0:
                entry["taxes"] = [{"taxType": "IVA", "percentage": iva_pct}]
            loggro_items.append(entry)

        customer = order.get("customer", {})
        payload  = {
            "invoiceDate":    _today_iso(),
            "dueDate":        _today_iso(),
            "resolutionId":   config.get("resolution_id"),
            "currency":       config.get("currency", "COP"),
            "customer": {
                "nit":   customer.get("nit", config.get("customer_nit_default", "222222222")),
                "name":  customer.get("name", "Final Consumer"),
                "email": customer.get("email", "")
            },
            "items": loggro_items,
            "paymentMethod": config.get("payment_method_code", "CASH"),
            "notes": f"Pedido Mesio #{order.get('id','')}",
            "total": float(order.get("total", 0))
        }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self.BASE_URL}/invoices",
                json=payload,
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()


# ══════════════════════════════════════════════════════════════════════
# FÁBRICA / DISPATCHER
# ══════════════════════════════════════════════════════════════════════

async def get_billing_config(restaurant_id: int) -> Optional[dict]:
    """Lee la config de billing del restaurante desde la BD."""
    pool  = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT billing_config FROM restaurants WHERE id=$1", restaurant_id
        )
        if not row or not row["billing_config"]:
            return None
        cfg = row["billing_config"]
        return cfg if isinstance(cfg, dict) else json.loads(cfg)

async def save_billing_config(restaurant_id: int, config: dict) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurants SET billing_config=$1::jsonb WHERE id=$2",
            json.dumps(config), restaurant_id
        )

async def get_billing_log(restaurant_id: int, limit: int = 50) -> list:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM billing_log
               WHERE restaurant_id=$1
               ORDER BY created_at DESC LIMIT $2""",
            restaurant_id, limit
        )
        return [db._serialize(dict(r)) for r in rows]

async def log_billing_event(restaurant_id: int, order_id: str,
                            provider: str, status: str,
                            external_id: str = "", error: str = "") -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO billing_log
               (restaurant_id, order_id, provider, status, external_id, error_message)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            restaurant_id, order_id, provider, status, external_id, error
        )

async def emit_invoice(order_id: str, restaurant_id: int,
                       customer_override: Optional[dict] = None) -> dict:
    """
    Punto de entrada principal.
    Lee la config del restaurante y emite la factura al proveedor configurado.
    Soporta consolidación automática de Subórdenes de Mesa.
    """
    config = await get_billing_config(restaurant_id)
    if not config:
        return {"success": False, "error": "Billing no configurado para este restaurante"}

    provider = config.get("provider", "").lower()
    if provider not in ("siigo", "alegra", "loggro"):
        return {"success": False, "error": f"Proveedor '{provider}' no soportado"}

    # 1. Intentar cargar como pedido normal de delivery
    order = await db.db_get_order(order_id)
    
    # 2. Si no existe en 'orders', es un pedido de MESA. Hay que consolidar la factura completa.
    if not order:
        full_bill = await db.db_get_table_bill(order_id)
        if not full_bill or not full_bill.get("sub_orders"):
            return {"success": False, "error": f"Orden {order_id} no encontrada"}

        # Consolidar (sumar) los items de TODAS las subórdenes de la visita
        aggregated_items = {}
        for sub in full_bill.get("sub_orders", []):
            items_list = sub.get("items", [])
            if isinstance(items_list, str):
                try:
                    items_list = json.loads(items_list)
                except Exception:
                    items_list = []
                    
            for item in items_list:
                name = item.get("name", "")
                qty = int(item.get("quantity", 1))
                price = float(item.get("price", 0)) 
                
                if name in aggregated_items:
                    aggregated_items[name]["quantity"] += qty
                else:
                    aggregated_items[name] = {"name": name, "quantity": qty, "price": price}

        order = {
            "id": order_id,
            "order_type": "mesa",
            "total": full_bill.get("total", 0),
            "items": list(aggregated_items.values())
        }

    if customer_override:
        order["customer"] = customer_override

    try:
        result_data = None
        if provider == "siigo":
            client      = SiigoClient(config["siigo_username"], config["siigo_access_key"])
            result_data = await client.create_invoice(order, config)

        elif provider == "alegra":
            client      = AlegraClient(config["alegra_email"], config["alegra_token"])
            result_data = await client.create_invoice(order, config)

        elif provider == "loggro":
            client      = LoggroClient(config["loggro_api_key"], config["loggro_company_id"])
            result_data = await client.create_invoice(order, config)

        ext_id = (
            str(result_data.get("id", ""))
            or str(result_data.get("invoiceId", ""))
            or str(result_data.get("number", ""))
        )
        await log_billing_event(restaurant_id, order_id, provider, "success", ext_id)
        return {"success": True, "provider": provider, "data": result_data, "external_id": ext_id}

    except httpx.HTTPStatusError as exc:
        err = f"HTTP {exc.response.status_code}: {exc.response.text[:400]}"
        await log_billing_event(restaurant_id, order_id, provider, "error", "", err)
        return {"success": False, "error": err}
    except Exception as exc:
        err = str(exc)[:400]
        await log_billing_event(restaurant_id, order_id, provider, "error", "", err)
        return {"success": False, "error": err}