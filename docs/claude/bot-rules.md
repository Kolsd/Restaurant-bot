# Bot rules — DO NOT BREAK (17 rules)

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Bot Rules — DO NOT BREAK (learned from 79 bugs across 4 audits)

These rules protect the bot's critical flows. Any change to `agent.py`, `agent_salon.py`, `orders.py`, `orders_repo.py`, `state_store.py` or `routes/diner.py` MUST comply with ALL of these rules. The bot is reached only through the web chat (`POST /api/diner/chat` → `agent.chat()`); the WhatsApp channel — webhook, inbox worker, Meta API — was deleted on 2026-09-25, and rules 4 and 6 went with it.

### 1. Serialization: Decimal NEVER in state_store
- `state_store` serializes with `json.dumps`. `Decimal` is not JSON-serializable.
- BEFORE storing any value in `checkout_set`, `nps_set`, or any `state_store.*_set`: convert to `float(quantize_money(value))` with the comment `# JSON boundary`.
- Check: search for `state["` + `Decimal` in the same function = bug.

### 2. Tool Use: Validate BEFORE executing
- `_validate_tool_call()` is the barrier between the LLM and the kitchen/DB. Every tool call goes through it.
- `tool_input` MUST be a `dict` (`isinstance` guard). Claude can return SDK objects.
- `qty` MUST be parsed with try/except, defaulting to 1. Claude can return `"two"` or `null`.
- `items` MUST be validated as a `list` before iterating.
- `guests` in reservations MUST be `int > 0`.
- The dedup guard MUST cover `place_order` (the only order-creating tool left — `create_delivery_order`/`create_pickup_order` were removed in chunk 9, docs/claude/delivery-web.md).
- When dedup blocks a call, return a NEUTRAL message ("this is already being processed"), NEVER the LLM's reply (which says "order confirmed").

### 3. Checkout Flow: Complete state machine
- Every checkout step (`asking_split`, `asking_tip`, `asking_tip_custom`, `asking_factura`, `asking_payment_N`, `confirming`) MUST have a branch in `handle_checkout_flow`.
- If a branch is missing, the message falls through to the LLM and checkout is lost.
- `requires_proof` MUST be persisted BEFORE `checkout_set`, not after.
- The `total` check in `_save_checkout_proposal` is the subtotal per check. The payment in `_auto_confirm_checks` MUST include `tip_per_check` in the amount.

### 4. (retired) — was the WhatsApp inbox worker's claim-then-ack. Never hold a DB connection across an LLM call still applies.

### 5. Cart Locks: Ownership required
- `cart_lock_acquire` returns a UUID token. `cart_lock_release` MUST receive that token.
- A release without a token (`token=None`) MUST be rejected (early return + error log).
- ALL functions that use `_cart_lock` MUST handle `RuntimeError("cart_lock_contention")`.
- Fallback cart lock timeout: 5 seconds max (not 30s, it blocks the event loop).

### 6. (retired) — was the Meta webhook contract.

