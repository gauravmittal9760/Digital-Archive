"""Duplicate detection: byte-identical files (SHA-256) and visually similar photos (dHash)."""
from __future__ import annotations

from sqlalchemy import select

from database import engine, media
from services.media_service import hamming

NEAR_THRESHOLD = 6          # differing bits out of 64


def find_duplicate_groups(user_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(
            media.c.id, media.c.file_name, media.c.media_type, media.c.storage_path, media.c.thumb_path, media.c.file_size,
            media.c.width, media.c.height, media.c.created_at, media.c.orig_checksum, media.c.checksum, media.c.phash
        ).where(media.c.user_id == user_id, media.c.is_deleted.is_(False))).all()]
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    kind = {}
    by_hash: dict[str, list[str]] = {}
    for r in rows:
        key = r["orig_checksum"] or r["checksum"]
        if key:
            by_hash.setdefault(key, []).append(r["id"])
    for ids in by_hash.values():
        for other in ids[1:]:
            union(ids[0], other)
            kind[other] = kind[ids[0]] = "exact"

    photos = [r for r in rows if r["media_type"] == "photo" and r["phash"]]
    for i, a in enumerate(photos):
        for b in photos[i + 1:]:
            if find(a["id"]) != find(b["id"]) and hamming(a["phash"], b["phash"]) <= NEAR_THRESHOLD:
                union(a["id"], b["id"])
                kind.setdefault(a["id"], "similar"); kind.setdefault(b["id"], "similar")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)
    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: ((m["width"] or 0) * (m["height"] or 0), m["file_size"] or 0), reverse=True)
        exact = all((m["orig_checksum"] or m["checksum"]) == (members[0]["orig_checksum"] or members[0]["checksum"]) for m in members)
        result.append({"kind": "exact" if exact else "similar", "items": members,
                       "reclaimable": sum(int(m["file_size"] or 0) for m in members[1:])})
    result.sort(key=lambda g: g["reclaimable"], reverse=True)
    return result
