"""Expiry reminders: e-mail the owner 30 / 7 / 1 days before a tracked document expires.

`expiry_notice` stores the last stage already sent (0 none, 1 = 30d, 2 = 7d, 3 = 1d) so nothing is sent twice.
"""
from __future__ import annotations

import html
import logging
from datetime import date, timedelta

from sqlalchemy import select, update

from database import engine, media, profiles, users, utcnow
from services import mail_service

log = logging.getLogger(__name__)
STAGES = ((1, 30), (2, 7), (3, 1))        # (stage number, days before expiry)


def stage_for(days_left: int) -> int:
    """Highest stage that is due for this many days remaining (0 when none)."""
    due = 0
    for stage, days in STAGES:
        if days_left <= days:
            due = stage
    return due


def expiring(user_id: str, within_days: int = 90, today: date | None = None) -> list[dict]:
    today = today or date.today()
    with engine.connect() as conn:
        rows = conn.execute(select(media).where(
            media.c.user_id == user_id, media.c.is_deleted.is_(False), media.c.expiry_date.is_not(None),
            media.c.expiry_date <= today + timedelta(days=within_days)).order_by(media.c.expiry_date)).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        item["days_left"] = (item["expiry_date"] - today).days
        out.append(item)
    return out


def run_reminders(today: date | None = None, limit: int = 200) -> int:
    today = today or date.today()
    horizon = today + timedelta(days=30)
    sent = 0
    with engine.connect() as conn:
        rows = conn.execute(select(media.c.id, media.c.user_id, media.c.file_name, media.c.doc_title, media.c.expiry_date,
                                   media.c.expiry_notice, users.c.email, profiles.c.username, users.c.reminders_enabled)
                            .join(users, users.c.id == media.c.user_id)
                            .join(profiles, profiles.c.id == users.c.id)
                            .where(media.c.is_deleted.is_(False), media.c.expiry_date.is_not(None),
                                   media.c.expiry_date <= horizon, users.c.reminders_enabled.is_(True),
                                   users.c.is_blocked.is_(False)).limit(limit)).mappings().all()
    for r in rows:
        days_left = (r["expiry_date"] - today).days
        stage = stage_for(days_left)
        if stage <= (r["expiry_notice"] or 0) or not r["email"]:
            continue
        name = html.escape(r["doc_title"] or r["file_name"])
        when = "has expired" if days_left < 0 else ("expires today" if days_left == 0 else f"expires in {days_left} day(s)")
        body = (f"<p>Hi {html.escape(r['username'])},</p><p>Your document <b>{name}</b> {when} "
                f"({r['expiry_date']:%d %b %Y}).</p><p>Open Digital Archive → Expiry tracker to review it.</p>")
        try:
            mail_service.send_email(r["email"], f"Reminder: {name} {when}", body, f"{name} {when} ({r['expiry_date']}).")
        except Exception:
            log.warning("Reminder mail failed for %s", r["id"])
            continue
        with engine.begin() as conn:
            conn.execute(update(media).where(media.c.id == r["id"]).values(expiry_notice=stage))
        sent += 1
    return sent
