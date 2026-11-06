"""Automatic categorisation of uploads (local, dependency-free part).

Two real, data-driven models work together:

1. **Pixel features** (Pillow only): white/ink ratios, saturation, hue mix, flatness and a
   text-line periodicity score decide whether an image is a *scan of a document*, a
   *screenshot* or a normal *photo*.
2. **Multinomial Naive Bayes over text** (filename, notes, tags, OCR / document text, AI
   caption). It is trained at start-up from a vocabulary corpus and then keeps learning
   from every category the user sets or corrects by hand (per-user model).

The optional vision model (see ai_service.py) refines the result for photos with no text.
"""
from __future__ import annotations

import math
import re
from collections import Counter, OrderedDict
from threading import Lock

from PIL import Image, ImageStat

# id, label, colour, one-line meaning (also used in the AI prompt)
CATEGORIES = [
    ("education", "Education & Marksheets", "#6366f1", "marksheets, report cards, certificates of school/college, admit cards, exam results"),
    ("medical", "Medical & Health", "#ef4444", "prescriptions, lab/blood test reports, X-ray/MRI/scan reports, hospital bills, vaccination records"),
    ("identity", "ID & Certificates", "#0ea5e9", "Aadhaar, PAN, passport, driving licence, voter ID, birth/domicile/caste certificates"),
    ("finance", "Bills & Finance", "#10b981", "invoices, receipts, bank statements, bills, insurance, tax papers, tickets"),
    ("work", "Work & Projects", "#8b5cf6", "resume/CV, offer letters, salary slips, presentations, project reports, assignments"),
    ("festival", "Festivals & Celebrations", "#f59e0b", "Diwali, Holi, Eid, Christmas, Navratri, pooja, rangoli, fireworks, decorations"),
    ("travel", "Travel & Places", "#06b6d4", "trips, tourist places, monuments, beaches, mountains, airports, hotels, road trips"),
    ("family", "Family & Friends", "#ec4899", "people, selfies, group photos, weddings, birthdays, parties, kids"),
    ("nature", "Nature & Outdoors", "#22c55e", "landscapes, sunsets, forests, rivers, flowers, sky, gardens"),
    ("food", "Food & Dining", "#f97316", "meals, restaurants, cafes, sweets, cooking, menus"),
    ("pets", "Animals & Pets", "#a855f7", "animals, birds, butterflies, insects, dogs, cats, wildlife, zoo"),
    ("screenshot", "Screenshots", "#64748b", "phone/computer screenshots, chats, app screens, screen recordings"),
    ("scans", "Documents & Scans", "#14b8a6", "scanned or photographed papers whose exact type is unclear"),
    ("other", "Uncategorised", "#94a3b8", "anything else"),
]
CATEGORY_IDS = [c[0] for c in CATEGORIES]
CATEGORY_LABEL = {c[0]: c[1] for c in CATEGORIES}
CATEGORY_COLOR = {c[0]: c[2] for c in CATEGORIES}
CATEGORY_HELP = {c[0]: c[3] for c in CATEGORIES}

# Values stored by older versions of the app map onto the new taxonomy.
LEGACY_CATEGORY = {
    "trip": "travel", "friends": "family", "camera_photo": "other", "downloaded_image": "other",
    "document": "scans", "screen_recording": "screenshot", "downloaded_video": "other",
}


def normalize_category(value: str | None) -> str:
    value = (value or "other").strip().lower()
    value = LEGACY_CATEGORY.get(value, value)
    return value if value in CATEGORY_LABEL else "other"


# ------------------------------------------------------------------------ text model
_TOKEN_RE = re.compile(r"[a-z0-9ऀ-ॿ]+")
_STOP = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "that", "the",
         "this", "to", "with", "your", "my", "img", "image", "photo", "pic", "jpg", "jpeg", "png", "webp", "pdf", "mp4",
         "file", "copy", "new", "final", "whatsapp", "date", "no", "name"}

