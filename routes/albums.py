"""Albums."""
from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from routes.common import login_required, media_view
from services import album_service, storage_service
from services.events import log_activity

bp = Blueprint("albums", __name__, url_prefix="/albums")


def _album_or_404(album_id):
    album = album_service.get_album(album_id, g.user["id"])
    if not album:
        abort(404)
    return album


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        if not name or len(name) > 100:
            flash("Give the album a name (up to 100 characters).", "error")
        elif len(description) > 500:
            flash("Description must be 500 characters or less.", "error")
        else:
            album_id = album_service.create_album(g.user["id"], name, description)
            log_activity(g.user["id"], "album_created", name)
            flash("Album created. Add some memories to it.", "success")
            return redirect(url_for("albums.detail", album_id=album_id))
        return redirect(url_for("albums.index"))
    items = album_service.list_albums(g.user["id"])
    for album in items:
        album["cover_url"] = storage_service.delivery_url((album["cover"] or {}).get("thumb_path") or (album["cover"] or {}).get("storage_path")) \
            if album.get("cover") else None
    return render_template("albums/index.html", albums=items, back_url=url_for("main.hub", slug="library"))


@bp.route("/<album_id>")
@login_required
def detail(album_id):
    album = _album_or_404(album_id)
    items = [media_view(m) for m in album_service.album_media_items(album_id, g.user["id"])]
    return render_template("albums/detail.html", album=album, items=items, back_url=url_for("albums.index"))


@bp.route("/<album_id>/edit", methods=["GET", "POST"])
@login_required
def edit(album_id):
    album = _album_or_404(album_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 100:
            flash("Album name is required (up to 100 characters).", "error")
        else:
            album_service.update_album(album_id, g.user["id"], name, request.form.get("description", "").strip()[:500])
            flash("Album updated.", "success")
            return redirect(url_for("albums.detail", album_id=album_id))
    return render_template("albums/edit.html", album=album, back_url=url_for("albums.detail", album_id=album_id))


@bp.route("/<album_id>/delete", methods=["POST"])
@login_required
def delete(album_id):
    _album_or_404(album_id)
    album_service.delete_album(album_id, g.user["id"])
    flash("Album deleted. Your files are untouched.", "success")
    return redirect(url_for("albums.index"))


@bp.route("/<album_id>/add", methods=["GET", "POST"])
@login_required
def add(album_id):
    album = _album_or_404(album_id)
    if request.method == "POST":
        ids = request.form.getlist("media_ids")
        if not ids:
            flash("Select at least one memory.", "error")
            return redirect(url_for("albums.add", album_id=album_id))
        added = album_service.add_media(album_id, g.user["id"], ids)
        flash(f"{added} memor{'y' if added == 1 else 'ies'} added.", "success")
        return redirect(url_for("albums.detail", album_id=album_id))
    items = [media_view(m) for m in album_service.available_media(album_id, g.user["id"])]
    return render_template("albums/add.html", album=album, items=items, back_url=url_for("albums.detail", album_id=album_id))


@bp.route("/<album_id>/remove", methods=["POST"])
@login_required
def remove(album_id):
    _album_or_404(album_id)
    album_service.remove_media(album_id, g.user["id"], request.form.get("media_id", ""))
    flash("Removed from the album.", "success")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<album_id>/cover", methods=["POST"])
@login_required
def cover(album_id):
    _album_or_404(album_id)
    album_service.set_cover(album_id, g.user["id"], request.form.get("media_id", ""))
    flash("Album cover updated.", "success")
    return redirect(url_for("albums.detail", album_id=album_id))
