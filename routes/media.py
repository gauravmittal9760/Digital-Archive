"""Upload, gallery, categories, trash, sharing, versions, export."""
from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import zipfile
from collections import Counter, OrderedDict
from datetime import timedelta
from tempfile import SpooledTemporaryFile

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, stream_with_context, url_for)
from sqlalchemy import delete, func, insert, or_, select, update

import config
from database import engine, file_versions, media, new_id, share_links, utcnow, aware
from routes.common import login_required, media_view, owned_media, safe_next
from services import ai_service, classifier, media_service, stats_service, storage_service, trash_service, upload_service
from services.events import log_activity

bp = Blueprint("media", __name__)
PAGE_SIZE = 60


# ----------------------------------------------------------------------- upload
@bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":                          # no-JavaScript fallback: several files in one request
        files = [f for f in request.files.getlist("media") if f and f.filename]
        if not files:
            flash("Choose at least one file.", "error")
            return redirect(url_for("media.upload"))
        done = 0
        used = stats_service.storage_used(g.user["id"])
        for storage in files:
            result = upload_service.ingest(g.user, storage, request.form.get("tags", ""), request.form.get("description", ""),
                                           request.form.get("category", "auto"), used_bytes=used)
            if result["status"] == "ok":
                done += 1
                used += result.get("stored", 0)
            else:
                flash(result["message"], "error" if result["status"] == "error" else "info")
        if done:
            log_activity(g.user["id"], "upload", f"Uploaded {done} file(s)")
            flash(f"{done} file(s) uploaded and sorted automatically.", "success")
        return redirect(url_for("media.upload"))
    return render_template("media/upload.html", overview=stats_service.storage_overview(g.user["id"]),
                           ai_mode=ai_service.ai_mode_label(), back_url=url_for("main.hub", slug="library"))


@bp.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    """One file per request so the browser can show real per-file progress."""
    storage = request.files.get("file")
    if not storage or not storage.filename:
        return jsonify(status="error", message="No file received."), 400
    result = upload_service.ingest(g.user, storage, request.form.get("tags", ""), request.form.get("description", ""),
                                   request.form.get("category", "auto"))
    payload = {"status": result["status"], "message": result["message"]}
    if result["status"] == "ok":
        category = result.get("category", "other")
        payload.update(media_id=result["media_id"], category=classifier.CATEGORY_LABEL.get(category, "Uncategorised"),
                       saved=result.get("saved", 0), near_duplicate=result.get("near_duplicate"))
        log_activity(g.user["id"], "upload", result["message"][:200])
    return jsonify(payload), (200 if result["status"] != "error" else 422)


@bp.route("/api/media/status")
@login_required
def api_status():
    ids = [i for i in request.args.get("ids", "").split(",") if i][:100]
    with engine.connect() as conn:
        rows = conn.execute(select(media.c.id, media.c.category, media.c.ai_status).where(
            media.c.user_id == g.user["id"], media.c.id.in_(ids))).all()
    return jsonify({r[0]: {"category": classifier.normalize_category(r[1]), "label": classifier.CATEGORY_LABEL[classifier.normalize_category(r[1])],
                           "color": classifier.CATEGORY_COLOR[classifier.normalize_category(r[1])], "status": r[2]} for r in rows})


# ---------------------------------------------------------------------- browsing
def _filtered(user_id: str, args) -> tuple:
    stmt = select(media).where(media.c.user_id == user_id, media.c.is_deleted.is_(False))
    kind = args.get("type", "all")
    if kind in {"photo", "video", "document"}:
        stmt = stmt.where(media.c.media_type == kind)
    category = args.get("category", "").strip()
    if category:
        legacy = [k for k, v in classifier.LEGACY_CATEGORY.items() if v == category]
        stmt = stmt.where(media.c.category.in_([category, *legacy]))
    if args.get("fav"):
        stmt = stmt.where(media.c.is_favorite.is_(True))
    text = args.get("q", "").strip()[:80]
    if text:
        like = f"%{text}%"
        stmt = stmt.where(or_(media.c.file_name.ilike(like), media.c.description.ilike(like), media.c.tags.ilike(like)))
    sort = args.get("sort", "newest")
    order = {"oldest": media.c.created_at.asc(), "name": media.c.file_name.asc(), "size": media.c.file_size.desc()}.get(sort, media.c.created_at.desc())
    return stmt.order_by(order), kind, category, text, sort


