"""Profile, settings (account / security / privacy / data), activity and theme."""
from __future__ import annotations

import io
import time
import uuid

from flask import Blueprint, abort, flash, g, jsonify, make_response, redirect, render_template, request, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select, update

from database import activity_logs, engine, login_history, media, profiles, users, utcnow, albums
from routes.common import login_required
from services import account_service, auth_service, otp_service, security, stats_service, storage_service
from services.events import client_ip, log_event

bp = Blueprint("account", __name__)

SETTINGS_ITEMS = [
    ("Account details", "Username, e-mail, sign-up date, sign-in records", "account.account_details", "user"),
    ("Security", "Password, e-mail, security question, two-step verification", "account.security_page", "shield"),
    ("Privacy & AI", "Control AI picture analysis", "account.privacy", "eye"),
    ("Your data", "Export your information or delete your account", "account.data", "database"),
]


@bp.route("/api/theme", methods=["POST"])
def set_theme():
    theme = (request.get_json(silent=True) or {}).get("theme") or request.form.get("theme")
    if theme not in {"light", "dark"}:
        return jsonify(error="bad theme"), 400
    if g.user:
        with engine.begin() as conn:
            conn.execute(update(users).where(users.c.id == g.user["id"]).values(theme=theme))
    response = make_response(jsonify(theme=theme))
    response.set_cookie("da_theme", theme, max_age=365 * 86400, samesite="Lax", secure=request.is_secure)
    return response


# ------------------------------------------------------------------------ profile
@bp.route("/profile")
@login_required
def profile():
    counts = stats_service.library_counts(g.user["id"])
    with engine.connect() as conn:
        album_count = conn.execute(select(albums.c.id).where(albums.c.user_id == g.user["id"])).all()
    return render_template("account/profile.html", counts=counts, album_count=len(album_count),
                           avatar=storage_service.delivery_url(g.user.get("avatar_url")), back_url=url_for("main.hub", slug="account"))


@bp.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    if request.method == "POST":
        display = request.form.get("display_name", "").strip()[:100] or g.user["username"]
        bio = request.form.get("bio", "").strip()[:300]
        username = request.form.get("username", "").strip()
        try:
            if username and username.lower() != g.user["username"].lower():
                auth_service.set_username(g.user["id"], username)
            with engine.begin() as conn:
                conn.execute(update(profiles).where(profiles.c.id == g.user["id"]).values(display_name=display, bio=bio or None, updated_at=utcnow()))
            flash("Profile updated.", "success")
            return redirect(url_for("account.profile"))
        except auth_service.SignupError as exc:
            flash(str(exc), "error")
    return render_template("account/edit_profile.html", back_url=url_for("account.profile"),
                           avatar=storage_service.delivery_url(g.user.get("avatar_url")))


@bp.route("/profile/avatar", methods=["POST"])
@login_required
def upload_avatar():
    storage = request.files.get("avatar")
    if not storage or not storage.filename:
        flash("Choose a picture first.", "error")
        return redirect(url_for("account.edit_profile"))
    data = storage.read()
    if len(data) > 8 * 1024 * 1024:
        flash("Picture must be smaller than 8 MB.", "error")
        return redirect(url_for("account.edit_profile"))
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"), (512, 512), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=84, method=4)
        old = g.user.get("avatar_url")
        path = f"{g.user['id']}/avatars/{uuid.uuid4().hex}.webp"
        storage_service.upload_bytes(path, buffer.getvalue(), "image/webp")
        with engine.begin() as conn:
            conn.execute(update(profiles).where(profiles.c.id == g.user["id"]).values(avatar_url=path, updated_at=utcnow()))
        if old:
            try:
                storage_service.delete_object(old)
            except Exception:
                pass
        flash("Profile picture updated.", "success")
    except (UnidentifiedImageError, OSError):
        flash("That file is not a valid picture.", "error")
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Avatar upload failed")
        flash("Could not upload the picture.", "error")
    return redirect(url_for("account.edit_profile"))


# ----------------------------------------------------------------------- settings
@bp.route("/settings")
@login_required
def settings():
    items = [{"label": l, "desc": d, "url": url_for(e), "icon": i} for l, d, e, i in SETTINGS_ITEMS]
    hub = {"title": "Settings", "icon": "settings", "tone": "amber", "blurb": "Manage your account, security, privacy and data."}
    return render_template("hub.html", hub=hub, items=items, back_url=url_for("main.hub", slug="account"))


