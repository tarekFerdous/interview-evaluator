import psycopg
import pytest
from fastapi.testclient import TestClient

from interview_evaluator.api import app
from interview_evaluator.db import MIGRATIONS_DIR, migrate


def test_migrations_twice_is_a_noop(db_url):
    assert migrate(db_url) == []  # the session fixture already applied everything
    with psycopg.connect(db_url) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        has_vector = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert versions == sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
    assert has_vector


def test_startup_fails_fast_when_database_unreachable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/nothing")
    with pytest.raises(RuntimeError, match="Database unreachable"):
        with TestClient(app):
            pass


def test_startup_migrates_when_database_reachable(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
