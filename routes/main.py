"""Dashboard and grouped hub pages."""
from __future__ import annotations

from collections import Counter

from flask import Blueprint, abort, g, redirect, render_template, url_for
from sqlalchemy import select

from database import engine, media
from routes.common import login_required, media_view
from services import ai_service, classifier, stats_service

bp = Blueprint("main", __name__)

# slug -> hub definition. Each item: (label, description, endpoint, kwargs, icon)
HUBS = {
    "library": {
        "title": "My Library", "icon": "image", "tone": "indigo",
        "blurb": "Everything you have saved, organised the way you remember it.",
        "items": [
            ("Upload", "Add photos, videos & documents", "media.upload", {}, "upload"),
            ("All memories", "Browse everything", "media.gallery", {}, "grid"),
            ("Photos", "Only pictures", "media.gallery", {"type": "photo"}, "image"),
            ("Videos", "Only videos", "media.gallery", {"type": "video"}, "video"),
            ("Documents", "PDFs & office files", "media.gallery", {"type": "document"}, "file"),
            ("Favorites", "Your starred memories", "media.gallery", {"fav": 1}, "heart"),
            ("Albums", "Collections you created", "albums.index", {}, "folder"),
            ("Timeline", "Explore by date", "media.timeline", {}, "clock"),
            ("Trash", "Restore or delete for good", "media.trash", {}, "trash"),
        ]},
    "smart": {
        "title": "Smart Tools", "icon": "sparkles", "tone": "violet",
        "blurb": "AI and machine learning that organise and find things for you.",
        "items": [
            ("Smart Search", "Ask like “meri 10th ki marksheet”", "intelligence.search", {}, "search"),
            ("Auto Categories", "Festivals, travel, medical, marksheets…", "media.categories", {}, "tag"),
            ("AI & ML Studio", "Groups and unusual files", "intelligence.studio", {}, "cpu"),
            ("Duplicate Finder", "Exact and look-alike copies", "intelligence.duplicates", {}, "copy"),
        ]},
    "storage": {
        "title": "Storage & Backup", "icon": "database", "tone": "teal",
        "blurb": "See what you use and keep a copy of everything.",
        "items": [
            ("Storage usage", "How much space you use", "intelligence.storage", {}, "pie"),
            ("Download archive", "Export your files as a ZIP", "media.download_all", {}, "download"),
            ("Trash", "Files waiting for deletion", "media.trash", {}, "trash"),
        ]},
    "sharing": {
        "title": "Sharing", "icon": "share", "tone": "pink",
        "blurb": "Private, expiring links you can revoke any time.",
        "items": [("Share links", "Manage the links you created", "media.shares", {}, "link")]},
    "account": {
        "title": "Account", "icon": "user", "tone": "amber",
        "blurb": "Your profile, security and activity.",
        "items": [
            ("Profile", "Your picture and details", "account.profile", {}, "user"),
            ("Settings", "Security, privacy and data", "account.settings", {}, "settings"),
            ("Activity history", "Recent actions on your account", "account.activity", {}, "activity"),
        ]},
}


def _resolve(items):
    return [{"label": l, "desc": d, "url": url_for(e, **k), "icon": i} for l, d, e, k, i in items]


@bp.route("/")
def home():
    return redirect(url_for("main.dashboard") if g.user else url_for("auth.login"))


@bp.route("/dashboard")
@login_required
def dashboard():
    uid = g.user["id"]
    ai_service.sweep_stale(uid)
    counts = stats_service.library_counts(uid)
    overview = stats_service.storage_overview(uid)
    with engine.connect() as conn:
        recent = [media_view(dict(r._mapping)) for r in conn.execute(select(media).where(
            media.c.user_id == uid, media.c.is_deleted.is_(False)).order_by(media.c.created_at.desc()).limit(8)).all()]
        cats = Counter(classifier.normalize_category(r[0]) for r in conn.execute(select(media.c.category).where(
            media.c.user_id == uid, media.c.is_deleted.is_(False))).all())
    top = [{"id": c, "label": classifier.CATEGORY_LABEL[c], "color": classifier.CATEGORY_COLOR[c], "count": n}
           for c, n in cats.most_common(6) if c != "other" or len(cats) == 1]
    hubs = [{"slug": slug, **{k: v for k, v in hub.items() if k != "items"}, "count": len(hub["items"])} for slug, hub in HUBS.items()]
    return render_template("dashboard.html", counts=counts, overview=overview, recent=recent, top_categories=top, hubs=hubs)


@bp.route("/hub/<slug>")
@login_required
def hub(slug):
    definition = HUBS.get(slug)
    if not definition:
        abort(404)
    return render_template("hub.html", hub=definition, items=_resolve(definition["items"]), back_url=url_for("main.dashboard"))
