# Current state, closed product decisions and next session

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## ▶ CURRENT STATE AND NEXT SESSION — read first (updated 2026-09-12)

### What happened (sessions 2026-09-10 → 12, 20 commits `0ab1641..ad8dca1`)
- **The suite was lying.** The "1368 green tests" came from a mocked subset: `alembic upgrade head` didn't build a DB from scratch, nobody ran the tests with a real DB, and 260 were silently skipped. Fixed: the migration chain builds from empty (0079), the DB tests run and pass, test dependencies are pinned.
- **Product pivot: own web channel instead of WhatsApp.** The bot is still the product (chat-first). QR → `/chat/{table_id}` → the bot presents the menu as cards.
- **Full table flow, no WhatsApp and no LLM:** scanning opens the table and shows a code → friends join with that code → deterministic cart with a per-dish note → "Send order" reaches the kitchen with the note on the ticket → table view ("You" / "Another diner") → "Ask for the bill" (mine / the whole table, card / cash) → the waiter charges on their card reader → cashier `pay_check` → NPS from 1 to 5 in the chat.
- **P0s closed:** table checkout always returned 500; `db_get_restaurant_by_id` returned ANOTHER restaurant when a location id collided with an organization id (removed; `users.org_id`/`location_id` in 0081); the superadmin was writing to the wrong customer (legacy endpoints removed); "dismiss alert" issued a DELETE; stock control never worked (double `json.dumps`); five endpoints with RLS wired wrong; three tests that failed only at night because of timezone. The flagship e2e test is no longer flaky: its "flakiness" was the id collision.
- **Transactional email** (`EMAIL_BACKEND=console|resend`) for password reset, weekly report and CRM welcome, so WhatsApp can be retired without locking out owners.

### Closed product decisions (do not re-discuss)
1. Anonymous diner (`web:<uuid4>`); name and phone optional, only when paying.
2. The order goes straight to the kitchen; the waiter gets notified, doesn't approve.
3. Floating waiter button, always visible, that asks the reason (bill / cutlery / napkins / something else).
4. Chat-first: the bot presents the menu as cards with photo and "+"; the first message greets and shows categories; "View full menu" opens a panel.
5. Free-text note per dish, no priced modifiers.
6. Shared table: whoever arrives later joins with the code the first person sees.
7. Checkout at launch BY THE WAITER, no gateway: "pay mine" or "pay for the whole table" (= remaining balance).
8. NPS at the end of service (at checkout), inside the chat.
9. Wompi OFF (webhook broken, see Wompi section). Bold deferred as the first gateway (see Bold section).
10. WhatsApp is being retired. Web push is out of scope until delivery is built.
11. The superadmin edits only business (organization) data; each location's data is edited by the restaurant.
12. Sales hook = free days on top of the paid plan using `comp_until` (NOT a `plan_code='free'`). **14 days since 2026-09-23** — it was 8 in code while the landing page promised 14 in five places, so the product could not keep the offer it advertised. The number lives in `services/provisioning.DEFAULT_TRIAL_DAYS`; change it there and nowhere else.
14. Go-to-market, four calls (2026-09-23). **(a) Self-serve signup**: a restaurant creates its own account and operates without anyone at Mesio touching anything — the CRM records conversions, it does not cause them. **(b) Colombia only, country-neutral schema**: keep selling in Colombia, but stop hardcoding it (`country`/`currency`/`locale`/`timezone` per org); no CFDI/SUNAT, no Mercado Pago, no Portuguese until there is demand. **(c) Billing manual, state internal**: Mesio invoices outside the product, but the subscription state (`trial → activo → vencido → suspendido`) and the cut-off live inside it, so switching to automatic collection later only changes who writes that state. **(d) Flat price per sede**, replacing the conversation caps — with an internal soft ceiling that alerts Mesio when a tenant's LLM cost crosses a share of its price, never one that cuts the bot off mid-service.
15. Pricing (2026-09-30), flat per sede, COP, no commission: **Esencial $119k** (QR table ordering without AI, KDS, caja, 5 users), **Restaurante $249k** (+ AI assistant, own delivery/pickup link, NPS, unlimited users; the 15-day trial is this plan), **Pro $349k** (+ reservations, auto-deducting inventory, DIAN with folios billed apart at ~$500-1000 each), **Cadena $299k/sede** from 3 sedes. Annual = pay 10, get 12. **Founder program**: first 10 restaurants pay 40% off list, frozen for life while the subscription stays active. The backend still has the 0070 plans (pulso/restaurante/pro/cadena at 149/299/549/899k with conversation caps): renaming pulso→esencial, the new prices, per-plan feature gating and a per-org founder price are the next plan wave. VAT on cloud services (art. 476 ET) is pending the accountant.
13. Each sede is its own restaurant (2026-09-20). The carta is the org's plus per-sede overrides: own price, hidden dish, own dishes. The gerente edits their own sede's; a sede price survives base-price changes. `/pedir/{slug}` stays one link per org and the sede picked by GPS decides the carta (2026-09-21, migration 0093; `rls-multitenant.md`).

