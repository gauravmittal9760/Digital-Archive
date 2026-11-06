"""Free, offline picture recognition: MobileNetV2 (ImageNet, 1000 classes) via onnxruntime.

* No API key, no paid service. The ~14 MB model file is downloaded once into ``models/`` (or put there by hand).
* Runs on CPU with one thread: ~100 MB RAM, ~50 ms per picture, fine for a 512 MB host.
* Output: the top matches ("monarch butterfly", "lion", "pizza" ...) plus a Digital Archive category.
  Those words are stored as AI labels, so the smart search can find "butterfly" or "janwar".
If onnxruntime or the model is unavailable every function quietly returns nothing and the other
models (text, pixels, optional Gemini) carry on.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import urllib.request

log = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
MODEL_FILE = os.path.join(MODEL_DIR, "mobilenetv2-12.onnx")
LABEL_FILE = os.path.join(MODEL_DIR, "imagenet_classes.txt")
MODEL_URLS = [u for u in (os.getenv("VISION_MODEL_URL", "").strip(),
                          "https://github.com/onnx/models/raw/main/validated/vision/classification/mobilenet/model/mobilenetv2-12.onnx",
                          "https://huggingface.co/onnxmodelzoo/mobilenetv2-12/resolve/main/mobilenetv2-12.onnx") if u]
LABEL_URLS = ["https://raw.githubusercontent.com/pytorch/hub/master/imagenet_classes.txt"]

_lock = threading.Lock()
_state: dict = {"session": None, "labels": None, "failed": False, "input": None}


def _available_runtime() -> bool:
    if os.getenv("DISABLE_LOCAL_VISION") == "1":          # set by the test-suite / by people who want to turn it off
        return False
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def available() -> bool:
    """True when the runtime is installed and the model is present (or can still be downloaded)."""
    return _available_runtime() and not _state["failed"]


def _download(urls: list[str], target: str, min_bytes: int) -> bool:
    os.makedirs(MODEL_DIR, exist_ok=True)
    for url in urls:
        try:
            tmp = target + ".part"
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "DigitalArchive/1.0"}), timeout=60) as resp, open(tmp, "wb") as out:
                while chunk := resp.read(1 << 16):
                    out.write(chunk)
            if os.path.getsize(tmp) >= min_bytes:
                os.replace(tmp, target)
                return True
            os.remove(tmp)
        except Exception as exc:
            log.warning("Vision download failed from %s: %s", url, exc)
    return False


def _load() -> bool:
    if _state["session"] is not None:
        return True
    with _lock:
        if _state["session"] is not None:
            return True
        if _state["failed"] or not _available_runtime():
            return False
        try:
            if not os.path.exists(MODEL_FILE) and not _download(MODEL_URLS, MODEL_FILE, 5_000_000):
                raise RuntimeError("model file missing - place mobilenetv2-12.onnx in the models/ folder")
            if not os.path.exists(LABEL_FILE) and not _download(LABEL_URLS, LABEL_FILE, 5_000):
                raise RuntimeError("label file missing - place imagenet_classes.txt in the models/ folder")
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            session = ort.InferenceSession(MODEL_FILE, sess_options=opts, providers=["CPUExecutionProvider"])
            with open(LABEL_FILE, encoding="utf-8") as fh:
                labels = [line.strip() for line in fh if line.strip()]
            if len(labels) != 1000:
                raise RuntimeError("label file must have 1000 lines")
            _state.update(session=session, labels=labels, input=session.get_inputs()[0].name)
            return True
        except Exception as exc:
            log.warning("Local vision model disabled: %s", exc)
            _state["failed"] = True
            return False


# ImageNet index ranges -> Digital Archive category (the standard class order is fixed).
def category_for_index(i: int) -> str:
    if 0 <= i <= 397:
        return "pets"          # fish, birds, reptiles, insects & butterflies, dogs, cats, big cats, wildlife
    if 924 <= i <= 969:
        return "food"
    if 970 <= i <= 980 or 984 <= i <= 998:
        return "nature"        # alp, cliff, coral reef, lakeside, seashore, valley, volcano, flowers, mushrooms
    if 981 <= i <= 983:
        return "family"        # ballplayer, groom, scuba diver
    if i in {483, 497, 698, 668, 718, 839, 437, 449, 975, 780, 833, 914, 580, 646, 663}:
        return "travel"        # castle, church, palace, mosque, pier, bridge, beacon, boathouse, sailboat, ship ...
    return "other"


def _synonyms(i: int, p: float) -> list[str]:
    """ImageNet names are specific ("monarch", "tabby"); add the everyday word people actually search for."""
    if p < 0.05:
        return []
    ranges = [((321, 326), "butterfly insect"), ((300, 320), "insect bug"), ((151, 268), "dog"), ((281, 285), "cat"),
              ((286, 293), "big cat wild"), ((294, 297), "bear"), ((370, 384), "monkey"), ((7, 24), "bird"), ((80, 100), "bird"),
              ((127, 146), "bird"), ((0, 6), "fish"), ((389, 397), "fish"), ((340, 353), "wild animal"), ((385, 386), "elephant"),
              ((984, 988), "flower plant"), ((980, 980), "volcano mountain"), ((970, 979), "landscape scenery")]
    for (lo, hi), words in ranges:
        if lo <= i <= hi:
            return words.split()
    return []


def _softmax(x):
    import numpy as np
    x = x - x.max()
    e = np.exp(x)
    return e / e.sum()


def analyze(jpeg: bytes | None, top: int = 5) -> dict | None:
    """Return {"labels": [...], "category": str, "confidence": float, "caption": str} or None."""
    if not jpeg or not _load():
        return None
    try:
        import numpy as np
        from PIL import Image
        with Image.open(io.BytesIO(jpeg)) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = 256 / min(w, h)
            im = im.resize((max(224, round(w * scale)), max(224, round(h * scale))), Image.Resampling.BILINEAR)
            w, h = im.size
            left, upper = (w - 224) // 2, (h - 224) // 2
            im = im.crop((left, upper, left + 224, upper + 224))
            arr = np.asarray(im, dtype=np.float32) / 255.0
        arr = (arr - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = arr.transpose(2, 0, 1)[None].astype(np.float32)
        out = _state["session"].run(None, {_state["input"]: tensor})[0].reshape(-1)
        probs = out if (out.min() >= 0 and abs(float(out.sum()) - 1.0) < 1e-3) else _softmax(out)
        order = np.argsort(-probs)[:top]
    except Exception as exc:
        log.warning("Local vision failed: %s", exc)
        return None

    labels, by_cat = [], {}
    for idx in order:
        p = float(probs[idx])
        if p < 0.03:
            continue
        name = _state["labels"][int(idx)].lower()
        labels.append(name)
        cat = category_for_index(int(idx))
        by_cat[cat] = by_cat.get(cat, 0.0) + p
    if not labels:
        return None
    # the most probable known category; "other" never wins over a real one with decent evidence
    real = {c: p for c, p in by_cat.items() if c != "other"}
    category, confidence = ("other", 0.0)
    if real:
        category, confidence = max(real.items(), key=lambda kv: kv[1])
        if confidence < 0.2:
            category, confidence = "other", 0.0
    words = list(labels)
    for idx in order:
        words += _synonyms(int(idx), float(probs[idx]))
    if category == "pets":
        words += ["animal", "wildlife"]
    elif category == "food":
        words += ["food", "dish"]
    elif category == "nature":
        words += ["nature", "scenery"]
    return {"labels": words, "category": category, "confidence": round(min(0.95, confidence), 3),
            "caption": "Looks like: " + ", ".join(labels[:3])}
