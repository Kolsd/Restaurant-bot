"""
app/services/delivery.py
=========================
Effective delivery/pickup configuration for a location — single source of
truth for the resolution precedence (docs/claude/delivery-web.md, locked
2026-09-17):

    locations.delivery_config  ->  organizations.features (legacy keys)  ->  hardcoded defaults

Every later chunk that needs delivery config (checkout validation, the
cashier's "Domicilios" section, the customer chat) MUST call
get_delivery_config() — there is no second copy of this precedence logic
anywhere else in the codebase.

Money values come back as Decimal (app/services/money.py). `float` only
appears at a JSON boundary and is explicitly marked as such.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone as _dt_timezone
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.repositories.restaurant_repo import haversine_km
from app.services.money import to_decimal

# Bottom of the precedence chain.
_DEFAULTS: dict[str, Any] = {
    "delivery_enabled": False,
    "pickup_enabled": False,
    "delivery_fee": Decimal("0"),
    "min_order": Decimal("0"),
    "radius_km": Decimal("5"),
    "prep_minutes": 30,
    "payment_methods": [],
}

# Legacy keys as stored today in organizations.features (pre-this-wave).
_LEGACY_KEY_MAP = {
    "delivery_fee": "delivery_fee",
    "min_order": "min_order",
    "delivery_radius_km": "radius_km",
    "payment_methods": "payment_methods",
}

_MONEY_KEYS = ("delivery_fee", "min_order", "radius_km")


def _as_dict(value: Any) -> dict:
    """Normalize a JSONB value that may come back as a dict or a JSON string.

    asyncpg's jsonb codec (app/services/database.py) decodes to a dict, but
    values read through other paths (e.g. the `restaurants` VIEW) can still
    surface as a raw string depending on driver/query path — same caveat
    documented on the existing `_features_dict` helpers in scheduler.py /
    subscription_guard.py.
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _legacy_org_config(org: Optional[dict]) -> dict:
    """Extract the legacy per-org delivery keys from organizations.features."""
    features = _as_dict((org or {}).get("features"))
    legacy: dict[str, Any] = {}
    for src_key, dst_key in _LEGACY_KEY_MAP.items():
        if src_key in features and features[src_key] is not None:
            legacy[dst_key] = features[src_key]
    return legacy


def get_delivery_config(org: Optional[dict], location: Optional[dict]) -> dict:
    """Resolve the effective delivery config for one location.

    Args:
        org: an organizations row (dict-like), or None.
        location: a locations row (dict-like), or None.

    Precedence: locations.delivery_config -> organizations.features (legacy)
    -> hardcoded defaults. Keys: delivery_enabled, pickup_enabled,
    delivery_fee, min_order, radius_km, prep_minutes, payment_methods.
    Money values are Decimal.
    """
    location_cfg = _as_dict((location or {}).get("delivery_config"))
    legacy_cfg = _legacy_org_config(org)

    resolved: dict[str, Any] = dict(_DEFAULTS)
    resolved.update(legacy_cfg)
    resolved.update(location_cfg)

    for key in _MONEY_KEYS:
        resolved[key] = to_decimal(resolved.get(key), default=_DEFAULTS[key])

    try:
        resolved["prep_minutes"] = int(resolved.get("prep_minutes") or _DEFAULTS["prep_minutes"])
    except (TypeError, ValueError):
        resolved["prep_minutes"] = _DEFAULTS["prep_minutes"]

    resolved["delivery_enabled"] = bool(resolved.get("delivery_enabled"))
    resolved["pickup_enabled"] = bool(resolved.get("pickup_enabled"))

    methods = resolved.get("payment_methods")
    resolved["payment_methods"] = list(methods) if isinstance(methods, (list, tuple)) else []

    return resolved


def delivery_config_to_json(config: dict) -> dict:
    """Convert a resolved config (Decimal money fields) to a JSON-safe dict.

    # JSON boundary — Decimal is not JSON-serializable; use this ONLY when
    # the config is about to leave the process (HTTP response body). Never
    # use this to build the value written to the `delivery_config` jsonb
    # column — that write goes through delivery_repo, which keeps its own
    # JSON-safety conversion right at the point of the DB call.
    """
    out = dict(config)
    for key in _MONEY_KEYS:
        if isinstance(out.get(key), Decimal):
            out[key] = float(out[key])  # JSON boundary
    return out