### 7. Rate Limiting: Redis for cross-worker
- Global rate limits (e.g. the diner chat's per-IP / per-token limits) MUST use `state_store.rate_limit_check` (Redis INCR), NOT module-level counters (those are per-worker, not per-platform).
- `rate_limit_check` in Redis: `EXPIRE` is only set when `count == 1` (first request). NEVER reset the TTL on every request.
- In-process fallback: dicts with a size cap of 10K entries. Eviction by oldest timestamp, NOT insertion order.

### 8. LLM: Never leave the customer silent
- `call_claude()` MUST be in a try/except. On failure → friendly reply ("technical issue, please try again").
- Retry: 3 attempts with backoff for transient errors (429, 503, 529, timeout, connection error).
- Empty or None reply from the LLM → fallback "How can I help you?"
- `end_session` blocked (active order/pending bill) → contextual message, NEVER the LLM's farewell.
- `_INJECTION_RE` is evaluated in `_wrap_user_message` BEFORE sending to the LLM. Blocked patterns return an empty string.

### 9. NPS: Race handling and cleanup
- `_handle_nps_flow` can return `None` if the key expired between two reads (multi-worker race). `_try_nps_active_flow` MUST check `if nps_reply is None: return None` before sending the prompt.
- `skip_nps` when the state is `waiting_comment` MUST finalize the pending record (`db_update_nps_comment("No comment")`), not leave it orphaned with `__pending__`.
- `trigger_nps` MUST receive the real `restaurant_name`, not an empty string.

### 10. Concurrency: Always assume 4 workers
- All mutable state (NPS, checkout, cooldowns, cart locks) goes through Redis via `state_store`.
- The in-process fallback is degraded, NOT equivalent. Document the differences.
- `decode_responses=True` on the Redis client → values are `str`, NEVER `bytes`. Don't add defensive `.decode()`.
- `FOR UPDATE SKIP LOCKED` only protects inside a transaction. Once the transaction is released, the row is visible to other workers.
- `asyncio.TimeoutError` is a subclass of `Exception` in Python 3.11+. Catches must use `except (Exception, asyncio.TimeoutError)` for compatibility.

### 11. GPS and Branch Routing
- Coordinates 0,0 are valid (Gulf of Guinea). Use `if lat is None` instead of `if not lat`.
- The bot's tenant key is `org_id` (0098/0099 removed `bot_number` and every WhatsApp column): `agent.chat(user_phone, user_message, org_id, location_id)`; carts, conversations and NPS state are keyed `(phone, org_id)`; the sede is `location_id` / `sede_context`.
- `restaurant_obj` MUST be updated when a branch override happens (don't keep the Matriz/head-office ID).
- `_try_checkout_flow` and `db_save_history` MUST propagate `branch_id` from the table_context.

### 12. find_dish: Safe matching
- Pass 1: exact match, case-insensitive (always).
- Pass 2: substring with a minimum 40% ratio (`query_len / item_len >= 0.4`). WITHOUT the ratio, a 2-letter query matches a 30-letter name.
- `remove_from_cart` MUST verify the item existed before confirming removal.
- `add_to_cart` MUST reject `qty <= 0`.

### 13. Typed errors in the order pipeline
- `InsufficientStockError` → a customer-facing message about stock, NOT silenced with `except Exception`.
- `OrderCommitError` → a customer-facing message about an order error.
- Both MUST be caught BEFORE the generic `except Exception` in `execute_action`.
- `commit_order_transaction` MUST receive the real cart with items, NEVER `cart={}`.

### 14. Tenant scope in bot runtime (RLS Phase 1)
- `routes/diner.py diner_chat` MUST call `agent.chat(...)` inside `with tenant_scope(org_id):` (it does, from the diner session). If you don't, ANY migrated repo blows up with `TenantNotSetError` inside the bot flow. Test harnesses (`tests/e2e/conftest.send_diner_message`, `tests/ai_sim/runner.py`) do the same.
- `scheduler._scheduler_loop` MUST enter `bypass_tenant_scope("scheduler_leader_tick")` before the leader tick, AND wrap each per-restaurant iteration in `tenant_scope(rid)`.
- `agent.py detect_table_context / get_session_state / _handle_nps_guard / _resolve_branch_id` use `_bypass_tenant` (aliased to `bypass_tenant_scope_if_unset`, **soft-bypass**). In production they run inside the `tenant_scope(org_id)` set by `diner_chat`, so the helper is a **no-op** — the query runs under the real scope. Use the strict `bypass_tenant_scope` (not the soft one) only for genuinely cross-tenant cases (internal admin, scheduler leader).
- `orders.py process_order_callback` (Wompi) MUST enter `tenant_scope(order["restaurant_id"])` after loading the order.
- Do NOT go back to "silent fail" in the bot runtime. If a `TenantNotSetError` shows up in production, it's a wiring gap, NOT a case to suppress.

### 15. Order confirmation: broad vocabulary + vowel elongation
- `_CONFIRM_WORDS` in `agent.py` includes `vale, bueno, claro, correcto, bien, excelente, genial, sii/siii/siiii` in addition to the classic set (`sí, ok, dale, perfecto, listo, ...`). FORBIDDEN to trim it "because it looks long" — every word is there because a real user used it and the bot ignored their confirmation.
- `_last_messages_have_confirmation` collapses vowel elongations (`re.sub(r"([aeiou])\1{1,}", r"\1", lowered)`) BEFORE matching: `vaaaaale`→`vale`, `siiii`→`si`. Do NOT remove the normalization — users stretch vowels on WhatsApp constantly.
- The match union includes BOTH forms (lowered + normalized) so as not to lose tokens where the elongation accidentally lands on a valid word.

### 16. Single-location restaurants: don't ask which branch
- `agent.py` injects `[UBICACION_UNICA: ...]` into the context when `db_get_branches` returns an empty list (single-location tenant). This is the explicit counterpart to the `[SUCURSALES: ...]` block used in the multi-location case.
- (Historical: `agent_external.py` STEP 2 had the same `CRITICAL — SINGLE-LOCATION RULE` for the WhatsApp delivery/pickup funnel; that file was deleted in chunk 9 — delivery/pickup ordering moved to the web channel, docs/claude/delivery-web.md.)
- Without this hint the LLM would ask "which branch?" at single-location restaurants when a customer places a dine-in order across multiple sedes. If you touch the system prompt or the context injection, KEEP it.

### 17. Bill request fires waiter_alert instantly (doesn't wait for full checkout)
- The `agent_salon.py` checkout flow calls `db.db_create_waiter_alert(alert_type='bill', ...)` **the moment the customer asks for the bill**, BEFORE the checkout state machine (split → tip → method → factura) finishes.
- Reason: floor staff need to see "table X asked for the bill" on the POS instantly, not after 4 turns of conversation.
- The alert is a **hint, not a commitment**. The actual payment still follows its normal flow — this only notifies.
- A failure creating the alert is logged but does NOT block checkout (best-effort). If you remove the try/except, the bot stops processing checkouts when waiter_alerts is down.
