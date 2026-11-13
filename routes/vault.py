"""Private Vault: encrypted storage unlocked with a separate passphrase."""
from __future__ import annotations

import io
import re

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.datastructures import FileStorage

from routes.common import login_required, owned_media
from services import security, stats_service, storage_service, trash_service, upload_service, vault_service
from services.events import log_activity
from services.vault_service import VaultError

bp = Blueprint("vault", __name__, url_prefix="/vault")
INLINE_OK = {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}


def _key():
    return vault_service.key_for(g.user["id"], session.get("vault_token"))


def _need_key():
    key = _key()
    if not key:
        flash("Unlock your vault first.", "info")
        abort(redirect(url_for("vault.index")))
    return key


def _guard(action: str) -> int:
    return security.blocked_seconds(f"vault:{action}:{g.user['id']}")


def _hit(action: str) -> None:
    security.register_hit(f"vault:{action}:{g.user['id']}", 5, 900, 900)


@bp.route("/")
@login_required
def index():
    back = url_for("main.dashboard")
    if not vault_service.is_setup(g.user):
        return render_template("vault/setup.html", back_url=back)
    if not _key():
        return render_template("vault/unlock.html", back_url=back, wait=_guard("unlock"))
    items = vault_service.list_items(g.user["id"])
    return render_template("vault/index.html", items=items, used=sum(i["file_size"] for i in items),
                           ttl=vault_service.UNLOCK_TTL // 60, back_url=back)


@bp.route("/setup", methods=["POST"])
@login_required
def setup():
    p1, p2 = request.form.get("passphrase", ""), request.form.get("confirm", "")
    if p1 != p2:
        flash("Passphrases do not match.", "error")
        return redirect(url_for("vault.index"))
    try:
        recovery = vault_service.setup(g.user["id"], p1)
    except VaultError as exc:
        flash(str(exc), "error")
        return redirect(url_for("vault.index"))
    session["vault_token"] = vault_service.unlock(g.user["id"], p1)
    log_activity(g.user["id"], "vault_created", "Private Vault created")
    return render_template("vault/recovery.html", recovery=recovery, back_url=url_for("vault.index"))


@bp.route("/unlock", methods=["POST"])
@login_required
def unlock():
    wait = _guard("unlock")
    if wait:
        flash(f"Too many wrong attempts. Try again in {wait // 60 + 1} min.", "error")
        return redirect(url_for("vault.index"))
    try:
        session["vault_token"] = vault_service.unlock(g.user["id"], request.form.get("passphrase", ""))
    except VaultError as exc:
        _hit("unlock")
        log_activity(g.user["id"], "vault_failed", "Wrong vault passphrase")
        flash(str(exc), "error")
    return redirect(url_for("vault.index"))


@bp.route("/lock", methods=["POST"])
@login_required
def lock():
    vault_service.lock(session.pop("vault_token", None))
    flash("Vault locked.", "success")
    return redirect(url_for("vault.index"))


@bp.route("/upload", methods=["POST"])
@login_required
def upload():
    key = _need_key()
    ok = 0
    for f in request.files.getlist("files")[:20]:
        if not f or not f.filename:
            continue
        try:
            vault_service.add_file(g.user["id"], key, f.filename.replace("\\", "/").rsplit("/", 1)[-1], f.mimetype, f.read())
            ok += 1
        except VaultError as exc:
            flash(f"{f.filename}: {exc}", "error")
    if ok:
        flash(f"{ok} file(s) encrypted and stored.", "success")
        log_activity(g.user["id"], "vault_upload", f"{ok} file(s) added to the vault")
    return redirect(url_for("vault.index"))


@bp.route("/<item_id>/file")
@login_required
def download(item_id):
    key = _need_key()
    item = vault_service.get_item(g.user["id"], item_id)
    if not item:
        abort(404)
    try:
        data = vault_service.read_file(key, item)
    except (VaultError, Exception) as exc:
        flash(str(exc) or "Could not open this file.", "error")
        return redirect(url_for("vault.index"))
    mime = item["mime_type"] or "application/octet-stream"
    inline = mime in INLINE_OK and not request.args.get("download")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", item["file_name"])[:160] or "file"
    return Response(data, mimetype=mime if inline else "application/octet-stream", headers={
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
        "Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"})


@bp.route("/<item_id>/delete", methods=["POST"])
@login_required
def delete(item_id):
    _need_key()
    if vault_service.delete_item(g.user["id"], item_id):
        log_activity(g.user["id"], "vault_delete", "A vault file was deleted")
        flash("Deleted permanently.", "success")
    return redirect(url_for("vault.index"))


@bp.route("/move/<media_id>", methods=["POST"])
@login_required
def move_in(media_id):
    key = _need_key()
    row = owned_media(media_id)
    try:
        data = storage_service.read_bytes(row["storage_path"], max_bytes=vault_service.MAX_FILE + 1)
        if len(data) > vault_service.MAX_FILE:
            raise VaultError("This file is larger than the 20 MB vault limit.")
        vault_service.add_file(g.user["id"], key, row["file_name"], row["mime_type"], data)
    except VaultError as exc:
        flash(str(exc), "error")
        return redirect(url_for("media.detail", media_id=media_id))
    except Exception:
        flash("Could not read this file from storage. Nothing was moved.", "error")
        return redirect(url_for("media.detail", media_id=media_id))
    trash_service.purge_media_rows([dict(row)])
    log_activity(g.user["id"], "vault_move", "A file was moved into the vault")
    flash("Moved into your Private Vault. It no longer appears in your library or search.", "success")
    return redirect(url_for("vault.index"))


@bp.route("/<item_id>/restore", methods=["POST"])
@login_required
def restore(item_id):
    key = _need_key()
    item = vault_service.get_item(g.user["id"], item_id)
    if not item:
        abort(404)
    try:
        data = vault_service.read_file(key, item)
        result = upload_service.ingest(g.user, FileStorage(io.BytesIO(data), filename=item["file_name"], content_type=item["mime_type"]),
                                       used_bytes=max(0, stats_service.storage_used(g.user["id"]) - item["file_size"]))
    except VaultError as exc:
        flash(str(exc), "error")
        return redirect(url_for("vault.index"))
    if result["status"] == "ok":
        vault_service.delete_item(g.user["id"], item_id)
        log_activity(g.user["id"], "vault_restore", "A file was moved back to the library")
        flash("Moved back to your library.", "success")
    else:
        flash(result["message"], "error")
    return redirect(url_for("vault.index"))


@bp.route("/passphrase", methods=["POST"])
@login_required
def passphrase():
    try:
        if request.form.get("recovery"):
            fresh = vault_service.reset_with_recovery(g.user["id"], request.form["recovery"], request.form.get("new", ""))
            session["vault_token"] = vault_service.unlock(g.user["id"], request.form["new"])
            log_activity(g.user["id"], "vault_reset", "Vault passphrase reset with the recovery key")
            return render_template("vault/recovery.html", recovery=fresh, back_url=url_for("vault.index"))
        vault_service.change_passphrase(g.user["id"], request.form.get("current", ""), request.form.get("new", ""))
        log_activity(g.user["id"], "vault_passphrase", "Vault passphrase changed")
        flash("Vault passphrase changed.", "success")
    except VaultError as exc:
        _hit("unlock")
        flash(str(exc), "error")
    return redirect(url_for("vault.index"))


@bp.route("/erase", methods=["POST"])
@login_required
def erase():
    """Last resort when both passphrase and recovery key are lost: wipe the vault and start fresh."""
    wait = _guard("erase")
    if wait:
        flash(f"Too many wrong attempts. Try again in {wait // 60 + 1} min.", "error")
        return redirect(url_for("vault.index"))
    if request.form.get("confirm", "").strip().upper() != "ERASE" or not security.verify_password(
            g.user["password_hash"], request.form.get("account_password", "")):
        _hit("erase")
        flash("Type ERASE and enter your correct account password to continue.", "error")
        return redirect(url_for("vault.index"))
    try:
        count = vault_service.erase_vault(g.user["id"])
    except Exception:
        flash("Could not erase the vault right now. Nothing was changed - please try again.", "error")
        return redirect(url_for("vault.index"))
    session.pop("vault_token", None)
    log_activity(g.user["id"], "vault_erased", f"Vault erased ({count} file(s) deleted) after lost passphrase")
    flash(f"Vault erased ({count} file(s) deleted). Create a new passphrase to start again.", "success")
    return redirect(url_for("vault.index"))
