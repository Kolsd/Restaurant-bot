import contextlib
import hashlib
import json
import re
import secrets
import uuid
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from app.services import database as db
from app.services import state_store
from app.services.logging import get_logger
from app.services.money import to_decimal, money_mul, money_sum, ZERO

APP_DOMAIN = os.getenv("APP_DOMAIN", "")

# NOTE: env-var Wompi credentials are LEGACY/FALLBACK. The canonical path is
# per-restaurant configuration via `features.wompi = {public_key, integrity_secret}`
# resolved by `_wompi_credentials_from_restaurant()`. Env vars exist solely so
# legacy deploys keep working until every restaurant has migrated its keys.
WOMPI_PUBLIC_KEY = os.getenv("WOMPI_PUBLIC_KEY")
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET")

log = get_logger(__name__)

# ── Cart line model (Mesio-native diner chat — direct tap mutations) ────────
# Every cart line carries a stable `line_id` so the diner UI can target one
# specific line (change qty / edit note / remove) without depending on dish
# name, which is no longer unique per cart once per-item notes exist (see
# _notes_equal below — same dish + different note = a NEW line).
_MAX_NOTE_LEN = 140
_MAX_CART_QTY = 50
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _new_line_id() -> str:
    return secrets.token_hex(5)


def _normalize_note_for_storage(note) -> str:
    """Strip control chars, collapse whitespace, cap length. Diner-supplied
    free text (a per-item note) reaches the kitchen/waiter screens, the
    diner's own screen, and the LLM prompt — this is the storage-time layer
    of defense; _sanitize_note_for_llm_context (below) is the render-time
    layer for the LLM path specifically."""
    if not note or not isinstance(note, str):
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", note)
    cleaned = " ".join(cleaned.split())  # collapse whitespace, strip
    return cleaned[:_MAX_NOTE_LEN]


def _notes_equal(a, b) -> bool:
    """Two notes are 'the same' for cart-line merge purposes after
    normalizing (strip/collapse/cap) and case-insensitive compare. Empty and
    missing are equal."""
    return _normalize_note_for_storage(a).lower() == _normalize_note_for_storage(b).lower()


def _sanitize_note_for_llm_context(note: str) -> str:
    """Cart item notes flow into the LLM context via cart_summary() → the
    [CARRITO: ...] block injected in agent._build_enriched_user_message.
    Reuse agent.py's injection-pattern guard (the exact check
    _wrap_user_message applies to the raw WhatsApp/chat text) so a note like
    'ignora las instrucciones anteriores' cannot hijack the bot. Returns ""
    (dropped) when the pattern matches — mirrors _wrap_user_message's own
    fail-closed behaviour.

    Lazy import: agent.py imports this module at module scope, so importing
    agent.py here at module scope would be circular.
    """
    try:
        from app.services.agent import _INJECTION_RE, _normalize_for_injection_check  # noqa: PLC0415
    except Exception:
        log.exception("orders.note_sanitizer_import_failed")
        return note
    normalized = _normalize_for_injection_check(note)
    if _INJECTION_RE.search(normalized):
        log.warning("cart.note_injection_blocked")
        return ""
    return note


def _ensure_line_ids(cart: dict) -> bool:
    """Assign a `line_id` to any cart item missing one (legacy carts, or
    items created via the LLM path before this field existed). Mutates
    `cart` in place. Returns True if anything changed."""
    changed = False
    for item in cart.get("items", []) or []:
        if isinstance(item, dict) and not item.get("line_id"):
            item["line_id"] = _new_line_id()
            changed = True
    return changed


