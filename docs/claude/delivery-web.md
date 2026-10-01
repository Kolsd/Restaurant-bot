# Web delivery wave — Phase A spec (locked 2026-09-17)

> Single source of truth for the delivery/pickup wave. Every agent working on
> this wave MUST read this file plus `rls-multitenant.md` and `bot-rules.md`
> before touching code. The product decisions here are CLOSED — do not
> re-discuss them and do not "improve" them. If something is genuinely
> missing, ask the PM.

## What this wave is

Delivery and pickup move OFF WhatsApp and onto Mesio's own web chat, the same
channel the QR table flow already uses (`/chat/{table_id}` → `diner-chat.html`
→ `/api/diner/*`). WhatsApp delivery is switched off at launch: no restaurant
is live on it, so there is no pilot to protect.

**Phase A (this wave)** — everything below.
**Phase B (NOT now)** — live rider map (Mapbox) and live customer↔cashier chat.
Do not build, stub or scaffold Phase B. A "llamar al restaurante" button covers
the customer's need to reach staff in Phase A.

## Locked product decisions

### Entry and location assignment
- ONE public link per organization: `/pedir/{organizations.slug}`.
  `organizations.slug` already exists (0034, nullable UNIQUE) — backfill it for
  orgs that lack one and guarantee it on signup.
- The page asks for GPS. With a fix, resolve the sede automatically:
  1. nearest sede that is OPEN, has delivery enabled, and whose coverage radius
     contains the point → assign it;
  2. if that sede is closed, try the next sede that is open AND covers the point;
  3. if no sede covers the point, or all of them are closed → offer PICKUP,
     showing the sede list **ordered by distance, nearest first**;
  4. GPS denied or unavailable → pickup only; the sede list is shown without
     distances and the customer picks.
- Coverage is validated on the GPS point against the sede's radius. The typed
  address is guidance for the rider, who also sees the pin. No paid geocoding.
- `restaurant_repo.find_nearest_location(lat, lon, radius_km)` already exists —
  extend or wrap it; do not write a second haversine.

### Per-sede delivery configuration
Today `delivery_fee`, `min_order` and `delivery_radius_km` live in
`organizations.features` (ORG level). This wave needs them PER SEDE.
- Add `locations.delivery_config JSONB NOT NULL DEFAULT '{}'::jsonb`.
- Keys: `delivery_enabled`, `pickup_enabled`, `delivery_fee`, `min_order`,
  `radius_km`, `prep_minutes`, `payment_methods` (list).
- Resolution order: `locations.delivery_config` → `organizations.features`
  (legacy default) → hardcoded default. One helper, used everywhere.
- Money in these configs is `Decimal` through `services/money.py`; `float` only
  at the JSON boundary.
- The restaurant edits this per sede in the locations UI (`/locations`), not in
  the org-wide settings page.

### Ordering UX
- The same chat as the table flow: dish cards with photo and "+", free-text note
  per dish, no priced modifiers. Sold-out dishes render as "agotado".
- The LLM is for conversation only. Address, payment, tip, scheduling and
  confirmation are a DETERMINISTIC form — never LLM-parsed.
- Delivery and pickup are both supported. Scheduled orders: SAME DAY ONLY. A
  customer who wants another day is told to call the restaurant (phone shown).
- Outside opening hours no order can be placed.
- The per-sede minimum order is enforced server-side before the order is created.
- **Sold-out dishes must be refused by the SERVER, not only greyed out in the
  page.** The dine-in flow already blocks them; the web checkout landed in
  chunk 3 without that guard, so a delivery customer can currently order a
  dish that is out of stock. Chunk 4 closes it, and the same chunk decides
  whether a delivery order deducts inventory the way a table order does
  (`orders_repo._deduct_inventory_in_tx`, today reachable only through
  `commit_order_transaction`).

### Identity and anti-abuse
- Reuses `diner_sessions` (`order_mode` = `delivery` | `pickup`, `table_id`
  NULL). Identity stays `web:<uuid4>`, used as the `phone` routing key as today.
- Checkout captures name + phone + address; email is optional. The browser
  remembers name/phone/address in `localStorage` for the next order.
- Cloudflare Turnstile on the checkout submit. `TURNSTILE_SECRET` /
  `TURNSTILE_SITE_KEY` env vars; when unset, verification is skipped (local and
  test) but the rate limits below still apply.
