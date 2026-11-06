"""Albums (user-curated collections)."""
from __future__ import annotations

from sqlalchemy import and_, delete, func, insert, select, update

from database import album_media, albums, engine, media, new_id, utcnow


def list_albums(user_id: str) -> list[dict]:
    with engine.connect() as conn:
        counts = dict(conn.execute(select(album_media.c.album_id, func.count()).join(media, media.c.id == album_media.c.media_id)
                                   .where(media.c.is_deleted.is_(False)).group_by(album_media.c.album_id)).all())
        rows = [dict(r._mapping) for r in conn.execute(select(albums).where(albums.c.user_id == user_id)
                                                       .order_by(albums.c.created_at.desc())).all()]
        covers = {}
        for row in rows:
            cover_id = row.get("cover_media_id")
            if not cover_id:
                cover_id = conn.execute(select(album_media.c.media_id).join(media, media.c.id == album_media.c.media_id).where(
                    album_media.c.album_id == row["id"], media.c.is_deleted.is_(False), media.c.media_type == "photo")
                    .order_by(album_media.c.created_at).limit(1)).scalar()
            if cover_id:
                covers[row["id"]] = conn.execute(select(media.c.storage_path, media.c.thumb_path).where(
                    media.c.id == cover_id, media.c.is_deleted.is_(False))).mappings().first()
    for row in rows:
        row["count"] = counts.get(row["id"], 0)
        row["cover"] = dict(covers[row["id"]]) if covers.get(row["id"]) else None
    return rows


def get_album(album_id: str, user_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(select(albums).where(albums.c.id == album_id, albums.c.user_id == user_id)).mappings().first()
    return dict(row) if row else None


def create_album(user_id: str, name: str, description: str | None) -> str:
    album_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(albums).values(id=album_id, user_id=user_id, name=name, description=description or None,
                                           created_at=utcnow(), updated_at=utcnow()))
    return album_id


def update_album(album_id: str, user_id: str, name: str, description: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(update(albums).where(albums.c.id == album_id, albums.c.user_id == user_id).values(
            name=name, description=description or None, updated_at=utcnow()))


def delete_album(album_id: str, user_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(delete(albums).where(albums.c.id == album_id, albums.c.user_id == user_id))


def album_media_items(album_id: str, user_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(media).join(album_media, album_media.c.media_id == media.c.id).where(
            album_media.c.album_id == album_id, media.c.user_id == user_id, media.c.is_deleted.is_(False))
            .order_by(album_media.c.created_at.desc())).all()]


def available_media(album_id: str, user_id: str) -> list[dict]:
    with engine.connect() as conn:
        inside = select(album_media.c.media_id).where(album_media.c.album_id == album_id)
        return [dict(r._mapping) for r in conn.execute(select(media).where(
            media.c.user_id == user_id, media.c.is_deleted.is_(False), media.c.id.not_in(inside))
            .order_by(media.c.created_at.desc()).limit(400)).all()]


def add_media(album_id: str, user_id: str, media_ids: list[str]) -> int:
    added = 0
    with engine.begin() as conn:
        if not conn.execute(select(albums.c.id).where(albums.c.id == album_id, albums.c.user_id == user_id)).first():
            return 0
        owned = {r[0] for r in conn.execute(select(media.c.id).where(media.c.user_id == user_id, media.c.id.in_(media_ids))).all()}
        existing = {r[0] for r in conn.execute(select(album_media.c.media_id).where(album_media.c.album_id == album_id)).all()}
        for media_id in media_ids:
            if media_id in owned and media_id not in existing:
                conn.execute(insert(album_media).values(album_id=album_id, media_id=media_id, created_at=utcnow()))
                added += 1
    return added


def remove_media(album_id: str, user_id: str, media_id: str) -> None:
    with engine.begin() as conn:
        if conn.execute(select(albums.c.id).where(albums.c.id == album_id, albums.c.user_id == user_id)).first():
            conn.execute(delete(album_media).where(album_media.c.album_id == album_id, album_media.c.media_id == media_id))
            conn.execute(update(albums).where(albums.c.id == album_id, albums.c.cover_media_id == media_id).values(cover_media_id=None))


def set_cover(album_id: str, user_id: str, media_id: str) -> None:
    with engine.begin() as conn:
        ok = conn.execute(select(album_media.c.media_id).join(albums, albums.c.id == album_media.c.album_id).where(
            album_media.c.album_id == album_id, album_media.c.media_id == media_id, albums.c.user_id == user_id)).first()
        if ok:
            conn.execute(update(albums).where(albums.c.id == album_id).values(cover_media_id=media_id, updated_at=utcnow()))
