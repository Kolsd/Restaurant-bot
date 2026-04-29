"""
Forward guard: ensure SQL query strings in app/ do not filter operational tables
by the legacy `branch_id` column using org_id as the value.

Background
----------
After Wave-2 (migrations 0034-0038) and migration 0057 (sync_table_branch_id),
the column `branch_id` on all operational tables was synced to carry `location_id`
values (NOT org_id).  The recurring bug pattern was:

    WHERE branch_id = $1   -- $1 = org_id (e.g. 8)

…which matched zero rows because branch_id now equals location_id (e.g. 1, 3, 4).

Five separate incidents were patched reactively on 2026-04-29:
  06088d9 floor_plan
  6ce8127 conversations
  83311ce caja auto-load
  0b38e13 checkout proposals
  0279aa8 table-orders endpoint

This test is the forward guard so CI fails before the next instance ships.

What is checked
---------------
For every string literal in app/repositories/ and app/routes/ that looks like
SQL AND contains `branch_id` in a WHERE/AND/OR filter position on `table_orders`:

FORBIDDEN:
  * `FROM table_orders ... WHERE ... branch_id = $N`  (unqualified, no table alias)
  * `FROM table_orders ... AND branch_id = $N`

ALLOWED (explicitly permitted):
  * Any table-qualified form: `o.branch_id`, `to2.branch_id`, `tor.branch_id`
  * `users.branch_id`, `u.branch_id`  — auth identity column, not a sede filter
  * `rt.branch_id`, `t.branch_id`     — restaurant_tables; post-0057 == location_id
  * `occupancy_snapshots.branch_id`   — stores location_id (set by scheduler)
  * Any string that contains `FROM users` — users.branch_id is legacy auth

To suppress a legitimate occurrence, add a comment on the *preceding* line
in the Python source:
    # branch_id-guard-allow: <reason>

NOTE: The guard deliberately focuses on `table_orders` because that's the table
where the org_id-vs-location_id confusion historically caused production bugs.
Filters on restaurant_tables, occupancy_snapshots, nps_responses, etc. are OK
because callers consistently pass location_id for those tables.
"""

import ast
import re
import textwrap
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent / "app"

# Directories to scan
_SCAN_DIRS = [APP_ROOT / "repositories", APP_ROOT / "routes"]

# ── Patterns ──────────────────────────────────────────────────────────────────

# A string is treated as SQL only when it contains a clear multi-word SQL construct.
_SQL_KEYWORD_RE = re.compile(
    r'(?:'
    r'SELECT\s+[\w\*]'
    r'|INSERT\s+INTO\s+\w'
    r'|DELETE\s+FROM\s+\w'
    r'|UPDATE\s+\w+\s+SET\s+\w'
    r'|WHERE\s+\w'
    r'|JOIN\s+\w'
    r'|RETURNING\s+\w'
    r')',
    re.IGNORECASE,
)

# Must contain table_orders reference (the table where the bug occurs)
_TABLE_ORDERS_RE = re.compile(r'\btable_orders\b', re.IGNORECASE)

# Forbidden: unqualified branch_id filter (no table alias before it)
# These are the patterns that historically caused bugs (org_id passed as location_id).
_FORBIDDEN_PATTERNS = [
    # AND branch_id = / WHERE branch_id =
    re.compile(r'\b(?:WHERE|AND|OR)\s+branch_id\s*(?:=|IN\b|IS\b)', re.IGNORECASE),
    # branch_id = ANY(...)
    re.compile(r'\bbranch_id\s*=\s*ANY\s*\(', re.IGNORECASE),
]

# Allowed: these patterns indicate the column is properly qualified with a table alias
# OR that the context is not the problematic one.
_ALLOWED_PATTERNS = [
    # Table-qualified forms of branch_id on table_orders (post-0057 == location_id)
    re.compile(r'\bo\.branch_id\b', re.IGNORECASE),       # table_orders alias o
    re.compile(r'\bto2\.branch_id\b', re.IGNORECASE),     # table_orders alias to2
    re.compile(r'\btor\.branch_id\b', re.IGNORECASE),     # table_orders alias tor
    re.compile(r'\bto\.branch_id\b', re.IGNORECASE),      # table_orders alias to
    # Table-qualified forms on restaurant_tables (safe — post-0057 == location_id)
    re.compile(r'\brt\.branch_id\b', re.IGNORECASE),
    re.compile(r'\bt\.branch_id\b', re.IGNORECASE),
    # Any reference to users table — users.branch_id is the auth identity column
    re.compile(r'\bFROM\s+users\b', re.IGNORECASE),
    re.compile(r'\bUPDATE\s+users\b', re.IGNORECASE),
    re.compile(r'\bINTO\s+users\b', re.IGNORECASE),
    re.compile(r'\busers\.branch_id\b', re.IGNORECASE),
    re.compile(r'\bu\.branch_id\b', re.IGNORECASE),
    # occupancy_snapshots — branch_id stores location_id (set by scheduler)
    re.compile(r'\boccupancy_snapshots\b', re.IGNORECASE),
]