- Rate limits: per IP, per device token, and per customer phone (a cap on
  simultaneously OPEN orders per phone).
- The phone is NOT verified (no OTP). Loyalty points accrue by phone, but there
  is no redemption on the web channel.

### Payment
- Cash on delivery (ask "¿con cuánto pagas?" and store the change-for amount),
  card reader on delivery, and Nequi/Bancolombia transfer with a proof image
  uploaded through the existing `services/image_host.py` (Cloudinary) into
  `orders.proof_url` (0061).
- No online gateway. Wompi stays OFF; Bold is not part of this wave.
- **Tip: an optional field in the web checkout** (suggested amounts), added to
  the total, shown to the rider, and reconciled with the rider's cash at the
  cashier.

### Order lifecycle
`orders.status` already carries the WhatsApp-era vocabulary (`en_preparacion`,
`listo`, `en_camino`, `en_puerta`, `entregado`). Reuse it and add the acceptance
step in front:

```
pendiente_aceptacion → (cashier accepts, sets ETA) → en_preparacion → listo
    → en_camino → entregado
pendiente_aceptacion → rechazado (reason, shown to the customer)
pendiente_aceptacion → cancelado (customer, only before acceptance)
```

- The CASHIER accepts first (availability + a manually typed ETA in minutes).
  Only after acceptance does the ticket reach the kitchen KDS.
- A rejection always carries a reason, and the customer sees it.
- The customer can cancel until the order is accepted, never after.

### Cashier UI
- A NEW "Domicilios" section in the staff app (`/staff`), not a filter inside the
  existing orders view. Section key `delivery`, granted to `caja`/`cashier`/
  `cajero` and to every admin role through `app/services/staff_sections.py`
  (single source of truth — do not re-derive the mapping anywhere else).
- The section shows: the queue waiting to be accepted, accept with ETA, reject
  with reason, assign a rider, the payment proof image, and each order's live
  state.
- Staff only ever see their own sede's orders. `users.location_id` exists (0081)
  and `services/auth.py` already resolves a `default_location_id` — verify it is
  actually enforced on every delivery endpoint instead of assuming it.

