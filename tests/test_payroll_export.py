"""
Suite — Payroll export flow
tests/test_payroll_export.py

Cubre el flujo completo de nómina:
  1. Calcular nómina: GET /api/staff/payroll/calculate → 200, entries correctas
  2. Guardar corrida:  POST /api/staff/payroll/runs    → 201, run guardado
  3. Listar corridas:  GET  /api/staff/payroll/runs    → 200, lista
  4. Exportar CSV:     GET  /api/staff/payroll/runs/{id}/export → 200, CSV válido
  5. Export 404:       run_id inexistente              → 404
  6. Snapshot vacío:   corrida sin empleados           → CSV con solo cabecera
"""
import csv
import io
import json
from unittest.mock import AsyncMock

import pytest

from tests.conftest import make_pool, make_row, patch_auth

_HEADERS = {"Authorization": "Bearer tok"}

# ── Fixtures compartidos ──────────────────────────────────────────────────────

_RUN_ID = "cccccccc-0000-4000-8000-000000000001"

_SNAPSHOT = [
    {
        "staff_id":           "aaaaaaaa-0000-4000-8000-000000000001",
        "name":               "Ana García",
        "role":               "mesero",
        "regular_hours":      40.0,
        "overtime_hours":     6.0,
        "hourly_rate":        12500.0,
        "gross_pay":          612500.0,
        "tip_earnings":       45000.0,
        "total_compensation": 657500.0,
        "deductions":         {"salud": 26300.0, "pension": 26300.0},
        "total_deductions":   52600.0,
        "net_pay":            604900.0,
    },
    {
        "staff_id":           "aaaaaaaa-0000-4000-8000-000000000002",
        "name":               "Carlos Mora",
        "role":               "caja",
        "regular_hours":      38.0,
        "overtime_hours":     0.0,
        "hourly_rate":        13000.0,
        "gross_pay":          494000.0,
        "tip_earnings":       0.0,
        "total_compensation": 494000.0,
        "deductions":         {"salud": 19760.0},
        "total_deductions":   19760.0,
        "net_pay":            474240.0,
    },
]

_RUN_ROW = {
    "id":           _RUN_ID,
    "restaurant_id": 1,
    "period_start": "2026-04-01",
    "period_end":   "2026-04-15",
    "status":       "draft",
    "snapshot":     json.dumps(_SNAPSHOT),
    "config":       "{}",
    "total_gross":  1106500.0,
    "total_net":    1079140.0,
    "created_by":   "+573001234567",
    "created_at":   "2026-04-15T18:00:00+00:00",
    "approved_at":  None,
}


def _auth(monkeypatch):
    """Habilita el módulo staff_tips y autentica."""
    import app.services.database as db_mod
    r = patch_auth(monkeypatch, features={"staff_tips": True})
    monkeypatch.setattr(db_mod, "db_check_module", AsyncMock(return_value=True))
    return r


# ══════════════════════════════════════════════════════════════════════════════
# 1. Calcular nómina
# ══════════════════════════════════════════════════════════════════════════════