@bp.route("/settings/account")
@login_required
def account_details():
    with engine.connect() as conn:
        logins = [dict(r._mapping) for r in conn.execute(select(login_history).where(login_history.c.user_id == g.user["id"])
                                                         .order_by(login_history.c.created_at.desc()).limit(15)).all()]
    return render_template("account/details.html", logins=logins, back_url=url_for("account.settings"))


@bp.route("/settings/security")
@login_required
def security_page():
    return render_template("account/security.html", questions=security.SECURITY_QUESTIONS, back_url=url_for("account.settings"))


def _confirm_password() -> bool:
    if not security.verify_password(g.user["password_hash"], request.form.get("current_password", "")):
        flash("Your current password is not correct.", "error")
        return False
    return True


@bp.route("/settings/security/password", methods=["POST"])
@login_required
def change_password():
    if not _confirm_password():
        return redirect(url_for("account.security_page"))
    new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
    if new != confirm:
        flash("The two new passwords do not match.", "error")
        return redirect(url_for("account.security_page"))
    try:
        auth_service.set_password(g.user["id"], new)
    except auth_service.SignupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("account.security_page"))
    fresh = auth_service.get_user(g.user["id"])
    session["sv"] = fresh["session_version"]            # keep this device signed in, sign out all others
    flash("Password changed. Other devices were signed out.", "success")
    return redirect(url_for("account.security_page"))


@bp.route("/settings/security/question", methods=["POST"])
@login_required
def change_question():
    if not _confirm_password():
        return redirect(url_for("account.security_page"))
    question = request.form.get("custom_question", "").strip() if request.form.get("question") == "__custom__" else request.form.get("question", "")
    try:
        auth_service.set_security_qa(g.user["id"], question, request.form.get("answer", ""))
        flash("Security question saved.", "success")
    except auth_service.SignupError as exc:
        flash(str(exc), "error")
    return redirect(url_for("account.security_page"))


@bp.route("/settings/security/email", methods=["POST"])
@login_required
def change_email():
    if not _confirm_password():
        return redirect(url_for("account.security_page"))
    new_email = request.form.get("new_email", "").strip().lower()
    if not security.EMAIL_RE.fullmatch(new_email):
        flash("Enter a valid e-mail address.", "error")
        return redirect(url_for("account.security_page"))
    result = otp_service.issue_otp("email_change", new_email, g.user["id"], client_ip())
    from routes.auth import _otp_feedback
    if not _otp_feedback(result) and result["status"] != "cooldown":
        return redirect(url_for("account.security_page"))
    session["ec"] = {"email": new_email, "ts": time.time()}
    return redirect(url_for("account.email_verify"))


@bp.route("/settings/security/email/verify", methods=["GET", "POST"])
@login_required
def email_verify():
    flow = session.get("ec")
    if not flow or time.time() - flow["ts"] > 900:
        session.pop("ec", None)
        return redirect(url_for("account.security_page"))
    if request.method == "POST":
        from routes.auth import _otp_feedback
        if request.form.get("action") == "resend":
            _otp_feedback(otp_service.issue_otp("email_change", flow["email"], g.user["id"], client_ip()))
            return redirect(url_for("account.email_verify"))
        outcome = otp_service.verify_otp("email_change", flow["email"], request.form.get("code", ""))
        if outcome == "ok":
            auth_service.set_email(g.user["id"], flow["email"])
            session.pop("ec", None)
            flash("E-mail address updated.", "success")
            return redirect(url_for("account.security_page"))
        flash({"invalid": "That code is not correct.", "expired": "That code has expired.", "too_many": "Too many wrong codes."}.get(outcome, "No active code."), "error")
        return redirect(url_for("account.email_verify"))
    return render_template("auth/verify_code.html", title="Confirm your new e-mail", subtitle=f"Enter the code sent to {security.mask_email(flow['email'])}",
                           wait=otp_service.seconds_until_resend("email_change", flow["email"]), back_url=url_for("account.security_page"))


@bp.route("/settings/security/two-step", methods=["POST"])
@login_required
def two_step():
    action = request.form.get("action")
    if action == "enable":
        from routes.auth import _otp_feedback
        result = otp_service.issue_otp("enable_2fa", g.user["email"], g.user["id"], client_ip())
        if not _otp_feedback(result) and result["status"] != "cooldown":
            return redirect(url_for("account.security_page"))
        session["e2"] = {"ts": time.time()}
        return redirect(url_for("account.two_step_verify"))
    if action == "disable":
        if not _confirm_password():
            return redirect(url_for("account.security_page"))
        with engine.begin() as conn:
            conn.execute(update(users).where(users.c.id == g.user["id"]).values(two_step_enabled=False))
        log_event("two_step_disabled", "Two-step verification turned off", user_id=g.user["id"], username=g.user["username"], email=g.user["email"])
        flash("Two-step verification is off.", "success")
    return redirect(url_for("account.security_page"))


