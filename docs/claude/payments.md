# Payments: per-restaurant Wompi (webhook broken) and Bold

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Per-restaurant Wompi configuration

Each restaurant stores its own Wompi credentials in `organizations.features.wompi`:

```json
{
  "wompi": {
    "public_key": "pub_test_... or pub_prod_...",
    "integrity_secret": "test_integrity_... or prod_integrity_..."
  }
}
```

**Why per-restaurant:** each customer has their own Wompi account (their merchant id, their tax id, their own bank payout). It makes no sense for the whole platform to charge into Mesio's account. Multi-tenant SaaS: each tenant configures its own keys.

**How it's configured:** the admin/owner opens `/settings → Payment methods → Wompi card reader`, toggles Wompi on, and the credentials block appears. They paste the `public_key` and `integrity_secret` from their Wompi dashboard (Settings → API Keys). Save.

**Secret security:**
- `GET /api/settings` returns `wompi.integrity_secret_set: bool` and `wompi.integrity_secret_last4: str` — the plaintext NEVER leaves the server.
- In the form, the input is type="password" with placeholder `•••• •••• •••• {last4}`. The admin only sees the plaintext at the moment they type it.
- `POST /api/settings`: if `integrity_secret` comes in empty or as a masked string (`xxxx_*`), the backend preserves the existing value (the admin can update just the `public_key` without re-typing the secret). If a different value comes in, it replaces it.

**Runtime resolution:**
- `app/services/orders.py::_wompi_credentials_from_restaurant(restaurant)` extracts the `(pk, integrity_secret)` pair from `restaurant.features.wompi`. Accepts `features` as either a dict or a JSON string (asyncpg variability).
- `generate_wompi_payment_link(order_id, amount, currency, public_key=None, integrity_secret=None)`: if the kwargs are set it uses them; if they come as None, it falls back to the `WOMPI_PUBLIC_KEY` / `WOMPI_INTEGRITY_SECRET` env vars (legacy fallback). Each credential is evaluated independently.
- `generate_deposit_link(reservation_id, amount, currency, restaurant=None)`: same pattern for reservation deposits.
- If neither the restaurant config nor env vars provide `integrity_secret` → `RuntimeError`. The bot call sites (`create_order`, `agent.py reserve flow`) catch that error and fall back to the manual proof-of-payment flow (Nequi/Bancolombia).

**Migration:** the `WOMPI_PUBLIC_KEY` and `WOMPI_INTEGRITY_SECRET` env vars remain valid as a fallback during the transition. They can be removed from Railway once ALL active restaurants have configured their credentials in `/settings`. Until then, restaurants without explicit config will keep charging into the global account.

### ⚠️ Status (2026-09-11): Wompi OFF at launch + broken webhook (pending P0)

**PM decision:** Wompi stays OFF at the start. The PM has no RUT (Colombian tax id) to open an account (not even sandbox). Remember that in the per-restaurant model **the merchant is each restaurant** (their RUT, their account, their keys), not Mesio — the PM's missing RUT blocks *testing*, not the product. Checkout at launch is card-reader/cash via the waiter ("bring the card reader"). Bold is the alternative under evaluation (many restaurants already have their own Bold portal/QR).

**While Wompi is OFF:** confirm in Railway that `WOMPI_PUBLIC_KEY` / `WOMPI_INTEGRITY_SECRET` are NOT set. If they are, the bot generates payment links against the global account, the customer pays, and the payment never confirms (see bug below) — the customer paid and the restaurant never finds out.

