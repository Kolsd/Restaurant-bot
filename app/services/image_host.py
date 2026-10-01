"""
image_host.py — Wrapper over the Cloudinary SDK for visual catalog v2.

Responsibilities:
  - sign_upload_params: signs parameters for a direct browser→Cloudinary upload
  - delete_image:       deletes images with multi-tenant ownership validation
  - build_transform_url: builds URLs with predefined transformations
  - is_cloudinary_url:  detection helper

Required config (env vars):
  CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET

If any variable is missing, the functions return manageable errors (dict with
an "error" key, or False) — the app doesn't crash on startup.
"""

from __future__ import annotations

import os
import re
import time
import hashlib

from app.services.logging import get_logger

log = get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_CLOUD_NAME  = os.getenv("CLOUDINARY_CLOUD_NAME", "")
_API_KEY     = os.getenv("CLOUDINARY_API_KEY", "")
_API_SECRET  = os.getenv("CLOUDINARY_API_SECRET", "")

_CLOUDINARY_HOST_RE = re.compile(r"https?://res\.cloudinary\.com/")

# Log once at import time if config is missing — do NOT crash.
if not all([_CLOUD_NAME, _API_KEY, _API_SECRET]):
    log.warning(
        "image_host.config_missing",
        cloud_name=bool(_CLOUD_NAME),
        api_key=bool(_API_KEY),
        api_secret=bool(_API_SECRET),
        hint="Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET",
    )

# ── Transform variant specs ───────────────────────────────────────────────────

_VARIANTS: dict[str, str] = {
    "thumb": "c_fill,w_300,h_300",
    "card":  "c_fill,w_600,h_450,q_auto,f_auto",
    "hero":  "c_fill,w_1200,h_900,q_auto,f_auto",
}


# ── Public API ────────────────────────────────────────────────────────────────

def is_cloudinary_url(url: str) -> bool:
    """Return True if *url* is served from res.cloudinary.com."""
    if not url or not isinstance(url, str):
        return False
    return bool(_CLOUDINARY_HOST_RE.match(url))


def sign_upload_params(restaurant_id: int, folder_suffix: str = "menu") -> dict:
    """
    Return signed parameters for a browser-direct upload to Cloudinary.

    The browser POSTs multipart to:
        https://api.cloudinary.com/v1_1/{cloud_name}/image/upload
    using these params plus the chosen file.

    Returns a dict with:
        signature, timestamp, api_key, cloud_name, folder, public_id_prefix

    On config error returns {"error": "<reason>"}.
    """
    if not all([_CLOUD_NAME, _API_KEY, _API_SECRET]):
        return {"error": "Cloudinary not configured — check env vars"}

    folder = f"mesio/r_{restaurant_id}/{folder_suffix}"
    timestamp = int(time.time())

    # Params to sign — alphabetical order as per Cloudinary spec.
    params_to_sign: dict = {
        "folder":    folder,
        "timestamp": timestamp,
    }

    signature = _make_signature(params_to_sign)

    return {
        "signature":        signature,
        "timestamp":        timestamp,
        "api_key":          _API_KEY,
        "cloud_name":       _CLOUD_NAME,
        "folder":           folder,
        "public_id_prefix": f"mesio/r_{restaurant_id}/",
    }


def delete_image(public_id: str, restaurant_id: int) -> bool:
    """
    Delete a Cloudinary image by public_id.

    Validates that public_id belongs to *restaurant_id* BEFORE deleting
    (prevents cross-tenant deletion).  Returns True on success, False otherwise.
    """
    if not all([_CLOUD_NAME, _API_KEY, _API_SECRET]):
        log.warning("image_host.delete.config_missing", public_id=public_id)
        return False

    expected_prefix = f"mesio/r_{restaurant_id}/"
    if not public_id or not public_id.startswith(expected_prefix):
        log.warning(
            "image_host.delete.ownership_check_failed",
            public_id=public_id,
            restaurant_id=restaurant_id,
            expected_prefix=expected_prefix,
        )
        return False

    try:
        import cloudinary.uploader  # type: ignore[import]
        import cloudinary           # type: ignore[import]

        cloudinary.config(
            cloud_name=_CLOUD_NAME,
            api_key=_API_KEY,
            api_secret=_API_SECRET,
        )

        result = cloudinary.uploader.destroy(public_id)
        success = result.get("result") == "ok"
        if not success:
            log.warning(
                "image_host.delete.cloudinary_error",
                public_id=public_id,
                cloudinary_result=result,
            )
        return success
    except Exception as exc:
        log.exception("image_host.delete.exception", public_id=public_id, error=str(exc))
        return False


