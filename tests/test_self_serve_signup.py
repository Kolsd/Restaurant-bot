"""
tests/test_self_serve_signup.py
===============================
A restaurant registers itself, with nobody at Mesio in the loop.

Until 2026-09-23 `POST /api/signup` only filed a CRM lead; the account was
created by a founder pressing "convertir". These tests hold the new
contract, and specifically the three things that would quietly put a human
back in the loop if they broke:

  - the org, its first sede, its trial and its owner all exist when the
    request returns, and the owner can log in with the password they typed
    (not with one mailed to them — RESEND_API_KEY may not be set);
  - the same owner (same phone) can register a second restaurant — the
    phone once landed on a UNIQUE WhatsApp column and made that a 409;
  - a repeat email is refused outright rather than silently given a
    suffixed username the person could never guess.

Requires TEST_DATABASE_URL: these go through the real repositories, because
the failure they guard against (a UNIQUE index) only exists in Postgres.

Shape note: the tests are synchronous and verify through their own short
`asyncio.run` connections. The endpoint commits through the app's global
asyncpg pool, which belongs to whichever event loop first touched it — an
`async def` test would run in a second loop and make that pool raise
"another operation is in progress" instead of testing anything.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.main import app

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)

_PASSWORD = "unaClaveSegura1"


@pytest.fixture(scope="module")
def client():
    """One client for the module, entered as a context manager.

    Both details matter. A fresh TestClient per request spins up a fresh
    event loop, and the app's cached asyncpg pool belongs to the loop that
    created it — the second request then dies with "another operation is in
    progress" before reaching any assertion. Entering the context keeps one
    loop alive for every request in the module.

    The embedded inbox worker is switched off: running the lifespan starts
    it, and these tests have nothing to do with the WhatsApp queue.
    """
    os.environ.setdefault("DISABLE_EMBEDDED_WORKER", "1")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def unique():
    """A suffix that keeps each run's rows distinct (these rows are committed)."""
    return uuid.uuid4().hex[:10]


def _run(coro):
    """Run a short DB coroutine on its own loop, away from the app's pool."""
    return asyncio.run(coro)


async def _query(sql: str, *args, fetch: str = "row"):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        if fetch == "row":
            row = await conn.fetchrow(sql, *args)
            return dict(row) if row else {}
        if fetch == "all":
            return [dict(r) for r in await conn.fetch(sql, *args)]
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


async def _purge(orgs: list[int], usernames: list[str], phones: list[str]):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        if usernames:
            await conn.execute(
                "DELETE FROM users WHERE username = ANY($1::text[])", usernames
            )
        if phones:
            await conn.execute(
                """DELETE FROM prospect_notes
                   WHERE prospect_id IN (
                       SELECT id FROM prospects WHERE phone = ANY($1::text[])
                   )""",
                phones,
            )
            await conn.execute("DELETE FROM prospects WHERE phone = ANY($1::text[])", phones)
        for org_id in orgs:
            await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))
            await conn.execute("DELETE FROM subscription_usage WHERE org_id = $1", org_id)
            await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def cleanup():
    """Collects ids created during the test and removes them afterwards.

    The endpoint commits for real — a rolled-back fixture connection cannot
    undo it, and mocking the repositories would remove the layer under test.
    """
    orgs: list[int] = []
    usernames: list[str] = []
    phones: list[str] = []
    yield orgs, usernames, phones
    _run(_purge(orgs, usernames, phones))


def _payload(unique: str, **overrides) -> dict:
    body = {
        "nombre": "María García",
        "email": f"maria.{unique}@ejemplo.com",
        "telefono": "+57 321 000 0000",
        "restaurante": f"La Parrilla {unique}",
        "ciudad": "Bogotá",
        "plan": "Restaurante",
        "password": _PASSWORD,
    }
    body.update(overrides)
    return body


def _org(org_id: int) -> dict:
    return _run(_query(
        """SELECT id, name, slug, subscription_plan, comp_until
           FROM organizations WHERE id = $1""",
        org_id,
    ))


def _locations(org_id: int) -> list[dict]:
    return _run(_query(
        "SELECT id, name, org_id, active FROM locations WHERE org_id = $1",
        org_id, fetch="all",
    ))


def _user(username: str) -> dict:
    return _run(_query(
        """SELECT username, password_hash, role, org_id, location_id, branch_id
           FROM users WHERE username = $1""",
        username.lower(),
    ))


