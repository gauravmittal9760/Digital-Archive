"""Transactional e-mail through the Brevo HTTP API (no SMTP, no extra dependency).

Required environment variables:
    BREVO_API_KEY        your Brevo v3 API key
    BREVO_SENDER_EMAIL   a sender address that is verified in your Brevo account
    BREVO_SENDER_NAME    optional display name
"""
from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.request

from config import BREVO_API_KEY, BREVO_SENDER_EMAIL, BREVO_SENDER_NAME, IS_PRODUCTION

log = logging.getLogger(__name__)
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def mail_configured() -> bool:
    return bool(BREVO_API_KEY and BREVO_SENDER_EMAIL)


class MailError(RuntimeError):
    pass


def send_email(to_email: str, subject: str, html_body: str, text_body: str = "", to_name: str = "") -> bool:
    """Send one e-mail. Returns True when handed to Brevo, False in dev-console mode.

    Raises MailError when the provider rejects the message.
    """
    if not mail_configured():
        if IS_PRODUCTION:
            raise MailError("E-mail is not configured (BREVO_API_KEY / BREVO_SENDER_EMAIL).")
        # Local development: print the message instead of failing so flows stay testable.
        log.warning("[DEV MAIL] to=%s subject=%s\n%s", to_email, subject, text_body or html_body)
        print(f"\n[DEV MAIL] To: {to_email}\nSubject: {subject}\n{text_body or html_body}\n")
        return False
    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": to_email, **({"name": to_name} if to_name else {})}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if text_body:
        payload["textContent"] = text_body
    request = urllib.request.Request(
        BREVO_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"api-key": BREVO_API_KEY, "accept": "application/json", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:300]
        log.error("Brevo rejected the message (%s): %s", exc.code, detail)
        raise MailError("The e-mail provider rejected the message.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.error("Brevo request failed: %s", exc)
        raise MailError("Could not reach the e-mail provider.") from exc


def otp_email(code: str, purpose_line: str, ttl_minutes: int) -> tuple[str, str]:
    """Return (html, text) bodies for a one-time-password message."""
    safe_code = html.escape(code)
    body = f"""<!doctype html><html><body style="margin:0;background:#f3f4fb;font-family:Segoe UI,Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 12px">
<table width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 8px 30px rgba(79,70,229,.12)">
<tr><td style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:26px 32px;color:#fff;font-size:20px;font-weight:700">Digital Archive</td></tr>
<tr><td style="padding:32px">
<p style="margin:0 0 8px;color:#0f172a;font-size:16px;font-weight:600">{html.escape(purpose_line)}</p>
<p style="margin:0 0 20px;color:#475569;font-size:14px;line-height:1.6">Use this one-time code. It expires in {ttl_minutes} minutes and works only once.</p>
<div style="text-align:center;margin:24px 0"><span style="display:inline-block;letter-spacing:10px;font-size:34px;font-weight:800;color:#4f46e5;background:#eef2ff;border-radius:14px;padding:14px 22px">{safe_code}</span></div>
<p style="margin:0;color:#64748b;font-size:12px;line-height:1.6">If you did not request this, you can safely ignore this e-mail. Never share this code with anyone.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = f"{purpose_line}\n\nYour one-time code: {code}\nIt expires in {ttl_minutes} minutes. If you did not request it, ignore this e-mail."
    return body, text
