"""
Frontend lint — catches bugs the Python test suite can't see.

Three checks:
  1. MOCK   — mock/fake/dummy/lorem/ipsum keywords in JS code.
  2. TODO   — TODO/FIXME/XXX/HACK markers (stale deferrals become live bugs).
  3. FETCH  — fetch('/api/...') paths that don't match any registered FastAPI route.

Run manually:
    python scripts/lint_frontend.py

Run in CI (preferred):
    pytest tests/test_frontend_lint.py -v

Allow a violation by adding `// lint-allow: <short reason>` at the end of the
line (same line, not the line above). The reason MUST be non-empty — this
prevents silent suppressions.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "app" / "static" / "js"
HTML_DIR = ROOT / "app" / "static" / "html"

# Files that are allowed to contain mock data by design.
# demo-data.js powers the public marketing demo at /demo-chat; it intercepts
# window.fetch so the demo runs without a backend. Not served to real admins.
MOCK_FILE_ALLOWLIST = {
    "demo-data.js",
    "mesio-demo-bus.js",
    "mesio-demo-orchestrator.js",
    "mesio-demo-scenarios.js",
}

# HTML pages exempt from seed-data checks — these are marketing/demo surfaces
# where fake names and hardcoded numbers are the point (landing, sales demo).
HTML_SEED_EXEMPT = {
    "landing.html",
    "demo-chat.html",
    "dashboard-demo.html",
    "privacidad.html",
    "terminos.html",
    "menu.html",  # public QR menu — restaurants edit content via admin
    "dish_page.html",  # public dish deep-link
}

# Spanish first names commonly used as fake seed data in admin pages.
# Restaurant-authored content (own staff, real customers) won't trigger in
# static HTML because it comes from the DB. If a real page needs to hardcode
# one (error messages, team org chart), suppress with `<!-- lint-allow: ... -->`.
SPANISH_NAME_RE = re.compile(
    r"\b("
    r"Ana|Andrea|Andr[eé]s|Camila|Camilo|Carlos|Carolina|Catalina|"
    r"Daniel|Diana|Diego|Felipe|Fernanda|Jorge|Juan|Juliana|Laura|"
    r"Luis|Manuela|Marcela|Mar[íi]a|Miguel|Paola|Pedro|Ricardo|Roberto|"
    r"Sebasti[aá]n|Sof[íi]a|Valentina"
    r")\b"
)

# Hardcoded money patterns suspicious in admin HTML (e.g. $142.8M, $86k, $1.84M).
# Matches `$NNk`, `$N.NM`, `$NN.NM` inside HTML content (between > and <).
MONEY_LITERAL_RE = re.compile(r">[^<]*\$\d+(?:\.\d+)?[kM]\b")

HTML_ALLOW_MARKER = re.compile(r"<!--\s*lint-allow:\s*(\S.+?)\s*-->")

# Extra path prefixes that are valid even though they don't appear in FastAPI
# routes (static assets, external URLs, templated values we can't resolve).
EXTERNAL_PATH_PREFIXES = (
    "/static/",
    "/health",  # liveness probe
    "http://",
    "https://",
)

ALLOW_MARKER = re.compile(r"//\s*lint-allow:\s*(\S.+?)\s*$")
MOCK_WORDS = re.compile(r"\b(mock|fake|dummy|lorem|ipsum)\b", re.IGNORECASE)
TODO_WORDS = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")

# Match fetch('/api/...') and fetch(`/api/...`) — single/double/backtick.
# Captures the literal path portion up to the first quote, ?, or whitespace.
FETCH_CALL = re.compile(
    r"""fetch\s*\(\s*['"`](?P<path>/[^'"`\s?]+)""",
)

# _staffFetch('/foo') is a helper that prefixes '/api/staff'.
STAFF_FETCH_CALL = re.compile(
    r"""_staffFetch\s*\(\s*['"`](?P<path>/[^'"`\s?]*)""",
)


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    kind: str  # MOCK | TODO | FETCH
    message: str

    def format(self) -> str:
        rel = self.file.replace(str(ROOT) + os.sep, "").replace("\\", "/")
        return f"{rel}:{self.line} [{self.kind}] {self.message}"


def iter_js_files() -> Iterable[Path]:
    for p in sorted(JS_DIR.rglob("*.js")):
        # Skip service worker (it's its own beast, no mocks, no fetches we lint)
        if p.name == "sw.js":
            continue
        yield p


def _line_is_allowed(line: str) -> str | None:
    """Return the allow-reason if the line ends with `// lint-allow: <reason>`."""
    m = ALLOW_MARKER.search(line)
    return m.group(1) if m else None


def _line_is_comment_only(line: str) -> bool:
    """True if the line is a pure // comment or inside a block comment marker."""
    stripped = line.lstrip()
    return stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*")


def check_mock_keywords(path: Path, source: str) -> list[Violation]:
    if path.name in MOCK_FILE_ALLOWLIST:
        return []
    out: list[Violation] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if not MOCK_WORDS.search(line):
            continue
        if _line_is_allowed(line):
            continue
        out.append(Violation(
            file=str(path),
            line=i,
            kind="MOCK",
            message=f"suspicious keyword in: {line.strip()[:120]}",
        ))
    return out


def check_todo_markers(path: Path, source: str) -> list[Violation]:
    out: list[Violation] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if not TODO_WORDS.search(line):
            continue
        if _line_is_allowed(line):
            continue
        out.append(Violation(
            file=str(path),
            line=i,
            kind="TODO",
            message=f"unresolved marker: {line.strip()[:120]}",
        ))
    return out


def _normalize_path(p: str) -> str:
    """Collapse `${var}` and `{var}` both sides to `{PARAM}` for comparison."""
    p = p.split("?", 1)[0]
    p = re.sub(r"\$\{[^}]+\}", "{PARAM}", p)
    p = re.sub(r"\{[^}]+\}", "{PARAM}", p)
    p = p.rstrip("/")
    return p or "/"


def _load_backend_routes() -> set[str]:
    """Import the FastAPI app and harvest every registered path."""
    # Ensure minimal env so `from app.main import app` succeeds outside tests.
    # Track which keys we are injecting so we can clean them up afterwards —
    # leaving dummy values in os.environ pollutes integration tests that run
    # after this function (e.g. test_health.py::TestHealthIntegration skips
    # correctly when DATABASE_URL is absent but *fails* when it finds the
    # dummy 127.0.0.1 URL and actually attempts a TCP connection).
    _injected: list[str] = []

    def _setdefault_tracking(key: str, value: str) -> None:
        if key not in os.environ:
            os.environ[key] = value
            _injected.append(key)

    _setdefault_tracking("ANTHROPIC_API_KEY", "sk-lint-dummy")
    _setdefault_tracking("META_APP_SECRET", "lint-dummy")
    _setdefault_tracking("ADMIN_KEY", "lint-dummy")
    _setdefault_tracking("DATABASE_URL", "postgresql://u:p@127.0.0.1/lint")
    # Enable ALL routers so every fetch('/api/…') in the frontend has a chance
    # to match.  Without this, the default _DEFAULT_DISABLED list in main.py
    # skips loyalty, reviews, webauthn, etc. and the lint check flags valid
    # calls as dead fetches.
    _setdefault_tracking("DISABLED_MODULES", "")

    sys.path.insert(0, str(ROOT))
    from app.main import app  # noqa: E402

    routes: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        routes.add(_normalize_path(path))

    # Clean up any dummy env vars we injected so they don't bleed into
    # subsequent test code (e.g. health integration tests).
    for key in _injected:
        os.environ.pop(key, None)

    return routes


def _route_segments_match(frontend: str, backend: str) -> bool:
    """Segment-wise match where backend `{PARAM}` matches any single segment."""
    fs = frontend.split("/")
    bs = backend.split("/")
    if len(fs) != len(bs):
        return False
    for f, b in zip(fs, bs):
        if b == "{PARAM}":
            continue
        if f != b:
            return False
    return True


def check_dead_fetches(path: Path, source: str, backend_routes: set[str]) -> list[Violation]:
    out: list[Violation] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if _line_is_allowed(line):
            continue

        # (raw, normalized, ends_with_slash)
        candidates: list[tuple[str, str, bool]] = []
        for m in FETCH_CALL.finditer(line):
            raw = m.group("path")
            if any(raw.startswith(pfx) for pfx in EXTERNAL_PATH_PREFIXES):
                continue
            candidates.append((raw, _normalize_path(raw), raw.endswith("/")))

        for m in STAFF_FETCH_CALL.finditer(line):
            raw = "/api/staff" + m.group("path")
            candidates.append((raw, _normalize_path(raw), raw.endswith("/")))

        for raw, norm, ends_slash in candidates:
            # 1. Exact match (no params).
            if norm in backend_routes:
                continue
            # 2. Segment-wise match (backend has {PARAM} placeholders).
            if any(_route_segments_match(norm, r) for r in backend_routes):
                continue
            # 3. Frontend path was truncated by string concat: `'/api/x/' + id`.
            #    The raw captured ends with `/`; accept if any backend route
            #    lives under this prefix. Match prefix literally, ignoring
            #    backend {PARAM} segments.
            if ends_slash:
                prefix = norm + "/"
                if any(r.startswith(prefix) for r in backend_routes):
                    continue
                # Also accept when the backend segment immediately under this
                # prefix is a {PARAM}: `/api/foo/{PARAM}` matches `/api/foo/` concat.
                matched_via_param = False
                for r in backend_routes:
                    r_parts = r.split("/")
                    n_parts = norm.split("/")
                    if len(r_parts) >= len(n_parts) + 1 and r_parts[:len(n_parts)] == n_parts:
                        matched_via_param = True
                        break
                if matched_via_param:
                    continue
            out.append(Violation(
                file=str(path),
                line=i,
                kind="FETCH",
                message=f"no FastAPI route matches '{raw}' (normalized '{norm}')",
            ))
    return out


def iter_admin_html_files() -> Iterable[Path]:
    """Yield admin HTML pages (excluding marketing/demo surfaces)."""
    for p in sorted(HTML_DIR.rglob("*.html")):
        rel = p.relative_to(HTML_DIR).as_posix()
        # Skip exempt root-level files.
        if p.name in HTML_SEED_EXEMPT:
            continue
        # Skip the internal/ subdirectory — those are Mesio team tools, not
        # restaurant-facing. Less critical if they flash fake data briefly.
        if rel.startswith("internal/"):
            continue
        yield p


def _html_line_is_allowed(line: str) -> str | None:
    m = HTML_ALLOW_MARKER.search(line)
    return m.group(1) if m else None


def check_html_seed_names(path: Path, source: str) -> list[Violation]:
    """Flag hardcoded Spanish first names in admin HTML.

    These almost always represent fake seed data pretending to be real
    clients/staff/customers. Real data flows from the DB via JS fetches.
    """
    out: list[Violation] = []
    for i, line in enumerate(source.splitlines(), start=1):
        m = SPANISH_NAME_RE.search(line)
        if not m:
            continue
        if _html_line_is_allowed(line):
            continue
        # Allow placeholder attributes (form inputs show example names).
        if 'placeholder="' in line and m.group(0) in line.split('placeholder="', 1)[1].split('"', 1)[0]:
            continue
        out.append(Violation(
            file=str(path),
            line=i,
            kind="HTML-SEED",
            message=f"hardcoded name '{m.group(0)}' — wire via JS fetch or add '<!-- lint-allow: reason -->'",
        ))
    return out


def check_html_seed_money(path: Path, source: str) -> list[Violation]:
    """Flag `$N.NM` / `$NNk` literals inside HTML content (not inside attributes).

    Real money values should be injected by JS. Hardcoded ones flash on load
    and persist if fetch fails.
    """
    out: list[Violation] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if not MONEY_LITERAL_RE.search(line):
            continue
        if _html_line_is_allowed(line):
            continue
        # Skip aria/tooltip labels that legitimately explain the currency format.
        if 'aria-label' in line or 'title="' in line:
            continue
        # Extract the matched text for a clearer error message.
        snippet = line.strip()[:120]
        out.append(Violation(
            file=str(path),
            line=i,
            kind="HTML-SEED",
            message=f"hardcoded money literal in: {snippet}",
        ))
    return out


def run() -> list[Violation]:
    all_violations: list[Violation] = []
    backend_routes = _load_backend_routes()

    for js_file in iter_js_files():
        source = js_file.read_text(encoding="utf-8")
        all_violations.extend(check_mock_keywords(js_file, source))
        all_violations.extend(check_todo_markers(js_file, source))
        all_violations.extend(check_dead_fetches(js_file, source, backend_routes))

    for html_file in iter_admin_html_files():
        source = html_file.read_text(encoding="utf-8")
        all_violations.extend(check_html_seed_names(html_file, source))
        all_violations.extend(check_html_seed_money(html_file, source))

    return all_violations


def main() -> int:
    violations = run()
    if not violations:
        print(f"[lint_frontend] OK — 0 violations across {sum(1 for _ in iter_js_files())} JS files.")
        return 0

    # Group by file for readable output.
    by_file: dict[str, list[Violation]] = {}
    for v in violations:
        by_file.setdefault(v.file, []).append(v)

    counts = {"MOCK": 0, "TODO": 0, "FETCH": 0}
    for v in violations:
        counts[v.kind] = counts.get(v.kind, 0) + 1

    print(f"[lint_frontend] {len(violations)} violations ({counts})\n")
    for f in sorted(by_file):
        for v in by_file[f]:
            print(v.format())
    print(f"\nTotal: {len(violations)}  (MOCK={counts['MOCK']} TODO={counts['TODO']} FETCH={counts['FETCH']})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
