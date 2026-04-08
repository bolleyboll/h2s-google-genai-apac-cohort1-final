"""PostgreSQL / AlloyDB connectivity, schema creation, and transactional connection helper.

    Supports ``DATABASE_URL`` (psycopg3) or AlloyDB Connector (``ALLOYDB_*`` env vars).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

_connector = None


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


def _add_column_if_missing(conn: Any, table: str, column: str, col_type: str) -> None:
    """Add a column to a table if it is missing (idempotent migration helper).

    Args:
        conn (Any): Active SQLAlchemy connection.
        table (str): Table name (unqualified, ``public`` schema).
        column (str): Column name to add.
        col_type (str): PostgreSQL column type SQL fragment (for example ``TEXT``).

    Returns:
        None
    """
    conn.execute(
        text(
            f"""
            DO $mig$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = '{table}'
                  AND column_name = '{column}'
              ) THEN
                ALTER TABLE {table} ADD COLUMN {column} {col_type};
              END IF;
            END $mig$;
            """
        )
    )


def _migrate_owner_sub(conn: Any, table: str) -> None:
    """Ensure ``owner_sub`` exists on ``table`` and backfill legacy rows.

    Args:
        conn (Any): Active SQLAlchemy connection.
        table (str): Sidekick table name.

    Returns:
        None
    """
    conn.execute(
        text(
            f"""
            DO $mig$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = '{table}'
                  AND column_name = 'owner_sub'
              ) THEN
                ALTER TABLE {table} ADD COLUMN owner_sub TEXT;
                UPDATE {table} SET owner_sub = '_legacy_shared' WHERE owner_sub IS NULL;
                ALTER TABLE {table} ALTER COLUMN owner_sub SET NOT NULL;
              END IF;
            END $mig$;
            """
        )
    )


def ensure_schema(engine: Engine) -> None:
    """Create Sidekick tables, run lightweight migrations, and create indexes.

    Args:
        engine (Engine): SQLAlchemy engine bound to the application database.

    Returns:
        None
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS sidekick_tasks (
        id SERIAL PRIMARY KEY,
        owner_sub TEXT NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        due_at TIMESTAMPTZ,
        google_task_id TEXT,
        google_tasklist_id TEXT,
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
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS sidekick_notes (
        id SERIAL PRIMARY KEY,
        owner_sub TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT,
        google_keep_note_name TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS sidekick_google_oauth (
        owner_sub TEXT PRIMARY KEY,
        refresh_token TEXT,
        access_token TEXT,
        expires_at TIMESTAMPTZ,
        scope TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
        for t in (
            "sidekick_tasks",
            "sidekick_calendar_events",
            "sidekick_notes",
        ):
            _migrate_owner_sub(conn, t)
        _add_column_if_missing(conn, "sidekick_tasks", "google_task_id", "TEXT")
        _add_column_if_missing(conn, "sidekick_tasks", "google_tasklist_id", "TEXT")
        _add_column_if_missing(conn, "sidekick_tasks", "google_quick_link", "TEXT")
        _add_column_if_missing(
            conn, "sidekick_calendar_events", "google_event_id", "TEXT"
        )
        _add_column_if_missing(conn, "sidekick_calendar_events", "google_quick_link", "TEXT")
        _add_column_if_missing(
            conn, "sidekick_notes", "google_keep_note_name", "TEXT"
        )
        _add_column_if_missing(conn, "sidekick_notes", "google_quick_link", "TEXT")
        index_specs = (
            ("sidekick_tasks", "idx_sidekick_tasks_owner_created", "owner_sub, created_at DESC"),
            (
                "sidekick_calendar_events",
                "idx_sidekick_calendar_owner_start",
                "owner_sub, start_at DESC",
            ),
            ("sidekick_notes", "idx_sidekick_notes_owner_created", "owner_sub, created_at DESC"),
        )
        for table, idx_name, cols in index_specs:
            conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})")
            )


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
