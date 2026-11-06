"""Upload validation and compression for photos, videos and documents.

Compression strategy (all lossless-to-the-eye, dependency-light):
* photos     -> auto-rotate, strip metadata, WebP, long edge <= 2048 px (<= 2400 px for scans of
               documents so text stays readable); a re-encode that would be *bigger* than the
               original is skipped.
* animations -> animated WebP with a bounded frame count/size.
* thumbnails -> 480 px WebP so galleries load instantly.
* videos     -> local ffmpeg (H.264 CRF 28, 720p) when ffmpeg exists on the host, otherwise
               Cloudinary's incoming transformation does the compression for us.
* PDFs       -> stream compression with pypdf when it actually saves space.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 64_000_000

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
ALLOWED_DOCUMENT_TYPES = {
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain", "text/csv",
}
EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    ".bmp": "image/bmp", ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".pdf": "application/pdf", ".doc": "application/msword", ".txt": "text/plain", ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel", ".ppt": "application/vnd.ms-powerpoint",
}
MAX_IMAGE_SIZE_MB, MAX_VIDEO_SIZE_MB, MAX_DOCUMENT_SIZE_MB = 25, 100, 25
PHOTO_EDGE, DOCUMENT_EDGE, THUMB_EDGE = 2048, 2400, 480


def resolve_mimetype(declared: str | None, filename: str | None) -> str:
    """Browsers sometimes send application/octet-stream; fall back to the extension."""
    declared = (declared or "").split(";")[0].strip().lower()
    if declared in ALLOWED_IMAGE_TYPES | ALLOWED_VIDEO_TYPES | ALLOWED_DOCUMENT_TYPES:
        return declared
    return EXT_MIME.get(Path(filename or "").suffix.lower(), declared or "application/octet-stream")


def get_media_type(mimetype: str) -> str | None:
    if mimetype in ALLOWED_IMAGE_TYPES:
        return "photo"
    if mimetype in ALLOWED_VIDEO_TYPES:
        return "video"
    if mimetype in ALLOWED_DOCUMENT_TYPES:
        return "document"
    return None


def validate_file_size(file_bytes: bytes, media_type: str) -> bool:
    limits = {"photo": MAX_IMAGE_SIZE_MB, "video": MAX_VIDEO_SIZE_MB, "document": MAX_DOCUMENT_SIZE_MB}
    return media_type in limits and len(file_bytes) / (1024 * 1024) <= limits[media_type]


def validate_file_content(file_bytes: bytes, media_type: str, mimetype: str) -> bool:
    """Check real file signatures instead of trusting the browser's MIME header."""
    if not file_bytes:
        return False
    if media_type == "photo":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(file_bytes)) as image:
                    image.verify()
            return True
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            return False
    if media_type == "video":
        if mimetype == "video/webm":
            return file_bytes.startswith(b"\x1aE\xdf\xa3")
        return len(file_bytes) >= 12 and file_bytes[4:8] == b"ftyp"
    if media_type == "document":
        if mimetype == "application/pdf":
            return file_bytes[:1024].find(b"%PDF-") != -1
        if mimetype.startswith("application/vnd.openxmlformats-officedocument"):
            return file_bytes.startswith(b"PK\x03\x04")
        if mimetype in {"application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint"}:
            return file_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        if mimetype in {"text/plain", "text/csv"}:
            head = file_bytes[:4096]
            if b"\x00" in head:
                return False
            try:
                head.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    head.decode("cp1252")
                except UnicodeDecodeError:
                    return False
            return True
    return False


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate_storage_path(user_id: str, media_type: str, filename: str | None = None, ext: str | None = None,
                          unique: str | None = None) -> str:
    """Per-user keys. Documents keep their extension (Cloudinary raw assets need it)."""
    unique = unique or uuid.uuid4().hex
    if media_type == "photo":
        return f"{user_id}/photos/{unique}.{(ext or 'webp').lstrip('.')}"
    if media_type == "video":
        ext = (ext or Path(filename or "").suffix.lower() or ".mp4")
        if ext not in {".mp4", ".webm", ".mov"}:
            ext = ".mp4"
        return f"{user_id}/videos/{unique}{ext}"
    ext = Path(filename or "").suffix.lower()[:12]
    if not ext or not ext[1:].isalnum():
        ext = ".bin"
    return f"{user_id}/documents/{unique}{ext}"


def thumb_storage_path(user_id: str, unique: str) -> str:
    return f"{user_id}/thumbs/{unique}.webp"


# ------------------------------------------------------------------------- images
def _exif_info(image: Image.Image) -> dict:
    info = {"captured_at": None, "camera": False, "orientation": 1}
    try:
        exif = image.getexif()
        if not exif:
            return info
        info["orientation"] = int(exif.get(274, 1) or 1)
        info["camera"] = bool(exif.get(271) or exif.get(272))
        raw = None
        try:
            raw = exif.get_ifd(0x8769).get(36867)
        except Exception:
            raw = None
        raw = raw or exif.get(306)
        if raw:
            match = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(raw))
            if match and match.group(1) != "0000":
                info["captured_at"] = datetime(*map(int, match.groups()), tzinfo=timezone.utc)
    except Exception:
        pass
    return info


