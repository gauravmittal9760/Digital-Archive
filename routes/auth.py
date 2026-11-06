"""Sign-up, login (username + password, optional 2-step), and every recovery flow."""
from __future__ import annotations

import secrets
import time

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import insert

import config
from database import admin_login_records, engine, new_id, utcnow
from routes.common import safe_next
from services import admin_service, auth_service, otp_service, security
from services.events import client_agent, client_ip, log_event

bp = Blueprint("auth", __name__)
FLOW_TTL = 15 * 60


# ------------------------------------------------------------------------- helpers
def _start_session(user: dict) -> None:
    theme = request.cookies.get("da_theme")
    session.clear()
    session["uid"], session["sv"] = user["id"], user["session_version"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


def _flow(name: str) -> dict | None:
    data = session.get(name)
    if not data or time.time() - data.get("ts", 0) > FLOW_TTL:
        session.pop(name, None)
        return None
    return data


def _otp_feedback(result: dict) -> bool:
    """Flash a message for an issue_otp result; True when a code was sent."""
    status = result["status"]
    if status == "ok":
        flash(f"A 6-digit code has been sent. It is valid for {config.OTP_TTL_MINUTES} minutes.", "success")
        if result.get("dev_code"):
            flash(f"Development mode (no Brevo key): your code is {result['dev_code']}", "info")
        return True
    if status == "cooldown":
        flash(f"Please wait {result['wait']} seconds before requesting another code.", "error")
    elif status == "limited":
        flash(f"Too many code requests. Try again in {security.human_duration(result['wait'])}.", "error")
    else:
        flash("We could not send the e-mail right now. Please try again shortly.", "error")
    return False


def _admin_record(stage: str, success: bool, details: str = "") -> None:
    with engine.begin() as conn:
        conn.execute(insert(admin_login_records).values(id=new_id(), stage=stage, success=success, details=details[:300],
                                                       ip_address=client_ip(), user_agent=client_agent(), created_at=utcnow()))


# --------------------------------------------------------------------------- signup
@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("main.dashboard"))
    form = {}
    if request.method == "POST":
        form = request.form
        question = form.get("custom_question", "").strip() if form.get("question") == "__custom__" else form.get("question", "")
        if form.get("password") != form.get("confirm_password"):
            flash("The two passwords do not match.", "error")
        else:
            try:
                auth_service.create_user(form.get("username", ""), form.get("email", ""), form.get("password", ""),
                                         form.get("display_name", ""), question, form.get("answer", ""))
                flash("Account created. Sign in with your username and password.", "success")
                return redirect(url_for("auth.login"))
            except auth_service.SignupError as exc:
                flash(str(exc), "error")
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Signup failed")
                flash("We could not create your account right now. Please try again.", "error")
    return render_template("auth/signup.html", questions=security.SECURITY_QUESTIONS, form=form)


# ---------------------------------------------------------------------------- login
@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("main.dashboard"))
    if session.pop("flash_blocked", None):
        flash("Your account has been blocked by the administrator.", "error")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Enter your username and password.", "error")
            return redirect(url_for("auth.login", next=request.args.get("next")))

        # Private door to the admin area: the special credentials from .env open the gateway.
        if admin_service.gate_ok(username, password):
            if security.blocked_seconds("admin:gate:" + (client_ip() or "x")):
                flash("Too many attempts. Please try again later.", "error")
                return redirect(url_for("auth.login"))
            session.clear()
            session["admin_gate"] = time.time()
            session["csrf_token"] = secrets.token_urlsafe(32)
            _admin_record("gateway", True, "Gateway credentials accepted")
            return redirect(url_for("admin.gateway"))

        result = auth_service.authenticate(username, password)
        status = result["status"]
        if status == "ok":
            user = result["user"]
            next_url = safe_next(request.args.get("next"))
            if user["two_step_enabled"]:
                sent = otp_service.issue_otp("login_2fa", user["email"], user["id"], client_ip())
                session.clear()                       # clear first, otherwise the flash messages below would be wiped
                session["csrf_token"] = secrets.token_urlsafe(32)
                if not _otp_feedback(sent) and sent["status"] != "cooldown":
                    return redirect(url_for("auth.login"))
                session["pending_2fa"] = {"uid": user["id"], "next": next_url, "ts": time.time()}
                return redirect(url_for("auth.verify_login"))
            _start_session(user)
            return redirect(next_url)
        if status == "locked":
            flash(f"Too many wrong attempts. Try again in {security.human_duration(result['wait'])}.", "error")
        elif status == "blocked":
            flash("Your account has been blocked by the administrator." + (f" Reason: {result['reason']}" if result.get("reason") else ""), "error")
        else:
            left = result.get("attempts_left", 0)
            flash(f"Invalid username or password. {left} attempt{'s' if left != 1 else ''} left before a temporary lock.", "error")
        return redirect(url_for("auth.login", next=request.args.get("next")))
    return render_template("auth/login.html")


