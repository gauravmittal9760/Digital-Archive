"""Vault, doc intelligence, reminders, share permissions, review/ask/expiry pages."""
import io
from datetime import date, timedelta

import pytest

from services import doc_intel, reminder_service, vault_service
from services.vault_service import VaultError


def test_doc_intel_expiry_and_type():
    r = doc_intel.analyze("Passport No Z123\nDate of Expiry: 12/05/2031\nPlace of issue Delhi", "p.pdf")
    assert r["doc_type"] == "passport" and r["expiry_date"] == date(2031, 5, 12)
    assert doc_intel.find_expiry("Valid till 3 March 2027") == date(2027, 3, 3)
    assert doc_intel.find_expiry("Issued on 01/01/2020") is None


def test_reminder_stages():
    assert reminder_service.stage_for(60) == 0
    assert reminder_service.stage_for(30) == 1 and reminder_service.stage_for(5) == 2 and reminder_service.stage_for(-2) == 3


def test_vault_crypto_roundtrip_and_wrong_pass(app):
    from database import engine, users, new_id, utcnow
    from sqlalchemy import insert
    uid = new_id()
    with engine.begin() as c:
        c.execute(insert(users).values(id=uid, email="v@x.com", password_hash="x", created_at=utcnow(), updated_at=utcnow()))
    rec = vault_service.setup(uid, "correct horse")
    with pytest.raises(VaultError):
        vault_service.unlock(uid, "wrong passphrase")
    token = vault_service.unlock(uid, "correct horse")
    key = vault_service.key_for(uid, token)
    assert key and vault_service.key_for("other", token) is None
    iid = vault_service.add_file(uid, key, "a.txt", "text/plain", b"secret data")
    item = vault_service.get_item(uid, iid)
    from services import storage_service
    assert b"secret data" not in storage_service.read_bytes(item["storage_path"])
    assert vault_service.read_file(key, item) == b"secret data"
    fresh = vault_service.reset_with_recovery(uid, rec, "new passphrase")
    assert vault_service.unlock(uid, "new passphrase") and fresh != rec
    vault_service.lock(token)
    assert vault_service.key_for(uid, token) is None


def _png():
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (64, 64), (200, 30, 30)).save(b, "PNG"); return b.getvalue()


def _user(client, name):
    from test_app import signup, login
    signup(client, name, f"{name}@example.com"); login(client, name)


def test_pages_render_and_vault_flow(client):
    from conftest import token
    _user(client, "vaultuser")
    for url in ("/expiry", "/ask?q=policy", "/review", "/vault/", "/dashboard", "/hub/smart", "/hub/library"):
        assert client.get(url).status_code == 200, url
    t = token(client, "/vault/")
    assert client.post("/vault/setup", data=dict(csrf_token=t, passphrase="my vault pass", confirm="my vault pass")).status_code == 200
    client.post("/vault/lock", data=dict(csrf_token=t))
    assert b"Unlock vault" in client.get("/vault/").data
    client.post("/vault/unlock", data=dict(csrf_token=t, passphrase="my vault pass"))
    r = client.post("/vault/upload", data=dict(csrf_token=t, files=(io.BytesIO(b"hello vault"), "n.txt")), content_type="multipart/form-data", follow_redirects=True)
    assert b"n.txt" in r.data
    from services import vault_service
    from database import engine, vault_items
    from sqlalchemy import select
    with engine.connect() as c:
        iid = c.execute(select(vault_items.c.id).where(vault_items.c.file_name == "n.txt")).scalar()
    assert client.get(f"/vault/{iid}/file?download=1").data == b"hello vault"
    client.post("/vault/lock", data=dict(csrf_token=t))
    assert client.get(f"/vault/{iid}/file").status_code in (302, 303)       # locked -> no access


def test_share_password_and_view_only(client):
    from conftest import token
    from database import engine, media, share_links
    from sqlalchemy import select
    _user(client, "sharer")
    t = token(client, "/upload")
    client.post("/upload", data=dict(csrf_token=t, media=(io.BytesIO(_png()), "pic.png")), content_type="multipart/form-data")
    with engine.connect() as c:
        mid = c.execute(select(media.c.id).where(media.c.file_name == "pic.png")).scalar()
    client.post(f"/media/{mid}/share", data=dict(csrf_token=token(client, f"/media/{mid}"), expires_hours="24", share_password="secret1"))
    link = client.get("/shares").get_data(as_text=True)
    import re
    url = re.search(r'/shared/[\w-]+', link).group(0)
    anon = client.application.test_client()
    page = anon.get(url)
    assert page.status_code == 200 and b"Protected file" in page.data and "no-store" in page.headers["Cache-Control"]
    assert anon.get(url + "/file").status_code == 403
    tk = token(anon, url)
    assert anon.post(url + "/unlock", data=dict(csrf_token=tk, password="bad")).status_code == 403
    anon.post(url + "/unlock", data=dict(csrf_token=tk, password="secret1"))
    assert anon.get(url + "/file").status_code == 200
    assert anon.get(url + "/file?download=1").status_code == 403           # download not allowed (box unchecked)


