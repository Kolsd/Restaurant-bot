# Bot rules — DO NOT BREAK (17 rules)

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Bot Rules — DO NOT BREAK (learned from 79 bugs across 4 audits)

These rules protect the WhatsApp bot's critical flows. Any change to `agent.py`, `agent_salon.py`, `agent_external.py`, `orders.py`, `orders_repo.py`, `inbox_worker.py`, `state_store.py`, or `chat.py` MUST comply with ALL of these rules.

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
- The dedup guard MUST cover `place_order`, `create_delivery_order` AND `create_pickup_order`.
- When dedup blocks a call, return a NEUTRAL message ("this is already being processed"), NEVER the LLM's reply (which says "order confirmed").

### 3. Checkout Flow: Complete state machine
- Every checkout step (`asking_split`, `asking_tip`, `asking_tip_custom`, `asking_factura`, `asking_payment_N`, `confirming`) MUST have a branch in `handle_checkout_flow`.
- If a branch is missing, the message falls through to the LLM and checkout is lost.
- `requires_proof` MUST be persisted BEFORE `checkout_set`, not after.
- The `total` check in `_save_checkout_proposal` is the subtotal per check. The payment in `_auto_confirm_checks` MUST include `tip_per_check` in the amount.

### 4. DB connections: NEVER hold during dispatch
- The inbox worker uses a claim-then-ack pattern in 3 phases:
  1. **Claim** (ms): `fetch_batch` + `claim_rows` inside a short transaction → release the connection
  2. **Dispatch** (up to 120s): no open DB connection, `asyncio.wait_for(timeout=120)`
  3. **Ack** (ms): a new short connection for `mark_processed` or `mark_failed`
- FORBIDDEN to put the dispatch inside an `async with conn.transaction()`. This causes pool deadlock under load.
- If `mark_processed` or `mark_failed` fail in phase 3, log and continue — the row will be retried when the claim expires (3 min).

### 5. Cart Locks: Ownership required
- `cart_lock_acquire` returns a UUID token. `cart_lock_release` MUST receive that token.
- A release without a token (`token=None`) MUST be rejected (early return + error log).
- ALL functions that use `_cart_lock` MUST handle `RuntimeError("cart_lock_contention")`.
- `migrate_cart` MUST lock BOTH bot_numbers (source AND destination) in a deterministic order to avoid deadlock.
- Fallback cart lock timeout: 5 seconds max (not 30s, it blocks the event loop).

### 6. Meta Webhook: Never lose messages
- Returning 200 to Meta = "message received, don't resend". Returning 503 = "resend the whole batch".
- If `enqueue` fails for ONE message in the batch, do NOT return 503 immediately — process the rest and return 503 at the end.
- `changes: []` or `messages: []` (empty list, not a missing key) MUST be handled with `if not list: continue`, not with direct `[0]` indexing.
- Invalid Meta signature → return 200 (not 401). 401 causes an infinite retry flood.
- Messages without a `wam_id` MUST get a synthetic `external_id` (`synth_sha256(phone:text:bot:epoch//10)`) so the dedup index works.
- NEVER store Meta's `access_token` in the inbox payload. The token is looked up from the DB at dispatch time.

### 7. Rate Limiting: Redis for cross-worker
- Global rate limits (webhook flood) MUST use `state_store.rate_limit_check` (Redis INCR), NOT module-level counters (those are per-worker, not per-platform).
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
- A branch with `whatsapp_number = NULL` → falls back to the parent's number.
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
- `inbox_worker._handle_meta_whatsapp` MUST wrap `_process_message(...)` in `with tenant_scope(_tenant_id):` once the restaurant has been resolved from `bot_number`. If you don't, ANY migrated repo blows up with `TenantNotSetError` inside the bot flow.
- `scheduler._scheduler_loop` MUST enter `bypass_tenant_scope("scheduler_leader_tick")` before the leader tick, AND wrap each per-restaurant iteration in `tenant_scope(rid)`.
- `chat.py meta_webhook` MUST enter `bypass_tenant_scope("webhook_enqueue_cross_tenant")` during enqueue (pre-resolution).
- `agent.py detect_table_context / get_session_state / _handle_nps_guard / _resolve_branch_id` use `_bypass_tenant` (aliased to `bypass_tenant_scope_if_unset`, **soft-bypass**). In production they run inside the `tenant_scope(rid)` set by `inbox_worker`, so the helper is a **no-op** — the query runs under the real scope. In legacy call sites (internal `/chat` POST endpoint, Twilio webhook) without a prior scope, it does enter a real bypass to preserve compatibility. Use the strict `bypass_tenant_scope` (not the soft one) only for genuinely cross-tenant cases (internal admin, scheduler leader, inbox pre-resolution).
- `orders.py process_order_callback` (Wompi) MUST enter `tenant_scope(order["restaurant_id"])` after loading the order.
- Do NOT go back to "silent fail" in the bot runtime. If a `TenantNotSetError` shows up in production, it's a wiring gap, NOT a case to suppress.

### 15. Order confirmation: broad vocabulary + vowel elongation
- `_CONFIRM_WORDS` in `agent.py` includes `vale, bueno, claro, correcto, bien, excelente, genial, sii/siii/siiii` in addition to the classic set (`sí, ok, dale, perfecto, listo, ...`). FORBIDDEN to trim it "because it looks long" — every word is there because a real user used it and the bot ignored their confirmation.
- `_last_messages_have_confirmation` collapses vowel elongations (`re.sub(r"([aeiou])\1{1,}", r"\1", lowered)`) BEFORE matching: `vaaaaale`→`vale`, `siiii`→`si`. Do NOT remove the normalization — users stretch vowels on WhatsApp constantly.
- The match union includes BOTH forms (lowered + normalized) so as not to lose tokens where the elongation accidentally lands on a valid word.

### 16. Single-location restaurants: don't ask which branch
- `agent.py` injects `[UBICACION_UNICA: ...]` into the context when `db_get_branches` returns an empty list (single-location tenant). This is the explicit counterpart to the `[SUCURSALES: ...]` block used in the multi-location case.
- `agent_external.py` STEP 2 has a `CRITICAL — SINGLE-LOCATION RULE` that says "if there is NO [SUCURSALES] block, there is only ONE location; NEVER ask which branch".
- Without these hints the LLM would ask "which branch?" at single-location restaurants, breaking the pickup/delivery flow. If you touch the system prompt or the context injection, KEEP both hints.

### 17. Bill request fires waiter_alert instantly (doesn't wait for full checkout)
- The `agent_salon.py` checkout flow calls `db.db_create_waiter_alert(alert_type='bill', ...)` **the moment the customer asks for the bill**, BEFORE the checkout state machine (split → tip → method → factura) finishes.
- Reason: floor staff need to see "table X asked for the bill" on the POS instantly, not after 4 turns of conversation.
- The alert is a **hint, not a commitment**. The actual payment still follows its normal flow — this only notifies.
- A failure creating the alert is logged but does NOT block checkout (best-effort). If you remove the try/except, the bot stops processing checkouts when waiter_alerts is down.
