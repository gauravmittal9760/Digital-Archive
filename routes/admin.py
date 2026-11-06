"""Hidden admin panel.

Entry: type the ADMIN_USERNAME / ADMIN_PASSWORD from .env on the normal login page ->
gateway (Login to Admin Panel | Back to login page | Forgot password) -> panel password -> panel.
Every admin URL answers 404 until that sequence is completed.
"""
from __future__ import annotations

import csv
import io
import time
import uuid

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import delete, func, insert, select, text

import config
from database import admin_avatars, admin_login_records, engine, media, new_id, utcnow, users
from services import (account_service, admin_service, auth_service, otp_service, security, stats_service, storage_service,
                      trash_service, ai_service, mail_service)
from services.events import client_agent, client_ip, log_event

bp = Blueprint("admin", __name__, url_prefix="/admin")

GATE_ENDPOINTS = {"admin.gateway", "admin.login", "admin.exit", "admin.forgot", "admin.forgot_question", "admin.forgot_otp", "admin.reset_password"}
GATE_TTL = 15 * 60


def _record(stage: str, success: bool, details: str = "") -> None:
    with engine.begin() as conn:
        conn.execute(insert(admin_login_records).values(id=new_id(), stage=stage, success=success, details=details[:300],
                                                       ip_address=client_ip(), user_agent=client_agent(), created_at=utcnow()))


def _authed() -> bool:
    if not session.get("admin_auth"):
        return False
    if time.time() - session.get("admin_seen", 0) > config.ADMIN_IDLE_MINUTES * 60:
        for key in ("admin_auth", "admin_seen"):
            session.pop(key, None)
        return False
    session["admin_seen"] = time.time()
    return True


def _gate_open() -> bool:
    return time.time() - session.get("admin_gate", 0) < GATE_TTL


@bp.before_request
def guard():
    if not config.ADMIN_ENABLED:
        abort(404)
    if request.endpoint in GATE_ENDPOINTS:
        if not _gate_open() and not _authed():
            abort(404)
        return
    if not _authed():
        abort(404)


@bp.context_processor
def admin_context():
    urls = []
    if session.get("admin_auth") or session.get("admin_gate"):
        urls = [storage_service.delivery_url(a["storage_path"]) for a in admin_service.avatars()] if session.get("admin_auth") else []
    return {"admin_avatar_urls": [u for u in urls if u], "admin_email": admin_service.admin_email() if session.get("admin_auth") else "",
            "is_admin_page": True}


# ----------------------------------------------------------------- gateway & login
@bp.route("/gateway")
def gateway():
    return render_template("admin/gateway.html")