def _wompi_credentials_from_restaurant(
    restaurant: dict | None,
) -> tuple[str | None, str | None]:
    """Extract (public_key, integrity_secret) from `restaurant.features.wompi`.

    Returns (None, None) if features.wompi is missing entirely. If only one of
    the two values is set, returns that one and None for the other — each
    credential falls back to its env var independently. The integrity_secret
    is NEVER logged here (it is a secret).
    """
    if not restaurant:
        return (None, None)
    feats = restaurant.get("features") or {}
    if isinstance(feats, str):
        try:
            feats = json.loads(feats)
        except Exception:
            return (None, None)
    if not isinstance(feats, dict):
        return (None, None)
    wompi_cfg = feats.get("wompi") or {}
    if not isinstance(wompi_cfg, dict):
        return (None, None)
    pk = (wompi_cfg.get("public_key") or "").strip() or None
    integrity = (wompi_cfg.get("integrity_secret") or "").strip() or None
    return (pk, integrity)


@contextlib.asynccontextmanager
async def _cart_lock(phone: str, bot_number: str, ttl_seconds: int = 30):
    """
    Async context manager that acquires a distributed cart lock for (phone, bot_number).
    Uses Redis SET NX EX when available, falls back to an asyncio.Lock per worker.
    Raises RuntimeError if the lock cannot be acquired (e.g. already held by another request).
    """
    token = await state_store.cart_lock_acquire(phone, bot_number, ttl_seconds=ttl_seconds)
    if not token:
        log.warning("cart_lock.contention", phone=phone, bot_number=bot_number)
        raise RuntimeError("cart_lock_contention")
    try:
        yield
    finally:
        await state_store.cart_lock_release(phone, bot_number, token=token)

async def find_dish(dish_name: str, bot_number: str) -> dict | None:
    if not dish_name or not dish_name.strip():
        log.info("find_dish.empty_query", bot_number=bot_number)
        return None

    menu = await db.db_get_menu(bot_number)
    if not menu:
        log.info("find_dish.no_menu", bot_number=bot_number)
        return None

    name_lower = dish_name.lower().strip()

    # Pass 1: exact match (case-insensitive)
    for category, dishes in menu.items():
        if not isinstance(dishes, list):
            continue
        for dish in dishes:
            if not isinstance(dish, dict):
                continue
            dish_name_stored = dish.get("name", "")
            if not isinstance(dish_name_stored, str):
                continue
            if dish_name_stored.lower().strip() == name_lower:
                log.info("find_dish.matched", query=dish_name, matched=dish_name_stored, method="exact")
                return {**dish, "category": category}

    # Pass 2: substring matches — collect all and pick the one whose name length
    # is closest to the query (avoids always returning the first-inserted item).
    # Require ratio = min(lens) / max(lens) >= 0.4 to prevent a 1-char query
    # from matching a long dish name (e.g. "a" must NOT match "Ajiaco").
    # The ratio is bidirectional: it bounds both query-in-name AND name-in-query.
    candidates = []
    for category, dishes in menu.items():
        if not isinstance(dishes, list):
            continue
        for dish in dishes:
            if not isinstance(dish, dict):
                continue
            dish_name_stored = dish.get("name", "")
            if not isinstance(dish_name_stored, str):
                continue
            dish_lower = dish_name_stored.lower()
            item_len = len(dish_lower)
            query_len = len(name_lower)
            if item_len == 0 or query_len == 0:
                continue
            # Bidirectional ratio: always in [0, 1], guards both match directions
            ratio = min(query_len, item_len) / max(query_len, item_len)
            if ratio < 0.4:
                continue
            if name_lower in dish_lower or dish_lower in name_lower:
                candidates.append({**dish, "category": category})

    if not candidates:
        log.info("find_dish.no_match", query=dish_name, method="none")
        return None

    # Prefer the dish whose name length is closest to the query length
    candidates.sort(key=lambda d: abs(len(d.get("name", "")) - len(dish_name)))
    matched = candidates[0]
    log.info("find_dish.matched", query=dish_name, matched=matched.get("name"), method="substring")
    return matched