@bp.route("/gallery")
@login_required
def gallery():
    stmt, kind, category, text, sort = _filtered(g.user["id"], request.args)
    page = max(1, request.args.get("page", 1, type=int))
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar() or 0
        rows = [media_view(dict(r._mapping)) for r in conn.execute(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()]
    title = "Favorites" if request.args.get("fav") else (classifier.CATEGORY_LABEL.get(category) if category else
            {"photo": "Photos", "video": "Videos", "document": "Documents"}.get(kind, "All memories"))
    return render_template("media/gallery.html", items=rows, total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)),
                           kind=kind, category=category, q=text, sort=sort, title=title, fav=bool(request.args.get("fav")),
                           pending=[i["id"] for i in rows if i.get("ai_status") == "pending"],
                           back_url=url_for("media.categories") if category else url_for("main.hub", slug="library"))


@bp.route("/categories")
@login_required
def categories():
    with engine.connect() as conn:
        rows = conn.execute(select(media.c.category, media.c.id, media.c.thumb_path, media.c.storage_path, media.c.media_type)
                            .where(media.c.user_id == g.user["id"], media.c.is_deleted.is_(False))
                            .order_by(media.c.created_at.desc())).all()
    counts, covers = Counter(), {}
    for category, _id, thumb, path, mtype in rows:
        category = classifier.normalize_category(category)
        counts[category] += 1
        if category not in covers and mtype != "document" and (thumb or mtype == "photo"):
            covers[category] = storage_service.delivery_url(thumb or path)
    cards = [{"id": cid, "label": label, "color": color, "help": classifier.CATEGORY_HELP[cid], "count": counts.get(cid, 0),
              "cover": covers.get(cid)} for cid, label, color, _ in classifier.CATEGORIES]
    cards.sort(key=lambda c: (c["count"] == 0, -c["count"]))
    return render_template("media/categories.html", cards=cards, total=sum(counts.values()), back_url=url_for("main.hub", slug="smart"))


@bp.route("/categories/rescan", methods=["POST"])
@login_required
def rescan_all():
    """Queue every picture you have not categorised by hand for a fresh AI analysis."""
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(select(media.c.id).where(
            media.c.user_id == g.user["id"], media.c.is_deleted.is_(False), media.c.category_locked.is_(False),
            media.c.media_type.in_(["photo", "video"])).order_by(media.c.created_at.desc()).limit(300)).all()]
    for media_id in ids:
        ai_service.reanalyze(media_id)
    flash(f"{len(ids)} file(s) are being re-analysed. Refresh in a minute.", "success" if ids else "info")
    return redirect(url_for("media.categories"))


@bp.route("/timeline")
@login_required
def timeline():
    with engine.connect() as conn:
        rows = [media_view(dict(r._mapping)) for r in conn.execute(select(media).where(
            media.c.user_id == g.user["id"], media.c.is_deleted.is_(False))
            .order_by(func.coalesce(media.c.captured_at, media.c.created_at).desc()).limit(600)).all()]
    groups: "OrderedDict[str, list]" = OrderedDict()
    for item in rows:
        when = item.get("captured_at") or item.get("created_at")
        groups.setdefault(when.strftime("%B %Y") if when else "Undated", []).append(item)
    return render_template("media/timeline.html", groups=groups, back_url=url_for("main.hub", slug="library"))


# ------------------------------------------------------------------------ detail
@bp.route("/media/<media_id>")
@login_required
def detail(media_id):
    row = media_view(owned_media(media_id))
    with engine.connect() as conn:
        versions = conn.execute(select(func.count()).where(file_versions.c.media_id == media_id)).scalar() or 0
    return render_template("media/detail.html", item=row, versions=versions, back_url=request.referrer and safe_next(request.referrer, "media.gallery") or url_for("media.gallery"))


@bp.route("/media/<media_id>/file")
@login_required
def file(media_id):
    row = owned_media(media_id)
    return _stream(row, download=bool(request.args.get("download")))


