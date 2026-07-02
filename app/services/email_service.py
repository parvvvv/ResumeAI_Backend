"""
Transactional email via Resend's REST API (no SDK dependency).

Env-gated: when RESEND_API_KEY is unset, sends are skipped and callers
should treat email-dependent flows as auto-approved (dev mode). All sends
are best-effort — a failed email must never fail the calling request.
"""

from __future__ import annotations

import structlog

from app.config import settings
from app.runtime import try_get_runtime

logger = structlog.get_logger()

_RESEND_ENDPOINT = "https://api.resend.com/emails"


def is_email_configured() -> bool:
    return bool(settings.RESEND_API_KEY)


async def send_email(to: str, subject: str, html: str) -> bool:
    """Send one email. Returns True on success, False otherwise (never raises)."""
    if not is_email_configured():
        logger.info("email_skipped_not_configured", to=to, subject=subject)
        return False

    runtime = try_get_runtime()
    if runtime is None:
        logger.warning("email_skipped_no_runtime", to=to)
        return False

    try:
        resp = await runtime.http_client.post(
            _RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": settings.EMAIL_FROM,
                "to": [to],
                "subject": subject,
                "html": html,
            },
        )
        if resp.status_code in (200, 201):
            logger.info("email_sent", to=to, subject=subject)
            return True
        logger.warning("email_send_failed", to=to, status=resp.status_code, body=resp.text[:200])
        return False
    except Exception as exc:
        logger.warning("email_send_error", to=to, error=str(exc))
        return False


def _layout(title: str, body_html: str, cta_label: str, cta_url: str) -> str:
    """Minimal, client-safe email layout."""
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;color:#1a1d26;">
  <h2 style="margin:0 0 8px;font-size:20px;">{title}</h2>
  {body_html}
  <a href="{cta_url}"
     style="display:inline-block;margin:24px 0;padding:12px 24px;background:#2f6df6;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;">
    {cta_label}
  </a>
  <p style="font-size:12px;color:#6b7280;margin-top:24px;">
    If the button doesn't work, copy this link into your browser:<br>
    <span style="word-break:break-all;">{cta_url}</span>
  </p>
  <p style="font-size:12px;color:#9ca3af;">If you didn't request this, you can safely ignore this email.</p>
</div>"""


async def send_verification_email(to: str, token: str) -> bool:
    url = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    return await send_email(
        to,
        "Verify your Hirecraft email",
        _layout(
            "Welcome to Hirecraft 👋",
            "<p style='font-size:14px;line-height:1.6;'>Confirm your email address to activate your account and start tailoring your resume.</p>",
            "Verify Email",
            url,
        ),
    )


async def send_password_reset_email(to: str, token: str) -> bool:
    url = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    return await send_email(
        to,
        "Reset your Hirecraft password",
        _layout(
            "Reset your password",
            f"<p style='font-size:14px;line-height:1.6;'>We received a request to reset your password. This link expires in {settings.PASSWORD_RESET_TOKEN_HOURS} hour(s).</p>",
            "Reset Password",
            url,
        ),
    )
