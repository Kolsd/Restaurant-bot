import asyncio
import os
import httpx
from app.services import database as db
from app.services import state_store
from app.repositories import reviews_repo as rr
from app.services.logging import get_logger

log = get_logger(__name__)

META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")

# Semáforo para limitar concurrencia del scheduler (V-13 parcial)
_scheduler_semaphore = asyncio.Semaphore(10)


async def _send_whatsapp(phone: str, message: str, bot_number: str, db_phone_id: str = None):
    token    = os.getenv("META_ACCESS_TOKEN", "")
    phone_id = db_phone_id or os.getenv("META_PHONE_NUMBER_ID", "")
    if not token or not phone_id:
        log.warning("scheduler.whatsapp_not_configured", phone=phone)
        return False
    clean_phone = phone.lstrip("+").replace(" ", "")
    url  = f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages"
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
        log.error("scheduler.whatsapp_send_failed", phone=phone, error=str(e))
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
        log.error("scheduler.inactivity_alert_failed", phone=session.get("phone"), error=str(e))


async def _process_stale_session(session: dict):
    """Procesa una sesión individual con semáforo para limitar concurrencia."""
    async with _scheduler_semaphore:
        phone      = session["phone"]
        bot_number = session["bot_number"]
        table_name = session.get("table_name", "tu mesa")
        order_delivered = session.get("order_delivered", False)
        has_order       = session.get("has_order", False)
        db_phone_id     = session.get("meta_phone_id")

        # 🛡️ FIX MULTI-WORKER: Intentamos marcar la sesión en la base de datos PRIMERO.
        # Si retorna False, significa que otro worker ya lo hizo en el mismo milisegundo.
        warned = await db.db_mark_session_warned(session["id"])
        if not warned:
            return

        if order_delivered:
            msg = (
                f"¡Hola! Esperamos que hayas disfrutado tu comida en {table_name}. "
                f"Cuando estés listo, puedes pedir la cuenta o llamar al mesero por aquí. ¡Es un placer atenderte!"
            )
        elif not has_order:
            msg = (
                f"¡Hola! Seguimos por aquí si necesitas algo en {table_name}. "
                f"¿Te ayudo con algo o te muestro el menú?"
            )
        else:
            msg = (
                f"¡Hola! ¿Todo bien en {table_name}? "
                f"Avísanos si necesitas algo más."
            )

        sent = await _send_whatsapp(phone, msg, bot_number, db_phone_id)
        if sent:
            await _create_inactivity_alert(session)
            log.info("scheduler.inactivity_warning_sent", phone=phone, table_name=table_name)


async def _process_closeable_session(session: dict):
    """Cierra una sesión inactiva con semáforo."""
    async with _scheduler_semaphore:
        phone      = session["phone"]
        bot_number = session["bot_number"]
        table_name = session.get("table_name", "tu mesa")
        db_phone_id = session.get("meta_phone_id")

        # 🛡️ FIX MULTI-WORKER: Intentamos cerrar la sesión en la base de datos ANTES
        # de mandar el WhatsApp. Si retorna None, otro worker ganó la carrera.
        closed_session = await db.db_close_session(
            phone=phone,
            bot_number=bot_number,
            reason="inactivity_timeout",
            closed_by_username="system"
        )

        if not closed_session:
            return  # Otro worker ya la cerró

        await _send_whatsapp(
            phone,
            f"Tu sesión en {table_name} ha sido cerrada por inactividad. ¡Fue un placer atenderte, esperamos verte pronto! 👋",
            bot_number,
            db_phone_id
        )

        # Cancelar NPS pendiente: si el usuario no respondió la encuesta antes de que
        # el scheduler cerrara la sesión por inactividad, no tiene sentido mantener el
        # estado NPS activo. La próxima vez que escriba debe poder ordenar sin bloqueos.
        try:
            await state_store.nps_delete(phone, bot_number)
        except Exception as e:
            log.error("scheduler.nps_state_clear_failed", phone=phone, error=str(e))

        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM conversations WHERE phone=$1 AND bot_number=$2",
                phone, bot_number
            )

        log.info("scheduler.session_closed_inactivity", phone=phone, table_name=table_name)


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
        log.exception("scheduler.inactivity_check_failed")


