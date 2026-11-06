"""Shared helpers for all blueprints."""
from __future__ import annotations

import secrets
from functools import wraps
from urllib.parse import urlparse

from flask import abort, flash, g, redirect, request, session, url_for
from sqlalchemy import select

from database import engine, media
from services import classifier, stats_service, storage_service, trash_service


def safe_next(candidate: str | None, fallback: str = "main.dashboard") -> str:
    """Only same-site relative redirects are allowed (blocks open-redirect tricks)."""
    if candidate:
        parsed = urlparse(candidate)
        if not parsed.scheme and not parsed.netloc and candidate.startswith("/") and not candidate.startswith("//") \
                and "\\" not in candidate:
            return candidate
        if parsed.scheme in {"http", "https"} and parsed.netloc == request.host and parsed.path.startswith("/") \
                and not parsed.path.startswith("//"):
            return parsed.path + (("?" + parsed.query) if parsed.query else "")
    return url_for(fallback)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            flash("Please sign in to continue.", "error")
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = session["csrf_token"] = secrets.token_urlsafe(32)
    return token


def media_view(row: dict, with_url: bool = True) -> dict:
    """Decorate a media row with display fields (URLs, labels, sizes)."""
    item = dict(row)
    category = classifier.normalize_category(item.get("category"))
    item["category"] = category
    item["category_label"] = classifier.CATEGORY_LABEL[category]
    item["category_color"] = classifier.CATEGORY_COLOR[category]
    item["size_h"] = stats_service.human_size(item.get("file_size") or 0)
    if with_url:
        mtype = item.get("media_type")
        if mtype == "document":
            item["url"] = url_for("media.file", media_id=item["id"])
            item["thumb_url"] = storage_service.delivery_url(item.get("thumb_path")) if item.get("thumb_path") else None
        else:
            item["url"] = storage_service.delivery_url(item.get("storage_path"))
            item["thumb_url"] = storage_service.delivery_url(item.get("thumb_path")) if item.get("thumb_path") else (
                item["url"] if mtype == "photo" else None)
    if item.get("is_deleted"):
        item["days_left"] = trash_service.days_left(item.get("deleted_at"))
    return item


def owned_media(media_id: str, include_deleted: bool = False) -> dict:
    stmt = select(media).where(media.c.id == media_id, media.c.user_id == g.user["id"])
    if not include_deleted:
        stmt = stmt.where(media.c.is_deleted.is_(False))
    with engine.connect() as conn:
        row = conn.execute(stmt).mappings().first()
    if not row:
        abort(404)
    return dict(row)


def password_form_values():
    return (request.form.get("new_password", ""), request.form.get("confirm_password", ""))
