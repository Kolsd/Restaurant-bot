"""
app/services/provisioning.py

Creating a tenant: organization + its first sede + its owner + the trial.

This used to live inside `POST /api/internal/crm/prospects/{id}/convert`,
which meant a restaurant could only exist if somebody at Mesio clicked a
button — the whole funnel ran through one person's calendar. The sequence
itself was never CRM-specific, so it lives here and has two callers: the
public self-serve signup (`/api/signup`) and the CRM convert, which keeps
its prospect bookkeeping and delegates the creation.

Order matters and is not arbitrary:

  1. organization  — must exist first; everything else references its id.
  2. sede          — an org with no location can be created but cannot
                     operate: every staff-facing listing filters by sede.
  3. trial         — before the owner can log in, so they never see a
                     "plan vencido" screen on their first visit.
  4. owner user    — last of the required steps, because it is the only one
                     that can collide on a UNIQUE username and we would
                     rather fail before charging anyone than after.
  5. welcome email — best effort, NEVER required. The owner chooses their
                     own password on self-serve signup precisely so that a
                     missing RESEND_API_KEY degrades the welcome mail into
                     a nicety instead of locking them out of the account
                     they just created. An account Mesio opens (CRM
                     convert) has no password at all: its email carries the
                     code the owner creates one with — Mesio never sets,
                     sees or relays a password (PM decision 2026-10-02).

Steps 1-4 are not one database transaction: the repo functions each open
their own connection, and stitching them into one would mean reaching past
them into raw SQL. A failure between them is therefore reported with the
stage it failed at (`ProvisioningError.stage`) so the caller can say what
exists and what does not, instead of a bare 500.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.services.logging import get_logger

log = get_logger(__name__)

# The trial the landing page advertises. It is the promise, so it is the
# default; a caller may shorten it but should not have to know the number.
DEFAULT_TRIAL_DAYS = 15

# The sede every org is born with. Renaming it is the restaurant's business.
DEFAULT_LOCATION_NAME = "Principal"


class ProvisioningError(RuntimeError):
    """A tenant could not be created. `stage` says how far it got."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


class TenantAlreadyExists(ProvisioningError):
    """Something unique about this tenant is already taken (phone, username)."""

    def __init__(self, stage: str, message: str):
        super().__init__(stage, message)


@dataclass
class ProvisionedTenant:
    """Everything a caller needs to tell the new owner how to get in."""

    org: dict
    location: dict
    username: str
    user_created: bool
    comp_until: datetime | None
    # Self-serve: the welcome email. Mesio-opened: the set-your-password
    # code, True only when it really left (Resend configured).
    welcome_email_sent: bool

    @property
    def org_id(self) -> int:
        return int(self.org["id"])

    @property
    def location_id(self) -> int | None:
        loc_id = self.location.get("id") if self.location else None
        return int(loc_id) if loc_id is not None else None


def username_from(email: str | None, restaurant_name: str) -> str:
    """Derive a login name when the caller did not pick one.

    The email's local part when there is an email, a slug of the restaurant
    name otherwise, and `owner` when neither yields anything usable.
    """
    email = (email or "").strip().lower()
    if email and "@" in email:
        local = email.split("@")[0]
        if local:
            return local[:60]
    folded = unicodedata.normalize("NFKD", restaurant_name or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9.]", "", folded.lower().replace(" ", "."))
    return slug[:20] or "owner"


def setup_url(email: str) -> str:
    """Absolute link to create the password: /reset-password, already on the
    "I have a code" step for this email. The email rides in the #fragment,
    which never reaches a server log."""
    from urllib.parse import quote  # noqa: PLC0415
    app_domain = os.getenv("APP_DOMAIN", "").strip() or "mesio.app"
    return f"https://{app_domain}/reset-password#codigo={quote(email)}"


def unusable_password_hash() -> str:
    """Hash of a random secret nobody ever sees, for accounts Mesio opens:
    the owner gets in only by creating their own password with an emailed
    code. Mesio never sets or sees a password (PM decision 2026-10-02)."""
    import secrets  # noqa: PLC0415

    from app.services.password_hash import hash_password  # noqa: PLC0415
    return hash_password(secrets.token_urlsafe(32))


