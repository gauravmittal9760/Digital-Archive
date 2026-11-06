"""Audit trail: permanent admin-visible account events + user-visible activity history."""
from __future__ import annotations

import logging

from sqlalchemy import insert

from database import account_events, activity_logs, engine, new_id, utcnow

log = logging.getLogger(__name__)


def client_ip() -> str | None:
    try:
        from flask import has_request_context, request
        if has_request_context():
            return (request.remote_addr or "")[:64] or None
    except Exception:
        pass
    return None


def client_agent() -> str | None:
    try:
        from flask import has_request_context, request
        if has_request_context():
            return (request.headers.get("User-Agent") or "")[:255] or None
    except Exception:
        pass
    return None


def log_event(event_type: str, details: str = "", user_id: str | None = None, username: str | None = None,
              email: str | None = None, user_visible: bool = True) -> None:
    """Record an account event. Never raises: auditing must not break the user's action.

    `account_events` has no foreign key so it survives account deletion; the
    user-facing `activity_logs` row is written only while the account exists.
    """
    try:
        now = utcnow()
        with engine.begin() as conn:
            conn.execute(insert(account_events).values(
                id=new_id(), user_id=user_id, username=username, email=email, event_type=event_type[:60],
                details=(details or "")[:1000], ip_address=client_ip(), created_at=now))
            if user_id and user_visible:
                conn.execute(insert(activity_logs).values(
                    id=new_id(), user_id=user_id, action=event_type[:100], details=(details or "")[:1000], created_at=now))
    except Exception as exc:                                           # pragma: no cover
        log.warning("Event logging failed (%s): %s", event_type, exc)


def log_activity(user_id: str, action: str, details: str = "") -> None:
    """User-visible activity only (uploads, deletes ...), not an admin account event."""
    try:
        with engine.begin() as conn:
            conn.execute(insert(activity_logs).values(
                id=new_id(), user_id=user_id, action=action[:100], details=(details or "")[:1000], created_at=utcnow()))
    except Exception as exc:                                           # pragma: no cover
        log.warning("Activity logging failed (%s): %s", action, exc)
