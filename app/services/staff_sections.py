"""
Canonical role -> Staff App section mapping (single source of truth).

The unified Staff App (`/staff`, `app/static/html/staff.html`) shows one
sidebar item per operational section (Cashier, Waiter, Kitchen, Bar,
Courier) plus "My shift" for everyone with a staff login. Which sections a
given user's sidebar shows is decided ONLY from this module — both the
`/api/staff/sections` endpoint (app/routes/auth_routes.py) and any other
backend code that needs to reason about role -> section must import from
here rather than re-deriving the mapping.

Section keys match `window.MesioStaffSections` in
app/static/js/staff/staff-shell.js:
    cashier | waiter | kitchen | bar | courier | myshift

Stored role values are Spanish (see CLAUDE.md): mesero, caja, cocina, bar,
domiciliario, gerente, otro — plus the legacy/WA-era English aliases
(waiter, cashier, cajero, cook, cocinero, delivery) that already appear
scattered through auth_routes.py's `_ROLE_REDIRECT` / `_PAGE_ROLES`.
"""
from __future__ import annotations

ADMIN_ROLES: frozenset[str] = frozenset({"owner", "admin", "gerente"})

# Role (lowercase, as stored) -> single operational section it grants.
# Admin roles are handled separately (they get every section).
_ROLE_TO_SECTION: dict[str, str] = {
    "mesero": "waiter",
    "waiter": "waiter",
    "caja": "cashier",
    "cashier": "cashier",
    "cajero": "cashier",
    "cocina": "kitchen",
    "cook": "kitchen",
    "cocinero": "kitchen",
    "bar": "bar",
    "domiciliario": "courier",
    "delivery": "courier",
}

# All operational sections, in the order they should appear in the sidebar.
ALL_OPERATIONAL_SECTIONS: tuple[str, ...] = ("cashier", "waiter", "kitchen", "bar", "courier")

# Every section key that can ever be mounted, in sidebar order.
ALL_SECTIONS: tuple[str, ...] = ALL_OPERATIONAL_SECTIONS + ("myshift",)


def normalize_role(role: str) -> str:
    return (role or "").strip().lower()


def sections_for_roles(roles: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Return the ordered list of section keys a user with these roles may see.

    - Any recognized role (operational or admin) always includes "myshift" —
      every staff login sees "My shift" per the locked product decision.
    - owner/admin/gerente see every operational section plus "myshift".
    - Any other recognized role sees only its own operational section(s).
    - An unrecognized/empty role set still gets "myshift" alone (e.g. "otro").
    """
    normalized = {normalize_role(r) for r in (roles or []) if normalize_role(r)}

    if normalized & ADMIN_ROLES:
        return list(ALL_SECTIONS)

    sections: list[str] = []
    for role in normalized:
        section = _ROLE_TO_SECTION.get(role)
        if section and section not in sections:
            sections.append(section)

    # Keep sidebar order stable regardless of role iteration order.
    ordered = [s for s in ALL_OPERATIONAL_SECTIONS if s in sections]
    ordered.append("myshift")
    return ordered


def default_section_for_roles(roles: list[str] | tuple[str, ...] | set[str]) -> str:
    """First operational section for these roles, or "myshift" if none."""
    sections = sections_for_roles(roles)
    for s in sections:
        if s != "myshift":
            return s
    return "myshift"