def test_vault_erase_start_over(client):
    from conftest import token
    from database import engine, vault_items, users
    from sqlalchemy import select
    from services import storage_service
    _user(client, "eraser")
    t = token(client, "/vault/")
    client.post("/vault/setup", data=dict(csrf_token=t, passphrase="forgotten one", confirm="forgotten one"))
    client.post("/vault/upload", data=dict(csrf_token=t, files=(io.BytesIO(b"lost data"), "lost.txt")), content_type="multipart/form-data")
    with engine.connect() as c:
        path = c.execute(select(vault_items.c.storage_path).where(vault_items.c.file_name == "lost.txt")).scalar()
    assert path
    client.post("/vault/lock", data=dict(csrf_token=t))
    # wrong account password -> nothing deleted
    client.post("/vault/erase", data=dict(csrf_token=t, confirm="ERASE", account_password="nope"))
    with engine.connect() as c:
        assert c.execute(select(vault_items.c.id).where(vault_items.c.file_name == "lost.txt")).first()
    from test_app import PW
    client.post("/vault/erase", data=dict(csrf_token=t, confirm="ERASE", account_password=PW))
    with engine.connect() as c:
        assert not c.execute(select(vault_items.c.id).where(vault_items.c.file_name == "lost.txt")).first()
    with pytest.raises(Exception):
        storage_service.read_bytes(path)
    assert b"Create vault" in client.get("/vault/").data            # fresh setup offered again
    r = client.post("/vault/setup", data=dict(csrf_token=t, passphrase="brand new pass", confirm="brand new pass"))
    assert r.status_code == 200 and b"recovery key" in r.data.lower()


def test_nav_says_gallery(client):
    _user(client, "navuser")
    page = client.get("/dashboard").get_data(as_text=True)
    assert "Gallery</a>" in page and "Library</a>" not in page


