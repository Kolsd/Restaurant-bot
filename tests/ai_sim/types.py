"""
tests/ai_sim/types.py — Shared dataclasses for the AI simulation harness.

These types form the contract between the harness (runner, judge, assertions,
report) and the scenario files (mesa, delivery, pickup, adversarial).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Turn:
    """A single scripted conversation turn."""

    user: str
    """The message the customer sends."""

    expect_bot_contains: list[str] = field(default_factory=list)
    """Soft hint for judge: bot reply SHOULD contain at least one of these
    strings (case-insensitive). Empty list = no hint."""

    expect_bot_not_contains: list[str] = field(default_factory=list)
    """Bot reply MUST NOT contain any of these strings (case-insensitive).
    Violations are recorded as hard failures in AssertionResult."""


@dataclass
class ExpectedState:
    """Per-scenario DB assertions. A missing/None field = 'don't care'."""

    table_orders_count: Optional[int] = None
    """Expected number of rows in table_orders for this user/bot pair."""

    orders_count: Optional[int] = None
    """Expected number of external delivery/pickup orders for this user."""

    carts_empty_after: bool = True
    """Default True: the cart should be empty (or non-existent) at scenario end."""

    waiter_alerts_count: Optional[int] = None
    """Expected number of waiter_alerts rows for this bot's restaurant."""

    waiter_alerts_types: list[str] = field(default_factory=list)
    """Expected alert types that must be present, e.g. ["bill", "assistance"]."""

    reservations_count: Optional[int] = None
    """Expected number of reservations for this bot's restaurant."""

    reservations_status: Optional[str] = None
    """Expected status of the latest reservation, e.g. "pending"."""

    items_in_orders: list[str] = field(default_factory=list)
    """Dish names (case-insensitive substring) that MUST appear in committed orders."""

    items_not_in_orders: list[str] = field(default_factory=list)
    """Dish names (case-insensitive substring) that MUST NOT appear in any order."""

    inbox_dead_letters: int = 0
    """Number of webhook_inbox dead-letter rows. Default 0 (zero tolerance)."""

    tokens_used_gt_zero: bool = True
    """True = subscription_usage.total_tokens must be > 0 after the scenario."""


@dataclass
class Scenario:
    """Full description of one simulation scenario."""

    id: str
    """Unique stable identifier, e.g. "mesa_01_flujo_completo"."""

    suite: str
    """Suite grouping: "mesa" | "delivery" | "pickup" | "adversarial"."""

    description: str
    """Human-readable description of what this scenario tests."""

    mode: str
    """Bot mode: "table" (dine-in, salon tools) | "external" (delivery/pickup)."""

    user_phone: str
    """Simulated customer phone, e.g. "+573000000001"."""

    bot_number: str
    """WhatsApp number of the seeded restaurant, must match seed row."""

    table_hint: Optional[str] = None
    """If set (e.g. "1"), the runner prefixes the first turn with
    "Estoy en la mesa {table_hint}. " unless the turn already mentions "mesa"."""

    turns: list[Turn] = field(default_factory=list)
    """Ordered list of scripted conversation turns."""

    expected_state: ExpectedState = field(default_factory=ExpectedState)
    """DB assertions evaluated after all turns complete."""

    judge_criteria: list[str] = field(default_factory=list)
    """Business rules expressed as English sentences. The judge evaluates
    these explicitly, e.g. "Bot must never confirm an order it did not place"."""


@dataclass
class TurnResult:
    """Result of executing a single turn."""

    user: str
    """The message that was sent."""

    bot: str
    """The bot's response text (or "[SILENT]" / "[ERROR: ...]")."""

    raw_result: Any
    """Full dict returned by agent.chat(), or None on error."""

    latency_ms: int
    """Wall-clock milliseconds from chat() call to return."""


@dataclass
class DBSnapshot:
    """Point-in-time DB state captured after all turns complete."""

    table_orders: list[dict]
    orders: list[dict]
    carts: list[dict]
    waiter_alerts: list[dict]
    reservations: list[dict]
    conversations: list[dict]
    webhook_inbox_stats: dict
    """Keys: total, pending, dead_letters."""
    subscription_usage: dict
    """Keys: total_tokens, total_invoices (or empty dict if no row)."""


@dataclass
class JudgeVerdict:
    """LLM judge evaluation result."""

    verdict: str
    """'PASS' | 'FAIL' | 'ERROR'."""

    score: int
    """0–100 quality score from the judge."""

    violated_rules: list[str]
    """Rules from judge_criteria or global bot rules that were violated."""

    critical_failures: list[str]
    """Severe issues that immediately caused FAIL verdict."""

    explanation: str
    """Free-text explanation from the judge model."""


@dataclass
class AssertionResult:
    """Result of hard DB assertions."""

    all_passed: bool
    failures: list[str]
    """Human-readable failure descriptions."""


@dataclass
class ScenarioResult:
    """Complete result of running one scenario end-to-end."""

    scenario: Scenario
    transcript: list[TurnResult]
    db_state: DBSnapshot
    judge: JudgeVerdict
    asserts: AssertionResult
    passed: bool
    """True only if judge.verdict == "PASS" AND asserts.all_passed."""
    total_latency_ms: int
    tokens_estimate: int
    """Rough token estimate: sum of user+bot message lengths for cost display."""