def test_signup_creates_a_tenant_the_owner_can_log_into(client, unique, cleanup):
    """One request → org + sede + trial + owner, and the password works."""
    from app.services.password_hash import verify_password
    from app.services.provisioning import DEFAULT_TRIAL_DAYS

    orgs, usernames, phones = cleanup
    body = _payload(unique)

    resp = client.post("/api/signup", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    orgs.append(data["org_id"])
    usernames.append(data["username"])
    phones.append(body["telefono"])

    # The owner logs in with the email they typed — not a derived variant.
    assert data["username"] == body["email"]
    assert data["login_url"] == "/login"
    assert data["trial_days"] == DEFAULT_TRIAL_DAYS

    org = _org(data["org_id"])
    assert org["name"] == body["restaurante"]
    assert org["slug"], "no slug means /pedir/{slug} 404s for every customer"
    assert org["subscription_plan"] == "restaurante"

    # The trial is real and lands on the advertised day, not a month away.
    expected = datetime.now(tz=timezone.utc) + timedelta(days=DEFAULT_TRIAL_DAYS)
    assert org["comp_until"] is not None
    assert abs((org["comp_until"] - expected).total_seconds()) < 300

    # A sede exists: an org without one cannot operate, since every
    # staff-facing listing filters by sede.
    locations = _locations(data["org_id"])
    assert len(locations) == 1
    assert locations[0]["id"] == data["location_id"]
    assert locations[0]["active"] is True

    # The funnel still sees the signup. This also covers migration 0095:
    # `prospects` was created as a four-column stub by 0012 and 0020's
    # CREATE TABLE IF NOT EXISTS silently skipped the real body, so on every
    # from-scratch database filing a lead raised UndefinedColumnError.
    prospect = _run(_query(
        """SELECT restaurant_name, owner_name, city, source, stage, tags
           FROM prospects WHERE phone = $1""",
        body["telefono"],
    ))
    assert prospect, "the signup must leave a CRM record"
    assert prospect["restaurant_name"] == body["restaurante"]
    assert prospect["owner_name"] == body["nombre"]
    assert prospect["city"] == body["ciudad"]
    assert prospect["source"] == "self_serve_signup"
    assert prospect["stage"] == "cerrado", "a self-serve signup is already closed"
    assert f"org:{data['org_id']}" in prospect["tags"]

    user = _user(data["username"])
    assert user, "the owner row must exist"
    assert user["role"] == "owner"
    assert user["org_id"] == data["org_id"]
    assert user["location_id"] is None, "an owner is not pinned to one sede"
    # The whole point of choosing a password at signup: it works without
    # any email being delivered.
    assert verify_password(_PASSWORD, user["password_hash"])
    assert not verify_password("otraClaveCualquiera", user["password_hash"])


def test_owner_is_greeted_by_the_name_typed_at_signup(client, unique, cleanup):
    """The signup form asks for the owner's name; the dashboard greeted them
    with their email because the name only ever reached the CRM."""
    orgs, usernames, phones = cleanup
    body = _payload(unique)

    signup = client.post("/api/signup", json=body)
    assert signup.status_code == 200, signup.text
    data = signup.json()
    orgs.append(data["org_id"])
    usernames.append(data["username"])
    phones.append(body["telefono"])

    stored = _run(_query(
        "SELECT display_name FROM users WHERE username = $1", data["username"].lower()
    ))
    assert stored["display_name"] == "María García"

    login = client.post(
        "/api/auth/login", json={"username": body["email"], "password": _PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["name"] == "María García"


def test_account_without_a_stored_name_still_logs_in_as_its_login(client, unique, cleanup):
    """Accounts created before users.display_name have none: login falls back
    to the username instead of returning an empty name."""
    orgs, usernames, phones = cleanup
    body = _payload(unique)

    data = client.post("/api/signup", json=body).json()
    orgs.append(data["org_id"])
    usernames.append(data["username"])
    phones.append(body["telefono"])

    _run(_query(
        "UPDATE users SET display_name = NULL WHERE username = $1",
        data["username"].lower(), fetch="none",
    ))
    login = client.post(
        "/api/auth/login", json={"username": body["email"], "password": _PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["name"] == body["email"]


def test_same_phone_can_register_a_second_restaurant(client, unique, cleanup):
    """The phone is a sales contact. It once landed on a UNIQUE WhatsApp
    column, which made a second restaurant by the same owner impossible to
    register."""
    orgs, usernames, phones = cleanup
    shared_phone = "+57 321 555 7788"

    first = client.post("/api/signup", json=_payload(
        unique, telefono=shared_phone, email=f"dueno.{unique}@ejemplo.com",
    ))
    assert first.status_code == 200, first.text
    orgs.append(first.json()["org_id"])
    usernames.append(first.json()["username"])
    phones.append(shared_phone)

    # Same person, same phone, second restaurant — this used to be a 409.
    second = client.post("/api/signup", json=_payload(
        unique, telefono=shared_phone,
        email=f"dueno.segundo.{unique}@ejemplo.com",
        restaurante=f"Segundo Local {unique}",
    ))
    assert second.status_code == 200, second.text
    orgs.append(second.json()["org_id"])
    usernames.append(second.json()["username"])

    assert second.json()["org_id"] != first.json()["org_id"]


def test_repeat_email_is_refused_not_silently_renamed(client, unique, cleanup):
    """409 with a message, never a `maria.42` account nobody can guess."""
    orgs, usernames, phones = cleanup
    body = _payload(unique)

    first = client.post("/api/signup", json=body)
    assert first.status_code == 200, first.text
    org_id = first.json()["org_id"]
    orgs.append(org_id)
    usernames.append(first.json()["username"])
    phones.append(body["telefono"])

    second_name = f"Otro Nombre {unique}"
    second = client.post("/api/signup", json=_payload(unique, restaurante=second_name))
    assert second.status_code == 409, second.text
    assert "cuenta" in second.json()["detail"].lower()

    # No orphan user was created under a suffixed name.
    assert not _user(f"{body['email']}.{org_id}")

    # And no orphan ORGANIZATION either. The org is created before the
    # owner, so a refusal discovered at the user step used to leave a dead
    # tenant behind on every repeat attempt — one that would still show up
    # in the superadmin list and in the MRR roll-up.
    leftovers = _run(_query(
        "SELECT id FROM organizations WHERE name = $1", second_name, fetch="all",
    ))
    assert leftovers == [], f"half-created orgs left behind: {leftovers}"


def test_a_weak_or_missing_password_is_rejected(client, unique):
    """No account is created without a password the owner can actually use."""
    short = client.post("/api/signup", json=_payload(unique, password="corta"))
    assert short.status_code == 422, short.text

    missing = _payload(unique)
    del missing["password"]
    assert client.post("/api/signup", json=missing).status_code == 422
