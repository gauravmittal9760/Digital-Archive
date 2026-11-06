"""Natural-language archive search ("meri 10th ki marksheet", "diwali 2023 photos").

Pipeline (all local, no paid API):
1. Query understanding - strips Hinglish filler words, detects category / media-type / year
   intent and expands concepts ("10th" -> tenth, matric, ssc ...).
2. Spelling tolerance - unknown words are matched against the archive's own vocabulary with
   difflib so "marksheat" still finds "marksheet".
3. Ranking - TF-IDF cosine similarity (ml_service) over filename, notes, tags, OCR/AI text,
   plus boosts for category and type intent.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime, timezone

from services import classifier, ml_service

FILLER = {
    "meri", "mera", "mere", "ki", "ka", "ke", "ko", "se", "me", "mein", "wali", "wala", "wale", "hai", "h", "hain", "do", "dikhao",
    "dikha", "dikhana", "batao", "bata", "mujhe", "muje", "chahiye", "show", "find", "search", "give", "get", "all", "my", "the",
    "of", "a", "an", "and", "please", "pls", "photo", "photos", "pic", "pics", "picture", "pictures", "image", "images", "file",
    "files", "wo", "woh", "vo", "wahi", "jo", "kahan", "kaha", "kidhar", "kaun", "sa", "si", "se", "sabhi", "saari", "sari",
    "mere", "apni", "apna", "apne", "in", "on", "for", "from", "with", "to", "me", "mai",
}

# concept -> (category intent, expansion tokens)
CONCEPTS: list[tuple[set[str], str | None, list[str]]] = [
    ({"10th", "tenth", "dasvi", "dasvin", "matric", "matriculation", "class10"}, "education", ["10th", "tenth", "x", "secondary", "ssc", "matric"]),
    ({"12th", "twelfth", "barvi", "barahvi", "intermediate", "class12"}, "education", ["12th", "twelfth", "xii", "senior", "hsc", "intermediate"]),
    ({"marksheet", "marksheets", "result", "results", "report", "card", "gradesheet", "scorecard", "number", "marks"}, "education", ["marksheet", "marks", "result", "grade"]),
    ({"school", "college", "university", "degree", "diploma", "admit", "semester", "exam", "certificate"}, "education", []),
    ({"aadhaar", "aadhar", "adhar", "uidai"}, "identity", ["aadhaar", "uidai", "unique", "identification"]),
    ({"pan", "pancard"}, "identity", ["pan", "permanent", "account", "number"]),
    ({"passport", "licence", "license", "dl", "voter", "id", "identity"}, "identity", []),
    ({"medical", "health", "prescription", "dawai", "davai", "doctor", "hospital", "blood", "xray", "mri", "lab", "pathology", "sugar"}, "medical", ["medical", "report", "patient"]),
    ({"bill", "invoice", "receipt", "bank", "statement", "payment", "insurance", "tax", "gst", "emi"}, "finance", []),
    ({"resume", "cv", "offer", "salary", "payslip", "project", "assignment", "presentation"}, "work", []),
    ({"diwali", "deepawali", "holi", "eid", "christmas", "navratri", "festival", "tyohar", "tyohaar", "rakhi", "pooja", "puja", "dussehra", "ganesh", "lohri", "onam", "pongal"}, "festival", []),
    ({"trip", "travel", "yatra", "safar", "ghumna", "tour", "vacation", "holiday", "beach", "manali", "goa", "shimla", "jaipur", "mountain", "hotel", "flight"}, "travel", []),
    ({"family", "parivar", "shaadi", "wedding", "birthday", "friends", "dost", "party", "selfie", "mummy", "papa", "bhai", "behen"}, "family", []),
    ({"nature", "sunset", "sunrise", "forest", "river", "garden", "waterfall", "sky"}, "nature", []),
    ({"food", "khana", "dinner", "lunch", "restaurant", "cake", "biryani", "pizza"}, "food", []),
    ({"pet", "dog", "cat", "puppy", "kitten", "animal", "animals", "janwar", "jaanwar", "jaanvar", "janvar", "pashu", "wildlife", "zoo", "bird", "birds", "pakshi", "chidiya", "insect", "kida"}, "pets", ["animal", "wildlife"]),
    ({"butterfly", "titli", "tiger", "bagh", "sher", "lion", "elephant", "hathi", "monkey", "bandar", "horse", "ghoda", "cow", "gaay", "peacock", "mor", "snake", "saanp", "fish", "machli"}, "pets", []),
    ({"phool", "flower", "flowers", "pedh", "ped", "tree", "jungle", "pahad", "nadi", "samudr", "sea", "ocean"}, "nature", []),
    ({"screenshot", "screenshots"}, "screenshot", []),
]
TYPE_WORDS = {"video": "video", "videos": "video", "vid": "video", "movie": "video", "clip": "video",
              "pdf": "document", "document": "document", "documents": "document", "doc": "document", "docx": "document",
              "photo": "photo", "photos": "photo", "pic": "photo", "pics": "photo", "image": "photo", "images": "photo"}
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_TOKEN = re.compile(r"[a-z0-9ऀ-ॿ]+")


def _vocabulary(items: list[dict]) -> set[str]:
    vocab: set[str] = set()
    for item in items:
        vocab.update(classifier.tokenize(classifier.text_blob(item)))
    vocab.update(classifier._SEED.vocab)          # so typos of common words (marksheet, aadhaar ...) are repaired too
    return vocab


def understand(query: str, vocab: set[str]) -> dict:
    raw_tokens = _TOKEN.findall(query.lower())
    intent = {"categories": [], "type": None, "year": None, "month": None, "terms": [], "corrected": {}}
    type_hint = None
    for token in raw_tokens:
        if token in TYPE_WORDS and token not in {"photo", "photos", "pic", "pics", "image", "images"}:
            type_hint = TYPE_WORDS[token]
        if re.fullmatch(r"(19|20)\d{2}", token):
            intent["year"] = int(token)
        elif token[:3] in MONTHS and len(token) <= 9 and token in {"january", "february", "march", "april", "june", "july", "august",
                                                                    "september", "october", "november", "december", "jan", "feb",
                                                                    "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}:
            intent["month"] = MONTHS[token[:3]]
    intent["type"] = type_hint

    terms: list[str] = []
    for token in raw_tokens:
        if token in FILLER or re.fullmatch(r"(19|20)\d{2}", token):
            continue
        word = token
        if word not in vocab and len(word) >= 4:
            close = difflib.get_close_matches(word, vocab, n=1, cutoff=0.8)
            if close:
                intent["corrected"][word] = close[0]
                word = close[0]
        terms.append(word)
        for keys, category, expansion in CONCEPTS:
            if word in keys or token in keys:
                if category and category not in intent["categories"]:
                    intent["categories"].append(category)
                terms.extend(expansion)
    # "10 th" / "class 10" style
    joined = " ".join(raw_tokens)
    for number, words in (("10", ["10th", "tenth", "ssc", "matric"]), ("12", ["12th", "twelfth", "hsc", "intermediate"])):
        if re.search(rf"\b{number}\s*(th|vi|vin|class)\b|\bclass\s*{number}\b", joined):
            terms.extend(words)
            if "education" not in intent["categories"]:
                intent["categories"].append("education")
    intent["terms"] = list(dict.fromkeys(terms))
    return intent


def _when(item: dict):
    value = item.get("captured_at") or item.get("created_at")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def search(items: list[dict], query: str, limit: int = 60) -> dict:
    """Return {"results": [...], "intent": {...}}; every result has `score` and `why`."""
    query = (query or "").strip()
    if not query or not items:
        return {"results": [], "intent": {}}
    vocab = _vocabulary(items)
    intent = understand(query, vocab)
    pool = items
    if intent["type"]:
        typed = [i for i in pool if i.get("media_type") == intent["type"]]
        pool = typed or pool
    if intent["year"]:
        pool = [i for i in pool if (w := _when(i)) and w.year == intent["year"]] or []
    if intent["month"]:
        pool = [i for i in pool if (w := _when(i)) and w.month == intent["month"]] or pool

    text_query = " ".join(intent["terms"]) or ("" if intent["categories"] else query)
    ranked = {r["id"]: r for r in ml_service.search_library(pool, text_query, limit=200)} if text_query else {}
    results = []
    for item in pool:
        category = classifier.normalize_category(item.get("category"))
        base = ranked.get(item["id"], {}).get("ml_score", 0.0)
        boost = 0.0
        why = []
        if category in intent["categories"]:
            boost += 0.45
            why.append(f"category: {classifier.CATEGORY_LABEL[category]}")
        if base > 0:
            why.append("text match")
        elif category == "scans" and intent["categories"] and set(intent["categories"]) & {"education", "identity", "medical", "finance", "work"}:
            boost += 0.1                       # unreadable scan: a plausible candidate when no text could be read
            why.append("document scan (text unreadable)")
        if intent["year"] or intent["month"]:
            boost += 0.05
        score = base + boost
        if score <= 0.04 and not (intent["categories"] and category in intent["categories"]):
            continue
        row = dict(item)
        row["score"] = round(score, 3)
        row["why"] = " · ".join(why) or "related"
        results.append(row)
    results.sort(key=lambda r: r["score"], reverse=True)
    return {"results": results[:limit], "intent": intent}
