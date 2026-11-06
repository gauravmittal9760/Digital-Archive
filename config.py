"""Environment-based configuration for Digital Archive.

Every secret lives in the `.env` file (never in code). See `.env.example` for the
full list of variable names.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- core
APP_ENV = _str("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV == "production"
SECRET_KEY = _str("SECRET_KEY")
PUBLIC_BASE_URL = _str("PUBLIC_BASE_URL").rstrip("/")
MAX_CONTENT_LENGTH_MB = _int("MAX_CONTENT_LENGTH_MB", 110)

if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY is required in production. Set it in your environment.")
    SECRET_KEY = "local-dev-only-change-this-secret-key-please"

# ----------------------------------------------------------------------- database
DATABASE_URL = _str("DATABASE_URL", "sqlite:///digital_archive.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

if IS_PRODUCTION and not DATABASE_URL.startswith("postgresql"):
    raise RuntimeError("DATABASE_URL must point to PostgreSQL (Neon) in production.")

# --------------------------------------------------------------------- cloudinary
CLOUDINARY_CLOUD_NAME = _str("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = _str("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = _str("CLOUDINARY_API_SECRET")

# ------------------------------------------------------------------ Brevo (e-mail)
BREVO_API_KEY = _str("BREVO_API_KEY")
BREVO_SENDER_EMAIL = _str("BREVO_SENDER_EMAIL")
BREVO_SENDER_NAME = _str("BREVO_SENDER_NAME", "Digital Archive")

# ------------------------------------------------- optional FREE vision AI (Gemini)
GEMINI_API_KEY = _str("GEMINI_API_KEY")
GEMINI_MODEL = _str("GEMINI_MODEL", "gemini-2.5-flash")
ENABLE_OCR = _bool("ENABLE_OCR", False)  # local Tesseract OCR, only if installed

# -------------------------------------------------------------------- admin panel
ADMIN_USERNAME = _str("ADMIN_USERNAME")
ADMIN_PASSWORD = _str("ADMIN_PASSWORD")
ADMIN_PANEL_PASSWORD = _str("ADMIN_PANEL_PASSWORD")
ADMIN_EMAIL = _str("ADMIN_EMAIL")
ADMIN_SECURITY_QUESTION = _str("ADMIN_SECURITY_QUESTION", "What is your admin recovery phrase?")
ADMIN_SECURITY_ANSWER = _str("ADMIN_SECURITY_ANSWER")
ADMIN_ENABLED = bool(ADMIN_USERNAME and ADMIN_PASSWORD and ADMIN_PANEL_PASSWORD)
ADMIN_IDLE_MINUTES = _int("ADMIN_IDLE_MINUTES", 30)

# ------------------------------------------------------------------ policy limits
USER_STORAGE_QUOTA_MB = _int("USER_STORAGE_QUOTA_MB", 500)
TRASH_RETENTION_DAYS = _int("TRASH_RETENTION_DAYS", 30)
LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCK_MINUTES = _int("LOGIN_LOCK_MINUTES", 15)
OTP_TTL_MINUTES = _int("OTP_TTL_MINUTES", 10)
OTP_RESEND_SECONDS = _int("OTP_RESEND_SECONDS", 60)
OTP_MAX_VERIFY_ATTEMPTS = _int("OTP_MAX_VERIFY_ATTEMPTS", 5)
OTP_HOURLY_LIMIT = _int("OTP_HOURLY_LIMIT", 5)

# Secret used by an external scheduler (GitHub Actions / cron-job.org) to trigger
# the global trash purge even when nobody logs in.
CRON_SECRET = _str("CRON_SECRET")
