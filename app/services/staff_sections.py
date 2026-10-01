"""
Canonical role -> Staff App section mapping (single source of truth).

The unified Staff App (`/staff`, `app/static/html/staff.html`) shows one
sidebar item per operational section (Cashier, Waiter, Kitchen, Bar,
Courier). Which sections a
given user's sidebar shows is decided ONLY from this module — both the
`/api/staff/sections` endpoint (app/routes/auth_routes.py) and any other
backend code that needs to reason about role -> section must import from
here rather than re-deriving the mapping.

Section keys match `window.MesioStaffSections` in
app/static/js/staff/staff-shell.js:
    cashier | delivery | waiter | kitchen | bar | courier

`delivery` is the "Domicilios" surface added in the web delivery/pickup wave
(docs/claude/delivery-web.md, chunk 4) — the cashier's queue of delivery/
pickup web orders (accept/reject/assign courier), a NEW section rather than a
filter bolted onto an existing one. Granted to the same roles as `cashier`
(caja/cashier/cajero) plus every admin role, per the locked product decision.

Stored role values are Spanish (see CLAUDE.md): mesero, caja, cocina, bar,
domiciliario, gerente, otro — plus the legacy/WA-era English aliases
(waiter, cashier, cajero, cook, cocinero, delivery) that already appear
scattered through auth_routes.py's `_ROLE_REDIRECT` / `_PAGE_ROLES`. Note the
legacy role alias "delivery" (English for domiciliario/courier, a ROLE name)
is unrelated to the new "delivery" SECTION key (Domicilios) below — same
spelling, different namespace; a role is never compared against a section key.
"""
from __future__ import annotations

ADMIN_ROLES: frozenset[str] = frozenset({"owner", "admin", "gerente"})

# Role (lowercase, as stored) -> the operational section(s) it grants.
# Admin roles are handled separately (they get every section). Most roles
# grant exactly one section; cashier roles additionally grant "delivery"
# (Domicilios) per the locked product decision (docs/claude/delivery-web.md).
_ROLE_TO_SECTION: dict[str, tuple[str, ...]] = {
    "mesero": ("waiter",),
    "waiter": ("waiter",),
    "caja": ("cashier", "delivery"),
    "cashier": ("cashier", "delivery"),
    "cajero": ("cashier", "delivery"),
    "cocina": ("kitchen",),
    "cook": ("kitchen",),
    "cocinero": ("kitchen",),
    "bar": ("bar",),
    "domiciliario": ("courier",),
    "delivery": ("courier",),  # legacy ROLE alias — see module docstring
}

# All operational sections, in the order they should appear in the sidebar.
ALL_OPERATIONAL_SECTIONS: tuple[str, ...] = ("cashier", "delivery", "waiter", "kitchen", "bar", "courier")

# Every section key that can ever be mounted, in sidebar order.
ALL_SECTIONS: tuple[str, ...] = ALL_OPERATIONAL_SECTIONS


def normalize_role(role: str) -> str:
    return (role or "").strip().lower()


def sections_for_roles(roles: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Return the ordered list of section keys a user with these roles may see.

    - owner/admin/gerente see every operational section.
    - Any other recognized role sees only its own operational section(s).
    - An unrecognized/empty role set (e.g. "otro") gets none — the Staff App
      tells them to ask an admin for a role.
    """
    normalized = {normalize_role(r) for r in (roles or []) if normalize_role(r)}

    if normalized & ADMIN_ROLES:
        return list(ALL_SECTIONS)

    sections: list[str] = []
    for role in normalized:
        for section in _ROLE_TO_SECTION.get(role, ()):
            if section not in sections:
                sections.append(section)

    # Keep sidebar order stable regardless of role iteration order.
    return [s for s in ALL_OPERATIONAL_SECTIONS if s in sections]


def default_section_for_roles(roles: list[str] | tuple[str, ...] | set[str]) -> str | None:
    """First section for these roles, or None if the role grants none."""
    sections = sections_for_roles(roles)
    return sections[0] if sections else None
