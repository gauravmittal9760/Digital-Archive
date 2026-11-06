"""E-mail one-time passwords: issue, throttle (1 per minute, hourly caps) and verify."""
from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

from sqlalchemy import insert, select, update

from config import (IS_PRODUCTION, OTP_HOURLY_LIMIT, OTP_MAX_VERIFY_ATTEMPTS, OTP_RESEND_SECONDS,
                    OTP_TTL_MINUTES, SECRET_KEY)
from database import aware, engine, new_id, otp_codes, utcnow
from services.mail_service import MailError, mail_configured, otp_email, send_email
from services.security import numeric_code, register_hit

PURPOSE_TEXT = {
    "reset_password": ("Reset your Digital Archive password", "Your password reset code"),
    "forgot_username": ("Your Digital Archive username lookup code", "Your username lookup code"),
    "email_change": ("Confirm your new e-mail address", "Confirm your new e-mail address"),
    "login_2fa": ("Your Digital Archive sign-in code", "Your sign-in verification code"),
    "enable_2fa": ("Turn on two-step verification", "Confirm two-step verification"),
    "admin_reset": ("Admin panel recovery code", "Your admin recovery code"),
}


def _code_hash(row_id: str, code: str) -> str:
    return hmac.new(SECRET_KEY.encode(), f"{row_id}:{code}".encode(), hashlib.sha256).hexdigest()


def seconds_until_resend(purpose: str, email: str) -> int:
    with engine.connect() as conn:
        created = conn.execute(select(otp_codes.c.created_at).where(
            otp_codes.c.purpose == purpose, otp_codes.c.email == email
        ).order_by(otp_codes.c.created_at.desc()).limit(1)).scalar()
    created = aware(created)
    if not created:
        return 0
    wait = OTP_RESEND_SECONDS - (utcnow() - created).total_seconds()
    return max(0, int(wait + 0.999))


def issue_otp(purpose: str, email: str, user_id: str | None = None, ip: str | None = None) -> dict:
    """Create and e-mail a code. Returns {"status": ok|cooldown|limited|mail_error, ...}."""
    email = email.strip().lower()
    wait = seconds_until_resend(purpose, email)
    if wait:
        return {"status": "cooldown", "wait": wait}
    # Hourly caps per address and per IP keep a single user from burning the provider quota.
    _, blocked_for = register_hit(f"otp:email:{email}", OTP_HOURLY_LIMIT, 3600, 3600)
    if blocked_for:
        return {"status": "limited", "wait": blocked_for}
    if ip:
        _, blocked_for = register_hit(f"otp:ip:{ip}", 15, 3600, 3600)
        if blocked_for:
            return {"status": "limited", "wait": blocked_for}

    code = numeric_code(6)
    row_id = new_id()
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(update(otp_codes).where(
            otp_codes.c.purpose == purpose, otp_codes.c.email == email, otp_codes.c.used_at.is_(None)
        ).values(used_at=now))
        conn.execute(insert(otp_codes).values(
            id=row_id, purpose=purpose, email=email, user_id=user_id, code_hash=_code_hash(row_id, code),
            attempts=0, expires_at=now + timedelta(minutes=OTP_TTL_MINUTES), ip_address=ip, created_at=now))
    subject, line = PURPOSE_TEXT.get(purpose, ("Your Digital Archive code", "Your verification code"))
    html_body, text_body = otp_email(code, line, OTP_TTL_MINUTES)
    try:
        send_email(email, subject, html_body, text_body)
    except MailError:
        with engine.begin() as conn:      # do not keep a code nobody received
            conn.execute(update(otp_codes).where(otp_codes.c.id == row_id).values(used_at=utcnow()))
        return {"status": "mail_error"}
    result = {"status": "ok", "wait": OTP_RESEND_SECONDS}
    if not mail_configured() and not IS_PRODUCTION:
        result["dev_code"] = code           # local development only; printed to console as well
    return result


def verify_otp(purpose: str, email: str, code: str) -> str:
    """Return one of: ok | invalid | expired | too_many | none."""
    email = email.strip().lower()
    code = (code or "").strip().replace(" ", "")
    now = utcnow()
    with engine.begin() as conn:
        row = conn.execute(select(otp_codes).where(
            otp_codes.c.purpose == purpose, otp_codes.c.email == email, otp_codes.c.used_at.is_(None)
        ).order_by(otp_codes.c.created_at.desc()).limit(1)).mappings().first()
        if not row:
            return "none"
        if aware(row["expires_at"]) <= now:
            conn.execute(update(otp_codes).where(otp_codes.c.id == row["id"]).values(used_at=now))
            return "expired"
        if int(row["attempts"]) >= OTP_MAX_VERIFY_ATTEMPTS:
            conn.execute(update(otp_codes).where(otp_codes.c.id == row["id"]).values(used_at=now))
            return "too_many"
        if not code.isdigit() or not hmac.compare_digest(_code_hash(row["id"], code), row["code_hash"]):
            attempts = int(row["attempts"]) + 1
            values = {"attempts": attempts}
            if attempts >= OTP_MAX_VERIFY_ATTEMPTS:
                values["used_at"] = now
            conn.execute(update(otp_codes).where(otp_codes.c.id == row["id"]).values(**values))
            return "too_many" if attempts >= OTP_MAX_VERIFY_ATTEMPTS else "invalid"
        conn.execute(update(otp_codes).where(otp_codes.c.id == row["id"]).values(used_at=now))
        return "ok"


def purge_old_otps() -> None:
    from sqlalchemy import delete
    cutoff = utcnow() - timedelta(days=2)
    with engine.begin() as conn:
        conn.execute(delete(otp_codes).where(otp_codes.c.created_at < cutoff))