# ── Opening hours evaluation (per-sede, per-sede timezone) ──────────────────
#
# Reuses the EXACT `locations.opening_hours` shape already written by the
# locations settings UI (app/static/js/pages/settings.js: collectFormData())
# and read back by restaurant_repo's location getters — a dict keyed by
# lowercase English day names (DAYS_EN there: monday..sunday), each value
# {"open": "HH:MM" | None, "close": "HH:MM" | None, "closed": bool}. This is
# the FIRST code path in the repo that actually gates on opening_hours
# (nothing previously read it back for scheduling decisions), so no second
# format is invented here.

_DAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DEFAULT_TIMEZONE = "America/Bogota"  # matches locations.timezone's own DEFAULT (migration 0034)


def _parse_hhmm(value: Any) -> Optional[tuple[int, int]]:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _location_timezone(location: Optional[dict]) -> ZoneInfo:
    tz_name = (location or {}).get("timezone") or _DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(_DEFAULT_TIMEZONE)


def is_location_open(location: Optional[dict], now: Optional[datetime] = None) -> bool:
    """Is this sede open at `now` (defaults to the real current instant)?

    Evaluated in the SEDE'S OWN timezone (`locations.timezone`), never the
    server's and never UTC — the same wall-clock instant can fall on a
    different calendar day (and therefore a different configured row of
    opening_hours) depending on the timezone used, which is exactly the
    date.today()-vs-utcnow() bug class this codebase has already been bitten
    by three times (docs/claude/delivery-web.md instructions for this chunk).

    `now`, if given, may be naive or aware; naive is treated as UTC (never
    as the SERVER's local time — the server's tz is irrelevant here).

    No opening_hours configured at all (a brand-new sede that has never
    touched the hours UI) -> treated as OPEN. This is deliberate: this
    function is the FIRST consumer that gates anything on opening_hours, and
    failing closed for every unconfigured sede would silently brick the
    entire delivery/pickup entry flow for restaurants that simply haven't
    visited the hours settings page yet. A day that IS present in the dict
    with closed=True (or a missing/invalid open or close time) DOES close
    the sede for that day — only TOTAL absence of any configuration defaults
    to open.
    """
    hours = _as_dict(location.get("opening_hours") if location else None)
    if not hours:
        return True

    tz = _location_timezone(location)
    instant = now if now is not None else datetime.now(_dt_timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=_dt_timezone.utc)
    local = instant.astimezone(tz)

    day_cfg = hours.get(_DAY_KEYS[local.weekday()])
    if not isinstance(day_cfg, dict) or day_cfg.get("closed"):
        return False

    open_hm = _parse_hhmm(day_cfg.get("open"))
    close_hm = _parse_hhmm(day_cfg.get("close"))
    if not open_hm or not close_hm:
        return False

    local_minutes = local.hour * 60 + local.minute
    open_minutes = open_hm[0] * 60 + open_hm[1]
    close_minutes = close_hm[0] * 60 + close_hm[1]
    if open_minutes == close_minutes:
        return False  # zero-length window configured -> treat as closed
    if close_minutes > open_minutes:
        return open_minutes <= local_minutes < close_minutes
    # Overnight window (e.g. 18:00 -> 02:00): open from `open` through
    # midnight, then again from midnight through `close`.
    return local_minutes >= open_minutes or local_minutes < close_minutes


# ── Sede resolution ladder (docs/claude/delivery-web.md, locked) ───────────
#
# 1. nearest sede that is OPEN, has delivery_enabled, and whose radius_km
#    contains the point -> assign it for DELIVERY.
# 2. if that sede is closed, continue to the next sede that is open AND
#    covers the point.
# 3. if no sede covers the point, or all covering sedes are closed -> the
#    answer is PICKUP, sede list ordered by distance, nearest first.
# 4. no GPS fix at all -> PICKUP only, sede list with NO distances.

REASON_OUT_OF_COVERAGE = "out_of_coverage"
REASON_ALL_CLOSED = "all_closed"
REASON_NO_GPS = "no_gps"
REASON_DELIVERY_DISABLED = "delivery_disabled"
# Chunk 3 (checkout) additions — the entry-point ladder above never needs
# these because it silently falls back to pickup instead of refusing.
REASON_PICKUP_DISABLED = "pickup_disabled"
REASON_SCHEDULE_NOT_TODAY = "schedule_not_today"
REASON_SCHEDULE_IN_PAST = "schedule_in_past"
REASON_SCHEDULE_OUTSIDE_HOURS = "schedule_outside_hours"