SEED_CORPUS: dict[str, str] = {
    "education": (
        "marksheet mark sheet marks obtained roll number roll no board examination exam cbse icse ssc hsc 10th 12th tenth twelfth "
        "class x xii secondary senior secondary school certificate result grade cgpa sgpa percentage semester university college "
        "degree diploma transcript admit card hall ticket enrollment subject theory practical total maximum marks passed division "
        "distinction principal registrar student statement of marks report card marksheets matric intermediate btech engineering "
        "convocation scholarship syllabus timetable "
        "अंक पत्र परीक्षा बोर्ड प्रमाण पत्र कक्षा विद्यालय परिणाम"),
    "medical": (
        "prescription rx diagnosis patient doctor dr hospital clinic pathology laboratory lab report blood test haemoglobin hemoglobin "
        "cbc sugar glucose cholesterol thyroid xray x-ray mri ct scan ultrasound sonography ecg discharge summary medicine tablet capsule "
        "dose mg bp blood pressure pulse reference range specimen sample collected radiology opd ipd health insurance vaccine vaccination "
        "dental physician surgery medical report diagnostic pharmacy dawai davai "
        "अस्पताल डॉक्टर दवा जांच रिपोर्ट"),
    "identity": (
        "aadhaar aadhar adhar uidai unique identification pan card income tax department permanent account number passport republic "
        "india driving licence license voter election commission epic ration card domicile caste birth certificate marriage certificate "
        "government authority identity proof address proof signature photo id"
        " आधार पैन पासपोर्ट मतदाता जन्म प्रमाण"),
    "finance": (
        "invoice receipt bill tax gst total amount paid payment bank statement account number ifsc upi transaction debit credit balance "
        "emi loan insurance policy premium electricity bill water bill recharge order subtotal quantity price rs inr cheque passbook "
        "ticket booking pnr fare refund challan fee receipt rent agreement"),
    "work": (
        "resume curriculum vitae cv experience skills projects objective internship offer letter appointment letter salary slip payslip "
        "employee company experience letter project report presentation assignment meeting agenda proposal portfolio linkedin github "
        "deadline quarterly roadmap"),
    "festival": (
        "diwali deepawali holi eid ramzan ramadan christmas navratri durga puja ganesh chaturthi ganpati raksha bandhan rakhi dussehra "
        "dasara lohri baisakhi onam pongal sankranti janmashtami karwa chauth festival celebration diya diyas rangoli lights fireworks "
        "crackers gulal colours mehndi aarti pooja temple new year lamps decoration"
        " दिवाली होली ईद त्योहार पूजा"),
    "travel": (
        "trip travel vacation holiday tour beach hill station mountain manali shimla goa kerala jaipur udaipur ladakh leh kashmir "
        "rishikesh haridwar varanasi mussoorie nainital darjeeling sightseeing airport flight train hotel resort road trip trek "
        "trekking camping lake fort monument tourist visit paris dubai singapore thailand london taj mahal darshan yatra safar ghumna"),
    "family": (
        "family mom dad mother father brother sister cousin uncle aunty grandma grandpa grandparents wedding marriage shaadi engagement "
        "reception birthday party friends friend gang group selfie baby kid kids celebration bhai behen mama papa"),
    "nature": (
        "nature forest river waterfall sunset sunrise sky clouds flowers garden tree trees green field farm landscape rain snow valley "
        "mountain view greenery"),
    "food": (
        "food dinner lunch breakfast restaurant cafe pizza burger biryani thali chai coffee cake dessert sweets mithai recipe cooking "
        "menu paneer dosa samosa momos"),
    "pets": "pet dog puppy cat kitten cow animal bird parrot horse zoo wildlife tiger elephant fish rabbit",
    "screenshot": "screenshot screen shot screen capture screenrecord recording chat instagram status snapshot",
}


def tokenize(text: str) -> list[str]:
    text = re.sub(r"(?<=[a-z])(?=\d)", " ", (text or "").lower().replace("_", " "))   # my10th -> my 10th
    return [t for t in _TOKEN_RE.findall(text) if t not in _STOP and len(t) > 1]


