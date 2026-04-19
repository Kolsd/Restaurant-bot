"""
Forward guard: ensure no SQL query string in app/ uses `is_primary` as a
filter, ordering, or write target.

After migration 0038 the column will be dropped entirely.  This test catches
accidental re-introduction at CI time.

Only string literals that look like SQL (contain a SQL keyword: SELECT, FROM,
WHERE, JOIN, UPDATE, INSERT, DELETE) are checked.  Pure Python usages such as
dict key accesses (.get("is_primary")), function parameter names, docstrings,
and comments are intentionally ignored.

ALLOWED SQL occurrences (explicit exceptions):
- SELECT columns: ``is_primary`` appearing only after SELECT or in a column list
  with no operator (i.e. just SELECTing the value, not filtering by it).
  The allowed pattern covers ``SELECT ..., is_primary, ...`` and
  ``RETURNING ..., is_primary, ...`` but NOT ``WHERE is_primary`` /
  ``ORDER BY is_primary`` / ``SET is_primary`` / ``is_primary =``.
"""

import ast
import re
import textwrap
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent / "app"

# Any mention of is_primary in a SQL string
_COLUMN_RE = re.compile(r'is_primary', re.IGNORECASE)

# A string is treated as SQL only when it contains at least one unambiguous SQL
# keyword that would not appear in ordinary English prose.  We require multi-word
# SQL constructs (SELECT …, INSERT INTO, DELETE FROM, UPDATE …, WHERE …) to
# reduce false positives from module docstrings that contain words like "SET".
_SQL_KEYWORD_RE = re.compile(
    r'(?:'
    r'SELECT\s+[\w\*]'          # SELECT col / SELECT *
    r'|INSERT\s+INTO\s+\w'      # INSERT INTO table
    r'|DELETE\s+FROM\s+\w'      # DELETE FROM table
    r'|UPDATE\s+\w+\s+SET\s+\w' # UPDATE table SET col
    r'|WHERE\s+\w'              # WHERE col
    r'|JOIN\s+\w'               # JOIN table
    r'|RETURNING\s+\w'          # RETURNING col
    r')',
    re.IGNORECASE,
)

# Forbidden patterns — filter / ordering / write uses of is_primary in SQL.
_FORBIDDEN_PATTERNS = [
    re.compile(r'WHERE\b[^;]*\bis_primary\b', re.IGNORECASE | re.DOTALL),
    re.compile(r'ORDER\s+BY\b[^;]*\bis_primary\b', re.IGNORECASE | re.DOTALL),
    re.compile(r'\bSET\b[^;]*\bis_primary\b', re.IGNORECASE | re.DOTALL),
    re.compile(r'\bis_primary\s*=', re.IGNORECASE),
    re.compile(r'=\s*is_primary\b', re.IGNORECASE),
    re.compile(r'\bAND\s+is_primary\b', re.IGNORECASE),
    re.compile(r'\bOR\s+is_primary\b', re.IGNORECASE),
]


def _is_sql(s: str) -> bool:
    return bool(_SQL_KEYWORD_RE.search(s))


def _is_forbidden(s: str) -> bool:
    return any(p.search(s) for p in _FORBIDDEN_PATTERNS)


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


def test_no_is_primary_filter_in_sql_strings():
    violations: list[str] = []

    for py_file in sorted(APP_ROOT.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8", errors="replace")

        for lineno, literal_value in _collect_string_literals(source):
            if (
                _COLUMN_RE.search(literal_value)
                and _is_sql(literal_value)
                and _is_forbidden(literal_value)
            ):
                excerpt = textwrap.shorten(literal_value, width=120, placeholder="…")
                rel = py_file.relative_to(APP_ROOT.parent)
                violations.append(f"{rel}:{lineno}  →  {excerpt!r}")

    assert not violations, (
        "SQL query string(s) that filter/order/write the vestigial column `is_primary` found.\n"
        "Refactor each before migration 0038 drops the column.\n\n"
        + "\n".join(violations)
    )