def test_payroll_calculate_returns_entries(client, monkeypatch):
    """GET /payroll/calculate devuelve la lista de entradas por empleado."""
    _auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_calculate_payroll", AsyncMock(return_value=_SNAPSHOT))

    r = client.get(
        "/api/staff/payroll/calculate",
        params={"period_start": "2026-04-01", "period_end": "2026-04-15"},
        headers=_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert "entries" in data
    assert len(data["entries"]) == 2
    assert data["entries"][0]["name"] == "Ana García"
    assert data["entries"][0]["net_pay"] == 604900.0


# ══════════════════════════════════════════════════════════════════════════════
# 2. Guardar corrida
# ══════════════════════════════════════════════════════════════════════════════

def test_save_payroll_run_returns_run(client, monkeypatch):
    """POST /payroll/runs guarda el snapshot y devuelve el run creado."""
    _auth(monkeypatch)
    import app.services.database as db_mod

    saved_run = {k: v for k, v in _RUN_ROW.items() if k != "snapshot"}
    monkeypatch.setattr(db_mod, "db_calculate_payroll", AsyncMock(return_value=_SNAPSHOT))
    monkeypatch.setattr(db_mod, "db_save_payroll_run",  AsyncMock(return_value=saved_run))

    r = client.post(
        "/api/staff/payroll/runs",
        json={"period_start": "2026-04-01", "period_end": "2026-04-15"},
        headers=_HEADERS,
    )
    assert r.status_code == 201
    run = r.json()["run"]
    assert run["status"] == "draft"
    assert run["total_gross"] == 1106500.0


def test_save_payroll_run_missing_dates_returns_422(client, monkeypatch):
    """POST /payroll/runs sin period_start/end → 422."""
    _auth(monkeypatch)
    r = client.post("/api/staff/payroll/runs", json={}, headers=_HEADERS)
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# 3. Listar corridas
# ══════════════════════════════════════════════════════════════════════════════

def test_list_payroll_runs(client, monkeypatch):
    """GET /payroll/runs devuelve lista de corridas."""
    _auth(monkeypatch)
    import app.services.database as db_mod
    summary = {k: v for k, v in _RUN_ROW.items() if k != "snapshot"}
    monkeypatch.setattr(db_mod, "db_get_payroll_runs", AsyncMock(return_value=[summary]))

    r = client.get("/api/staff/payroll/runs", headers=_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["runs"]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. Exportar CSV — caso feliz
# ══════════════════════════════════════════════════════════════════════════════

def test_export_payroll_run_csv(client, monkeypatch):
    """GET /payroll/runs/{id}/export devuelve CSV con cabecera y una fila por empleado."""
    _auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_get_payroll_run", AsyncMock(return_value=_RUN_ROW))

    r = client.get(f"/api/staff/payroll/runs/{_RUN_ID}/export", headers=_HEADERS)
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert 'attachment; filename="nomina_20260401_20260415.csv"' in r.headers["content-disposition"]

    reader = csv.DictReader(io.StringIO(r.text))
    rows = list(reader)
    assert len(rows) == 2

    ana = rows[0]
    assert ana["Nombre"] == "Ana García"
    assert ana["Rol"] == "mesero"
    assert float(ana["Horas Regulares"]) == 40.0
    assert float(ana["Horas Extra"]) == 6.0
    assert float(ana["Pago Neto"]) == 604900.0

    carlos = rows[1]
    assert carlos["Nombre"] == "Carlos Mora"
    assert float(carlos["Horas Extra"]) == 0.0
    assert float(carlos["Pago Neto"]) == 474240.0


# ══════════════════════════════════════════════════════════════════════════════
# 5. Export 404
# ══════════════════════════════════════════════════════════════════════════════

def test_export_payroll_run_not_found(client, monkeypatch):
    """run_id inexistente → 404."""
    _auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_get_payroll_run", AsyncMock(return_value=None))

    r = client.get(f"/api/staff/payroll/runs/{_RUN_ID}/export", headers=_HEADERS)
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 6. Snapshot vacío → CSV solo con cabecera
# ══════════════════════════════════════════════════════════════════════════════

def test_export_empty_snapshot(client, monkeypatch):
    """Corrida sin empleados → CSV válido con solo la fila de cabecera."""
    _auth(monkeypatch)
    import app.services.database as db_mod
    empty_run = {**_RUN_ROW, "snapshot": json.dumps([])}
    monkeypatch.setattr(db_mod, "db_get_payroll_run", AsyncMock(return_value=empty_run))

    r = client.get(f"/api/staff/payroll/runs/{_RUN_ID}/export", headers=_HEADERS)
    assert r.status_code == 200
    reader = csv.DictReader(io.StringIO(r.text))
    rows = list(reader)
    assert rows == []
    assert "Nombre" in r.text  # cabecera presente
