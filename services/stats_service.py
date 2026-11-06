"""Per-user numbers: counts, storage use, quota."""
from __future__ import annotations

from sqlalchemy import func, select

from config import USER_STORAGE_QUOTA_MB
from database import engine, file_versions, media

QUOTA_BYTES = USER_STORAGE_QUOTA_MB * 1024 * 1024


def human_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return "0 B"


def storage_used(user_id: str) -> int:
    """Everything the user occupies: files, thumbnails, trash and old versions."""
    with engine.connect() as conn:
        files = conn.execute(select(func.coalesce(func.sum(media.c.file_size), 0) + func.coalesce(func.sum(media.c.thumb_size), 0))
                             .where(media.c.user_id == user_id)).scalar() or 0
        versions = conn.execute(select(func.coalesce(func.sum(file_versions.c.file_size), 0))
                                .where(file_versions.c.user_id == user_id)).scalar() or 0
    return int(files) + int(versions)


def library_counts(user_id: str) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(select(media.c.media_type, media.c.is_favorite, func.count()).where(
            media.c.user_id == user_id, media.c.is_deleted.is_(False)).group_by(media.c.media_type, media.c.is_favorite)).all()
        trash = conn.execute(select(func.count()).where(media.c.user_id == user_id, media.c.is_deleted.is_(True))).scalar() or 0
    counts = {"total": 0, "photos": 0, "videos": 0, "documents": 0, "favorites": 0, "trash": int(trash)}
    for media_type, favorite, n in rows:
        counts["total"] += n
        counts[{"photo": "photos", "video": "videos", "document": "documents"}.get(media_type, "documents")] += n
        if favorite:
            counts["favorites"] += n
    return counts


def storage_overview(user_id: str) -> dict:
    used = storage_used(user_id)
    with engine.connect() as conn:
        by_type = {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in conn.execute(
            select(media.c.media_type, func.sum(media.c.file_size + func.coalesce(media.c.thumb_size, 0)), func.count())
            .where(media.c.user_id == user_id, media.c.is_deleted.is_(False)).group_by(media.c.media_type)).all()}
        trash_bytes = conn.execute(select(func.coalesce(func.sum(media.c.file_size + func.coalesce(media.c.thumb_size, 0)), 0))
                                   .where(media.c.user_id == user_id, media.c.is_deleted.is_(True))).scalar() or 0
        saved = conn.execute(select(func.coalesce(func.sum(media.c.original_size - media.c.file_size), 0)).where(
            media.c.user_id == user_id, media.c.original_size.is_not(None), media.c.original_size > media.c.file_size)).scalar() or 0
        largest = [dict(r._mapping) for r in conn.execute(
            select(media.c.id, media.c.file_name, media.c.file_size, media.c.media_type).where(
                media.c.user_id == user_id, media.c.is_deleted.is_(False)).order_by(media.c.file_size.desc()).limit(6)).all()]
    return {"used": used, "quota": QUOTA_BYTES, "percent": min(100.0, round(used * 100.0 / QUOTA_BYTES, 1)) if QUOTA_BYTES else 0,
            "by_type": by_type, "trash_bytes": int(trash_bytes), "saved": int(saved), "largest": largest,
            "used_h": human_size(used), "quota_h": human_size(QUOTA_BYTES)}
