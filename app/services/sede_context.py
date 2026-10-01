"""The sede a bot turn is serving, as ambient context next to tenant_scope.

The bot reads the carta deep inside its call chain (find_dish, add_to_cart,
the tool guards), far from where the sede is known. Each sede has its own
carta (migration 0093), so those reads need the sede — and threading a
parameter through every one of them is how a caller ends up forgetting it.

`agent.chat` opens a turn with `begin_turn()`, names the sede once it has
resolved the restaurant (`set_sede`), and closes it with `end_turn()`.
Outside a bot turn `current_sede_id()` is None; routes pass the sede
explicitly instead of relying on this.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

_current_sede: ContextVar[Optional[int]] = ContextVar("mesio_current_sede", default=None)


def begin_turn() -> Token:
    return _current_sede.set(None)


def set_sede(location_id: int | None) -> None:
    _current_sede.set(int(location_id) if location_id else None)


def end_turn(token: Token) -> None:
    _current_sede.reset(token)


def current_sede_id() -> Optional[int]:
    return _current_sede.get()
