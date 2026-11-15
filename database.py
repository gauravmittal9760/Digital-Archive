"""Database layer: SQLAlchemy Core tables, engine, safe forward-only migrations.

* Neon PostgreSQL in production, SQLite for zero-config local development.
* No Supabase, no compatibility adapters: routes talk to SQLAlchemy directly.
* All datetimes are stored and returned as timezone-aware UTC (see UTCDateTime).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
                        MetaData, String, Table, Text, create_engine, event, false, inspect,
                        text, true)
from sqlalchemy.types import TypeDecorator

from config import DATABASE_URL


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def aware(value):
    """Return `value` as an aware UTC datetime (naive values are treated as UTC)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on both PostgreSQL and SQLite."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return aware(value)

    def process_result_value(self, value, dialect):
        return aware(value)


metadata = MetaData()


def _ts(name="created_at", nullable=False, default=True):
    kwargs = {"nullable": nullable}
    if default:
        kwargs["default"] = utcnow
    return Column(name, UTCDateTime, **kwargs)


# ----------------------------------------------------------------------- accounts
users = Table(
    "users", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("email", String(320), nullable=False, index=True),          # NOT unique: one e-mail may own many usernames
    Column("password_hash", String(255), nullable=False),
    Column("security_question", String(255)),
    Column("security_answer_hash", String(255)),
    Column("session_version", Integer, nullable=False, server_default="0"),
    Column("is_blocked", Boolean, nullable=False, server_default=false()),
    Column("blocked_reason", Text),
    Column("blocked_at", UTCDateTime),
    Column("locked_until", UTCDateTime),                                # temporary lock after wrong passwords
    Column("theme", String(10), nullable=False, server_default="light"),
    Column("ai_enabled", Boolean, nullable=False, server_default=true()),
    Column("two_step_enabled", Boolean, nullable=False, server_default=false()),
    Column("last_login_at", UTCDateTime),
    Column("reminders_enabled", Boolean, nullable=False, server_default=true()),   # document-expiry e-mails
    Column("vault_salt", String(64)),                                  # Private Vault: key material (never the passphrase)
    Column("vault_wrapped_key", Text),                                 # data key wrapped by the passphrase
    Column("vault_recovery_wrapped", Text),                            # data key wrapped by the one-time recovery key
    _ts("created_at"), _ts("updated_at"),
)

