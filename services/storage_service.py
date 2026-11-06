"""Private Cloudinary storage (authenticated assets) with a local-disk fallback for development.

Delivery strategy
* photos / videos -> signed Cloudinary CDN URLs (fast, no API rate-limit usage)
* documents (PDF, DOCX...) -> streamed through the app after an ownership check, because
  Cloudinary free accounts block direct PDF/ZIP delivery by default.
"""
from __future__ import annotations

import io
import logging
import os
import time
import urllib.request
from pathlib import Path

from config import (APP_ENV, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET, CLOUDINARY_CLOUD_NAME,
                    IS_PRODUCTION)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_UPLOAD_DIR = BASE_DIR / "instance" / "uploads"

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
_VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}
_configured = False


def cloudinary_ready() -> bool:
    return bool(CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET)


def _cloudinary():
    global _configured
    import cloudinary
    if not _configured:
        cloudinary.config(cloud_name=CLOUDINARY_CLOUD_NAME, api_key=CLOUDINARY_API_KEY,
                          api_secret=CLOUDINARY_API_SECRET, secure=True)
        _configured = True
    return cloudinary


def storage_mode() -> str:
    if cloudinary_ready():
        return "cloudinary"
    return "unconfigured" if IS_PRODUCTION else "local-development"


def asset_info(storage_path: str) -> tuple[str, str, str | None]:
    """Return (resource_type, public_id, format) for a stored key."""
    key = str(storage_path or "").replace("\\", "/").lstrip("/")
    suffix = Path(key).suffix.lower()
    stem = os.path.splitext(key)[0]
    if "/videos/" in f"/{key}" or suffix in _VIDEO_EXT:
        return "video", stem, suffix.lstrip(".") or "mp4"
    if any(part in f"/{key}" for part in ("/photos/", "/thumbs/", "/avatars/", "/avatar.")) or suffix in _IMAGE_EXT:
        return "image", stem, suffix.lstrip(".") or "webp"
    return "raw", key, None


def _local_path(storage_path: str) -> Path:
    clean = str(storage_path or "").replace("\\", "/").lstrip("/")
    root = LOCAL_UPLOAD_DIR.resolve()
    path = (LOCAL_UPLOAD_DIR / clean).resolve()
    if path == root or root not in path.parents:
        raise ValueError("Invalid storage path")
    return path


# ------------------------------------------------------------------------- upload
VIDEO_INCOMING = [{"width": 1280, "height": 720, "crop": "limit"}, {"quality": "auto"}]