class TextNB:
    """Multinomial Naive Bayes with Laplace smoothing, trained incrementally."""

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self.counts: dict[str, Counter] = {}
        self.totals: dict[str, float] = {}
        self.vocab: set[str] = set()

    def add(self, category: str, text: str, weight: float = 1.0) -> None:
        tokens = tokenize(text)
        if not tokens:
            return
        counter = self.counts.setdefault(category, Counter())
        for token, tf in Counter(tokens).items():
            value = weight * (1 + math.log(tf))
            counter[token] += value
            self.totals[category] = self.totals.get(category, 0.0) + value
            self.vocab.add(token)

    def predict(self, text: str) -> tuple[dict[str, float], int]:
        """Return (probabilities, number of distinct recognised tokens)."""
        tokens = [t for t in dict.fromkeys(tokenize(text)) if t in self.vocab]
        if not tokens or not self.counts:
            return {}, 0
        size = len(self.vocab)
        scores = {}
        for category, counter in self.counts.items():
            total = self.totals.get(category, 0.0)
            scores[category] = sum(math.log((counter.get(t, 0.0) + self.alpha) / (total + self.alpha * size)) for t in tokens)
        scale = 1.0 / math.sqrt(len(tokens))          # calibrate: long texts must not look 100 % certain
        top = max(scores.values())
        exp = {c: math.exp((s - top) * scale) for c, s in scores.items()}
        norm = sum(exp.values())
        return {c: v / norm for c, v in exp.items()}, len(tokens)


def _build_seed_model() -> TextNB:
    model = TextNB()
    for category, text in SEED_CORPUS.items():
        n = max(1, len(set(tokenize(text))))
        model.add(category, text, weight=100.0 / n)        # equal total mass per category
    return model


_SEED = _build_seed_model()
_USER_MODELS: "OrderedDict[str, tuple[int, TextNB]]" = OrderedDict()
_LOCK = Lock()


def user_model(user_id: str, labelled: list[tuple[str, str]]) -> TextNB:
    """Seed model + the user's manually labelled items (cached until the label count changes)."""
    with _LOCK:
        cached = _USER_MODELS.get(user_id)
        if cached and cached[0] == len(labelled):
            _USER_MODELS.move_to_end(user_id)
            return cached[1]
    model = TextNB()
    for category, text in SEED_CORPUS.items():
        model.add(category, text, weight=100.0 / max(1, len(set(tokenize(text)))))
    for category, text in labelled:
        model.add(category, text, weight=4.0)
    with _LOCK:
        _USER_MODELS[user_id] = (len(labelled), model)
        while len(_USER_MODELS) > 24:
            _USER_MODELS.popitem(last=False)
    return model


def forget_user_model(user_id: str) -> None:
    with _LOCK:
        _USER_MODELS.pop(user_id, None)


def text_blob(item: dict) -> str:
    parts = [item.get("file_name"), item.get("description"), item.get("tags"), item.get("extracted_text"),
             item.get("ai_caption"), item.get("ai_labels")]
    return " ".join(str(p).replace("_", " ").replace("-", " ") for p in parts if p)


def predict_from_text(text: str, model: TextNB | None = None) -> tuple[str | None, float, int]:
    probs, evidence = (model or _SEED).predict(text)
    if not probs or evidence < 2 and max(probs.values()) < 0.9:
        return None, 0.0, evidence
    category = max(probs, key=probs.get)
    return category, probs[category], evidence