async def _run_deposit_expiry():
    """
    Cancel reservations whose deposit was never paid within 2 hours.
    Runs every 10 minutes via the scheduler loop counter.
    """
    try:
        from app.repositories import reservation_deposits_repo as deposits_repo  # noqa: PLC0415
        expired = await deposits_repo.db_get_pending_deposits(older_than_hours=2)
        for deposit in expired:
            reservation_id = deposit.get("reservation_id")
            if not reservation_id:
                continue
            try:
                await db.db_cancel_reservation(reservation_id, "deposit_expired")
                log.info("scheduler.reservation_deposit_expired", reservation_id=reservation_id)
            except Exception as e:
                log.error("scheduler.reservation_cancel_failed", reservation_id=reservation_id, error=str(e))
    except Exception:
        log.exception("scheduler.deposit_expiry_failed")


async def _run_occupancy_snapshot():
    """
    Capture a point-in-time occupancy snapshot for every restaurant.
    Runs every 15 minutes via the scheduler loop.
    """
    try:
        restaurants = await db.db_get_all_restaurants()
        pool = await db.get_pool()
        for rest in restaurants:
            rid = rest["id"]
            async with pool.acquire() as conn:
                # Count distinct active table sessions and sum capacities
                tables_row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(rt.id)::int AS total_tables,
                        COUNT(rt.id) FILTER (
                            WHERE ts.id IS NOT NULL AND ts.closed_at IS NULL
                        )::int AS occupied_tables,
                        COALESCE(SUM(rt.capacity), 0)::int AS total_capacity,
                        COALESCE(SUM(rt.capacity) FILTER (
                            WHERE ts.id IS NOT NULL AND ts.closed_at IS NULL
                        ), 0)::int AS seated_guests
                    FROM restaurant_tables rt
                    LEFT JOIN table_sessions ts
                        ON ts.table_id = rt.id AND ts.closed_at IS NULL
                    WHERE rt.branch_id = $1 AND rt.active = TRUE
                    """,
                    rid,
                )
            if tables_row:
                await rr.db_save_occupancy_snapshot(
                    restaurant_id=rid,
                    branch_id=rid,
                    total_tables=tables_row["total_tables"],
                    occupied_tables=tables_row["occupied_tables"],
                    total_capacity=tables_row["total_capacity"],
                    seated_guests=tables_row["seated_guests"],
                )
    except Exception:
        log.exception("scheduler.occupancy_snapshot_failed")


async def _run_reservation_reminders():
    """Send WhatsApp reminders for upcoming confirmed reservations (24h ahead)."""
    try:
        upcoming = await db.db_get_upcoming_unconfirmed(hours_ahead=24)
        for res in upcoming:
            phone = res.get("phone", "")
            bot_number = res.get("bot_number", "")
            name = res.get("name", "")
            date_str = res.get("date", "")
            time_str = res.get("time", "")
            guests = res.get("guests", 1)
            if not phone or not bot_number:
                continue
            msg = (
                f"Hola {name}, te recordamos tu reserva para el {date_str} "
                f"a las {time_str} para {guests} persona(s). "
                f"Responde *CONFIRMAR* para confirmar o *CANCELAR* para cancelar."
            )
            # Resolve phone_id from restaurant
            restaurant = await db.db_get_restaurant_by_bot_number(bot_number)
            phone_id = (restaurant or {}).get("meta_phone_id", "")
            ok = await _send_whatsapp(phone, msg, bot_number, phone_id)
            if ok:
                await db.db_mark_confirmation_sent(res["id"])
    except Exception:
        log.exception("scheduler.reservation_reminder_failed")


async def _scheduler_loop():
    log.info("scheduler.started")
    _reminder_counter = 0
    while True:
        await asyncio.sleep(60)
        await _run_inactivity_check()
        _reminder_counter += 1
        # Run reservation reminders every 5 minutes
        if _reminder_counter % 5 == 0:
            await _run_reservation_reminders()
        # Run deposit expiry every 10 minutes
        if _reminder_counter % 10 == 0:
            await _run_deposit_expiry()
        # Capture occupancy snapshots every 15 minutes
        if _reminder_counter % 15 == 0:
            await _run_occupancy_snapshot()


async def start_scheduler():
    asyncio.create_task(_scheduler_loop())