### Next session — in this order
1. **Validate the bot with the real LLM — blocking.** Everything verified ran WITHOUT `ANTHROPIC_API_KEY`. The PM loads the key; run the full `pytest tests/e2e` (44 tests with LLM) and `python run_ai_sim.py` (~$2-5). Check: the `add_to_cart` tool creates lines with `line_id`, the cart enters context with sanitized notes, NPS and checkout written via web chat.
2. **Real time:** SSE (+ Redis pub/sub, for the 4 workers) in kitchen / bar / waiter / cashier and in diner state; sound in the KDS. Today everything is 6-30s polling.
3. **First customer:** 8-day trial (`comp_until`) at CRM signup; verify signup leaves menu, tables, QR and staff ready to operate.
4. **Multi-location:** the waiter screen doesn't filter alerts by location (staff login doesn't store `location_id`). Mandatory before selling to chains.
5. Sweep the `json.dumps()` passed to `$n::jsonb` pattern (double encoding) in the rest of the code.
6. Turn off WhatsApp: first migrate `tests/e2e/conftest.py` and `test_happy_path_full_flow` to the web channel; never delete before having the equivalent harness.
7. Before reactivating Wompi: fix the webhook (Wompi section).
- **Ops (Railway):** confirm `WOMPI_*` are NOT set, that `REDIS_URL` and `DATABASE_URL_ADMIN` ARE set, and apply `alembic upgrade head` (0079-0081).

### Verified state at close
- Head `0081_users_org_location`, a single head, builds from an empty DB.
- `pytest tests/ --ignore=tests/e2e --ignore=tests/ai_sim`: **1741 passed / 0 failed** with DB · **1400 passed** without DB.
- `pytest tests/e2e -m e2e_no_llm`: **15/15**, 11 clean runs in a row. `scripts/lint_frontend.py`: 0 violations. `sw.js` `CACHE_VERSION = 'v45'`.

### Local environment (Windows)
- Python 3.12 in `.venv` · local Postgres 16 (superuser `postgres` / `mesio_local_dev`; role `mesio_app` / `mesio_app_pw`) · no Redis (in-process fallback).
- DBs: `mesio_tests` (suite), `mesio_test` (e2e), `mesio_fresh` (isolated scratch). All with `ALTER DATABASE <db> SET timezone TO 'UTC'` — mandatory.
- Mandatory pins: `pytest==8.4.2`, `pytest-asyncio==0.24.0` (1.x breaks ~94 tests with "coroutine was never awaited").
- To run tests: `TEST_DATABASE_URL`, `DATABASE_URL` and `DATABASE_URL_ADMIN` pointing at the DB; `DISABLE_META_SIGNATURE_VERIFY=1` for e2e.
- Local server: `uvicorn` is not on the PATH and `.claude/launch.json` is a versioned file with 3 configurations — do NOT overwrite it; use your own launcher that sets `DATABASE_URL`.

### Rules learned in these sessions
- **`CREATE TABLE IF NOT EXISTS` after a stub is a silent no-op** (found 2026-09-23). 0012 created a 4-column `prospects` stub just to satisfy an FK; 0020's full CRM body never ran on any DB built from empty, so the whole CRM was broken and `/api/signup` 500'd on every lead. Later `ALTER TABLE ... ADD COLUMN` migrations DID apply, which is the fingerprint: the patches are there, the table body is not. Repaired by 0095. When a migration must create a table another one may have stubbed, add the columns explicitly — never assume the `CREATE` ran.
- **A silently-skipped test is a test that lies.** Tests with DB must run against a real DB.
- **Organization/location id collision:** never pass an id of ambiguous type to a lookup. Every tenant test must seed ids that collide ON PURPOSE; previous tests only passed because they didn't collide.
- **Gateway signatures:** test against the documentation or a real event, never against a signature generated by our own code.
- **Commit with static files → bump `sw.js`'s `CACHE_VERSION` in that same commit**, and run `test_sw_cache_version` after committing (it only looks at the latest commit).
- **Dates in tests:** always compare on the same clock (UTC). `date.today()` vs. `utcnow()` broke three tests after 19:00.
- **Agents:** give them the why, require browser and DB verification, forbid weakening tests, and verify every report before committing — several initial diagnoses (both mine and agents') were wrong.