@bp.route("/exit", methods=["POST"])
def exit():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip() or "x"
        keys = (f"admin:panel:{ip}", "admin:panel:global")
        wait = max(security.blocked_seconds(k) for k in keys)
        if wait:
            flash(f"Too many wrong attempts. Try again in {security.human_duration(wait)}.", "error")
            return redirect(url_for("admin.login"))
        if admin_service.panel_password_ok(request.form.get("password", "")):
            for key in keys:
                security.reset_limit(key)
            session["admin_auth"] = True
            session["admin_seen"] = time.time()
            session.pop("admin_gate", None)
            _record("panel", True, "Signed in to the admin panel")
            return redirect(url_for("admin.dashboard"))
        count, blocked = security.register_hit(keys[0], config.LOGIN_MAX_ATTEMPTS, config.LOGIN_LOCK_MINUTES * 120, config.LOGIN_LOCK_MINUTES * 60)
        security.register_hit(keys[1], 20, config.LOGIN_LOCK_MINUTES * 120, config.LOGIN_LOCK_MINUTES * 60)
        _record("panel", False, "Wrong admin password")
        if blocked:
            flash(f"Locked for {config.LOGIN_LOCK_MINUTES} minutes after too many wrong passwords.", "error")
        else:
            flash(f"Wrong admin password. {max(0, config.LOGIN_MAX_ATTEMPTS - count)} attempts left.", "error")
        return redirect(url_for("admin.login"))
    return render_template("admin/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    _record("panel", True, "Signed out")
    session.clear()
    flash("Signed out of the admin panel.", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------- recovery
@bp.route("/forgot")
def forgot():
    return render_template("admin/forgot.html", has_email=bool(admin_service.admin_email()),
                           has_question=bool(config.ADMIN_SECURITY_ANSWER))


@bp.route("/forgot/question", methods=["GET", "POST"])
def forgot_question():
    if not config.ADMIN_SECURITY_ANSWER:
        abort(404)
    if request.method == "POST":
        key = f"admin:recovery:{client_ip() or 'x'}"
        wait = security.blocked_seconds(key)
        if wait:
            flash(f"Too many wrong answers. Try again in {security.human_duration(wait)}.", "error")
        elif admin_service.recovery_answer_ok(request.form.get("answer", "")):
            security.reset_limit(key)
            session["admin_reset_ok"] = time.time()
            _record("recovery", True, "Security question answered")
            return redirect(url_for("admin.reset_password"))
        else:
            security.register_hit(key, config.LOGIN_MAX_ATTEMPTS, config.LOGIN_LOCK_MINUTES * 120, config.LOGIN_LOCK_MINUTES * 60)
            _record("recovery", False, "Wrong security answer")
            flash("That answer is not correct.", "error")
        return redirect(url_for("admin.forgot_question"))
    return render_template("admin/forgot_question.html", question=config.ADMIN_SECURITY_QUESTION)


@bp.route("/forgot/otp", methods=["GET", "POST"])
def forgot_otp():
    email = admin_service.admin_email()
    if not email:
        abort(404)
    from routes.auth import _otp_feedback
    if request.method == "POST":
        if request.form.get("action") in {"send", "resend"}:
            _otp_feedback(otp_service.issue_otp("admin_reset", email, None, client_ip()))
            return redirect(url_for("admin.forgot_otp"))
        outcome = otp_service.verify_otp("admin_reset", email, request.form.get("code", ""))
        if outcome == "ok":
            session["admin_reset_ok"] = time.time()
            _record("recovery", True, "E-mail code verified")
            return redirect(url_for("admin.reset_password"))
        _record("recovery", False, "Wrong e-mail code")
        flash({"invalid": "That code is not correct.", "expired": "That code has expired.", "too_many": "Too many wrong codes."}.get(outcome, "No active code."), "error")
        return redirect(url_for("admin.forgot_otp"))
    wait = otp_service.seconds_until_resend("admin_reset", email)
    return render_template("admin/forgot_otp.html", masked=security.mask_email(email), wait=wait)


@bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if time.time() - session.get("admin_reset_ok", 0) > 600:
        abort(404)
    if request.method == "POST":
        new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
        problem = security.password_problem(new) if new == confirm else "The two passwords do not match."
        if problem:
            flash(problem, "error")
            return redirect(url_for("admin.reset_password"))
        admin_service.set_setting("panel_password_hash", security.hash_password(new))
        session.pop("admin_reset_ok", None)
        security.reset_limits_with_prefix("admin:panel")
        _record("recovery", True, "Admin panel password was reset")
        flash("Admin password updated. Sign in with the new password.", "success")
        return redirect(url_for("admin.login"))
    return render_template("admin/reset_password.html")


# ---------------------------------------------------------------------- dashboard
@bp.route("/")
def dashboard():
    return render_template("admin/dashboard.html", o=admin_service.overview(), page="dashboard")


@bp.route("/api/overview")
def api_overview():
    o = admin_service.overview()
    return jsonify({k: o[k] for k in ("users", "new_7", "new_30", "blocked", "locked", "active_24", "files", "stored_h", "trash_files", "two_step")})


# ------------------------------------------------------------------------- users
@bp.route("/users")
def users_page():
    rows = admin_service.list_users(request.args.get("q", ""), request.args.get("status", "all"), request.args.get("sort", "newest"))
    return render_template("admin/users.html", users=rows, q=request.args.get("q", ""), status=request.args.get("status", "all"),
                           sort=request.args.get("sort", "newest"), page="users")


@bp.route("/users/<user_id>")
def user_detail(user_id):
    user = auth_service.get_user(user_id)
    if not user:
        abort(404)
    from routes.common import media_view
    items = [media_view(m) for m in admin_service.user_media(user_id, 48)]
    return render_template("admin/user_detail.html", u=user, breakdown=admin_service.user_storage_breakdown(user_id),
                           events=admin_service.user_events(user_id), logins=admin_service.user_logins(user_id), items=items,
                           locked=auth_service.is_locked(user), page="users")


@bp.route("/users/<user_id>/cloudinary")
def user_cloudinary(user_id):
    result = storage_service.cloudinary_prefix_bytes(f"{user_id}/")
    if result.get("available"):
        result["bytes_h"] = stats_service.human_size(result["bytes"])
    return jsonify(result)


@bp.route("/users/<user_id>/block", methods=["POST"])
def block(user_id):
    if auth_service.get_user(user_id):
        auth_service.block_user(user_id, request.form.get("reason", "").strip())
        flash("User blocked and signed out everywhere.", "success")
    return redirect(request.form.get("return") or url_for("admin.user_detail", user_id=user_id))


@bp.route("/users/<user_id>/unblock", methods=["POST"])
def unblock(user_id):
    if auth_service.get_user(user_id):
        auth_service.unblock_user(user_id)
        flash("User unblocked. They can sign in again.", "success")
    nxt = request.form.get("return", "")
    return redirect(nxt if nxt.startswith("/admin") else url_for("admin.user_detail", user_id=user_id))


@bp.route("/users/<user_id>/delete", methods=["POST"])
def delete_user(user_id):
    user = auth_service.get_user(user_id)
    if not user:
        abort(404)
    if request.form.get("confirm", "").strip().lower() != user["username"].lower():
        flash("Type the username exactly to confirm deletion.", "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    try:
        account_service.delete_account(user_id, "the administrator")
        flash(f"Account “{user['username']}” and its files were deleted.", "success")
        return redirect(url_for("admin.users_page"))
    except account_service.AccountDeleteError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))


@bp.route("/blocked")
def blocked():
    return render_template("admin/blocked.html", rows=admin_service.blocked_users(), page="blocked")


# ----------------------------------------------------------------- events & logins
@bp.route("/events")
def events_page():
    kind, q = request.args.get("type", ""), request.args.get("q", "")
    return render_template("admin/events.html", rows=admin_service.events(kind, q), types=admin_service.event_types(), kind=kind, q=q, page="events")


@bp.route("/events.csv")
def events_csv():
    rows = admin_service.events(request.args.get("type", ""), request.args.get("q", ""), 5000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["time_utc", "event", "username", "email", "details", "ip"])
    for r in rows:
        writer.writerow([r["created_at"].isoformat(), r["event_type"], r["username"], r["email"], r["details"], r["ip_address"]])
    return Response(buffer.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="account-events.csv"'})


@bp.route("/logins")
def logins_page():
    tab = request.args.get("tab", "users")
    return render_template("admin/logins.html", tab=tab, users_rows=admin_service.all_logins() if tab == "users" else [],
                           admin_rows=admin_service.recent_admin_records() if tab == "admin" else [], page="logins")


# ---------------------------------------------------------------------- storage etc.
@bp.route("/storage")
def storage_page():
    usage = storage_service.cloudinary_usage()
    db_size = None
    try:
        with engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                db_size = stats_service.human_size(conn.execute(text("SELECT pg_database_size(current_database())")).scalar())
    except Exception:
        pass
    flat = {}
    if usage and "error" not in usage:
        storage = usage.get("storage") or {}
        bandwidth = usage.get("bandwidth") or {}
        credits_usage = usage.get("credits") or {}
        flat = {"plan": usage.get("plan"), "storage_h": stats_service.human_size(storage.get("usage") or 0),
                "storage_limit": storage.get("limit"), "bandwidth_h": stats_service.human_size(bandwidth.get("usage") or 0),
                "resources": usage.get("resources"), "credits": credits_usage.get("usage"), "credits_limit": credits_usage.get("limit"),
                "credits_pct": credits_usage.get("used_percent")}
    return render_template("admin/storage.html", o=admin_service.overview(), usage=flat, usage_error=(usage or {}).get("error"),
                           cloud=storage_service.cloudinary_ready(), db_size=db_size, pending=trash_service.pending_purge_count(),
                           retention=config.TRASH_RETENTION_DAYS, page="storage")


@bp.route("/storage/purge", methods=["POST"])
def purge_now():
    removed = trash_service.purge_expired(batch=500)
    flash(f"{removed} expired file(s) permanently deleted.", "success")
    return redirect(url_for("admin.storage_page"))


@bp.route("/media")
def media_page():
    from routes.common import media_view
    user_id = request.args.get("user", "")
    items = [media_view(m) | {"owner": m["username"]} for m in admin_service.user_media(user_id or None, 96)]
    return render_template("admin/media.html", items=items, user_id=user_id, page="media")


@bp.route("/announcements", methods=["GET", "POST"])
def announcements_page():
    if request.method == "POST":
        if request.form.get("action") == "clear":
            admin_service.clear_announcements()
            flash("Banner removed.", "success")
        elif request.form.get("message", "").strip():
            admin_service.add_announcement(request.form["message"].strip(), request.form.get("level", "info"))
            flash("Banner is now visible to every user.", "success")
        return redirect(url_for("admin.announcements_page"))
    return render_template("admin/announcements.html", rows=admin_service.list_announcements(), page="announcements")


@bp.route("/system")
def system_page():
    checks = [
        ("Database", engine.dialect.name, True), ("Cloudinary storage", "connected" if storage_service.cloudinary_ready() else "not configured", storage_service.cloudinary_ready()),
        ("E-mail (Brevo)", "configured" if mail_service.mail_configured() else "not configured", mail_service.mail_configured()),
        ("Vision AI (Gemini)", "on" if ai_service.vision_available() else "off (local models only)", ai_service.vision_available()),
        ("Local OCR", "on" if ai_service.ocr_available() else "off", ai_service.ocr_available()),
        ("Video compression", "ffmpeg" if __import__("services.media_service", fromlist=["x"]).ffmpeg_path() else "Cloudinary (incoming transformation)", True),
        ("Trash purge secret (CRON_SECRET)", "set" if config.CRON_SECRET else "not set", bool(config.CRON_SECRET)),
        ("Environment", config.APP_ENV, True),
    ]
    from sqlalchemy import select as _sel
    from database import app_state
    with engine.connect() as conn:
        last = conn.execute(_sel(app_state.c.value).where(app_state.c.key == "last_purge")).scalar()
    return render_template("admin/system.html", checks=checks, last_purge=last or None, page="system")


# ------------------------------------------------------------------------ profile
@bp.route("/profile")
def profile():
    return render_template("admin/profile.html", avatars=[{**a, "url": storage_service.delivery_url(a["storage_path"])} for a in admin_service.avatars()],
                           records=admin_service.recent_admin_records(12), page="profile")


@bp.route("/profile/avatar", methods=["POST"])
def avatar_upload():
    if len(admin_service.avatars()) >= 8:
        flash("You can keep up to 8 pictures. Delete one first.", "error")
        return redirect(url_for("admin.profile"))
    storage = request.files.get("avatar")
    try:
        data = storage.read() if storage else b""
        if not data or len(data) > 8 * 1024 * 1024:
            raise ValueError("Choose a picture smaller than 8 MB.")
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"), (512, 512), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=84, method=4)
        path = f"admin/avatars/{uuid.uuid4().hex}.webp"
        storage_service.upload_bytes(path, buffer.getvalue(), "image/webp")
        with engine.begin() as conn:
            conn.execute(insert(admin_avatars).values(id=new_id(), storage_path=path, file_size=len(buffer.getvalue()), position=int(time.time()), created_at=utcnow()))
        flash("Picture added to the rotation.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except (UnidentifiedImageError, OSError):
        flash("That file is not a valid picture.", "error")
    return redirect(url_for("admin.profile"))


@bp.route("/profile/avatar/<avatar_id>/delete", methods=["POST"])
def avatar_delete(avatar_id):
    with engine.connect() as conn:
        row = conn.execute(select(admin_avatars.c.storage_path).where(admin_avatars.c.id == avatar_id)).first()
    if row:
        try:
            storage_service.delete_object(row[0])
        except Exception:
            pass
        with engine.begin() as conn:
            conn.execute(delete(admin_avatars).where(admin_avatars.c.id == avatar_id))
    return redirect(url_for("admin.profile"))


@bp.route("/profile/email", methods=["POST"])
def change_email():
    if not admin_service.panel_password_ok(request.form.get("password", "")):
        flash("Admin password is not correct.", "error")
    else:
        email = request.form.get("email", "").strip().lower()
        if not security.EMAIL_RE.fullmatch(email):
            flash("Enter a valid e-mail address.", "error")
        else:
            admin_service.set_setting("admin_email", email)
            _record("security", True, "Admin e-mail changed")
            flash("Admin e-mail updated.", "success")
    return redirect(url_for("admin.profile"))


@bp.route("/profile/password", methods=["POST"])
def change_password():
    which = request.form.get("which", "panel")
    if not admin_service.panel_password_ok(request.form.get("password", "")):
        flash("Current admin password is not correct.", "error")
        return redirect(url_for("admin.profile"))
    new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
    problem = security.password_problem(new) if new == confirm else "The two passwords do not match."
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.profile"))
    admin_service.set_setting("gate_password_hash" if which == "gate" else "panel_password_hash", security.hash_password(new))
    _record("security", True, f"Admin {which} password changed")
    flash("Gateway password updated." if which == "gate" else "Admin password updated.", "success")
    return redirect(url_for("admin.profile"))