@bp.route("/settings/security/two-step/verify", methods=["GET", "POST"])
@login_required
def two_step_verify():
    flow = session.get("e2")
    if not flow or time.time() - flow["ts"] > 900:
        session.pop("e2", None)
        return redirect(url_for("account.security_page"))
    if request.method == "POST":
        from routes.auth import _otp_feedback
        if request.form.get("action") == "resend":
            _otp_feedback(otp_service.issue_otp("enable_2fa", g.user["email"], g.user["id"], client_ip()))
            return redirect(url_for("account.two_step_verify"))
        outcome = otp_service.verify_otp("enable_2fa", g.user["email"], request.form.get("code", ""))
        if outcome == "ok":
            with engine.begin() as conn:
                conn.execute(update(users).where(users.c.id == g.user["id"]).values(two_step_enabled=True))
            session.pop("e2", None)
            log_event("two_step_enabled", "Two-step verification turned on", user_id=g.user["id"], username=g.user["username"], email=g.user["email"])
            flash("Two-step verification is on. A code will be e-mailed at every sign-in.", "success")
            return redirect(url_for("account.security_page"))
        flash({"invalid": "That code is not correct.", "expired": "That code has expired.", "too_many": "Too many wrong codes."}.get(outcome, "No active code."), "error")
        return redirect(url_for("account.two_step_verify"))
    return render_template("auth/verify_code.html", title="Turn on two-step verification",
                           subtitle=f"Enter the code sent to {security.mask_email(g.user['email'])}",
                           wait=otp_service.seconds_until_resend("enable_2fa", g.user["email"]), back_url=url_for("account.security_page"))


@bp.route("/settings/privacy", methods=["GET", "POST"])
@login_required
def privacy():
    if request.method == "POST":
        enabled = request.form.get("ai_enabled") == "1"
        with engine.begin() as conn:
            conn.execute(update(users).where(users.c.id == g.user["id"]).values(ai_enabled=enabled))
        flash("AI picture analysis is now " + ("on." if enabled else "off. New uploads are sorted with on-server models only."), "success")
        return redirect(url_for("account.privacy"))
    from services import ai_service
    return render_template("account/privacy.html", vision=ai_service.vision_available(), back_url=url_for("account.settings"))


@bp.route("/settings/data")
@login_required
def data():
    return render_template("account/data.html", back_url=url_for("account.settings"))


@bp.route("/settings/data/export")
@login_required
def export_json():
    import json
    uid = g.user["id"]
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(media).where(media.c.user_id == uid)).all()]
        logs = [dict(r._mapping) for r in conn.execute(select(activity_logs).where(activity_logs.c.user_id == uid).limit(1000)).all()]
    keep = ("id", "file_name", "media_type", "category", "file_size", "description", "tags", "created_at", "is_deleted", "ai_caption", "ai_labels")
    payload = {"exported_at": utcnow().isoformat(), "account": {"username": g.user["username"], "email": g.user["email"],
               "created_at": g.user["created_at"].isoformat()}, "media": [{k: r.get(k) for k in keep} for r in rows],
               "activity": logs, "note": "Metadata only. Use Download archive for the files."}
    response = make_response(json.dumps(payload, default=str, indent=2))
    response.headers["Content-Type"] = "application/json"
    response.headers["Content-Disposition"] = 'attachment; filename="digital-archive-data.json"'
    return response


@bp.route("/settings/data/delete", methods=["POST"])
@login_required
def delete_account():
    if request.form.get("confirm", "").strip() != "DELETE":
        flash("Type DELETE exactly to confirm.", "error")
        return redirect(url_for("account.data"))
    if not _confirm_password():
        return redirect(url_for("account.data"))
    try:
        account_service.delete_account(g.user["id"], "the user")
    except account_service.AccountDeleteError as exc:
        flash(str(exc), "error")
        return redirect(url_for("account.data"))
    session.clear()
    flash("Your account and all stored files were deleted.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/activity")
@login_required
def activity():
    with engine.connect() as conn:
        logs = [dict(r._mapping) for r in conn.execute(select(activity_logs).where(activity_logs.c.user_id == g.user["id"])
                                                       .order_by(activity_logs.c.created_at.desc()).limit(200)).all()]
    return render_template("account/activity.html", logs=logs, back_url=url_for("main.hub", slug="account"))