def build_transform_url(cloudinary_url: str, variant: str) -> str:
    """
    Return a Cloudinary URL with the requested transformation inserted.

    variant options: "thumb" (300x300), "card" (600x450), "hero" (1200x900)

    If *cloudinary_url* is not a Cloudinary URL, return it unchanged (backward-compat).
    """
    if not is_cloudinary_url(cloudinary_url):
        return cloudinary_url

    transform = _VARIANTS.get(variant)
    if not transform:
        log.warning("image_host.build_transform_url.unknown_variant", variant=variant)
        return cloudinary_url

    # Insert transformation after /upload/
    # e.g. https://res.cloudinary.com/mesio/image/upload/v123/mesio/r_1/dish.webp
    #   -> https://res.cloudinary.com/mesio/image/upload/c_fill,w_300,h_300/v123/mesio/r_1/dish.webp
    return cloudinary_url.replace("/upload/", f"/upload/{transform}/", 1)


# ── Delivery/pickup payment-proof upload (docs/claude/delivery-web.md chunk 3) ─
#
# Distinct from sign_upload_params() above: menu photos use a browser-direct
# upload (the server never touches the bytes) because they're an authenticated
# admin action with no reason to route large files through our own process. A
# payment-proof screenshot is different — it's PUBLIC unauthenticated input
# (a diner_sessions token, not an admin JWT) and the spec requires the SERVER
# to actually validate "this is really an image" before trusting it, which is
# only possible if the server receives the bytes. Same Cloudinary account,
# same config — NOT a second uploader.

_MAX_PROOF_BYTES = 8 * 1024 * 1024  # 8 MB
_ALLOWED_PROOF_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"}

# Real magic-byte checks — never trust the client's declared Content-Type
# alone, since that header is fully attacker-controlled.
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_WEBP_RIFF = b"RIFF"
_WEBP_TAG = b"WEBP"


def _looks_like_image(data: bytes) -> bool:
    if not data:
        return False
    if data.startswith(_JPEG_MAGIC) or data.startswith(_PNG_MAGIC):
        return True
    if any(data.startswith(m) for m in _GIF_MAGICS):
        return True
    if data.startswith(_WEBP_RIFF) and len(data) >= 12 and data[8:12] == _WEBP_TAG:
        return True
    return False


def upload_delivery_proof(org_id: int, file_bytes: bytes, content_type: str) -> dict:
    """Server-side upload of a Nequi/Bancolombia transfer-proof screenshot.

    Validates size and that the bytes are ACTUALLY an image (magic-byte
    check — the declared Content-Type is advisory only) before ever calling
    Cloudinary. Returns {"secure_url": ..., "public_id": ...} on success, or
    {"error": <reason>} — reasons: "config_missing", "empty_file",
    "file_too_large", "not_an_image", "upload_failed". Never raises.

    The caller (app/routes/diner_delivery.py) is responsible for binding the
    returned URL to the uploading diner's OWN session before it can ever be
    attached to an order — this function knows nothing about sessions/orders.
    """
    if not all([_CLOUD_NAME, _API_KEY, _API_SECRET]):
        log.warning("image_host.upload_delivery_proof.config_missing", org_id=org_id)
        return {"error": "config_missing"}
    if not file_bytes:
        return {"error": "empty_file"}
    if len(file_bytes) > _MAX_PROOF_BYTES:
        return {"error": "file_too_large"}
    if not _looks_like_image(file_bytes):
        log.warning(
            "image_host.upload_delivery_proof.not_an_image",
            org_id=org_id, declared_content_type=content_type,
        )
        return {"error": "not_an_image"}

    try:
        import cloudinary.uploader  # type: ignore[import]
        import cloudinary           # type: ignore[import]

        cloudinary.config(cloud_name=_CLOUD_NAME, api_key=_API_KEY, api_secret=_API_SECRET)
        folder = f"mesio/r_{org_id}/delivery_proof"
        result = cloudinary.uploader.upload(file_bytes, folder=folder, resource_type="image")
        secure_url = result.get("secure_url")
        if not secure_url:
            log.warning("image_host.upload_delivery_proof.no_url_returned", org_id=org_id)
            return {"error": "upload_failed"}
        return {"secure_url": secure_url, "public_id": result.get("public_id")}
    except Exception as exc:
        log.exception("image_host.upload_delivery_proof.exception", org_id=org_id, error=str(exc))
        return {"error": "upload_failed"}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _make_signature(params: dict) -> str:
    """
    Generate a Cloudinary upload signature.
    Params are sorted alphabetically, joined as key=value&..., then
    SHA-1 hashed with api_secret appended.
    """
    pairs = "&".join(
        f"{k}={v}"
        for k, v in sorted(params.items())
    )
    to_sign = pairs + _API_SECRET
    return hashlib.sha1(to_sign.encode("utf-8")).hexdigest()  # noqa: S324 — Cloudinary spec requires SHA-1
