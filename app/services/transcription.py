"""Voice note transcription via OpenAI Whisper API.

Used by the inbox worker (inbox_worker.py) to convert WhatsApp audio messages
into text before feeding them through the existing _process_message pipeline.

Never called from the webhook route — the webhook only enqueues.
"""
from __future__ import annotations

import io
import os
from typing import Optional

import httpx

from app.services.logging import get_logger

log = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

MAX_AUDIO_BYTES = 16 * 1024 * 1024   # 16 MB — Meta's documented limit for audio
WHISPER_TIMEOUT_S = 15.0
WHISPER_RETRIES = 2
WHISPER_MODEL = "whisper-1"

# Transient HTTP status codes worth retrying on
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


# ── Typed exceptions ───────────────────────────────────────────────────────────

class TranscriptionError(Exception):
    """Transient / retryable failure (network timeout, 5xx, etc.)."""


class TranscriptionUnavailable(Exception):
    """OPENAI_API_KEY is not configured. Do NOT retry — send fallback to user."""


class AudioTooLongError(Exception):
    """Audio exceeds MAX_AUDIO_BYTES. Do NOT retry — send fallback to user."""


# ── Media download ─────────────────────────────────────────────────────────────

async def download_whatsapp_media(media_id: str, access_token: str) -> tuple[bytes, str]:
    """Download audio from Meta Graph API.

    Two-step:
      1. GET /v{VERSION}/{media_id}  → JSON with {url, mime_type}
      2. GET {url} with Bearer token → raw bytes

    Returns (audio_bytes, mime_type).
    Raises TranscriptionError on HTTP errors or network failures.
    Never logs the signed download URL.
    """
    api_version = os.getenv("META_API_VERSION", "v20.0")
    metadata_url = f"https://graph.facebook.com/{api_version}/{media_id}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            # Step 1: fetch metadata to get the signed download URL
            meta_res = await client.get(metadata_url, headers=headers)
            if meta_res.status_code != 200:
                raise TranscriptionError(
                    f"Meta media metadata fetch failed: HTTP {meta_res.status_code}"
                )
            data = meta_res.json()
            download_url = data.get("url")
            mime_type = data.get("mime_type", "audio/ogg")
            if not download_url:
                raise TranscriptionError(
                    f"Meta media metadata missing 'url' field for media_id={media_id}"
                )

            # Step 2: download the actual audio bytes (URL is pre-signed, short-lived)
            audio_res = await client.get(download_url, headers=headers)
            if audio_res.status_code != 200:
                raise TranscriptionError(
                    f"Meta audio download failed: HTTP {audio_res.status_code}"
                )

            audio_bytes = audio_res.content
            log.info(
                "audio.downloaded",
                media_id=media_id,
                bytes=len(audio_bytes),
                mime_type=mime_type,
            )
            return audio_bytes, mime_type

    except TranscriptionError:
        raise
    except httpx.TimeoutException as exc:
        raise TranscriptionError(f"Timeout downloading audio media_id={media_id}: {exc}") from exc
    except Exception as exc:
        raise TranscriptionError(
            f"Unexpected error downloading audio media_id={media_id}: {exc}"
        ) from exc


# ── Whisper transcription ──────────────────────────────────────────────────────

async def transcribe_audio(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    language_hint: Optional[str] = None,
) -> str:
    """Transcribe audio bytes via OpenAI Whisper API.

    Returns the stripped transcript string (may be empty if Whisper heard nothing).

    Raises:
        TranscriptionUnavailable  — OPENAI_API_KEY not set (do not retry)
        AudioTooLongError         — audio_bytes > MAX_AUDIO_BYTES (do not retry)
        TranscriptionError        — transient failure after WHISPER_RETRIES attempts
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise TranscriptionUnavailable(
            "OPENAI_API_KEY is not configured; cannot transcribe audio"
        )

    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise AudioTooLongError(
            f"Audio is {len(audio_bytes)} bytes, exceeds limit of {MAX_AUDIO_BYTES}"
        )

    # Map WhatsApp MIME types to file extensions Whisper accepts
    _MIME_TO_EXT = {
        "audio/ogg": "ogg",
        "audio/ogg; codecs=opus": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4": "mp4",
        "audio/wav": "wav",
        "audio/webm": "webm",
        "audio/aac": "aac",
    }
    # Normalise mime_type (strip params after first ';' for lookup fallback)
    mime_clean = mime_type.split(";")[0].strip().lower()
    ext = _MIME_TO_EXT.get(mime_type, _MIME_TO_EXT.get(mime_clean, "ogg"))
    filename = f"audio.{ext}"

    last_exc: Exception = TranscriptionError("No attempts made")

    for attempt in range(1, WHISPER_RETRIES + 2):  # 1, 2, 3
        try:
            async with httpx.AsyncClient(timeout=WHISPER_TIMEOUT_S) as client:
                files = {
                    "file": (filename, io.BytesIO(audio_bytes), mime_type),
                    "model": (None, WHISPER_MODEL),
                }
                if language_hint:
                    files["language"] = (None, language_hint)

                res = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files=files,
                )

                if res.status_code == 200:
                    text = res.json().get("text", "").strip()
                    log.info(
                        "audio.transcribed",
                        bytes=len(audio_bytes),
                        chars=len(text),
                        attempt=attempt,
                    )
                    return text

                if res.status_code in _RETRYABLE_STATUSES:
                    last_exc = TranscriptionError(
                        f"Whisper API returned HTTP {res.status_code} on attempt {attempt}"
                    )
                    log.warning(
                        "audio.transcription_retry",
                        status=res.status_code,
                        attempt=attempt,
                        max_attempts=WHISPER_RETRIES + 1,
                    )
                    # Continue to next attempt
                else:
                    # Non-retryable 4xx (e.g. 400 bad format, 401 bad key)
                    log.error(
                        "audio.transcription_failed",
                        status=res.status_code,
                        body=res.text[:200],
                        attempt=attempt,
                    )
                    raise TranscriptionError(
                        f"Whisper API non-retryable error HTTP {res.status_code}: {res.text[:200]}"
                    )

        except TranscriptionError:
            raise
        except TranscriptionUnavailable:
            raise
        except AudioTooLongError:
            raise
        except httpx.TimeoutException as exc:
            last_exc = TranscriptionError(
                f"Whisper API timeout on attempt {attempt}: {exc}"
            )
            log.warning(
                "audio.transcription_timeout",
                attempt=attempt,
                max_attempts=WHISPER_RETRIES + 1,
            )
        except Exception as exc:
            last_exc = TranscriptionError(
                f"Unexpected error calling Whisper on attempt {attempt}: {exc}"
            )
            log.warning(
                "audio.transcription_unexpected_error",
                attempt=attempt,
                error=str(exc),
            )

    # All attempts exhausted
    log.error("audio.transcription_failed", attempts=WHISPER_RETRIES + 1, error=str(last_exc))
    raise last_exc
