"""
app/services/loyalty.py
=======================
Capa de negocio para el módulo de Fidelización (Loyalty).

Estas funciones se llaman como asyncio background tasks desde los hooks de pago,
por lo que deben absorber cualquier excepción sin interrumpir el flujo principal.
"""

from app.services import database as db


async def accrue_on_order(
    bot_number: str,
    phone: str,
    order_id: str,
    total_cop: float,
) -> None:
    """
    Acumula puntos cuando se confirma un pago de orden (domicilio / recoger).
    Verifica que el módulo 'loyalty' esté activo antes de operar.
    No propaga excepciones — es seguro llamar como create_task().
    """
    try:
        if not await db.db_check_module(bot_number, "loyalty"):
            return
        restaurant = await db.db_get_restaurant_by_bot_number(bot_number)
        if not restaurant:
            return
        pts = await db.db_accrue_loyalty_points(
            restaurant_id=restaurant["id"],
            phone=phone,
            order_id=order_id,
            total_cop=total_cop,
        )
        if pts:
            print(f"[LOYALTY] +{pts}pts — phone={phone} order={order_id}", flush=True)
    except Exception as exc:
        print(f"[LOYALTY] accrue_on_order error — {exc}", flush=True)


async def accrue_on_check(
    restaurant_id: int,
    bot_number: str,
    base_order_id: str,
    check_id: str,
    total_cop: float,
) -> None:
    """
    Acumula puntos cuando se paga un check de mesa (caja/POS).
    Resuelve el teléfono del cliente desde table_orders.
    No propaga excepciones — es seguro llamar como create_task().
    """
    try:
        if not await db.db_check_module(bot_number, "loyalty"):
            return
        phone = await db.db_get_phone_for_base_order(base_order_id)
        if not phone:
            return
        pts = await db.db_accrue_loyalty_points(
            restaurant_id=restaurant_id,
            phone=phone,
            order_id=check_id,
            total_cop=total_cop,
        )
        if pts:
            print(f"[LOYALTY] +{pts}pts — phone={phone} check={check_id}", flush=True)
    except Exception as exc:
        print(f"[LOYALTY] accrue_on_check error — {exc}", flush=True)
