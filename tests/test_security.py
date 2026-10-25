import logging

from fakes import FakeMessages, fake_client
from fastapi.testclient import TestClient
from test_api import SAMPLE

from interview_evaluator.api import app, get_client
from interview_evaluator.security import RateLimiter, get_rate_limiter, get_service_api_key

KEY = "s3cret-key"


def make_client(api_key: str | None = KEY) -> TestClient:
    app.dependency_overrides[get_client] = lambda: fake_client(FakeMessages())
    app.dependency_overrides[get_service_api_key] = lambda: api_key
    return TestClient(app)


def test_missing_key_is_401():
    res = make_client().post("/evaluate", json=SAMPLE)
    assert res.status_code == 401
    assert "X-API-Key" in res.json()["detail"]


def test_wrong_key_is_401():
    res = make_client().post("/evaluate", json=SAMPLE, headers={"X-API-Key": "nope"})
    assert res.status_code == 401


def test_correct_key_is_200():
    res = make_client().post("/evaluate", json=SAMPLE, headers={"X-API-Key": KEY})
    assert res.status_code == 200


def test_health_and_docs_need_no_key():
    client = make_client()
    for path in ("/health", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 200, path
    assert client.get("/", follow_redirects=False).status_code == 307


def test_auth_disabled_without_configured_key_and_warns(caplog, monkeypatch):
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    del app.dependency_overrides[get_service_api_key]  # use the real env lookup
    app.dependency_overrides[get_client] = lambda: fake_client(FakeMessages())
    with caplog.at_level(logging.WARNING), TestClient(app) as client:  # `with` runs the lifespan
        assert client.post("/evaluate", json=SAMPLE).status_code == 200
    assert "auth is DISABLED" in caplog.text


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def limited_client(per_minute: int, clock: FakeClock) -> TestClient:
    limiter = RateLimiter(per_minute, clock=clock)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    return make_client(api_key=None)


def test_request_over_limit_is_429_with_retry_after():
    clock = FakeClock()
    client = limited_client(3, clock)
    for _ in range(3):
        assert client.post("/evaluate", json=SAMPLE).status_code == 200
    res = client.post("/evaluate", json=SAMPLE)
    assert res.status_code == 429
    assert res.headers["Retry-After"] == "20"  # 3/min refills one token every 20s

    clock.now += 20
    assert client.post("/evaluate", json=SAMPLE).status_code == 200


def test_forwarded_ips_have_independent_buckets():
    client = limited_client(1, FakeClock())
    a = {"X-Forwarded-For": "203.0.113.1, 10.0.0.1"}
    b = {"X-Forwarded-For": "203.0.113.2, 10.0.0.1"}
    assert client.post("/evaluate", json=SAMPLE, headers=a).status_code == 200
    assert client.post("/evaluate", json=SAMPLE, headers=a).status_code == 429
    assert client.post("/evaluate", json=SAMPLE, headers=b).status_code == 200


def test_health_is_not_rate_limited():
    client = limited_client(1, FakeClock())
    for _ in range(5):
        assert client.get("/health").status_code == 200
