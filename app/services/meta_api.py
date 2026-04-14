"""
app/services/meta_api.py

Centralized Meta Graph API messaging helpers.

Provides send_text and send_image for sending WhatsApp messages via the
Meta Graph API. Both functions include retry logic (3 attempts with
exponential backoff) for transient errors (429, 503, 5xx, timeouts).
"""
from __future__ import annotations

import asyncio
import os

import httpx

from app.services.logging import get_logger

log = get_logger(__name__)

META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds


async def send_text(
    bot_number: str,
    access_token: str,
    phone: str,
    text: str,
    phone_id: str | None = None,
) -> bool:
    """
    Send a WhatsApp text message via Meta Graph API.

    Args:
        bot_number: WhatsApp bot phone number (used to derive phone_id if not given).
        access_token: Bearer token for Meta API.
        phone: Recipient phone number (digits only, no '+').
        text: Message body.
        phone_id: Override Meta phone_number_id. Defaults to bot_number if not provided.

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    pid = phone_id or bot_number.lstrip("+")
    clean_phone = phone.lstrip("+").replace(" ", "")
    url = f"https://graph.facebook.com/{META_API_VERSION}/{pid}/messages"
    body = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "text",
        "text": {"body": text},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200:
                return True
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                log.warning(
                    "meta_api.send_text.retry",
                    attempt=attempt,
                    status=resp.status_code,
                    phone=phone,
                )
                await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
                continue
            log.error(
                "meta_api.send_text.failed",
                status=resp.status_code,
                phone=phone,
                bot_number=bot_number,
            )
            return False
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt < _MAX_RETRIES:
                log.warning(
                    "meta_api.send_text.timeout_retry",
                    attempt=attempt,
                    phone=phone,
                    error=str(exc),
                )
                await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
                continue
            log.exception(
                "meta_api.send_text.failed",
                phone=phone,
                bot_number=bot_number,
            )
            return False
        except Exception:
            log.exception(
                "meta_api.send_text.unexpected_error",
                phone=phone,
                bot_number=bot_number,
            )
            return False

    return False


async def send_image(
    bot_number: str,
    access_token: str,
    phone: str,
    image_url: str,
    caption: str | None = None,
    phone_id: str | None = None,
) -> bool:
    """
    Send a WhatsApp image message via Meta Graph API.

    Args:
        bot_number: WhatsApp bot phone number (used to derive phone_id if not given).
        access_token: Bearer token for Meta API.
        phone: Recipient phone number (digits only, no '+').
        image_url: Publicly accessible URL of the image.
        caption: Optional caption shown below the image (max 1024 chars).
        phone_id: Override Meta phone_number_id. Defaults to bot_number if not provided.

    Returns:
        True if the image was sent successfully, False otherwise.
    """
    pid = phone_id or bot_number.lstrip("+")
    clean_phone = phone.lstrip("+").replace(" ", "")
    url = f"https://graph.facebook.com/{META_API_VERSION}/{pid}/messages"
    image_body: dict = {"link": image_url}
    if caption:
        image_body["caption"] = caption[:1024]
    body = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "image",
        "image": image_body,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200:
                return True
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                log.warning(
                    "meta_api.send_image.retry",
                    attempt=attempt,
                    status=resp.status_code,
                    phone=phone,
                )
                await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
                continue
            log.error(
                "meta_api.send_image.failed",
                status=resp.status_code,
                phone=phone,
                bot_number=bot_number,
            )
            return False
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt < _MAX_RETRIES:
                log.warning(
                    "meta_api.send_image.timeout_retry",
                    attempt=attempt,
                    phone=phone,
                    error=str(exc),
                )
                await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
                continue
            log.exception(
                "meta_api.send_image.failed",
                phone=phone,
                bot_number=bot_number,
            )
            return False
        except Exception:
            log.exception(
                "meta_api.send_image.unexpected_error",
                phone=phone,
                bot_number=bot_number,
            )
            return False

    return False
