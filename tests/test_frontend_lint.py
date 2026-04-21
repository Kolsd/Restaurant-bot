"""
Runs scripts/lint_frontend.py as a pytest test so CI blocks regressions.

Three checks enforced (see scripts/lint_frontend.py for details):
  1. MOCK   — mock/fake/dummy keywords in admin JS (demo-data.js is exempt)
  2. TODO   — TODO/FIXME/XXX/HACK markers (force explicit handling)
  3. FETCH  — fetch('/api/...') paths that don't match any registered route

To suppress a specific line, add at the end:
    // lint-allow: short reason why this is intentional
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lint_frontend import run as run_lint  # noqa: E402


def test_frontend_has_no_lint_violations():
    violations = run_lint()
    if violations:
        lines = [v.format() for v in violations]
        header = (
            f"\n{len(violations)} frontend lint violation(s):\n"
            "  MOCK  = suspicious mock/fake keyword\n"
            "  TODO  = unresolved TODO/FIXME/XXX/HACK\n"
            "  FETCH = fetch(path) with no matching FastAPI route\n\n"
            "Fix the underlying issue, or add `// lint-allow: <reason>` "
            "at the end of the line if it's genuinely intentional.\n"
        )
        raise AssertionError(header + "\n".join(lines))
