import asyncio
import json
import os
from datetime import date, datetime, timedelta, timezone


from app.services import database as db
from app.services import state_store
from app.repositories import reviews_repo as rr
from app.repositories import weekly_reports_repo
from app.repositories import restaurant_repo
from app.services.logging import get_logger, mask_email
from app.services.email import send_email
from app.services.email_templates import render_weekly_report_email

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
            bot_number=session["bot_number"],
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
        bot_number = session["bot_number"]
        table_name = session.get("table_name", "tu mesa")

        # 🛡️ FIX MULTI-WORKER: close in the DB first; None means another
        # worker won the race.
        closed_session = await db.db_close_session(
            phone=phone,
            bot_number=bot_number,
            reason="inactivity_timeout",
            closed_by_username="system"
        )

        if not closed_session:
            return  # Otro worker ya la cerró

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


async def _run_occupancy_snapshot():
    """
    Capture a point-in-time occupancy snapshot for every restaurant.
    Runs every 15 minutes via the scheduler loop.
    """
    from app.services.tenant_context import tenant_scope, bypass_tenant_scope  # noqa: PLC0415

    try:
        # Wave-2: occupancy is per-SEDE (each location has its own tables),
        # not per-business. Iterate every (org, location) pair so a multi-sede
        # tenant gets one snapshot row per sede instead of one per "matriz".
        # The old loop relied on the Matriz invariant (rid == location_id of
        # the primary sede) and silently dropped data for sub-sucursales.
        orgs = await db.db_get_all_orgs(active_only=True)
        for org in orgs:
            org_id = org["id"]
            try:
                with bypass_tenant_scope("scheduler.occupancy_snapshot.list_locations"):
                    locations = await db.db_get_org_locations(org_id, active_only=True)
            except Exception:
                log.exception("scheduler.occupancy_snapshot.locations_failed", org_id=org_id)
                continue

            for loc in locations:
                location_id = loc["id"]
                with tenant_scope(org_id):
                    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
                    async with tenant_connection() as conn:
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
                            location_id,
                        )
                if tables_row:
                    with tenant_scope(org_id):
                        await rr.db_save_occupancy_snapshot(
                            restaurant_id=org_id,
                            branch_id=location_id,
                            total_tables=tables_row["total_tables"],
                            occupied_tables=tables_row["occupied_tables"],
                            total_capacity=tables_row["total_capacity"],
                            seated_guests=tables_row["seated_guests"],
                        )
    except Exception:
        log.exception("scheduler.occupancy_snapshot_failed")


async def _resolve_owner_email(restaurant: dict, org_id: int) -> "str | None":
    """Resolve the destination email for weekly-report delivery.

    WhatsApp is being retired; email is now the primary channel for the
    weekly owner report. Preference order:
      1. `features.owner_email` — explicit override an admin can set.
      2. The org's registered owner/admin/gerente user — `users.username`
         IS the login email in this schema (same convention relied on by
         auth_routes.py's forgot-password flow).

    `users` is a GLOBAL table (no restaurant_id/org_id, no RLS — see
    CLAUDE.md "Blindaje Multi-tenant RLS"), so `db_get_team_users` uses the
    global pool directly and does not need `tenant_scope`. Never raises —
    any lookup failure just means "no destination", handled by the caller.
    """
    explicit = (_features_dict(restaurant).get("owner_email") or "").strip().lower()
    if explicit and "@" in explicit:
        return explicit

    try:
        team = await restaurant_repo.db_get_team_users(int(org_id))
    except Exception:
        log.exception("scheduler.weekly_reports.owner_lookup_failed", restaurant_id=org_id)
        return None

    role_priority = {"owner": 0, "admin": 1, "gerente": 2}
    candidates = sorted(
        (u for u in team if "@" in (u.get("username") or "")),
        key=lambda u: role_priority.get(u.get("role"), 99),
    )
    if candidates:
        return candidates[0]["username"].strip().lower()
    return None


