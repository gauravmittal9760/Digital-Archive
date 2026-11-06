"""Trash lifecycle: permanent deletion after 30 days, for every user, logged in or not."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import delete, select

from config import TRASH_RETENTION_DAYS
from database import app_state, engine, file_versions, media, utcnow, aware
from services import storage_service
from services.events import log_activity

log = logging.getLogger(__name__)
_purge_lock = threading.Lock()


def purge_media_rows(rows: list[dict]) -> int:
    """Delete storage objects first, then DB rows. A storage failure keeps the row for a retry."""
    removed = 0
    for row in rows:
        try:
            with engine.connect() as conn:
                version_paths = [r[0] for r in conn.execute(select(file_versions.c.storage_path).where(
                    file_versions.c.media_id == row["id"])).all()]
            storage_service.delete_objects([row.get("storage_path"), row.get("thumb_path"), *version_paths])
            with engine.begin() as conn:
                conn.execute(delete(media).where(media.c.id == row["id"]))
            removed += 1
        except Exception as exc:
            log.warning("Could not purge %s: %s", row.get("id"), exc)
    return removed


def purge_expired(batch: int = 200) -> int:
    """Permanently delete everything that has been in the trash longer than the retention period."""
    if not _purge_lock.acquire(blocking=False):
        return 0
    try:
        cutoff = utcnow() - timedelta(days=TRASH_RETENTION_DAYS)
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(media.c.id, media.c.user_id, media.c.storage_path,
                    media.c.thumb_path, media.c.file_name).where(media.c.is_deleted.is_(True), media.c.deleted_at < cutoff)
                    .limit(batch)).all()]
        removed = purge_media_rows(rows)
        for row in rows[:removed]:
            log_activity(row["user_id"], "trash_auto_purged", f"{row['file_name']} was permanently deleted after {TRASH_RETENTION_DAYS} days")
        _remember_run()
        return removed
    finally:
        _purge_lock.release()


def _remember_run() -> None:
    from sqlalchemy import insert, update
    now = utcnow().isoformat()
    with engine.begin() as conn:
        updated = conn.execute(update(app_state).where(app_state.c.key == "last_purge").values(value=now, updated_at=utcnow()))
        if not updated.rowcount:
            conn.execute(insert(app_state).values(key="last_purge", value=now, updated_at=utcnow()))


def pending_purge_count() -> int:
    from sqlalchemy import func
    cutoff = utcnow() - timedelta(days=TRASH_RETENTION_DAYS)
    with engine.connect() as conn:
        return conn.execute(select(func.count()).where(media.c.is_deleted.is_(True), media.c.deleted_at < cutoff)).scalar() or 0


def days_left(deleted_at) -> int:
    deleted_at = aware(deleted_at)
    if not deleted_at:
        return TRASH_RETENTION_DAYS
    remaining = deleted_at + timedelta(days=TRASH_RETENTION_DAYS) - utcnow()
    return max(0, int(remaining.total_seconds() // 86400) + (1 if remaining.total_seconds() % 86400 else 0))


_started = False


def start_background_purger(interval_seconds: int = 3600) -> None:
    """Hourly purge loop in a daemon thread (also triggered by /cron/purge for sleeping hosts)."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(20)
        while True:
            try:
                purge_expired()
            except Exception:
                log.exception("Background purge failed")
            time.sleep(interval_seconds)

    threading.Thread(target=loop, name="trash-purger", daemon=True).start()
