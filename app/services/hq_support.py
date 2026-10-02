"""
Mesio HQ support helpers that are more than one SQL statement.

send_password_reset: the same code + email as "¿Olvidaste tu contraseña?"
(auth_routes.forgot_password), triggered by Mesio for a user it verified
belongs to the organization. Unlike the public route it reports whether the
email left — Mesio needs to know; there is no enumeration risk here.
"""
from __future__ import annotations

import asyncpg

from app.services.logging import get_logger

log = get_logger(__name__)


async def send_password_reset(email: str, restaurant_name: str | None) -> bool:
    from app.repositories.password_reset_repo import db_create_password_reset  # noqa: PLC0415
    from app.services.email import delivers_for_real, send_email  # noqa: PLC0415
    from app.services.email_templates import render_password_reset_email  # noqa: PLC0415

    try:
        code = await db_create_password_reset(email)
    except asyncpg.PostgresError:
        log.exception("hq_support.password_reset_create_failed", email_prefix=email[:3] + "***")
        return False
    subject, html, text = render_password_reset_email(code=code, restaurant_name=restaurant_name or "tu restaurante")
    sent = await send_email(to=email, subject=subject, html=html, text=text)
    # Without a real provider the "send" is only a log line: report it as
    # not sent so Mesio doesn't tell the owner to check an empty inbox.
    sent = bool(sent) and delivers_for_real()
    log.info("hq_support.password_reset_sent", email_prefix=email[:3] + "***", sent=sent)
    return sent
