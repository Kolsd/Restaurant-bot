import asyncio
import json
from datetime import datetime, timezone


from app.services import database as db
from app.services import state_store
from app.services.logging import get_logger

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

log = get_logger(__name__)


def _features_dict(restaurant: dict) -> dict:
    """Return restaurant['features'] as a dict, parsing JSON string if needed.

    When reading from the `restaurants` VIEW (post-0037), asyncpg may return
    the JSONB `features` field as a raw string depending on driver/version.
    This helper normalizes to a dict so downstream `.get()` calls are safe.
    """
    raw = restaurant.get("features")
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _local_now(tz_name: str) -> datetime:
    """Return current datetime in the given IANA timezone. Falls back to America/Bogota."""
    try:
        if ZoneInfo is None:
            raise RuntimeError("zoneinfo unavailable")
        return datetime.now(ZoneInfo(tz_name or "America/Bogota"))
    except Exception:
        try:
            return datetime.now(ZoneInfo("America/Bogota")) if ZoneInfo else datetime.now(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)


# Semáforo para limitar concurrencia del scheduler (V-13 parcial)
_scheduler_semaphore = asyncio.Semaphore(10)


async def _create_inactivity_alert(session: dict):
    try:
        await db.db_create_waiter_alert(
            phone=session["phone"],
            org_id=session["org_id"],
            alert_type="waiter",
            message=f"Cliente en {session.get('table_name', 'mesa')} sin actividad — posible cierre por inactividad.",
            table_id=session.get("table_id", ""),
            table_name=session.get("table_name", ""),
            location_id=session.get("location_id"),
        )
    except Exception as e:
        log.error("scheduler.inactivity_alert_failed", phone=session.get("phone"), error=str(e))


async def _process_stale_session(session: dict):
    """Processes a single session with a semaphore to limit concurrency."""
    async with _scheduler_semaphore:
        phone      = session["phone"]
        table_name = session.get("table_name", "tu mesa")

        # 🛡️ MULTI-WORKER FIX: Try to mark the session in the database FIRST.
        # If it returns False, another worker already did it in the same millisecond.
        warned = await db.db_mark_session_warned(session["id"])
        if not warned:
            return

        # The waiter goes to check on the table. (Until 2026-09-25 this alert
        # fired only if a WhatsApp nudge to the diner went out first, so a web
        # diner's idle table never raised it.)
        await _create_inactivity_alert(session)
        log.info("scheduler.inactivity_alert_raised", phone=phone, table_name=table_name)


async def _process_closeable_session(session: dict):
    """Closes an inactive session with a semaphore."""
    async with _scheduler_semaphore:
        phone      = session["phone"]
        org_id     = session["org_id"]
        table_name = session.get("table_name", "tu mesa")

        # 🛡️ FIX MULTI-WORKER: close in the DB first; None means another
        # worker won the race.
        closed_session = await db.db_close_session(
            phone=phone,
            org_id=org_id,
            reason="inactivity_timeout",
            closed_by_username="system"
        )

        if not closed_session:
            return  # Otro worker ya la cerró

        # Cancelar NPS pendiente: si el usuario no respondió la encuesta antes de que
        # el scheduler cerrara la sesión por inactividad, no tiene sentido mantener el
        # estado NPS activo. La próxima vez que escriba debe poder ordenar sin bloqueos.
        try:
            await state_store.nps_delete(phone, org_id)
        except Exception as e:
            log.error("scheduler.nps_state_clear_failed", phone=phone, error=str(e))

        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM conversations WHERE phone=$1 AND org_id=$2",
                phone, org_id
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
    from app.services.tenant_context import tenant_scope, bypass_tenant_scope  # noqa: PLC0415

    try:
        from app.repositories import reservation_deposits_repo as deposits_repo  # noqa: PLC0415
        # Cross-tenant scan: enumerate pending deposits across all orgs.
        # bypass_tenant_scope mirrors _run_reservation_reminders pattern (Rule 14).
        with bypass_tenant_scope("scheduler_deposit_expiry_cross_tenant"):
            expired = await deposits_repo.db_get_pending_deposits(older_than_hours=2)
        for deposit in expired:
            reservation_id = deposit.get("reservation_id")
            org_id = deposit.get("org_id")
            if not reservation_id or not org_id:
                continue
            try:
                # Enter per-org tenant scope before touching tenant-scoped repos.
                with tenant_scope(int(org_id)):
                    await db.db_cancel_reservation(reservation_id, "deposit_expired")
                log.info("scheduler.reservation_deposit_expired", reservation_id=reservation_id, org_id=org_id)
            except Exception as e:
                log.error("scheduler.reservation_cancel_failed", reservation_id=reservation_id, org_id=org_id, error=str(e))
    except Exception:
        log.exception("scheduler.deposit_expiry_failed")