@bp.route("/login/verify", methods=["GET", "POST"])
def verify_login():
    pending = _flow("pending_2fa")
    if not pending:
        flash("Your verification session expired. Please sign in again.", "error")
        return redirect(url_for("auth.login"))
    user = auth_service.get_user(pending["uid"])
    if not user:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if request.form.get("action") == "resend":
            _otp_feedback(otp_service.issue_otp("login_2fa", user["email"], user["id"], client_ip()))
            return redirect(url_for("auth.verify_login"))
        outcome = otp_service.verify_otp("login_2fa", user["email"], request.form.get("code", ""))
        if outcome == "ok":
            _start_session(user)
            log_event("login_2fa", "Signed in with two-step verification", user_id=user["id"], username=user["username"],
                      email=user["email"], user_visible=False)
            return redirect(pending["next"])
        messages = {"invalid": "That code is not correct.", "expired": "That code has expired. Request a new one.",
                    "none": "No active code. Request a new one.", "too_many": "Too many wrong codes. Request a new one."}
        flash(messages.get(outcome, "Invalid code."), "error")
        return redirect(url_for("auth.verify_login"))
    return render_template("auth/verify_code.html", title="Two-step verification",
                           subtitle=f"Enter the 6-digit code sent to {security.mask_email(user['email'])}",
                           wait=otp_service.seconds_until_resend("login_2fa", user["email"]), back_url=url_for("auth.login"))


@bp.route("/logout", methods=["POST"])
def logout():
    theme = None
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------- recovery hub
@bp.route("/forgot")
def forgot():
    return render_template("auth/forgot_hub.html")


# ------------------------------------------------------------- forgot username
@bp.route("/forgot/username", methods=["GET", "POST"])
def forgot_username():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not security.EMAIL_RE.fullmatch(email):
            flash("Enter a valid e-mail address.", "error")
            return redirect(url_for("auth.forgot_username"))
        # Same answer whether or not accounts exist, so nobody can probe which e-mails are registered.
        if auth_service.usernames_for_email(email):
            result = otp_service.issue_otp("forgot_username", email, None, client_ip())
            if not _otp_feedback(result) and result["status"] != "cooldown":
                return redirect(url_for("auth.forgot_username"))
        else:
            flash(f"If this e-mail has accounts, a 6-digit code has been sent. It is valid for {config.OTP_TTL_MINUTES} minutes.", "success")
        session["fu"] = {"email": email, "ts": time.time()}
        return redirect(url_for("auth.forgot_username_verify"))
    return render_template("auth/forgot_username.html")


@bp.route("/forgot/username/verify", methods=["GET", "POST"])
def forgot_username_verify():
    flow = _flow("fu")
    if not flow:
        return redirect(url_for("auth.forgot_username"))
    if request.method == "POST":
        if request.form.get("action") == "resend":
            if auth_service.usernames_for_email(flow["email"]):
                _otp_feedback(otp_service.issue_otp("forgot_username", flow["email"], None, client_ip()))
            return redirect(url_for("auth.forgot_username_verify"))
        outcome = otp_service.verify_otp("forgot_username", flow["email"], request.form.get("code", ""))
        if outcome == "ok":
            usernames = auth_service.usernames_for_email(flow["email"])
            session.pop("fu", None)
            return render_template("auth/username_result.html", usernames=usernames, email=security.mask_email(flow["email"]))
        flash({"invalid": "That code is not correct.", "expired": "That code has expired. Request a new one.",
               "too_many": "Too many wrong codes. Request a new one."}.get(outcome, "No active code. Request a new one."), "error")
        return redirect(url_for("auth.forgot_username_verify"))
    return render_template("auth/verify_code.html", title="Find your username",
                           subtitle=f"Enter the code sent to {security.mask_email(flow['email'])}",
                           wait=otp_service.seconds_until_resend("forgot_username", flow["email"]),
                           back_url=url_for("auth.forgot_username"))


