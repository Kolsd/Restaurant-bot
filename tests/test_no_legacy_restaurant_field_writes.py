"""
Forward guard: `db_update_restaurant_fields` and `db_update_subscription`
must never reappear under app/, and the two legacy superadmin endpoints that
called them must never come back either.

P0 (2026-09-12): both functions resolved the organization to update via
`WHERE id = (SELECT org_id FROM locations WHERE id = $N)` — i.e. they
expected a LOCATION id. The superadmin UI (app/static/html/internal/
superadmin.html) actually sent an ORG id (rows from db_get_all_orgs /
GET /api/internal/admin/restaurants). Org ids and location ids are
independent sequences over the same integer range (Wave 2), so whenever an
org's own id happened to equal a DIFFERENT org's location id, the subquery
silently resolved to — and wrote — the WRONG TENANT. Repro: org A id=9100
(location 9101), org B id=9200 with a location id=9100 — calling
db_update_subscription(9100, "suspended") left A untouched and suspended B.

Same bug class as the deleted db_get_restaurant_by_id (see
tests/test_no_ambiguous_restaurant_lookup.py) and the deleted
`is_primary`/`parent_restaurant_id` symbols (test_no_is_primary_sql.py /
test_no_parent_restaurant_id_sql.py) — same forward-guard pattern.

Replacement: the already-correct, unambiguous `db_update_organization(org_id,
**fields)` in app/repositories/restaurant_repo.py, called from
`PATCH /api/internal/admin/organizations/{org_id}`. superadmin.html now
calls that endpoint directly instead of the deleted
POST /update-restaurant / POST /set-subscription routes.
"""

import re
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent / "app"

# Matches an actual definition or call of a banned symbol — the name followed
# by "(" — so prose in comments/docstrings that merely MENTIONS the deleted
# names (which several files intentionally do, to document the incident)
# doesn't false-positive, while `def db_update_subscription(`,
# `db_update_subscription(x)`, `db.db_update_subscription(x)` etc. are all
# caught. A leading "was DELETED" style comment line is prose, not a call —
# but since prose can still contain "name(" (e.g. in a docstring listing
# call syntax), the real safety net is test_functions_are_gone below, which
# checks the live module object rather than source text.
_BANNED_CALL_RE = re.compile(r"\b(db_update_restaurant_fields|db_update_subscription)\(")

# Comment/docstring lines that are ALLOWED to mention the banned name (they
# document the deletion) — recognized by a handful of marker substrings we
# actually use in those comments. Anything else matching _BANNED_CALL_RE is
# a violation.
_ALLOWED_MARKERS = ("DELETED", "deleted", "forward guard", "P0 (2026-09")


def _iter_py_files():
    return sorted(APP_ROOT.rglob("*.py"))


def test_legacy_functions_never_reappear_as_live_code():
    """Source scan: no non-comment call/def of either banned name under app/."""
    violations: list[str] = []

    for py_file in _iter_py_files():
        source = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(source.splitlines(), start=1):
            if not _BANNED_CALL_RE.search(line):
                continue
            stripped = line.strip()
            # Skip lines that are clearly comments documenting the deletion.
            if stripped.startswith("#") and any(m in line for m in _ALLOWED_MARKERS):
                continue
            rel = py_file.relative_to(APP_ROOT.parent)
            violations.append(f"{rel}:{lineno}  →  {stripped!r}")

    assert not violations, (
        "db_update_restaurant_fields / db_update_subscription reappeared "
        "under app/ as live code. Both were deleted 2026-09-12 for a P0 "
        "cross-tenant write (ambiguous location-id-shaped subquery fed an "
        "org id). Use db_update_organization(org_id, **fields) instead.\n\n"
        + "\n".join(violations)
    )


def test_functions_are_gone_from_repo_and_database_module():
    """Sanity check the module-level contract directly (belt + suspenders)."""
    import app.repositories.restaurant_repo as repo

    assert not hasattr(repo, "db_update_restaurant_fields"), (
        "db_update_restaurant_fields must stay deleted from restaurant_repo"
    )
    assert not hasattr(repo, "db_update_subscription"), (
        "db_update_subscription must stay deleted from restaurant_repo"
    )
    assert hasattr(repo, "db_update_organization"), (
        "db_update_organization (the unambiguous, org-scoped replacement) must exist"
    )

    from app.services import database as db

    assert not hasattr(db, "db_update_restaurant_fields")
    assert not hasattr(db, "db_update_subscription")


def test_legacy_routes_are_gone():
    """The two legacy POST endpoints that called the banned functions must not exist."""
    import app.routes.internal.admin as admin_module

    paths = {route.path for route in admin_module.router.routes}
    assert "/api/internal/admin/set-subscription" not in paths, (
        "POST /set-subscription must stay deleted — it wrote via the "
        "now-deleted db_update_subscription. Use PATCH /organizations/{org_id}."
    )
    assert "/api/internal/admin/update-restaurant" not in paths, (
        "POST /update-restaurant must stay deleted — it wrote via the "
        "now-deleted db_update_restaurant_fields. Use PATCH /organizations/{org_id}."
    )
    assert "/api/internal/admin/organizations/{org_id}" in paths, (
        "PATCH /organizations/{org_id} (the unambiguous replacement) must exist"
    )


def test_superadmin_html_does_not_call_legacy_endpoints():
    """The superadmin UI must not POST to either deleted legacy path."""
    html_path = APP_ROOT / "static" / "html" / "internal" / "superadmin.html"
    source = html_path.read_text(encoding="utf-8", errors="replace")

    assert "/api/internal/admin/update-restaurant" not in source, (
        "superadmin.html still references the deleted POST /update-restaurant "
        "endpoint — it must call PATCH /api/internal/admin/organizations/{id} instead."
    )
    assert "/api/internal/admin/set-subscription" not in source, (
        "superadmin.html still references the deleted POST /set-subscription "
        "endpoint — it must call PATCH /api/internal/admin/organizations/{id} instead."
    )