def _stream(row: dict, download: bool = False):
    try:
        stream, length = storage_service.open_stream(row["storage_path"])
    except Exception:
        abort(404)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(row.get("file_name") or "file"))[:160] or "file"
    if download and row["media_type"] == "photo" and not safe_name.lower().endswith((".webp", ".jpg", ".jpeg", ".png", ".gif")):
        safe_name += "." + row["storage_path"].rsplit(".", 1)[-1]

    def generate():
        try:
            while chunk := stream.read(64 * 1024):
                yield chunk
        finally:
            stream.close()

    headers = {"Content-Disposition": f'{"attachment" if download else "inline"}; filename="{safe_name}"', "X-Content-Type-Options": "nosniff"}
    if length:
        headers["Content-Length"] = str(length)
    return Response(stream_with_context(generate()), mimetype=row.get("mime_type") or "application/octet-stream", headers=headers)


@bp.route("/local-file/<path:storage_path>")
def local_file(storage_path):
    """Development-only private file delivery (used when Cloudinary is not configured)."""
    allowed = (g.user and storage_path.split("/")[0] == g.user["id"]) or bool(session.get("admin_auth"))
    if config.IS_PRODUCTION or not allowed:
        abort(404)
    try:
        path = storage_service._local_path(storage_path)
        if not path.is_file():
            abort(404)
        return send_from_directory(path.parent, path.name, conditional=True)
    except ValueError:
        abort(404)


# ------------------------------------------------------------------------ updates
@bp.route("/media/<media_id>/update", methods=["POST"])
@login_required
def update_media(media_id):
    row = owned_media(media_id)
    category = request.form.get("category", "auto").strip().lower()
    values = {"description": request.form.get("description", "").strip()[:1000] or None,
              "tags": request.form.get("tags", "").strip()[:500] or None, "updated_at": utcnow()}
    if category in classifier.CATEGORY_LABEL:
        values.update(category=category, category_locked=True)
    elif category == "auto":
        values["category_locked"] = False
    with engine.begin() as conn:
        conn.execute(update(media).where(media.c.id == media_id, media.c.user_id == g.user["id"]).values(**values))
    classifier.forget_user_model(g.user["id"])
    if category == "auto":
        ai_service.reanalyze(media_id)
        flash("Details saved. The file is being re-analysed.", "success")
    else:
        flash("Details saved.", "success")
    return redirect(request.form.get("return") and safe_next(request.form["return"]) or url_for("media.detail", media_id=media_id))


@bp.route("/media/<media_id>/reanalyze", methods=["POST"])
@login_required
def reanalyze(media_id):
    owned_media(media_id)
    ai_service.reanalyze(media_id)
    flash("Re-analysis started. The category will refresh in a moment.", "success")
    return redirect(url_for("media.detail", media_id=media_id))


@bp.route("/media/<media_id>/favorite", methods=["POST"])
@login_required
def favorite(media_id):
    row = owned_media(media_id)
    new_value = not row["is_favorite"]
    with engine.begin() as conn:
        conn.execute(update(media).where(media.c.id == media_id).values(is_favorite=new_value, updated_at=utcnow()))
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(favorite=new_value)
    return redirect(safe_next(request.referrer, "media.gallery"))


@bp.route("/media/<media_id>/delete", methods=["POST"])
@login_required
def delete_media(media_id):
    row = owned_media(media_id)
    with engine.begin() as conn:
        conn.execute(update(media).where(media.c.id == media_id).values(is_deleted=True, deleted_at=utcnow(), updated_at=utcnow()))
        conn.execute(update(share_links).where(share_links.c.media_id == media_id, share_links.c.revoked_at.is_(None)).values(revoked_at=utcnow()))
    log_activity(g.user["id"], "moved_to_trash", row["file_name"])
    flash(f"Moved to trash. It will be deleted permanently in {config.TRASH_RETENTION_DAYS} days unless you restore it.", "success")
    return redirect(url_for("media.gallery"))


@bp.route("/bulk", methods=["POST"])
@login_required
def bulk():
    ids = list(dict.fromkeys(request.form.getlist("media_ids")))[:200]
    action = request.form.get("action", "")
    if not ids:
        flash("Select at least one file first.", "error")
        return redirect(safe_next(request.referrer, "media.gallery"))
    with engine.begin() as conn:
        owned = [r[0] for r in conn.execute(select(media.c.id).where(media.c.user_id == g.user["id"], media.c.id.in_(ids),
                                                                     media.c.is_deleted.is_(False))).all()]
        where = (media.c.user_id == g.user["id"], media.c.id.in_(owned))
        if action == "favorite":
            conn.execute(update(media).where(*where).values(is_favorite=True))
        elif action == "unfavorite":
            conn.execute(update(media).where(*where).values(is_favorite=False))
        elif action == "trash":
            conn.execute(update(media).where(*where).values(is_deleted=True, deleted_at=utcnow(), updated_at=utcnow()))
        elif action.startswith("cat:") and action[4:] in classifier.CATEGORY_LABEL:
            conn.execute(update(media).where(*where).values(category=action[4:], category_locked=True, updated_at=utcnow()))
            classifier.forget_user_model(g.user["id"])
        else:
            flash("Unknown action.", "error")
            return redirect(safe_next(request.referrer, "media.gallery"))
    log_activity(g.user["id"], "bulk_action", f"{action}: {len(owned)} file(s)")
    flash(f"{len(owned)} file(s) updated.", "success")
    return redirect(safe_next(request.referrer, "media.gallery"))


# -------------------------------------------------------------------------- trash
@bp.route("/trash")
@login_required
def trash():
    with engine.connect() as conn:
        rows = [media_view(dict(r._mapping)) for r in conn.execute(select(media).where(
            media.c.user_id == g.user["id"], media.c.is_deleted.is_(True)).order_by(media.c.deleted_at.desc())).all()]
    return render_template("media/trash.html", items=rows, days=config.TRASH_RETENTION_DAYS, back_url=url_for("main.hub", slug="library"))


@bp.route("/trash/<media_id>/restore", methods=["POST"])
@login_required
def restore(media_id):
    row = owned_media(media_id, include_deleted=True)
    with engine.begin() as conn:
        conn.execute(update(media).where(media.c.id == media_id).values(is_deleted=False, deleted_at=None, updated_at=utcnow()))
    log_activity(g.user["id"], "restored", row["file_name"])
    flash("Restored to your library.", "success")
    return redirect(url_for("media.trash"))


@bp.route("/trash/<media_id>/delete", methods=["POST"])
@login_required
def purge_one(media_id):
    row = owned_media(media_id, include_deleted=True)
    if not row["is_deleted"]:
        abort(400)
    if trash_service.purge_media_rows([row]):
        log_activity(g.user["id"], "deleted_forever", row["file_name"])
        flash("Deleted permanently.", "success")
    else:
        flash("Could not delete the file from storage. Please try again.", "error")
    return redirect(url_for("media.trash"))


@bp.route("/trash/empty", methods=["POST"])
@login_required
def empty_trash():
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(media.c.id, media.c.storage_path, media.c.thumb_path, media.c.file_name)
                                                      .where(media.c.user_id == g.user["id"], media.c.is_deleted.is_(True))).all()]
    removed = trash_service.purge_media_rows(rows)
    log_activity(g.user["id"], "trash_emptied", f"{removed} file(s) deleted permanently")
    flash(f"{removed} file(s) deleted permanently." if removed == len(rows) else
          f"{removed} of {len(rows)} deleted; the rest will be retried.", "success" if removed else "error")
    return redirect(url_for("media.trash"))


# ----------------------------------------------------------------------- versions
@bp.route("/media/<media_id>/versions")
@login_required
def versions(media_id):
    item = media_view(owned_media(media_id))
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(file_versions).where(
            file_versions.c.media_id == media_id, file_versions.c.user_id == g.user["id"]).order_by(file_versions.c.created_at.desc())).all()]
    for row in rows:
        row["size_h"] = stats_service.human_size(row["file_size"])
        row["url"] = storage_service.delivery_url(row["storage_path"])
    return render_template("media/versions.html", item=item, versions=rows, back_url=url_for("media.detail", media_id=media_id))


def _archive_current(conn, current: dict) -> None:
    conn.execute(insert(file_versions).values(
        id=new_id(), user_id=current["user_id"], media_id=current["id"], storage_path=current["storage_path"],
        file_size=current.get("file_size") or 0, file_name=current.get("file_name"), mime_type=current.get("mime_type"),
        checksum=current.get("checksum"), width=current.get("width"), height=current.get("height"),
        extracted_text=current.get("extracted_text"), created_at=utcnow()))


@bp.route("/media/<media_id>/replace", methods=["POST"])
@login_required
def replace(media_id):
    current = owned_media(media_id)
    storage = request.files.get("file")
    try:
        if not storage or not storage.filename:
            raise ValueError("Choose a replacement file first.")
        data = storage.read()
        mimetype = media_service.resolve_mimetype(storage.mimetype, storage.filename)
        kind = media_service.get_media_type(mimetype)
        if kind != current["media_type"] or not data or not media_service.validate_file_size(data, kind) \
                or not media_service.validate_file_content(data, kind, mimetype):
            raise ValueError("The replacement must be a valid file of the same type as the current one.")
        name = os.path.basename(storage.filename.replace("\\", "/"))[:255]
        width = height = None
        thumb_path = current.get("thumb_path")
        unique = media_service.uuid.uuid4().hex
        thumb_bytes = None
        if kind == "photo":
            processed = media_service.process_image(data, classify=classifier.extract_features)
            stored, content_type, ext = processed["bytes"], processed["mime"], processed["ext"]
            width, height, thumb_bytes = processed["width"], processed["height"], processed["thumb_bytes"]
        else:
            stored, content_type, ext = data, mimetype, None
        new_path = media_service.generate_storage_path(g.user["id"], kind, name, ext if kind == "photo" else None, unique)
        storage_service.upload_bytes(new_path, stored, content_type)
        new_thumb = None
        if thumb_bytes:
            new_thumb = media_service.thumb_storage_path(g.user["id"], unique)
            storage_service.upload_bytes(new_thumb, thumb_bytes, "image/webp")
        try:
            with engine.begin() as conn:
                _archive_current(conn, current)
                conn.execute(update(media).where(media.c.id == media_id).values(
                    file_name=name, storage_path=new_path, thumb_path=new_thumb or thumb_path, mime_type=content_type,
                    file_size=len(stored), width=width, height=height, checksum=media_service.sha256_hex(stored),
                    orig_checksum=media_service.sha256_hex(data), updated_at=utcnow()))
        except Exception:
            storage_service.delete_objects([new_path, new_thumb])
            raise
        if new_thumb and thumb_path and thumb_path != new_thumb:
            try:
                storage_service.delete_object(thumb_path)
            except Exception:
                pass
        log_activity(g.user["id"], "file_replaced", current["file_name"])
        flash("File replaced. The previous version is saved in version history.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        current_app_log("Replace failed")
        flash("The file could not be replaced.", "error")
    return redirect(url_for("media.versions", media_id=media_id))


def current_app_log(message: str) -> None:
    import logging
    logging.getLogger(__name__).exception(message)


@bp.route("/media/<media_id>/versions/<version_id>/restore", methods=["POST"])
@login_required
def restore_version(media_id, version_id):
    current = owned_media(media_id)
    with engine.begin() as conn:
        version = conn.execute(select(file_versions).where(file_versions.c.id == version_id, file_versions.c.media_id == media_id,
                                                           file_versions.c.user_id == g.user["id"])).mappings().first()
        if not version:
            flash("That version is no longer available.", "error")
            return redirect(url_for("media.versions", media_id=media_id))
        _archive_current(conn, current)
        conn.execute(update(media).where(media.c.id == media_id).values(
            storage_path=version["storage_path"], file_size=version["file_size"] or 0, file_name=version["file_name"] or current["file_name"],
            mime_type=version["mime_type"] or current["mime_type"], checksum=version["checksum"], width=version["width"],
            height=version["height"], updated_at=utcnow()))
        conn.execute(delete(file_versions).where(file_versions.c.id == version_id))
    flash("Previous version restored.", "success")
    return redirect(url_for("media.versions", media_id=media_id))


# ------------------------------------------------------------------------ sharing
def _public_url(path: str) -> str:
    return (config.PUBLIC_BASE_URL + path) if config.PUBLIC_BASE_URL else request.host_url.rstrip("/") + path


@bp.route("/media/<media_id>/share", methods=["POST"])
@login_required
def create_share(media_id):
    owned_media(media_id)
    hours = request.form.get("expires_hours", type=int)
    hours = hours if hours in {1, 24, 168, 720} else 24
    token = secrets.token_urlsafe(32)
    with engine.begin() as conn:
        conn.execute(insert(share_links).values(id=new_id(), token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=g.user["id"],
                                               media_id=media_id, expires_at=utcnow() + timedelta(hours=hours), created_at=utcnow()))
    log_activity(g.user["id"], "share_created", f"Read-only link, expires in {hours} hour(s)")
    session["new_share_link"] = _public_url(url_for("media.shared_view", token=token))
    flash("Share link created. Copy it from the banner below.", "success")
    return redirect(url_for("media.shares"))


@bp.route("/shares")
@login_required
def shares():
    with engine.connect() as conn:
        rows = conn.execute(select(share_links.c.id, share_links.c.expires_at, share_links.c.revoked_at, share_links.c.created_at,
                                   media.c.file_name, media.c.media_type, media.c.is_deleted).join(media, share_links.c.media_id == media.c.id)
                            .where(share_links.c.user_id == g.user["id"]).order_by(share_links.c.created_at.desc()).limit(300)).mappings().all()
    now = utcnow()
    links = []
    for row in rows:
        item = dict(row)
        item["status"] = "revoked" if item["revoked_at"] else ("expired" if aware(item["expires_at"]) <= now else
                                                              ("in trash" if item["is_deleted"] else "active"))
        links.append(item)
    return render_template("media/shares.html", links=links, new_link=session.pop("new_share_link", None),
                           back_url=url_for("main.hub", slug="sharing"))


@bp.route("/shares/<share_id>/revoke", methods=["POST"])
@login_required
def revoke_share(share_id):
    with engine.begin() as conn:
        conn.execute(update(share_links).where(share_links.c.id == share_id, share_links.c.user_id == g.user["id"],
                                               share_links.c.revoked_at.is_(None)).values(revoked_at=utcnow()))
    flash("Link revoked.", "success")
    return redirect(url_for("media.shares"))


def _shared_row(token: str):
    digest = hashlib.sha256(token.encode()).hexdigest()
    with engine.connect() as conn:
        row = conn.execute(select(media).join(share_links, share_links.c.media_id == media.c.id).where(
            share_links.c.token_hash == digest, share_links.c.revoked_at.is_(None), share_links.c.expires_at > utcnow(),
            media.c.is_deleted.is_(False))).mappings().first()
    if not row:
        abort(404)
    return dict(row)


@bp.route("/shared/<token>")
def shared_view(token):
    row = _shared_row(token)
    return render_template("media/shared.html", item=row, file_url=url_for("media.shared_file", token=token))


@bp.route("/shared/<token>/file")
def shared_file(token):
    return _stream(_shared_row(token), download=bool(request.args.get("download")))


# ------------------------------------------------------------------------- export
@bp.route("/download-all")
@login_required
def download_all():
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(media).where(
            media.c.user_id == g.user["id"], media.c.is_deleted.is_(False)).order_by(media.c.created_at.desc()).limit(400)).all()]
    if not rows:
        flash("There are no files to export yet.", "info")
        return redirect(url_for("main.hub", slug="storage"))
    cap = 450 * 1024 * 1024
    output = SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    added = skipped = total = 0
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
            for item in rows:
                if total + int(item.get("file_size") or 0) > cap:
                    skipped += 1
                    continue
                try:
                    data = storage_service.read_bytes(item["storage_path"], max_bytes=cap - total)
                except Exception:
                    skipped += 1
                    continue
                folder = classifier.CATEGORY_LABEL[classifier.normalize_category(item["category"])].replace("&", "and")
                name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(item["file_name"]))[:140] or "file"
                archive.writestr(f"{folder}/{item['id'][:8]}_{name}", data)
                total += len(data)
                added += 1
        if not added:
            output.close()
            flash("Files could not be packaged. Please try again.", "error")
            return redirect(url_for("main.hub", slug="storage"))
        log_activity(g.user["id"], "download_all", f"Exported {added} file(s); skipped {skipped}")
        output.seek(0)
        response = send_file(output, mimetype="application/zip", as_attachment=True, download_name="digital-archive.zip")
        response.call_on_close(output.close)
        return response
    except Exception:
        output.close()
        current_app_log("ZIP export failed")
        flash("The export could not be completed.", "error")
        return redirect(url_for("main.hub", slug="storage"))
