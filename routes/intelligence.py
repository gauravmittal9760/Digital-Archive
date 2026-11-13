"""Smart search, AI & ML studio, duplicates and storage."""
from __future__ import annotations

from collections import Counter

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select, update

import re

from database import engine, media, utcnow
from routes.common import login_required, media_view
from services import ai_service, classifier, doc_intel, duplicate_service, ml_service, reminder_service, smart_search, stats_service
from services.events import log_activity

bp = Blueprint("intelligence", __name__)


def _all_items(user_id: str, limit: int = 1500) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(media).where(media.c.user_id == user_id, media.c.is_deleted.is_(False))
                                                       .order_by(media.c.created_at.desc()).limit(limit)).all()]


@bp.route("/search")
@login_required
def search():
    query = request.args.get("q", "").strip()[:160]
    result = {"results": [], "intent": {}}
    if query:
        items = _all_items(g.user["id"])
        for item in items:
            item["category_label"] = classifier.CATEGORY_LABEL[classifier.normalize_category(item.get("category"))]
        result = smart_search.search(items, query)
    results = [media_view(r) for r in result["results"]]
    return render_template("intelligence/search.html", q=query, results=results, intent=result["intent"],
                           back_url=url_for("main.hub", slug="smart"))


@bp.route("/studio")
@login_required
def studio():
    items = _all_items(g.user["id"])
    report = ml_service.build_intelligence(items)
    for cluster in report["clusters"]:
        cluster["items"] = [media_view(i) for i in cluster["items"][:8]]
    anomalies = [media_view(i) for i in report["anomalies"][:8]]
    cats = Counter(classifier.normalize_category(i.get("category")) for i in items)
    distribution = [{"id": c, "label": classifier.CATEGORY_LABEL[c], "color": classifier.CATEGORY_COLOR[c], "count": n}
                    for c, n in cats.most_common()]
    counts = {"total": len(items), "categorised": sum(n for c, n in cats.items() if c != "other"),
              "groups": len(report["clusters"]), "outliers": len(anomalies),
              "ai_done": sum(1 for i in items if i.get("ai_status") == "done" and i.get("ai_source") == "gemini-vision")}
    return render_template("intelligence/studio.html", counts=counts, clusters=report["clusters"], anomalies=anomalies,
                           distribution=distribution, ai_mode=ai_service.ai_mode_label(), vision=ai_service.vision_available(),
                           back_url=url_for("main.hub", slug="smart"))


@bp.route("/duplicates")
@login_required
def duplicates():
    groups = duplicate_service.find_duplicate_groups(g.user["id"])
    for group in groups:
        group["items"] = [media_view(i) for i in group["items"]]
    return render_template("intelligence/duplicates.html", groups=groups,
                           reclaim=sum(grp["reclaimable"] for grp in groups), back_url=url_for("main.hub", slug="smart"))


@bp.route("/duplicates/clean", methods=["POST"])
@login_required
def clean_duplicates():
    ids = request.form.getlist("media_ids")[:300]
    if not ids:
        flash("Select the copies you want to move to trash.", "error")
        return redirect(url_for("intelligence.duplicates"))
    with engine.begin() as conn:
        result = conn.execute(update(media).where(media.c.user_id == g.user["id"], media.c.id.in_(ids), media.c.is_deleted.is_(False))
                              .values(is_deleted=True, deleted_at=utcnow(), updated_at=utcnow()))
    log_activity(g.user["id"], "duplicates_trashed", f"{result.rowcount} duplicate(s) moved to trash")
    flash(f"{result.rowcount} duplicate(s) moved to trash.", "success")
    return redirect(url_for("intelligence.duplicates"))


@bp.route("/storage")
@login_required
def storage():
    overview = stats_service.storage_overview(g.user["id"])
    return render_template("intelligence/storage.html", o=overview, tips=stats_service.optimizer_tips(g.user["id"], overview), back_url=url_for("main.hub", slug="storage"))


@bp.route("/expiry")
@login_required
def expiry():
    items = reminder_service.expiring(g.user["id"], within_days=365)
    expired = [media_view(i) | {"days_left": i["days_left"], "doc_title": i.get("doc_title")} for i in items if i["days_left"] < 0]
    soon = [media_view(i) | {"days_left": i["days_left"], "doc_title": i.get("doc_title")} for i in items if i["days_left"] >= 0]
    return render_template("intelligence/expiry.html", expired=expired, soon=soon, back_url=url_for("main.hub", slug="smart"))


@bp.route("/ask")
@login_required
def ask():
    """Find the lines inside your documents that answer a question (keyword + smart-search ranking, fully local)."""
    q = request.args.get("q", "").strip()[:160]
    answers = []
    if q:
        items = _all_items(g.user["id"])
        for item in items:
            item["category_label"] = classifier.CATEGORY_LABEL[classifier.normalize_category(item.get("category"))]
        ranked = smart_search.search(items, q)["results"][:8]
        words = [w for w in re.findall(r"\w{3,}", q.lower()) if w not in {"the", "and", "for", "what", "which", "kab", "kya", "meri", "mera", "mere"}]
        for item in ranked:
            snippets = []
            for line in re.split(r"[\n\r]+", item.get("extracted_text") or ""):
                if any(w in line.lower() for w in words) and 6 <= len(line.strip()) <= 300:
                    snippets.append(line.strip())
                if len(snippets) >= 3:
                    break
            answers.append({"item": media_view(item), "snippets": snippets})
    return render_template("intelligence/ask.html", q=q, answers=answers, back_url=url_for("main.hub", slug="smart"))


@bp.route("/review")
@login_required
def review():
    """Files the AI was unsure about - confirm or fix in one click."""
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(media).where(
            media.c.user_id == g.user["id"], media.c.is_deleted.is_(False), media.c.review_dismissed.is_(False),
            media.c.category_locked.is_(False), media.c.ai_status == "done",
            ((media.c.category == "other") | (media.c.ai_confidence < 0.5))).order_by(media.c.created_at.desc()).limit(60)).all()]
    return render_template("intelligence/review.html", items=[media_view(r) for r in rows], back_url=url_for("main.hub", slug="smart"))


@bp.route("/review/<media_id>", methods=["POST"])
@login_required
def review_action(media_id):
    category = request.form.get("category", "")
    values = {"updated_at": utcnow(), "review_dismissed": True}
    if not request.form.get("dismiss") and category in classifier.CATEGORY_LABEL:
        values.update(category=category, category_locked=True)
        classifier.forget_user_model(g.user["id"])
    with engine.begin() as conn:
        conn.execute(update(media).where(media.c.id == media_id, media.c.user_id == g.user["id"]).values(**values))
    return redirect(url_for("intelligence.review"))