# -------------------------------------------------------------------- pixel features
def extract_features(small: Image.Image, exif: dict, size: tuple[int, int]) -> dict:
    """Cheap, explainable image statistics from a <=256 px thumbnail."""
    rgb = small.convert("RGB")
    gray = rgb.convert("L")
    width, height = gray.size
    count = max(1, width * height)
    hist = gray.histogram()
    white = sum(hist[205:]) / count
    dark = sum(hist[:90]) / count
    hsv = rgb.resize((64, 64), Image.Resampling.BILINEAR).convert("HSV")
    hs, ss, vs = [list(band.getdata()) for band in hsv.split()]
    n = len(hs)
    sat_mean = sum(ss) / (255.0 * n)
    warm = green = blue = 0
    for h, s, v in zip(hs, ss, vs):
        if s < 70 or v < 70:
            continue
        if h < 32 or h > 235:
            warm += 1
        elif 45 <= h <= 115:
            green += 1
        elif 120 <= h <= 190:
            blue += 1
    warm, green, blue = warm / n, green / n, blue / n

    pixels = list(gray.getdata())
    flat_pairs = total_pairs = 0
    for row in range(0, height, 2):
        base = row * width
        for col in range(width - 1):
            total_pairs += 1
            if abs(pixels[base + col] - pixels[base + col + 1]) <= 2:
                flat_pairs += 1
    flat = flat_pairs / max(1, total_pairs)

    # text-line periodicity: dark-pixel density per row oscillates in a page of text
    mean_gray = ImageStat.Stat(gray).mean[0]
    threshold = mean_gray * 0.72
    profile = [sum(1 for px in pixels[r * width:(r + 1) * width] if px < threshold) / width for r in range(height)]
    avg = sum(profile) / max(1, len(profile))
    crossings = 0
    if avg > 0.004:
        above = profile[0] > avg
        for value in profile[1:]:
            now = value > avg
            if now != above:
                crossings += 1
                above = now
    amplitude = (sum((p - avg) ** 2 for p in profile) / max(1, len(profile))) ** 0.5 / (avg + 1e-6)
    line_score = min(1.0, crossings / (0.12 * height)) * min(1.0, amplitude / 0.7)

    doc_score = 0.0
    doc_score += 0.30 if white > 0.45 else (0.15 if white > 0.30 else 0.0)
    doc_score += 0.20 if sat_mean < 0.20 else (0.08 if sat_mean < 0.30 else 0.0)
    doc_score += 0.10 if 0.01 < dark < 0.38 else 0.0
    doc_score += 0.40 * line_score
    if exif.get("camera") and white < 0.35:
        doc_score -= 0.15

    w, h = size
    long_edge, short_edge = max(w, h), max(1, min(w, h))
    aspect = long_edge / short_edge
    screenish = (aspect >= 1.7 and not exif.get("camera")) or (w, h) in {(1920, 1080), (1366, 768), (2560, 1440)}
    kind = "photo"
    if doc_score >= 0.62:
        kind = "document_scan"
    elif screenish and flat > 0.55 and not exif.get("camera"):
        kind = "screenshot"
    return {"kind": kind, "doc_score": round(doc_score, 3), "white": round(white, 3), "dark": round(dark, 3),
            "sat": round(sat_mean, 3), "warm": round(warm, 3), "green": round(green, 3), "blue": round(blue, 3),
            "flat": round(flat, 3), "line_score": round(line_score, 3), "camera": bool(exif.get("camera"))}


def visual_hint(features: dict) -> tuple[str | None, float, str]:
    """Weak, honest guess from pixels alone (only used when no text / vision AI is available)."""
    kind = features.get("kind")
    if kind == "screenshot":
        return "screenshot", 0.6, "screenshot layout"
    if kind == "document_scan":
        return "scans", 0.55, "looks like a scanned page"
    if features.get("green", 0) + features.get("blue", 0) > 0.55 and features.get("sat", 0) > 0.25:
        return "nature", 0.35, "mostly green/blue scenery"
    return None, 0.0, ""


# ---------------------------------------------------------------------- the decision
def classify_local(item: dict, features: dict | None = None, model: TextNB | None = None) -> dict:
    """Best local guess: text model first, pixel features second. Always returns a category."""
    features = features or {}
    text = text_blob(item)
    category, confidence, evidence = predict_from_text(text, model)
    source = "text-model"
    reason = f"{evidence} matching words" if category else ""

    visual_category, visual_conf, visual_reason = visual_hint(features)
    if category is None or (visual_category in {"screenshot"} and confidence < 0.8):
        if visual_category:
            category, confidence, source, reason = visual_category, visual_conf, "pixel-features", visual_reason
    elif features.get("kind") == "document_scan" and category in {"nature", "food", "pets", "travel", "festival", "family"} \
            and confidence < 0.8:
        category, confidence, source, reason = "scans", 0.55, "pixel-features", visual_reason

    if category is None:
        if item.get("media_type") == "document":
            category, confidence, source, reason = "scans", 0.4, "file-type", "document without readable text"
        else:
            category, confidence, source, reason = "other", 0.0, "none", ""
    return {"category": category, "confidence": round(float(confidence), 3), "source": source, "reason": reason}