def test_album_suggestions_and_storage_tips(client):
    from conftest import token
    from database import engine, media, new_id, utcnow
    from sqlalchemy import insert, select, update
    from services import album_service, stats_service
    _user(client, "albumuser")
    from database import users
    from services import auth_service
    with engine.connect() as c:
        uid = c.execute(select(users.c.id).where(users.c.email == "albumuser@example.com")).scalar()
    base = utcnow()
    with engine.begin() as c:
        for i in range(7):
            c.execute(insert(media).values(id=new_id(), user_id=uid, file_name=f"t{i}.jpg", storage_path=f"{uid}/photos/t{i}.jpg", media_type="photo",
                      mime_type="image/jpeg", file_size=1000, category="travel", is_deleted=False, is_favorite=False,
                      captured_at=base.replace(year=2024, month=8, day=10 + (i // 3)), created_at=base, updated_at=base))
    sug = album_service.suggestions(uid)
    assert any(s["key"].startswith("trip:2024-08-10") for s in sug) and any(s["key"] == "cat:travel" for s in sug)
    page = client.get("/albums/")
    assert page.status_code == 200 and b"Suggested albums" in page.data
    r = client.post("/albums/suggest", data=dict(csrf_token=token(client, "/albums/"), key="cat:travel"))
    assert r.status_code == 302
    assert not any(s["key"] == "cat:travel" for s in album_service.suggestions(uid))     # now covered by an album
    assert client.get("/storage").status_code == 200
    assert isinstance(stats_service.optimizer_tips(uid), list)


# ----------------------------------------------------------------- security hardening round
def test_delivery_tokens_expire_and_reject_tampering(client):
    import time
    from services import storage_service as ss
    path = "u1/photos/a.webp"
    tok = ss.make_delivery_token(path)
    assert ss.read_delivery_token(tok)[0] == path
    assert ss.make_delivery_token(path) == tok                              # stable inside a window -> browser cache works
    win = ss.MEDIA_URL_MINUTES * 60
    assert ss.read_delivery_token(tok, now=time.time() + 2 * win + 5) is None   # expired
    assert ss.read_delivery_token(tok[:-2] + "xx") is None                    # tampered
    assert ss.read_delivery_token(ss.make_delivery_token("u1/vault/x.bin")) is None   # vault blobs never deliverable
    assert client.get("/dl/not-a-token").status_code == 404


def test_delivery_route_serves_then_expires(client):
    import re, time
    from conftest import token
    from services import storage_service as ss
    _user(client, "dluser")
    client.post("/upload", data=dict(csrf_token=token(client, "/upload"), media=(io.BytesIO(_png()), "d.png")), content_type="multipart/form-data")
    html = client.get("/gallery").get_data(as_text=True)
    url = re.search(r'/dl/[\w.\-]+', html).group(0)
    anon = client.application.test_client()
    r = anon.get(url)
    assert r.status_code == 200 and "private" in r.headers["Cache-Control"] and r.headers["X-Robots-Tag"] == "noindex"
    ss.MEDIA_URL_MINUTES, old = 5, ss.MEDIA_URL_MINUTES
    try:
        real = time.time
        time.time = lambda: real() + 3 * 3600
        assert anon.get(url).status_code == 404
    finally:
        time.time = real
        ss.MEDIA_URL_MINUTES = old


def test_vault_erase_is_atomic_when_storage_delete_fails(app, monkeypatch):
    from database import engine, users, new_id, utcnow, vault_items, storage_cleanup
    from sqlalchemy import insert, select
    from services import storage_service as ss
    uid = new_id()
    with engine.begin() as c:
        c.execute(insert(users).values(id=uid, email="atomic@x.com", password_hash="x", created_at=utcnow(), updated_at=utcnow()))
    vault_service.setup(uid, "atomic passphrase")
    key = vault_service.key_for(uid, vault_service.unlock(uid, "atomic passphrase"))
    ids = [vault_service.add_file(uid, key, f"f{i}.txt", "text/plain", b"x" * 50) for i in range(3)]
    paths = [vault_service.get_item(uid, i)["storage_path"] for i in ids]
    real_delete = ss.delete_object
    monkeypatch.setattr(ss, "delete_object", lambda p: (_ for _ in ()).throw(RuntimeError("cloudinary down")))
    assert vault_service.erase_vault(uid) == 3                                  # does NOT fail halfway
    with engine.connect() as c:
        assert not c.execute(select(vault_items.c.id).where(vault_items.c.user_id == uid)).first()   # rows gone
        assert c.execute(select(users.c.vault_wrapped_key).where(users.c.id == uid)).scalar() is None
        queued = {r[0] for r in c.execute(select(storage_cleanup.c.storage_path)).all()}
    assert set(paths) <= queued                                                 # every blob is queued, none orphaned
    monkeypatch.setattr(ss, "delete_object", real_delete)
    assert ss.process_cleanup() >= 3                                            # retry succeeds later
    with engine.connect() as c:
        assert not {r[0] for r in c.execute(select(storage_cleanup.c.storage_path)).all()} & set(paths)
    for p in paths:
        with pytest.raises(Exception):
            ss.read_bytes(p)


def test_parallel_uploads_cannot_exceed_quota(app, monkeypatch):
    import threading
    from werkzeug.datastructures import FileStorage
    from database import engine, users, new_id, utcnow
    from sqlalchemy import insert
    from services import auth_service, stats_service, upload_service
    uid = new_id()
    with engine.begin() as c:
        c.execute(insert(users).values(id=uid, email="quota@x.com", password_hash="x", created_at=utcnow(), updated_at=utcnow()))
    user = {"id": uid, "ai_enabled": False}
    r0 = upload_service.ingest(user, FileStorage(io.BytesIO(_png()), filename="probe.png", content_type="image/png"))
    assert r0["status"] == "ok"
    one = stats_service.storage_used(uid)
    monkeypatch.setattr(stats_service, "QUOTA_BYTES", int(one * 2.5))             # room for exactly two files in total
    results = []

    def go(i):
        results.append(upload_service.ingest(user, FileStorage(io.BytesIO(_png()), filename=f"p{i}.png", content_type="image/png"), used_bytes=0))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in threads]; [t.join() for t in threads]
    ok = sum(r["status"] == "ok" for r in results)
    assert ok == 1, [r["message"] for r in results]                              # 1 + probe = 2 files, never 3+
    assert stats_service.storage_used(uid) <= stats_service.QUOTA_BYTES


def test_vault_unlock_is_shared_via_database_and_revocable(app):
    from database import engine, users, new_id, utcnow, vault_sessions
    from sqlalchemy import insert, select, update
    uid = new_id()
    with engine.begin() as c:
        c.execute(insert(users).values(id=uid, email="sess@x.com", password_hash="x", created_at=utcnow(), updated_at=utcnow()))
    vault_service.setup(uid, "session passphrase")
    token = vault_service.unlock(uid, "session passphrase")
    key = vault_service.key_for(uid, token)
    assert key and len(key) == 32
    with engine.connect() as c:
        wrapped = c.execute(select(vault_sessions.c.wrapped_key)).scalars().all()
    assert wrapped and all(key.hex() not in w and key.decode("latin1") not in w for w in wrapped)   # never stored in clear
    assert vault_service.key_for("someone-else", token) is None
    with engine.begin() as c:
        c.execute(update(vault_sessions).values(expires_at=utcnow().replace(year=2020)))
    assert vault_service.key_for(uid, token) is None                              # expired
    token2 = vault_service.unlock(uid, "session passphrase")
    vault_service.lock(token2)
    assert vault_service.key_for(uid, token2) is None                             # lock revokes server-side


def test_postgres_url_uses_pure_python_driver_and_cleans_neon_options():
    import importlib, config
    from database import postgres_engine_settings
    url, args = postgres_engine_settings("postgresql+pg8000://u:p%40ss@ep-x-pooler.ap-south-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
    assert url.drivername == "postgresql+pg8000" and dict(url.query) == {} and url.password == "p@ss"
    assert args["ssl_context"] is True and args["timeout"] == 30
    _, args = postgres_engine_settings("postgresql+pg8000://u:p@localhost/db?sslmode=disable")
    assert "ssl_context" not in args
    url, args = postgres_engine_settings("postgresql+psycopg://u:p@h/db?sslmode=require")     # explicit psycopg still supported
    assert url.drivername == "postgresql+psycopg" and args["prepare_threshold"] is None
    import os
    for given in ("postgres://u:p@h/db", "postgresql://u:p@h/db"):
        os.environ["DATABASE_URL"] = given
        try:
            assert importlib.reload(config).DATABASE_URL == "postgresql+pg8000://u:p@h/db"
        finally:
            os.environ.pop("DATABASE_URL", None)
            importlib.reload(config)


def test_reminders_run_with_real_user_and_do_not_resend(client):
    from datetime import date, timedelta
    from sqlalchemy import insert, select
    from database import engine, media, new_id, users, utcnow
    from services import reminder_service
    _user(client, "remind1")
    with engine.connect() as c:
        uid = c.execute(select(users.c.id).where(users.c.email == "remind1@example.com")).scalar()
    mid = new_id()
    with engine.begin() as c:
        c.execute(insert(media).values(id=mid, user_id=uid, file_name="passport.pdf", storage_path=f"{uid}/documents/p.pdf", media_type="document",
                                       mime_type="application/pdf", file_size=10, category="identity", is_deleted=False, is_favorite=False,
                                       doc_title="Passport", expiry_date=date.today() + timedelta(days=6), created_at=utcnow(), updated_at=utcnow()))
    assert reminder_service.run_reminders() == 1          # 6 days left -> stage 2 (7-day notice)
    assert reminder_service.run_reminders() == 0          # already sent, never twice


def test_dead_connection_is_replaced_not_raised(tmp_path):
    import sqlite3, ssl
    from sqlalchemy import create_engine, text
    from database import install_liveness_check
    made = []

    class Flaky(sqlite3.Connection):
        bad = False
        def cursor(self, *a, **k):
            if self.bad:
                raise ssl.SSLError("DECRYPTION_FAILED_OR_BAD_RECORD_MAC")
            return super().cursor(*a, **k)

    def creator():
        conn = sqlite3.connect(str(tmp_path / "x.db"), factory=Flaky, check_same_thread=False)
        made.append(conn)
        return conn

    from sqlalchemy.pool import QueuePool
    eng = create_engine("sqlite://", creator=creator, poolclass=QueuePool, pool_size=1, max_overflow=0)
    install_liveness_check(eng)
    with eng.connect() as c:
        assert c.execute(text("select 1")).scalar() == 1
    made[0].bad = True                                       # the idle connection "dies"
    with eng.connect() as c:                                 # must silently reconnect, not raise ssl.SSLError
        assert c.execute(text("select 2")).scalar() == 2
    assert len(made) == 2


def test_dead_connection_close_noise_is_hidden_but_real_errors_are_not():
    import logging
    from database import QuietDeadConnectionLog
    class InterfaceError(Exception):
        pass
    f = QuietDeadConnectionLog()
    def rec(msg, exc):
        try:
            raise exc
        except Exception:
            import sys
            return logging.LogRecord("x", logging.ERROR, __file__, 1, msg, None, sys.exc_info())
    assert f.filter(rec("Exception terminating connection <c>", InterfaceError("network error"))) is False
    assert f.filter(rec("Exception terminating connection <c>", OSError("reset"))) is False
    assert f.filter(rec("Exception terminating connection <c>", ValueError("something else"))) is True
    assert f.filter(rec("Some other pool error", InterfaceError("network error"))) is True
