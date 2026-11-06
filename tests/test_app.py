import re
from conftest import token

PW = "Str0ng!Pass"


def signup(client, name, email="u@example.com"):
    return client.post("/signup", data=dict(csrf_token=token(client, "/signup"), username=name, email=email, password=PW,
                                            confirm_password=PW, question="What was the name of your first school?", answer="DAV"))


def login(client, name, pw=PW):
    return client.post("/login", data=dict(csrf_token=token(client), username=name, password=pw), follow_redirects=True)


def test_signup_and_username_login(client):
    signup(client, "alice")
    assert b"Hello, alice" in login(client, "alice").data


def test_five_wrong_attempts_lock_account(client):
    signup(client, "bob", "b@example.com")
    for _ in range(5):
        r = login(client, "bob", "wrong")
    assert b"Try again in" in r.data
    assert b"Hello, bob" not in login(client, "bob").data      # correct password still refused while locked


def test_forgot_username_needs_otp_and_lists_all(client):
    signup(client, "carol1", "shared@example.com"); signup(client, "carol2", "shared@example.com")
    client.post("/forgot/username", data=dict(csrf_token=token(client, "/forgot/username"), email="shared@example.com"))
    page = client.get("/forgot/username/verify").get_data(as_text=True)
    assert "carol1" not in page                                  # nothing leaks before verification
    from database import engine, otp_codes
    from services import otp_service
    # issue a known code through the service layer and verify it end to end
    import hashlib, hmac, config
    from sqlalchemy import select, update
    with engine.begin() as conn:
        row = conn.execute(select(otp_codes).where(otp_codes.c.email == "shared@example.com").order_by(otp_codes.c.created_at.desc())).mappings().first()
        code = next(c for c in (f"{i:06d}" for i in range(1000000)) if hmac.new(config.SECRET_KEY.encode(), f"{row['id']}:{c}".encode(), hashlib.sha256).hexdigest() == row["code_hash"])
    r = client.post("/forgot/username/verify", data=dict(csrf_token=token(client, "/forgot/username/verify"), code=code))
    assert b"carol1" in r.data and b"carol2" in r.data


def test_otp_resend_cooldown():
    from services import otp_service
    first = otp_service.issue_otp("reset_password", "cool@example.com")
    assert first["status"] == "ok"
    assert otp_service.issue_otp("reset_password", "cool@example.com")["status"] == "cooldown"


def test_security_question_reset(client):
    signup(client, "dave", "d@example.com")
    client.post("/forgot/password", data=dict(csrf_token=token(client, "/forgot/password"), username="dave"))
    client.post("/forgot/password/question", data=dict(csrf_token=token(client, "/forgot/password/question"), answer=" dav "))
    new = "N3w!Password"
    client.post("/forgot/password/reset", data=dict(csrf_token=token(client, "/forgot/password/reset"), new_password=new, confirm_password=new))
    assert b"Hello, dave" in login(client, "dave", new).data


def test_admin_hidden_until_gate(client):
    assert client.get("/admin/").status_code == 404
    client.post("/login", data=dict(csrf_token=token(client), username="root_door", password="Door#Pass1"))
    assert client.get("/admin/gateway").status_code == 200
    assert client.get("/admin/").status_code == 404              # gate alone is not enough
    client.post("/admin/login", data=dict(csrf_token=token(client, "/admin/login"), password="Panel#Pass1"))
    assert client.get("/admin/").status_code == 200


def test_pages_have_back_buttons(client):
    signup(client, "erin", "e@example.com"); login(client, "erin")
    for url in ["/gallery", "/upload", "/search", "/settings", "/trash", "/storage"]:
        assert b'class="back-btn"' in client.get(url).data, url


def test_search_understands_hinglish():
    from services import smart_search
    intent = smart_search.understand("meri 10th ki marksheat", {"marksheet"})
    assert "education" in intent["categories"] and intent["corrected"].get("marksheat") == "marksheet"


def _dev_code(client):
    with client.session_transaction() as s:
        flashes = [m for _, m in s.get("_flashes", [])]
    return next(re.search(r"code is (\d{6})", m).group(1) for m in flashes if "code is" in m)


def test_two_step_login(client):
    signup(client, "fay", "f@example.com"); login(client, "fay")
    client.post("/settings/security/two-step", data=dict(csrf_token=token(client, "/settings/security"), action="enable"))
    code = _dev_code(client)
    client.post("/settings/security/two-step/verify", data=dict(csrf_token=token(client, "/settings/security/two-step/verify"), code=code))
    client.post("/logout", data=dict(csrf_token=token(client, "/gallery")))
    client.post("/login", data=dict(csrf_token=token(client), username="fay", password=PW))   # no redirect: keep the flash
    code = _dev_code(client)
    page = client.get("/login/verify").data
    assert b"Two-step verification" in page and b"Hello, fay" not in page
    r = client.post("/login/verify", data=dict(csrf_token=token(client, "/login/verify"), code=code), follow_redirects=True)
    assert b"Hello, fay" in r.data


def _png(color=(200, 120, 40)):
    import io
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (300, 300), color).save(buf, "PNG"); buf.seek(0)
    return buf


def test_duplicate_upload_is_allowed_but_flagged(client):
    signup(client, "gita", "gi@example.com"); login(client, "gita")
    first = client.post("/api/upload", data={"file": (_png(), "a.png"), "csrf_token": token(client, "/upload")}, content_type="multipart/form-data").get_json()
    second = client.post("/api/upload", data={"file": (_png(), "b.png"), "csrf_token": token(client, "/upload")}, content_type="multipart/form-data").get_json()
    assert first["status"] == "ok" and second["status"] == "ok" and second["near_duplicate"]
    assert b"Exact copies" in client.get("/duplicates").data


def test_local_vision_labels_make_photo_searchable(client, monkeypatch):
    from services import ai_service, vision_local
    signup(client, "hari", "h@example.com"); login(client, "hari")
    r = client.post("/api/upload", data={"file": (_png((90, 160, 60)), "IMG_2231.png"), "csrf_token": token(client, "/upload")}, content_type="multipart/form-data").get_json()
    fake = {"labels": ["monarch", "butterfly", "insect", "animal", "wildlife"], "category": "pets", "confidence": 0.8, "caption": "Looks like: monarch"}
    monkeypatch.setattr(vision_local, "analyze", lambda jpeg, top=5: fake)
    ai_service.run_analysis(r["media_id"], b"x")
    for query in ("butterfly image", "janwar ki photo"):
        page = client.get("/search", query_string={"q": query}).get_data(as_text=True)
        assert "IMG_2231.png" in page, query
    assert "Animals" in client.get(f"/media/{r['media_id']}").get_data(as_text=True)