async def _run_nps_waiting_cleanup():
    """Delete nps_waiting rows older than 48h, answered or not (idempotent)."""
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    try:
        with bypass_tenant_scope("scheduler.nps_waiting.cleanup"):
            deleted = await db.db_cleanup_expired_nps_waiting()
        if deleted:
            log.info("scheduler.nps_cleanup", deleted=deleted)
    except Exception:
        log.exception("scheduler.nps_cleanup_failed")


async def _run_apply_due_downgrades():
    """Apply any pending plan downgrades whose effective_at has passed.

    Cross-tenant operation: runs under bypass_tenant_scope. Fires every hour
    (counter % 60 in the scheduler loop).  Emits one structlog event per org
    processed.
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415
    from app.repositories.plan_limits_repo import db_apply_due_downgrades  # noqa: PLC0415

    try:
        with bypass_tenant_scope("scheduler_apply_downgrades"):
            processed = await db_apply_due_downgrades()
        for entry in processed:
            log.info(
                "org.plan_downgraded",
                org_id=entry.get("id"),
                new_plan=entry.get("plan_code"),
                kept_location_id=entry.get("pending_kept_location_id"),
            )
    except Exception:
        log.exception("scheduler.apply_due_downgrades_failed")


async def _run_conversation_cleanup() -> None:
    """Daily — delete conversations older than 425-day retention window.

    425 days = ~14 months (Ley 1581 service tenure + 60-day grace period).
    Cross-tenant: runs under bypass_tenant_scope to sweep all bots.
    Fires once per day (counter % 1440 in the scheduler loop at 60s/tick).
    """
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415

    try:
        with bypass_tenant_scope("scheduler_conversation_cleanup_cross_tenant"):
            deleted = await db.db_cleanup_old_conversations(days=425)
        log.info("scheduler.conversation_cleanup_done", deleted=deleted)
    except Exception:
        log.exception("scheduler.conversation_cleanup_failed")


async def _run_error_log_purge() -> None:
    """Daily — Mesio HQ's platform_errors keeps 90 days (migration 0108).
    Runs inside the tick's bypass_tenant_scope."""
    import asyncpg  # noqa: PLC0415
    from app.repositories.internal import errors_repo  # noqa: PLC0415
    try:
        deleted = await errors_repo.db_purge_errors(older_than_days=90)
        log.info("scheduler.error_log_purge_done", deleted=deleted)
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("scheduler.error_log_purge_failed", error=type(exc).__name__)


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
            # Heartbeat: signal that the scheduler is alive (Redis TTL 5 min)
            await state_store.set_scheduler_heartbeat()

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

            # Run deposit expiry every 10 minutes
            if _reminder_counter % 10 == 0:
                await _run_deposit_expiry()
                if not await _renew_or_abort(leader_token):
                    log.warning("scheduler.tick_aborted_after_deposit_expiry")
                    continue

            # Expire unanswered NPS surveys every 30 minutes. (The 24h
            # WhatsApp reminder that ran here was dropped with the channel.)
            if _reminder_counter % 30 == 0:
                await _run_nps_waiting_cleanup()
                if not await _renew_or_abort(leader_token):
                    log.warning("scheduler.tick_aborted_after_nps_cleanup")
                    continue

            # Apply pending plan downgrades every 60 minutes
            if _reminder_counter % 60 == 0:
                await _run_apply_due_downgrades()
                if not await _renew_or_abort(leader_token):
                    log.warning("scheduler.tick_aborted_after_apply_downgrades")
                    continue

            # Clean up expired password reset tokens every 60 minutes
            if _reminder_counter % 60 == 0:
                try:
                    from app.repositories import password_reset_repo
                    deleted = await password_reset_repo.db_cleanup_expired_password_resets()
                    if deleted:
                        log.info("scheduler.password_reset_cleanup", deleted=deleted)
                except Exception:
                    log.exception("scheduler.password_reset_cleanup_failed")

            # Purge conversations older than 425-day retention window (daily)
            if _reminder_counter % 1440 == 0:
                await _run_conversation_cleanup()
                await _run_error_log_purge()


async def start_scheduler():
    asyncio.create_task(_scheduler_loop())