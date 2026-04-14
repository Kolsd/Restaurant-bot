"""
Cloudinary image host helpers.

Wraps Cloudinary SDK calls and URL transforms.
CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET must be set
for sign/delete operations. build_transform_url / is_cloudinary_url work
without credentials.
"""

from __future__ import annotations

import hashlib
import os
import time

from app.services.logging import get_logger

log = get_logger(__name__)

_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
_API_KEY    = os.getenv("CLOUDINARY_API_KEY", "")
_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")

_TRANSFORM_VARIANTS: dict[str, str] = {
    "thumb": "c_fill,w_300,h_300,f_webp,q_auto",
    "card":  "c_fill,w_600,h_450,f_webp,q_auto",
    "hero":  "c_fill,w_1200,h_900,f_webp,q_auto",
}


def is_cloudinary_url(url: str | None) -> bool:
    """Return True if the URL points to Cloudinary."""
    if not url:
        return False
    return "res.cloudinary.com" in url or "cloudinary.com" in url


def build_transform_url(url: str | None, variant: str = "card") -> str | None:
    """
    Insert a Cloudinary transformation string into an existing Cloudinary URL.

    Example:
        https://res.cloudinary.com/demo/image/upload/v1/mesio/r_42/dish.webp
        → https://res.cloudinary.com/demo/image/upload/c_fill,w_1200,h_900,f_webp,q_auto/v1/mesio/r_42/dish.webp

    Returns the original URL unchanged if it is not a Cloudinary URL or the
    variant is unknown. Returns None if url is None.
    """
    if not url:
        return None
    transform = _TRANSFORM_VARIANTS.get(variant)
    if not transform or not is_cloudinary_url(url):
        return url
    # Insert transform after /upload/
    return url.replace("/upload/", f"/upload/{transform}/", 1)


def _cloudinary_configured() -> bool:
    return bool(_CLOUD_NAME and _API_KEY and _API_SECRET)


def sign_upload_params(restaurant_id: int, folder_suffix: str = "menu") -> dict:
    """
    Return params for a direct browser→Cloudinary upload (signed).
    Raises RuntimeError if credentials are not configured.
    """
    if not _cloudinary_configured():
        raise RuntimeError("Cloudinary env vars not configured")

    folder      = f"mesio/r_{restaurant_id}/{folder_suffix}"
    timestamp   = int(time.time())
    public_id_prefix = f"dish_{timestamp}"

    params_to_sign = f"folder={folder}&public_id={public_id_prefix}&timestamp={timestamp}"
    signature = hashlib.sha1(f"{params_to_sign}{_API_SECRET}".encode()).hexdigest()

    return {
        "signature":        signature,
        "timestamp":        timestamp,
        "api_key":          _API_KEY,
        "cloud_name":       _CLOUD_NAME,
        "folder":           folder,
        "public_id_prefix": public_id_prefix,
    }


async def delete_image(public_id: str, restaurant_id: int) -> dict:
    """
    Delete a Cloudinary image by public_id, validating cross-tenant ownership.
    Returns {"success": True, "public_id": public_id}.
    Raises ValueError on ownership violation.
    Raises RuntimeError if credentials are not configured.
    """
    expected_prefix = f"mesio/r_{restaurant_id}/"
    if not public_id.startswith(expected_prefix):
        raise ValueError(
            f"public_id '{public_id}' does not belong to restaurant {restaurant_id}"
        )

    if not _cloudinary_configured():
        raise RuntimeError("Cloudinary env vars not configured")

    try:
        import cloudinary
        import cloudinary.uploader  # type: ignore

        cloudinary.config(
            cloud_name=_CLOUD_NAME,
            api_key=_API_KEY,
            api_secret=_API_SECRET,
        )
        result = cloudinary.uploader.destroy(public_id)
        log.info("image_host.delete", public_id=public_id, result=result.get("result"))
    except ImportError:
        log.warning("image_host.cloudinary_sdk_missing", public_id=public_id)

    return {"success": True, "public_id": public_id}
