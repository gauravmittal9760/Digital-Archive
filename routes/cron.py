"""Endpoints for an external scheduler (GitHub Actions / cron-job.org / UptimeRobot)."""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, request
from sqlalchemy import func, select

import config
from database import engine, users
from services import otp_service, security, storage_service, trash_service

bp = Blueprint("cron", __name__)


@bp.route("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(select(func.count()).select_from(users)).scalar_one()
        database = "connected"
    except Exception:
        database = "unavailable"
    storage = storage_service.storage_mode()
    healthy = database == "connected" and (not config.IS_PRODUCTION or storage == "cloudinary")
    return jsonify(status="ok" if healthy else "degraded", database=database, storage=storage), (200 if healthy else 503)


@bp.route("/cron/purge", methods=["GET", "POST"])
def purge():
    if not config.CRON_SECRET or not security.safe_equals(request.args.get("key", "") or request.headers.get("X-Cron-Key", ""), config.CRON_SECRET):
        abort(404)
    removed = trash_service.purge_expired(batch=500)
    otp_service.purge_old_otps()
    return jsonify(purged=removed, remaining=trash_service.pending_purge_count())