async def add_to_cart(phone: str, dish_name: str, quantity: int, bot_number: str, note: str | None = None) -> dict:
    if quantity <= 0:
        return {"success": False, "error": "La cantidad debe ser mayor a cero"}

    dish = await find_dish(dish_name, bot_number)
    if not dish:
        return {"success": False, "error": f"No encontré '{dish_name}' en el menú"}

    norm_note = _normalize_note_for_storage(note)
    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)
            _ensure_line_ids(cart)

            found = False
            for item in cart["items"]:
                if item["name"] == dish["name"] and _notes_equal(item.get("note"), norm_note):
                    item["quantity"] += quantity
                    item["subtotal"] = float(money_mul(to_decimal(item["price"]), item["quantity"]))  # JSON boundary
                    found = True
                    break

            if not found:
                new_item = {
                    "line_id": _new_line_id(),
                    "name": dish["name"], "price": dish["price"],
                    "quantity": quantity, "subtotal": float(money_mul(to_decimal(dish["price"]), quantity)),  # JSON boundary
                    "category": dish["category"],
                    "note": norm_note,
                }
                if dish.get("sku"):
                    new_item["sku"] = dish["sku"]
                cart["items"].append(new_item)

            await db.db_save_cart(phone, bot_number, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart, "dish": dish}


async def resolve_dish_for_cart(
    bot_number: str, org_id: int, sku: str | None = None, name: str | None = None,
    location_id: int | None = None,
) -> dict | None:
    """Server-side dish resolution for DIRECT (non-LLM) cart taps from the
    diner chat UI. NEVER trusts price/category from the client — both are
    read from the menu here. Matches `sku` exactly when given, else falls
    back to find_dish()'s fuzzy name match. Rejects inactive dishes and
    dishes explicitly marked unavailable in menu_availability — defense in
    depth: the dish-card UI already hides these, but a stale client or a
    direct API call must not be able to bypass it.
    """
    menu = await db.db_get_menu(bot_number)
    if not menu:
        return None

    dish = None
    sku_clean = (sku or "").strip()
    if sku_clean:
        for category, dishes in menu.items():
            if not isinstance(dishes, list):
                continue
            for d in dishes:
                if isinstance(d, dict) and str(d.get("sku") or "").strip() == sku_clean:
                    dish = {**d, "category": category}
                    break
            if dish:
                break

    if dish is None and name and name.strip():
        dish = await find_dish(name, bot_number)

    if dish is None:
        return None

    if dish.get("active", True) is False:
        log.info("resolve_dish_for_cart.inactive", dish=dish.get("name"), bot_number=bot_number)
        return None

    # Sold out is per sede (migration 0091): the diner is at ONE of them, and
    # what another sede ran out of is irrelevant here. Without a sede there is
    # no honest answer, so the check is skipped rather than guessed — the
    # caller (the diner chat) always has one.
    availability = {}
    if location_id is not None:
        try:
            availability = await db.db_get_menu_availability(org_id, location_id)
        except Exception:
            log.exception(
                "resolve_dish_for_cart.availability_check_failed",
                bot_number=bot_number, location_id=location_id,
            )
            availability = {}
    else:
        log.warning("resolve_dish_for_cart.no_sede", bot_number=bot_number, org_id=org_id)

    if availability.get(dish.get("name"), True) is False:
        log.info("resolve_dish_for_cart.unavailable", dish=dish.get("name"), bot_number=bot_number)
        return None

    return dish


async def add_cart_line(phone: str, bot_number: str, dish: dict, qty: int, note: str | None = None) -> dict:
    """Deterministic (non-LLM) cart add for a tap on a dish card. `dish` MUST
    already be server-resolved (see resolve_dish_for_cart) — price/category
    are taken from it verbatim, never from client input.

    Merge rule: same dish + same note (after normalizing) increments the
    existing line's quantity; same dish + a different note creates a new
    line (see _notes_equal).
    """
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        return {"success": False, "error": "La cantidad debe ser un número entero mayor a cero"}
    if qty > _MAX_CART_QTY:
        return {"success": False, "error": f"La cantidad máxima por plato es {_MAX_CART_QTY}"}

    norm_note = _normalize_note_for_storage(note)
    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)
            _ensure_line_ids(cart)

            price_dec = to_decimal(dish.get("price", 0))
            merged = None
            for item in cart["items"]:
                if item.get("name") == dish.get("name") and _notes_equal(item.get("note"), norm_note):
                    merged = item
                    break

            if merged is not None:
                merged["quantity"] = int(merged.get("quantity") or 0) + qty
                merged["subtotal"] = float(money_mul(to_decimal(merged["price"]), merged["quantity"]))  # JSON boundary
                if dish.get("sku") and not merged.get("sku"):
                    merged["sku"] = dish["sku"]
            else:
                new_item = {
                    "line_id": _new_line_id(),
                    "name": dish.get("name", ""),
                    "price": dish.get("price", 0),
                    "quantity": qty,
                    "subtotal": float(money_mul(price_dec, qty)),  # JSON boundary
                    "category": dish.get("category", ""),
                    "note": norm_note,
                }
                if dish.get("sku"):
                    new_item["sku"] = dish["sku"]
                cart["items"].append(new_item)

            await db.db_save_cart(phone, bot_number, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart}


