"""Background AI enrichment for uploads (free options only).

* **Gemini vision (optional, free tier)** - set GEMINI_API_KEY (from aistudio.google.com).
  One call returns category, caption, labels and the visible text of the picture.
  Pictures are down-scaled to <=1024 px before they leave the server and users can switch AI
  analysis off in Settings -> Privacy.
* **Tesseract OCR (optional, local)** - used when ENABLE_OCR=true and the `tesseract`
  executable exists on the machine.

Both are optional: without them the local models in classifier.py still categorise uploads.
The queue uses one worker thread so a 512 MB host is never overloaded and the free API rate
limit (~15 requests/minute) is respected.
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select, update

from config import ENABLE_OCR, GEMINI_API_KEY, GEMINI_MODEL
from database import engine, media, users, utcnow, aware
from services import classifier, vision_local

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-worker")
_gate = threading.Lock()
_last_call = 0.0
MIN_INTERVAL = 4.5          # seconds between Gemini calls (free tier friendly)


def vision_available() -> bool:
    return bool(GEMINI_API_KEY)


def ocr_available() -> bool:
    return bool(ENABLE_OCR and shutil.which("tesseract"))


def local_vision_available() -> bool:
    return vision_local.available()


def ai_mode_label() -> str:
    if vision_available():
        return "Gemini vision + local models"
    if local_vision_available():
        return "Local vision (MobileNet) + local models"
    if ocr_available():
        return "Local OCR + local models"
    return "Local models"


# ----------------------------------------------------------------------- Gemini
def _prompt() -> str:
    lines = "\n".join(f'- "{cid}": {desc}' for cid, _label, _color, desc in classifier.CATEGORIES)
    return (
        "You organise a person's private photo/document archive (India). Look at the image and answer in JSON only.\n"
        "Choose exactly ONE category id from this list:\n" + lines + "\n"
        "Rules: marksheets, report cards, school/college certificates -> education. Lab reports, prescriptions, scans "
        "-> medical. Aadhaar/PAN/passport/licence -> identity. Bills, receipts, statements -> finance. People-centred "
        "photos -> family. Use \"other\" only if nothing fits. Paper/document pictures never go to nature, food or pets.\n"
        'JSON keys: {"category": id, "confidence": number 0-1, "caption": max 18 words, "labels": up to 8 short English '
        'tags (include the document type like "10th marksheet" or festival name when visible), "text": the readable '
        "text in the picture (max 700 characters, empty if none)}"
    )


def _post_gemini(jpeg: bytes, with_thinking_off: bool = True) -> dict:
    global _last_call
    with _gate:                                               # serialise + respect the free-tier rate limit
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        generation = {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 2048}
        if with_thinking_off:
            generation["thinkingConfig"] = {"thinkingBudget": 0}
        body = {"contents": [{"parts": [{"text": _prompt()},
                                        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]}],
                "generationConfig": generation}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.loads(response.read().decode())
        finally:
            _last_call = time.time()
    parts = (payload.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    return json.loads(text)


def analyze_with_gemini(jpeg: bytes) -> dict | None:
    """Return {"category","confidence","caption","labels","text"} or None on failure."""
    if not vision_available():
        return None
    for attempt in range(3):
        try:
            data = _post_gemini(jpeg, with_thinking_off=(attempt == 0))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            if exc.code == 400 and attempt == 0:      # model without a switchable thinking budget
                continue
            log.warning("Gemini HTTP %s", exc.code)
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            log.warning("Gemini call failed: %s", exc)
            return None
    else:
        return None
    if not isinstance(data, dict):
        return None
    category = classifier.normalize_category(str(data.get("category", "other")))
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    labels = [str(x).strip()[:40] for x in (data.get("labels") or []) if str(x).strip()][:8]
    return {"category": category, "confidence": confidence, "caption": str(data.get("caption") or "")[:300],
            "labels": labels, "text": str(data.get("text") or "")[:1500]}


# --------------------------------------------------------------------------- OCR
_ocr_langs: str | None = None


def ocr_image(jpeg: bytes) -> str | None:
    global _ocr_langs
    if not ocr_available():
        return None
    try:
        if _ocr_langs is None:
            listing = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=15).stdout
            _ocr_langs = "eng+hin" if "hin" in listing.split() else "eng"
        result = subprocess.run(["tesseract", "stdin", "stdout", "-l", _ocr_langs, "--psm", "3"],
                                input=jpeg, capture_output=True, timeout=45)
        text = result.stdout.decode("utf-8", "ignore").strip()
        return text[:3000] or None
    except Exception as exc:
        log.warning("OCR failed: %s", exc)
        return None


# -------------------------------------------------------------------- the pipeline
def _user_labelled(user_id: str) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(select(media).where(media.c.user_id == user_id, media.c.category_locked.is_(True),
                                                media.c.is_deleted.is_(False)).limit(1500)).mappings().all()
    return [(classifier.normalize_category(r["category"]), classifier.text_blob(dict(r))) for r in rows]


def user_model_for(user_id: str):
    return classifier.user_model(user_id, _user_labelled(user_id))


def run_analysis(media_id: str, jpeg: bytes | None) -> None:
    """Enrich one media row. Safe to call from a worker thread. Never raises."""
    try:
        with engine.connect() as conn:
            row = conn.execute(select(media).where(media.c.id == media_id)).mappings().first()
            if not row or row["is_deleted"]:
                return
            ai_enabled = conn.execute(select(users.c.ai_enabled).where(users.c.id == row["user_id"])).scalar()
        item = dict(row)
        gemini = analyze_with_gemini(jpeg) if (jpeg and ai_enabled) else None
        ocr_text = None if (gemini and gemini["text"]) else (ocr_image(jpeg) if jpeg else None)

        extra_text = (gemini or {}).get("text") or ocr_text or ""
        merged_text = "\n".join(x for x in (item.get("extracted_text"), extra_text) if x)[:6000] or None
        item["extracted_text"] = merged_text
        if gemini:
            item["ai_caption"] = gemini["caption"]
            item["ai_labels"] = ", ".join(gemini["labels"])

        # free offline picture recognition (only when Gemini did not already describe the picture)
        seen = vision_local.analyze(jpeg) if (jpeg and not gemini and ai_enabled is not False) else None
        if seen:
            item["ai_caption"] = seen["caption"]
            item["ai_labels"] = ", ".join(seen["labels"])[:500]

        local = classifier.classify_local(item, None, user_model_for(item["user_id"]))
        category, confidence, source = None, 0.0, "none"
        if gemini and gemini["confidence"] >= 0.45:
            category, confidence, source = gemini["category"], gemini["confidence"], "gemini-vision"
            if local["source"] == "text-model" and local["confidence"] >= 0.92 and gemini["confidence"] < 0.7 \
                    and local["category"] != category:
                category, confidence, source = local["category"], local["confidence"], "text-model"
        elif local["category"] != "other" and local["confidence"] > 0:
            category, confidence, source = local["category"], local["confidence"], local["source"]
        # picture recognition decides for ordinary photos; documents/screenshots keep their text/pixel verdict
        if seen and seen["category"] != "other" and not gemini:
            document_like = {"screenshot", "scans", "education", "medical", "identity", "finance", "work"}
            if (item.get("ai_category") or "other") not in document_like and local["category"] not in document_like - {"scans"} \
                    and not (local["source"] == "text-model" and local["confidence"] >= 0.85):
                category, confidence, source = seen["category"], seen["confidence"], "local-vision"

        gemini_expected = bool(vision_available() and ai_enabled and jpeg)
        values = {"extracted_text": merged_text, "ai_status": "failed" if (gemini_expected and not gemini) else "done",
                  "ai_source": source, "updated_at": utcnow()}
        if gemini:
            values.update(ai_caption=gemini["caption"], ai_labels=values_labels(gemini))
        elif seen:
            values.update(ai_caption=seen["caption"], ai_labels=", ".join(seen["labels"])[:500])
        if category:
            values.update(ai_category=category, ai_confidence=confidence)
            if not item["category_locked"]:
                values["category"] = category
        with engine.begin() as conn:
            conn.execute(update(media).where(media.c.id == media_id).values(**values))
    except Exception:
        log.exception("AI analysis failed for %s", media_id)
        try:
            with engine.begin() as conn:
                conn.execute(update(media).where(media.c.id == media_id).values(ai_status="failed"))
        except Exception:
            pass


def values_labels(gemini: dict) -> str:
    return ", ".join(gemini["labels"])[:500]


def submit(media_id: str, jpeg: bytes | None) -> None:
    _executor.submit(run_analysis, media_id, jpeg)


def reanalyze(media_id: str) -> bool:
    """Re-run analysis from the stored file (used by the Re-analyse button and the retry sweep)."""
    from services import storage_service
    with engine.connect() as conn:
        row = conn.execute(select(media.c.storage_path, media.c.thumb_path, media.c.media_type, media.c.mime_type)
                           .where(media.c.id == media_id)).mappings().first()
    if not row:
        return False
    jpeg = None
    if row["media_type"] in {"photo", "video"}:
        try:
            import io
            from PIL import Image
            source_path = row["storage_path"] if row["media_type"] == "photo" else row["thumb_path"]
            if source_path:
                raw = storage_service.read_bytes(source_path, max_bytes=30 * 1024 * 1024)
                with Image.open(io.BytesIO(raw)) as image:
                    image = image.convert("RGB")
                    image.thumbnail((1024, 1024))
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=80)
                    jpeg = buffer.getvalue()
        except Exception as exc:
            log.warning("Could not load %s for re-analysis: %s", media_id, exc)
    with engine.begin() as conn:
        conn.execute(update(media).where(media.c.id == media_id).values(ai_status="pending"))
    submit(media_id, jpeg)
    return True


def sweep_stale(user_id: str, limit: int = 4) -> int:
    """Re-queue items stuck in `pending` (e.g. the server restarted mid-queue)."""
    cutoff = utcnow() - timedelta(minutes=8)
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(select(media.c.id).where(
            media.c.user_id == user_id, media.c.ai_status == "pending", media.c.updated_at < cutoff,
            media.c.is_deleted.is_(False)).limit(limit)).all()]
    for media_id in ids:
        reanalyze(media_id)
    return len(ids)
