"""restaurant_repo.haversine_km is the ONE distance function in the codebase.

It serves two very different scales: km-radius delivery coverage
(app/services/delivery.py) and the 50 m QR-scan geofence
(app/routes/dashboard.py, /api/qr-claim). These tests pin both, against
values computed independently from the Earth radius the function uses
(6371 km), not against the function's own output.
"""
import math

import pytest

from app.repositories.restaurant_repo import haversine_km

_KM_PER_DEGREE = 6371.0 * math.pi / 180.0  # 111.19492664455873 km


def test_one_degree_of_latitude_along_a_meridian():
    assert haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(_KM_PER_DEGREE, rel=1e-12)


def test_zero_distance_is_exactly_zero():
    assert haversine_km(4.6097, -74.0817, 4.6097, -74.0817) == 0.0


def test_metre_scale_precision_for_the_qr_geofence():
    """The 50 m geofence sits on the boundary where the old acos-based
    formula lost precision. 0.00045 deg of latitude is 50.0377 m."""
    expected_m = 0.00045 * _KM_PER_DEGREE * 1000.0
    got_m = haversine_km(4.6097, -74.0817, 4.6097 + 0.00045, -74.0817) * 1000.0
    assert got_m == pytest.approx(expected_m, abs=1e-6)  # sub-micrometre
    assert got_m > 50.0, "just outside the 50 m fence must not read as inside"

    inside_m = haversine_km(4.6097, -74.0817, 4.6097 + 0.00044, -74.0817) * 1000.0
    assert inside_m < 50.0


def test_is_symmetric():
    a = haversine_km(4.6097, -74.0817, 6.2442, -75.5812)   # Bogota -> Medellin
    b = haversine_km(6.2442, -75.5812, 4.6097, -74.0817)
    assert a == pytest.approx(b, rel=1e-15)


def test_antipodal_points_do_not_blow_up():
    """a can drift a hair above 1.0 at the antipode; the clamp keeps asin
    defined and the result is half the great circle."""
    assert haversine_km(0.0, 0.0, 0.0, 180.0) == pytest.approx(
        math.pi * 6371.0, rel=1e-12,
    )