async def send_account_setup(email: str, restaurant_name: str | None) -> bool:
    """Email a new owner their set-your-password code (the reset code, same
    15-minute TTL). True only when an email really left (Resend configured).
    Never raises; never logs the code."""
    import asyncpg  # noqa: PLC0415

    from app.repositories.password_reset_repo import db_create_password_reset  # noqa: PLC0415
    from app.services.email import delivers_for_real, send_email  # noqa: PLC0415
    from app.services.email_templates import render_account_setup_email  # noqa: PLC0415

    try:
        code = await db_create_password_reset(email)
        subject, html, text = render_account_setup_email(
            restaurant_name=restaurant_name or "tu restaurante", username=email,
            code=code, setup_url=setup_url(email),
        )
        sent = await send_email(to=email, subject=subject, html=html, text=text)
    except (asyncpg.PostgresError, OSError):
        log.exception("provisioning.account_setup_failed", email_prefix=email[:3] + "***")
        return False
    return bool(sent) and delivers_for_real()


def login_url() -> str:
    """Absolute /login URL for emails, from APP_DOMAIN."""
    app_domain = os.getenv("APP_DOMAIN", "").strip()
    return f"https://{app_domain}/login" if app_domain else "https://mesio.app/login"


async def create_tenant(
    *,
    restaurant_name: str,
    username: str | None = None,
    password: str | None = None,
    owner_email: str | None = None,
    owner_name: str | None = None,
    plan_code: str = "restaurante",
    trial_days: int = DEFAULT_TRIAL_DAYS,
    features: dict | None = None,
    location_name: str = DEFAULT_LOCATION_NAME,
    send_welcome_email: bool = True,
    allow_username_suffix: bool = True,
) -> ProvisionedTenant:
    """Create an organization, its first sede, its trial and its owner.

    `password` is the owner's own when the caller has one (self-serve
    signup, where the person typed it). None means Mesio opens the account
    (CRM convert): `owner_email` is then required, the account gets an
    unusable hash, and the owner is emailed a code to create their password.

    `allow_username_suffix` decides what happens when the login name is
    taken. The CRM convert wants a working account no matter what, so it
    accepts `juan.42`. Self-serve does NOT: a person who cannot guess their
    own username cannot log in, so it raises TenantAlreadyExists and the
    page tells them to sign in instead.

    Raises ProvisioningError (or TenantAlreadyExists) tagged with the stage
    that failed.
    """
    import asyncpg  # noqa: PLC0415

    from app.repositories import restaurant_repo  # noqa: PLC0415
    from app.services.password_hash import hash_password  # noqa: PLC0415

    restaurant_name = (restaurant_name or "").strip()
    if not restaurant_name:
        raise ProvisioningError("validate", "Falta el nombre del restaurante.")

    owner_email = (owner_email or "").strip().lower()
    final_username = (username or username_from(owner_email, restaurant_name)).strip().lower()
    if not final_username:
        raise ProvisioningError("validate", "No se pudo derivar un usuario.")

    # No password from the caller = an account Mesio opens (CRM convert):
    # it gets an unusable hash and the owner creates their own password with
    # an emailed code. Mesio never generates, shows or emails one.
    mesio_opened = not password
    if mesio_opened and "@" not in owner_email:
        raise ProvisioningError("validate", "Falta el email del dueño: con él crea su contraseña.")

    # ── 0. Is the login name free? ───────────────────────────────────────
    # Asked BEFORE anything is written. The org is created first and the
    # user last, so a name that turns out to be taken would otherwise leave
    # a committed organization behind on every repeat signup — a dead
    # tenant in the superadmin list and in the MRR roll-up, once per
    # attempt. Callers that accept a suffixed name (the CRM convert) skip
    # this: for them a collision is not an error.
    if not allow_username_suffix:
        try:
            taken = await restaurant_repo.db_get_user(final_username)
        except Exception:
            log.exception("provisioning.username_check_failed")
            taken = None
        if taken:
            raise TenantAlreadyExists("user", "Ya existe una cuenta con ese correo.")

    # ── 1. Organization ──────────────────────────────────────────────────
    try:
        org = await restaurant_repo.db_create_organization(
            name=restaurant_name,
            features=features or {},
            plan_code=plan_code,
        )
    except asyncpg.UniqueViolationError as exc:
        log.warning("provisioning.org_unique_violation", detail=str(exc)[:200])
        raise TenantAlreadyExists(
            "organization",
            "Ya existe una organización con ese teléfono o nombre.",
        ) from exc
    except Exception as exc:
        log.exception("provisioning.org_create_failed")
        raise ProvisioningError("organization", "No se pudo crear la organización.") from exc

    org_id = int(org["id"])

    # ── 2. First sede ────────────────────────────────────────────────────
    try:
        location = await restaurant_repo.db_create_location(
            org_id=org_id,
            name=location_name,
            code="principal",
            active=True,
        )
    except Exception as exc:
        log.exception("provisioning.location_create_failed", org_id=org_id)
        raise ProvisioningError(
            "location",
            f"Organización #{org_id} creada, pero falló la creación de la sede.",
        ) from exc

    # ── 3. Trial ─────────────────────────────────────────────────────────
    # Non-fatal on purpose: an org that exists without a trial can be fixed
    # from the admin screen in a second, whereas losing the whole signup
    # over it loses the customer.
    comp_until = None
    if trial_days and trial_days > 0:
        from app.repositories import plan_limits_repo  # noqa: PLC0415
        from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415

        candidate = datetime.now(tz=timezone.utc) + timedelta(days=trial_days)
        try:
            with bypass_tenant_scope("tenant_provisioning_trial"):
                await plan_limits_repo.db_set_comp_until(org_id, candidate)
            comp_until = candidate
        except Exception:
            log.exception("provisioning.trial_failed", org_id=org_id)

    # ── 4. Owner ─────────────────────────────────────────────────────────
    pw_hash = unusable_password_hash() if mesio_opened else hash_password(password)   # never log `password`
    user_created = False
    try:
        # branch_id carries the ORG id here (owner of the whole org, not of
        # one sede) and org_id is set explicitly so auth never has to guess
        # which kind of id it is holding. location_id stays NULL: an owner
        # is not pinned to a sede.
        user_created = await restaurant_repo.db_create_user(
            username=final_username,
            password_hash=pw_hash,
            restaurant_name=restaurant_name,
            role="owner",
            branch_id=org_id,
            org_id=org_id,
            display_name=owner_name,
        )
        if not user_created:
            if not allow_username_suffix:
                # Lost the race against a simultaneous signup: step 0 said
                # the name was free and it no longer is.
                await _discard_org(org_id)
                raise TenantAlreadyExists(
                    "user", "Ya existe una cuenta con ese correo."
                )
            final_username = f"{final_username}.{org_id}"
            user_created = await restaurant_repo.db_create_user(
                username=final_username,
                password_hash=pw_hash,
                restaurant_name=restaurant_name,
                role="owner",
                branch_id=org_id,
                org_id=org_id,
                display_name=owner_name,
            )
    except TenantAlreadyExists:
        raise
    except Exception as exc:
        log.exception("provisioning.user_create_failed", org_id=org_id)
        await _discard_org(org_id)
        raise ProvisioningError(
            "user",
            "No se pudo crear el usuario dueño. No se creó la cuenta.",
        ) from exc

    if not user_created:
        await _discard_org(org_id)
        raise ProvisioningError(
            "user",
            "No se pudo crear el usuario dueño. No se creó la cuenta.",
        )

    # ── 5. Welcome email (best effort) ───────────────────────────────────
    welcome_sent = False
    if mesio_opened:
        # Not optional: without this code the owner has no way in.
        welcome_sent = await send_account_setup(owner_email, restaurant_name)
    elif send_welcome_email and owner_email and "@" in owner_email:
        welcome_sent = await _send_welcome_email(
            restaurant_name=restaurant_name,
            username=final_username,
            to=owner_email,
            org_id=org_id,
        )

    log.info(
        "provisioning.tenant_created",
        org_id=org_id,
        location_id=location.get("id"),
        username=final_username,
        trial_days=trial_days if comp_until else 0,
        welcome_sent=welcome_sent,
        # password intentionally omitted
    )

    return ProvisionedTenant(
        org=org,
        location=location,
        username=final_username,
        user_created=True,
        comp_until=comp_until,
        welcome_email_sent=welcome_sent,
    )


