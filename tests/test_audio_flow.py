"""
tests/test_audio_flow.py

Test suite for Feature 1: Audio / Voice Notes (WhatsApp → Whisper transcription).

Covers three layers:
  - Webhook enqueue tests (POST /api/webhook/meta via TestClient)
  - Worker transcription tests (_handle_meta_whatsapp called directly via asyncio)
  - Unit tests for transcription.py helpers (no network)

Patterns reused from test_full_flow.py / test_orders_flow.py:
  - make_pool / make_row / patch_auth helpers from conftest
  - _meta_payload / _meta_sig helpers for signed webhook payloads
  - asyncio.get_event_loop().run_until_complete(...) for async worker calls
    (NOT pytest.mark.asyncio — matches existing suite convention)
  - monkeypatch.setattr on module-level names for mocking
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import httpx

from fastapi.testclient import TestClient
from app.main import app
from app.services import database as db

from tests.conftest import make_pool, make_row, patch_auth


# ── Shared helpers ────────────────────────────────────────────────────────────

_SECRET = "test_audio_secret"
_BOT_NUMBER = "+573009876543"
_USER_PHONE = "573001234567"
_WAM_ID = "wam_audio_001"
_AUDIO_ID = "media_xyz"
_PHONE_ID = "phid-001"


def _meta_audio_payload(
    wam_id: str = _WAM_ID,
    audio_id: str = _AUDIO_ID,
    phone: str = _USER_PHONE,
    bot_number: str = _BOT_NUMBER,
) -> dict:
    """Build a Meta webhook payload with type=audio."""
    msg: dict = {
        "from": phone.replace("+", ""),
        "type": "audio",
        "audio": {"id": audio_id},
        "timestamp": str(int(time.time())),
    }
    if wam_id:
        msg["id"] = wam_id
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "phone_number_id": _PHONE_ID,
                        "display_phone_number": bot_number,
                    },
                    "messages": [msg],
                }
            }]
        }]
    }


def _meta_sig(body: bytes, secret: str) -> str:
    """Produce X-Hub-Signature-256 header value."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _signed_post(client: TestClient, payload: dict, secret: str = _SECRET) -> object:
    """POST a signed webhook payload to /api/webhook/meta."""
    body = json.dumps(payload).encode()
    sig = _meta_sig(body, secret)
    return client.post(
        "/api/webhook/meta",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )


def _mock_pool_for_rate_limit(monkeypatch):
    """
    The webhook calls db.get_pool() internally for rate limiting and for enqueue.
    Return a pool whose connection has fetchval=0 (not rate-limited).
    """
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool = make_pool(conn)
    monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=pool))
    return pool


def _worker_audio_payload(
    audio_id: str = _AUDIO_ID,
    user_phone: str = _USER_PHONE,
    bot_number: str = _BOT_NUMBER,
    phone_id: str = _PHONE_ID,
) -> dict:
    """Minimal payload that inbox_worker._handle_meta_whatsapp expects for audio."""
    return {
        "user_phone":          user_phone,
        "user_text":           "",
        "audio_id":            audio_id,
        "needs_transcription": True,
        "bot_number":          bot_number,
        "phone_id":            phone_id,
    }


# ── Test class ────────────────────────────────────────────────────────────────

