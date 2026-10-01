"""The live demo at /demo — a real restaurant a prospect orders from.

PM decision 2026-09-25: no animation. The prospect scans a QR with their own
phone, orders by tapping the carta, and watches the ticket reach the kitchen
screen on the /demo page, where they mark it ready and see their phone say
so. It is the product's own table flow end to end.

"Casa Mesio" is an ordinary tenant created on the first visit (idempotent by
slug) on the Esencial plan: the table chat has no AI assistant, so a demo
never costs an LLM call. It has DEMO_TABLES tables; a visitor gets a free
one, or the one whose session has been open longest when all are taken.
"""

import json
import random
from datetime import timezone

import asyncpg

from app.repositories import delivery_repo, restaurant_repo
from app.repositories import tables_repo as tr
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope, tenant_scope

log = get_logger(__name__)

DEMO_SLUG = "casa-mesio-demo"
DEMO_NAME = "Casa Mesio"
DEMO_TABLES = 20

# Statuses the visitor can move their own ticket through from the demo
# kitchen (the real kitchen screen uses the same ones).
KITCHEN_STATUSES = ("en_preparacion", "listo", "entregado")

DEMO_MENU = {
    "Entradas": [
        {"name": "Empanadas de pipián", "description": "Tres empanadas con ají de la casa", "price": 14000},
        {"name": "Patacones con hogao", "description": "Plátano verde crocante y hogao", "price": 16000},
        {"name": "Ceviche de camarón", "description": "Limón, cebolla morada y cilantro", "price": 26000},
    ],
    "Platos fuertes": [
        {"name": "Bandeja paisa", "description": "Frijoles, chicharrón, carne molida, chorizo, arepa y aguacate", "price": 38000},
        {"name": "Ajiaco santafereño", "description": "Pollo, tres papas, mazorca, crema y alcaparras", "price": 32000},
        {"name": "Posta cartagenera", "description": "Con arroz con coco y tajadas", "price": 42000},
        {"name": "Hamburguesa de la casa", "description": "Carne madurada, queso costeño y papas", "price": 34000},
    ],
    "Bebidas": [
        {"name": "Limonada de coco", "description": "", "price": 12000},
        {"name": "Jugo de lulo", "description": "En agua o en leche", "price": 9000},
        {"name": "Cerveza artesanal", "description": "Rubia o roja", "price": 15000},
    ],
    "Postres": [
        {"name": "Tres leches", "description": "", "price": 14000},
        {"name": "Brevas con arequipe", "description": "Con queso campesino", "price": 13000},
    ],
}

# The demo org's ids, once known in this worker. They never change.
_ids: tuple[int, int] | None = None


def _iso_utc(value) -> str | None:
    """table_orders.created_at is a naive UTC timestamp; say so to the browser."""
    if not hasattr(value, "isoformat"):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def table_ids() -> list[str]:
    return [f"{DEMO_SLUG}-{n}" for n in range(1, DEMO_TABLES + 1)]


async def ensure_demo() -> tuple[int, int]:
    """Return (org_id, location_id) of Casa Mesio, creating it the first time.

    Two first visitors racing both try the insert; the slug is unique, so the
    loser re-reads. Tables and carta are upserts, safe to repeat.
    """
    global _ids
    if _ids is not None:
        return _ids

    org = await delivery_repo.db_get_org_by_slug(DEMO_SLUG)
    if org is None:
        try:
            org = await restaurant_repo.db_create_organization(
                name=DEMO_NAME, slug=DEMO_SLUG, features={"currency": "COP"},
                plan_code="esencial",
            )
            log.info("live_demo.org_created", org_id=org["id"])
        except asyncpg.UniqueViolationError:
            org = await delivery_repo.db_get_org_by_slug(DEMO_SLUG)
    org_id = int(org["id"])

    with bypass_tenant_scope("live_demo_setup: the demo tenant provisions itself"):
        locations = await restaurant_repo.db_get_org_locations(org_id)
        if locations:
            location_id = int(locations[0]["id"])
        else:
            location = await restaurant_repo.db_create_location(
                org_id=org_id, name="Principal", code="principal", active=True,
            )
            location_id = int(location["id"])

    with tenant_scope(org_id):
        await restaurant_repo.db_update_menu(org_id, DEMO_MENU)
        for number, table_id in enumerate(table_ids(), start=1):
            await tr.db_create_table(table_id, number, str(number), branch_id=location_id)

    _ids = (org_id, location_id)
    return _ids


async def assign_table() -> dict:
    """A table for a new visitor: a free one at random, else recycle the
    table whose session has been open longest."""
    org_id, _ = await ensure_demo()
    with tenant_scope(org_id):
        open_sessions = await tr.db_get_open_sessions_by_table(org_id)
        busy = {s["table_id"] for s in open_sessions}
        free = [t for t in table_ids() if t not in busy]
        if free:
            table_id = random.choice(free)
        else:
            oldest = open_sessions[0]
            table_id = oldest["table_id"]
            await tr.db_close_session(oldest["phone"], org_id, reason="demo_recycle")
            log.info("live_demo.table_recycled", table_id=table_id)
    number = table_ids().index(table_id) + 1
    return {"table_id": table_id, "table_name": f"Mesa {number}", "chat_url": f"/chat/{table_id}"}


def is_demo_table(table_id: str) -> bool:
    return table_id in table_ids()


async def kitchen_orders(table_id: str) -> list[dict]:
    """The demo kitchen screen for ONE table, and only the orders of the
    session open on it now — a recycled table never shows the previous
    visitor's tickets, and strangers' notes never reach another page."""
    org_id, location_id = await ensure_demo()
    with tenant_scope(org_id):
        session = await tr.db_get_active_session_by_table_id(table_id)
    if not session:
        return []
    with bypass_tenant_scope("live_demo_kitchen: the demo org's own orders"):
        rows = await tr.db_get_table_orders_for_branch(
            branch_id=location_id, status=None, is_admin=False, org_id=org_id,
        )
    orders = []
    for row in rows:
        if str(row.get("table_id")) != table_id or row.get("phone") != session["phone"]:
            continue
        items = row.get("items") or []
        if isinstance(items, str):
            items = json.loads(items)
        orders.append({
            "id": row["id"],
            "status": row.get("status"),
            "created_at": _iso_utc(row.get("created_at")),
            "items": [
                {"name": i.get("name"), "qty": i.get("quantity") or i.get("qty") or 1,
                 "notes": i.get("notes") or ""}
                for i in items if isinstance(i, dict)
            ],
        })
    return orders


async def advance_order(order_id: str, status: str) -> bool:
    """Move a demo order to `status`, as the kitchen would. False when the
    order is not one of the demo's."""
    org_id, _ = await ensure_demo()
    with bypass_tenant_scope("live_demo_kitchen: order lookup by id"):
        record = await tr.db_get_table_order_record(order_id)
    if not record or int(record.get("org_id") or 0) != org_id or not is_demo_table(str(record.get("table_id"))):
        return False
    with tenant_scope(org_id):
        await tr.db_update_table_order_status(order_id, status)
    return True

