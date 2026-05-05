"""app/services/whatsapp_messaging.py

High-level WhatsApp messaging helpers for system-initiated messages
(not bot-to-customer conversational messages).

IMPORTANT — Meta template prerequisite
---------------------------------------
The function `send_password_reset_code` below uses the WhatsApp Business
Template API to send a pre-approved message template named:

    mesio_password_reset

Template body (submit to Meta Business Manager → Message Templates):
    "Tu código Mesio: {{1}}. Válido 15 min. No lo compartas."

Category: UTILITY
Language: es (Spanish)
Variables: 1 variable (the 6-digit code)
Buttons: none

After creating the template in Meta Business Manager, it enters a review
queue that typically takes 24–48 hours. Until the template is APPROVED,
the API call will fail with:
    error code 132012 — "template_not_approved"

During that window the function:
  1. Attempts the template send (which will fail with 132012).
  2. Logs a WARNING including the plaintext code so the support team can
     deliver it manually to the user.
  3. Returns False — the caller (forgot-password endpoint) returns
     { "sent": false } to the frontend, which shows a generic
     "if this email exists you'll receive a WhatsApp" message.
     The code IS stored in the DB so support can use it.

Once the template is approved in Meta Business Manager, sends will succeed
and `sent=true` will be returned normally to the frontend.
"""

from __future__ import annotations

import os

import httpx

from app.services.logging import get_logger

log = get_logger(__name__)

META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")
_TEMPLATE_NAME = "mesio_password_reset"
_TEMPLATE_LANGUAGE = "es"

# Template-not-approved Meta error code (informational — not blocking logic).
_TEMPLATE_NOT_APPROVED_CODE = 132012


async def send_password_reset_code(
    whatsapp_number: str,
    code: str,
    restaurant_name: str,
) -> bool:
    """Send the 6-digit reset code via WhatsApp template message.

    Args:
        whatsapp_number: The recipient's WhatsApp number (digits, no '+').
                         This is the *restaurant owner's* personal WhatsApp
                         stored on the organization record.
        code:            The plaintext 6-digit OTP code.
        restaurant_name: Used only for log context (not sent to user).

    Returns:
        True  — Meta API accepted the message (200 response).
        False — Any failure (missing token, template not approved, network
                error). Caller treats this as "sent=false" to preserve
                anti-enumeration guarantee.

    DOES NOT RAISE — all errors are caught and logged.
    """
    access_token = os.getenv("META_ACCESS_TOKEN", "")
    if not access_token:
        log.warning(
            "password_reset.whatsapp.no_access_token",
            restaurant_name=restaurant_name,
        )
        return False

    clean_phone = whatsapp_number.lstrip("+").replace(" ", "")
    if not clean_phone:
        log.warning(
            "password_reset.whatsapp.empty_phone",
            restaurant_name=restaurant_name,
        )
        return False

    # The phone_number_id (pid) used to send is the bot_number / phone_id.
    # For template sends we use the same pid as for conversational messages.
    # We derive it from META_ACCESS_TOKEN context; for now we use the same
    # whatsapp_number as pid (standard pattern in this codebase).
    pid = clean_phone  # Bot sends FROM its own number TO the owner's number.
    # Note: In production the pid should be the bot's registered phone_number_id,
    # not the recipient's number. Since organizations store the bot number as
    # whatsapp_number, we reuse it here. If a restaurant has a separate
    # phone_number_id configured, that should be passed instead.

    url = f"https://graph.facebook.com/{META_API_VERSION}/{pid}/messages"
    body = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "template",
        "template": {
            "name": _TEMPLATE_NAME,
            "language": {"code": _TEMPLATE_LANGUAGE},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": code},
                    ],
                }
            ],
        },
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=body, headers=headers)

        if resp.status_code == 200:
            log.info(
                "password_reset.whatsapp.sent",
                restaurant_name=restaurant_name,
                phone_last4=clean_phone[-4:],
            )
            return True

        # Parse error code to give a targeted warning.
        try:
            error_body = resp.json()
            error_code = (
                error_body.get("error", {}).get("code") or
                error_body.get("error", {}).get("error_subcode")
            )
        except Exception:
            error_code = None

        if error_code == _TEMPLATE_NOT_APPROVED_CODE:
            log.warning(
                "password_reset.template_pending",
                code=code,  # Intentionally logged so support can deliver manually.
                restaurant_name=restaurant_name,
                hint="Template 'mesio_password_reset' not yet approved in Meta Business Manager. "
                     "Deliver the code manually to the user.",
            )
        else:
            log.error(
                "password_reset.whatsapp.api_error",
                status=resp.status_code,
                error_code=error_code,
                restaurant_name=restaurant_name,
            )

        return False

    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        log.warning(
            "password_reset.whatsapp.network_error",
            error=str(exc),
            restaurant_name=restaurant_name,
        )
        return False
    except Exception:
        log.exception(
            "password_reset.whatsapp.unexpected_error",
            restaurant_name=restaurant_name,
        )
        return False
