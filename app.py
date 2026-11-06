"""Digital Archive - application factory and WSGI entry point."""
from __future__ import annotations

import logging
import os
import secrets
import time
from datetime import timedelta

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from database import engine, init_db, utcnow
from routes.common import csrf_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_last_lazy_purge = 0.0


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config.update(
        MAX_CONTENT_LENGTH=config.MAX_CONTENT_LENGTH_MB * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=4 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.IS_PRODUCTION, SESSION_COOKIE_NAME="da_session",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7), JSON_SORT_KEYS=False,
    )
    if config.IS_PRODUCTION:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    init_db()

    from routes import admin, albums, auth, cron, intelligence, main, media, account
    for module in (auth, main, media, albums, intelligence, account, admin, cron):
        app.register_blueprint(module.bp)

    app.jinja_env.globals["csrf_token"] = csrf_token
    register_hooks(app)
    register_errors(app)

    if not os.getenv("DISABLE_BACKGROUND_JOBS"):
        from services import trash_service
        trash_service.start_background_purger()
    return app


def register_hooks(app: Flask) -> None:
    from services import auth_service, stats_service, storage_service
    from services.classifier import CATEGORIES, CATEGORY_COLOR, CATEGORY_LABEL

    @app.before_request
    def load_user():
        g.user = None
        if request.endpoint in (None, "static"):
            return
        uid = session.get("uid")
        if uid:
            user = auth_service.get_user(uid)
            if user and not user["is_blocked"] and session.get("sv") == user["session_version"]:
                g.user = user
            else:
                blocked = bool(user and user["is_blocked"])
                for key in ("uid", "sv"):
                    session.pop(key, None)
                if blocked:
                    session["flash_blocked"] = True

    @app.before_request
    def enforce_csrf():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint != "static":
            expected = session.get("csrf_token")
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                abort(400, description="Your session security token expired. Please refresh the page and try again.")

    @app.before_request
    def lazy_purge():
        """Opportunistic trash purge so cleanup also happens on hosts that sleep."""
        global _last_lazy_purge
        if os.getenv("DISABLE_BACKGROUND_JOBS") or request.endpoint == "static":
            return
        if time.time() - _last_lazy_purge > 900:
            _last_lazy_purge = time.time()
            import threading
            from services import trash_service
            threading.Thread(target=trash_service.purge_expired, daemon=True).start()

    @app.context_processor
    def inject_globals():
        theme = (g.user or {}).get("theme") or request.cookies.get("da_theme") or "light"
        if request.blueprint == "admin":
            theme = request.cookies.get("da_theme") or "dark"
        avatar = storage_service.delivery_url(g.user["avatar_url"]) if g.get("user") and g.user.get("avatar_url") else None
        return {"csrf_token": csrf_token, "avatar_url": avatar, "theme": theme if theme in ("light", "dark") else "light",
                "current_user": g.get("user"), "CATEGORIES": CATEGORIES, "CATEGORY_LABEL": CATEGORY_LABEL,
                "CATEGORY_COLOR": CATEGORY_COLOR, "human_size": stats_service.human_size,
                "announcement": _announcement() if g.get("user") else None}

    @app.template_filter("dt")
    def format_dt(value, fmt="%d %b %Y, %I:%M %p"):
        return value.astimezone().strftime(fmt) if value else "—"

    @app.template_filter("ago")
    def ago(value):
        if not value:
            return "—"
        seconds = int((utcnow() - value).total_seconds())
        for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if seconds >= size:
                n = seconds // size
                return f"{n} {unit}{'s' if n != 1 else ''} ago"
        return "just now"

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if config.IS_PRODUCTION:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint != "static" and (g.get("user") or request.blueprint in {"admin", "auth"}):
            response.headers["Cache-Control"] = "no-store, private, max-age=0"
        return response


def _announcement():
    from sqlalchemy import select
    from database import announcements
    try:
        with engine.connect() as conn:
            row = conn.execute(select(announcements.c.message, announcements.c.level).where(announcements.c.is_active.is_(True))
                               .order_by(announcements.c.created_at.desc()).limit(1)).first()
        return {"message": row[0], "level": row[1]} if row else None
    except Exception:
        return None


def register_errors(app: Flask) -> None:
    def render_error(code, title, message, status):
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), status
        back = url_for("main.dashboard") if g.get("user") else url_for("auth.login")
        return render_template("error.html", code=code, title=title, message=message, back_url=back), status

    @app.errorhandler(400)
    def e400(error):
        return render_error(400, "Request could not be accepted", getattr(error, "description", "Please try again."), 400)

    @app.errorhandler(403)
    def e403(_e):
        return render_error(403, "Access denied", "You do not have permission to open this page.", 403)

    @app.errorhandler(404)
    def e404(_e):
        return render_error(404, "Page not found", "The page or file may have been removed, expired, or the link is incorrect.", 404)

    @app.errorhandler(413)
    def e413(_e):
        return render_error(413, "Upload too large", f"This upload exceeds the {config.MAX_CONTENT_LENGTH_MB} MB limit. Choose fewer or smaller files.", 413)

    @app.errorhandler(500)
    def e500(error):
        app.logger.error("Unhandled error: %s", error)
        return render_error(500, "Something went wrong", "The request could not be completed. Please try again.", 500)


app = create_app() if os.getenv("DA_SKIP_APP_CREATE") != "1" else None

if __name__ == "__main__":
    app.run(host="127.0.0.1" if not config.IS_PRODUCTION else "0.0.0.0", port=int(os.getenv("PORT", "5000")),
            debug=(config.APP_ENV == "development"))