async def update_cart_line(
    phone: str, bot_number: str, line_id: str, qty: int | None = None, note: str | None = None,
) -> dict:
    """Update quantity and/or note on one cart line by `line_id`. qty=0
    removes the line. Returns {"success": False, "error": "not_found"} for
    an unknown line_id — callers map that to a 404-style response."""
    if qty is not None:
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
            return {"success": False, "error": "Cantidad inválida"}
        if qty > _MAX_CART_QTY:
            return {"success": False, "error": f"La cantidad máxima por plato es {_MAX_CART_QTY}"}

    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)
            _ensure_line_ids(cart)

            target = next((i for i in cart["items"] if i.get("line_id") == line_id), None)
            if target is None:
                return {"success": False, "error": "not_found"}

            if qty == 0:
                cart["items"] = [i for i in cart["items"] if i.get("line_id") != line_id]
            else:
                if qty is not None:
                    target["quantity"] = qty
                if note is not None:
                    target["note"] = _normalize_note_for_storage(note)
                target["subtotal"] = float(money_mul(to_decimal(target["price"]), target["quantity"]))  # JSON boundary

            await db.db_save_cart(phone, bot_number, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart}


async def remove_cart_line(phone: str, bot_number: str, line_id: str) -> dict:
    """Remove one cart line by `line_id`. Returns
    {"success": False, "error": "not_found"} for an unknown line_id."""
    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)
            _ensure_line_ids(cart)

            original_len = len(cart["items"])
            cart["items"] = [i for i in cart["items"] if i.get("line_id") != line_id]
            if len(cart["items"]) == original_len:
                return {"success": False, "error": "not_found"}

            await db.db_save_cart(phone, bot_number, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart}


async def get_cart_with_line_ids(phone: str, bot_number: str) -> dict:
    """Read the cart, lazily backfilling `line_id` on any legacy item and
    persisting the backfill (under the cart lock, re-reading fresh to avoid
    clobbering a concurrent mutation) so ids are stable across repeated
    reads (a page reload must see the SAME line_id it saw before). Best
    effort: on lock contention, returns the in-memory backfilled copy
    without persisting — a later mutation will persist it."""
    cart = await db.db_get_cart(phone, bot_number)
    if not _ensure_line_ids(cart):
        return cart
    try:
        async with _cart_lock(phone, bot_number):
            fresh = await db.db_get_cart(phone, bot_number)
            if _ensure_line_ids(fresh):
                await db.db_save_cart(phone, bot_number, fresh)
            return fresh
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            log.warning("cart.line_id_backfill_lock_contention", phone=phone, bot_number=bot_number)
            return cart
        raise

async def remove_from_cart(phone: str, dish_name: str, bot_number: str) -> dict:
    dish = await find_dish(dish_name, bot_number)
    if not dish:
        return {"success": False, "error": "Plato no encontrado"}

    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)
            original_count = len(cart["items"])
            cart["items"] = [i for i in cart["items"] if i["name"].lower() != dish["name"].lower()]
            if len(cart["items"]) == original_count:
                return {"success": False, "error": f"{dish['name']} no está en tu pedido."}
            await db.db_save_cart(phone, bot_number, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart}