class TestAudioVoiceNotes:
    """Test suite for Feature 1: audio / voice note transcription pipeline."""

    # =========================================================================
    # Section A — Webhook enqueue tests
    # =========================================================================

    def test_audio_message_enqueues_with_transcription_flag(self, client, monkeypatch):
        """Audio webhook enqueues payload with needs_transcription=True and correct fields."""
        monkeypatch.setenv("META_APP_SECRET", _SECRET)
        monkeypatch.setattr(db, "db_is_duplicate_wam", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "db_get_restaurant_by_phone", AsyncMock(return_value=None))
        _mock_pool_for_rate_limit(monkeypatch)

        enqueue_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("app.repositories.inbox_repo.enqueue", enqueue_mock)

        payload = _meta_audio_payload(wam_id=_WAM_ID, audio_id=_AUDIO_ID)
        resp = _signed_post(client, payload)

        assert resp.status_code == 200
        enqueue_mock.assert_awaited_once()

        # Inspect the payload passed to enqueue
        _, kwargs = enqueue_mock.call_args
        enqueued = kwargs.get("payload") or enqueue_mock.call_args[0][2]
        # enqueue(pool, provider, external_id, payload) — positional
        call_args = enqueue_mock.call_args
        enqueued_payload = call_args.kwargs.get("payload") or call_args.args[3]

        assert enqueued_payload["needs_transcription"] is True
        assert enqueued_payload["audio_id"] == _AUDIO_ID
        assert enqueued_payload["user_text"] == ""
        assert enqueued_payload["user_phone"] != ""
        assert enqueued_payload["bot_number"] != ""

    def test_audio_message_without_id_is_skipped(self, client, monkeypatch):
        """Audio message with no audio.id is silently skipped; enqueue is NOT called."""
        monkeypatch.setenv("META_APP_SECRET", _SECRET)
        monkeypatch.setattr(db, "db_is_duplicate_wam", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "db_get_restaurant_by_phone", AsyncMock(return_value=None))
        _mock_pool_for_rate_limit(monkeypatch)

        enqueue_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("app.repositories.inbox_repo.enqueue", enqueue_mock)

        # Build a payload where audio has NO id field
        raw = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "phone_number_id": _PHONE_ID,
                            "display_phone_number": _BOT_NUMBER,
                        },
                        "messages": [{
                            "id": "wam_no_audio_id",
                            "from": _USER_PHONE,
                            "type": "audio",
                            "audio": {},   # <-- no 'id' key
                            "timestamp": str(int(time.time())),
                        }],
                    }
                }]
            }]
        }
        resp = _signed_post(client, raw)

        assert resp.status_code == 200
        enqueue_mock.assert_not_awaited()

    def test_audio_uses_wam_id_for_dedup_when_present(self, client, monkeypatch):
        """When wam_id is present it is used as external_id for deduplication."""
        monkeypatch.setenv("META_APP_SECRET", _SECRET)
        monkeypatch.setattr(db, "db_is_duplicate_wam", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "db_get_restaurant_by_phone", AsyncMock(return_value=None))
        _mock_pool_for_rate_limit(monkeypatch)

        enqueue_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("app.repositories.inbox_repo.enqueue", enqueue_mock)

        wam_id = "wam_abc"
        payload = _meta_audio_payload(wam_id=wam_id, audio_id=_AUDIO_ID)
        resp = _signed_post(client, payload)

        assert resp.status_code == 200
        enqueue_mock.assert_awaited_once()

        call_args = enqueue_mock.call_args
        # enqueue(pool, provider=..., external_id=..., payload=...)
        external_id = call_args.kwargs.get("external_id") or call_args.args[2]
        assert external_id == wam_id

    def test_audio_synthesizes_dedup_id_when_no_wam_id(self, client, monkeypatch):
        """When wam_id is absent a synthetic external_id starting with 'synth_' is used."""
        monkeypatch.setenv("META_APP_SECRET", _SECRET)
        monkeypatch.setattr(db, "db_is_duplicate_wam", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "db_get_restaurant_by_phone", AsyncMock(return_value=None))
        _mock_pool_for_rate_limit(monkeypatch)

        enqueue_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("app.repositories.inbox_repo.enqueue", enqueue_mock)

        # Build payload with NO wam_id on the message
        raw = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "phone_number_id": _PHONE_ID,
                            "display_phone_number": _BOT_NUMBER,
                        },
                        "messages": [{
                            # no "id" key → wam_id will be ""
                            "from": _USER_PHONE,
                            "type": "audio",
                            "audio": {"id": _AUDIO_ID},
                            "timestamp": str(int(time.time())),
                        }],
                    }
                }]
            }]
        }
        resp = _signed_post(client, raw)

        assert resp.status_code == 200
        enqueue_mock.assert_awaited_once()

        call_args = enqueue_mock.call_args
        external_id = call_args.kwargs.get("external_id") or call_args.args[2]
        assert external_id.startswith("synth_"), (
            f"Expected synthetic external_id starting with 'synth_', got: {external_id!r}"
        )

    # =========================================================================
    # Section B — Worker transcription tests
    # =========================================================================

    def test_worker_transcribes_and_calls_process_message(self, monkeypatch):
        """Happy path: worker downloads audio, transcribes, calls _process_message."""
        from app.services import inbox_worker

        download_mock = AsyncMock(return_value=(b"audio_bytes", "audio/ogg"))
        transcribe_mock = AsyncMock(return_value="hola quiero una pizza")
        process_mock = AsyncMock()

        monkeypatch.setattr("app.services.transcription.download_whatsapp_media", download_mock)
        monkeypatch.setattr("app.services.transcription.transcribe_audio", transcribe_mock)
        monkeypatch.setattr("app.routes.chat._process_message", process_mock)
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value={"id": 1, "name": "Test", "wa_access_token": "tok-test",
                                   "wa_phone_id": "ph1", "whatsapp_number": _BOT_NUMBER,
                                   "menu": {}, "features": {"bot_voice_notes": True}, "matched_location_id": None}),
        )

        payload = _worker_audio_payload()
        asyncio.get_event_loop().run_until_complete(
            inbox_worker._handle_meta_whatsapp(payload)
        )

        process_mock.assert_awaited_once()
        _, call_kwargs = process_mock.call_args
        assert call_kwargs["user_text"] == "hola quiero una pizza"
        assert call_kwargs["user_phone"] == _USER_PHONE
        assert call_kwargs["bot_number"] == _BOT_NUMBER

    def test_worker_transcription_unavailable_sends_fallback(self, monkeypatch):
        """TranscriptionUnavailable → fallback WA text sent; _process_message NOT called."""
        from app.services import inbox_worker
        from app.services.transcription import TranscriptionUnavailable

        download_mock = AsyncMock(return_value=(b"bytes", "audio/ogg"))
        transcribe_mock = AsyncMock(side_effect=TranscriptionUnavailable("no key"))
        send_mock = AsyncMock()
        process_mock = AsyncMock()

        monkeypatch.setattr("app.services.transcription.download_whatsapp_media", download_mock)
        monkeypatch.setattr("app.services.transcription.transcribe_audio", transcribe_mock)
        monkeypatch.setattr("app.services.meta_api.send_text", send_mock)
        monkeypatch.setattr("app.routes.chat._process_message", process_mock)
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value={"id": 1, "name": "Test", "wa_access_token": "tok-test",
                                   "wa_phone_id": "ph1", "whatsapp_number": _BOT_NUMBER,
                                   "menu": {}, "features": {"bot_voice_notes": True}, "matched_location_id": None}),
        )

        payload = _worker_audio_payload()
        asyncio.get_event_loop().run_until_complete(
            inbox_worker._handle_meta_whatsapp(payload)
        )

        send_mock.assert_awaited_once()
        fallback_text = send_mock.call_args.args[3]
        assert "audios" in fallback_text.lower() or "escrib" in fallback_text.lower(), (
            f"Expected fallback about audio/writing, got: {fallback_text!r}"
        )
        process_mock.assert_not_awaited()

    def test_worker_audio_too_long_sends_fallback(self, monkeypatch):
        """AudioTooLongError → fallback with 'largo'/'corto'; _process_message NOT called."""
        from app.services import inbox_worker
        from app.services.transcription import AudioTooLongError

        download_mock = AsyncMock(return_value=(b"big_bytes", "audio/ogg"))
        transcribe_mock = AsyncMock(side_effect=AudioTooLongError("too big"))
        send_mock = AsyncMock()
        process_mock = AsyncMock()

        monkeypatch.setattr("app.services.transcription.download_whatsapp_media", download_mock)
        monkeypatch.setattr("app.services.transcription.transcribe_audio", transcribe_mock)
        monkeypatch.setattr("app.services.meta_api.send_text", send_mock)
        monkeypatch.setattr("app.routes.chat._process_message", process_mock)
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value={"id": 1, "name": "Test", "wa_access_token": "tok-test",
                                   "wa_phone_id": "ph1", "whatsapp_number": _BOT_NUMBER,
                                   "menu": {}, "features": {"bot_voice_notes": True}, "matched_location_id": None}),
        )

        payload = _worker_audio_payload()
        asyncio.get_event_loop().run_until_complete(
            inbox_worker._handle_meta_whatsapp(payload)
        )

        send_mock.assert_awaited_once()
        fallback_text = send_mock.call_args.args[3]
        assert "largo" in fallback_text.lower() or "corto" in fallback_text.lower(), (
            f"Expected fallback about length, got: {fallback_text!r}"
        )
        process_mock.assert_not_awaited()

    def test_worker_transient_error_raises_for_retry(self, monkeypatch):
        """TranscriptionError (transient) re-raises so inbox retries the row."""
        from app.services import inbox_worker
        from app.services.transcription import TranscriptionError

        download_mock = AsyncMock(return_value=(b"bytes", "audio/ogg"))
        transcribe_mock = AsyncMock(side_effect=TranscriptionError("timeout"))
        process_mock = AsyncMock()

        monkeypatch.setattr("app.services.transcription.download_whatsapp_media", download_mock)
        monkeypatch.setattr("app.services.transcription.transcribe_audio", transcribe_mock)
        monkeypatch.setattr("app.routes.chat._process_message", process_mock)
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value={"id": 1, "name": "Test", "wa_access_token": "tok-test",
                                   "wa_phone_id": "ph1", "whatsapp_number": _BOT_NUMBER,
                                   "menu": {}, "features": {"bot_voice_notes": True}, "matched_location_id": None}),
        )

        payload = _worker_audio_payload()
        with pytest.raises(TranscriptionError):
            asyncio.get_event_loop().run_until_complete(
                inbox_worker._handle_meta_whatsapp(payload)
            )

        process_mock.assert_not_awaited()

    def test_worker_empty_transcription_sends_fallback(self, monkeypatch):
        """EmptyTranscriptionError → fallback WA text; _process_message NOT called.

        transcribe_audio now raises EmptyTranscriptionError instead of returning ""
        so that the inbox_worker handles it via the typed exception branch.
        """
        from app.services import inbox_worker
        from app.services.transcription import EmptyTranscriptionError

        download_mock = AsyncMock(return_value=(b"bytes", "audio/ogg"))
        # transcribe_audio raises EmptyTranscriptionError when Whisper hears nothing
        transcribe_mock = AsyncMock(side_effect=EmptyTranscriptionError("inaudible"))
        send_mock = AsyncMock()
        process_mock = AsyncMock()

        monkeypatch.setattr("app.services.transcription.download_whatsapp_media", download_mock)
        monkeypatch.setattr("app.services.transcription.transcribe_audio", transcribe_mock)
        monkeypatch.setattr("app.services.meta_api.send_text", send_mock)
        monkeypatch.setattr("app.routes.chat._process_message", process_mock)
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value={"id": 1, "name": "Test", "wa_access_token": "tok-test",
                                   "wa_phone_id": "ph1", "whatsapp_number": _BOT_NUMBER,
                                   "menu": {}, "features": {"bot_voice_notes": True}, "matched_location_id": None}),
        )

        payload = _worker_audio_payload()
        asyncio.get_event_loop().run_until_complete(
            inbox_worker._handle_meta_whatsapp(payload)
        )

        send_mock.assert_awaited_once()
        fallback_text = send_mock.call_args.args[3]
        assert "escuch" in fallback_text.lower() or "intent" in fallback_text.lower(), (
            f"Expected fallback about hearing nothing, got: {fallback_text!r}"
        )
        process_mock.assert_not_awaited()

    def test_worker_unknown_exception_sends_fallback_and_acks(self, monkeypatch):
        """RuntimeError from transcription → fallback sent; function returns (no raise)."""
        from app.services import inbox_worker

        download_mock = AsyncMock(return_value=(b"bytes", "audio/ogg"))
        transcribe_mock = AsyncMock(side_effect=RuntimeError("weird bug"))
        send_mock = AsyncMock()
        process_mock = AsyncMock()

        monkeypatch.setattr("app.services.transcription.download_whatsapp_media", download_mock)
        monkeypatch.setattr("app.services.transcription.transcribe_audio", transcribe_mock)
        monkeypatch.setattr("app.services.meta_api.send_text", send_mock)
        monkeypatch.setattr("app.routes.chat._process_message", process_mock)
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value={"id": 1, "name": "Test", "wa_access_token": "tok-test",
                                   "wa_phone_id": "ph1", "whatsapp_number": _BOT_NUMBER,
                                   "menu": {}, "features": {"bot_voice_notes": True}, "matched_location_id": None}),
        )

        payload = _worker_audio_payload()
        # Should NOT raise — unknown exceptions are acked to avoid infinite retry
        asyncio.get_event_loop().run_until_complete(
            inbox_worker._handle_meta_whatsapp(payload)
        )

        send_mock.assert_awaited_once()
        fallback_text = send_mock.call_args.args[3]
        assert "audio" in fallback_text.lower() or "escrib" in fallback_text.lower(), (
            f"Expected fallback mentioning audio/writing, got: {fallback_text!r}"
        )
        process_mock.assert_not_awaited()

    def test_worker_voice_notes_disabled_sends_friendly_fallback(self, monkeypatch):
        """bot_voice_notes=False (default) → friendly 'solo recibo mensajes escritos'; no transcription."""
        from app.services import inbox_worker

        download_mock = AsyncMock()
        send_mock = AsyncMock()
        process_mock = AsyncMock()

        monkeypatch.setattr("app.services.transcription.download_whatsapp_media", download_mock)
        monkeypatch.setattr("app.services.meta_api.send_text", send_mock)
        monkeypatch.setattr("app.routes.chat._process_message", process_mock)
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value={"id": 1, "name": "Test", "wa_access_token": "tok-test",
                                   "wa_phone_id": "ph1", "whatsapp_number": _BOT_NUMBER,
                                   "menu": {}, "features": {}, "matched_location_id": None}),
        )

        payload = _worker_audio_payload()
        asyncio.get_event_loop().run_until_complete(
            inbox_worker._handle_meta_whatsapp(payload)
        )

        # Audio download should NOT be called — flag disabled before transcription
        download_mock.assert_not_awaited()
        # A friendly message should be sent
        send_mock.assert_awaited_once()
        fallback_text = send_mock.call_args.args[3]
        assert "escrit" in fallback_text.lower() or "mensajes" in fallback_text.lower(), (
            f"Expected friendly fallback about written messages, got: {fallback_text!r}"
        )
        process_mock.assert_not_awaited()

    def test_worker_no_access_token_returns_without_sending(self, monkeypatch):
        """No token from DB and no env vars → returns early; nothing sent."""
        from app.services import inbox_worker

        send_mock = AsyncMock()
        process_mock = AsyncMock()

        monkeypatch.setattr("app.services.meta_api.send_text", send_mock)
        monkeypatch.setattr("app.routes.chat._process_message", process_mock)
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value=None),   # no Org record → ack immediately, no dispatch
        )
        # Remove env-var fallbacks so access_token resolves to ""
        monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)

        payload = _worker_audio_payload()
        # Must return without raising
        asyncio.get_event_loop().run_until_complete(
            inbox_worker._handle_meta_whatsapp(payload)
        )

        send_mock.assert_not_awaited()
        process_mock.assert_not_awaited()

    # =========================================================================
    # Section C — Transcription unit tests
    # =========================================================================

    def test_transcribe_audio_raises_unavailable_without_key(self, monkeypatch):
        """transcribe_audio raises TranscriptionUnavailable when OPENAI_API_KEY is absent."""
        from app.services.transcription import transcribe_audio, TranscriptionUnavailable

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(TranscriptionUnavailable):
            asyncio.get_event_loop().run_until_complete(
                transcribe_audio(b"fake audio bytes")
            )

    def test_transcribe_audio_raises_too_long_above_limit(self, monkeypatch):
        """transcribe_audio raises AudioTooLongError for audio >16 MB without any HTTP call."""
        from app.services.transcription import (
            transcribe_audio,
            AudioTooLongError,
            MAX_AUDIO_BYTES,
        )

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        # Track whether httpx was called
        post_mock = AsyncMock()
        monkeypatch.setattr("httpx.AsyncClient.post", post_mock)

        oversized = b"x" * (MAX_AUDIO_BYTES + 1)
        with pytest.raises(AudioTooLongError):
            asyncio.get_event_loop().run_until_complete(
                transcribe_audio(oversized)
            )

        post_mock.assert_not_awaited()

    def test_transcribe_audio_retries_on_5xx_then_succeeds(self, monkeypatch):
        """transcribe_audio retries on 503 and succeeds on second attempt."""
        from app.services.transcription import transcribe_audio

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = "Service Unavailable"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json = MagicMock(return_value={"text": "ok hola"})

        post_mock = AsyncMock(side_effect=[resp_503, resp_200])
        monkeypatch.setattr("httpx.AsyncClient.post", post_mock)

        result = asyncio.get_event_loop().run_until_complete(
            transcribe_audio(b"small audio")
        )

        assert result == "ok hola"
        assert post_mock.await_count == 2

    def test_transcribe_audio_does_not_retry_on_401(self, monkeypatch):
        """transcribe_audio raises TranscriptionError immediately on 401 (no retry)."""
        from app.services.transcription import transcribe_audio, TranscriptionError

        monkeypatch.setenv("OPENAI_API_KEY", "bad-key")

        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.text = '{"error": "Incorrect API key"}'

        post_mock = AsyncMock(return_value=resp_401)
        monkeypatch.setattr("httpx.AsyncClient.post", post_mock)

        with pytest.raises(TranscriptionError):
            asyncio.get_event_loop().run_until_complete(
                transcribe_audio(b"audio")
            )

        # Should have been called exactly once — 401 is non-retryable
        assert post_mock.await_count == 1
