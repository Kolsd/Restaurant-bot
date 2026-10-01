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
from app.services import sede_context, sede_menu
from app.services.tenant_context import peek_tenant

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
async def _cart_lock(phone: str, org_id: int, ttl_seconds: int = 30):
    """
    Async context manager that acquires a distributed cart lock for (phone, org_id).
    Uses Redis SET NX EX when available, falls back to an asyncio.Lock per worker.
    Raises RuntimeError if the lock cannot be acquired (e.g. already held by another request).
    """
    token = await state_store.cart_lock_acquire(phone, org_id, ttl_seconds=ttl_seconds)
    if not token:
        log.warning("cart_lock.contention", phone=phone, org_id=org_id)
        raise RuntimeError("cart_lock_contention")
    try:
        yield
    finally:
        await state_store.cart_lock_release(phone, org_id, token=token)

async def _turn_menu(org_id: int | None = None, location_id: int | None = None) -> dict:
    """The carta of the sede this turn serves (migration 0093 — each sede
    has its own prices, hidden dishes and dishes of its own).

    Explicit ids win; otherwise the bot turn's ambient sede. With no sede at
    all the org's base carta is the only honest answer, and it is logged: an
    order priced from it may not be what the sede charges.
    """
    org = org_id or peek_tenant()
    sede = location_id or sede_context.current_sede_id()
    if org and sede:
        return await sede_menu.get_sede_menu(int(org), int(sede))
    log.warning("menu.read_without_sede", org_id=org)
    if not org:
        return {}
    return await db.db_get_menu(int(org)) or {}


async def find_dish(dish_name: str, org_id: int) -> dict | None:
    if not dish_name or not dish_name.strip():
        log.info("find_dish.empty_query", org_id=org_id)
        return None

    menu = await _turn_menu(org_id)
    if not menu:
        log.info("find_dish.no_menu", org_id=org_id)
        return None
    return await _find_in_menu(dish_name, menu, org_id)


async def _find_in_menu(dish_name: str, menu: dict, org_id: int) -> dict | None:
    """find_dish's matching (rule 12) against a carta the caller already
    holds, so a caller with an explicit sede does not re-read it."""
    if not dish_name or not dish_name.strip():
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

async def add_to_cart(phone: str, dish_name: str, quantity: int, org_id: int, note: str | None = None) -> dict:
    if quantity <= 0:
        return {"success": False, "error": "La cantidad debe ser mayor a cero"}

    dish = await find_dish(dish_name, org_id)
    if not dish:
        return {"success": False, "error": f"No encontré '{dish_name}' en el menú"}

    norm_note = _normalize_note_for_storage(note)
    try:
        async with _cart_lock(phone, org_id):
            cart = await db.db_get_cart(phone, org_id)
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

            await db.db_save_cart(phone, org_id, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart, "dish": dish}


async def resolve_dish_for_cart(
    org_id: int, sku: str | None = None, name: str | None = None,
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
    menu = await _turn_menu(org_id, location_id)
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
        dish = await _find_in_menu(name, menu, org_id)

    if dish is None:
        return None

    if dish.get("active", True) is False:
        log.info("resolve_dish_for_cart.inactive", dish=dish.get("name"), org_id=org_id)
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
                org_id=org_id, location_id=location_id,
            )
            availability = {}
    else:
        log.warning("resolve_dish_for_cart.no_sede", org_id=org_id)

    if availability.get(dish.get("name"), True) is False:
        log.info("resolve_dish_for_cart.unavailable", dish=dish.get("name"), org_id=org_id)
        return None

    return dish


async def add_cart_line(phone: str, org_id: int, dish: dict, qty: int, note: str | None = None) -> dict:
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
        async with _cart_lock(phone, org_id):
            cart = await db.db_get_cart(phone, org_id)
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

            await db.db_save_cart(phone, org_id, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart}


async def update_cart_line(
    phone: str, org_id: int, line_id: str, qty: int | None = None, note: str | None = None,
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
        async with _cart_lock(phone, org_id):
            cart = await db.db_get_cart(phone, org_id)
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

            await db.db_save_cart(phone, org_id, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart}


async def remove_cart_line(phone: str, org_id: int, line_id: str) -> dict:
    """Remove one cart line by `line_id`. Returns
    {"success": False, "error": "not_found"} for an unknown line_id."""
    try:
        async with _cart_lock(phone, org_id):
            cart = await db.db_get_cart(phone, org_id)
            _ensure_line_ids(cart)

            original_len = len(cart["items"])
            cart["items"] = [i for i in cart["items"] if i.get("line_id") != line_id]
            if len(cart["items"]) == original_len:
                return {"success": False, "error": "not_found"}

            await db.db_save_cart(phone, org_id, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart}


async def get_cart_with_line_ids(phone: str, org_id: int) -> dict:
    """Read the cart, lazily backfilling `line_id` on any legacy item and
    persisting the backfill (under the cart lock, re-reading fresh to avoid
    clobbering a concurrent mutation) so ids are stable across repeated
    reads (a page reload must see the SAME line_id it saw before). Best
    effort: on lock contention, returns the in-memory backfilled copy
    without persisting — a later mutation will persist it."""
    cart = await db.db_get_cart(phone, org_id)
    if not _ensure_line_ids(cart):
        return cart
    try:
        async with _cart_lock(phone, org_id):
            fresh = await db.db_get_cart(phone, org_id)
            if _ensure_line_ids(fresh):
                await db.db_save_cart(phone, org_id, fresh)
            return fresh
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            log.warning("cart.line_id_backfill_lock_contention", phone=phone, org_id=org_id)
            return cart
        raise

async def clear_cart(phone: str, org_id: int):
    try:
        async with _cart_lock(phone, org_id):
            await db.db_clear_cart(phone, org_id)
    except RuntimeError as e:
        if str(e) == "cart_lock_contention":
            log.warning("cart.clear_lock_contention", phone=phone)
            return
        raise


async def get_cart_total(phone: str, org_id: int) -> float:
    cart = await db.db_get_cart(phone, org_id)
    return sum(item["subtotal"] for item in cart["items"])

async def cart_summary(phone: str, org_id: int) -> str:
    cart = await db.db_get_cart(phone, org_id)
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