def dhash(image: Image.Image) -> str:
    """64-bit difference hash (hex). Similar pictures differ in only a few bits."""
    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[row * 9 + col] > pixels[row * 9 + col + 1] else 0)
    return f"{bits:016x}"


def hamming(hex_a: str, hex_b: str) -> int:
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")


def _save_webp(image: Image.Image, quality: int, method: int = 4) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=quality, method=method)
    return buffer.getvalue()


def process_image(file_bytes: bytes, classify=None) -> dict:
    """Decode once; return compressed bytes, thumbnail, hash, EXIF facts and (optional) features.

    `classify` is an optional callable(PIL small image, exif_info, size) -> dict of image features.
    """
    original_size = len(file_bytes)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        source = Image.open(io.BytesIO(file_bytes))
        with source:
            original_format = source.format
            real_width, real_height = source.size
            frame_count = int(getattr(source, "n_frames", 1) or 1)
            exif = _exif_info(source)

            if frame_count > 1:
                return _process_animation(source, file_bytes, frame_count, exif)

            if original_format == "JPEG":
                source.draft("RGB", (DOCUMENT_EDGE, DOCUMENT_EDGE))     # decode at reduced size: faster, less RAM
            image = ImageOps.exif_transpose(source)
            has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
            image = image.convert("RGBA" if has_alpha else "RGB")
            if exif["orientation"] in (5, 6, 7, 8):
                real_width, real_height = real_height, real_width

    small = image.copy()
    small.thumbnail((256, 256), Image.Resampling.BILINEAR)
    features = classify(small, exif, (real_width, real_height)) if classify else {}
    kind = features.get("kind", "photo")

    edge = DOCUMENT_EDGE if kind == "document_scan" else PHOTO_EDGE
    working = image.copy()
    working.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    quality = 86 if kind in ("document_scan", "screenshot") else 78
    webp = _save_webp(working, quality)

    out_bytes, mime, ext, out_w, out_h = webp, "image/webp", "webp", working.width, working.height
    untouched = (max(real_width, real_height) <= edge and exif["orientation"] == 1
                 and original_format in ("JPEG", "WEBP") and len(file_bytes) <= len(webp))
    if untouched:       # already smaller than what we could make: keep the user's bytes
        out_bytes, mime, ext = file_bytes, ("image/jpeg" if original_format == "JPEG" else "image/webp"), \
            ("jpg" if original_format == "JPEG" else "webp")
        out_w, out_h = real_width, real_height

    thumb = image.copy()
    thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    thumb_bytes = _save_webp(thumb, 72)

    ai = image.copy()
    ai.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    ai_buffer = io.BytesIO()
    ai.convert("RGB").save(ai_buffer, format="JPEG", quality=80)

    return {
        "bytes": out_bytes, "mime": mime, "ext": ext, "width": out_w, "height": out_h,
        "thumb_bytes": thumb_bytes, "phash": dhash(image), "captured_at": exif["captured_at"],
        "camera": exif["camera"], "features": features, "ai_jpeg": ai_buffer.getvalue(),
        "original_size": original_size, "frame_count": 1,
    }


def _process_animation(source: Image.Image, file_bytes: bytes, frame_count: int, exif: dict) -> dict:
    """Animated GIF/WebP -> bounded animated WebP; very long animations stay as uploaded."""
    first = source.convert("RGB")
    first.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    thumb_bytes = _save_webp(first, 72)
    ai_buffer = io.BytesIO()
    first.save(ai_buffer, format="JPEG", quality=80)
    width, height = source.size
    result = {"thumb_bytes": thumb_bytes, "phash": dhash(first), "captured_at": exif["captured_at"],
              "camera": False, "features": {"kind": "photo"}, "ai_jpeg": ai_buffer.getvalue(),
              "original_size": len(file_bytes), "frame_count": frame_count}
    if frame_count > 150 or max(width, height) > 4000:
        fmt = (source.format or "GIF").upper()
        result.update(bytes=file_bytes, mime="image/gif" if fmt == "GIF" else "image/webp",
                      ext="gif" if fmt == "GIF" else "webp", width=width, height=height)
        return result
    frames, durations = [], []
    for index in range(frame_count):
        source.seek(index)
        frame = source.convert("RGBA")
        frame.thumbnail((1000, 1000), Image.Resampling.LANCZOS)
        frames.append(frame)
        durations.append(max(20, int(source.info.get("duration", 100) or 100)))
    buffer = io.BytesIO()
    frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:], duration=durations,
                   loop=int(source.info.get("loop", 0) or 0), quality=70, method=3)
    out = buffer.getvalue()
    if len(out) >= len(file_bytes):
        fmt = (source.format or "GIF").upper()
        result.update(bytes=file_bytes, mime="image/gif" if fmt == "GIF" else "image/webp",
                      ext="gif" if fmt == "GIF" else "webp", width=width, height=height)
    else:
        result.update(bytes=out, mime="image/webp", ext="webp", width=frames[0].width, height=frames[0].height)
    return result


