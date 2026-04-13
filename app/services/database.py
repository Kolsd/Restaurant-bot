import os
import asyncpg
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from app.services.money import to_decimal, money_mul, money_sum, quantize_money, ZERO
from app.services.logging import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════
# EXCEPCIONES DE NEGOCIO
# ══════════════════════════════════════════════════════════════════════

class UsageLimitExceeded(Exception):
    """
    Se lanza cuando un restaurante supera su límite diario de tokens o facturas.
    Los límites se configuran en restaurants.features.plan_limits:
      { "daily_tokens": 100000, "daily_invoices": 50 }
    """
    def __init__(self, resource: str, used: int, limit: int):
        self.resource = resource
        self.used     = used
        self.limit    = limit
        super().__init__(
            f"Límite diario de {resource} alcanzado: {used:,}/{limit:,}. "
            "Actualiza tu plan para continuar."
        )

_pool = None

SESSION_TTL_HOURS = 72  # V-06: tokens expiran en 72 horas

def _normalize_phone(number: str) -> str:
    if not number: return ""
    return number.replace(" ", "").replace("+", "")

def _to_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _serialize(d: dict) -> dict:
    # Datetimes → ISO string. All other types (including Decimal from NUMERIC columns)
    # pass through unchanged. Decimal→float conversion happens at the JSON boundary
    # (float(...) calls) in the repo layer, not here. Keeping Decimal intact preserves
    # precision for any further arithmetic done between _serialize and the response.
    result = {}
    for k, v in d.items():
        if hasattr(v, 'isoformat'):
            result[k] = v.isoformat()[:19]
        elif v is None:
            result[k] = None
        else:
            result[k] = v
    return result

async def get_pool():
    global _pool
    if _pool is None:
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            raise RuntimeError("DATABASE_URL no esta configurada")
        database_url = database_url.replace("postgres://", "postgresql://", 1)
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=2,
            max_size=20,
            command_timeout=30,
            init=lambda conn: conn.set_type_codec(
                'jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog'
            )
        )
    return _pool

async def init_pool():
    """Warm up the asyncpg connection pool. No DDL — use Alembic for schema."""
    await get_pool()


async def init_db():
    """Deprecated alias for init_pool(). DDL now lives in Alembic migrations."""
    await init_pool()


# db_init_nps_inventory re-exported below alongside the rest of the inventory module (Fase 6)

# === Reservations: moved to app.repositories.reservations_repo (Apparta integration) ===
from app.repositories.reservations_repo import (
    db_add_reservation,
    db_get_reservations_range,
    db_get_all_reservations,
    db_get_reservation_by_id,
    db_update_reservation_status,
    db_get_available_tables,
    db_assign_table_to_reservation,
    db_mark_no_show,
    db_cancel_reservation,
    db_get_reservations_by_status,
    db_get_reservation_stats,
    db_confirm_reservation,
    db_get_upcoming_unconfirmed,
    db_mark_confirmation_sent,
)


# === Orders: moved to app.repositories.orders_repo (Fase 6) ===
# ── ORDENES DELIVERY ─────────────────────────────────────────────────
from app.repositories.orders_repo import (
    db_save_order,
    db_confirm_payment,
    db_get_orders_range,
    db_get_order,
    db_get_all_orders,
    db_get_delivery_orders,
    db_update_pending_order_payment_method,
    db_update_order_status,
)

# === Conversations: moved to app.repositories.conversations_repo (Fase 6) ===
from app.repositories.conversations_repo import (
    db_get_history,
    db_save_history,
    db_get_all_conversations,
    db_delete_conversation,
    db_get_conversation_details,
    db_toggle_bot,
    db_cleanup_old_conversations,
)


