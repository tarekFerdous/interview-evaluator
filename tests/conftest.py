import os

import psycopg
import pytest

from interview_evaluator.api import app, get_runner, get_session_store
from interview_evaluator.db import get_database_url, migrate
from interview_evaluator.security import RateLimiter, get_rate_limiter, get_service_api_key
from interview_evaluator.sessions import InMemorySessionStore
from interview_evaluator.transcriptions import JobStore, get_job_store

# DB-backed tests run only against this database, which they WIPE. Never point it at real data.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
APP_TABLES = ("evaluations", "rubrics")


@pytest.fixture(scope="session")
def migrated_db() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip(
            "TEST_DATABASE_URL is not set (docker compose up db, then "
            "TEST_DATABASE_URL=postgresql://evaluator:evaluator@localhost:5433/evaluator_test)"
        )
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture
def db_url(migrated_db: str) -> str:
    """A migrated, empty test database, wired into the app."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        existing = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        tables = [t for t in APP_TABLES if t in existing]
        if tables:
            conn.execute(f"TRUNCATE {', '.join(tables)} CASCADE")
    app.dependency_overrides[get_database_url] = lambda: migrated_db
    return migrated_db


@pytest.fixture(autouse=True)
def isolated_app(monkeypatch):
    """Each test starts with auth off, no database, a fresh generous rate limiter and an empty
    session store whose background scoring runs inline (deterministic), whatever .env says."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_database_url] = lambda: None
    app.dependency_overrides[get_service_api_key] = lambda: None
    limiter = RateLimiter(1000)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    store = InMemorySessionStore()
    app.dependency_overrides[get_session_store] = lambda: store
    app.dependency_overrides[get_runner] = lambda: lambda fn: fn()
    jobs = JobStore()
    app.dependency_overrides[get_job_store] = lambda: jobs
    yield
    app.dependency_overrides.clear()
