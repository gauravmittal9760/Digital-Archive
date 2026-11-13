from conftest import token

PW = "Str0ng!Pass"


def _signup_and_login(client):
    client.post("/signup", data={
        "csrf_token": token(client, "/signup"),
        "username": "rewind_user", "email": "rewind@example.com",
        "password": PW, "confirm_password": PW,
        "question": "What was the name of your first school?", "answer": "DAV",
    })
    client.post("/login", data={"csrf_token": token(client), "username": "rewind_user", "password": PW})


def test_pwa_endpoints_are_available(client):
    manifest = client.get("/manifest.webmanifest")
    worker = client.get("/sw.js")
    offline = client.get("/offline.html")
    assert manifest.status_code == worker.status_code == offline.status_code == 200
    assert b'"display": "standalone"' in manifest.data
    assert "Service-Worker-Allowed" in worker.headers
    assert b"You are offline" in offline.data
    assert b"/dashboard" not in worker.data and b"/media/" not in worker.data


def test_memory_rewind_requires_login_and_loads(client):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    import config
    from database import engine, media, profiles, new_id
    from sqlalchemy import select

    assert client.get("/on-this-day").status_code == 302
    _signup_and_login(client)
    today = datetime.now(ZoneInfo(config.APP_TIMEZONE)).date()
    prior = datetime(today.year - 1, today.month, today.day, 12, 0, tzinfo=timezone.utc)
    with engine.connect() as conn:
        user_id = conn.execute(select(profiles.c.id).where(profiles.c.username == "rewind_user")).scalar_one()
    with engine.begin() as conn:
        conn.execute(media.insert().values(
            id=new_id(), user_id=user_id, file_name="old-memory.pdf", storage_path=f"{user_id}/documents/old-memory.pdf",
            media_type="document", mime_type="application/pdf", file_size=1200, original_size=1200,
            captured_at=prior, created_at=prior, updated_at=prior, is_deleted=False,
        ))
    response = client.get("/on-this-day")
    assert response.status_code == 200
    assert b"Memory Rewind" in response.data
    assert b"old-memory.pdf" in response.data
