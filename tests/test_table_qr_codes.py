"""
tests/test_table_qr_codes.py
============================
The QR on the table is the front door of the product.

Until 2026-09-24 every generated code pointed at `/menu/{table_id}` — the
catalog page — and the printable sheet told the diner to "pedir por
WhatsApp", the channel being retired. A restaurant that printed its codes
was handing customers the wrong flow on paper, which is the most expensive
place to be wrong: fixing it means reprinting and re-sticking every table.

The product is `/chat/{table_id}`: scanning opens the diner's chat, the bot
shows the carta as cards, the order reaches the kitchen (closed product
decision, docs/claude/status.md).

Also covers the all-tables sheet, which exists because printing codes one
page at a time is twenty round trips on a restaurant's first day.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


def _table(table_id: str = "mesa-7", name: str = "Mesa 7") -> dict:
    return {"id": table_id, "name": name, "number": 7, "location_id": 3, "org_id": 1}


def _lookup(table: dict | None = _table()):
    return patch("app.routes.tables.db.db_get_table_by_id", new=AsyncMock(return_value=table))


# ── Where the code points ─────────────────────────────────────────────────────

def test_the_table_qr_points_at_the_diner_chat_not_the_catalog(client):
    with _lookup():
        r = client.get("/api/tables/mesa-7/qr")
    assert r.status_code == 200

    # urllib.parse.quote keeps "/" unescaped, so the path is literal here.
    assert "/chat/mesa-7" in r.text, (
        "the QR must open the diner chat — the product — not the catalog page"
    )
    assert "/menu/mesa-7" not in r.text


def test_the_printable_sheet_points_at_the_diner_chat(client):
    with _lookup():
        r = client.get("/api/tables/mesa-7/qr-sheet")
    assert r.status_code == 200
    assert "/chat/mesa-7" in r.text
    assert "/menu/mesa-7" not in r.text


def test_the_printable_sheet_no_longer_tells_diners_to_order_by_whatsapp(client):
    """Paper outlives a deploy. The instructions have to match the product."""
    with _lookup():
        r = client.get("/api/tables/mesa-7/qr-sheet")
    assert r.status_code == 200
    assert "WhatsApp" not in r.text, "the printed steps still describe the retired channel"
    assert "Mesa 7" in r.text


def test_an_unknown_table_is_a_404_not_a_broken_code(client):
    with _lookup(None):
        r = client.get("/api/tables/no-existe/qr")
    assert r.status_code == 404


# ── The all-tables sheet ──────────────────────────────────────────────────────

def _sheet_with(tables: list[dict]):
    """Patch auth, scope and the table read; the HTML is generated for real."""
    return patch.multiple(
        "app.routes.tables",
        require_auth=AsyncMock(return_value=None),
        _tables_scope=AsyncMock(return_value=(1, 3)),
    ), patch("app.routes.tables.db.db_get_tables", new=AsyncMock(return_value=tables))


def test_the_sheet_has_one_card_per_table(client):
    scope, read = _sheet_with([
        _table("mesa-1", "Mesa 1"),
        _table("mesa-2", "Mesa 2"),
        _table("terraza-1", "Terraza 1"),
    ])
    with scope, read:
        r = client.get("/api/tables/qr-sheet")

    assert r.status_code == 200
    assert r.text.count("class='card'") == 3
    for table_id in ("mesa-1", "mesa-2", "terraza-1"):
        assert f"/chat/{table_id}" in r.text
    assert "3 mesas" in r.text


def test_a_restaurant_with_no_tables_yet_gets_told_what_to_do(client):
    """Not an error and not a blank page — the next step, spelled out."""
    scope, read = _sheet_with([])
    with scope, read:
        r = client.get("/api/tables/qr-sheet")

    assert r.status_code == 200
    assert "Todavía no tienes mesas" in r.text
    assert "class='card'" not in r.text


def test_a_table_name_cannot_inject_markup_into_the_sheet(client):
    """Table names are typed by the restaurant and printed on a public page."""
    scope, read = _sheet_with([_table("mesa-x", "<script>alert(1)</script>")])
    with scope, read:
        r = client.get("/api/tables/qr-sheet")

    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_a_table_without_an_id_is_skipped_rather_than_printed_blank(client):
    """A code that encodes nothing is worse than a missing card."""
    scope, read = _sheet_with([_table("mesa-1", "Mesa 1"), {"name": "Rota", "id": ""}])
    with scope, read:
        r = client.get("/api/tables/qr-sheet")

    assert r.status_code == 200
    assert r.text.count("class='card'") == 1
    assert "Rota" not in r.text


def test_the_sheet_is_scoped_the_same_way_as_the_table_list(client):
    """The codes a restaurant prints must not cover a sede it cannot see.

    Both go through `_tables_scope`, which verifies X-Branch-ID belongs to
    the user's org; the sheet passes whatever it returns straight to the
    repository read.
    """
    scope, read = _sheet_with([_table("mesa-1", "Mesa 1")])
    with scope, read as mock_read:
        r = client.get("/api/tables/qr-sheet")

    assert r.status_code == 200
    mock_read.assert_awaited_once_with(branch_id=3)
