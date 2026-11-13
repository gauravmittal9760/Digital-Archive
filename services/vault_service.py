"""Private Vault: client-independent, server-side envelope encryption.

* A random 256-bit data key encrypts every vault file with AES-GCM.
* The data key is stored twice, wrapped by (a) a key derived from the user's vault passphrase and
  (b) a key derived from a one-time recovery key. We never store the passphrase or recovery key.
* Forgetting both means the files cannot be recovered - by design.
* An unlocked vault is a row in `vault_sessions`: the data key encrypted with a key derived from SECRET_KEY,
  valid UNLOCK_TTL seconds and deleted on lock. It lives in the database, so every worker/instance sees it.
"""
from __future__ import annotations

import base64
import os
import secrets
import hashlib
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import delete, func, insert, select, update

from datetime import timedelta

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from database import engine, new_id, storage_cleanup, users, utcnow, vault_items, vault_sessions
from services import storage_service

UNLOCK_TTL = 600
MAX_FILE = 20 * 1024 * 1024
_SCRYPT = dict(n=2 ** 15, r=8, p=1)


class VaultError(ValueError):
    pass


def _b(x: bytes) -> str:
    return base64.urlsafe_b64encode(x).decode()


def _u(x: str) -> bytes:
    return base64.urlsafe_b64decode(x.encode())


def _kek(secret: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, **_SCRYPT).derive(secret.encode("utf-8"))


def _wrap(secret: str, data_key: bytes) -> str:
    salt, nonce = os.urandom(16), os.urandom(12)
    return f"{_b(salt)}.{_b(nonce)}.{_b(AESGCM(_kek(secret, salt)).encrypt(nonce, data_key, b'vault-key'))}"


def _unwrap(secret: str, blob: str) -> bytes:
    try:
        salt, nonce, ct = (_u(p) for p in blob.split("."))
        return AESGCM(_kek(secret, salt)).decrypt(nonce, ct, b"vault-key")
    except (InvalidTag, ValueError):
        raise VaultError("Wrong passphrase or recovery key.")


def is_setup(user: dict) -> bool:
    return bool(user.get("vault_wrapped_key"))


def setup(user_id: str, passphrase: str) -> str:
    """Create the vault. Returns the recovery key (shown once)."""
    if len(passphrase) < 8:
        raise VaultError("Vault passphrase must be at least 8 characters.")
    data_key = AESGCM.generate_key(256)
    recovery = "-".join(secrets.token_hex(2).upper() for _ in range(6))
    with engine.begin() as conn:
        done = conn.execute(select(users.c.vault_wrapped_key).where(users.c.id == user_id)).scalar()
        if done:
            raise VaultError("Vault is already set up.")
        conn.execute(update(users).where(users.c.id == user_id).values(
            vault_wrapped_key=_wrap(passphrase, data_key), vault_recovery_wrapped=_wrap(recovery, data_key), vault_salt="v1"))
    return recovery


def _row(user_id: str) -> dict:
    with engine.connect() as conn:
        r = conn.execute(select(users.c.vault_wrapped_key, users.c.vault_recovery_wrapped).where(users.c.id == user_id)).mappings().first()
    if not r or not r["vault_wrapped_key"]:
        raise VaultError("Vault is not set up.")
    return dict(r)


def unlock(user_id: str, passphrase: str) -> str:
    key = _unwrap(passphrase, _row(user_id)["vault_wrapped_key"])
    return _remember(user_id, key)


def _server_kek() -> bytes:
    from config import SECRET_KEY
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"da-vault-session").derive(SECRET_KEY.encode())


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _remember(user_id: str, key: bytes) -> str:
    token = secrets.token_urlsafe(24)
    nonce = os.urandom(12)
    wrapped = _b(nonce + AESGCM(_server_kek()).encrypt(nonce, key, user_id.encode()))
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(delete(vault_sessions).where(vault_sessions.c.expires_at < now))
        conn.execute(insert(vault_sessions).values(token_hash=_th(token), user_id=user_id, wrapped_key=wrapped,
                                                   expires_at=now + timedelta(seconds=UNLOCK_TTL), created_at=now))
    return token


def key_for(user_id: str, token: str | None) -> bytes | None:
    """Data key for an unlocked session (sliding 10-minute window), or None if locked/expired/not yours."""
    if not token:
        return None
    now = utcnow()
    with engine.begin() as conn:
        row = conn.execute(select(vault_sessions.c.wrapped_key, vault_sessions.c.expires_at).where(
            vault_sessions.c.token_hash == _th(token), vault_sessions.c.user_id == user_id)).first()
        if not row:
            return None
        if row[1] < now:
            conn.execute(delete(vault_sessions).where(vault_sessions.c.token_hash == _th(token)))
            return None
        conn.execute(update(vault_sessions).where(vault_sessions.c.token_hash == _th(token))
                     .values(expires_at=now + timedelta(seconds=UNLOCK_TTL)))
    try:
        raw = _u(row[0])
        return AESGCM(_server_kek()).decrypt(raw[:12], raw[12:], user_id.encode())
    except (InvalidTag, ValueError):
        return None


