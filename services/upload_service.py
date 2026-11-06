"""The upload pipeline: validate -> de-duplicate -> compress -> store -> classify -> AI queue."""
from __future__ import annotations

import os
import uuid

from sqlalchemy import insert, select

from database import engine, media, new_id, utcnow
from services import ai_service, classifier, media_service, stats_service, storage_service
from services.events import log_activity


class UploadResult(dict):
    pass


def _find_exact_duplicate(user_id: str, orig_checksum: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(select(media.c.id, media.c.file_name).where(
            media.c.user_id == user_id, media.c.orig_checksum == orig_checksum, media.c.is_deleted.is_(False)).limit(1)).mappings().first()
    return dict(row) if row else None


def _near_duplicate(user_id: str, phash: str) -> str | None:
    with engine.connect() as conn:
        rows = conn.execute(select(media.c.file_name, media.c.phash).where(
            media.c.user_id == user_id, media.c.is_deleted.is_(False), media.c.phash.is_not(None))).all()
    for name, other in rows:
        if other and media_service.hamming(phash, other) <= 4:
            return name
    return None


def ingest(user: dict, storage, tags: str = "", description: str = "", chosen_category: str = "auto",
           used_bytes: int | None = None) -> UploadResult:
    """Store one uploaded file. Returns {"status": ok|duplicate|error, "message", ...}."""
    user_id = user["id"]
    filename = os.path.basename((storage.filename or "upload").replace("\\", "/"))[:255] or "upload"
    try:
        data = storage.read()
        mimetype = media_service.resolve_mimetype(storage.mimetype, filename)
        media_type = media_service.get_media_type(mimetype)
        if not data:
            return UploadResult(status="error", message=f"{filename}: the file is empty.")
        if not media_type:
            return UploadResult(status="error", message=f"{filename}: this file type is not supported.")
        if not media_service.validate_file_size(data, media_type):
            return UploadResult(status="error", message=f"{filename}: the file is too large for its type.")
        if not media_service.validate_file_content(data, media_type, mimetype):
            return UploadResult(status="error", message=f"{filename}: the content does not match a valid {media_type}.")

        used = stats_service.storage_used(user_id) if used_bytes is None else used_bytes
        if used + len(data) * 0.9 > stats_service.QUOTA_BYTES:
            return UploadResult(status="error", message=f"{filename}: your {stats_service.human_size(stats_service.QUOTA_BYTES)} storage is full.")

        orig_checksum = media_service.sha256_hex(data)
        duplicate = _find_exact_duplicate(user_id, orig_checksum)
        duplicate_of = duplicate["file_name"] if duplicate else None      # still saved, but flagged for the user

        unique = uuid.uuid4().hex
        thumb_bytes = ai_jpeg = None
        features: dict = {}
        phash = captured_at = duration = None
        width = height = None
        compress_in_cloud = False
        extracted_text = None

        if media_type == "photo":
            processed = media_service.process_image(data, classify=classifier.extract_features)
            stored, content_type, ext = processed["bytes"], processed["mime"], processed["ext"]
            width, height = processed["width"], processed["height"]
            thumb_bytes, ai_jpeg = processed["thumb_bytes"], processed["ai_jpeg"]
            features, phash, captured_at = processed["features"], processed["phash"], processed["captured_at"]
            near = _near_duplicate(user_id, phash)
        elif media_type == "video":
            processed = media_service.process_video(data, filename, mimetype)
            stored, content_type, ext = processed["bytes"], processed["mime"], processed["ext"]
            width, height, duration, thumb_bytes = processed["width"], processed["height"], processed["duration"], processed["thumb_bytes"]
            compress_in_cloud = not processed["compressed_locally"] and not media_service.ffmpeg_path()
            near = None
            if thumb_bytes:
                import io
                from PIL import Image
                with Image.open(io.BytesIO(thumb_bytes)) as frame:
                    buffer = io.BytesIO()
                    frame.convert("RGB").save(buffer, format="JPEG", quality=80)
                    ai_jpeg = buffer.getvalue()
        else:
            stored = media_service.compress_pdf(data) if mimetype == "application/pdf" else data
            content_type, ext, near = mimetype, None, None
            extracted_text = media_service.extract_document_text(data, mimetype)

        path = media_service.generate_storage_path(user_id, media_type, filename, ext and (ext if ext.startswith(".") or media_type == "photo" else "." + ext), unique)
        result = storage_service.upload_bytes(path, stored, content_type, compress_video=compress_in_cloud)
        stored_size = int(result.get("bytes") or len(stored))

        thumb_path, thumb_size = None, 0
        if thumb_bytes:
            thumb_path = media_service.thumb_storage_path(user_id, unique)
            try:
                storage_service.upload_bytes(thumb_path, thumb_bytes, "image/webp")
                thumb_size = len(thumb_bytes)
            except Exception:
                thumb_path = None

        row = {"id": new_id(), "user_id": user_id, "file_name": filename, "storage_path": path, "thumb_path": thumb_path,
               "media_type": media_type, "mime_type": content_type, "file_size": stored_size, "thumb_size": thumb_size,
               "original_size": len(data), "width": width, "height": height, "duration_seconds": duration,
               "captured_at": captured_at, "description": (description or "").strip()[:1000] or None,
               "tags": (tags or "").strip()[:500] or None, "extracted_text": extracted_text,
               "checksum": media_service.sha256_hex(stored), "orig_checksum": orig_checksum, "phash": phash,
               "is_favorite": False, "is_deleted": False, "created_at": utcnow(), "updated_at": utcnow()}

        guess = classifier.classify_local(row, features, ai_service.user_model_for(user_id))
        chosen = (chosen_category or "auto").strip().lower()
        if chosen != "auto" and chosen in classifier.CATEGORY_LABEL:
            row.update(category=chosen, category_locked=True, ai_category=guess["category"], ai_confidence=guess["confidence"],
                       ai_source="you", ai_status="done")
        else:
            row.update(category=guess["category"], category_locked=False, ai_category=guess["category"],
                       ai_confidence=guess["confidence"], ai_source=guess["source"], ai_status="done")
            if ai_jpeg and user.get("ai_enabled", True) and (ai_service.vision_available() or ai_service.ocr_available() or ai_service.local_vision_available()):
                row["ai_status"] = "pending"
        try:
            with engine.begin() as conn:
                conn.execute(insert(media).values(**row))
        except Exception:
            storage_service.delete_objects([path, thumb_path])      # never leave orphans behind
            raise
        if row["ai_status"] == "pending":
            ai_service.submit(row["id"], ai_jpeg)
        message = f"{filename}: saved"
        if duplicate_of:
            near = f"exact copy of {duplicate_of}"
            message += f" — duplicate of “{duplicate_of}” (review it in Duplicate Finder)"
        if len(data) > stored_size + thumb_size and len(data) > 50 * 1024:
            message += f" (compressed {stats_service.human_size(len(data))} → {stats_service.human_size(stored_size)})"
        return UploadResult(status="ok", message=message, media_id=row["id"], category=row["category"],
                            near_duplicate=near, saved=max(0, len(data) - stored_size), stored=stored_size + thumb_size)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("Upload failed for %s", filename)
        return UploadResult(status="error", message=f"{filename}: could not be saved ({type(exc).__name__}).")
