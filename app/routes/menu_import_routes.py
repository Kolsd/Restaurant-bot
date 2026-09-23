"""
app/routes/menu_import_routes.py

POST /api/menu/import — read a carta from a photo or from pasted text and
return it as a DRAFT for the menu editor.

This endpoint writes nothing. It has no database call other than the token
accounting for the LLM call it makes, and it deliberately does not share a
path with `PUT /api/menu/update`: the owner reviews the draft in the editor
that already exists and saves it there, through the same validation every
hand-typed dish goes through. A misread price is then a line to fix in an
unsaved draft, not a price change on a live carta.

Same permission as editing the base carta — owner/admin only. A gerente
edits their own sede's carta and does not get to redraw the one every sede
inherits.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.routes.deps import get_current_restaurant, get_current_user, may_span_locations, require_auth
from app.services import state_store
from app.services.logging import get_logger
from app.services.menu_import import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    MAX_TEXT_CHARS,
    MenuImportError,
    parse_menu,
)

log = get_logger(__name__)

router = APIRouter()

# An import is one LLM call over a whole document — the most expensive thing
# a logged-in owner can trigger by holding down a button. Per org, not per
# IP: the cost lands on the org either way.
_IMPORTS_PER_HOUR = 20


class MenuImportPayload(BaseModel):
    """Either `text` or `image_b64` + `image_type`. Never both."""

    text: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    image_b64: str | None = None
    image_type: str | None = None


@router.post("/api/menu/import")
async def import_menu(payload: MenuImportPayload, request: Request):
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)

    if not may_span_locations(user):
        raise HTTPException(
            status_code=403,
            detail="Solo el dueño o un admin pueden importar la carta general.",
        )

    org_id = restaurant["id"]
    allowed = await state_store.rate_limit_check(
        f"menu_import:{org_id}", max_requests=_IMPORTS_PER_HOUR, window_seconds=3600
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Demasiadas importaciones seguidas. Intenta de nuevo en un rato.",
        )

    has_text = bool((payload.text or "").strip())
    has_image = bool(payload.image_b64)
    if has_text == has_image:
        raise HTTPException(
            status_code=400,
            detail="Envía el texto de la carta o una foto, no ambos.",
        )

    if has_image:
        if payload.image_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=400,
                detail="Ese formato de imagen no se puede leer. Usa JPG o PNG.",
            )
        # Measured on the DECODED bytes: base64 inflates by ~33%, and the
        # limit we care about is what gets sent upstream.
        try:
            raw_size = len(base64.b64decode(payload.image_b64, validate=True))
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="La imagen no se pudo leer.")
        if raw_size > MAX_IMAGE_BYTES:
            mb = MAX_IMAGE_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=f"La foto pesa más de {mb} MB. Tómala con menos resolución.",
            )

    try:
        menu = await parse_menu(
            text=payload.text,
            image_b64=payload.image_b64,
            image_type=payload.image_type,
            org_id=org_id,
        )
    except MenuImportError as exc:
        # 422: the request was fine, the document was not readable. The page
        # shows the reason next to a still-usable editor.
        raise HTTPException(status_code=422, detail=exc.reason) from exc

    log.info(
        "menu_import.draft_returned",
        org_id=org_id,
        source="image" if has_image else "text",
        dishes=menu.dish_count,
    )
    return {
        "ok": True,
        # Same shape as GET /api/dashboard/menu, so the editor loads a draft
        # exactly as it loads the saved carta.
        "menu": menu.to_editor_payload(),
        "dish_count": menu.dish_count,
        "warnings": menu.warnings,
        # Nothing has been written. The page says so, and the owner saves
        # from the editor like any other change.
        "saved": False,
    }
