"""The key a restaurant is known by when it has no WhatsApp number.

Found 2026-09-25: self-serve signup creates orgs with no WhatsApp number,
and the web flows still key on `restaurants.whatsapp_number` (the bot
runtime's `bot_number`), so those orgs could not open a table. Migration
0096 makes the view fall back to `web<org_id>` — one key per organization,
the same shape as a WhatsApp org whose sedes share its number.

This module is the Python mirror for code that composes the key itself
instead of reading the view, and for the few places that must NOT treat it
as a phone number (a wa.me link, a Meta API call).
"""

from __future__ import annotations

WEB_KEY_PREFIX = "web"


def web_key(org_id: int) -> str:
    return f"{WEB_KEY_PREFIX}{int(org_id)}"


def is_web_key(number: str | None) -> bool:
    """True for a key minted by 0096 — never a real, dialable number."""
    return bool(number) and str(number).startswith(WEB_KEY_PREFIX)


def dialable(number: str | None) -> str:
    """The number for a wa.me link or a Meta call, or "" when there is none."""
    if not number or is_web_key(number):
        return ""
    return str(number)
