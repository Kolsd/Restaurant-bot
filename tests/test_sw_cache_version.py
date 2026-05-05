"""Service worker freshness check.

If you modified static assets without bumping sw.js, returning users will
get stale JS/CSS from their cache after the deploy. This test catches the
omission at lint/CI time.
"""
import subprocess
import pytest
from pathlib import Path


def _git_diff_names(rev: str = "HEAD~1") -> set[str]:
    """Files changed in the most recent commit, relative to repo root."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", f"{rev}..HEAD"],
            text=True,
        )
        return {line.strip() for line in out.splitlines() if line.strip()}
    except subprocess.CalledProcessError:
        return set()


def test_sw_version_bumped_when_static_changes():
    """If static assets changed in the last commit, sw.js must have changed too.

    Acceptable to skip when running on a fresh clone with no diff history.
    """
    changed = _git_diff_names()
    if not changed:
        pytest.skip("no commit diff available (fresh clone or initial commit)")

    static_changes = {
        f for f in changed
        if f.startswith("app/static/")
        and not f.endswith("sw.js")
        and not f.endswith(".md")
    }
    if not static_changes:
        return  # nothing to enforce

    sw_changed = "app/static/js/sw.js" in changed
    assert sw_changed, (
        f"Static assets changed but app/static/js/sw.js was NOT updated.\n"
        f"Bump CACHE_VERSION in sw.js so returning users don't get stale cached JS/CSS.\n"
        f"Changed static files: {sorted(static_changes)}"
    )
