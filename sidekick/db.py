"""PostgreSQL / AlloyDB connectivity, schema creation, and transactional connection helper.

    Supports ``DATABASE_URL`` (psycopg3) or AlloyDB Connector (``ALLOYDB_*`` env vars).
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from functools import lru_cache
from threading import Lock
from typing import Any, Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

_connector = None
_schema_lock = Lock()
_schema_ready = False


def _postgres_url_for_psycopg3(database_url: str) -> str:
    """Normalize a database URL to use the psycopg3 SQLAlchemy driver.

    Args:
        database_url (str): SQLAlchemy-style URL, often ``postgresql://...``.

    Returns:
        str: Rendered URL using ``postgresql+psycopg`` when applicable.
    """
    url = make_url(database_url)
    if url.drivername in ("postgresql", "postgresql+psycopg2"):
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def _build_engine() -> Engine:
    """Create a SQLAlchemy engine from ``DATABASE_URL`` or AlloyDB Connector env vars.

    Raises:
        RuntimeError: If neither ``DATABASE_URL`` nor a complete AlloyDB configuration is set.

    Returns:
        Engine: Configured SQLAlchemy engine with connection pooling.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        return create_engine(
            _postgres_url_for_psycopg3(database_url),
            pool_pre_ping=True,
            pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
            max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
        )

    instance_uri = os.environ.get("ALLOYDB_INSTANCE_URI", "").strip()
    if not instance_uri:
        raise RuntimeError(
            "Set DATABASE_URL for PostgreSQL/AlloyDB, or ALLOYDB_INSTANCE_URI + "
            "ALLOYDB_USER + ALLOYDB_DB (and optional ALLOYDB_PASSWORD / IAM)."
        )

    from google.cloud.alloydb.connector import Connector

    user = os.environ["ALLOYDB_USER"]
    db = os.environ["ALLOYDB_DB"]
    password = os.environ.get("ALLOYDB_PASSWORD") or None
    enable_iam = os.environ.get("ALLOYDB_ENABLE_IAM_AUTH", "").lower() in (
        "1",
        "true",
        "yes",
    )

    global _connector
    _connector = Connector()

    def creator():
        kwargs: dict = {
            "user": user,
            "db": db,
            "enable_iam_auth": enable_iam,
        }
        if password is not None:
            kwargs["password"] = password
        return _connector.connect(instance_uri, "pg8000", **kwargs)

    return create_engine(
        "postgresql+pg8000://",
        creator=creator,
        pool_pre_ping=True,
        pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide cached SQLAlchemy engine.

    Returns:
        Engine: Same engine instance for all callers within the process.
    """
    return _build_engine()


def _new_agent_session_id() -> str:
    """Generate a fresh ADK session id for a new chat.

    Returns:
        str: URL-safe random id (~24 chars).
    """
    return "ck_" + secrets.token_urlsafe(18)


def ensure_schema(engine: Engine) -> None:
    """Create Sidekick tables, run lightweight migrations, and create indexes.

    Idempotent and process-cached: subsequent calls in the same process return
    immediately after the first successful run.

    Args:
        engine (Engine): SQLAlchemy engine bound to the application database.

    Returns:
        None
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        use_pgvector = os.environ.get("USE_PGVECTOR", "").lower() in ("1", "true", "yes")
        try:
            dim = int(os.environ.get("SIDEKICK_EMBED_DIM", "768").strip() or "768")
        except Exception:
            dim = 768
        if use_pgvector:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            notes_embedding_vector = f",\n            embedding_vector vector({dim})"
        else:
            notes_embedding_vector = ""

        ddl = f"""
        CREATE TABLE IF NOT EXISTS sidekick_chats (
            id BIGSERIAL PRIMARY KEY,
            owner_sub TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'New chat',
            agent_session_id TEXT NOT NULL,
            is_inbox BOOLEAN NOT NULL DEFAULT FALSE,
            archived_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS sidekick_google_oauth (
            owner_sub TEXT PRIMARY KEY,
            refresh_token TEXT,
            access_token TEXT,
            expires_at TIMESTAMPTZ,
            scope TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS sidekick_tasks (
            id SERIAL PRIMARY KEY,
            owner_sub TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            due_at TIMESTAMPTZ,
            google_task_id TEXT,
            google_tasklist_id TEXT,
            google_quick_link TEXT,
            chat_id BIGINT REFERENCES sidekick_chats(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS sidekick_calendar_events (
            id SERIAL PRIMARY KEY,
            owner_sub TEXT NOT NULL,
            title TEXT NOT NULL,
            start_at TIMESTAMPTZ NOT NULL,
            end_at TIMESTAMPTZ,
            notes TEXT,
            google_event_id TEXT,
            google_quick_link TEXT,
            chat_id BIGINT REFERENCES sidekick_chats(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS sidekick_notes (
            id SERIAL PRIMARY KEY,
            owner_sub TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            google_doc_id TEXT,
            google_quick_link TEXT,
            chat_id BIGINT REFERENCES sidekick_chats(id) ON DELETE SET NULL,
            embedding BYTEA{notes_embedding_vector},
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS sidekick_chat_messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL REFERENCES sidekick_chats(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content_ciphertext BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS sidekick_chat_resource_access (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL REFERENCES sidekick_chats(id) ON DELETE CASCADE,
            resource_type TEXT NOT NULL,
            resource_id BIGINT NOT NULL,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            granted_from_chat_id BIGINT REFERENCES sidekick_chats(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS sidekick_chat_mcp_access (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL REFERENCES sidekick_chats(id) ON DELETE CASCADE,
            mcp_prefix TEXT NOT NULL,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS sidekick_memory (
            id BIGSERIAL PRIMARY KEY,
            owner_sub TEXT NOT NULL,
            text_ciphertext BYTEA NOT NULL,
            embedding BYTEA NOT NULL,
            source_kind TEXT NOT NULL DEFAULT 'manual',
            source_chat_id BIGINT REFERENCES sidekick_chats(id) ON DELETE SET NULL,
            importance INT NOT NULL DEFAULT 2,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMPTZ,
            use_count INT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sidekick_run_telemetry (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL REFERENCES sidekick_chats(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL,
            run_path TEXT NOT NULL DEFAULT 'run',
            request_payload_json TEXT,
            response_payload_json TEXT,
            raw_events_json TEXT NOT NULL,
            timeline_json TEXT NOT NULL,
            assistant_text TEXT,
            user_text TEXT,
            status_code INT NOT NULL DEFAULT 200,
            duration_ms INT NOT NULL DEFAULT 0,
            event_count INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
        with engine.begin() as conn:
            conn.execute(text(ddl))
            if use_pgvector:
                # Existing deployments created sidekick_notes before pgvector support;
                # CREATE TABLE IF NOT EXISTS does not add new columns, so ensure the
                # column exists before building the ivfflat index on it.
                conn.execute(
                    text(
                        f"ALTER TABLE sidekick_notes "
                        f"ADD COLUMN IF NOT EXISTS embedding_vector vector({dim})"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_sidekick_notes_embedding_vector "
                        "ON sidekick_notes USING ivfflat (embedding_vector vector_l2_ops)"
                    )
                )
            index_specs = (
                ("sidekick_tasks", "idx_sidekick_tasks_owner_created", "owner_sub, created_at DESC"),
                (
                    "sidekick_calendar_events",
                    "idx_sidekick_calendar_owner_start",
                    "owner_sub, start_at DESC",
                ),
                ("sidekick_notes", "idx_sidekick_notes_owner_created", "owner_sub, created_at DESC"),
                ("sidekick_tasks", "idx_sidekick_tasks_chat", "chat_id"),
                ("sidekick_calendar_events", "idx_sidekick_calendar_chat", "chat_id"),
                ("sidekick_notes", "idx_sidekick_notes_chat", "chat_id"),
                ("sidekick_chats", "idx_chats_owner_updated", "owner_sub, updated_at DESC"),
                (
                    "sidekick_chat_messages",
                    "idx_chat_messages_chat_created",
                    "chat_id, created_at",
                ),
                (
                    "sidekick_memory",
                    "idx_memory_owner_created",
                    "owner_sub, created_at DESC",
                ),
                (
                    "sidekick_run_telemetry",
                    "idx_run_telemetry_chat_created",
                    "chat_id, created_at DESC",
                ),
            )
            for table, idx_name, cols in index_specs:
                conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})")
                )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_chats_inbox_uniq "
                    "ON sidekick_chats (owner_sub) WHERE is_inbox"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_grants_unique "
                    "ON sidekick_chat_resource_access "
                    "(chat_id, resource_type, resource_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_mcp_grants_unique "
                    "ON sidekick_chat_mcp_access (chat_id, mcp_prefix)"
                )
            )
        _schema_ready = True


@contextmanager
def db_connection() -> Generator:
    """Provide a database connection inside a transaction with schema ensured.

    Yields:
        Connection: SQLAlchemy connection from ``engine.begin()`` after ``ensure_schema`` runs.
    """
    engine = get_engine()
    ensure_schema(engine)
    with engine.begin() as conn:
        yield conn


def new_chat(
    conn: Any,
    owner_sub: str,
    title: str = "New chat",
) -> dict[str, Any]:
    """Insert a fresh non-inbox chat for ``owner_sub`` and return its row.

    Args:
        conn (Any): Active SQLAlchemy connection.
        owner_sub (str): Authenticated user id.
        title (str): Initial display title.

    Returns:
        dict[str, Any]: Row dict with chat metadata.
    """
    row = conn.execute(
        text(
            "INSERT INTO sidekick_chats "
            "(owner_sub, title, agent_session_id) "
            "VALUES (:owner, :title, :sid) "
            "RETURNING id, owner_sub, title, agent_session_id, "
            "archived_at, created_at, updated_at"
        ),
        {"owner": owner_sub, "title": title, "sid": _new_agent_session_id()},
    ).first()
    return dict(row._mapping)


def get_chat(
    conn: Any, chat_id: int, owner_sub: str
) -> dict[str, Any] | None:
    """Fetch a chat row owned by ``owner_sub``.

    Args:
        conn (Any): Active SQLAlchemy connection.
        chat_id (int): Chat primary key.
        owner_sub (str): Authenticated user id.

    Returns:
        dict[str, Any] | None: Row mapping, or ``None`` if not found / not owned.
    """
    row = conn.execute(
        text(
            "SELECT id, owner_sub, title, agent_session_id, "
            "archived_at, created_at, updated_at "
            "FROM sidekick_chats "
            "WHERE id = :id AND owner_sub = :owner"
        ),
        {"id": chat_id, "owner": owner_sub},
    ).first()
    return dict(row._mapping) if row is not None else None


def touch_chat(conn: Any, chat_id: int) -> None:
    """Update ``updated_at`` on a chat (used to bump sidebar ordering).

    Args:
        conn (Any): Active SQLAlchemy connection.
        chat_id (int): Chat primary key.

    Returns:
        None
    """
    conn.execute(
        text("UPDATE sidekick_chats SET updated_at = NOW() WHERE id = :id"),
        {"id": chat_id},
    )