# ------------------------------------------------------------------------- videos
def ffmpeg_path() -> str | None:
    found = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
    if found:
        return found
    try:                                              # optional bundled binary: pip install imageio-ffmpeg
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def process_video(file_bytes: bytes, filename: str, mimetype: str, time_budget: int = 110) -> dict:
    """Compress with ffmpeg when available and make a poster thumbnail. Never raises."""
    result = {"bytes": file_bytes, "mime": mimetype, "ext": Path(filename).suffix.lower() or ".mp4",
              "thumb_bytes": None, "duration": None, "width": None, "height": None,
              "original_size": len(file_bytes), "compressed_locally": False}
    binary = ffmpeg_path()
    if not binary:
        return result
    workdir = tempfile.mkdtemp(prefix="da_video_")
    try:
        src = os.path.join(workdir, "in" + (result["ext"] if result["ext"] in {".mp4", ".webm", ".mov"} else ".mp4"))
        with open(src, "wb") as handle:
            handle.write(file_bytes)
        probe = subprocess.run([binary, "-hide_banner", "-i", src], capture_output=True, text=True, timeout=30)
        text = probe.stderr or ""
        duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if duration:
            h, m, s = duration.groups()
            result["duration"] = int(h) * 3600 + int(m) * 60 + float(s)
        size = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
        if size:
            result["width"], result["height"] = int(size.group(1)), int(size.group(2))

        poster = os.path.join(workdir, "poster.jpg")
        seek = "1" if (result["duration"] or 2) > 2 else "0"
        subprocess.run([binary, "-y", "-ss", seek, "-i", src, "-frames:v", "1", "-vf", f"scale={THUMB_EDGE}:-2", poster],
                       capture_output=True, timeout=30)
        if os.path.isfile(poster):
            with Image.open(poster) as frame:
                result["thumb_bytes"] = _save_webp(frame.convert("RGB"), 72)

        if len(file_bytes) > 2 * 1024 * 1024:
            out = os.path.join(workdir, "out.mp4")
            command = [binary, "-y", "-i", src, "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                       "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out]
            try:
                subprocess.run(command, capture_output=True, timeout=time_budget, check=True)
                if os.path.isfile(out) and 0 < os.path.getsize(out) < len(file_bytes) * 0.92:
                    with open(out, "rb") as handle:
                        result.update(bytes=handle.read(), mime="video/mp4", ext=".mp4", compressed_locally=True)
            except (subprocess.SubprocessError, OSError):
                pass            # keep the original when encoding fails or exceeds the time budget
    except Exception:
        pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return result


# ------------------------------------------------------------------------ documents
def compress_pdf(file_bytes: bytes) -> bytes:
    """Try lossless PDF stream compression; return the original if it does not help."""
    if len(file_bytes) > 20 * 1024 * 1024 or len(file_bytes) < 50 * 1024:
        return file_bytes
    try:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(file_bytes), strict=False)
        if reader.is_encrypted:
            return file_bytes
        writer = PdfWriter(clone_from=reader)
        for page in writer.pages:
            page.compress_content_streams()
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        buffer = io.BytesIO()
        writer.write(buffer)
        out = buffer.getvalue()
        return out if 0 < len(out) < len(file_bytes) * 0.95 else file_bytes
    except Exception:
        return file_bytes


def extract_document_text(file_bytes: bytes, mimetype: str, limit: int = 20000) -> str | None:
    """Plain-text extraction for PDFs, Office files and text files (best effort, bounded)."""
    try:
        if mimetype == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes), strict=False)
            parts, total = [], 0
            for page in reader.pages[:25]:
                chunk = page.extract_text() or ""
                parts.append(chunk)
                total += len(chunk)
                if total >= limit:
                    break
            return "\n".join(parts).strip()[:limit] or None
        if mimetype in {"text/plain", "text/csv"}:
            return file_bytes[:limit * 2].decode("utf-8", "ignore").strip()[:limit] or None
        if mimetype.startswith("application/vnd.openxmlformats-officedocument"):
            targets = {"wordprocessingml": [r"word/document\.xml"], "presentationml": [r"ppt/slides/slide\d+\.xml"],
                       "spreadsheetml": [r"xl/sharedStrings\.xml"]}
            patterns = next((p for key, p in targets.items() if key in mimetype), [])
            tag = re.compile(r"<(?:w:t|a:t|t)(?:\s[^>]*)?>([^<]*)</(?:w:t|a:t|t)>")
            parts = []
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
                for name in archive.namelist():
                    if any(re.fullmatch(p, name) for p in patterns):
                        parts.extend(tag.findall(archive.read(name).decode("utf-8", "ignore")))
                        if sum(map(len, parts)) > limit:
                            break
            return " ".join(parts).strip()[:limit] or None
    except Exception:
        return None
    return None
