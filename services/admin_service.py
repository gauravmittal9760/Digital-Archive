"""Admin-panel data: settings, authentication helpers and aggregate queries (live from Neon)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, case, delete, func, insert, or_, select, update

import config
from database import (account_events, admin_avatars, admin_login_records, admin_settings, announcements, engine, login_history,
                      media, new_id, profiles, users, utcnow, aware, file_versions)
from services import security, stats_service


# ------------------------------------------------------------------------ settings
def get_setting(key: str, default: str | None = None) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(select(admin_settings.c.value).where(admin_settings.c.key == key)).first()
    return row[0] if row and row[0] is not None else default


def set_setting(key: str, value: str) -> None:
    with engine.begin() as conn:
        updated = conn.execute(update(admin_settings).where(admin_settings.c.key == key).values(value=value, updated_at=utcnow()))
        if not updated.rowcount:
            conn.execute(insert(admin_settings).values(key=key, value=value, updated_at=utcnow()))


def admin_email() -> str:
    return get_setting("admin_email") or config.ADMIN_EMAIL


def gate_ok(username: str, password: str) -> bool:
    """Credentials typed on the normal login page that open the admin gateway."""
    if not config.ADMIN_ENABLED:
        return False
    stored = get_setting("gate_password_hash")
    pw_ok = security.verify_password(stored, password) if stored else security.safe_equals(password, config.ADMIN_PASSWORD)
    return security.safe_equals(username.strip().lower(), config.ADMIN_USERNAME.lower()) and pw_ok


def panel_password_ok(password: str) -> bool:
    stored = get_setting("panel_password_hash")
    if stored:
        return security.verify_password(stored, password)
    return security.safe_equals(password, config.ADMIN_PANEL_PASSWORD)


def recovery_answer_ok(answer: str) -> bool:
    expected = security.normalize_answer(config.ADMIN_SECURITY_ANSWER)
    return bool(expected) and security.safe_equals(security.normalize_answer(answer), expected)


def recent_admin_records(limit: int = 100) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(admin_login_records).order_by(admin_login_records.c.created_at.desc()).limit(limit)).all()]


# ----------------------------------------------------------------------- overview
def overview() -> dict:
    now = utcnow()
    with engine.connect() as conn:
        total_users = conn.execute(select(func.count()).select_from(users)).scalar() or 0
        new_7 = conn.execute(select(func.count()).select_from(users).where(users.c.created_at > now - timedelta(days=7))).scalar() or 0
        new_30 = conn.execute(select(func.count()).select_from(users).where(users.c.created_at > now - timedelta(days=30))).scalar() or 0
        blocked = conn.execute(select(func.count()).select_from(users).where(users.c.is_blocked.is_(True))).scalar() or 0
        locked = conn.execute(select(func.count()).select_from(users).where(users.c.locked_until > now, users.c.is_blocked.is_(False))).scalar() or 0
        active_24 = conn.execute(select(func.count()).select_from(users).where(users.c.last_login_at > now - timedelta(hours=24))).scalar() or 0
        two_step = conn.execute(select(func.count()).select_from(users).where(users.c.two_step_enabled.is_(True))).scalar() or 0
        mrow = conn.execute(select(func.count(), func.coalesce(func.sum(media.c.file_size), 0), func.coalesce(func.sum(media.c.thumb_size), 0),
                                   func.coalesce(func.sum(media.c.original_size), 0)).select_from(media)).first()
        by_type = [(r[0], int(r[1]), int(r[2] or 0)) for r in conn.execute(select(media.c.media_type, func.count(), func.sum(media.c.file_size))
                                                                          .group_by(media.c.media_type)).all()]
        trash = conn.execute(select(func.count(), func.coalesce(func.sum(media.c.file_size), 0)).where(media.c.is_deleted.is_(True))).first()
        created_rows = conn.execute(select(users.c.created_at).where(users.c.created_at > now - timedelta(days=30))).all()
        top = [dict(r._mapping) for r in conn.execute(
            select(profiles.c.username, users.c.id,
                   func.coalesce(func.sum(media.c.file_size + func.coalesce(media.c.thumb_size, 0)), 0).label("bytes"),
                   func.count(media.c.id).label("files")).select_from(users.join(profiles, profiles.c.id == users.c.id)
                   .outerjoin(media, media.c.user_id == users.c.id)).group_by(profiles.c.username, users.c.id)
            .order_by(func.coalesce(func.sum(media.c.file_size), 0).desc()).limit(8)).all()]
        events = [dict(r._mapping) for r in conn.execute(select(account_events).order_by(account_events.c.created_at.desc()).limit(10)).all()]
    daily = {}
    for i in range(29, -1, -1):
        daily[(now - timedelta(days=i)).strftime("%d %b")] = 0
    for (created,) in created_rows:
        key = aware(created).strftime("%d %b")
        if key in daily:
            daily[key] += 1
    stored = int(mrow[1]) + int(mrow[2])
    original = int(mrow[3] or 0)
    return {"users": total_users, "new_7": new_7, "new_30": new_30, "blocked": blocked, "locked": locked, "active_24": active_24,
            "two_step": two_step, "files": int(mrow[0]), "stored": stored, "stored_h": stats_service.human_size(stored),
            "saved_h": stats_service.human_size(max(0, original - int(mrow[1]))), "by_type": by_type,
            "trash_files": int(trash[0]), "trash_h": stats_service.human_size(int(trash[1])), "signups": list(daily.items()),
            "top_users": [{**t, "bytes_h": stats_service.human_size(t["bytes"])} for t in top], "events": events}


# --------------------------------------------------------------------------- users
def list_users(search: str = "", status: str = "all", sort: str = "newest", limit: int = 300) -> list[dict]:
    now = utcnow()
    storage = (select(media.c.user_id.label("uid"), func.count().label("files"),
                      func.coalesce(func.sum(media.c.file_size + func.coalesce(media.c.thumb_size, 0)), 0).label("bytes"))
               .group_by(media.c.user_id).subquery())
    stmt = (select(users.c.id, users.c.email, users.c.created_at, users.c.last_login_at, users.c.is_blocked, users.c.locked_until,
                   users.c.two_step_enabled, profiles.c.username, profiles.c.display_name,
                   func.coalesce(storage.c.files, 0).label("files"), func.coalesce(storage.c.bytes, 0).label("bytes"))
            .select_from(users.join(profiles, profiles.c.id == users.c.id).outerjoin(storage, storage.c.uid == users.c.id)))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(profiles.c.username.ilike(like), users.c.email.ilike(like), profiles.c.display_name.ilike(like)))
    if status == "blocked":
        stmt = stmt.where(users.c.is_blocked.is_(True))
    elif status == "locked":
        stmt = stmt.where(users.c.locked_until > now)
    elif status == "active":
        stmt = stmt.where(users.c.is_blocked.is_(False))
    order = {"storage": func.coalesce(storage.c.bytes, 0).desc(), "name": profiles.c.username.asc(),
             "login": users.c.last_login_at.desc()}.get(sort, users.c.created_at.desc())
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(stmt.order_by(order).limit(limit)).all()]
    for row in rows:
        row["bytes_h"] = stats_service.human_size(row["bytes"])
        row["locked"] = bool(row["locked_until"] and aware(row["locked_until"]) > now)
        row["percent"] = min(100, round(row["bytes"] * 100 / stats_service.QUOTA_BYTES, 1)) if stats_service.QUOTA_BYTES else 0
    return rows


def user_events(user_id: str, limit: int = 100) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(account_events).where(account_events.c.user_id == user_id)
                                                       .order_by(account_events.c.created_at.desc()).limit(limit)).all()]


def user_logins(user_id: str, limit: int = 50) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(login_history).where(login_history.c.user_id == user_id)
                                                       .order_by(login_history.c.created_at.desc()).limit(limit)).all()]


def user_storage_breakdown(user_id: str) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(select(media.c.media_type, func.count(), func.coalesce(func.sum(media.c.file_size + func.coalesce(media.c.thumb_size, 0)), 0))
                            .where(media.c.user_id == user_id, media.c.is_deleted.is_(False)).group_by(media.c.media_type)).all()
        trash = conn.execute(select(func.count(), func.coalesce(func.sum(media.c.file_size), 0)).where(
            media.c.user_id == user_id, media.c.is_deleted.is_(True))).first()
    used = stats_service.storage_used(user_id)
    return {"types": [{"type": r[0], "count": int(r[1]), "bytes": int(r[2]), "bytes_h": stats_service.human_size(int(r[2]))} for r in rows],
            "trash_count": int(trash[0]), "trash_h": stats_service.human_size(int(trash[1])), "used": used,
            "used_h": stats_service.human_size(used), "quota_h": stats_service.human_size(stats_service.QUOTA_BYTES),
            "percent": min(100, round(used * 100 / stats_service.QUOTA_BYTES, 1)) if stats_service.QUOTA_BYTES else 0}


def blocked_users() -> list[dict]:
    now = utcnow()
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(users.c.id, users.c.email, users.c.is_blocked, users.c.blocked_reason,
                                                               users.c.blocked_at, users.c.locked_until, profiles.c.username)
                                                       .select_from(users.join(profiles, profiles.c.id == users.c.id))
                                                       .where(or_(users.c.is_blocked.is_(True), users.c.locked_until > now))
                                                       .order_by(users.c.blocked_at.desc())).all()]
    for row in rows:
        row["kind"] = "Blocked by admin" if row["is_blocked"] else "Temporary lock (wrong passwords)"
    return rows


def events(event_type: str = "", search: str = "", limit: int = 400) -> list[dict]:
    stmt = select(account_events)
    if event_type:
        stmt = stmt.where(account_events.c.event_type == event_type)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(account_events.c.username.ilike(like), account_events.c.email.ilike(like), account_events.c.details.ilike(like)))
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt.order_by(account_events.c.created_at.desc()).limit(limit)).all()]


def event_types() -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(select(account_events.c.event_type).distinct().order_by(account_events.c.event_type)).all()]


def all_logins(limit: int = 300) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(login_history.c.created_at, login_history.c.ip_address, login_history.c.user_agent,
                                                              login_history.c.success, profiles.c.username, users.c.email)
                                                       .select_from(login_history.join(users, users.c.id == login_history.c.user_id)
                                                                    .join(profiles, profiles.c.id == users.c.id))
                                                       .order_by(login_history.c.created_at.desc()).limit(limit)).all()]


def user_media(user_id: str | None = None, limit: int = 60) -> list[dict]:
    stmt = select(media, profiles.c.username).select_from(media.join(profiles, profiles.c.id == media.c.user_id))
    stmt = stmt.where(media.c.is_deleted.is_(False))
    if user_id:
        stmt = stmt.where(media.c.user_id == user_id)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt.order_by(media.c.created_at.desc()).limit(limit)).all()]


# ------------------------------------------------------------------ announcements
def list_announcements() -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(announcements).order_by(announcements.c.created_at.desc()).limit(30)).all()]


def add_announcement(message: str, level: str) -> None:
    with engine.begin() as conn:
        conn.execute(update(announcements).values(is_active=False))      # one active banner at a time
        conn.execute(insert(announcements).values(id=new_id(), message=message[:400], level=level if level in {"info", "warning", "success"} else "info",
                                                  is_active=True, created_at=utcnow()))


def clear_announcements() -> None:
    with engine.begin() as conn:
        conn.execute(update(announcements).values(is_active=False))


# ----------------------------------------------------------------------- avatars
def avatars() -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(admin_avatars).order_by(admin_avatars.c.position, admin_avatars.c.created_at)).all()]
