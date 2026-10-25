"""Postgres + pgvector: connections and idempotent plain-SQL migrations applied at startup.

Persistence is optional: with DATABASE_URL unset the API still evaluates, but nothing is stored
and the rubric/evaluation endpoints answer 503.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
from fastapi import Depends, HTTPException
from pgvector.psycopg import register_vector

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK = 72_640_001  # pg_advisory_lock key: one migrator at a time


def get_database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or None


def connect(url: str) -> psycopg.Connection:
    """Autocommit connection (each statement commits; wrap multi-statement work in
    conn.transaction()), so writes are durable before the response is sent."""
    conn = psycopg.connect(url, connect_timeout=10, autocommit=True)
    register_vector(conn)
    return conn


def migrate(url: str) -> list[str]:
    """Applies migrations/*.sql not yet recorded in schema_migrations, in name order, each in its
    own transaction. Returns the versions applied (empty when already up to date)."""
    applied = []
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.stem in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied.append(path.stem)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    return applied


def get_db(url: str | None = Depends(get_database_url)) -> Iterator[psycopg.Connection]:
    """One connection per request."""
    if url is None:
        raise HTTPException(503, "Persistence is not configured (DATABASE_URL is unset)")
    with connect(url) as conn:
        yield conn


def get_optional_db(url: str | None = Depends(get_database_url)) -> Iterator[psycopg.Connection | None]:
    """Like get_db, but yields None when persistence is off (for endpoints that work without it)."""
    if url is None:
        yield None
        return
    with connect(url) as conn:
        yield conn
