"""Whole-account operations shared by the user (self-delete) and the admin (delete user)."""
from __future__ import annotations

from sqlalchemy import delete, select

from database import engine, file_versions, media, profiles, users
from services import auth_service, classifier, storage_service
from services.events import log_event


class AccountDeleteError(RuntimeError):
    pass


def delete_account(user_id: str, actor: str = "user") -> None:
    """Remove every stored object first, then the database rows (cascade). Logged permanently."""
    user = auth_service.get_user(user_id)
    if not user:
        return
    with engine.connect() as conn:
        paths = []
        for row in conn.execute(select(media.c.storage_path, media.c.thumb_path).where(media.c.user_id == user_id)).all():
            paths.extend(row)
        paths += [r[0] for r in conn.execute(select(file_versions.c.storage_path).where(file_versions.c.user_id == user_id)).all()]
    if user.get("avatar_url"):
        paths.append(user["avatar_url"])
    failed = 0
    for path in dict.fromkeys(p for p in paths if p):
        try:
            storage_service.delete_object(path)
        except Exception:
            failed += 1
    if failed:
        raise AccountDeleteError(f"{failed} stored file(s) could not be removed from storage; nothing was deleted from the database.")
    log_event("account_deleted", f"Account deleted by {actor}; {len(paths)} stored file(s) removed", user_id=user_id,
              username=user["username"], email=user["email"], user_visible=False)
    with engine.begin() as conn:
        conn.execute(delete(users).where(users.c.id == user_id))
    classifier.forget_user_model(user_id)
