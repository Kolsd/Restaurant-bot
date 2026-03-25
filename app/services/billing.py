"""
Mesio — Módulo de Facturación / Billing
Integración con: Siigo, Alegra, Loggro, Mesio Native (DIAN)
"""

import os
import json
import httpx
import base64
import hashlib
from abc import ABC, abstractmethod
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
# PATRÓN ADAPTADOR
# ══════════════════════════════════════════════════════════════════════

class BillingAdapter(ABC):
    """Contrato único para todos los proveedores de facturación."""

    @abstractmethod
    async def create_invoice(self, order: dict, config: dict) -> dict:
        """Emite la factura y devuelve el resultado del proveedor."""

    @abstractmethod
    async def test_connection(self, config: dict) -> dict:
        """Valida las credenciales/configuración sin emitir factura real."""


class SiigoAdapter(BillingAdapter):
    async def create_invoice(self, order: dict, config: dict) -> dict:
        client = SiigoClient(config["siigo_username"], config["siigo_access_key"])
        return await client.create_invoice(order, config)

    async def test_connection(self, config: dict) -> dict:
        client = SiigoClient(config["siigo_username"], config["siigo_access_key"])
        result = await client.get_document_types()
        return {"sample": result[:2]}


class AlegraAdapter(BillingAdapter):
    async def create_invoice(self, order: dict, config: dict) -> dict:
        client = AlegraClient(config["alegra_email"], config["alegra_token"])
        return await client.create_invoice(order, config)

    async def test_connection(self, config: dict) -> dict:
        client = AlegraClient(config["alegra_email"], config["alegra_token"])
        result = await client.get_payment_methods()
        return {"sample": result[:2]}


class LoggroAdapter(BillingAdapter):
    async def create_invoice(self, order: dict, config: dict) -> dict:
        client = LoggroClient(config["loggro_api_key"], config["loggro_company_id"])
        return await client.create_invoice(order, config)

    async def test_connection(self, config: dict) -> dict:
        client = LoggroClient(config["loggro_api_key"], config["loggro_company_id"])
        result = await client.get_products()
        return {"sample": result[:2]}


