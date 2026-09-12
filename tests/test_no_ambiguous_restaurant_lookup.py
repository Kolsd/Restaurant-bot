"""
Forward guard: `db_get_restaurant_by_id` must never reappear under app/.

P0 (2026-09): `db_get_restaurant_by_id(restaurant_id)` accepted EITHER a
location_id OR an org_id (`WHERE r.id = $1 OR l.org_id = $1`) and, on
ambiguity, preferred the ORG match. Org ids and location ids are
independent sequences over the same integer range, so for any org created
after Wave 2 a location id could collide with an unrelated org's real id —
serving that org's name/menu/features/payment-methods to the wrong tenant,
and scoping writes (tenant_scope) into the wrong org entirely. See
memory/ambiguous-restaurant-lookup-p0.md for the full incident writeup.

The function has been deleted and every caller migrated to one of two
unambiguous replacements:
  - db_get_restaurant_by_location_id(location_id) — filters ONLY on l.id
  - db_get_restaurant_by_org_id(org_id)           — filters ONLY on l.org_id

This test fails immediately if anyone reintroduces the ambiguous name
anywhere under app/ — as a definition, an import, or a call — so a future
"quick fix" or a merge from a stale branch cannot silently resurrect the
vulnerability. Modeled after tests/test_no_is_primary_sql.py /
tests/test_no_parent_restaurant_id_sql.py (same forward-guard pattern).
"""

import re
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent / "app"

# Matches an actual definition or call of the banned symbol — the name
# followed by "(" — so prose in comments/docstrings that merely MENTIONS
# the deleted function (e.g. "replaces the old db_get_restaurant_by_id")
# doesn't false-positive, while `def db_get_restaurant_by_id(`,
# `db_get_restaurant_by_id(x)`, `db.db_get_restaurant_by_id(x)` etc. are all
# caught. Word boundary on the left excludes the unambiguous replacements
# (`db_get_restaurant_by_location_id(`, `db_get_restaurant_by_org_id(`).
_BANNED_RE = re.compile(r"\bdb_get_restaurant_by_id\(")


def test_ambiguous_restaurant_lookup_never_reappears():
    violations: list[str] = []

    for py_file in sorted(APP_ROOT.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(source.splitlines(), start=1):
            if _BANNED_RE.search(line):
                rel = py_file.relative_to(APP_ROOT.parent)
                violations.append(f"{rel}:{lineno}  →  {line.strip()!r}")

    assert not violations, (
        "The ambiguous, now-deleted `db_get_restaurant_by_id` reappeared under app/.\n"
        "Use db_get_restaurant_by_location_id(location_id) or "
        "db_get_restaurant_by_org_id(org_id) instead — trace the id's origin\n"
        "to pick the right one; never guess.\n\n"
        + "\n".join(violations)
    )


def test_new_resolvers_exist_and_old_one_is_gone():
    """Sanity check the module-level contract directly (belt + suspenders
    against the regex guard above being bypassed by dynamic string tricks)."""
    import app.repositories.restaurant_repo as repo

    assert hasattr(repo, "db_get_restaurant_by_location_id"), (
        "db_get_restaurant_by_location_id must exist in restaurant_repo"
    )
    assert hasattr(repo, "db_get_restaurant_by_org_id"), (
        "db_get_restaurant_by_org_id must exist in restaurant_repo"
    )
    assert not hasattr(repo, "db_get_restaurant_by_id"), (
        "db_get_restaurant_by_id must be deleted from restaurant_repo"
    )

    from app.services import database as db

    assert hasattr(db, "db_get_restaurant_by_location_id")
    assert hasattr(db, "db_get_restaurant_by_org_id")
    assert not hasattr(db, "db_get_restaurant_by_id")
