"""
app/services/onboarding.py

What a restaurant still has to do before it can sell through Mesio.

Mesio already had an onboarding checklist — five stages behind
`/api/internal/admin/organizations/{id}/onboarding`, for the Mesio team to
watch a customer stall. The restaurant itself never saw anything: an owner
logged in for the first time, landed on a dashboard full of zeros, and had
to guess what to do. This is the same question asked from the other side,
and it is a different list because it answers "what do I do next", not
"where is this account stuck".

Three rules shaped it:

  - Only steps the product can actually VERIFY. "Print the QR codes" is
    not a step, because nothing here can know whether paper came out of a
    printer; it is an action hanging off the step that can be verified
    (having tables). A checklist that ticks itself off on faith trains
    people to ignore it.
  - Every step carries where to go. A checklist that says what is missing
    without saying where to fix it is a nag, not help.
  - Optional steps are marked optional. A one-person dark kitchen has no
    team to invite, and a list that calls that "incomplete" forever is
    telling the truth about the data and lying about the situation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.logging import get_logger

log = get_logger(__name__)


@dataclass
class Action:
    label: str
    href: str
    # True opens in a new tab — used for the print sheet, which the owner
    # sends to a printer and then comes back from.
    external: bool = False

    def to_json(self) -> dict:
        return {"label": self.label, "href": self.href, "external": self.external}


@dataclass
class Step:
    key: str
    title: str
    detail: str
    done: bool
    actions: list[Action] = field(default_factory=list)
    optional: bool = False

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "detail": self.detail,
            "done": self.done,
            "optional": self.optional,
            "actions": [a.to_json() for a in self.actions],
        }


def count_dishes(menu: object) -> int:
    """Dishes in an `organizations.menu` structure, tolerant of junk.

    The menu is JSON written by the editor, so it is well formed in
    practice — but this decides whether a restaurant is told its carta is
    ready, and getting that wrong on a malformed value is worse than
    counting zero.
    """
    if not isinstance(menu, dict):
        return 0
    total = 0
    for dishes in menu.values():
        if isinstance(dishes, list):
            total += sum(1 for d in dishes if isinstance(d, dict) and str(d.get("name") or "").strip())
    return total


def trial_days_left(comp_until: datetime | None) -> int | None:
    """Whole days left on the trial, or None when there is no trial.

    Rounded UP: with 20 hours left a restaurant has "1 día", not "0 días".
    Zero means it has run out, which the page says differently.
    """
    if comp_until is None:
        return None
    if comp_until.tzinfo is None:
        comp_until = comp_until.replace(tzinfo=timezone.utc)
    remaining = comp_until - datetime.now(tz=timezone.utc)
    if remaining.total_seconds() <= 0:
        return 0
    return int(-(-remaining.total_seconds() // 86400))


def build_steps(*, dish_count: int, table_count: int, staff_count: int, order_count: int) -> list[Step]:
    """The checklist itself. Pure — the counts come from the caller."""
    return [
        Step(
            key="menu",
            title="Sube tu carta",
            detail=(
                f"{dish_count} platos cargados." if dish_count
                else "Sin carta, el chat no tiene nada que ofrecer a tus clientes."
            ),
            done=dish_count > 0,
            actions=[
                Action("Editar carta", "/menu-admin"),
                Action("Importar desde una foto", "/menu-admin"),
            ],
        ),
        Step(
            key="tables",
            title="Crea tus mesas e imprime los códigos",
            detail=(
                f"{table_count} mesa(s) creada(s). Imprime sus códigos y pega uno en cada mesa."
                if table_count
                else "Cada mesa necesita su propio código QR para que el cliente pueda pedir."
            ),
            done=table_count > 0,
            actions=(
                [Action("Ver mesas", "/floorplan"),
                 Action("Imprimir códigos QR", "/api/tables/qr-sheet", external=True)]
                if table_count else
                [Action("Crear mesas", "/floorplan")]
            ),
        ),
        Step(
            key="team",
            title="Invita a tu equipo",
            detail=(
                f"{staff_count} persona(s) en tu equipo."
                if staff_count
                else "Tus meseros y cocina entran con su propio usuario. Opcional si atiendes solo."
            ),
            done=staff_count > 0,
            optional=True,
            actions=[Action("Gestionar equipo", "/team")],
        ),
        Step(
            key="first_order",
            title="Recibe tu primer pedido",
            detail=(
                "¡Listo! Ya recibiste pedidos por Mesio."
                if order_count
                else "Escanea tú mismo un código para probar el flujo completo antes de abrir."
            ),
            done=order_count > 0,
            actions=[Action("Ver pedidos", "/orders")],
        ),
    ]


async def get_checklist(org_id: int, location_id: int | None = None) -> dict:
    """Assemble the owner's checklist. Never raises.

    A count that cannot be read is reported as 0, i.e. "not done yet": the
    worst outcome is a step that stays ticked off when it should not, and
    of the two failure modes, telling someone to check something they
    already did is much cheaper than telling them they are finished when
    they are not.
    """
    from app.repositories import onboarding_repo, restaurant_repo, sede_menu_repo  # noqa: PLC0415

    dish_count = 0
    try:
        menu = await sede_menu_repo.db_get_org_menu(org_id)
        dish_count = count_dishes(menu)
    except Exception:
        log.exception("onboarding.menu_read_failed", org_id=org_id)

    counts = {"tables": 0, "staff": 0, "orders": 0}
    try:
        counts = await onboarding_repo.db_onboarding_counts(org_id, location_id)
    except Exception:
        log.exception("onboarding.counts_failed", org_id=org_id)

    comp_until = None
    try:
        org = await restaurant_repo.db_get_org_by_id(org_id)
        comp_until = (org or {}).get("comp_until")
        if isinstance(comp_until, str):
            comp_until = datetime.fromisoformat(comp_until.replace("Z", "+00:00"))
    except Exception:
        log.exception("onboarding.org_read_failed", org_id=org_id)

    steps = build_steps(
        dish_count=dish_count,
        table_count=counts["tables"],
        staff_count=counts["staff"],
        order_count=counts["orders"],
    )

    # Required steps only: a dark kitchen that never invites anyone should
    # be able to reach 100%, not sit at 75% forever.
    required = [s for s in steps if not s.optional]
    done = sum(1 for s in required if s.done)

    return {
        "steps": [s.to_json() for s in steps],
        "done": done,
        "total": len(required),
        "complete": done == len(required),
        "trial_days_left": trial_days_left(comp_until),
    }
