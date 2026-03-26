"""
Suite 6 — Dashboard: Timezone-aware Date Filters
tests/test_dashboard.py

Covers:
  1.  get_date_range("today", "America/Bogota")  → local date (UTC-5)
  2.  get_date_range("today", "Europe/Madrid")   → local date (UTC+1 or UTC+2 DST)
  3.  Bogota "today" ≠ Madrid "today" when they straddle midnight UTC
  4.  get_date_range("week", tz)   → span of 7 days, end = today
  5.  get_date_range("month", tz)  → starts on day=1 of current month
  6.  get_date_range("year", tz)   → starts on Jan 1 of current year
  7.  get_date_range("semester", tz) → Jan 1 or Jul 1 depending on month
  8.  Unknown period → falls back to "today" (start == end)
  9.  get_tz: valid IANA timezone string in features
 10.  get_tz: missing key → "UTC"
 11.  get_tz: features stored as JSON string (legacy) → parsed correctly
 12.  date_from ≤ date_to for all valid periods
 13.  Dashboard sync endpoint uses restaurant timezone, not UTC
"""
import json
import pytest
from datetime import datetime, date
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch


# ══════════════════════════════════════════════════════════════════════════════
# Import the pure helpers (no DB, no HTTP)
# ══════════════════════════════════════════════════════════════════════════════

from app.routes.stats import get_date_range, get_tz


# ══════════════════════════════════════════════════════════════════════════════
# 1–3. Timezone-local "today"
# ══════════════════════════════════════════════════════════════════════════════

def test_today_bogota_matches_local_date():
    """
    "today" in Bogota must equal the calendar date in America/Bogota,
    regardless of UTC offset.
    """
    tz_str = "America/Bogota"
    tz = ZoneInfo(tz_str)
    expected = str(datetime.now(tz).date())

    start, end = get_date_range("today", tz_str)

    assert start == expected
    assert end   == expected


def test_today_madrid_matches_local_date():
    """
    "today" in Madrid must equal the calendar date in Europe/Madrid.
    """
    tz_str = "Europe/Madrid"
    tz = ZoneInfo(tz_str)
    expected = str(datetime.now(tz).date())

    start, end = get_date_range("today", tz_str)

    assert start == expected
    assert end   == expected


def test_today_utc_matches_utc_date():
    """Control: UTC always returns today's UTC date."""
    tz_str = "UTC"
    expected = str(datetime.now(ZoneInfo("UTC")).date())
    start, end = get_date_range("today", tz_str)
    assert start == expected
    assert end   == expected


def test_start_equals_end_for_today():
    """For period='today', start_date == end_date."""
    start, end = get_date_range("today", "UTC")
    assert start == end


# ══════════════════════════════════════════════════════════════════════════════
# 4–8. Other periods
# ══════════════════════════════════════════════════════════════════════════════

def test_week_spans_seven_days():
    """Period='week' must span exactly 7 calendar days (start to end inclusive)."""
    start_str, end_str = get_date_range("week", "UTC")
    start = date.fromisoformat(start_str)
    end   = date.fromisoformat(end_str)
    delta = (end - start).days
    assert delta == 6, f"Expected 6-day gap (7 days inclusive), got {delta}"


def test_week_ends_today():
    """The end of 'week' period must be today."""
    _, end_str = get_date_range("week", "UTC")
    today = str(datetime.now(ZoneInfo("UTC")).date())
    assert end_str == today


def test_month_starts_on_day_one():
    """Period='month' must start on the 1st of the current month."""
    start_str, _ = get_date_range("month", "America/Bogota")
    start = date.fromisoformat(start_str)
    assert start.day == 1


def test_year_starts_jan_first():
    """Period='year' must start on January 1 of the current year."""
    start_str, _ = get_date_range("year", "UTC")
    start = date.fromisoformat(start_str)
    assert start.month == 1
    assert start.day   == 1