async def clear_cart(phone: str, bot_number: str):
    try:
        async with _cart_lock(phone, bot_number):
            await db.db_clear_cart(phone, bot_number)
    except RuntimeError as e:
        if str(e) == "cart_lock_contention":
            log.warning("cart.clear_lock_contention", phone=phone)
            return
        raise


async def migrate_cart(phone: str, from_bot_number: str, to_bot_number: str) -> bool:
    """Migrate cart from one bot_number to another under a distributed lock.

    Locks both bot_numbers in deterministic sorted order to prevent deadlocks.
    Returns False if either lock cannot be acquired; re-raises non-contention errors.
    """
    keys = sorted([from_bot_number, to_bot_number])
    try:
        async with _cart_lock(phone, keys[0]):
            # Second lock acquired inside the first — if it fails, the first
            # _cart_lock's finally block releases the first lock automatically.
            try:
                async with _cart_lock(phone, keys[1]):
                    await db.db_migrate_cart(phone, from_bot_number, to_bot_number)
            except RuntimeError as e:
                if "cart_lock_contention" in str(e):
                    log.warning(
                        "cart_migration_lock_contention",
                        phone=phone,
                        failed_lock=keys[1],
                        reason="second lock unavailable — first lock released by context manager",
                    )
                    return False
                raise
    except RuntimeError as e:
        if "cart_lock_contention" in str(e):
            log.warning(
                "cart_migration_lock_contention",
                phone=phone,
                failed_lock=keys[0],
                reason="first lock unavailable",
            )
            return False
        raise
    return True
        
async def get_cart_total(phone: str, bot_number: str) -> float:
    cart = await db.db_get_cart(phone, bot_number)
    return sum(item["subtotal"] for item in cart["items"])

async def cart_summary(phone: str, bot_number: str) -> str:
    cart = await db.db_get_cart(phone, bot_number)
    if not cart["items"]:
        return "Cart is empty."

    lines = []
    for i in cart["items"]:
        line = f"• {i['quantity']}x {i['name']} — {i['subtotal']:,}"
        note = (i.get("note") or "").strip()
        if note:
            safe_note = _sanitize_note_for_llm_context(note)
            if safe_note:
                line += f" (nota: {safe_note})"
        lines.append(line)
    total = sum(item["subtotal"] for item in cart["items"])
    lines.append(f"\n*Total: {total:,}*")
    return "\n".join(lines)

# Zero-decimal currencies (no cents multiplier needed)
_ZERO_DECIMAL_CURRENCIES = {"COP", "CLP", "JPY", "KRW", "VND", "PYG", "ISK"}


def generate_wompi_payment_link(
    order_id: str,
    amount: int,
    currency: str = "COP",
    public_key: str | None = None,
    integrity_secret: str | None = None,
) -> str:
    """Generate a Wompi checkout payment link for a given order.

    Credential priority:
      1. Explicit kwargs (`public_key`, `integrity_secret`) — typically resolved
         from the restaurant's `features.wompi` configuration via
         `_wompi_credentials_from_restaurant()`.
      2. Env vars `WOMPI_PUBLIC_KEY` / `WOMPI_INTEGRITY_SECRET` — backward-compat
         fallback for deploys that have not yet migrated to per-restaurant config.

    Each credential falls back independently. If neither source supplies a
    value, raises RuntimeError so the caller can fall through to the
    manual-proof flow (Nequi / Bancolombia text instructions).
    """
    pk = public_key if (public_key and public_key.strip()) else (WOMPI_PUBLIC_KEY or "")
    integrity = (
        integrity_secret if (integrity_secret and integrity_secret.strip())
        else (WOMPI_INTEGRITY_SECRET or "")
    )

    # Fail-fast on missing config: silently signing with empty secret produces
    # a link Wompi rejects, so the customer gets a "broken link" with no clue
    # what went wrong. Better to crash here so the deploy alarms fire.
    if not integrity:
        log.error("orders.wompi_integrity_secret_missing", order_id=order_id)
        raise RuntimeError(
            "WOMPI_INTEGRITY_SECRET is not configured. Cannot generate payment link."
        )
    if not pk:
        log.error("orders.wompi_public_key_missing", order_id=order_id)
        raise RuntimeError(
            "WOMPI_PUBLIC_KEY is not configured. Cannot generate payment link."
        )

    from app.services.money import to_decimal, quantize_money  # noqa: PLC0415
    if currency in _ZERO_DECIMAL_CURRENCIES:
        # Zero-decimal currencies (COP, CLP…): amount is already in the base unit.
        # Coerce via Decimal to avoid float precision bugs (e.g. int(28000.0 * 100)).
        amount_cents = int(quantize_money(to_decimal(amount), currency))
    else:
        # Standard 2-decimal currencies: multiply by 100 to get cents.
        amount_cents = int(quantize_money(to_decimal(amount), currency) * 100)  # JSON boundary
    signature_string = f"{order_id}{amount_cents}{currency}{integrity}"
    signature = hashlib.sha256(signature_string.encode()).hexdigest()
    redirect_base = f"https://{APP_DOMAIN}" if APP_DOMAIN else ""
    redirect_url = f"{redirect_base}/api/payment/confirm"
    return f"https://checkout.wompi.co/p/?public-key={pk}&currency={currency}&amount-in-cents={amount_cents}&reference={order_id}&signature:integrity={signature}&redirect-url={redirect_url}"

