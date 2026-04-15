"""
tests/test_tenant_context.py

Unit tests for tenant_context.py ContextVar plumbing.
No database required — everything is in-process.
"""
import asyncio
import pytest

from app.services.tenant_context import (
    TenantContextConflict,
    TenantNotSetError,
    bypass_tenant_scope,
    get_current_tenant,
    peek_tenant,
    tenant_scope,
)


# ── tenant_scope basic behaviour ──────────────────────────────────────────────

def test_tenant_scope_sets_and_restores():
    """Inside the scope the tenant is visible; after exit it raises again."""
    with tenant_scope(42):
        assert get_current_tenant() == 42
    # After exiting, no tenant is set — must raise
    with pytest.raises(TenantNotSetError):
        get_current_tenant()


def test_tenant_scope_zero_raises():
    with pytest.raises(ValueError):
        with tenant_scope(0):
            pass


def test_tenant_scope_negative_raises():
    with pytest.raises(ValueError):
        with tenant_scope(-5):
            pass


def test_tenant_scope_string_raises():
    with pytest.raises(ValueError):
        with tenant_scope("not_an_int"):  # type: ignore[arg-type]
            pass


def test_tenant_scope_none_raises():
    with pytest.raises(ValueError):
        with tenant_scope(None):  # type: ignore[arg-type]
            pass


def test_tenant_scope_bool_raises():
    """bool is a subclass of int in Python; we explicitly reject it."""
    with pytest.raises(ValueError):
        with tenant_scope(True):  # type: ignore[arg-type]
            pass


def test_nested_tenant_scope():
    """Inner scope shadows outer; outer is restored on inner exit."""
    with tenant_scope(10):
        assert get_current_tenant() == 10
        with tenant_scope(99):
            assert get_current_tenant() == 99
        assert get_current_tenant() == 10


# ── bypass_tenant_scope ───────────────────────────────────────────────────────

def test_bypass_returns_none():
    """get_current_tenant() returns None (not raises) while bypass is active."""
    with bypass_tenant_scope("admin_migration_run"):
        result = get_current_tenant()
        assert result is None


def test_bypass_reason_too_short_raises():
    with pytest.raises(ValueError):
        with bypass_tenant_scope("x"):  # len == 1, need >= 8
            pass


def test_bypass_reason_exactly_7_raises():
    with pytest.raises(ValueError):
        with bypass_tenant_scope("1234567"):  # len == 7
            pass


def test_bypass_reason_exactly_8_ok():
    with bypass_tenant_scope("12345678"):  # len == 8, borderline valid
        assert get_current_tenant() is None


def test_bypass_conflict_with_pinned_tenant():
    """bypass_tenant_scope must raise if a tenant is already pinned."""
    with tenant_scope(5):
        with pytest.raises(TenantContextConflict):
            with bypass_tenant_scope("should_fail_here"):
                pass


def test_bypass_restores_after_exit():
    """After bypass exits, the previous state (no tenant) is restored."""
    with bypass_tenant_scope("restore_check_ok"):
        pass
    # Now neither bypass nor tenant — should raise TenantNotSetError
    with pytest.raises(TenantNotSetError):
        get_current_tenant()


# ── peek_tenant ───────────────────────────────────────────────────────────────

def test_peek_tenant_never_raises():
    """peek_tenant() is always safe — returns None when nothing is set."""
    assert peek_tenant() is None


def test_peek_tenant_inside_scope():
    with tenant_scope(77):
        assert peek_tenant() == 77
    assert peek_tenant() is None


def test_peek_tenant_inside_bypass():
    """peek_tenant bypasses the bypass-flag logic — returns raw ContextVar."""
    with bypass_tenant_scope("peek_during_bypass"):
        # _current_tenant is None (not set), so peek returns None
        assert peek_tenant() is None


# ── Async task isolation ──────────────────────────────────────────────────────

async def test_contextvar_task_isolation():
    """Each asyncio Task copies the ContextVar snapshot at creation time.

    This test creates two tasks inside different tenant_scope() contexts and
    verifies they each see their own tenant, proving copy-on-write semantics.
    """
    results = {}

    async def capture(name: str, tid: int):
        with tenant_scope(tid):
            # Simulate an await boundary so the event loop can interleave.
            await asyncio.sleep(0)
            results[name] = get_current_tenant()

    await asyncio.gather(
        capture("task_a", 100),
        capture("task_b", 200),
    )

    assert results["task_a"] == 100
    assert results["task_b"] == 200


async def test_contextvar_task_no_leak():
    """A tenant set in a parent task must NOT leak into a child task created
    after the scope exits — child sees no tenant and raises TenantNotSetError."""
    errors: list[Exception] = []

    async def child_task():
        try:
            get_current_tenant()
        except TenantNotSetError as exc:
            errors.append(exc)

    # Set tenant, then exit scope before spawning child.
    with tenant_scope(55):
        pass  # scope already exited here

    task = asyncio.create_task(child_task())
    await task

    assert len(errors) == 1, "Child task should have raised TenantNotSetError"