class MesioNativeAdapter(BillingAdapter):
    """
    Proveedor nativo de Facturación Electrónica DIAN (Colombia).
    Arquitectura: calcula CUFE/CUDS, arma un payload JSON limpio y hace
    HTTP POST al API REST de un Proveedor Tecnológico certificado (marca blanca).
    La firma XMLDSig y la transmisión SOAP a la DIAN las gestiona el proveedor.
    Si provider_api_url no está configurado, opera en modo mock (desarrollo/pruebas).
    """

    # ── CUFE / CUDS ───────────────────────────────────────────────────

    @staticmethod
    def _calcular_cufe(
        num_fac: str, fec_fac: str, hor_fac: str,
        val_fac: str, val_imp1: str, val_imp2: str, val_tot: str,
        nit_ofe: str, num_adq: str, cl_tec: str,
        cod_imp1: str = "01", cod_imp2: str = "04", cod_imp3: str = "03",
        val_imp3: str = "0.00",
    ) -> str:
        """
        Calcula el CUFE según especificación DIAN Anexo Técnico FE v1.9.
        Parámetros val_* deben tener exactamente 2 decimales, ej: "45000.00".
        cod_imp1="01"=IVA, cod_imp2="04"=INC, cod_imp3="03"=ICA (generalmente 0).
        """
        cadena = (
            f"{num_fac}{fec_fac}{hor_fac}{val_fac}"
            f"{cod_imp1}{val_imp1}"
            f"{cod_imp2}{val_imp2}"
            f"{cod_imp3}{val_imp3}"
            f"{val_tot}{nit_ofe}{num_adq}{cl_tec}"
        )
        return hashlib.sha384(cadena.encode("utf-8")).hexdigest()

    @staticmethod
    def _calcular_cuds(software_id: str, pin: str, nit_emisor: str) -> str:
        """Código único del software (CUDS) = SHA-384(software_id + pin + nit)."""
        cadena = f"{software_id}{pin}{nit_emisor}"
        return hashlib.sha384(cadena.encode("utf-8")).hexdigest()

    # ── Payload JSON para el Proveedor Tecnológico ────────────────────

    def _build_provider_payload(
        self,
        invoice_number: str,
        prefix: str,
        inv_number: int,
        issue_date: str,
        issue_time: str,
        cufe: str,
        cuds: str,
        qr_url: str,
        resolution: dict,
        config: dict,
        order: dict,
        subtotal_cents: int,
        tax_cents: int,
        total_cents: int,
        tax_regime: str,
        tax_pct: float,
        customer: dict,
        env: str,
    ) -> dict:
        """
        Arma el payload JSON limpio para el Proveedor Tecnológico certificado.
        El proveedor se encarga de la firma XMLDSig y la transmisión a la DIAN.
        """
        items = order.get("items", [])
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []

        line_items = []
        for item in items:
            price    = float(item.get("price", 0))
            qty      = int(item.get("quantity", 1))
            line_sub = price * qty
            line_tax = round(line_sub * tax_pct / 100, 2)
            line_items.append({
                "name":       item.get("name", ""),
                "quantity":   qty,
                "unit_price": price,
                "subtotal":   round(line_sub, 2),
                "tax":        line_tax,
                "total":      round(line_sub + line_tax, 2),
            })

        return {
            "invoice_number": invoice_number,
            "prefix":         prefix,
            "consecutive":    inv_number,
            "issue_date":     issue_date,
            "issue_time":     issue_time + "-05:00",
            "cufe":           cufe,
            "cuds":           cuds,
            "qr_url":         qr_url,
            "environment":    env,
            "currency":       config.get("currency", "COP"),
            "resolution": {
                "number":        resolution["resolution_number"],
                "from_number":   resolution["from_number"],
                "to_number":     resolution["to_number"],
                "valid_from":    str(resolution["valid_from"]),
                "valid_to":      str(resolution["valid_to"]),
                "technical_key": resolution.get("technical_key", ""),
            },
            "emitter": {
                "nit":        config.get("restaurant_nit", ""),
                "nit_type":   config.get("nit_id_type", "31"),
                "legal_name": config.get("restaurant_legal_name", ""),
                "city_code":  config.get("restaurant_city_code", "11001"),
                "city_name":  config.get("restaurant_city_name", ""),
                "address":    config.get("restaurant_address_dian", ""),
                "tax_regime": tax_regime,
                "software_id": resolution.get("software_id") or config.get("software_id", ""),
            },
            "customer": customer,
            "items": line_items,
            "totals": {
                "subtotal":   round(subtotal_cents / 100, 2),
                "tax_regime": tax_regime,
                "tax_pct":    tax_pct,
                "tax":        round(tax_cents / 100, 2),
                "total":      round(total_cents / 100, 2),
            },
            "payment_method": order.get("payment_method", "cash"),
            "order_ref":      order.get("id", ""),
        }

    async def _call_provider_api(self, payload: dict, config: dict) -> dict:
        """
        HTTP POST al API REST del Proveedor Tecnológico certificado (marca blanca).
        Si provider_api_url no está configurado retorna un mock de éxito para
        permitir desarrollo y pruebas sin conexión al proveedor real.
        """
        api_url = config.get("provider_api_url", "")
        api_key = config.get("provider_api_key", "")

        if not api_url:
            # MOCK: simula respuesta exitosa del proveedor certificado
            mock_cufe = hashlib.sha384(
                f"MOCK-{payload['invoice_number']}-{payload['issue_date']}".encode("utf-8")
            ).hexdigest()
            return {
                "success":    True,
                "cufe":       mock_cufe,
                "uuid_dian":  f"MOCK-{payload['invoice_number']}",
                "dian_status": "accepted",
                "mock":        True,
            }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                api_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
            )
            resp.raise_for_status()
            return resp.json()

    # ── create_invoice ────────────────────────────────────────────────

    async def create_invoice(self, order: dict, config: dict) -> dict:
        """
        Genera la factura electrónica DIAN:
        1. Obtiene la resolución del restaurante desde DB.
        2. Reclama el próximo número de factura (atómico en PostgreSQL).
        3. Calcula IVA o INC sobre el total.
        4. Calcula CUFE (SHA-384) y CUDS localmente.
        5. Construye el payload JSON y lo envía al Proveedor Tecnológico.
        6. Persiste resultado en fiscal_invoices.
        """
        restaurant_id = config.get("_restaurant_id")
        if not restaurant_id:
            raise ValueError("config debe incluir '_restaurant_id' para Mesio Native")

        resolution = await db.db_get_fiscal_resolution(restaurant_id)
        if not resolution:
            raise RuntimeError("No hay resolución DIAN configurada. Ve a Configuración → Resolución DIAN.")

        # Validar vigencia de la resolución
        today = date.today()
        valid_to = resolution.get("valid_to", "")
        if valid_to and str(valid_to)[:10] < str(today):
            raise RuntimeError(
                f"La resolución DIAN venció el {valid_to}. Renuévala ante la DIAN."
            )

        # Reclamar número consecutivo (atómico en PostgreSQL)
        inv_number = await db.db_claim_next_invoice_number(restaurant_id)

        # Calcular montos fiscales (en centavos para evitar float drift)
        total_raw = float(order.get("total", 0))
        tax_regime = config.get("tax_regime", "iva")
        if tax_regime == "ico":
            tax_pct = float(config.get("tax_percentage", 8.0))
        else:
            tax_pct = float(config.get("tax_percentage", 19.0))

        # En Colombia el precio en menú generalmente ya incluye el impuesto.
        # Extraemos la base gravable: subtotal = total / (1 + pct/100)
        if tax_pct > 0:
            subtotal_raw = total_raw / (1 + tax_pct / 100)
        else:
            subtotal_raw = total_raw

        subtotal_cents = round(subtotal_raw * 100)
        tax_cents      = round(total_raw * 100) - subtotal_cents
        total_cents    = subtotal_cents + tax_cents

        # Fechas y hora
        now        = datetime.utcnow()
        issue_date = now.strftime("%Y-%m-%d")
        issue_time = now.strftime("%H:%M:%S")

        # Número completo: prefijo + consecutivo
        prefix     = resolution.get("prefix", "")
        full_num   = f"{prefix}{inv_number}"

        # Cliente
        customer_override = order.get("customer", {})
        customer = {
            "nit":     customer_override.get("nit", "222222222"),
            "name":    customer_override.get("name", "Consumidor Final"),
            "email":   customer_override.get("email", ""),
            "id_type": customer_override.get("id_type", "13"),
        }

        # CUDS (identifica el software)
        nit_emisor  = config.get("restaurant_nit", "")
        software_id = resolution.get("software_id") or config.get("software_id", "")
        software_pin = resolution.get("software_pin") or config.get("software_pin", "")
        cuds = self._calcular_cuds(software_id, software_pin, nit_emisor)

        # CUFE (identifica la factura individual)
        tech_key    = resolution.get("technical_key", "")
        val_fac_str = f"{subtotal_cents / 100:.2f}"
        tax_str     = f"{tax_cents / 100:.2f}"
        tot_str     = f"{total_cents / 100:.2f}"

        if tax_regime == "ico":
            cufe = self._calcular_cufe(
                num_fac=full_num, fec_fac=issue_date, hor_fac=issue_time + "-05:00",
                val_fac=val_fac_str,
                cod_imp1="01", val_imp1="0.00",
                cod_imp2="04", val_imp2=tax_str,
                cod_imp3="03", val_imp3="0.00",
                val_tot=tot_str,
                nit_ofe=nit_emisor,
                num_adq=customer["nit"],
                cl_tec=tech_key,
            )
        else:
            cufe = self._calcular_cufe(
                num_fac=full_num, fec_fac=issue_date, hor_fac=issue_time + "-05:00",
                val_fac=val_fac_str,
                cod_imp1="01", val_imp1=tax_str,
                cod_imp2="04", val_imp2="0.00",
                cod_imp3="03", val_imp3="0.00",
                val_tot=tot_str,
                nit_ofe=nit_emisor,
                num_adq=customer["nit"],
                cl_tec=tech_key,
            )

        # URL de validación DIAN (QR)
        env = resolution.get("environment", config.get("dian_environment", "test"))
        if env == "production":
            qr_base = "https://catalogo-vpfe.dian.gov.co/document/searchqr"
        else:
            qr_base = "https://catalogo-vpfe-hab.dian.gov.co/document/searchqr"
        qr_url = f"{qr_base}?documentkey={cufe}"

        # Construir payload JSON para el Proveedor Tecnológico
        provider_payload = self._build_provider_payload(
            invoice_number=full_num,
            prefix=prefix,
            inv_number=inv_number,
            issue_date=issue_date,
            issue_time=issue_time,
            cufe=cufe,
            cuds=cuds,
            qr_url=qr_url,
            resolution=resolution,
            config=config,
            order=order,
            subtotal_cents=subtotal_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
            tax_regime=tax_regime,
            tax_pct=tax_pct,
            customer=customer,
            env=env,
        )

        # Enviar al Proveedor Tecnológico certificado (o mock si no está configurado)
        provider_response = await self._call_provider_api(provider_payload, config)
        cufe_final  = provider_response.get("cufe", cufe)
        dian_status = provider_response.get("dian_status", "pending")
        uuid_dian   = provider_response.get("uuid_dian", "")

        # Persistir en fiscal_invoices
        fiscal_id = await db.db_save_fiscal_invoice({
            "billing_log_id":    None,
            "restaurant_id":     restaurant_id,
            "order_id":          order.get("id", ""),
            "resolution_number": resolution["resolution_number"],
            "prefix":            prefix,
            "invoice_number":    inv_number,
            "issue_date":        issue_date,
            "issue_time":        issue_time,
            "subtotal_cents":    subtotal_cents,
            "tax_regime":        tax_regime,
            "tax_pct":           tax_pct,
            "tax_cents":         tax_cents,
            "total_cents":       total_cents,
            "cufe":              cufe_final,
            "qr_data":           qr_url,
            "uuid_dian":         uuid_dian,
            "xml_content":       json.dumps(provider_payload),
            "pdf_url":           None,
            "customer_nit":      customer["nit"],
            "customer_name":     customer["name"],
            "customer_email":    customer["email"],
            "customer_id_type":  customer["id_type"],
            "payment_method":    order.get("payment_method", "cash"),
            "dian_status":       dian_status,
            "dian_response":     json.dumps(provider_response),
        })

        return {
            "id":             fiscal_id,
            "invoice_number": full_num,
            "cufe":           cufe_final,
            "qr_data":        qr_url,
            "subtotal":       subtotal_cents / 100,
            "tax_regime":     tax_regime,
            "tax_pct":        tax_pct,
            "tax":            tax_cents / 100,
            "total":          total_cents / 100,
            "dian_status":    dian_status,
            "provider_mock":  provider_response.get("mock", False),
        }

    # ── test_connection ───────────────────────────────────────────────

    async def test_connection(self, config: dict) -> dict:
        """
        Valida la configuración DIAN:
        - Campos obligatorios presentes en config
        - Resolución en DB existe y está vigente
        - Rango de numeración no está agotado
        """
        required = ["restaurant_nit", "restaurant_legal_name", "software_id", "software_pin"]
        missing  = [k for k in required if not config.get(k)]
        if missing:
            raise ValueError(f"Faltan campos DIAN obligatorios: {', '.join(missing)}")

        restaurant_id = config.get("_restaurant_id")
        status_info: dict = {
            "config_ok":   True,
            "environment": config.get("dian_environment", "test"),
        }

        if restaurant_id:
            resolution = await db.db_get_fiscal_resolution(restaurant_id)
            if resolution:
                today    = str(date.today())
                expired  = str(resolution.get("valid_to", ""))[:10] < today
                cur      = int(resolution.get("current_number", 0))
                to_num   = int(resolution.get("to_number", 0))
                remaining = to_num - cur
                status_info["resolution"] = {
                    "number":         resolution["resolution_number"],
                    "prefix":         resolution.get("prefix", ""),
                    "valid_to":       str(resolution.get("valid_to", "")),
                    "expired":        expired,
                    "invoices_used":  cur,
                    "invoices_left":  remaining,
                }
                if expired:
                    raise RuntimeError(
                        f"Resolución DIAN vencida el {resolution['valid_to']}. Renuévala ante la DIAN."
                    )
                if remaining <= 0:
                    raise RuntimeError("Rango de facturación agotado. Solicita nueva resolución.")
            else:
                status_info["resolution"] = "No configurada — ve a Configuración → Resolución DIAN"

        return {"sample": [status_info]}


