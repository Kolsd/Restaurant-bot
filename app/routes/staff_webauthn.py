"""
WebAuthn biometric authentication for Staff clock-in / clock-out terminals.

Endpoints:
  POST /api/staff/webauthn/register-options   — start credential registration (staff token)
  POST /api/staff/webauthn/register-complete  — finish registration & persist credential
  POST /api/staff/webauthn/auth-options       — get assertion challenge (public, clock terminal)
  POST /api/staff/webauthn/auth-complete      — verify assertion & execute clock action
  GET  /api/staff/webauthn/credentials        — list credentials for authenticated staff
  DELETE /api/staff/webauthn/credentials/{id} — delete a credential (admin)

Security model:
  - Registration requires a valid staff JWT (Bearer).
  - Authentication (clock-in/out) is intentionally public so kiosk terminals
    can operate without storing any credential.
  - Challenges are single-use and expire after 5 minutes (enforced in DB).
"""
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
    AuthenticatorAttachment,
    PublicKeyCredentialDescriptor,
    AuthenticatorTransport,
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes

from app.routes.deps import get_current_restaurant, require_auth, require_module
from app.services import database as db
from app.services.auth import verify_token

router = APIRouter(prefix="/api/staff/webauthn", tags=["staff-webauthn"])

# Module guard shared by the endpoints that require a staff/admin token.
_MODULE_DEPS = [Depends(require_module("staff_tips"))]

RP_NAME = "Mesio"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rp_id(request: Request) -> str:
    """Determine the Relying Party ID from env var or request hostname."""
    return os.environ.get("APP_DOMAIN", request.url.hostname)


async def _get_staff_id_from_token(request: Request) -> str:
    """
    Extract the staff UUID from a Bearer token that encodes 'staff:<uuid>'.
    Raises 401 if the token is missing, invalid, or does not belong to a staff member.
    """
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Token inválido o ausente")
    if not username.startswith("staff:"):
        raise HTTPException(status_code=403, detail="Este endpoint es solo para empleados (staff)")
    return username.split(":", 1)[1]


# ── Pydantic models ───────────────────────────────────────────────────────────

class RegisterCompleteBody(BaseModel):
    device_name:        str = Field("", max_length=100)
    # Raw fields forwarded from the browser's PublicKeyCredential
    id:                 str
    raw_id:             str
    response:           dict   # { attestationObject, clientDataJSON }
    type:               str = "public-key"


class AuthOptionsBody(BaseModel):
    restaurant_id: int
    action:        str = Field(..., pattern="^(clock_in|clock_out)$")


class AuthCompleteBody(BaseModel):
    action:             str   = Field(..., pattern="^(clock_in|clock_out)$")
    credential_id:      str
    authenticator_data: str
    client_data_json:   str
    signature:          str
    user_handle:        str | None = None


# ── 1. Registration options ───────────────────────────────────────────────────

@router.post("/register-options", dependencies=_MODULE_DEPS)
async def register_options(request: Request):
    """
    Generate a WebAuthn registration challenge for the authenticated staff member.
    Returns PublicKeyCredentialCreationOptions.
    """
    staff_id = await _get_staff_id_from_token(request)

    # Fetch staff details to populate the user entity
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        staff = await conn.fetchrow(
            "SELECT id::text, name, restaurant_id FROM staff WHERE id = $1::uuid AND active = TRUE",
            staff_id,
        )
    if not staff:
        raise HTTPException(status_code=404, detail="Empleado no encontrado o inactivo")

    rp_id = _rp_id(request)

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=staff_id.encode(),
        user_name=staff["name"],
        user_display_name=staff["name"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )

    challenge_b64 = bytes_to_base64url(options.challenge)

    await db.db_save_webauthn_challenge(
        challenge=challenge_b64,
        staff_id=staff_id,
        challenge_type="registration",
        restaurant_id=staff["restaurant_id"],
    )

    return json.loads(options_to_json(options))