# ------------------------------------------------------------- forgot password
@bp.route("/forgot/password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        user = auth_service.get_user_by_username(request.form.get("username", ""))
        if not user:
            flash("We could not find an account with that username.", "error")
            return redirect(url_for("auth.forgot_password"))
        session["fp"] = {"uid": user["id"], "verified": False, "ts": time.time()}
        return redirect(url_for("auth.forgot_method"))
    return render_template("auth/forgot_password.html")


@bp.route("/forgot/password/method")
def forgot_method():
    flow = _flow("fp")
    user = auth_service.get_user(flow["uid"]) if flow else None
    if not user:
        return redirect(url_for("auth.forgot_password"))
    return render_template("auth/forgot_method.html", username=user["username"], masked=security.mask_email(user["email"]),
                           has_question=bool(user["security_question"] and user["security_answer_hash"]))


@bp.route("/forgot/password/question", methods=["GET", "POST"])
def forgot_question():
    flow = _flow("fp")
    user = auth_service.get_user(flow["uid"]) if flow else None
    if not user or not user["security_question"]:
        return redirect(url_for("auth.forgot_password"))
    if request.method == "POST":
        ok, wait = auth_service.check_recovery_answer(user, request.form.get("answer", ""))
        if ok:
            flow["verified"], flow["ts"] = True, time.time()
            session["fp"] = flow
            log_event("recovery_security_question", "Security question answered correctly", user_id=user["id"],
                      username=user["username"], email=user["email"], user_visible=False)
            return redirect(url_for("auth.forgot_reset"))
        if wait:
            flash(f"Too many wrong answers. Try again in {security.human_duration(wait)}.", "error")
        else:
            flash("That answer is not correct.", "error")
        return redirect(url_for("auth.forgot_question"))
    return render_template("auth/forgot_question.html", question=user["security_question"])


@bp.route("/forgot/password/otp", methods=["GET", "POST"])
def forgot_otp():
    flow = _flow("fp")
    user = auth_service.get_user(flow["uid"]) if flow else None
    if not user:
        return redirect(url_for("auth.forgot_password"))
    if request.method == "POST":
        action = request.form.get("action")
        if action in {"send", "resend"}:
            _otp_feedback(otp_service.issue_otp("reset_password", user["email"], user["id"], client_ip()))
            return redirect(url_for("auth.forgot_otp"))
        outcome = otp_service.verify_otp("reset_password", user["email"], request.form.get("code", ""))
        if outcome == "ok":
            flow["verified"], flow["ts"] = True, time.time()
            session["fp"] = flow
            return redirect(url_for("auth.forgot_reset"))
        flash({"invalid": "That code is not correct.", "expired": "That code has expired. Request a new one.",
               "too_many": "Too many wrong codes. Request a new one."}.get(outcome, "No active code. Request a new one."), "error")
        return redirect(url_for("auth.forgot_otp"))
    wait = otp_service.seconds_until_resend("reset_password", user["email"])
    return render_template("auth/verify_code.html", title="Verify your e-mail",
                           subtitle=f"We will send a code to {security.mask_email(user['email'])}" if not wait else
                           f"Enter the code sent to {security.mask_email(user['email'])}",
                           wait=wait, send_first=not wait and not _has_active_code(user["email"]),
                           back_url=url_for("auth.forgot_method"))


def _has_active_code(email: str) -> bool:
    from sqlalchemy import select
    from database import otp_codes, aware
    with engine.connect() as conn:
        row = conn.execute(select(otp_codes.c.expires_at).where(otp_codes.c.purpose == "reset_password", otp_codes.c.email == email,
                                                                otp_codes.c.used_at.is_(None)).order_by(otp_codes.c.created_at.desc()).limit(1)).first()
    return bool(row and aware(row[0]) > utcnow())


@bp.route("/forgot/password/reset", methods=["GET", "POST"])
def forgot_reset():
    flow = _flow("fp")
    if not flow or not flow.get("verified"):
        return redirect(url_for("auth.forgot_password"))
    user = auth_service.get_user(flow["uid"])
    if not user:
        return redirect(url_for("auth.forgot_password"))
    if request.method == "POST":
        new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
        if new != confirm:
            flash("The two passwords do not match.", "error")
            return redirect(url_for("auth.forgot_reset"))
        try:
            auth_service.set_password(user["id"], new, "password_reset", "Password reset through account recovery")
        except auth_service.SignupError as exc:
            flash(str(exc), "error")
            return redirect(url_for("auth.forgot_reset"))
        security.reset_limit(f"login:u:{user['username'].lower()}")
        session.pop("fp", None)
        flash("Password updated. Sign in with your new password.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", username=user["username"])