async def create_order(phone: str, order_type: str, address: str, notes: str, bot_number: str, payment_method: str = "", channel: str | None = "whatsapp_bot", location_id: int | None = None, scheduled_pickup_at: str | None = None) -> dict:
    from app.repositories.orders_repo import commit_order_transaction, OrderCommitError, InsufficientStockError

    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)
            if not cart["items"]:
                return {"success": False, "error": "El carrito está vacío"}
            if order_type == "domicilio" and not address:
                return {"success": False, "error": "Se necesita dirección de entrega"}

            rest_data = await db.db_get_restaurant_by_phone(bot_number)
            delivery_fee = ZERO
            tz_str = "UTC"
            restaurant_id = None
            if rest_data:
                restaurant_id = rest_data.get("id")
                _raw_feats = rest_data.get("features") or {}
                if isinstance(_raw_feats, str):
                    try:
                        _raw_feats = json.loads(_raw_feats)
                    except Exception:
                        _raw_feats = {}
                if not isinstance(_raw_feats, dict):
                    _raw_feats = {}

                delivery_fee = to_decimal(_raw_feats.get("delivery_fee", 0)) if order_type == "domicilio" else ZERO
                tz_str = _raw_feats.get("timezone", "UTC")

            # Resolve per-restaurant Wompi credentials (fallback to env vars).
            # Done once here so both the additional-order and new-order branches
            # below reuse the same values without re-querying.
            wompi_pk, wompi_integrity = _wompi_credentials_from_restaurant(rest_data)

            # Plan-limit guard: monthly orders cap. Enforced BEFORE we tie up
            # any inventory or generate a Wompi link. UsageLimitExceeded is
            # caught here and returned as a soft error so the bot replies with
            # a message instead of dying with a stack trace.
            if restaurant_id and rest_data:
                try:
                    from app.services.subscription_guard import enforce_order_limit  # noqa: PLC0415
                    from app.services.database import UsageLimitExceeded  # noqa: PLC0415
                    await enforce_order_limit(restaurant_id, rest_data)
                except UsageLimitExceeded as exc:
                    log.warning(
                        "create_order.order_limit_hit",
                        phone=phone, restaurant_id=restaurant_id,
                        used=exc.used, limit=exc.limit,
                    )
                    return {"success": False, "error": str(exc)}

            subtotal = sum(to_decimal(item["subtotal"]) for item in cart["items"])
            total = subtotal + delivery_fee

            # Use a SINGLE connection for all transit/status reads to eliminate TOCTOU races.
            # tenant_connection() applies SET LOCAL app.org_id — required so the SELECT on
            # orders below cannot match rows from a different tenant that shares phone+bot_number.
            from app.services.tenant_db import tenant_connection  # noqa: PLC0415
            pool = await db.get_pool()
            async with tenant_connection() as conn:
                base_order = await conn.fetchrow(
                    """SELECT id, address, notes, payment_method, status
                       FROM orders
                       WHERE phone=$1 AND bot_number=$2
                         AND order_type=$3
                         AND (base_order_id IS NULL OR base_order_id = id)
                         AND status NOT IN ('entregado','cancelado')
                       ORDER BY created_at DESC LIMIT 1""",
                    phone, bot_number, order_type
                )

                if base_order:
                    current_status = base_order["status"]
                    if current_status in ("en_camino", "en_puerta"):
                        return {"success": False, "error": "in_transit", "blocked_in_transit": True}

                    base_id = base_order["id"]
                    # Re-check status within the same connection to close the TOCTOU window
                    locked = await conn.fetchrow(
                        "SELECT status FROM orders WHERE id=$1 AND status NOT IN ('en_camino','en_puerta','entregado','cancelado')",
                        base_id
                    )
                    if not locked:
                        return {"success": False, "error": "in_transit", "blocked_in_transit": True}

                    # sub_number is intentionally a hint only; commit_order_transaction
                    # recomputes it atomically inside the transaction to prevent races.
                    max_sub = await conn.fetchval(
                        "SELECT COALESCE(MAX(sub_number), 1) FROM orders WHERE base_order_id=$1 OR id=$1",
                        base_id
                    )
                    sub_number = max_sub + 1
                    order_id   = f"{base_id}-{sub_number}"

                    order = {
                        "id":                  order_id,
                        "phone":               phone,
                        "items":               cart["items"].copy(),
                        "order_type":          order_type,
                        "address":             address or base_order.get("address", ""),
                        "notes":               notes or base_order.get("notes", ""),
                        "subtotal":            subtotal,
                        "delivery_fee":        ZERO,
                        "total":               subtotal,
                        "status":              "pendiente",
                        "paid":                False,
                        "created_at":          datetime.now(ZoneInfo(tz_str)).isoformat(),
                        "bot_number":          bot_number,
                        "payment_method":      payment_method or base_order.get("payment_method", ""),
                        "is_additional":       True,
                        "base_order_id":       base_id,
                        "sub_number":          sub_number,
                        "scheduled_pickup_at": scheduled_pickup_at if order_type == "recoger" else None,
                    }
                    # Wompi link is OPTIONAL. If env vars are missing, fall through
                    # to the manual-proof flow: the conversation appends
                    # `features.payment_instructions[method]` so the customer pays
                    # to the restaurant's Nequi/Bancolombia and sends the receipt.
                    # Cash/efectivo never needs a link.
                    order["payment_url"] = None
                    if payment_method and payment_method.lower() not in ("efectivo", "cash"):
                        try:
                            order["payment_url"] = generate_wompi_payment_link(
                                order_id, subtotal,
                                public_key=wompi_pk,
                                integrity_secret=wompi_integrity,
                            )
                        except RuntimeError:
                            log.warning(
                                "create_order.wompi_unavailable_manual_proof_flow",
                                phone=phone, order_id=order_id, payment_method=payment_method,
                            )
                    try:
                        await commit_order_transaction(
                            pool,
                            restaurant_id=restaurant_id or 0,
                            conversation_id=phone,
                            cart=cart,
                            order_payload=order,
                            channel=channel,
                            location_id=location_id,
                        )
                    except InsufficientStockError as exc:
                        return {"success": False, "error": f"Stock insuficiente para '{exc.sku}'"}
                    except OrderCommitError as exc:
                        log.exception("create_order.commit_failed", error=str(exc), order_id=order_id)
                        return {"success": False, "error": "No pudimos procesar tu pedido, por favor intenta de nuevo."}
                    # Track subscription usage (best-effort, never blocks the order)
                    try:
                        from app.repositories.subscription_repo import db_increment_orders  # noqa: PLC0415
                        if restaurant_id:
                            await db_increment_orders(restaurant_id)
                    except Exception:
                        log.exception("subscription.order_increment_failed", phone=phone, order_id=order_id)
                    # Update customer memory after successful order (best-effort, never blocks on failure)
                    try:
                        from app.repositories.customer_profiles_repo import increment_after_order  # noqa: PLC0415
                        from decimal import Decimal
                        item_strs = [f"{i.get('qty', i.get('quantity', 1))}x {i.get('name', 'item')}" for i in cart.get("items", [])][:5]
                        summary = ", ".join(item_strs) if item_strs else "pedido"
                        order_total_decimal = Decimal(str(order["total"]))
                        if restaurant_id:
                            await increment_after_order(
                                restaurant_id=restaurant_id,
                                phone=phone,
                                order_total=order_total_decimal,
                                order_summary=summary,
                            )
                    except Exception:
                        log.exception("customer.profile_increment_failed", phone=phone)
                    return {"success": True, "order": order}

            order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
            order = {
                "id":                  order_id,
                "phone":               phone,
                "items":               cart["items"].copy(),
                "order_type":          order_type,
                "address":             address or "",
                "notes":               notes,
                "subtotal":            subtotal,
                "delivery_fee":        delivery_fee,
                "total":               total,
                "status":              "pendiente",
                "paid":                False,
                "created_at":          datetime.now(ZoneInfo(tz_str)).isoformat(),
                "bot_number":          bot_number,
                "payment_method":      payment_method,
                "is_additional":       False,
                "base_order_id":       None,
                "sub_number":          1,
                "scheduled_pickup_at": scheduled_pickup_at if order_type == "recoger" else None,
            }
            # Wompi link is OPTIONAL. If env vars are missing, fall through to
            # the manual-proof flow: the conversation appends
            # `features.payment_instructions[method]` so the customer pays to the
            # restaurant's Nequi/Bancolombia and sends the receipt.
            # Cash/efectivo never needs a link.
            order["payment_url"] = None
            if payment_method and payment_method.lower() not in ("efectivo", "cash"):
                try:
                    order["payment_url"] = generate_wompi_payment_link(
                        order_id, total,
                        public_key=wompi_pk,
                        integrity_secret=wompi_integrity,
                    )
                except RuntimeError:
                    log.warning(
                        "create_order.wompi_unavailable_manual_proof_flow",
                        phone=phone, order_id=order_id, payment_method=payment_method,
                    )
            try:
                await commit_order_transaction(
                    pool,
                    restaurant_id=restaurant_id or 0,
                    conversation_id=phone,
                    cart=cart,
                    order_payload=order,
                    channel=channel,
                    location_id=location_id,
                )
            except InsufficientStockError as exc:
                return {"success": False, "error": f"Stock insuficiente para '{exc.sku}'"}
            except OrderCommitError as exc:
                log.exception("create_order.commit_failed", error=str(exc), order_id=order_id)
                return {"success": False, "error": "No pudimos procesar tu pedido, por favor intenta de nuevo."}
            # Track subscription usage (best-effort, never blocks the order)
            try:
                from app.repositories.subscription_repo import db_increment_orders  # noqa: PLC0415
                if restaurant_id:
                    await db_increment_orders(restaurant_id)
            except Exception:
                log.exception("subscription.order_increment_failed", phone=phone, order_id=order_id)
            # Update customer memory after successful order (best-effort, never blocks on failure)
            try:
                from app.repositories.customer_profiles_repo import increment_after_order  # noqa: PLC0415
                from decimal import Decimal
                item_strs = [f"{i.get('qty', i.get('quantity', 1))}x {i.get('name', 'item')}" for i in cart.get("items", [])][:5]
                summary = ", ".join(item_strs) if item_strs else "pedido"
                order_total_decimal = Decimal(str(order["total"]))
                if restaurant_id:
                    await increment_after_order(
                        restaurant_id=restaurant_id,
                        phone=phone,
                        order_total=order_total_decimal,
                        order_summary=summary,
                    )
            except Exception:
                log.exception("customer.profile_increment_failed", phone=phone)
            return {"success": True, "order": order}
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise