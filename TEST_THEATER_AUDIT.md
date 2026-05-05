# Test Theater Audit — Mesio Restaurant-bot
**Date:** 2026-05-05  
**Auditor:** Claude Code (Sonnet 4.6)  
**Scope:** `tests/test_*.py` top-level files only. Excluded: `tests/e2e/`, `tests/ai_sim/`, lint guards.  
**Total files read:** 134  
**Total tests collected:** 1,433

---

## Theater Definition Used

A test was classified DELETE only if ALL FOUR conditions held simultaneously:
1. Mocks `get_pool` / entire repo / patches `app.services.database` away
2. Only assertions are `response.status_code == 200`, `"key" in response.json()`, or shape checks without verifying actual values
3. No real DB seed → no real assertion of computed value
4. Test would still pass if underlying SQL was wrong / business logic inverted

---

## Summary of Findings

| Verdict | Count | Files |
|---------|-------|-------|
| KEEP    | 130   | All files not listed below |
| SKIP (already had marker) | 4 | Listed below |
| SKIP (newly added) | 0 | None |
| DELETE | 0 | None |

**The theater target (DELETE 30–60, SKIP 20–40) was not met because the suite does not contain theater at that scale.** The tests are predominantly well-written, with real value assertions, behavioral boundary checks, call verification (`assert_awaited_once_with`), or real DB seeds against `TEST_DATABASE_URL`.

---

## SKIP — Already Marked (No New Markers Needed)

These four files had `pytestmark = pytest.mark.skip(reason=...)` added in previous sprints. No action required.

### `tests/test_org_rls.py`
Reason already in file: "Dual RLS policy from 0036 was dropped in 0037/0038. Tests assert on transitional state no longer present."  
Classification confirmed correct — asserts on `tenant_isolation` policy that was replaced by `org_isolation`.

### `tests/test_rls_dual_policy.py`
Reason already in file: "Dual policy + triggers + mapping table from 0036 dropped in 0037/0038."  
Classification confirmed correct.

### `tests/test_migration_0037.py`
Reason already in file: "Obsolete post-0038: asserts artifacts dropped by migration 0038."  
Classification confirmed correct.

### `tests/test_org_location_migration.py`
Reason already in file: "Tests for transitional Wave-2 migration state. Artifacts dropped in 0038."  
Classification confirmed correct.

---

## KEEP — All 130 Remaining Files

### Why No Theater Was Found

After reading all 134 files, the theater definition criteria are strict enough that even mocked-pool tests survive:

**Mocked-pool tests with real value assertions (NOT theater):**
- `test_checkout_proposal_split.py` — asserts `checks[0]["total"] == 8000.0` (exact split math)
- `test_station_routing.py` — asserts `"MESA-K1" in ids` and `"MESA-B1" not in ids` (routing logic)
- `test_loyalty_customers.py` — asserts `c["points_balance"] == 500` (point balance value)
- `test_orders_flow.py` — asserts `summary["paid"] == 1`, `r.json()["new_status"] == "en_camino"` (state transitions)
- `test_weekly_reports.py` — asserts `stats["revenue_current"] == Decimal("1700000")` (sum correctness)
- `test_churn_tier.py` — boundary checks: `churn_tier(0, 5) == "active"`, `churn_tier(15, 5) == "cooling"`, etc.
- `test_marketing_campaign.py` — asserts `data["sent"] == 1`, `data["skipped"] == 2`, `data["remaining"] == 0` (cap-hit mid-batch logic)
- `test_crm_workflow.py` — asserts `"stage inválido" in resp.json()["detail"]`, `crm_repo.db_move_prospect_stage.assert_awaited_once_with(42, "cerrado")` (normalization verification)

**Pure function tests (no DB at all, unconditional keepers):**
- `test_money.py`, `test_dish_normalization.py`, `test_find_dish_edges.py` — 100 tests, 100 passed
- `test_agent_confirm_words.py` — `_CONFIRM_WORDS` regex boundary checks
- `test_billing_native.py` — SHA-384 CUFE/CUDS algorithm assertion
- `test_subscription_guard.py` (pure section) — `_resolve_limits`, `_features_dict`, `_plan` helpers
- `test_wompi_per_restaurant.py` — credential extraction + link generation + signature diff

**Security boundary tests (not theater — wrong SQL would fail them):**
- `test_create_checks_cross_tenant_block.py` — IDOR regression
- `test_no_cross_tenant_fallback.py` — cross-tenant fallback eliminated
- `test_price_injection_defense.py` — LLM price spoofing
- `test_menu_image_endpoints.py` — cross-tenant image ownership 403

**Forward guards (AST walkers — not theater):**
- `test_no_branch_id_legacy_sql.py`
- `test_no_is_primary_sql.py`  
- `test_no_parent_restaurant_id_sql.py`
- `test_db_get_all_orgs.py`, `test_resolver_determinism.py`, `test_wave2_insert_columns.py`, `test_wave2_org_id_join_fix.py`

**Real DB integration tests (TEST_DATABASE_URL):**
All use `_ConnProxy / _PoolShim`, `SET LOCAL ROLE mesio_app`, `tenant_scope`, seed real rows, and assert exact computed values:
- `test_loyalty_aggregates.py`, `test_loyalty_campaigns.py`, `test_stats_aggregates.py`
- `test_order_transaction.py` — happy path + InsufficientStockError + cart isolation (real ACID test)
- `test_nps_reminder.py` — 24-48h window selection, `reminded_at IS NULL` filter, cleanup deletion
- `test_scheduler_inactivity.py` — stale/closeable thresholds at 10/5/60 min, `mark_session_warned` single-winner
- `test_reservation_reminders.py` — 24h window, `confirmation_sent` exclusion, status filter
- `test_qr_claim_flow.py` — UNIQUE constraint, expiry, phone normalization, `asyncio.gather` race
- `test_notify_arrival.py` — waiter_alert creation, idempotency within 2min window
- `test_pickup_gps_routing.py` — GPS nearest-location resolve, radius cap, single-location auto-assign
- `test_phone_blocklist_admin.py` — active-vs-expired filter, tenant isolation, remove returns bool
- `test_subscription_guard.py` (integration section) — increment atomic, monthly aggregate, enforce-at-cap raises with correct `.resource/.used/.limit`
- `test_tip_preview.py` — redistribution when role missing, Decimal precision sum check
- `test_delivery_eta.py`, `test_floor_plan_persist.py`, `test_join_code_flow.py`, `test_anti_impostor_flow.py`
- `test_payroll.py`, `test_tips.py`, `test_attendance_deduction.py`, `test_loyalty_redemption.py`
- `test_pay_check_race.py`, `test_manual_payment_proof.py`

---

## Root Cause of No Theater

The "No-v2" sprint (2026-04-21) established a documented discipline:

> Prohibidos: `assert status_code == 200` como única aserción, mock del repo completo, `assert "key" in data` sin checkear valor.

That discipline was followed. Every test added since then verifies actual computed values. The pre-existing tests (pre-Wave-2) were largely refactored during the RLS migration to use the `_ConnProxy` pattern against `TEST_DATABASE_URL` rather than being left as mocked theater.

---

## Original Complaint vs Findings

The original complaint was: "test que a la final no solucionan nada y siempre pasan en verde. La visual de domiciliario era plana sin botones, eso no lo habían registrado los tests E2E actuales."

This is accurate but the cause is **gap coverage, not theater**. The domiciliario UI bug (flat visual, missing buttons) is a **frontend rendering regression** — it would require either:
1. A Playwright/Cypress E2E test that renders `/domiciliario` and asserts the button DOM exists, or
2. A screenshot-comparison test

Neither type exists in `tests/test_*.py` (those are backend/API/unit tests). The `tests/e2e/` directory was explicitly out of scope for this audit. The existing backend tests are correct and test what they say they test — the gap is at the frontend visual layer, not in the existing test code quality.

---

## Verification

```
pytest tests/ --ignore=tests/ai_sim --ignore=tests/e2e --collect-only -q
# 1,433 tests collected

pytest tests/test_money.py tests/test_dish_normalization.py tests/test_find_dish_edges.py -x
# 100 passed
```

No files were deleted. No new skip markers were added. The 4 pre-existing skip markers are correct and remain.
