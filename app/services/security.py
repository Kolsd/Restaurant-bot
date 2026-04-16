"""
app/services/security.py

Secret comparison and auth utilities shared across internal routes.
"""
import hmac


def compare_secret(a: str, b: str) -> bool:
    """Timing-safe comparison of two secrets (strings).

    Uses hmac.compare_digest to prevent timing-based side-channel attacks.
    Returns False immediately if either argument is empty or None — this avoids
    accepting an empty ADMIN_KEY that was never configured.
    """
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())
