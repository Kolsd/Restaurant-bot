"""How a restaurant names itself to a human.

PM decision 2026-09-20: each sede is its own restaurant, so a diner has to be
able to tell WHICH one they are dealing with — but only when there is more
than one. A single-sede restaurant, which is almost all of them, shows its
plain name and nothing else; a suffix is noise until it disambiguates
something.

The source of truth is the `restaurants` view's `display_name` column
(migration 0092). This module is the in-Python mirror for the paths that
build a restaurant dict by merging an organization row with a location row
instead of reading the view — notably
`app/routes/diner.py::_resolve_diner_restaurant`, which deliberately avoids
the ambiguous by-id lookup. Keep the two in step: same separator, same
guards.
"""

from __future__ import annotations

SEDE_SEPARATOR = " · "


def restaurant_display_name(
    org_name: str | None,
    location_name: str | None,
    sede_count: int,
) -> str:
    """"Marca · Sede" when the org runs several sedes, "Marca" otherwise.

    Guards, mirroring the view:
      - one sede (or an unknown count) → just the brand;
      - a blank sede name → just the brand, since it disambiguates nothing;
      - a sede named after the org → just the brand, so "Mesio Demo" inside
        "Mesio Demo" does not render twice. Seeds and older onboarding do
        exactly that.
    """
    brand = (org_name or "").strip() or (location_name or "").strip()
    sede = (location_name or "").strip()

    if sede_count is None or sede_count <= 1 or not sede:
        return brand
    if sede.lower() == brand.lower():
        return brand
    return f"{brand}{SEDE_SEPARATOR}{sede}"