_ADAPTERS: dict[str, BillingAdapter] = {
    "siigo":         SiigoAdapter(),
    "alegra":        AlegraAdapter(),
    "loggro":        LoggroAdapter(),
    "mesio_native":  MesioNativeAdapter(),
}


def get_adapter(provider: str) -> BillingAdapter:
    """Devuelve el adaptador correcto. Lanza ValueError si el proveedor no existe."""
    adapter = _ADAPTERS.get(provider.lower())
    if not adapter:
        raise ValueError(f"Proveedor '{provider}' no soportado. Opciones: {list(_ADAPTERS)}")
    return adapter


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
    try:
        adapter = get_adapter(provider)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    # 1. Intentar cargar como pedido normal de delivery
    order = await db.db_get_order(order_id)

    # 2. Si no existe en 'orders', es un pedido de MESA. Consolidar subórdenes.
    if not order:
        full_bill = await db.db_get_table_bill(order_id)
        if not full_bill or not full_bill.get("sub_orders"):
            return {"success": False, "error": f"Orden {order_id} no encontrada"}

        aggregated_items: dict[str, dict] = {}
        for sub in full_bill.get("sub_orders", []):
            items_list = sub.get("items", [])
            if isinstance(items_list, str):
                try:
                    items_list = json.loads(items_list)
                except Exception:
                    items_list = []

            for item in items_list:
                name  = item.get("name", "")
                qty   = int(item.get("quantity", 1))
                price = float(item.get("price", 0))
                if name in aggregated_items:
                    aggregated_items[name]["quantity"] += qty
                else:
                    aggregated_items[name] = {"name": name, "quantity": qty, "price": price}

        order = {
            "id":         order_id,
            "order_type": "mesa",
            "total":      full_bill.get("total", 0),
            "items":      list(aggregated_items.values()),
        }

    if customer_override:
        order["customer"] = customer_override

    try:
        result_data = await adapter.create_invoice(order, config)

        ext_id = (
            str(result_data.get("id", ""))
            or str(result_data.get("invoiceId", ""))
            or str(result_data.get("number", ""))
            or str(result_data.get("cufe", ""))[:16]
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