### Customer status page
- `/pedido/{public_code}` — a short public code stored on the order, GLOBALLY
  unique (the URL carries no org, so a per-org code could resolve to another
  tenant's order) and not guessable by incrementing. Minted by
  `delivery_repo.db_claim_public_code()`, which relies on the unique index
  with savepoint retry — a pre-check read is blind across tenants under RLS.
- The chat shows the code/link and stores it in `localStorage`. The email
  carrying the link is wired behind `EMAIL_BACKEND` but stays on the `console`
  backend until `RESEND_API_KEY` exists, so the page must be fully usable with
  no email at all.
- The page updates over the existing SSE stream (`app/services/realtime.py`,
  `mesio-realtime.js`), with polling as the 60s net, exactly like the staff app.
- It carries a "llamar al restaurante" button (the sede's phone).
- NPS 1–5 appears on the status page once the order is `entregado`.

### Riders
- The restaurant's own riders, on the existing `courier` section of the staff app
  (`app/static/js/staff/sections/courier.js`). The cashier assigns.
- The rider marks picked up / delivered and reports the cash collected plus the
  tip; the cashier reconciles.
- No rider GPS broadcasting in Phase A — that is the Phase B map.

### WhatsApp removal in this wave — DONE (chunk 9, 2026-09-19)
- Deleted `app/services/agent_external.py` (the whole "external" delivery/
  pickup prompt + tool handlers) and every route into it from
  `app/services/agent.py`: `TOOLS_EXTERNAL`/its 5 order-lifecycle tool defs
  (`create_delivery_order`, `create_pickup_order`, `change_payment_method`,
  `cancel_order`, `notify_arrival`) are gone from `app/services/agent_tools.py`
  too, so the LLM can never again be offered a tool that creates or mutates a
  delivery/pickup order. `TOOLS_SALON` is now the only tool list.
- A WhatsApp customer with no active table session who ASKS FOR DELIVERY OR
  PICKUP (`agent.py::_is_delivery_intent`, a plain keyword match) gets a
  deterministic, no-LLM reply pointing at `/pedir/{organizations.slug}`
  (`agent.py::_whatsapp_no_table_reply`) — or, if the org has no slug yet, a
  reply telling them to call the restaurant.
  The gate is deliberately narrow: it first deflected EVERY table-less
  WhatsApp message, which also killed reservations (a customer books BEFORE
  arriving, so a booking always comes from a phone with no table session) and
  every ordinary question. Those still reach the LLM, which keeps
  `TOOLS_SALON` — the guarantee that WhatsApp cannot create a delivery order
  is structural (no such tool exists any more), not a matter of who answers.
  For delivery wording the keyword list misses, `build_system_prompt` adds a
  `[PEDIDOS_A_DOMICILIO_Y_RECOGER]` block with the link so the model hands it
  over instead of offering to take the order.
  The web ordering chat (order_mode delivery/pickup, also table_context None)
  is excluded from the gate by the `web:` prefix check.
- The `delivery` and `pickup` AI-sim suites and the two `mode="external"`
  adversarial scenarios were retired with the funnel (`tests/ai_sim/`): they
  drove the deleted tools and would only burn LLM credit. 9 scenarios remain.
- **Deleted `GET /api/delivery/orders`** (`app/routes/orders_routes.py`) and
  its now-dead siblings that only ever served the WhatsApp flow: `GET
  /api/delivery/check-updates`, `POST /api/delivery/orders/{id}/validate`,
  `POST /api/delivery/orders/{id}/eta`, `PATCH /api/delivery/orders/{id}/status`,
  plus `send_delivery_notification()` (WhatsApp push on status change).
  `orders_repo.db_get_delivery_orders` / `tables_repo.db_get_delivery_status_
  hash_for_restaurant` (the repo functions those routes called) have no
  remaining caller either, but were left in place — both have their own
  direct Wave-2 org_id regression tests (`tests/test_wave2_org_id_join_fix.py`)
  independent of the route, and `db_set_order_eta` (behind the deleted `/eta`
  route) similarly stays for `tests/test_delivery_eta.py`.
  `app/static/js/staff/sections/cashier.js` still had a duplicate "Para
  Recoger"/"Domicilios" tab pair calling these routes (pre-dating the
  dedicated `delivery.js` "Domicilios" section from chunks 5-8) — removed;
  `courier.js` still had a legacy fallback branch and hash-polling call —
  removed, `fetchOrders()` already only reads `/api/staff/delivery/orders/mine`.
  **Gap found while doing this, CLOSED 2026-09-20:** the sede-scoped API had
  no equivalent to the old manual "validate Nequi/transfer proof → paid=true"
  action. It was worse than the transfer case: `orders.paid` was written by
  `orders_repo.db_confirm_payment` alone, whose only caller is the switched-off
  Wompi webhook, so cash at the door and the rider's card reader never marked
  an order paid either — and `stats_repo` sums `orders.total WHERE paid=TRUE`,
  so no delivery sale appeared in the owner's totals at all.
  Now: `POST /api/staff/delivery/orders/{id}/mark-paid` with
  `{payment_method}` (one of `app/services/delivery.ALLOWED_PAYMENT_METHODS`),
  backed by `delivery_repo.db_mark_order_paid`. Same authorization as the
  en-route/delivered transitions — the sede's cashier or an admin for any
  order, plus the order's OWN assigned courier. The conditional UPDATE
  (`paid = FALSE AND status NOT IN (cancelado, rechazado)`) makes it
  idempotent, so a second submit gets a 409 instead of overwriting who
  collected the money; migration 0089 adds `orders.paid_by_staff_id`.
  UI: a method select + "Registrar pago" on the cashier's Domicilios card,
  and "💵 Cobré efectivo" / "💳 Cobré con tarjeta" on the courier screen —
  a transfer is confirmed against the bank by the cashier, never from a
  phone at someone's door. `/pedido/{code}` now says
  "Pago: nequi · confirmado / pendiente de confirmar".
- Defensive fix while auditing "never message a `web:` identity over
  WhatsApp": `orders_repo.db_get_orders_needing_eta_communication` (feeds
  `scheduler.py`'s WhatsApp ETA push, still alive for phone-based orders) now
  excludes `phone LIKE 'web:%'` rows. Now that the gap above is closed and web
  orders DO reach `paid=TRUE`, this exclusion is what actually stops the ETA
  scheduler from handing a synthetic `web:<uuid>` identity to Meta's send API.

### What remains for the full WhatsApp shutdown (CLAUDE.md item 6)
Only delivery/pickup was retired in chunk 9. Still live and untouched:
- The Meta/Twilio webhooks (`app/routes/chat.py`) and the salon (dine-in)
  WhatsApp flow (`agent_salon.py`, `TOOLS_SALON`) — a customer who scanned a
  table QR still orders, pays and reserves over WhatsApp exactly as before.
- `db_attach_order_proof` / the chat.py photo-proof handler — now effectively
  dead in practice for NEW orders (no WhatsApp order can exist to attach a
  proof to any more) but left in place; it's a small, generic, still-tested
  repo helper, not something this chunk was asked to remove.
- `tests/e2e` still drives the salon flow via `simulate_whatsapp_inbound` —
  per the standing rule, do not delete or touch the Meta/Twilio webhooks or
  the salon flow until that harness has an equivalent web-channel path.
- Reservation-without-a-table over WhatsApp is gone as a side effect of
  deleting `agent_external.py` (it was the only place that let a table-less
  WhatsApp customer call `make_reservation`) — dine-in reservations
  (`table_context` present) are unaffected; this is a product-visible
  change beyond delivery/pickup, called out for the PM here rather than
  decided unilaterally.

### Known open items found during verification (not yet fixed)
- ~~The kitchen KDS delivery feed is scoped by org only~~ — closed in chunk 8
  (`_kitchen_delivery_location_filter`), and the same rule was swept across
  every other staff-facing listing on 2026-09-20. The canonical resolver is
  `app/routes/deps.py::resolve_sede_filter`; see the "Sede scoping" section
  of `docs/claude/rls-multitenant.md` before adding any new listing.
- ~~Multi-sede orgs have no UI to assign a sede to staff~~ — done in chunk 8
  (team invite/edit sede selector, "Sin sede" badge); ~~kitchen feed not
  sede-scoped~~ — done in chunk 8.
- ~~`/api/staff` gated by `staff_tips`~~ — **PM decision 2026-09-20: the
  roster ships on every plan.** `staff_tips` ("Staff & Propinas") now covers
  only what it is sold as — shifts, tips, payroll, schedules, deductions,
  contracts, attendance. List/create/update/delete staff and `/api/staff/locations`
  carry `require_auth` alone, because a restaurant cannot operate without
  creating a mesero, a cajero or a domiciliario and orgs are created with
  `features = {}`.
- ~~per-sede delivery config is owner/admin only~~ — **PM decision 2026-09-20:
  `gerente` added, scoped to their OWN sede** (owner/admin keep every sede in
  the org; a gerente with no `location_id` is refused, never defaulted).
  `app/routes/location_delivery.py` now reuses
  `staff_sections.ADMIN_ROLES` instead of its own narrower set — `owner` and
  `admin` are the same authorization level product-wide.
- Do NOT touch the Meta/Twilio webhooks or the salon WhatsApp flow yet: those are
  removed in the next step, only AFTER `tests/e2e` is migrated to the web
  channel. Never delete before the equivalent harness exists.

## Non-negotiable engineering rules for this wave
- SQL only in `app/repositories/`, `$n` parameters, never an f-string with values.
- `async with tenant_connection()` + `tenant_scope(org_id)`. The public ordering
  endpoints know the slug or the token before they know the org — resolve the org
  first under `bypass_tenant_scope("reason of 8+ chars")`, exactly like
  `diner_sessions_repo.get_by_token()`, then scope everything else.
- A new tenant table means RLS ENABLE + FORCE in the migration.
- `org_id` and `location_id` are distinct integers. Never fall back between them.
- Money is `Decimal`; never a `Decimal` inside `state_store`.
- Mutable cross-worker state goes through `state_store` (Redis), never process
  memory — production runs 4 workers.
- `get_logger(__name__)`, typed excepts, `mask_phone()` in logs, no `print`.
- Frontend: `textContent` for customer data, `mesioHeaders()` / `_staffFetch`,
  and any commit touching `app/static/**` bumps `CACHE_VERSION` in `sw.js` in
  that same commit. Run `python scripts/lint_frontend.py`.
- Alembic: revision id ≤ 32 chars, `sa.text()` + `CAST(:p AS type)`,
  `IF NOT EXISTS`, and a single head afterwards.
- Tests must be truthful: never `status_code == 200` as the only assertion, never
  mock a whole repo, no silently skipped tests, dates in UTC. Tenant tests seed
  ids that collide on purpose.