# ── 2. Registration complete ──────────────────────────────────────────────────

@router.post("/register-complete", dependencies=_MODULE_DEPS, status_code=201)
async def register_complete(request: Request, body: RegisterCompleteBody):
    """
    Verify the attestation response and persist the new WebAuthn credential.
    """
    staff_id = await _get_staff_id_from_token(request)

    # Reconstruct client_data_json bytes to derive the challenge
    try:
        client_data_json_bytes = base64url_to_bytes(body.response["clientDataJSON"])
        client_data = json.loads(client_data_json_bytes)
        challenge_b64 = client_data.get("challenge", "")
    except Exception:
        raise HTTPException(status_code=400, detail="clientDataJSON inválido")

    # Consume the challenge atomically (single-use + expiry enforced in DB)
    stored = await db.db_consume_webauthn_challenge(challenge_b64)
    if not stored:
        raise HTTPException(status_code=400, detail="Challenge no encontrado o expirado")
    if stored.get("staff_id") != staff_id:
        raise HTTPException(status_code=403, detail="El challenge no pertenece a este empleado")

    rp_id = _rp_id(request)
    expected_origin = str(request.base_url).rstrip("/")

    try:
        verification = verify_registration_response(
            credential=body.model_dump(),
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=rp_id,
            expected_origin=expected_origin,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Verificación fallida: {exc}")

    credential_id_b64 = bytes_to_base64url(verification.credential_id)
    public_key_b64 = bytes_to_base64url(verification.credential_public_key)
    transports = [t.value if hasattr(t, "value") else str(t)
                  for t in (verification.credential_device_type and [] or [])]

    try:
        credential = await db.db_save_webauthn_credential(
            staff_id=staff_id,
            credential_id=credential_id_b64,
            public_key=public_key_b64,
            sign_count=verification.sign_count,
            transports=transports,
            device_name=body.device_name,
        )
    except Exception as exc:
        # Likely a unique-constraint violation (credential already registered)
        raise HTTPException(status_code=409, detail=f"No se pudo guardar la credencial: {exc}")

    return {"success": True, "credential": credential}


# ── 3. Authentication options (public) ───────────────────────────────────────

@router.post("/auth-options")
async def auth_options(request: Request, body: AuthOptionsBody):
    """
    Generate a WebAuthn authentication challenge for a clock terminal.
    This endpoint is intentionally public — no auth token required.
    """
    # Housekeeping: remove stale challenges before generating a new one
    await db.db_cleanup_expired_challenges()

    credentials_raw = await db.db_get_webauthn_credentials_by_restaurant(body.restaurant_id)
    if not credentials_raw:
        raise HTTPException(
            status_code=404,
            detail="No hay credenciales biométricas registradas para este restaurante",
        )

    allow_credentials = []
    for cred in credentials_raw:
        raw_transports = cred.get("transports") or []
        if isinstance(raw_transports, str):
            try:
                raw_transports = json.loads(raw_transports)
            except Exception:
                raw_transports = []

        transports = []
        for t in raw_transports:
            try:
                transports.append(AuthenticatorTransport(t))
            except ValueError:
                pass  # Ignore unknown transport strings

        allow_credentials.append(
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(cred["credential_id"]),
                transports=transports,
            )
        )

    rp_id = _rp_id(request)

    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    challenge_b64 = bytes_to_base64url(options.challenge)

    await db.db_save_webauthn_challenge(
        challenge=challenge_b64,
        staff_id=None,
        challenge_type=body.action,
        restaurant_id=body.restaurant_id,
    )

    return json.loads(options_to_json(options))


# ── 4. Authentication complete (public) ──────────────────────────────────────