_VALID_REQUESTED_MODES = ("delivery", "pickup")


def _location_distance_km(location: dict, lat: Optional[float], lon: Optional[float]) -> Optional[float]:
    if lat is None or lon is None:
        return None
    loc_lat, loc_lon = location.get("latitude"), location.get("longitude")
    if loc_lat is None or loc_lon is None:
        return None
    return haversine_km(float(lat), float(lon), float(loc_lat), float(loc_lon))


def _public_sede_view(entry: dict, include_distance: bool) -> dict:
    location = entry["location"]
    view = {
        "location_id": location.get("id"),
        "name": location.get("name"),
        "address": location.get("address"),
        "phone": location.get("phone"),
        "open_now": entry["open_now"],
    }
    if include_distance and entry["distance_km"] is not None:
        view["distance_km"] = round(entry["distance_km"], 3)
    return view


def _sort_key_by_distance(entry: dict):
    dist = entry["distance_km"]
    return dist if dist is not None else float("inf")


def _sort_key_by_name(entry: dict):
    return ((entry["location"].get("name") or ""), entry["location"].get("id") or 0)


def resolve_order_mode(
    *,
    org: Optional[dict],
    locations: list[dict],
    lat: Optional[float],
    lon: Optional[float],
    requested_mode: str,
    now: Optional[datetime] = None,
) -> dict:
    """Run the sede-assignment ladder for the public delivery/pickup entry
    point. `locations` must be the org's ACTIVE locations (caller already
    tenant-scoped and fetched them — this function does no I/O).

    Returns one of:
      {"mode": "delivery", "location": <public sede dict w/ distance_km>,
       "reason": None, "candidates": None}
      {"mode": "pickup", "location": None, "reason": <code or None>,
       "candidates": [<public sede dict>, ...]}

    `reason` is None for an EXPLICIT pickup request that was honored as-is
    (not a fallback) — it is only set when a delivery request could not be
    honored and was downgraded to pickup.
    """
    if requested_mode not in _VALID_REQUESTED_MODES:
        raise ValueError(f"invalid requested_mode: {requested_mode!r}")

    entries = []
    for location in locations or []:
        if location.get("active") is False:
            continue
        entries.append({
            "location": location,
            "config": get_delivery_config(org, location),
            "distance_km": _location_distance_km(location, lat, lon),
            "open_now": is_location_open(location, now),
        })

    def _pickup_fallback(reason: Optional[str]) -> dict:
        pickup_entries = [e for e in entries if e["config"]["pickup_enabled"]]
        has_gps = lat is not None and lon is not None
        pickup_entries.sort(key=_sort_key_by_distance if has_gps else _sort_key_by_name)
        return {
            "mode": "pickup",
            "location": None,
            "reason": reason,
            "candidates": [_public_sede_view(e, include_distance=has_gps) for e in pickup_entries],
        }

    if requested_mode == "pickup":
        return _pickup_fallback(reason=None)

    # requested_mode == "delivery"
    if lat is None or lon is None:
        return _pickup_fallback(reason=REASON_NO_GPS)

    delivery_capable = [e for e in entries if e["config"]["delivery_enabled"]]
    if not delivery_capable:
        return _pickup_fallback(reason=REASON_DELIVERY_DISABLED)

    covering = [
        e for e in delivery_capable
        if e["distance_km"] is not None and e["distance_km"] <= float(e["config"]["radius_km"])
    ]
    if not covering:
        return _pickup_fallback(reason=REASON_OUT_OF_COVERAGE)

    covering.sort(key=_sort_key_by_distance)
    for entry in covering:
        if entry["open_now"]:
            return {
                "mode": "delivery",
                "location": _public_sede_view(entry, include_distance=True),
                "reason": None,
                "candidates": None,
            }

    # Every covering sede is closed right now.
    return _pickup_fallback(reason=REASON_ALL_CLOSED)