def upload_bytes(storage_path: str, data: bytes, content_type: str, overwrite: bool = False,
                 compress_video: bool = False) -> dict:
    """Store bytes under `storage_path`. Returns {"bytes": stored_size, ...}."""
    if not data:
        raise ValueError("Cannot upload an empty file.")
    if cloudinary_ready():
        _cloudinary()
        from cloudinary import uploader
        resource_type, public_id, _fmt = asset_info(storage_path)
        options = dict(public_id=public_id, resource_type=resource_type, type="authenticated",
                       overwrite=overwrite, invalidate=bool(overwrite))
        stream = io.BytesIO(data)
        stream.name = os.path.basename(storage_path)

        def _send(opts):
            if resource_type == "video" and len(data) > 18 * 1024 * 1024:
                return uploader.upload_large(stream, chunk_size=6_000_000, **opts)
            stream.seek(0)
            return uploader.upload(stream, **opts)

        if resource_type == "video" and compress_video:
            try:
                result = _send({**options, "transformation": VIDEO_INCOMING})
                return {"bytes": int(result.get("bytes") or len(data)), "compressed_by_cloudinary": True}
            except Exception as exc:            # fall back to storing the original video
                log.warning("Cloudinary video compression failed, storing original: %s", exc)
                stream.seek(0)
        result = _send(options)
        return {"bytes": int(result.get("bytes") or len(data))}
    if IS_PRODUCTION:
        raise RuntimeError("Cloudinary is not configured (CLOUDINARY_CLOUD_NAME / API_KEY / API_SECRET).")
    path = _local_path(storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError("File already exists")
    path.write_bytes(data)
    return {"bytes": len(data), "local": True}


# ----------------------------------------------------------------------- delivery
def delivery_url(storage_path: str | None) -> str | None:
    """Signed CDN URL for photos/videos; None for documents (use the proxy route)."""
    if not storage_path:
        return None
    if cloudinary_ready():
        resource_type, public_id, fmt = asset_info(storage_path)
        if resource_type == "raw":
            return None
        _cloudinary()
        from cloudinary.utils import cloudinary_url
        try:
            return cloudinary_url(public_id, resource_type=resource_type, type="authenticated",
                                  sign_url=True, secure=True, format=fmt)[0]
        except Exception as exc:                                       # pragma: no cover
            log.warning("Could not sign URL: %s", exc)
            return None
    if IS_PRODUCTION:
        return None
    from flask import url_for
    return url_for("media.local_file", storage_path=storage_path)


def _download_url(storage_path: str, ttl: int = 600) -> str:
    _cloudinary()
    from cloudinary.utils import private_download_url
    resource_type, public_id, fmt = asset_info(storage_path)
    return private_download_url(public_id, fmt, resource_type=resource_type, type="authenticated",
                                expires_at=int(time.time()) + ttl, attachment=False)


def open_stream(storage_path: str):
    """Return (file_like, content_length|None) for streaming a stored object."""
    if cloudinary_ready():
        response = urllib.request.urlopen(_download_url(storage_path), timeout=30)
        length = response.headers.get("Content-Length")
        return response, int(length) if length and length.isdigit() else None
    path = _local_path(storage_path)
    return open(path, "rb"), path.stat().st_size


def read_bytes(storage_path: str, max_bytes: int = 120 * 1024 * 1024) -> bytes:
    stream, _ = open_stream(storage_path)
    try:
        data = stream.read(max_bytes + 1)
    finally:
        stream.close()
    if len(data) > max_bytes:
        raise ValueError("Object is larger than the allowed limit.")
    return data


# ------------------------------------------------------------------------- delete
def delete_object(storage_path: str | None) -> None:
    """Delete one stored object. Missing objects are not an error."""
    if not storage_path:
        return
    if cloudinary_ready():
        _cloudinary()
        from cloudinary import uploader
        resource_type, public_id, _fmt = asset_info(storage_path)
        result = uploader.destroy(public_id, resource_type=resource_type, type="authenticated", invalidate=True)
        if result.get("result") not in {"ok", "not found"}:
            raise RuntimeError(f"Cloudinary could not delete {storage_path}: {result}")
        return
    path = _local_path(storage_path)
    if path.is_file():
        path.unlink()


def delete_objects(paths) -> int:
    """Delete many objects; raises on the first hard failure. Returns number processed."""
    count = 0
    for path in dict.fromkeys(p for p in (paths or []) if p):
        delete_object(path)
        count += 1
    return count


# --------------------------------------------------------------------- admin stats
def cloudinary_usage() -> dict | None:
    """Live account usage straight from Cloudinary (storage, bandwidth, credits)."""
    if not cloudinary_ready():
        return None
    _cloudinary()
    from cloudinary import api
    try:
        return api.usage()
    except Exception as exc:
        log.warning("Cloudinary usage lookup failed: %s", exc)
        return {"error": str(exc)[:200]}


def cloudinary_prefix_bytes(prefix: str, max_pages: int = 6) -> dict:
    """Sum the real stored bytes below `prefix` across image/video/raw resources."""
    if not cloudinary_ready():
        return {"available": False}
    _cloudinary()
    from cloudinary import api
    total = count = 0
    truncated = False
    try:
        for resource_type in ("image", "video", "raw"):
            cursor = None
            for _ in range(max_pages):
                kwargs = dict(type="authenticated", resource_type=resource_type, prefix=prefix, max_results=500)
                if cursor:
                    kwargs["next_cursor"] = cursor
                page = api.resources(**kwargs)
                for item in page.get("resources", []):
                    total += int(item.get("bytes") or 0)
                    count += 1
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            else:
                truncated = True
    except Exception as exc:
        return {"available": False, "error": str(exc)[:200]}
    return {"available": True, "bytes": total, "count": count, "truncated": truncated}