# === Restaurants/Users/Menu: moved to app.repositories.restaurant_repo ===
from app.repositories.restaurant_repo import (
    db_get_user,
    db_create_user,
    db_get_all_users,
    db_get_restaurant_by_phone,
    db_get_restaurant_by_bot_number,
    db_get_restaurant_by_name,
    db_get_restaurant_by_id,
    db_get_all_restaurants,
    db_find_nearest_branch,
    db_find_nearest_branch_any,
    db_check_module,
    db_create_restaurant,
    db_sync_menu_to_branches,
    db_update_menu,
    db_get_menu,
    db_get_top_dishes,
    db_update_subscription,
    db_delete_branch,
    db_get_menu_availability,
    db_set_dish_availability,
    db_sync_batch,
    db_get_nps_stats,
    db_get_nps_responses,
    db_get_restaurant_settings,
    db_increment_token_usage,
    db_increment_invoice_usage,
    db_check_usage_limits,
)
# Re-export mutable dict and decorator so test_sync.py can access db._SYNC_HANDLERS / db._register_sync_handler
from app.repositories.restaurant_repo import _SYNC_HANDLERS, _register_sync_handler  # noqa: F401

# === Sessions: moved to app.repositories.sessions_repo (Fase 6) ===
from app.repositories.sessions_repo import (
    db_save_session,
    db_get_session,
    db_cleanup_expired_sessions,
)


# === Conversations (carts): moved to app.repositories.conversations_repo (Fase 6) ===
from app.repositories.conversations_repo import (
    db_get_cart,
    db_save_cart,
    db_clear_cart,
    db_migrate_cart,
)

# === Tables/POS: moved to app.repositories.tables_repo (Fase 6) ===
from app.repositories.tables_repo import (
    db_init_tables,
    db_get_tables,
    db_create_table,
    db_auto_create_table,
    db_delete_table,
    db_get_table_by_id,
    db_save_table_order,
    db_get_base_order_status,
    db_merge_table_order_items,
    db_get_table_orders,
    db_update_table_order_status,
    db_get_base_order_id,
    db_get_next_sub_number,
    db_get_table_bill,
    db_close_table_bill,
    db_mark_factura_generada,
    db_get_first_table_order,
    db_cleanup_after_checkout,
    db_get_open_session_by_phone,
    db_has_pending_invoice,
    db_get_active_table_order,
    db_init_waiter_alerts,
    db_create_waiter_alert,
    db_get_waiter_alerts,
    db_dismiss_waiter_alert,
    db_init_table_sessions,
    db_get_active_session,
    db_create_table_session,
    db_touch_session,
    db_touch_session_with_phone_id,
    db_session_mark_order,
    db_session_mark_delivered,
    db_mark_session_nps_pending,
    db_close_session,
    db_mark_session_warned,
    db_get_stale_sessions,
    db_get_closeable_sessions,
    db_get_closed_sessions,
    db_get_session_by_id,
    db_reopen_session,
)

# === Conversations (NPS per-conversation state): moved to app.repositories.conversations_repo (Fase 6) ===
# Restaurant-wide NPS analytics (db_get_nps_stats, db_get_nps_responses) moved to restaurant_repo.
from app.repositories.conversations_repo import (
    db_save_nps_response,
    db_save_nps_pending,
    db_update_nps_comment,
    db_get_pending_nps_score,
)

# === Conversations (NPS waiting state): moved to app.repositories.conversations_repo (Fase 6) ===
from app.repositories.conversations_repo import (
    db_save_nps_waiting,
    db_get_nps_waiting,
    db_clear_nps_waiting,
)


# === Inventory: moved to app.repositories.inventory_repo (Fase 6) ===
from app.repositories.inventory_repo import (
    db_init_nps_inventory,
    db_get_inventory,
    db_create_inventory_item,
    db_update_inventory_item,
    db_delete_inventory_item,
    db_adjust_inventory_stock,
    db_get_inventory_history,
    db_get_inventory_alerts,
    db_deduct_inventory_for_order,
    db_init_dish_recipes,
    db_upsert_dish_recipe,
    db_get_dish_recipe,
    db_get_all_recipes,
    db_delete_dish_recipe,
    db_get_food_costs,
    _sync_dish_availability,
    _sync_dish_availability_conn,
)