async def _discard_org(org_id: int) -> None:
    """Undo a half-created tenant. Never raises — the caller is already failing.

    Only removes the empty shell (the repo refuses if a user, order or
    table is attached), so the worst case of a failure here is one orphan
    org to clean up by hand, never a deleted customer.
    """
    from app.repositories import restaurant_repo  # noqa: PLC0415

    try:
        removed = await restaurant_repo.db_delete_new_organization(org_id)
        if not removed:
            log.warning("provisioning.discard_org_refused", org_id=org_id)
    except Exception:
        log.exception("provisioning.discard_org_failed", org_id=org_id)


async def _send_welcome_email(
    *,
    restaurant_name: str,
    username: str,
    to: str,
    org_id: int,
) -> bool:
    """Send the welcome mail. Returns False on any failure — never raises."""
    from app.services.email import send_email  # noqa: PLC0415
    from app.services.email_templates import render_welcome_email  # noqa: PLC0415

    try:
        subject, html, text = render_welcome_email(
            restaurant_name=restaurant_name,
            username=username,
            # The owner picked their own password at signup; Mesio-opened
            # accounts get render_account_setup_email instead.
            temp_password="(la que elegiste al registrarte)",
            login_url=login_url(),
        )
        return await send_email(to=to, subject=subject, html=html, text=text)
    except Exception:
        log.exception("provisioning.welcome_email_failed", org_id=org_id)
        return False
