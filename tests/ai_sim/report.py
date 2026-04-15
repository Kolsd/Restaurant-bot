"""
tests/ai_sim/report.py — Output writers for the AI simulation harness.

Provides:
  write_jsonl(results, path)   — one JSON line per scenario
  write_html(results, path)    — single-file navigable HTML report
  print_summary(results)       — ASCII table to stdout
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tests.ai_sim.types import ScenarioResult
from app.services.logging import get_logger

log = get_logger(__name__)


# ── JSON serialisation helpers ────────────────────────────────────────────────

def _to_serializable(obj: Any) -> Any:
    """Recursively convert dataclasses, non-JSON types to JSON-safe values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_serializable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(i) for i in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    # asyncpg Record objects and similar
    try:
        return dict(obj)
    except (TypeError, ValueError):
        pass
    return obj


# ── JSONL writer ──────────────────────────────────────────────────────────────

def write_jsonl(results: list[ScenarioResult], path: str | Path) -> None:
    """Write one JSON line per scenario to *path*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        for result in results:
            try:
                line = json.dumps(_to_serializable(result), ensure_ascii=False, default=str)
                f.write(line + "\n")
            except Exception as exc:  # noqa: BLE001
                log.warning("report.jsonl_serialize_error", scenario_id=result.scenario.id, error=str(exc))
                fallback = json.dumps({
                    "scenario_id": result.scenario.id,
                    "passed": result.passed,
                    "error": str(exc),
                })
                f.write(fallback + "\n")

    log.info("report.jsonl_written", path=str(path), count=len(results))


# ── HTML writer ───────────────────────────────────────────────────────────────

_HTML_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e2e8f0; line-height: 1.5; }
.header { padding: 24px 32px; border-bottom: 1px solid #2d3748; }
.header h1 { font-size: 1.5rem; font-weight: 700; color: #63b3ed; }
.header .meta { font-size: 0.85rem; color: #718096; margin-top: 4px; }
.summary-bar { display: flex; gap: 24px; padding: 16px 32px;
               background: #1a202c; border-bottom: 1px solid #2d3748; }
.stat { display: flex; flex-direction: column; align-items: center; }
.stat .val { font-size: 1.8rem; font-weight: 700; }
.stat .lbl { font-size: 0.75rem; color: #718096; text-transform: uppercase; }
.pass-val { color: #68d391; }
.fail-val { color: #fc8181; }
.scenarios { padding: 16px 32px; }
details { background: #1a202c; border: 1px solid #2d3748; border-radius: 8px;
          margin-bottom: 12px; overflow: hidden; }
details[open] { border-color: #4a5568; }
summary { display: flex; align-items: center; gap: 12px; padding: 14px 16px;
          cursor: pointer; list-style: none; user-select: none; }
summary::-webkit-details-marker { display: none; }
summary:hover { background: #2d3748; }
.badge { font-size: 0.75rem; font-weight: 700; padding: 2px 8px; border-radius: 4px;
         text-transform: uppercase; letter-spacing: 0.05em; }
.badge-pass { background: #276749; color: #9ae6b4; }
.badge-fail { background: #742a2a; color: #fc8181; }
.badge-error { background: #5a3e00; color: #f6ad55; }
.scenario-id { font-weight: 600; font-size: 0.95rem; flex: 1; }
.scenario-meta { font-size: 0.8rem; color: #718096; }
.detail-body { padding: 16px; border-top: 1px solid #2d3748; }
.section-title { font-size: 0.75rem; font-weight: 700; text-transform: uppercase;
                 color: #718096; letter-spacing: 0.08em; margin: 16px 0 8px; }
.section-title:first-child { margin-top: 0; }
.transcript { display: flex; flex-direction: column; gap: 8px; }
.turn { display: flex; flex-direction: column; gap: 4px; }
.turn-user { align-self: flex-end; background: #2b4c7e; color: #bee3f8;
             padding: 8px 12px; border-radius: 12px 12px 2px 12px;
             max-width: 80%; font-size: 0.9rem; }
.turn-bot-pass { align-self: flex-start; background: #1c4532; color: #9ae6b4;
                 padding: 8px 12px; border-radius: 12px 12px 12px 2px;
                 max-width: 80%; font-size: 0.9rem; }
.turn-bot-fail { align-self: flex-start; background: #742a2a; color: #fed7d7;
                 padding: 8px 12px; border-radius: 12px 12px 12px 2px;
                 max-width: 80%; font-size: 0.9rem; }
.turn-meta { font-size: 0.75rem; color: #718096; margin-top: 2px; }
.verdict-block { background: #2d3748; border-radius: 6px; padding: 12px;
                 font-size: 0.9rem; }
.failures { list-style: disc; padding-left: 20px; }
.failure-item { color: #fc8181; font-size: 0.88rem; margin: 4px 0; }
.db-block { background: #1e1e2e; border: 1px solid #2d3748; border-radius: 6px;
            padding: 12px; font-family: 'Menlo','Monaco','Courier New',monospace;
            font-size: 0.8rem; color: #a0aec0; white-space: pre-wrap; overflow-x: auto; }
.score-bar-wrap { background: #2d3748; border-radius: 4px; height: 8px; margin: 8px 0; }
.score-bar { height: 8px; border-radius: 4px; }
"""


