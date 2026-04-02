"""Payroll module — calculate, save, approve and export payroll runs."""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.routes.deps import get_current_restaurant, require_auth, require_module
from app.services import database as db

router = APIRouter(
    tags=["staff-payroll"],
    dependencies=[Depends(require_auth), Depends(require_module("staff_tips"))],
)


# ── Pydantic models ──────────────────────────────────────────────────────────

class PayrollRunCreate(BaseModel):
    period_start: str = Field(..., description="YYYY-MM-DD")
    period_end:   str = Field(..., description="YYYY-MM-DD")
    overtime_daily:      float = Field(8.0,  ge=0)
    overtime_weekly:     float = Field(40.0, ge=0)
    overtime_multiplier: float = Field(1.5,  ge=1.0)


# ── Helper ───────────────────────────────────────────────────────────────────

def _build_config(
    overtime_daily: float,
    overtime_weekly: float,
    overtime_multiplier: float,
) -> dict:
    return {
        "overtime_daily":      overtime_daily,
        "overtime_weekly":     overtime_weekly,
        "overtime_multiplier": overtime_multiplier,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/api/staff/payroll/calculate")
async def calculate_payroll(
    request: Request,
    period_start:        str,
    period_end:          str,
    overtime_daily:      float = 8.0,
    overtime_weekly:     float = 40.0,
    overtime_multiplier: float = 1.5,
):
    """Preview payroll for a date range without persisting."""
    restaurant = await get_current_restaurant(request)
    config = _build_config(overtime_daily, overtime_weekly, overtime_multiplier)

    entries = await db.db_calculate_payroll(
        restaurant["id"], period_start, period_end, config
    )

    totals = {
        "gross":      sum(e.get("gross_pay", 0)       for e in entries),
        "tips":       sum(e.get("tip_earnings", 0)    for e in entries),
        "deductions": sum(e.get("total_deductions", 0) for e in entries),
        "net":        sum(e.get("net_pay", 0)         for e in entries),
    }

    return {"entries": entries, "totals": totals}


@router.post("/api/staff/payroll/runs")
async def create_payroll_run(request: Request, body: PayrollRunCreate):
    """Save a payroll run as draft."""
    restaurant = await get_current_restaurant(request)
    config = _build_config(
        body.overtime_daily, body.overtime_weekly, body.overtime_multiplier
    )

    snapshot = await db.db_calculate_payroll(
        restaurant["id"], body.period_start, body.period_end, config
    )

    run = await db.db_save_payroll_run(
        restaurant_id=restaurant["id"],
        period_start=body.period_start,
        period_end=body.period_end,
        config=config,
        snapshot=snapshot,
    )

    return {"run": run}


@router.get("/api/staff/payroll/runs")
async def list_payroll_runs(request: Request, limit: int = 20):
    """List payroll runs for the restaurant."""
    restaurant = await get_current_restaurant(request)
    runs = await db.db_list_payroll_runs(restaurant["id"], limit=limit)
    return {"runs": runs}


@router.get("/api/staff/payroll/runs/{run_id}")
async def get_payroll_run(request: Request, run_id: str):
    """Get a single payroll run including its full snapshot."""
    restaurant = await get_current_restaurant(request)
    run = await db.db_get_payroll_run(run_id, restaurant["id"])
    if not run:
        raise HTTPException(404, "Nómina no encontrada.")
    return {"run": run}


@router.put("/api/staff/payroll/runs/{run_id}/approve")
async def approve_payroll_run(request: Request, run_id: str):
    """Approve a draft payroll run (draft → approved)."""
    restaurant = await get_current_restaurant(request)
    run = await db.db_approve_payroll_run(run_id, restaurant["id"])
    if not run:
        raise HTTPException(404, "Nómina no encontrada o ya no está en borrador.")
    return {"run": run}


@router.get("/api/staff/payroll/export/{run_id}")
async def export_payroll(request: Request, run_id: str):
    """Export a payroll run as a CSV download."""
    restaurant = await get_current_restaurant(request)
    run = await db.db_get_payroll_run(run_id, restaurant["id"])
    if not run:
        raise HTTPException(404, "Nómina no encontrada.")

    snapshot = run.get("snapshot", [])
    if isinstance(snapshot, str):
        import json
        snapshot = json.loads(snapshot)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Nombre", "Rol", "Horas Regulares", "Horas Extra",
        "Tarifa/Hora", "Pago Bruto", "Propinas",
        "Total Compensación", "Total Deducciones", "Pago Neto",
    ])
    for e in snapshot:
        writer.writerow([
            e.get("name", ""),
            e.get("role", ""),
            e.get("regular_hours", 0),
            e.get("overtime_hours", 0),
            e.get("hourly_rate", 0),
            e.get("gross_pay", 0),
            e.get("tip_earnings", 0),
            e.get("total_compensation", 0),
            e.get("total_deductions", 0),
            e.get("net_pay", 0),
        ])

    output.seek(0)
    filename = f"nomina_{run.get('period_start', '')}_{run.get('period_end', '')}.csv"
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
