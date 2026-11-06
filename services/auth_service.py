"""Account lifecycle: sign-up, username login with lock-out, recovery helpers."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, insert, select, update

import config
from database import engine, media, file_versions, new_id, login_history, profiles, users, utcnow, aware
from services import security
from services.events import client_agent, client_ip, log_event

USER_COLUMNS = [
    users.c.id, users.c.email, users.c.password_hash, users.c.security_question, users.c.security_answer_hash,
    users.c.session_version, users.c.is_blocked, users.c.blocked_reason, users.c.locked_until, users.c.theme,
    users.c.ai_enabled, users.c.two_step_enabled, users.c.last_login_at, users.c.created_at,
    profiles.c.username, profiles.c.display_name, profiles.c.avatar_url, profiles.c.bio,
]


def _joined():
    return select(*USER_COLUMNS).select_from(users.join(profiles, profiles.c.id == users.c.id))


def get_user(user_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(_joined().where(users.c.id == user_id)).mappings().first()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(_joined().where(func.lower(profiles.c.username) == (username or "").strip().lower())).mappings().first()
    return dict(row) if row else None


def usernames_for_email(email: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(select(profiles.c.username).select_from(users.join(profiles, profiles.c.id == users.c.id))
                            .where(func.lower(users.c.email) == email.strip().lower())
                            .order_by(profiles.c.username)).all()
    return [r[0] for r in rows]


def username_taken(username: str, exclude_user_id: str | None = None) -> bool:
    stmt = select(profiles.c.id).where(func.lower(profiles.c.username) == username.strip().lower())
    if exclude_user_id:
        stmt = stmt.where(profiles.c.id != exclude_user_id)
    with engine.connect() as conn:
        return conn.execute(stmt).first() is not None


def is_reserved_username(username: str) -> bool:
    lowered = username.strip().lower()
    if lowered in security.RESERVED_USERNAMES:
        return True
    return bool(config.ADMIN_USERNAME) and lowered == config.ADMIN_USERNAME.strip().lower()


# --------------------------------------------------------------------------- sign-up
class SignupError(ValueError):
    pass


def create_user(username: str, email: str, password: str, display_name: str, question: str, answer: str) -> dict:
    username = username.strip()
    email = email.strip().lower()
    display_name = (display_name or "").strip()[:100] or username
    if not security.USERNAME_RE.fullmatch(username):
        raise SignupError("Username must be 3-30 characters: letters, numbers, dot, dash or underscore.")
    if is_reserved_username(username):
        raise SignupError("This username is reserved. Please choose another one.")
    if not security.EMAIL_RE.fullmatch(email):
        raise SignupError("Please enter a valid e-mail address.")
    problem = security.password_problem(password)
    if problem:
        raise SignupError(problem)
    if not question or len(question.strip()) < 8 or len(question) > 200:
        raise SignupError("Please choose or write a security question.")
    if len(security.normalize_answer(answer)) < 2:
        raise SignupError("Please enter an answer to your security question.")
    if username_taken(username):
        raise SignupError("That username is already taken. Please choose another one.")

    user_id, now = new_id(), utcnow()
    with engine.begin() as conn:
        conn.execute(insert(users).values(
            id=user_id, email=email, password_hash=security.hash_password(password),
            security_question=question.strip(), security_answer_hash=security.hash_answer(answer),
            created_at=now, updated_at=now))
        conn.execute(insert(profiles).values(
            id=user_id, username=username, display_name=display_name, created_at=now, updated_at=now))
    log_event("account_created", "New account registered", user_id=user_id, username=username, email=email)
    return get_user(user_id)


# ----------------------------------------------------------------------------- login
def authenticate(username: str, password: str) -> dict:
    """Check credentials with lock-out. Returns {"status": ...}.

    status: ok | invalid | locked | blocked.  `attempts_left` is reported uniformly for known
    and unknown usernames so the response does not reveal which usernames exist.
    """
    uname = (username or "").strip().lower()
    ip = client_ip() or "unknown"
    lock_seconds = config.LOGIN_LOCK_MINUTES * 60
    user_key, ip_key = f"login:u:{uname}", f"login:ip:{ip}"

    wait = max(security.blocked_seconds(user_key), security.blocked_seconds(ip_key))
    if wait:
        return {"status": "locked", "wait": wait}

    user = get_user_by_username(uname)
    password_ok = security.verify_password(user["password_hash"], password) if user else False
    if not user:
        security.burn_password_check(password)

    if not password_ok:
        count, blocked_for = security.register_hit(user_key, config.LOGIN_MAX_ATTEMPTS, lock_seconds * 2, lock_seconds)
        security.register_hit(ip_key, 40, lock_seconds, lock_seconds)
        if user and blocked_for:
            with engine.begin() as conn:
                conn.execute(update(users).where(users.c.id == user["id"]).values(
                    locked_until=utcnow() + timedelta(seconds=blocked_for)))
            log_event("account_locked", f"Locked for {config.LOGIN_LOCK_MINUTES} minutes after "
                      f"{config.LOGIN_MAX_ATTEMPTS} wrong password attempts", user_id=user["id"],
                      username=user["username"], email=user["email"])
        if blocked_for:
            return {"status": "locked", "wait": blocked_for}
        return {"status": "invalid", "attempts_left": max(0, config.LOGIN_MAX_ATTEMPTS - count)}

    if user["is_blocked"]:
        return {"status": "blocked", "reason": user.get("blocked_reason") or ""}

    security.reset_limit(user_key)
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(update(users).where(users.c.id == user["id"]).values(locked_until=None, last_login_at=now))
        conn.execute(insert(login_history).values(
            id=new_id(), user_id=user["id"], success=True, ip_address=ip[:64], user_agent=client_agent(), created_at=now))
    return {"status": "ok", "user": user}


# ----------------------------------------------------------------------- credentials
def set_password(user_id: str, new_password: str, event: str = "password_changed", details: str = "") -> None:
    """Change the password and sign out every other session (session_version bump)."""
    problem = security.password_problem(new_password)
    if problem:
        raise SignupError(problem)
    with engine.begin() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(
            password_hash=security.hash_password(new_password), session_version=users.c.session_version + 1,
            updated_at=utcnow()))
    user = get_user(user_id) or {}
    log_event(event, details or "Password was changed", user_id=user_id, username=user.get("username"), email=user.get("email"))


def set_email(user_id: str, new_email: str) -> str:
    new_email = new_email.strip().lower()
    if not security.EMAIL_RE.fullmatch(new_email):
        raise SignupError("Please enter a valid e-mail address.")
    old = get_user(user_id) or {}
    with engine.begin() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(email=new_email, updated_at=utcnow()))
    log_event("email_changed", f"E-mail changed from {old.get('email')} to {new_email}", user_id=user_id,
              username=old.get("username"), email=new_email)
    return new_email


def set_security_qa(user_id: str, question: str, answer: str) -> None:
    if not question or len(question.strip()) < 8 or len(question) > 200:
        raise SignupError("Please choose or write a security question.")
    if len(security.normalize_answer(answer)) < 2:
        raise SignupError("Please enter an answer.")
    with engine.begin() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(
            security_question=question.strip(), security_answer_hash=security.hash_answer(answer), updated_at=utcnow()))
    user = get_user(user_id) or {}
    log_event("security_question_changed", "Security question was updated", user_id=user_id,
              username=user.get("username"), email=user.get("email"))


def set_username(user_id: str, new_username: str) -> None:
    new_username = new_username.strip()
    if not security.USERNAME_RE.fullmatch(new_username):
        raise SignupError("Username must be 3-30 characters: letters, numbers, dot, dash or underscore.")
    if is_reserved_username(new_username):
        raise SignupError("This username is reserved. Please choose another one.")
    if username_taken(new_username, exclude_user_id=user_id):
        raise SignupError("That username is already taken.")
    old = get_user(user_id) or {}
    with engine.begin() as conn:
        conn.execute(update(profiles).where(profiles.c.id == user_id).values(username=new_username, updated_at=utcnow()))
    log_event("username_changed", f"Username changed from {old.get('username')} to {new_username}", user_id=user_id,
              username=new_username, email=old.get("email"))


# ------------------------------------------------------------ recovery (security Q)
def check_recovery_answer(user: dict, answer: str) -> tuple[bool, int]:
    """Verify a security answer with its own lock-out. Returns (ok, wait_seconds)."""
    key = f"recovery:{user['id']}"
    wait = security.blocked_seconds(key)
    if wait:
        return False, wait
    if user.get("security_answer_hash") and security.verify_answer(user["security_answer_hash"], answer):
        security.reset_limit(key)
        return True, 0
    _, blocked_for = security.register_hit(key, config.LOGIN_MAX_ATTEMPTS, config.LOGIN_LOCK_MINUTES * 120,
                                           config.LOGIN_LOCK_MINUTES * 60)
    return False, blocked_for


# ------------------------------------------------------------------ admin operations
def block_user(user_id: str, reason: str = "") -> None:
    user = get_user(user_id)
    if not user:
        return
    with engine.begin() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(
            is_blocked=True, blocked_reason=(reason or "")[:300], blocked_at=utcnow(),
            session_version=users.c.session_version + 1))
    log_event("blocked_by_admin", reason or "Blocked by the administrator", user_id=user_id,
              username=user["username"], email=user["email"])


def unblock_user(user_id: str) -> None:
    user = get_user(user_id)
    if not user:
        return
    with engine.begin() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(
            is_blocked=False, blocked_reason=None, blocked_at=None, locked_until=None))
    security.reset_limit(f"login:u:{user['username'].lower()}")
    security.reset_limit(f"recovery:{user_id}")
    log_event("unblocked_by_admin", "Account unblocked by the administrator", user_id=user_id,
              username=user["username"], email=user["email"])


def is_locked(user: dict) -> bool:
    until = aware(user.get("locked_until"))
    return bool(until and until > utcnow())
