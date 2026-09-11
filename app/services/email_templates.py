"""app/services/email_templates.py

HTML + plain-text bodies for Mesio's transactional emails.

These are rendered by app/services/email.py's `send_email()`. Kept as pure
string-building functions (no I/O, no DB) so they are trivial to unit test —
each `render_*` function returns `(subject, html, text)`.

Design notes:
  - Inline styles only. Email clients (Outlook, Gmail app, etc.) are not
    browsers — no external stylesheets, no <style> blocks relied upon for
    critical layout, no JS.
  - Spanish copy for Colombian restaurant owners — matches the tone used in
    weekly_reports_repo.format_report_message and the existing WhatsApp
    copy in whatsapp_messaging.py / routes/internal/crm.py.
  - Every function returns a plain-text alternative too. Some clients and
    spam filters penalize HTML-only email.
"""
from __future__ import annotations

from datetime import date
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

_MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def render_weekly_report_email(
    restaurant_name: str,
    message_text: str,
    dashboard_url: str,
    week_start: date,
    week_end: date,
) -> tuple[str, str, str]:
    """Wrap the existing WhatsApp-style report text into an HTML email.

    `message_text` is `weekly_reports_repo.format_report_message(...)`'s
    output — a short Spanish summary with emoji bullets. We reuse it
    verbatim as the plain-text alternative and render each line as an HTML
    paragraph/list item so the same underlying stats logic (untouched)
    drives both channels.
    """
    from datetime import timedelta

    last_day = week_end - timedelta(days=1)
    month_name = _MONTHS_ES[week_start.month]
    name = _esc(restaurant_name or "tu restaurante")
    subject = f"Tu reporte semanal Mesio — {restaurant_name or 'tu restaurante'}"

    # Split the WhatsApp-style message into non-empty lines and render each
    # as its own paragraph, preserving the original text as the `text` alt.
    lines = [ln for ln in message_text.split("\n") if ln.strip()]
    # First line is the greeting/header — treat it separately for styling.
    header_line = _esc(lines[0]) if lines else f"Tu reporte Mesio — semana del {week_start.day} al {last_day.day} de {month_name}"
    body_lines = lines[1:] if len(lines) > 1 else []

    items_html = "".join(
        f'<p style="margin:0 0 10px;font-size:15px;">{_esc(line)}</p>'
        for line in body_lines
        if not line.startswith("Ver detalle")
    )

    body_html = f"""\
      <p style="margin:0 0 4px;font-size:13px;color:{_MUTED_COLOR};text-transform:uppercase;letter-spacing:0.5px;">
        Reporte semanal · {name}
      </p>
      <p style="margin:0 0 20px;font-size:17px;font-weight:bold;">{header_line}</p>
      {items_html}
      <div style="margin:24px 0 4px;text-align:center;">
        <a href="{_esc(dashboard_url)}"
           style="display:inline-block;background:{_BRAND_COLOR};color:#ffffff;text-decoration:none;
                  padding:12px 28px;border-radius:8px;font-weight:bold;font-size:14px;">
          Ver detalle en el dashboard
        </a>
      </div>
    """
    html = _wrap_html(header_line, body_html)
    return subject, html, message_text


# ── 3. Client welcome (CRM convert flow) ──────────────────────────────────

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
