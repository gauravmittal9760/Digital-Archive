import os, re, sys, tempfile, pytest

_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(DISABLE_LOCAL_VISION="1", DATABASE_URL=f"sqlite:///{_db}", DISABLE_BACKGROUND_JOBS="1", APP_ENV="development", BREVO_API_KEY="",
                  ADMIN_USERNAME="root_door", ADMIN_PASSWORD="Door#Pass1", ADMIN_PANEL_PASSWORD="Panel#Pass1",
                  ADMIN_EMAIL="admin@example.com", ADMIN_SECURITY_ANSWER="blue")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def token(client, url="/login"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(url).get_data(as_text=True)).group(1)


@pytest.fixture(scope="session")
def app():
    from app import app as flask_app
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()
