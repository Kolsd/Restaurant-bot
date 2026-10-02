"""app/services/email_templates.py

HTML + plain-text bodies for Mesio's transactional emails.

These are rendered by app/services/email.py's `send_email()`. Kept as pure
string-building functions (no I/O, no DB) so they are trivial to unit test —
each `render_*` function returns `(subject, html, text)`.

Design notes:
  - Inline styles only. Email clients (Outlook, Gmail app, etc.) are not
    browsers — no external stylesheets, no <style> blocks relied upon for
    critical layout, no JS.
  - Spanish copy for Colombian restaurant owners.
  - Every function returns a plain-text alternative too. Some clients and
    spam filters penalize HTML-only email.
"""
from __future__ import annotations

from html import escape as _esc

_BRAND_COLOR = "#1D9E75"        # tokens.css --brand
_TEXT_COLOR = "#1a1a1a"
_MUTED_COLOR = "#6b7280"
_BG_COLOR = "#f4f6f5"
_CARD_BG = "#ffffff"


def _wrap_html(preheader: str, body_html: str) -> str:
    """Shared page shell: centered card, Mesio brand color, mobile-safe width."""
    return f"""\
<div style="background:{_BG_COLOR};padding:24px 12px;font-family:Arial,Helvetica,sans-serif;">
  <span style="display:none;max-height:0;overflow:hidden;opacity:0;">{_esc(preheader)}</span>
  <div style="max-width:480px;margin:0 auto;background:{_CARD_BG};border-radius:12px;overflow:hidden;border:1px solid #e5e7eb;">
    <div style="background:{_BRAND_COLOR};padding:20px 28px;">
      <span style="color:#ffffff;font-size:20px;font-weight:bold;letter-spacing:0.5px;">Mesio</span>
    </div>
    <div style="padding:28px;color:{_TEXT_COLOR};font-size:15px;line-height:1.55;">
      {body_html}
    </div>
    <div style="padding:16px 28px;border-top:1px solid #e5e7eb;">
      <p style="margin:0;color:{_MUTED_COLOR};font-size:12px;">
        Mesio — software para restaurantes. Si no esperabas este correo, puedes ignorarlo con tranquilidad.
      </p>
    </div>
  </div>
</div>"""


# ── 1. Password reset ─────────────────────────────────────────────────────

def render_password_reset_email(code: str, restaurant_name: str) -> tuple[str, str, str]:
    """Password-reset OTP email. `code` is the plaintext 6-digit code.

    Callers MUST NOT log `code` alongside this function's output — that is
    the caller's responsibility (see auth_routes.py forgot-password, which
    only ever logs an email prefix, never the code).
    """
    name = _esc(restaurant_name or "tu restaurante")
    subject = "Tu código para restablecer tu contraseña en Mesio"

    body_html = f"""\
      <p style="margin:0 0 16px;">Hola,</p>
      <p style="margin:0 0 16px;">
        Recibimos una solicitud para restablecer la contraseña de la cuenta de
        <strong>{name}</strong> en Mesio. Usa este código para continuar:
      </p>
      <div style="margin:24px 0;text-align:center;">
        <span style="display:inline-block;background:{_BG_COLOR};border:1px solid #e5e7eb;border-radius:8px;
                     padding:16px 24px;font-size:32px;font-weight:bold;letter-spacing:8px;color:{_BRAND_COLOR};">
          {_esc(code)}
        </span>
      </div>
      <p style="margin:0 0 16px;">
        Este código vence en <strong>15 minutos</strong> y solo puede usarse una vez.
      </p>
      <p style="margin:0 0 16px;color:{_MUTED_COLOR};">
        Si tú no pediste este cambio, no hagas nada — tu contraseña actual sigue siendo válida.
        Nunca compartas este código con nadie, ni siquiera con alguien que diga ser del equipo de Mesio.
      </p>
    """
    html = _wrap_html(f"Tu código Mesio: {code}", body_html)

    text = (
        f"Hola,\n\n"
        f"Recibimos una solicitud para restablecer la contraseña de la cuenta de "
        f"{restaurant_name or 'tu restaurante'} en Mesio.\n\n"
        f"Tu código: {code}\n\n"
        f"Vence en 15 minutos y solo puede usarse una vez.\n\n"
        f"Si tú no pediste este cambio, no hagas nada — tu contraseña actual sigue "
        f"siendo válida. Nunca compartas este código con nadie.\n\n"
        f"— Mesio"
    )
    return subject, html, text


# ── 2. Weekly owner report ────────────────────────────────────────────────