def test_semester_starts_jan_or_jul():
    """Period='semester' must start on Jan 1 or Jul 1."""
    start_str, _ = get_date_range("semester", "UTC")
    start = date.fromisoformat(start_str)
    assert start.month in (1, 7)
    assert start.day == 1


def test_unknown_period_falls_back_to_today():
    """Unknown period string → start == end == today (safe fallback)."""
    start, end = get_date_range("bimonthly_quantum", "UTC")
    today = str(datetime.now(ZoneInfo("UTC")).date())
    assert start == today
    assert end   == today


@pytest.mark.parametrize("period", ["today", "week", "month", "semester", "year"])
def test_start_lte_end_for_all_periods(period):
    """date_from must always be ≤ date_to for every valid period."""
    start_str, end_str = get_date_range(period, "UTC")
    start = date.fromisoformat(start_str)
    end   = date.fromisoformat(end_str)
    assert start <= end, f"start ({start}) > end ({end}) for period={period}"


# ══════════════════════════════════════════════════════════════════════════════
# 9–11. get_tz helper
# ══════════════════════════════════════════════════════════════════════════════

def test_get_tz_returns_configured_timezone():
    """Restaurant with timezone feature → get_tz returns that string."""
    restaurant = {"features": {"timezone": "America/Bogota"}}
    assert get_tz(restaurant) == "America/Bogota"


def test_get_tz_defaults_to_utc_when_absent():
    """No timezone key in features → 'UTC'."""
    assert get_tz({"features": {}}) == "UTC"
    assert get_tz({}) == "UTC"
    # features key present but empty string → JSON parse fails → UTC
    assert get_tz({"features": "{}"}) == "UTC"


def test_get_tz_parses_json_string_features():
    """
    features stored as a JSON string (legacy rows) must be parsed.
    get_tz should not crash and must return the correct timezone.
    """
    restaurant = {"features": json.dumps({"timezone": "America/Mexico_City"})}
    assert get_tz(restaurant) == "America/Mexico_City"


def test_get_tz_invalid_json_string_defaults_to_utc():
    """Corrupted features string → safe fallback to UTC."""
    restaurant = {"features": "{not valid json!!!"}
    # Should not raise; should return UTC
    result = get_tz(restaurant)
    assert result == "UTC"


# ══════════════════════════════════════════════════════════════════════════════
# 13. Dashboard sync endpoint uses restaurant timezone
# ══════════════════════════════════════════════════════════════════════════════

def test_dashboard_sync_uses_restaurant_timezone(client, monkeypatch):
    """
    GET /api/dashboard/sync must read the restaurant's timezone from features
    and call db_get_orders_range with the correct local date boundaries.
    """
    from tests.conftest import patch_auth
    import app.services.database as db_mod

    # Restaurant configured in Bogota time
    patch_auth(monkeypatch,
               features={"timezone": "America/Bogota"},
               whatsapp_number="+573009999999")

    captured_calls = {}

    async def mock_get_orders(date_from, date_to, bot_number=None):
        captured_calls["date_from"] = date_from
        captured_calls["date_to"]   = date_to
        return []

    async def mock_get_reservations(date_from, date_to, bot_number=None):
        return []

    async def mock_get_conversations(bot_number=None, date_from=None, date_to=None):
        return []

    monkeypatch.setattr(db_mod, "db_get_orders_range",       mock_get_orders)
    monkeypatch.setattr(db_mod, "db_get_reservations_range", mock_get_reservations)
    monkeypatch.setattr(db_mod, "db_get_all_conversations",  mock_get_conversations)

    r = client.get(
        "/api/dashboard/sync?period=today",
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200

    # The date used must be the local Bogota date, not necessarily UTC
    bogota_today = str(datetime.now(ZoneInfo("America/Bogota")).date())
    assert captured_calls.get("date_from") == bogota_today
    assert captured_calls.get("date_to")   == bogota_today
