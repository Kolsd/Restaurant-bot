"""Mesio's price list — the one place in code that names the plans.

Pricing decision 2026-09-30 (docs/claude/status.md #15): a flat price per
sede per month, in COP, with no commission per order or conversation. The
`plan_limits` table mirrors these prices (migration 0101) so SQL can join on
them; everything else — order, what each plan unlocks, the trial, the founder
discount — is read from here.
"""

from datetime import datetime, timezone

PLAN_ORDER: tuple[str, ...] = ("esencial", "restaurante", "pro", "cadena")
PAYING_PLANS: frozenset[str] = frozenset(PLAN_ORDER)

PLAN_NAMES: dict[str, str] = {
    "esencial": "Esencial",
    "restaurante": "Restaurante",
    "pro": "Pro",
    "cadena": "Cadena",
}

# List price per sede per month.
PRICES_COP: dict[str, int] = {
    "esencial": 119_000,
    "restaurante": 249_000,
    "pro": 349_000,
    "cadena": 299_000,
}

# The landing's "15 días gratis" is a trial of the Restaurante plan.
TRIAL_PLAN = "restaurante"

# Founder program: the first 10 restaurants pay 40% off list, frozen for life
# while the subscription stays active (organizations.founder_price_cop).
FOUNDER_DISCOUNT_PCT = 40
FOUNDER_SPOTS = 10

# What each plan unlocks on top of the table flow every plan has
# (QR carta, ordering by tapping dishes, kitchen screen, caja, dashboard).
AI_ASSISTANT = "ai_assistant"   # free-text chat with the bot at the table
DELIVERY = "delivery"           # /pedir delivery and pickup
RESERVATIONS = "reservations"
INVENTORY = "inventory"
DIAN = "dian"                   # electronic invoicing

_FEATURES: dict[str, frozenset[str]] = {
    "esencial": frozenset(),
    "restaurante": frozenset({AI_ASSISTANT, DELIVERY}),
    "pro": frozenset({AI_ASSISTANT, DELIVERY, RESERVATIONS, INVENTORY, DIAN}),
    "cadena": frozenset({AI_ASSISTANT, DELIVERY, RESERVATIONS, INVENTORY, DIAN}),
}

# Active staff users allowed; plans not listed have no cap.
STAFF_CAP: dict[str, int] = {"esencial": 5}

# Which plan a feature first appears in — for "sube a X para usar esto" copy.
FEATURE_MIN_PLAN: dict[str, str] = {
    AI_ASSISTANT: "restaurante",
    DELIVERY: "restaurante",
    RESERVATIONS: "pro",
    INVENTORY: "pro",
    DIAN: "pro",
}


def normalize_plan(plan_code: str | None) -> str:
    """Map a stored plan_code onto the price list; anything unknown is the base plan."""
    code = (plan_code or "").strip().lower()
    return code if code in PAYING_PLANS else PLAN_ORDER[0]


def plan_rank(plan_code: str | None) -> int:
    """Position in PLAN_ORDER (smaller = cheaper plan); unknown plans rank last."""
    code = (plan_code or "").strip().lower()
    return PLAN_ORDER.index(code) if code in PAYING_PLANS else len(PLAN_ORDER)


def founder_price(plan_code: str) -> int:
    """Founder price per sede: list minus the discount, rounded down to the thousand.

    119.000 → 71.000, 249.000 → 149.000 — the numbers the landing prints.
    """
    discounted = PRICES_COP[normalize_plan(plan_code)] * (100 - FOUNDER_DISCOUNT_PCT) // 100
    return discounted // 1000 * 1000


def in_trial(comp_until: datetime | str | None, now: datetime | None = None) -> bool:
    """Whether the org is inside its free days. Repos hand datetimes back as
    ISO strings as often as not, so both are accepted."""
    if not comp_until:
        return False
    if isinstance(comp_until, str):
        comp_until = datetime.fromisoformat(comp_until)
    if comp_until.tzinfo is None:
        # Naive timestamps from the DB are UTC by project convention.
        comp_until = comp_until.replace(tzinfo=timezone.utc)
    return comp_until > (now or datetime.now(tz=timezone.utc))


def effective_plan(plan_code: str | None, comp_until: datetime | str | None,
                   now: datetime | None = None) -> str:
    """The plan whose features apply right now.

    During the free days the restaurant gets at least the trial plan, so a
    restaurant that signed up for Esencial still tries the full Restaurante.
    """
    plan = normalize_plan(plan_code)
    if in_trial(comp_until, now) and plan_rank(plan) < plan_rank(TRIAL_PLAN):
        return TRIAL_PLAN
    return plan


def has_feature(org: dict, feature: str, now: datetime | None = None) -> bool:
    """Whether an org (a row with plan_code and comp_until) has a feature today."""
    plan = effective_plan(org.get("plan_code"), org.get("comp_until"), now)
    return feature in _FEATURES[plan]


def staff_cap(org: dict, now: datetime | None = None) -> int | None:
    """Active staff users allowed today; None means no cap."""
    plan = effective_plan(org.get("plan_code"), org.get("comp_until"), now)
    return STAFF_CAP.get(plan)


def monthly_price_per_sede(plan_code: str | None, founder_price_cop: int | None) -> int:
    """What one sede pays per month: the frozen founder price when there is one."""
    if founder_price_cop:
        return int(founder_price_cop)
    return PRICES_COP[normalize_plan(plan_code)]


def upgrade_message(feature: str) -> str:
    """Spanish copy for a feature the plan does not include."""
    plan = PLAN_NAMES[FEATURE_MIN_PLAN[feature]]
    return f"Tu plan no incluye esta función. Está disponible desde el plan {plan}."
