"""
Forward guard: ensure no SQL query string in app/ references the dropped column
`parent_restaurant_id`.

After migration 0038, the column will not exist on any table.  This test
catches accidental re-introduction at CI time.

Only string literals that look like SQL (contain a SQL keyword: SELECT, FROM,
WHERE, JOIN, UPDATE, INSERT, DELETE) are checked.  Pure Python usages such as
dict key accesses (.get("parent_restaurant_id")), function parameter names,
docstrings, and comments are intentionally ignored.

ALLOWED SQL occurrences (explicit exceptions):
- The NULL alias in deps.py  (``NULL::int AS parent_restaurant_id``)
  which is a VIEW-compatibility shim that emits the column *name* as an alias
  but does not *filter* by it.
"""

import ast
import re
import textwrap
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent / "app"

_COLUMN_RE = re.compile(r'parent_restaurant_id', re.IGNORECASE)

# A string is treated as SQL only when it contains at least one SQL keyword.
_SQL_KEYWORD_RE = re.compile(
    r'\b(SELECT|FROM|WHERE|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM|ON\s+\w|SET\s+\w)\b',
    re.IGNORECASE,
)

# Allowed patterns — even if the literal looks like SQL and contains the column name.
_ALLOWED_PATTERNS = [
    re.compile(r'NULL(?:::int)?\s+AS\s+parent_restaurant_id', re.IGNORECASE),
]


def _is_sql(s: str) -> bool:
    return bool(_SQL_KEYWORD_RE.search(s))


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


def test_no_parent_restaurant_id_in_sql_strings():
    violations: list[str] = []

    for py_file in sorted(APP_ROOT.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8", errors="replace")

        for lineno, literal_value in _collect_string_literals(source):
            if (
                _COLUMN_RE.search(literal_value)
                and _is_sql(literal_value)
                and not _is_allowed(literal_value)
            ):
                excerpt = textwrap.shorten(literal_value, width=120, placeholder="…")
                rel = py_file.relative_to(APP_ROOT.parent)
                violations.append(f"{rel}:{lineno}  →  {excerpt!r}")

    assert not violations, (
        "SQL query string(s) referencing the dropped column `parent_restaurant_id` found.\n"
        "Refactor each to use `locations.org_id` before migration 0038.\n\n"
        + "\n".join(violations)
    )
