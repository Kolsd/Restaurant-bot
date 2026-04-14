"""
image_host.py — Wrapper sobre Cloudinary SDK para el catálogo visual v2.

Responsabilidades:
  - sign_upload_params: firma parámetros para upload directo browser→Cloudinary
  - delete_image:       borra imágenes con validación de ownership multi-tenant
  - build_transform_url: construye URLs con transformaciones predefinidas
  - is_cloudinary_url:  helper de detección

Config requerida (env vars):
  CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET

Si alguna variable falta, las funciones retornan errores manejables (dict con
"error" key o False) — la app no crashea al arrancar.
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