# Python source-level suppression comment
_SUPPRESS_COMMENT_RE = re.compile(r'branch_id-guard-allow', re.IGNORECASE)


def _is_sql(s: str) -> bool:
    return bool(_SQL_KEYWORD_RE.search(s))


def _has_table_orders(s: str) -> bool:
    return bool(_TABLE_ORDERS_RE.search(s))


def _is_forbidden(s: str) -> bool:
    return any(p.search(s) for p in _FORBIDDEN_PATTERNS)


def _is_allowed(s: str) -> bool:
    return any(p.search(s) for p in _ALLOWED_PATTERNS)


def _collect_string_literals(source: str) -> list[tuple[int, str]]:
    """Return (lineno, value) for every string constant in the AST."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            results.append((node.lineno, node.value))
    return results


def _source_line_has_suppress(source_lines: list[str], lineno: int) -> bool:
    """Check if the line BEFORE the string (lineno-1) has a suppress comment."""
    check_idx = lineno - 2
    if 0 <= check_idx < len(source_lines):
        return bool(_SUPPRESS_COMMENT_RE.search(source_lines[check_idx]))
    return False


def test_no_branch_id_legacy_filter_in_sql_strings():
    """Fail CI if any SQL string filters table_orders by unqualified `branch_id`.

    Post-Wave-2 + migration 0057: table_orders.branch_id == location_id.
    Filtering with org_id (passed as branch_id) returns zero rows — root cause
    of 5 production incidents patched on 2026-04-29.

    The guard only checks queries that reference `table_orders` to avoid false
    positives on tables where unqualified `branch_id` filters are correct
    (restaurant_tables, occupancy_snapshots, nps_responses — all store location_id).

    Table-qualified forms (o.branch_id, to2.branch_id) are allowed because they
    explicitly name the table alias, making the intent clear.
    """
    violations: list[str] = []

    for scan_dir in _SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for py_file in sorted(scan_dir.rglob("*.py")):
            source = py_file.read_text(encoding="utf-8", errors="replace")
            source_lines = source.splitlines()

            for lineno, literal_value in _collect_string_literals(source):
                if "branch_id" not in literal_value.lower():
                    continue
                if not _is_sql(literal_value):
                    continue
                # Only flag queries that touch table_orders (the problematic table)
                if not _has_table_orders(literal_value):
                    continue
                if not _is_forbidden(literal_value):
                    continue
                # Exempted if a table-qualified alias or safe context
                if _is_allowed(literal_value):
                    continue
                # Exempted by suppression comment in source
                if _source_line_has_suppress(source_lines, lineno):
                    continue

                excerpt = textwrap.shorten(literal_value, width=140, placeholder="…")
                rel = py_file.relative_to(APP_ROOT.parent)
                violations.append(f"{rel}:{lineno}  →  {excerpt!r}")

    assert not violations, (
        "SQL query string(s) that filter `table_orders` by unqualified `branch_id`.\n"
        "Post-Wave-2 + migration 0057, `table_orders.branch_id` carries location_id,\n"
        "NOT org_id — filtering with org_id returns zero rows.\n\n"
        "Fix options:\n"
        "  1. Filter by org_id = $N (tenant filter — RLS already does this).\n"
        "  2. Filter by location_id = $N (sede filter — use the canonical column).\n"
        "  3. Add a table alias to the branch_id ref: `o.branch_id`, `to2.branch_id`.\n"
        "  4. Add a `# branch_id-guard-allow: <reason>` comment on the line BEFORE\n"
        "     the string literal (for genuinely correct uses the heuristic misses).\n\n"
        + "\n".join(violations)
    )
