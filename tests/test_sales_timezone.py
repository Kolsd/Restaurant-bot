"""
tests/test_sales_timezone.py

Sales are counted on the restaurant's local days, not on UTC days.

created_at is stored in UTC. In Colombia (UTC-5) a sale at 20:00 is 01:00
UTC the next day, so bucketing by UTC date moved every evening sale to the
following day and dropped it from "hoy" (walk-through 2026-09-28).
"""
from __future__ import annotations

from datetime import date, datetime

from app.repositories.stats_repo import _local_day, _local_midnight_utc


def test_an_evening_sale_in_bogota_belongs_to_that_bogota_day():
    # 2026-09-28 20:30 in Bogota == 2026-09-29 01:30 UTC
    assert _local_day(datetime(2026, 9, 29, 1, 30), "America/Bogota") == date(2026, 9, 28)
    assert _local_day(datetime(2026, 9, 29, 1, 30), "UTC") == date(2026, 9, 29)


def test_a_bogota_day_starts_at_05_utc():
    assert _local_midnight_utc(date(2026, 9, 28), "America/Bogota") == datetime(2026, 9, 28, 5, 0)
    assert _local_midnight_utc(date(2026, 9, 28), "UTC") == datetime(2026, 9, 28, 0, 0)
