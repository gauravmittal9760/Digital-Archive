"""Per-user numbers: counts, storage use, quota."""
from __future__ import annotations

from sqlalchemy import func, select

from config import USER_STORAGE_QUOTA_MB
import threading
from contextlib import contextmanager

from sqlalchemy import text

from database import engine, file_versions, media, vault_items

QUOTA_BYTES = USER_STORAGE_QUOTA_MB * 1024 * 1024


def human_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return "0 B"


def used_on(conn, user_id: str) -> int:
    files = conn.execute(select(func.coalesce(func.sum(media.c.file_size), 0) + func.coalesce(func.sum(media.c.thumb_size), 0))
                         .where(media.c.user_id == user_id)).scalar() or 0
    versions = conn.execute(select(func.coalesce(func.sum(file_versions.c.file_size), 0))
                            .where(file_versions.c.user_id == user_id)).scalar() or 0
    vault = conn.execute(select(func.coalesce(func.sum(vault_items.c.file_size), 0)).where(vault_items.c.user_id == user_id)).scalar() or 0
    return int(files) + int(versions) + int(vault)


def storage_used(user_id: str) -> int:
    """Everything the user occupies: files, thumbnails, trash, old versions and the vault."""
    with engine.connect() as conn:
        return used_on(conn, user_id)


class QuotaExceeded(Exception):
    pass


_stripes = [threading.Lock() for _ in range(64)]


@contextmanager
def quota_transaction(user_id: str, extra_bytes: int = 0):
    """Serialise 'check quota, then write' for one user so parallel uploads cannot overshoot the limit.

    Combines a process lock (covers SQLite and a single worker) with a PostgreSQL advisory lock held for the
    transaction (covers several app instances). Raises QuotaExceeded before the caller's write if the
    extra bytes do not fit; the caller performs its INSERT/UPDATE on the yielded connection.
    """
    with _stripes[hash(user_id) % len(_stripes)]:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"quota:{user_id}"})
            if extra_bytes and used_on(conn, user_id) + extra_bytes > QUOTA_BYTES:
                raise QuotaExceeded(f"This would exceed your {human_size(QUOTA_BYTES)} storage quota.")
            yield conn


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


def optimizer_tips(user_id: str, overview: dict | None = None) -> list[dict]:
    """Concrete, safe ways to free space. Each tip: {title, detail, bytes, url_endpoint}."""
    o = overview or storage_overview(user_id)
    tips = []
    if o["trash_bytes"] > 0:
        tips.append({"title": "Empty the trash", "detail": "Deleted files still count until the trash is emptied.", "bytes": o["trash_bytes"], "endpoint": "media.trash"})
    try:
        from services import duplicate_service
        reclaim = sum(g["reclaimable"] for g in duplicate_service.find_duplicate_groups(user_id))
    except Exception:
        reclaim = 0
    if reclaim > 0:
        tips.append({"title": "Remove duplicate copies", "detail": "Exact and look-alike copies you can safely trash.", "bytes": reclaim, "endpoint": "intelligence.duplicates"})
    with engine.connect() as conn:
        versions = int(conn.execute(select(func.coalesce(func.sum(file_versions.c.file_size), 0)).where(file_versions.c.user_id == user_id)).scalar() or 0)
        big = conn.execute(select(func.count(), func.coalesce(func.sum(media.c.file_size), 0)).where(
            media.c.user_id == user_id, media.c.is_deleted.is_(False), media.c.file_size > 25 * 1024 * 1024)).first()
    if versions > 0:
        tips.append({"title": "Old file versions", "detail": "Replaced files keep older versions. Open a file → Versions to delete them.", "bytes": versions, "endpoint": "media.gallery"})
    if big and big[0]:
        tips.append({"title": f"{big[0]} large file(s) over 25 MB", "detail": "Review big videos and documents you no longer need.", "bytes": int(big[1]), "endpoint": "media.gallery"})
    for t in tips:
        t["bytes_h"] = human_size(t["bytes"])
    return sorted(tips, key=lambda t: -t["bytes"])
