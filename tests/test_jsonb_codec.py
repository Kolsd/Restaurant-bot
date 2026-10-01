"""
tests/test_jsonb_codec.py

The app pool's jsonb codec must store a value exactly once, whether the
writer passed a Python object or JSON text it serialized itself.

Until 2026-09-29 the encoder was a plain `json.dumps`, so every writer that
passed `json.dumps(x)` to a `$n::jsonb` parameter stored a JSON *string*
wrapping the object (carts, conversation history, table checks, org menu and
features). Readers hid it by `json.loads`-ing any str, but SQL that looks
inside the value (`->`, `jsonb_array_elements`) saw a scalar. Migration 0100
repaired the stored rows.

Requires TEST_DATABASE_URL: the point is what Postgres ends up holding.
"""
from __future__ import annotations

import json
import os

import asyncpg
import pytest

from app.services.database import _encode_jsonb, init_connection

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")


def test_encoder_serializes_objects_and_passes_json_text_through():
    assert _encode_jsonb({"a": 1}) == '{"a": 1}'
    assert _encode_jsonb([1, 2]) == "[1, 2]"
    assert _encode_jsonb('{"a": 1}') == '{"a": 1}'


@pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set")
@pytest.mark.asyncio
async def test_both_writer_styles_store_an_object():
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await init_connection(conn)
        value = {"items": [{"name": "Ajiaco", "qty": 2}]}
        for param in (value, json.dumps(value)):
            row = await conn.fetchrow(
                "SELECT jsonb_typeof($1::jsonb) AS t, $1::jsonb -> 'items' -> 0 ->> 'name' AS name",
                param,
            )
            assert row["t"] == "object"
            assert row["name"] == "Ajiaco"
    finally:
        await conn.close()


@pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set")
@pytest.mark.asyncio
async def test_no_jsonb_column_holds_a_wrapped_object():
    """After 0100 no stored value is a string that is really an object/array."""
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        cols = await conn.fetch(
            """SELECT c.table_name, c.column_name
                 FROM information_schema.columns c
                 JOIN information_schema.tables t USING (table_schema, table_name)
                WHERE c.table_schema = 'public' AND c.data_type = 'jsonb'
                  AND t.table_type = 'BASE TABLE'"""
        )
        wrapped = {}
        for r in cols:
            n = await conn.fetchval(
                f'SELECT count(*) FROM "{r["table_name"]}" '
                f'WHERE jsonb_typeof("{r["column_name"]}") = \'string\' '
                f'AND ltrim("{r["column_name"]}" #>> \'{{}}\') ~ \'^[\\[{{]\''
            )
            if n:
                wrapped[f'{r["table_name"]}.{r["column_name"]}'] = n
        assert wrapped == {}
    finally:
        await conn.close()
