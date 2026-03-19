import asyncio
import os
import httpx
from app.services import database as db

# Semáforo para limitar concurrencia del scheduler (V-13 parcial)
_scheduler_semaphore = asyncio.Semaphore(10)


async def _send_whatsapp(phone: str, message: str, bot_number: str, db_phone_id: str = None):
    token    = os.getenv("META_ACCESS_TOKEN", "")
    phone_id = db_phone_id or os.getenv("META_PHONE_NUMBER_ID", "")
    if not token or not phone_id:
        print("⚠️ Scheduler: META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados", flush=True)
        return False
    clean_phone = phone.lstrip("+").replace(" ", "")
    url  = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    body = {
        "messaging_product": "whatsapp",
        "to":   clean_phone,
        "type": "text",
        "text": {"body": message},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url, json=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            )
            return resp.status_code == 200
    except Exception as e:
        print(f"⚠️ Scheduler send error: {e}", flush=True)
        return False


async def _create_inactivity_alert(session: dict):
    try:
        await db.db_create_waiter_alert(
            phone=session["phone"],
            bot_number=session["bot_number"],
            alert_type="waiter",
            message=f"Cliente en {session.get('table_name', 'mesa')} sin actividad — posible cierre por inactividad.",
            table_id=session.get("table_id", ""),
            table_name=session.get("table_name", ""),
        )
    except Exception as e:
        print(f"⚠️ Scheduler alert error: {e}", flush=True)


async def _process_stale_session(session: dict):
    """Procesa una sesión individual con semáforo para limitar concurrencia."""
    async with _scheduler_semaphore:
        phone      = session["phone"]
        bot_number = session["bot_number"]
        table_name = session.get("table_name", "tu mesa")
        order_delivered = session.get("order_delivered", False)
        has_order       = session.get("has_order", False)
        db_phone_id     = session.get("meta_phone_id")

        if order_delivered:
            msg = (
                f"¡Hola! 😊 Esperamos que todo haya estado delicioso en {table_name}. "
                f"Cuando gustes, puedes pedir la cuenta o llamar al mesero. ¡Fue un placer atenderte!"
            )
        elif not has_order:
            msg = (
                f"¡Hola! 😊 Seguimos aquí por si necesitas algo en {table_name}. "
                f"¿Te puedo ayudar con algo o ver el menú?"
            )
        else:
            msg = (
                f"¡Hola! 😊 ¿Todo bien en {table_name}? "
                f"Aquí estamos si necesitas algo más."
            )

        sent = await _send_whatsapp(phone, msg, bot_number, db_phone_id)
        if sent:
            await db.db_mark_session_warned(session["id"])
            await _create_inactivity_alert(session)
            print(f"⏰ Scheduler: advertencia → {phone} ({table_name})", flush=True)


async def _process_closeable_session(session: dict):
    """Cierra una sesión inactiva con semáforo."""
    async with _scheduler_semaphore:
        phone      = session["phone"]
        bot_number = session["bot_number"]
        table_name = session.get("table_name", "tu mesa")
        db_phone_id = session.get("meta_phone_id")

        await _send_whatsapp(
            phone,
            f"¡Hasta pronto! 👋 Fue un placer atenderte en {table_name}. ¡Esperamos verte de nuevo pronto!",
            bot_number,
            db_phone_id
        )

        await db.db_close_session(
            phone=phone,
            bot_number=bot_number,
            reason="inactivity_timeout",
            closed_by_username=""
        )

        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM conversations WHERE phone=$1 AND bot_number=$2",
                phone, bot_number
            )

        print(f"🔒 Scheduler: sesión cerrada por inactividad → {phone} ({table_name})", flush=True)


async def _run_inactivity_check():
    try:
        # PASO 1: Sesiones stale — procesar en paralelo (V-13 FIX: asyncio.gather)
        stale = await db.db_get_stale_sessions()
        if stale:
            await asyncio.gather(
                *[_process_stale_session(s) for s in stale],
                return_exceptions=True
            )

        # PASO 2: Sesiones a cerrar — procesar en paralelo
        closeable = await db.db_get_closeable_sessions()
        if closeable:
            await asyncio.gather(
                *[_process_closeable_session(s) for s in closeable],
                return_exceptions=True
            )

        # PASO 3: Limpieza periódica de tokens expirados (V-06)
        # Solo cada 10 ejecuciones (cada ~10 minutos)
        if not hasattr(_run_inactivity_check, '_counter'):
            _run_inactivity_check._counter = 0
        _run_inactivity_check._counter += 1
        if _run_inactivity_check._counter % 10 == 0:
            await db.db_cleanup_expired_sessions()

    except Exception:
        import traceback
        print(f"❌ Scheduler error: {traceback.format_exc()}", flush=True)


async def _scheduler_loop():
    print("⏰ Scheduler de inactividad iniciado", flush=True)
    while True:
        await asyncio.sleep(60)
        await _run_inactivity_check()


async def start_scheduler():
    asyncio.create_task(_scheduler_loop())