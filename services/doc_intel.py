"""Document intelligence: spot the kind of document and any expiry date in its text.

Pure regex/keyword logic - no model, no network, microseconds per file.
"""
from __future__ import annotations

import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# doc_type -> (label, keywords, tracks_expiry)
DOC_TYPES = {
    "passport": ("Passport", ("passport", "republic of india", "place of issue"), True),
    "licence": ("Driving licence", ("driving licence", "driving license", "licence no", "transport department"), True),
    "insurance": ("Insurance", ("insurance", "policy no", "policy number", "premium", "sum insured"), True),
    "warranty": ("Warranty", ("warranty", "guarantee card", "warranty period"), True),
    "pan": ("PAN card", ("permanent account number", "income tax department"), False),
    "aadhaar": ("Aadhaar", ("aadhaar", "aadhar", "unique identification authority"), False),
    "marksheet": ("Marksheet", ("marksheet", "mark sheet", "statement of marks", "grade card", "marks obtained"), False),
    "certificate": ("Certificate", ("certificate", "certify that", "hereby certified"), False),
    "invoice": ("Invoice / bill", ("invoice", "tax invoice", "bill no", "receipt", "amount paid", "gstin"), False),
    "ticket": ("Ticket", ("boarding pass", "pnr", "e-ticket", "ticket no"), False),
    "medical": ("Medical record", ("prescription", "diagnosis", "patient name", "lab report", "discharge summary"), False),
    "bank": ("Bank / finance", ("bank statement", "account number", "ifsc", "cheque"), False),
}
EXPIRY_WORDS = r"(?:valid\s*(?:till|upto|up\s*to|until|thru|through)|expiry(?:\s*date)?|expires?(?:\s*on)?|date\s*of\s*expiry|" \
               r"renewal\s*date|due\s*date|valid\s*to|end\s*date)"
_DATE = (r"(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})"
         r"|(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})"
         r"|([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
EXPIRY_RE = re.compile(EXPIRY_WORDS + r"[^0-9A-Za-z]{0,12}(?:" + _DATE + ")", re.I)
ANY_DATE_RE = re.compile(_DATE, re.I)


def _mk(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _groups_to_date(g: tuple) -> date | None:
    if g[0]:
        return _mk(int(g[2]), int(g[1]), int(g[0]))
    if g[3]:
        m = MONTHS.get(g[4].lower()[:3])
        return _mk(int(g[5]), m, int(g[3])) if m else None
    m = MONTHS.get(g[6].lower()[:3]) if g[6] else None
    return _mk(int(g[8]), m, int(g[7])) if m else None


def find_expiry(text: str, today: date | None = None) -> date | None:
    """Latest date that follows an expiry-style keyword and is plausible (2000-2100)."""
    found = []
    for match in EXPIRY_RE.finditer(text or ""):
        d = _groups_to_date(match.groups())
        if d and 2000 <= d.year <= 2100:
            found.append(d)
    return max(found) if found else None


def detect_type(text: str, file_name: str = "") -> tuple[str | None, bool]:
    hay = f"{file_name}\n{text or ''}".lower()
    best, best_hits = None, 0
    for key, (_label, words, _track) in DOC_TYPES.items():
        hits = sum(1 for w in words if w in hay)
        if hits > best_hits:
            best, best_hits = key, hits
    if not best:
        return None, False
    return best, DOC_TYPES[best][2]


def label(doc_type: str | None) -> str:
    return DOC_TYPES.get(doc_type or "", ("", (), False))[0]


def analyze(text: str, file_name: str = "") -> dict:
    """Returns {doc_type, doc_title, expiry_date} (values may be None)."""
    text = (text or "")[:8000]
    doc_type, tracks = detect_type(text, file_name)
    expiry = find_expiry(text) if (doc_type is None or tracks or doc_type in {"certificate", "licence"}) else None
    title = label(doc_type) or None
    return {"doc_type": doc_type, "doc_title": title, "expiry_date": expiry}
