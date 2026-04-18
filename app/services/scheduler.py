import asyncio
import json
import os
from datetime import date, datetime, timedelta

import httpx

from app.services import database as db
from app.services import state_store
from app.repositories import reviews_repo as rr
from app.repositories import weekly_reports_repo
from app.services.logging import get_logger

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

log = get_logger(__name__)


def _local_now(tz_name: str) -> datetime:
    """Return current datetime in the given IANA timezone. Falls back to America/Bogota."""
    try:
        if ZoneInfo is None:
            raise RuntimeError("zoneinfo unavailable")
        return datetime.now(ZoneInfo(tz_name or "America/Bogota"))
    except Exception:
        try:
            return datetime.now(ZoneInfo("America/Bogota")) if ZoneInfo else datetime.utcnow()
        except Exception:
            return datetime.utcnow()

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
    from app.services.tenant_context import tenant_scope  # noqa: PLC0415

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
                with tenant_scope(rid):
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


async def _run_weekly_owner_reports():
    """
    Send a weekly performance summary to each restaurant owner via WhatsApp.
    Runs every scheduler tick (60s) but only sends when the restaurant's local
    time is Monday 09:xx AND no report has been sent yet for the current week.
    Dry-run mode: set WEEKLY_REPORT_DRY_RUN=1 to skip actual WhatsApp send.
    """
    from app.services.tenant_context import tenant_scope  # noqa: PLC0415

    dry_run = os.getenv("WEEKLY_REPORT_DRY_RUN", "0") == "1"
    dashboard_url = os.getenv("APP_DOMAIN", "https://mesio.com").rstrip("/") + "/dashboard"

    try:
        restaurants = await db.db_get_all_restaurants()
    except Exception:
        log.exception("scheduler.weekly_reports.fetch_restaurants_failed")
        return

    for restaurant in restaurants:
        rid = restaurant.get("id")
        restaurant_name = restaurant.get("name", f"Restaurant {rid}")
        try:
            # ── 1. Resolve timezone and local time ────────────────────────────
            tz_name = (restaurant.get("features") or {}).get("timezone") or "America/Bogota"
            local_now = _local_now(tz_name)

            # ── 2. Only proceed on Monday 09:xx local time ────────────────────
            if local_now.weekday() != 0 or local_now.hour != 9:
                continue

            # ── 3. Compute week window (previous week: Mon–Sun) ───────────────
            # week_start = last Monday (7 days ago), week_end = this Monday (exclusive)
            week_start: date = local_now.date() - timedelta(days=7)
            week_end: date = week_start + timedelta(days=7)

            # ── 4. Skip if already sent this week ─────────────────────────────
            if await weekly_reports_repo.already_sent_for_week(rid, week_start):
                log.info(
                    "scheduler.weekly_reports.already_sent",
                    restaurant_id=rid,
                    week_start=week_start.isoformat(),
                )
                continue

            # ── 5. Check feature flag (default True, skip if explicitly False) ─
            features = restaurant.get("features") or {}
            if isinstance(features, str):
                try:
                    features = json.loads(features)
                except Exception:
                    features = {}
            if features.get("weekly_report_enabled") is False:
                log.info(
                    "scheduler.weekly_reports.feature_disabled",
                    restaurant_id=rid,
                )
                continue

            # ── 6. Compute stats ──────────────────────────────────────────────
            stats = await weekly_reports_repo.compute_weekly_stats(rid, week_start, week_end)

            # ── 7. Skip dormant / new restaurants with no signal ──────────────
            if not stats["has_signal"]:
                log.info(
                    "scheduler.weekly_reports.no_signal_skip",
                    restaurant_id=rid,
                    week_start=week_start.isoformat(),
                )
                continue

            # ── 8. Format message ─────────────────────────────────────────────
            msg = weekly_reports_repo.format_report_message(
                stats, restaurant_name, week_start, week_end, dashboard_url
            )
            if not msg:
                log.info(
                    "scheduler.weekly_reports.empty_message_skip",
                    restaurant_id=rid,
                )
                continue

            # ── 9. Resolve owner phone ────────────────────────────────────────
            owner_phone = (restaurant.get("features") or {}).get("owner_phone")

            # ── 10. Build payload and persist the report row ──────────────────
            payload = weekly_reports_repo.build_payload(stats, week_start, week_end)

            if dry_run:
                initial_status = "dry_run"
            elif owner_phone:
                initial_status = "pending"
            else:
                initial_status = "skipped"

            report_row = await weekly_reports_repo.save_report(
                restaurant_id=rid,
                week_start=week_start,
                payload=payload,
                message_text=msg,
                owner_phone=owner_phone,
                delivery_status=initial_status,
                error_message=None if owner_phone else "missing_owner_phone",
            )

            # ON CONFLICT DO NOTHING → already exists; skip to avoid double-send
            if report_row is None:
                log.info(
                    "scheduler.weekly_reports.conflict_skip",
                    restaurant_id=rid,
                    week_start=week_start.isoformat(),
                )
                continue

            report_id = report_row["id"]

            # ── 11. Send (or dry-run) ─────────────────────────────────────────
            if dry_run:
                log.info(
                    "scheduler.weekly_reports.dry_run",
                    restaurant_id=rid,
                    owner_phone=owner_phone,
                    week_start=week_start.isoformat(),
                )
                continue

            if not owner_phone:
                log.info(
                    "scheduler.weekly_reports.no_owner_phone",
                    restaurant_id=rid,
                    week_start=week_start.isoformat(),
                )
                continue

            bot_number = restaurant.get("whatsapp_number", "")
            phone_id = restaurant.get("wa_phone_id")

            try:
                ok = await _send_whatsapp(owner_phone, msg, bot_number, phone_id)
                if ok:
                    await weekly_reports_repo.mark_sent(report_id)
                    log.info(
                        "scheduler.weekly_reports.sent",
                        restaurant_id=rid,
                        owner_phone=owner_phone,
                        week_start=week_start.isoformat(),
                    )
                else:
                    error_msg = "whatsapp_send_returned_false"
                    await weekly_reports_repo.mark_failed(report_id, error_msg)
                    log.warning(
                        "scheduler.weekly_reports.send_failed",
                        restaurant_id=rid,
                        owner_phone=owner_phone,
                        week_start=week_start.isoformat(),
                        reason=error_msg,
                    )
            except Exception as exc:
                error_str = str(exc)[:500]
                await weekly_reports_repo.mark_failed(report_id, error_str)
                log.exception(
                    "scheduler.weekly_reports.send_exception",
                    restaurant_id=rid,
                    owner_phone=owner_phone,
                    week_start=week_start.isoformat(),
                )

        except Exception:
            log.exception(
                "scheduler.weekly_reports.restaurant_loop_failed",
                restaurant_id=rid,
            )


