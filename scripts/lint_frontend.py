"""
Frontend lint — catches bugs the Python test suite can't see.

Checks:
  1. MOCK         — mock/fake/dummy/lorem/ipsum keywords in JS code.
  2. TODO         — TODO/FIXME/XXX/HACK markers (stale deferrals become live bugs).
  3. FETCH        — fetch('/api/...') paths that don't match any registered FastAPI route.
  4. TOKEN-DRIFT  — hardcoded brand color #1D9E75 outside tokens.css.
                    Strict (fail CI): dashboard.css, shared.css.
                    Warning-only (report, don't fail): pages/*.css, inline HTML <style>.
                    Suppression in CSS: /* lint-allow: reason */ on the same line.
                    Suppression in HTML: <!-- lint-allow: reason --> on the same line.

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
CSS_DIR = ROOT / "app" / "static" / "css"

# ── TOKEN-DRIFT configuration ─────────────────────────────────────────────────
# The canonical brand color.  Any occurrence outside tokens.css is a drift.
BRAND_HEX_RE = re.compile(r"#1[Dd]9[Ee]75", re.IGNORECASE)

# Source of truth — this file is the ONLY place #1D9E75 is allowed to appear
# as a literal value.
TOKEN_DRIFT_EXEMPT = {
    CSS_DIR / "tokens.css",
}

# CSS files where violations are ERRORS (fail CI).
# These are the "compiled" shared stylesheets that have been fully tokenized.
TOKEN_DRIFT_STRICT_CSS = {
    CSS_DIR / "dashboard.css",
    CSS_DIR / "shared.css",
}

# CSS suppression marker (same line):  /* lint-allow: reason */
CSS_ALLOW_MARKER = re.compile(r"/\*\s*lint-allow:\s*(\S.+?)\s*\*/")

# ─────────────────────────────────────────────────────────────────────────────

# Files that are allowed to contain mock data by design.
# These power the public marketing demo at /demo and /dashboard-demo.
# Not served to real admins.
MOCK_FILE_ALLOWLIST = {
    "mesio-demo-bus.js",
    "mesio-demo-orchestrator.js",
    "mesio-demo-scenarios.js",
    "dashboard-demo-mesio.js",
}

# HTML pages exempt from seed-data checks — these are marketing/demo surfaces
# where fake names and hardcoded numbers are the point (landing, sales demo).
HTML_SEED_EXEMPT = {
    "landing.html",
    "demo.html",
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


# ── PAGE-CONTRACTS ────────────────────────────────────────────────────────────
# Declares the MINIMUM set of load-bearing UI anchors that MUST exist in each
# operational page's JS render code.  "Load-bearing" means: if this string
# disappears the page renders flat / non-functional for the role that uses it.
#
# Format:
#   html_file: {
#     "js": "relative path under app/static/js/",
#     "required_button_labels": list of button text literals that must appear,
#     "required_fetches":       list of fetch URL substrings that must appear,
#   }
#
# Rules:
#   - 3-5 anchors per page max.  Only truly load-bearing ones.
#   - NO lint-allow suppression is supported for PAGE-CONTRACTS violations —
#     the check exists precisely because suppressing it trivially is dangerous.
#   - When you add a new page, add a contract here.  When you rename a button,
#     update the contract.
PAGE_CONTRACTS: dict[str, dict] = {
    "domiciliario.html": {
        "js": "pages/domiciliario.js",
        "required_button_labels": [
            "Salir a entregar",
            "Llegué",
            "Entregado",
            "Tomar",
        ],
        "required_fetches": [
            "/api/delivery/orders",
            "/status",
        ],
    },
    "mesero.html": {
        "js": "pages/mesero.js",
        "required_button_labels": [
            "Cobrar mesa",
            "Mandar a caja",
            "Confirmar mesa real",
            "Marcar entregado",
        ],
        "required_fetches": [
            "/api/pos/tables-status",
            "/api/table-orders",
            "/api/waiter-alerts",
        ],
    },
    "caja.html": {
        "js": "pages/caja.js",
        "required_button_labels": [
            "Cobrar total completo",
            "Cobrar este check",
            "Dividir cuenta",
        ],
        "required_fetches": [
            "/api/pos/tables-status",
            "/api/pos/menu",
            "/api/pos/order",
        ],
    },
    "kitchen.html": {
        "js": "pages/kitchen.js",
        "required_button_labels": [
            "Listo",
            "+ 2 min",
        ],
        "required_fetches": [
            "/api/table-orders",
            "/api/kitchen/delivery-orders",
        ],
    },
    "bar.html": {
        "js": "pages/bar.js",
        "required_button_labels": [
            "Listo",
            "+2 min",
        ],
        "required_fetches": [
            "/api/table-orders",
        ],
    },
    "staff-hq.html": {
        "js": "pages/staff-clock.js",
        "required_button_labels": [
            "Confirmar entrada",
            "Confirmar salida",
            "Entrada registrada",
            "Salida registrada",
        ],
        "required_fetches": [
            "/api/staff/self/profile",
            "/api/staff/self/clock-in",
            "/api/staff/self/clock-out",
        ],
    },
    "settings.html": {
        "js": "pages/settings.js",
        # Labels are JS strings that must exist in settings.js to prove wiring:
        # saveBtnTop/js-save-btn = Guardar changes; kiosko-copy-btn = copy kiosko URL; btnPause = danger zone
        "required_button_labels": [
            "saveBtnTop",
            "kiosko-copy-btn",
            "btnPause",
        ],
        "required_fetches": [
            "/api/settings",
        ],
    },
    "equipo.html": {
        "js": "pages/equipo.js",
        # Labels are JS identifier strings that must exist to prove wiring:
        # inviteBtn = Invitar miembro handler; membersTbody = team roster render target
        "required_button_labels": [
            "inviteBtn",
            "membersTbody",
        ],
        "required_fetches": [
            "/api/staff",
            "/api/staff/schedules",
        ],
    },
    "fidelizacion.html": {
        "js": "pages/fidelizacion.js",
        # Labels are JS getElementById strings that must exist to prove handler wiring:
        # btn-new-campaign = Nueva campaña; btn-configure = Configurar programa; campaigns-list = render target
        "required_button_labels": [
            "btn-new-campaign",
            "btn-configure",
            "campaigns-list",
        ],
        "required_fetches": [
            "/api/loyalty/aggregates",
            "/api/loyalty/segments",
            "/api/loyalty/campaigns",
        ],
    },
}


def check_page_contracts(violations: list[Violation]) -> None:
    """Check that each operational page's JS contains required load-bearing anchors.

    For every entry in PAGE_CONTRACTS we read the corresponding JS file and do
    simple substring search for each required button label and fetch URL.  A
    missing anchor means the page almost certainly renders flat or broken for
    the role that depends on it — exactly the class of bug that escaped CI when
    domiciliario.html was deployed with its render code gutted.

    Violations are appended to the provided list in-place.  Error format:
        PAGE-CONTRACTS [<html>]: missing required button label '<label>'
        PAGE-CONTRACTS [<html>]: missing required fetch '<url>'

    No lint-allow suppression is supported for this check.
    """
    js_root = JS_DIR  # app/static/js/

    for html_name, contract in PAGE_CONTRACTS.items():
        js_rel = contract["js"]          # e.g. "pages/domiciliario.js"
        js_path = js_root / js_rel
        if not js_path.exists():
            violations.append(Violation(
                file=str(js_path),
                line=0,
                kind="PAGE-CONTRACTS",
                message=(
                    f"[{html_name}]: JS file '{js_rel}' does not exist — "
                    "cannot verify page contracts"
                ),
            ))
            continue

        source = js_path.read_text(encoding="utf-8")

        for label in contract.get("required_button_labels", []):
            if label not in source:
                violations.append(Violation(
                    file=str(js_path),
                    line=0,
                    kind="PAGE-CONTRACTS",
                    message=(
                        f"[{html_name}]: missing required button label "
                        f"'{label}' in {js_rel} — page would render flat for "
                        f"the role that uses this page"
                    ),
                ))

        for fetch_url in contract.get("required_fetches", []):
            if fetch_url not in source:
                violations.append(Violation(
                    file=str(js_path),
                    line=0,
                    kind="PAGE-CONTRACTS",
                    message=(
                        f"[{html_name}]: missing required fetch '{fetch_url}' "
                        f"in {js_rel} — status-update or data-load call absent"
                    ),
                ))


def _css_line_is_allowed(line: str) -> str | None:
    """Return the allow-reason if the CSS line has a /* lint-allow: reason */ comment."""
    m = CSS_ALLOW_MARKER.search(line)
    return m.group(1) if m else None


def check_token_drift_css(path: Path, source: str) -> list[Violation]:
    """Flag hardcoded brand color #1D9E75 in a CSS file.

    Returns Violation objects for every un-suppressed occurrence.
    Suppression: add `/* lint-allow: reason */` on the same line.
    The path must NOT be in TOKEN_DRIFT_EXEMPT (caller's responsibility).
    """
    out: list[Violation] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if not BRAND_HEX_RE.search(line):
            continue
        if _css_line_is_allowed(line):
            continue
        if _html_line_is_allowed(line):  # handles <!-- lint-allow --> in HTML <style>
            continue
        out.append(Violation(
            file=str(path),
            line=i,
            kind="TOKEN-DRIFT",
            message=(
                "hardcoded brand color #1D9E75 — use var(--brand) instead. "
                "Suppress with: /* lint-allow: reason */"
            ),
        ))
    return out


def check_token_drift_html_inline(path: Path, source: str) -> list[Violation]:
    """Flag hardcoded brand color #1D9E75 inside inline <style> blocks in HTML.

    Only scans content inside <style>…</style> tags (case-insensitive).
    Suppression: add <!-- lint-allow: reason --> on the same line.
    """
    out: list[Violation] = []
    # Track whether we're inside a <style> block.
    in_style = False
    for i, line in enumerate(source.splitlines(), start=1):
        lower = line.lower()
        if "<style" in lower:
            in_style = True
        if "</style" in lower:
            in_style = False
            # Still check this closing line — it might contain a color on the
            # same line as </style> in minified HTML (rare but possible).
        if not in_style and "</style" not in lower:
            continue
        if not BRAND_HEX_RE.search(line):
            continue
        if _html_line_is_allowed(line):
            continue
        if _css_line_is_allowed(line):
            continue
        out.append(Violation(
            file=str(path),
            line=i,
            kind="TOKEN-DRIFT",
            message=(
                "hardcoded brand color #1D9E75 in inline <style> — use var(--brand) instead. "
                "Suppress with: <!-- lint-allow: reason -->"
            ),
        ))
    return out


def run_token_drift_check(
    strict_violations: list[Violation],
    warning_violations: list[Violation],
) -> None:
    """Scan CSS files and HTML inline styles for TOKEN-DRIFT violations.

    Results are sorted into two lists:
      strict_violations — files in TOKEN_DRIFT_STRICT_CSS (will fail CI).
      warning_violations — all other files (reported but don't fail CI).

    Both lists are mutated in-place.
    """
    # Scan all CSS files under app/static/css/ (recursive).
    for css_path in sorted(CSS_DIR.rglob("*.css")):
        if css_path in TOKEN_DRIFT_EXEMPT:
            continue
        source = css_path.read_text(encoding="utf-8")
        violations = check_token_drift_css(css_path, source)
        if css_path in TOKEN_DRIFT_STRICT_CSS:
            strict_violations.extend(violations)
        else:
            warning_violations.extend(violations)

    # Scan inline <style> blocks in all HTML files under app/static/html/.
    for html_path in sorted(HTML_DIR.rglob("*.html")):
        source = html_path.read_text(encoding="utf-8")
        violations = check_token_drift_html_inline(html_path, source)
        # HTML inline styles are always warning-only (not in strict set).
        warning_violations.extend(violations)


def run() -> list[Violation]:
    """Return all STRICT violations (fail CI).

    TOKEN-DRIFT warnings from non-strict files are handled separately in main()
    via run_token_drift_check() so they can be reported without causing failure.
    """
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

    check_page_contracts(all_violations)

    # TOKEN-DRIFT: strict files only (non-strict are warnings surfaced in main()).
    token_strict: list[Violation] = []
    token_warnings: list[Violation] = []
    run_token_drift_check(token_strict, token_warnings)
    all_violations.extend(token_strict)

    # Attach the warning list as a module-level side-effect so main() can
    # retrieve it without re-running the scan.
    run._token_drift_warnings = token_warnings  # type: ignore[attr-defined]

    return all_violations


def main() -> int:
    violations = run()
    token_warnings: list[Violation] = getattr(run, "_token_drift_warnings", [])

    # ── Print TOKEN-DRIFT warnings (non-strict files) ──────────────────────────
    if token_warnings:
        by_file: dict[str, list[Violation]] = {}
        for v in token_warnings:
            by_file.setdefault(v.file, []).append(v)

        print(
            f"[TOKEN-DRIFT] WARNING — {len(token_warnings)} hardcoded #1D9E75 occurrences "
            f"in {len(by_file)} non-strict file(s) (not failing CI; clean up incrementally):"
        )
        shown = 0
        for f in sorted(by_file):
            count = len(by_file[f])
            rel = f.replace(str(ROOT) + os.sep, "").replace("\\", "/")
            print(f"  {rel}: {count} occurrence(s)")
            if shown < 5:
                for v in by_file[f][:max(0, 5 - shown)]:
                    print(f"    line {v.line}: {v.message}")
                shown += count
        print()

    # ── Strict violations ──────────────────────────────────────────────────────
    if not violations:
        print(f"[lint_frontend] OK — 0 violations across {sum(1 for _ in iter_js_files())} JS files.")
        return 0

    # Group by file for readable output.
    by_file_strict: dict[str, list[Violation]] = {}
    for v in violations:
        by_file_strict.setdefault(v.file, []).append(v)

    counts: dict[str, int] = {}
    for v in violations:
        counts[v.kind] = counts.get(v.kind, 0) + 1

    print(f"[lint_frontend] {len(violations)} violations ({counts})\n")
    for f in sorted(by_file_strict):
        for v in by_file_strict[f]:
            print(v.format())
    summary_parts = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"\nTotal: {len(violations)}  ({summary_parts})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