def _esc(s: str) -> str:
    """Minimal HTML escaping."""
    return (s
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _scenario_html(result: ScenarioResult) -> str:
    s = result.scenario
    j = result.judge
    a = result.asserts

    verdict_badge_cls = {
        "PASS": "badge-pass",
        "FAIL": "badge-fail",
        "ERROR": "badge-error",
    }.get(j.verdict, "badge-error")

    # Transcript
    transcript_parts = []
    for t in result.transcript:
        is_error = "[ERROR:" in t.bot or t.bot == "[SILENT]"
        bot_cls = "turn-bot-fail" if is_error else "turn-bot-pass"
        transcript_parts.append(
            f'<div class="turn">'
            f'<div class="turn-user">{_esc(t.user)}</div>'
            f'<div class="{bot_cls}">{_esc(t.bot)}</div>'
            f'<div class="turn-meta">{t.latency_ms}ms</div>'
            f'</div>'
        )
    transcript_html = "\n".join(transcript_parts)

    # Score bar color
    score_color = "#68d391" if j.score >= 70 else "#f6ad55" if j.score >= 40 else "#fc8181"
    score_pct = max(0, min(100, j.score))

    # Assertion failures
    failures_html = ""
    if not a.all_passed:
        items = "".join(f'<li class="failure-item">{_esc(f)}</li>' for f in a.failures)
        failures_html = f'<div class="section-title">Assertion Failures</div><ul class="failures">{items}</ul>'

    # Violated rules
    rules_html = ""
    if j.violated_rules:
        items = "".join(f'<li class="failure-item">{_esc(r)}</li>' for r in j.violated_rules)
        rules_html = f'<div class="section-title">Violated Rules</div><ul class="failures">{items}</ul>'

    # Critical failures
    crit_html = ""
    if j.critical_failures:
        items = "".join(f'<li class="failure-item">{_esc(c)}</li>' for c in j.critical_failures)
        crit_html = f'<div class="section-title">Critical Failures</div><ul class="failures">{items}</ul>'

    # DB summary
    db = result.db_state
    db_summary = (
        f"table_orders: {len(db.table_orders)} | orders: {len(db.orders)} | "
        f"carts: {len(db.carts)} | waiter_alerts: {len(db.waiter_alerts)} | "
        f"reservations: {len(db.reservations)}\n"
        f"inbox: total={db.webhook_inbox_stats.get('total',0)} "
        f"pending={db.webhook_inbox_stats.get('pending',0)} "
        f"dead={db.webhook_inbox_stats.get('dead_letters',0)}\n"
        f"tokens_used: {db.subscription_usage.get('total_tokens', 0)}"
    )

    return f"""
<details {"open" if not result.passed else ""}>
  <summary>
    <span class="badge {verdict_badge_cls}">{_esc(j.verdict)}</span>
    <span class="scenario-id">{_esc(s.id)}</span>
    <span class="scenario-meta">{_esc(s.suite)} | {_esc(s.description[:60])} | {result.total_latency_ms}ms</span>
  </summary>
  <div class="detail-body">
    <div class="section-title">Verdict (score: {j.score}/100)</div>
    <div class="score-bar-wrap"><div class="score-bar" style="width:{score_pct}%;background:{score_color}"></div></div>
    <div class="verdict-block">{_esc(j.explanation)}</div>
    {crit_html}
    {rules_html}
    {failures_html}
    <div class="section-title">Transcript ({len(result.transcript)} turns)</div>
    <div class="transcript">{transcript_html}</div>
    <div class="section-title">DB State After Scenario</div>
    <div class="db-block">{_esc(db_summary)}</div>
  </div>
</details>
"""


def write_html(results: list[ScenarioResult], path: str | Path) -> None:
    """Write a single-file HTML report to *path*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    total_ms = sum(r.total_latency_ms for r in results)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    scenarios_html = "\n".join(_scenario_html(r) for r in results)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mesio AI Sim Report — {now}</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<div class="header">
  <h1>Mesio AI Simulation Report</h1>
  <div class="meta">Generated: {now}</div>
</div>
<div class="summary-bar">
  <div class="stat"><span class="val">{total}</span><span class="lbl">Total</span></div>
  <div class="stat"><span class="val pass-val">{passed}</span><span class="lbl">Passed</span></div>
  <div class="stat"><span class="val fail-val">{failed}</span><span class="lbl">Failed</span></div>
  <div class="stat"><span class="val">{total_ms // 1000}s</span><span class="lbl">Total Time</span></div>
</div>
<div class="scenarios">
{scenarios_html}
</div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    log.info("report.html_written", path=str(path))


# ── ASCII summary ─────────────────────────────────────────────────────────────

def print_summary(results: list[ScenarioResult]) -> None:
    """Print an ASCII table to stdout with one row per scenario."""
    if not results:
        print("No results to display.")
        return

    col_id = max(len(r.scenario.id) for r in results)
    col_id = max(col_id, 12)

    header = (
        f"{'scenario_id':<{col_id}}  "
        f"{'suite':<12}  "
        f"{'verdict':<7}  "
        f"{'score':>5}  "
        f"{'ms':>6}  "
        f"notes"
    )
    sep = "-" * (col_id + 50)

    print()
    print(sep)
    print(header)
    print(sep)

    total_ms = 0
    passed_count = 0
    total_tokens = 0

    for r in results:
        verdict = r.judge.verdict
        verdict_display = verdict
        if r.passed:
            passed_count += 1

        notes_parts = []
        if r.asserts.failures:
            notes_parts.append(f"{len(r.asserts.failures)} assert fail(s)")
        if r.judge.critical_failures:
            notes_parts.append(f"{len(r.judge.critical_failures)} critical")
        notes = "; ".join(notes_parts) if notes_parts else ""

        total_ms += r.total_latency_ms
        total_tokens += r.tokens_estimate

        print(
            f"{r.scenario.id:<{col_id}}  "
            f"{r.scenario.suite:<12}  "
            f"{verdict_display:<7}  "
            f"{r.judge.score:>5}  "
            f"{r.total_latency_ms:>6}  "
            f"{notes}"
        )

    print(sep)

    # Cost estimate: rough $0.003 per 1K chars (Sonnet-class model blended)
    cost_usd = (total_tokens / 1000) * 0.003
    total_s = total_ms / 1000

    print(
        f"\n{passed_count}/{len(results)} passed  |  "
        f"total time {total_s:.1f}s  |  "
        f"tokens ~{total_tokens:,}  |  "
        f"estimated cost ~${cost_usd:.2f}"
    )
    print()