**Webhook P0 bug — fix BEFORE reactivating Wompi.** `POST /payment/wompi-webhook` (`app/routes/orders_routes.py`) can never verify a real Wompi event:
1. **Wrong formula.** The code computes `sha256(raw_body + WOMPI_EVENTS_SECRET)`. Wompi signs with the SHA256 of: the values of the fields listed in `signature.properties` (paths like `transaction.id` inside `data`, in that order) + `timestamp` + the merchant's **events secret**; it's compared against `signature.checksum` (also present in the `X-Event-Checksum` header). Logical proof: the event body *contains* `signature.checksum`, so it can't be the hash of the body. Confirmed by the official docs (https://docs.wompi.co/en/docs/colombia/eventos/) and by a third-party integration in production.
2. **Single global secret.** `WOMPI_EVENTS_SECRET` is one env var, but the events secret is **per merchant account**. `features.wompi` only stores `public_key` + `integrity_secret`.
3. **Why the tests pass:** `tests/e2e/test_delivery_wompi_callback_lifecycle.py` signs its payloads with the same wrong formula — the code is tested against itself.

**Fix specification:** implement the documented algorithm iterating over the payload's `signature.properties` for each event (the docs warn the property list changes: NEVER hardcode it); add `events_secret` to `features.wompi` (masked in `GET /api/settings` the same way as `integrity_secret`); resolve the event's restaurant via `data.transaction.reference` → order / table check / deposit `dep_` → org, verify with that org's secret and fall back to the global env only as a last resort; don't act on the payload before verifying; rewrite the e2e signer with the real algorithm. **The docs' worked example does NOT work as a test anchor**: its checksum (`3476DDA5…`) is decorative — the documented concatenation yields `5A18EC5E…` and no alternative ordering reproduces it. The real anchor is capturing a real event from a sandbox account (requires some test merchant's RUT).

### Bold — evaluated 2026-09-11, DEFERRED (first gateway to integrate once gateway-based checkout is turned on)

**PM decision:** at launch, table checkout goes through the waiter, with NO gateway (the diner picks "mine / the whole table" and "card / cash" in the chat; the waiter gets the exact amount, charges it on whatever card reader the restaurant has, and the cashier marks it paid via `pay_check`). Bold is the gateway planned for later because many restaurants already have their own card readers/QR. Same as Wompi: the merchant is each restaurant with its own account and keys.

- **Keys** (per merchant, test and production versions): "identity key" (public, header `Authorization: x-api-key <identity_key>`) and "secret key" (private). Panel at bold.co → Integrations → Integration keys. TEST keys only appear after Bold approves a request; there's no self-service sandbox, and the docs don't say whether they accept an individual without a RUT.
- **Push to the card reader — the best fit for "bring the card reader"** (https://developers.bold.co/api-integrations/integration): base `https://integrations.api.bold.co`; `GET /payments/payment-methods`, `GET /payments/binded-terminals`, `POST /payments/app-checkout` with `amount{currency,total_amount,taxes,tip_amount}`, `payment_method`, `terminal_model`, `terminal_serial`, `reference`, `user_email`; the result comes via webhook. Only Smart / Smart Pro card readers enabled under "API Connections". The sandbox requires a physical SmartPro and simulates results by amount (111.111 insufficient funds, 222.222 invalid PIN, 999.999 general decline).
- **Payment link + Bre-B QR** (https://developers.bold.co/pagos-en-linea/api-link-de-pagos): `POST /online/link/v1` (`amount_type` CLOSE, `amount`, `reference` ≤60, `expiration_date` in Unix nanoseconds, `callback_url`) → `payment_link` + `url`. `GET /online/link/v1/{payment_link}` returns the `status` (ACTIVE/PROCESSING/PAID/REJECTED/CANCELLED/EXPIRED): useful as a backup if a webhook is lost.
- **Webhooks** (https://developers.bold.co/webhook): header `x-bold-signature` = HMAC-SHA256 hex of the raw body **Base64-encoded**, with the merchant's secret key; in test mode the key is EMPTY. Events SALE_APPROVED / SALE_REJECTED / VOID_APPROVED / VOID_REJECTED; identify the payment by `metadata.reference`. Resolve the restaurant from the reference before verifying, and don't act on anything without verifying. Lesson from Wompi: anchor the test on a REAL event captured from the sandbox, never just signatures generated by our own code.