async def _renew_or_abort(token: str, ttl_seconds: int = 90) -> bool:
    """
    Renew the scheduler leader lease.  Returns False if the lease was lost
    (another worker took over), signalling that the current tick should stop.
    """
    from app.services import state_store  # noqa: PLC0415
    return await state_store.scheduler_leader_renew(token, ttl_seconds=ttl_seconds)


async def _scheduler_loop():
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415

    log.info("scheduler.started")
    _reminder_counter = 0
    while True:
        await asyncio.sleep(60)

        # Leader election: only one worker runs the scheduler tick.
        # TTL=90s covers the 60s sleep + processing time.
        # acquire() now returns a UUID token (or None if not leader).
        from app.services import state_store  # noqa: PLC0415
        leader_token = await state_store.scheduler_leader_acquire(ttl_seconds=90)
        if not leader_token:
            continue

        # The scheduler tick enumerates all restaurants (cross-tenant) and then
        # performs per-restaurant operations.  The bypass allows cross-tenant DB
        # queries; individual per-restaurant helpers apply tenant_scope() internally
        # when they need to call tenant-scoped repos.
        #
        # We renew the lease at each phase boundary.  If a slow DB or Anthropic
        # rate-limit caused a previous phase to exceed the TTL, another worker
        # will have stolen the lease and renew returns False — we break out
        # immediately rather than double-executing the remaining phases.
        with bypass_tenant_scope("scheduler_leader_tick"):
            await _run_inactivity_check()

            if not await _renew_or_abort(leader_token):
                log.warning("scheduler.tick_aborted_after_inactivity_check")
                continue

            from app.services.alerts import check_alerts  # late import — avoids circular
            await check_alerts()

            if not await _renew_or_abort(leader_token):
                log.warning("scheduler.tick_aborted_after_alerts")
                continue

            _reminder_counter += 1
            # Run reservation reminders every 5 minutes
            if _reminder_counter % 5 == 0:
                await _run_reservation_reminders()
                if not await _renew_or_abort(leader_token):
                    log.warning("scheduler.tick_aborted_after_reminders")
                    continue

            # Run deposit expiry every 10 minutes
            if _reminder_counter % 10 == 0:
                await _run_deposit_expiry()
                if not await _renew_or_abort(leader_token):
                    log.warning("scheduler.tick_aborted_after_deposit_expiry")
                    continue

            # Capture occupancy snapshots every 15 minutes
            if _reminder_counter % 15 == 0:
                await _run_occupancy_snapshot()
                if not await _renew_or_abort(leader_token):
                    log.warning("scheduler.tick_aborted_after_occupancy_snapshot")
                    continue

            # Send weekly owner reports (runs every tick; skips internally when not Monday 09:xx)
            await _run_weekly_owner_reports()


async def start_scheduler():
    asyncio.create_task(_scheduler_loop())