# ══════════════════════════════════════════════════════════════════════
# FACTURACIÓN ELECTRÓNICA DIAN — TABLAS FISCALES
# ══════════════════════════════════════════════════════════════════════

# === Fiscal/DIAN: moved to app.repositories.fiscal_repo (Fase 6) ===
from app.repositories.fiscal_repo import (
    db_init_fiscal_tables,
    db_get_fiscal_resolution,
    db_upsert_fiscal_resolution,
    db_claim_next_invoice_number,
    db_save_fiscal_invoice,
    db_get_fiscal_invoices,
    db_get_next_invoice_number,
    db_update_invoice_dian_data,
)


# ══════════════════════════════════════════════════════════════════════
# SUBSCRIPTION USAGE: moved to app.repositories.restaurant_repo
# ══════════════════════════════════════════════════════════════════════

# === Tables/POS — split checks + ticket data: moved to app.repositories.tables_repo (Fase 6) ===
from app.repositories.tables_repo import (
    db_get_order_ticket_data,
    db_init_table_checks,
    db_create_checks,
    db_get_checks,
    db_get_check,
    db_finalize_check_payment,
    db_delete_open_check,
    db_attach_proposal,
    db_set_check_tip,
    db_attach_proof,
    db_get_open_proposal_for_phone,
    db_list_checkout_proposals,
    db_cancel_checkout_proposal,
    db_get_check_ticket,
    db_update_table_properties,
    db_update_table_position,
    db_get_floor_plan,
)


# === Staff: moved to app.repositories.staff_repo (Fase 6) ===
from app.repositories.staff_repo import (
    db_get_staff,
    db_get_team_staff_by_branch,
    db_get_staff_for_pin_login,
    db_get_staff_candidates_by_name,
    db_create_staff,
    db_update_staff,
    db_delete_staff,
    _record_attendance_deduction,
    db_clock_in,
    db_clock_out,
    db_get_open_shifts,
    db_get_shifts,
    db_calculate_tip_pool,
    db_calculate_tips_by_attendance,
    db_save_tip_distribution,
    db_get_tip_distributions,
)


# === Loyalty: moved to app.repositories.loyalty_repo (Fase 6) ===
from app.repositories.loyalty_repo import (
    db_get_loyalty_balance,
    db_accrue_loyalty_points,
    db_redeem_loyalty_points,
    db_adjust_loyalty_points,
    db_get_loyalty_ledger,
    db_get_loyalty_stats,
    db_get_phone_for_base_order,
)


# === Conversations (WAM deduplication): moved to app.repositories.conversations_repo (Fase 6) ===
from app.repositories.conversations_repo import db_is_duplicate_wam


# === Staff (advanced): moved to app.repositories.staff_repo (Fase 6) ===
from app.repositories.staff_repo import (
    db_save_webauthn_credential,
    db_get_webauthn_credentials_by_staff,
    db_get_webauthn_credentials_by_restaurant,
    db_get_webauthn_credential,
    db_update_webauthn_sign_count,
    db_delete_webauthn_credential,
    db_save_webauthn_challenge,
    db_consume_webauthn_challenge,
    db_cleanup_expired_challenges,
    db_start_break,
    db_end_break,
    db_get_breaks_for_shift,
    db_get_open_break,
    db_upsert_schedule,
    db_bulk_upsert_schedules,
    db_get_schedules,
    db_delete_schedule,
    db_edit_shift,
    db_get_timecard,
    db_get_overtime_report,
    db_get_attendance_report,
    db_list_deduction_items,
    db_create_deduction_item,
    db_update_deduction_item,
    db_delete_deduction_item,
    db_calculate_payroll,
    db_save_payroll_run,
    db_get_payroll_runs,
    db_get_payroll_run,
    db_approve_payroll_run,
    db_list_contract_templates,
    db_create_contract_template,
    db_update_contract_template,
    db_delete_contract_template,
    db_assign_staff_contract,
    db_list_overtime_requests,
    db_upsert_overtime_request,
    db_review_overtime_request,
)