def lock(token: str | None) -> None:
    if token:
        with engine.begin() as conn:
            conn.execute(delete(vault_sessions).where(vault_sessions.c.token_hash == _th(token)))


def purge_sessions() -> int:
    with engine.begin() as conn:
        return conn.execute(delete(vault_sessions).where(vault_sessions.c.expires_at < utcnow())).rowcount


def change_passphrase(user_id: str, current: str, new: str) -> None:
    if len(new) < 8:
        raise VaultError("New passphrase must be at least 8 characters.")
    key = _unwrap(current, _row(user_id)["vault_wrapped_key"])
    with engine.begin() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(vault_wrapped_key=_wrap(new, key)))


def reset_with_recovery(user_id: str, recovery: str, new: str) -> str:
    """Set a new passphrase using the recovery key. Returns a fresh recovery key."""
    if len(new) < 8:
        raise VaultError("New passphrase must be at least 8 characters.")
    key = _unwrap(recovery.strip().upper(), _row(user_id)["vault_recovery_wrapped"])
    fresh = "-".join(secrets.token_hex(2).upper() for _ in range(6))
    with engine.begin() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(
            vault_wrapped_key=_wrap(new, key), vault_recovery_wrapped=_wrap(fresh, key)))
    return fresh


# ------------------------------------------------------------------ files
def _enc(key: bytes, data: bytes, aad: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, aad.encode())


def _dec(key: bytes, blob: bytes, aad: str) -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad.encode())


def add_file(user_id: str, key: bytes, name: str, mime: str, data: bytes) -> str:
    from services import stats_service
    if not data:
        raise VaultError("The file is empty.")
    if len(data) > MAX_FILE:
        raise VaultError(f"Vault files can be up to {MAX_FILE // (1024 * 1024)} MB each.")
    item_id = new_id()
    blob = _enc(key, data, item_id)
    path = f"{user_id}/vault/{item_id}.bin"
    storage_service.upload_bytes(path, blob, "application/octet-stream")
    try:
        with stats_service.quota_transaction(user_id, len(blob)) as conn:
            conn.execute(insert(vault_items).values(id=item_id, user_id=user_id, file_name=name[:255], mime_type=(mime or "")[:255],
                                                    plain_size=len(data), file_size=len(blob), storage_path=path, created_at=utcnow()))
    except stats_service.QuotaExceeded:
        storage_service.delete_or_queue([path])
        raise VaultError("This file would exceed your storage quota.")
    except Exception:
        storage_service.delete_or_queue([path])
        raise
    return item_id


def list_items(user_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(vault_items).where(vault_items.c.user_id == user_id)
                                              .order_by(vault_items.c.created_at.desc())).mappings().all()]


def get_item(user_id: str, item_id: str) -> dict | None:
    with engine.connect() as conn:
        r = conn.execute(select(vault_items).where(vault_items.c.id == item_id, vault_items.c.user_id == user_id)).mappings().first()
    return dict(r) if r else None


def read_file(key: bytes, item: dict) -> bytes:
    blob = storage_service.read_bytes(item["storage_path"], max_bytes=MAX_FILE + 1024)
    try:
        return _dec(key, blob, item["id"])
    except InvalidTag:
        raise VaultError("This file could not be decrypted (wrong key or damaged data).")


def delete_item(user_id: str, item_id: str) -> bool:
    item = get_item(user_id, item_id)
    if not item:
        return False
    with engine.begin() as conn:                                   # row and cleanup job commit together
        conn.execute(delete(vault_items).where(vault_items.c.id == item_id))
        storage_service.queue_cleanup(conn, [item["storage_path"]])
    storage_service.process_cleanup(limit=5)
    return True


def bytes_used(user_id: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.coalesce(func.sum(vault_items.c.file_size), 0)).where(vault_items.c.user_id == user_id)).scalar() or 0)


def erase_vault(user_id: str) -> int:
    """Forgotten passphrase AND recovery key: wipe the vault so a new one can be created.

    Everything happens in ONE database transaction: the vault rows and keys are removed and every storage path is
    written to the cleanup queue. Either all of that commits or none of it does, so the database can never point at
    a file that has already been deleted. The actual object deletion then runs afterwards and is retried by the
    background job for anything Cloudinary could not delete immediately - nothing is left orphaned.
    """
    with engine.begin() as conn:
        paths = [r[0] for r in conn.execute(select(vault_items.c.storage_path).where(vault_items.c.user_id == user_id)).all()]
        conn.execute(delete(vault_items).where(vault_items.c.user_id == user_id))
        conn.execute(delete(vault_sessions).where(vault_sessions.c.user_id == user_id))
        conn.execute(update(users).where(users.c.id == user_id).values(vault_wrapped_key=None, vault_recovery_wrapped=None, vault_salt=None))
        storage_service.queue_cleanup(conn, paths)
    try:
        storage_service.process_cleanup(limit=max(50, len(paths)))
    except Exception:
        pass                                                         # the background job will finish the job
    return len(paths)