@router.post("/auth-complete")
async def auth_complete(request: Request, body: AuthCompleteBody):
    """
    Verify the assertion and execute the clock-in or clock-out action.
    Returns staff name, action performed, and the resulting shift record.
    This endpoint is intentionally public for kiosk terminals.
    """
    # Derive the challenge from the clientDataJSON
    try:
        client_data_bytes = base64url_to_bytes(body.client_data_json)
        client_data = json.loads(client_data_bytes)
        challenge_b64 = client_data.get("challenge", "")
    except Exception:
        raise HTTPException(status_code=400, detail="client_data_json inválido")

    stored = await db.db_consume_webauthn_challenge(challenge_b64)
    if not stored:
        raise HTTPException(status_code=400, detail="Challenge no encontrado o expirado")
    if stored.get("type") != body.action:
        raise HTTPException(
            status_code=400,
            detail=f"El challenge fue generado para '{stored.get('type')}', no '{body.action}'",
        )

    cred_record = await db.db_get_webauthn_credential(body.credential_id)
    if not cred_record:
        raise HTTPException(status_code=404, detail="Credencial no encontrada")

    # Ensure the credential belongs to the restaurant in the stored challenge
    if cred_record.get("restaurant_id") != stored.get("restaurant_id"):
        raise HTTPException(status_code=403, detail="Credencial no pertenece a este restaurante")

    rp_id = _rp_id(request)
    expected_origin = str(request.base_url).rstrip("/")

    try:
        verification = verify_authentication_response(
            credential={
                "id": body.credential_id,
                "rawId": body.credential_id,
                "response": {
                    "authenticatorData": body.authenticator_data,
                    "clientDataJSON": body.client_data_json,
                    "signature": body.signature,
                    "userHandle": body.user_handle,
                },
                "type": "public-key",
            },
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=rp_id,
            expected_origin=expected_origin,
            credential_public_key=base64url_to_bytes(cred_record["public_key"]),
            credential_current_sign_count=cred_record["sign_count"],
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Verificación biométrica fallida: {exc}")

    # Update sign counter to prevent replay attacks
    await db.db_update_webauthn_sign_count(body.credential_id, verification.new_sign_count)

    # Execute the clock action
    staff_id = cred_record["staff_id"]
    restaurant_id = cred_record["restaurant_id"]

    try:
        if body.action == "clock_in":
            shift = await db.db_clock_in(staff_id, restaurant_id)
        else:
            shift = await db.db_clock_out(staff_id, restaurant_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if shift is None:
        raise HTTPException(
            status_code=400,
            detail="No se encontró un turno activo para hacer clock-out"
            if body.action == "clock_out"
            else "Error al registrar la entrada",
        )

    return {
        "success": True,
        "staff_name": cred_record.get("staff_name", ""),
        "action": body.action,
        "shift": shift,
    }


# ── 5. List credentials (staff token) ────────────────────────────────────────

@router.get("/credentials", dependencies=_MODULE_DEPS)
async def list_credentials(request: Request):
    """Return all registered WebAuthn credentials for the authenticated staff member."""
    staff_id = await _get_staff_id_from_token(request)
    credentials = await db.db_get_webauthn_credentials_by_staff(staff_id)
    return {"credentials": credentials}


# ── 6. Delete credential (admin) ─────────────────────────────────────────────

@router.delete("/credentials/{credential_id}", dependencies=_MODULE_DEPS)
async def delete_credential(
    credential_id: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Delete a WebAuthn credential.
    Requires a valid admin/restaurant-owner session (get_current_restaurant).
    """
    cred = await db.db_get_webauthn_credential(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credencial no encontrada")

    # Ensure the credential belongs to this restaurant (or a branch of it)
    if cred.get("restaurant_id") != restaurant["id"]:
        # Allow parent restaurant (matrix) to delete credentials of its branches
        if restaurant.get("parent_restaurant_id") is not None:
            raise HTTPException(status_code=403, detail="No tiene permiso para eliminar esta credencial")

    deleted = await db.db_delete_webauthn_credential(credential_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Credencial no encontrada o ya eliminada")

    return {"success": True, "deleted_credential_id": credential_id}