# ── Checkout re-validation (chunk 3, docs/claude/delivery-web.md) ──────────
#
# resolve_order_mode() above is the ENTRY-POINT ladder: it picks AMONG
# several sedes and always lands on SOME answer (delivery or a pickup
# fallback). By the time a diner reaches checkout the sede is already fixed
# (the diner_sessions row created at entry), so what checkout needs is a
# different question: is THIS ALREADY-CHOSEN sede still valid for THIS
# order_mode RIGHT NOW, given the checkout's own (possibly different, and
# always untrusted) GPS pin? The client's earlier entry-point resolution is
# NEVER trusted at checkout time — see the chunk-3 instructions ("re-resolve
# coverage and opening hours on the server").


def validate_coverage_and_hours(
    *,
    location: dict,
    config: dict,
    order_mode: str,
    lat: Optional[float],
    lon: Optional[float],
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Returns None when the sede is valid for order_mode right now, else one
    of the REASON_* codes above."""
    if order_mode not in _VALID_REQUESTED_MODES:
        raise ValueError(f"invalid order_mode: {order_mode!r}")

    if location.get("active") is False:
        return REASON_ALL_CLOSED

    if order_mode == "delivery":
        if not config["delivery_enabled"]:
            return REASON_DELIVERY_DISABLED
        if lat is None or lon is None:
            return REASON_NO_GPS
        distance = _location_distance_km(location, lat, lon)
        if distance is None or distance > float(config["radius_km"]):
            return REASON_OUT_OF_COVERAGE
    else:  # pickup
        if not config["pickup_enabled"]:
            return REASON_PICKUP_DISABLED

    if not is_location_open(location, now=now):
        return REASON_ALL_CLOSED
    return None


def validate_schedule(
    location: dict, scheduled_at: datetime, now: Optional[datetime] = None,
) -> Optional[str]:
    """Validate a same-day scheduled pickup/delivery time against the sede's
    OWN opening hours and timezone (docs/claude/delivery-web.md: "Scheduled
    orders: SAME DAY ONLY"). Returns None when OK, else a REASON_* code.

    Naive datetimes are treated as UTC, matching is_location_open()'s own
    convention — never the server's local time.
    """
    tz = _location_timezone(location)
    instant_now = now if now is not None else datetime.now(_dt_timezone.utc)
    if instant_now.tzinfo is None:
        instant_now = instant_now.replace(tzinfo=_dt_timezone.utc)
    scheduled = scheduled_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=_dt_timezone.utc)

    local_now = instant_now.astimezone(tz)
    local_scheduled = scheduled.astimezone(tz)

    if local_scheduled.date() != local_now.date():
        return REASON_SCHEDULE_NOT_TODAY
    if local_scheduled < local_now:
        return REASON_SCHEDULE_IN_PAST
    if not is_location_open(location, now=scheduled):
        return REASON_SCHEDULE_OUTSIDE_HOURS
    return None


# ── Customer status page ETA (chunk 6, docs/claude/delivery-web.md) ────────


def compute_eta(
    accepted_at: Optional[datetime], estimated_minutes: Optional[int], location: Optional[dict],
) -> Optional[dict]:
    """ETA = accepted_at + estimated_minutes, rendered in the SEDE'S OWN
    timezone — never UTC, never the server's local time (same convention as
    is_location_open() / validate_schedule() above; docs/claude/delivery-web.md
    chunk 6 test requirement: "ETA is computed in the sede's timezone").

    Returns None when the order hasn't been accepted yet (no accepted_at) or
    the cashier hasn't typed an ETA — the customer status page shows
    "Esperando confirmación del restaurante" in that case instead.

    Returns {"iso": <UTC instant, JSON boundary>, "local_label": "HH:MM",
    "timezone": "<IANA name>"} — the ISO instant for anything that wants to
    do further arithmetic, plus a ready-to-display sede-local HH:MM label so
    the frontend never has to reimplement timezone conversion.
    """
    if accepted_at is None or estimated_minutes is None:
        return None
    instant = accepted_at
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=_dt_timezone.utc)
    eta_instant = instant + timedelta(minutes=int(estimated_minutes))
    tz = _location_timezone(location)
    local = eta_instant.astimezone(tz)
    return {
        "iso": eta_instant.isoformat(),
        "local_label": local.strftime("%H:%M"),
        "timezone": str(tz),
    }