profiles = Table(
    "profiles", metadata,
    Column("id", String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("username", String(50), nullable=False, unique=True, index=True),
    Column("display_name", String(100), nullable=False, default=""),
    Column("avatar_url", Text),
    Column("bio", Text),
    _ts("created_at"), _ts("updated_at"),
)

# ------------------------------------------------------------------------- media
media = Table(
    "media", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("file_name", String(255), nullable=False),
    Column("storage_path", Text, nullable=False),
    Column("thumb_path", Text),
    Column("media_type", String(30), nullable=False),                  # photo | video | document
    Column("category", String(100), server_default="other"),
    Column("mime_type", String(255)),
    Column("file_size", BigInteger, server_default="0"),                # bytes actually stored (after compression)
    Column("thumb_size", BigInteger, server_default="0"),
    Column("original_size", BigInteger),                                # bytes the user uploaded
    Column("width", Integer), Column("height", Integer),
    Column("description", Text),
    Column("duration_seconds", Float),
    Column("captured_at", UTCDateTime),
    Column("location_name", String(255)),
    Column("extracted_text", Text),
    Column("ai_caption", Text),
    Column("ai_category", String(40)),
    Column("ai_confidence", Float),
    Column("ai_labels", Text),
    Column("ai_source", String(30)),
    Column("ai_status", String(16), server_default="none"),            # none | pending | done | failed
    Column("category_locked", Boolean, nullable=False, server_default=false()),
    Column("review_dismissed", Boolean, nullable=False, server_default=false()),
    Column("doc_type", String(40)),                                    # detected document type (Aadhaar, Passport, Marksheet ...)
    Column("doc_title", String(200)),
    Column("issue_date", Date),
    Column("expiry_date", Date),
    Column("expiry_notice", Integer, nullable=False, server_default="0"),   # highest reminder stage already e-mailed
    Column("is_favorite", Boolean, nullable=False, server_default=false()),
    Column("is_deleted", Boolean, nullable=False, server_default=false()),
    Column("deleted_at", UTCDateTime),
    Column("tags", Text),
    Column("checksum", String(64)),
    Column("orig_checksum", String(64), index=True),
    Column("phash", String(16)),
    _ts("created_at"), _ts("updated_at"),
)
Index("ix_media_user_deleted", media.c.user_id, media.c.is_deleted)
Index("ix_media_deleted_at", media.c.is_deleted, media.c.deleted_at)

albums = Table(
    "albums", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("name", String(150), nullable=False),
    Column("description", Text),
    Column("cover_media_id", String(36)),
    _ts("created_at"), _ts("updated_at"),
)

album_media = Table(
    "album_media", metadata,
    Column("album_id", String(36), ForeignKey("albums.id", ondelete="CASCADE"), primary_key=True),
    Column("media_id", String(36), ForeignKey("media.id", ondelete="CASCADE"), primary_key=True),
    _ts("created_at"),
)

file_versions = Table(
    "file_versions", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("media_id", String(36), ForeignKey("media.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("storage_path", Text, nullable=False),
    Column("file_size", BigInteger, server_default="0"),
    Column("file_name", String(255)), Column("mime_type", String(255)),
    Column("checksum", String(64)), Column("width", Integer), Column("height", Integer),
    Column("extracted_text", Text),
    _ts("created_at"),
)

share_links = Table(
    "share_links", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("token_hash", String(64), nullable=False, unique=True, index=True),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("media_id", String(36), ForeignKey("media.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("revoked_at", UTCDateTime),
    Column("password_hash", String(255)),                              # optional link password
    Column("allow_download", Boolean, nullable=False, server_default=true()),
    Column("view_count", Integer, nullable=False, server_default="0"),
    Column("last_accessed_at", UTCDateTime),
    _ts("created_at"),
)

share_access = Table(       # who opened a link (access history)
    "share_access", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("share_id", String(36), ForeignKey("share_links.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("action", String(20), nullable=False),                      # view | download | denied | password_failed
    Column("ip_address", String(64)),
    Column("user_agent", String(255)),
    _ts("created_at"),
)

vault_items = Table(        # Private Vault: AES-GCM encrypted blobs; names are visible only inside the unlocked vault
    "vault_items", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("file_name", String(255), nullable=False),
    Column("mime_type", String(255)),
    Column("plain_size", BigInteger, server_default="0"),
    Column("file_size", BigInteger, server_default="0"),               # encrypted bytes stored
    Column("storage_path", Text, nullable=False),
    _ts("created_at"),
)

vault_sessions = Table(     # unlocked-vault sessions shared by all workers; the data key is encrypted with a server-side key
    "vault_sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("wrapped_key", Text, nullable=False),
    _ts("expires_at", default=False),
    _ts("created_at"),
)

storage_cleanup = Table(    # objects that must be deleted from storage; written in the same transaction as the DB change
    "storage_cleanup", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("storage_path", Text, nullable=False),
    Column("attempts", Integer, server_default="0"),
    _ts("created_at"),
)

# ------------------------------------------------------- history / audit / security
activity_logs = Table(      # visible to the user ("Activity history")
    "activity_logs", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("action", String(100), nullable=False),
    Column("details", Text),
    _ts("created_at"),
)

account_events = Table(     # visible to the admin; survives account deletion (no FK on purpose)
    "account_events", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("user_id", String(36), index=True),
    Column("username", String(50)),
    Column("email", String(320)),
    Column("event_type", String(60), nullable=False, index=True),
    Column("details", Text),
    Column("ip_address", String(64)),
    _ts("created_at"),
)
Index("ix_account_events_created", account_events.c.created_at)

login_history = Table(
    "login_history", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("success", Boolean, nullable=False, server_default=true()),
    Column("ip_address", String(64)),
    Column("user_agent", String(255)),
    _ts("created_at"),
)

otp_codes = Table(
    "otp_codes", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("purpose", String(30), nullable=False),                     # reset_password | forgot_username | email_change | admin_reset | login_2fa | enable_2fa
    Column("email", String(320), nullable=False, index=True),
    Column("user_id", String(36)),
    Column("code_hash", String(64), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("used_at", UTCDateTime),
    Column("ip_address", String(64)),
    _ts("created_at"),
)

rate_limits = Table(
    "rate_limits", metadata,
    Column("key", String(200), primary_key=True),
    Column("count", Integer, nullable=False, server_default="0"),
    Column("window_start", UTCDateTime, nullable=False, default=utcnow),
    Column("blocked_until", UTCDateTime),
)

# ----------------------------------------------------------------------- admin
admin_settings = Table(
    "admin_settings", metadata,
    Column("key", String(80), primary_key=True),
    Column("value", Text),
    _ts("updated_at"),
)

admin_avatars = Table(
    "admin_avatars", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("storage_path", Text, nullable=False),
    Column("file_size", BigInteger, server_default="0"),
    Column("position", Integer, nullable=False, server_default="0"),
    _ts("created_at"),
)

admin_login_records = Table(
    "admin_login_records", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("stage", String(30), nullable=False),                       # gateway | panel | recovery | security
    Column("success", Boolean, nullable=False, server_default=true()),
    Column("details", Text),
    Column("ip_address", String(64)),
    Column("user_agent", String(255)),
    _ts("created_at"),
)

announcements = Table(
    "announcements", metadata,
    Column("id", String(36), primary_key=True, default=new_id),
    Column("message", Text, nullable=False),
    Column("level", String(12), nullable=False, server_default="info"),
    Column("is_active", Boolean, nullable=False, server_default=true()),
    _ts("created_at"),
)

app_state = Table(
    "app_state", metadata,
    Column("key", String(80), primary_key=True),
    Column("value", Text),
    _ts("updated_at"),
)

# ------------------------------------------------------------------------ engine
def postgres_engine_settings(url_string: str):
    """Return (url, connect_args) for a PostgreSQL URL, tuned per driver.

    pg8000 does not understand libpq-only query options that Neon puts in its URL (sslmode, channel_binding...), so
    they are stripped and translated: sslmode other than 'disable' becomes a verified TLS context.
    Small pool: Neon's free tier and 512 MB hosts do not need more.
    """
    from sqlalchemy.engine import make_url
    url = make_url(url_string)
    if url.drivername.endswith("+pg8000"):
        query = dict(url.query)
        sslmode = str(query.pop("sslmode", "require")).lower()
        for key in ("channel_binding", "options", "application_name", "connect_timeout", "sslrootcert", "sslcert",
                    "sslkey", "gssencmode", "target_session_attrs"):
            query.pop(key, None)
        args = {"timeout": 30}
        if sslmode != "disable":
            args["ssl_context"] = True
        return url.set(query=query), args
    # psycopg: prepare_threshold=None keeps it compatible with Neon's pooled (PgBouncer) endpoint.
    return url, {"connect_timeout": 15, "prepare_threshold": None}


def install_liveness_check(eng) -> None:
    """Replace SQLAlchemy's pre-ping with one that also survives raw TLS/socket errors.

    Neon (and NAT gateways) silently kill idle connections. With pg8000 the dead socket can surface as
    ssl.SSLError / OSError, which SQLAlchemy's built-in pre-ping does not treat as a disconnect, so the error
    reached the request. Raising DisconnectionError from a checkout hook makes the pool discard the dead
    connection and transparently open a fresh one.
    """
    from sqlalchemy import exc as sa_exc

    @event.listens_for(eng, "checkout")
    def _liveness(dbapi_connection, _record, _proxy):
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SELECT 1")
            finally:
                cursor.close()
        except Exception as exc:                          # noqa: BLE001 - any failure here means the socket is unusable
            raise sa_exc.DisconnectionError(str(exc)) from exc


class QuietDeadConnectionLog(logging.Filter):
    """Hide the harmless 'Exception terminating connection' error SQLAlchemy logs when it closes a connection that the
    server (Neon) had already dropped. The pool has already replaced it; the request itself succeeds."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage().startswith("Exception terminating connection") and record.exc_info:
            kind = record.exc_info[0]
            if kind and (issubclass(kind, OSError) or kind.__name__ in {"InterfaceError", "SSLError", "DisconnectionError"}):
                return False
        return True


logging.getLogger("sqlalchemy.pool.impl.QueuePool").addFilter(QuietDeadConnectionLog())

if not DATABASE_URL.startswith("sqlite"):
    _pg_url, _pg_connect_args = postgres_engine_settings(DATABASE_URL)
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30},
                           pool_pre_ping=True, future=True)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
else:
    engine = create_engine(_pg_url, pool_pre_ping=False, pool_size=3, max_overflow=2, pool_recycle=180, future=True,
                           connect_args=_pg_connect_args)
    install_liveness_check(engine)


# --------------------------------------------------------------------- migrations
def _default_sql(column, dialect) -> str:
    server_default = column.server_default
    if server_default is None:
        return ""
    arg = server_default.arg
    if hasattr(arg, "compile"):                       # false() / true()
        return " DEFAULT " + str(arg.compile(dialect=dialect))
    raw = str(arg)
    return " DEFAULT " + (raw if raw.lstrip("-").isdigit() else "'" + raw.replace("'", "''") + "'")


def _sync_columns(conn) -> None:
    """Add any column that the models declare but the live table lacks (forward-only)."""
    inspector = inspect(conn)
    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl_type = column.type.compile(dialect=conn.dialect)
            conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}"
                              f"{_default_sql(column, conn.dialect)}"))


def _relax_email_uniqueness(conn) -> None:
    """Older versions made users.email UNIQUE. One e-mail may now own several usernames."""
    inspector = inspect(conn)
    if not inspector.has_table("users"):
        return
    unique_indexes = [ix for ix in inspector.get_indexes("users") if ix.get("unique") and ix["column_names"] == ["email"]]
    for ix in unique_indexes:
        conn.execute(text(f'DROP INDEX IF EXISTS "{ix["name"]}"'))
        conn.execute(text(f'CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)'))
    if conn.dialect.name == "postgresql":
        for uc in inspector.get_unique_constraints("users"):
            if uc["column_names"] == ["email"] and uc.get("name"):
                conn.execute(text(f'ALTER TABLE users DROP CONSTRAINT IF EXISTS "{uc["name"]}"'))


EXTRA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_vault_sessions_exp ON vault_sessions (expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_media_user_created ON media (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_media_user_category ON media (user_id, category)",
    "CREATE INDEX IF NOT EXISTS ix_media_user_expiry ON media (user_id, expiry_date)",
    "CREATE INDEX IF NOT EXISTS ix_media_ai_status ON media (ai_status)",
    "CREATE INDEX IF NOT EXISTS ix_login_history_user_created ON login_history (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_otp_lookup ON otp_codes (purpose, email, created_at)",
]


def _ensure_indexes(conn) -> None:
    """create_all() never adds indexes to tables that already exist, so do it explicitly (idempotent)."""
    for statement in EXTRA_INDEXES:
        conn.execute(text(statement))


def _initialize_schema() -> None:
    """Create/upgrade the schema once, even when several workers boot together.

    A *transaction-level* advisory lock is used (safe with Neon's pooled endpoint,
    unlike session-level locks).
    """
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(72623859790382856)"))
        metadata.create_all(conn)
        _sync_columns(conn)
        _relax_email_uniqueness(conn)
        _ensure_indexes(conn)


_initialized = False


def init_db() -> None:
    global _initialized
    if not _initialized:
        _initialize_schema()
        _initialized = True
