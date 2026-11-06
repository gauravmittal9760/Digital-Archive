"""Security primitives: password rules, hashing, rate limiting, small validators."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, insert, select, update
from werkzeug.security import check_password_hash, generate_password_hash

from database import engine, rate_limits, utcnow, aware

USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,30}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")
RESERVED_USERNAMES = {"admin", "administrator", "root", "support", "system", "superuser", "owner", "staff", "moderator"}

SECURITY_QUESTIONS = [
    "What was the name of your first school?",
    "What is your mother's maiden name?",
    "What was the name of your childhood best friend?",
    "In which city were you born?",
    "What was your first pet's name?",
    "What is the name of your favourite teacher?",
    "What was the first mobile phone you owned?",
    "What is your favourite childhood game?",
]


# ---------------------------------------------------------------------- passwords
def password_problem(password: str) -> str | None:
    """Return a human-readable reason if the password is too weak, else None."""
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if not any(c.islower() for c in password) or not any(c.isupper() for c in password) or not any(c.isdigit() for c in password):
        return "Use an uppercase letter, a lowercase letter and a number in your password."
    if len(password) > 128:
        return "Password is too long (maximum 128 characters)."
    return None


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return bool(stored_hash) and check_password_hash(stored_hash, password or "")
    except Exception:
        return False


# a dummy hash so unknown usernames cost the same time as real ones (timing side channel)
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def burn_password_check(password: str) -> None:
    check_password_hash(_DUMMY_HASH, password or "")


def normalize_answer(answer: str) -> str:
    return re.sub(r"\s+", " ", (answer or "").strip().lower())


def hash_answer(answer: str) -> str:
    return generate_password_hash(normalize_answer(answer))


def verify_answer(stored_hash: str, answer: str) -> bool:
    return verify_password(stored_hash, normalize_answer(answer))


def safe_equals(a: str, b: str) -> bool:
    """Constant-time string comparison (both sides are hashed to equal length first)."""
    ha = hashlib.sha256((a or "").encode()).digest()
    hb = hashlib.sha256((b or "").encode()).digest()
    return hmac.compare_digest(ha, hb) and bool(a) and bool(b)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def numeric_code(length: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def mask_email(email: str) -> str:
    try:
        name, domain = email.split("@", 1)
    except ValueError:
        return "***"
    visible = name[:1] if len(name) <= 3 else name[:2]
    host, _, tld = domain.rpartition(".")
    return f"{visible}{'*' * max(3, len(name) - len(visible))}@{host[:1]}{'*' * max(2, len(host) - 1)}.{tld}"


# ------------------------------------------------------------------ rate limiting
def blocked_seconds(key: str) -> int:
    """Seconds remaining while `key` is blocked (0 when it is free)."""
    with engine.connect() as conn:
        row = conn.execute(select(rate_limits.c.blocked_until).where(rate_limits.c.key == key)).first()
    until = aware(row[0]) if row else None
    if until and until > utcnow():
        return max(1, int((until - utcnow()).total_seconds()))
    return 0


def register_hit(key: str, limit: int, window_seconds: int, block_seconds: int) -> tuple[int, int]:
    """Count one event. Returns (count_in_window, blocked_seconds_if_now_blocked)."""
    now = utcnow()
    with engine.begin() as conn:
        row = conn.execute(select(rate_limits).where(rate_limits.c.key == key)).mappings().first()
        if row is None:
            conn.execute(insert(rate_limits).values(key=key, count=1, window_start=now, blocked_until=None))
            count, blocked_until = 1, None
        else:
            window_start = aware(row["window_start"])
            blocked_until = aware(row["blocked_until"])
            expired_block = blocked_until is not None and blocked_until <= now
            if expired_block or (now - window_start).total_seconds() > window_seconds:
                count, window_start, blocked_until = 1, now, None
            else:
                count = int(row["count"]) + 1
            if count >= limit and not blocked_until:
                blocked_until = now + timedelta(seconds=block_seconds)
            conn.execute(update(rate_limits).where(rate_limits.c.key == key).values(
                count=count, window_start=window_start, blocked_until=blocked_until))
    remaining = int((blocked_until - now).total_seconds()) if blocked_until and blocked_until > now else 0
    return count, remaining


def reset_limit(key: str) -> None:
    with engine.begin() as conn:
        conn.execute(delete(rate_limits).where(rate_limits.c.key == key))


def reset_limits_with_prefix(prefix: str) -> None:
    with engine.begin() as conn:
        conn.execute(delete(rate_limits).where(rate_limits.c.key.like(prefix + "%")))


def human_duration(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds < 90:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = (seconds + 59) // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