def render_welcome_email(
    restaurant_name: str,
    username: str,
    temp_password: str,
    login_url: str,
) -> tuple[str, str, str]:
    """New-owner welcome email with login credentials.

    Callers MUST NOT log `temp_password` — mirrors the existing rule in
    routes/internal/crm.py (`_send_welcome_whatsapp`), which already keeps
    the password out of structlog and Sentry.
    """
    name = _esc(restaurant_name or "tu restaurante")
    subject = "¡Bienvenido a Mesio! Tus datos de acceso"

    body_html = f"""\
      <p style="margin:0 0 16px;">¡Bienvenido a Mesio! 🎉</p>
      <p style="margin:0 0 16px;">
        La cuenta de <strong>{name}</strong> ya está lista. Estos son tus datos
        de acceso al panel administrativo:
      </p>
      <div style="margin:20px 0;background:{_BG_COLOR};border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;">
        <p style="margin:0 0 8px;font-size:14px;">
          <span style="color:{_MUTED_COLOR};">Usuario:</span>
          <strong>{_esc(username)}</strong>
        </p>
        <p style="margin:0;font-size:14px;">
          <span style="color:{_MUTED_COLOR};">Contraseña temporal:</span>
          <strong style="letter-spacing:1px;">{_esc(temp_password)}</strong>
        </p>
      </div>
      <div style="margin:24px 0;text-align:center;">
        <a href="{_esc(login_url)}"
           style="display:inline-block;background:{_BRAND_COLOR};color:#ffffff;text-decoration:none;
                  padding:12px 28px;border-radius:8px;font-weight:bold;font-size:14px;">
          Iniciar sesión
        </a>
      </div>
      <p style="margin:0;color:{_MUTED_COLOR};">
        Por seguridad, te recomendamos cambiar esta contraseña la primera vez que inicies sesión.
      </p>
    """
    html = _wrap_html("Tu cuenta Mesio ya está lista", body_html)

    text = (
        f"¡Bienvenido a Mesio!\n\n"
        f"La cuenta de {restaurant_name or 'tu restaurante'} ya está lista.\n\n"
        f"Usuario: {username}\n"
        f"Contraseña temporal: {temp_password}\n\n"
        f"Inicia sesión en: {login_url}\n\n"
        f"Por seguridad, cambia esta contraseña la primera vez que inicies sesión.\n\n"
        f"— Mesio"
    )
    return subject, html, text


def render_account_setup_email(restaurant_name: str, username: str, code: str, setup_url: str) -> tuple[str, str, str]:
    """Welcome for an account Mesio opened (CRM convert, Superadmin › Usuarios).

    Carries a set-your-password code instead of a password: Mesio never sets
    or sees one (PM decision 2026-10-02). The code is the same one-time,
    15-minute code as "¿Olvidaste tu contraseña?"; the page asks for a new
    one when it has expired. Callers MUST NOT log `code`.
    """
    name = _esc(restaurant_name or "tu restaurante")
    subject = "¡Bienvenido a Mesio! Crea tu contraseña"

    body_html = f"""\
      <p style="margin:0 0 16px;">¡Bienvenido a Mesio! 🎉</p>
      <p style="margin:0 0 16px;">
        La cuenta de <strong>{name}</strong> ya está lista. Tu usuario es
        <strong>{_esc(username)}</strong>. Solo falta que crees tu contraseña con este código:
      </p>
      <div style="margin:24px 0;text-align:center;">
        <span style="display:inline-block;background:{_BG_COLOR};border:1px solid #e5e7eb;border-radius:8px;
                     padding:16px 24px;font-size:32px;font-weight:bold;letter-spacing:8px;color:{_BRAND_COLOR};">
          {_esc(code)}
        </span>
      </div>
      <div style="margin:24px 0;text-align:center;">
        <a href="{_esc(setup_url)}"
           style="display:inline-block;background:{_BRAND_COLOR};color:#ffffff;text-decoration:none;
                  padding:12px 28px;border-radius:8px;font-weight:bold;font-size:14px;">
          Crear mi contraseña
        </a>
      </div>
      <p style="margin:0 0 16px;">
        El código vence en <strong>15 minutos</strong>. Si se venció, en esa misma página
        pide uno nuevo con tu correo ("¿Olvidaste tu contraseña?").
      </p>
      <p style="margin:0;color:{_MUTED_COLOR};">
        Nadie de Mesio conoce tu contraseña ni te la va a pedir. No compartas este código.
      </p>
    """
    html = _wrap_html("Tu cuenta Mesio ya está lista: crea tu contraseña", body_html)
    text = (
        f"¡Bienvenido a Mesio!\n\n"
        f"La cuenta de {restaurant_name or 'tu restaurante'} ya está lista.\n"
        f"Usuario: {username}\n\n"
        f"Crea tu contraseña con este código (vence en 15 minutos): {code}\n"
        f"Entra aquí: {setup_url}\n\n"
        f"Si el código se venció, pide uno nuevo en esa página con tu correo.\n"
        f"Nadie de Mesio conoce tu contraseña ni te la va a pedir.\n\n"
        f"— Mesio"
    )
    return subject, html, text