async def _run_weekly_owner_reports():
    """
    Send a weekly performance summary to each restaurant owner via email.
    Runs every scheduler tick (60s) but only sends when the restaurant's local
    time is Monday 09:xx AND no report has been sent yet for the current week.
    Dry-run mode: set WEEKLY_REPORT_DRY_RUN=1 to skip actual email send.
    """
    from app.services.tenant_context import tenant_scope, bypass_tenant_scope  # noqa: PLC0415

    dry_run = os.getenv("WEEKLY_REPORT_DRY_RUN", "0") == "1"
    # APP_DOMAIN is a bare domain (e.g. "mesioai.com"), not a URL — see the
    # convention in orders.py/tables.py/dashboard.py (`f"https://{APP_DOMAIN}"`).
    # A prior version of this line defaulted to a full URL ("https://mesio.com")
    # but prepended nothing when APP_DOMAIN WAS set, producing a schemeless
    # "mesioai.com/dashboard" link in the email button.
    _app_domain = os.getenv("APP_DOMAIN", "").strip()
    dashboard_url = f"https://{_app_domain}/dashboard" if _app_domain else "https://mesio.com/dashboard"

    try:
        # Wave-2 canonical: weekly reports are per-business (per-org), not per-sede.
        # Use the org-level enumeration primitive (db_get_all_orgs) instead of
        # db_get_all_restaurants which returns one row per location.
        # db_get_all_orgs returns active orgs only.
        # db_get_all_orgs is GLOBAL (no tenant scope), but wrap in bypass so that
        # if it ever gains tenant_connection usage this code stays correct (Rule 14).
        with bypass_tenant_scope("scheduler_weekly_reports_enumerate"):
            restaurants = await db.db_get_all_orgs(active_only=True)
    except Exception:
        log.exception("scheduler.weekly_reports.fetch_restaurants_failed")
        return

    for restaurant in restaurants:
        rid = restaurant.get("id")  # org_id — what tenant_scope expects
        restaurant_name = restaurant.get("name", f"Restaurant {rid}")
        try:
            # ── 1. Resolve timezone and local time ────────────────────────────
            tz_name = _features_dict(restaurant).get("timezone") or "America/Bogota"
            local_now = _local_now(tz_name)

            # ── 2. Only proceed on Monday 09:xx local time ────────────────────
            if local_now.weekday() != 0 or local_now.hour != 9:
                continue

            # ── 3. Compute week window (previous week: Mon–Sun) ───────────────
            # week_start = last Monday (7 days ago), week_end = this Monday (exclusive)
            week_start: date = local_now.date() - timedelta(days=7)
            week_end: date = week_start + timedelta(days=7)

            # Steps 4-10 use weekly_reports_repo which calls tenant_connection().
            # Must be inside tenant_scope(rid) — without this, TenantNotSetError
            # was silently swallowed by the outer except, so reports never ran (Fix 3).
            with tenant_scope(int(rid)):
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

                # ── 9. Resolve owner phone (kept for the row's audit history —
                #        schema predates the email channel) and owner email
                #        (the actual delivery destination now) ─────────────────
                owner_phone = _features_dict(restaurant).get("owner_phone")
                owner_email = await _resolve_owner_email(restaurant, rid)

                # ── 10. Build payload and persist the report row ──────────────────
                payload = weekly_reports_repo.build_payload(stats, week_start, week_end)

                if dry_run:
                    initial_status = "dry_run"
                elif owner_email:
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
                    error_message=None if owner_email else "missing_owner_email",
                )

                # ON CONFLICT DO NOTHING → row exists for this week. If it's a
                # failed row with attempts < MAX, retry; otherwise skip.
                if report_row is None:
                    retriable = await weekly_reports_repo.get_retriable_report(
                        rid, week_start,
                    )
                    if retriable is None:
                        log.info(
                            "scheduler.weekly_reports.conflict_skip",
                            restaurant_id=rid,
                            week_start=week_start.isoformat(),
                        )
                        continue
                    report_row = retriable
                    log.info(
                        "scheduler.weekly_reports.retry",
                        restaurant_id=rid,
                        week_start=week_start.isoformat(),
                        attempts=report_row.get("attempts", 0),
                    )

                report_id = report_row["id"]

                # ── 11. Send (or dry-run) ─────────────────────────────────────────
                if dry_run:
                    log.info(
                        "scheduler.weekly_reports.dry_run",
                        restaurant_id=rid,
                        owner_email=mask_email(owner_email),
                        week_start=week_start.isoformat(),
                    )
                    continue

                if not owner_email:
                    log.info(
                        "scheduler.weekly_reports.no_owner_email",
                        restaurant_id=rid,
                        week_start=week_start.isoformat(),
                    )
                    continue

            # Send happens outside tenant_scope — send_email is pure HTTP (no DB).
            try:
                subject, html, text = render_weekly_report_email(
                    restaurant_name, msg, dashboard_url, week_start, week_end
                )
                ok = await send_email(to=owner_email, subject=subject, html=html, text=text)
                if ok:
                    with tenant_scope(int(rid)):
                        await weekly_reports_repo.mark_sent(report_id)
                    log.info(
                        "scheduler.weekly_reports.sent",
                        restaurant_id=rid,
                        owner_email=mask_email(owner_email),
                        week_start=week_start.isoformat(),
                    )
                else:
                    error_msg = "email_send_returned_false"
                    with tenant_scope(int(rid)):
                        await weekly_reports_repo.mark_failed(report_id, error_msg)
                    log.warning(
                        "scheduler.weekly_reports.send_failed",
                        restaurant_id=rid,
                        owner_email=mask_email(owner_email),
                        week_start=week_start.isoformat(),
                        reason=error_msg,
                    )
            except Exception:
                error_str = "email_send_exception"
                with tenant_scope(int(rid)):
                    await weekly_reports_repo.mark_failed(report_id, error_str)
                log.exception(
                    "scheduler.weekly_reports.send_exception",
                    restaurant_id=rid,
                    owner_email=mask_email(owner_email),
                    week_start=week_start.isoformat(),
                )

        except Exception:
            log.exception(
                "scheduler.weekly_reports.restaurant_loop_failed",
                restaurant_id=rid,
            )


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

            # Capture occupancy snapshots every 15 minutes
            if _reminder_counter % 15 == 0:
                await _run_occupancy_snapshot()
                if not await _renew_or_abort(leader_token):
                    log.warning("scheduler.tick_aborted_after_occupancy_snapshot")
                    continue

            # Expire unanswered NPS surveys every 30 minutes. (The 24h
            # WhatsApp reminder that ran here was dropped with the channel.)
            if _reminder_counter % 30 == 0:
                await _run_nps_waiting_cleanup()
                if not await _renew_or_abort(leader_token):
                    log.warning("scheduler.tick_aborted_after_nps_cleanup")
                    continue

            # Send weekly owner reports (runs every tick; skips internally when not Monday 09:xx)
            await _run_weekly_owner_reports()

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


async def start_scheduler():
    asyncio.create_task(_scheduler_loop())