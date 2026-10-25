from fakes import FakeMessages, evaluation, fake_client
from fastapi.testclient import TestClient
from test_api import SAMPLE

from interview_evaluator.api import app, get_client, get_session_store
from interview_evaluator.security import RateLimiter, get_rate_limiter, get_service_api_key
from interview_evaluator.sessions import InMemorySessionStore

CONTEXT = {"job_description": SAMPLE["job_description"], "rubric": SAMPLE["rubric"]}
QAS = SAMPLE["transcript"]


def sent_qa_count(call: dict) -> int:
    return call["messages"][0]["content"][1]["text"].count("\nA")


def session_client(messages: FakeMessages | None = None) -> tuple[TestClient, FakeMessages]:
    messages = messages or FakeMessages()
    app.dependency_overrides[get_client] = lambda: fake_client(messages)
    return TestClient(app), messages


def start(client: TestClient) -> str:
    res = client.post("/sessions", json=CONTEXT)
    assert res.status_code == 201
    return res.json()["session_id"]


def test_lifecycle_scores_after_each_qa_and_end_returns_final():
    client, messages = session_client(FakeMessages(parsed=[evaluation({"Python": 2}), evaluation({"Python": 4})]))
    sid = start(client)

    assert client.post(f"/sessions/{sid}/qa", json=QAS[0]).status_code == 202
    state = client.get(f"/sessions/{sid}").json()
    assert state["evaluated_qa_count"] == 1 and len(state["transcript"]) == 1
    assert state["latest_evaluation"]["scores"][0]["score"] == 2
    assert state["rubric"] == CONTEXT["rubric"]

    client.post(f"/sessions/{sid}/qa", json=QAS[1])
    state = client.get(f"/sessions/{sid}").json()
    assert state["evaluated_qa_count"] == 2
    assert [sent_qa_count(c) for c in messages.calls] == [1, 2]

    final = client.post(f"/sessions/{sid}/end")
    assert final.status_code == 200
    assert final.json()["evaluation"]["scores"][0]["score"] == 4
    assert len(messages.calls) == 2  # latest scorecard already covered every Q&A

    assert client.get(f"/sessions/{sid}").status_code == 404
    assert client.post(f"/sessions/{sid}/qa", json=QAS[2]).status_code == 404
    assert client.post(f"/sessions/{sid}/end").status_code == 404


def test_end_reevaluates_when_latest_scorecard_is_stale():
    error = FakeMessages(parsed=[RuntimeError("boom"), evaluation({"Python": 3})])
    client, messages = session_client(error)
    sid = start(client)
    client.post(f"/sessions/{sid}/qa", json=QAS[0])  # background scoring fails
    assert client.get(f"/sessions/{sid}").json()["evaluated_qa_count"] == 0
    final = client.post(f"/sessions/{sid}/end")
    assert final.status_code == 200 and final.json()["evaluation"]["scores"][0]["score"] == 3


def test_end_without_qa_is_422():
    client, _ = session_client()
    assert client.post(f"/sessions/{start(client)}/end").status_code == 422


def test_unknown_session_is_404():
    client, _ = session_client()
    assert client.get("/sessions/nope").status_code == 404
    assert client.post("/sessions/nope/qa", json=QAS[0]).status_code == 404


def test_session_expires_after_two_hours_of_inactivity():
    now = [0.0]
    store = InMemorySessionStore(clock=lambda: now[0])
    app.dependency_overrides[get_session_store] = lambda: store
    client, _ = session_client()
    sid = start(client)

    now[0] += 60 * 60  # 1h: still alive, and this access resets the timer
    assert client.get(f"/sessions/{sid}").status_code == 200
    now[0] += 2 * 60 * 60 - 1
    assert client.get(f"/sessions/{sid}").status_code == 200
    now[0] += 2 * 60 * 60 + 1
    assert client.get(f"/sessions/{sid}").status_code == 404


def test_31st_qa_is_422():
    client, _ = session_client()
    sid = start(client)
    for _ in range(30):
        assert client.post(f"/sessions/{sid}/qa", json=QAS[0]).status_code == 202
    res = client.post(f"/sessions/{sid}/qa", json=QAS[0])
    assert res.status_code == 422 and "30" in res.json()["detail"]


def test_session_endpoints_require_key_and_are_rate_limited():
    client, _ = session_client()
    app.dependency_overrides[get_service_api_key] = lambda: "k"
    assert client.post("/sessions", json=CONTEXT).status_code == 401
    assert client.get("/sessions/whatever").status_code == 401

    limiter = RateLimiter(2)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    headers = {"X-API-Key": "k"}
    sid = client.post("/sessions", json=CONTEXT, headers=headers).json()["session_id"]
    assert client.get(f"/sessions/{sid}", headers=headers).status_code == 200
    assert client.get(f"/sessions/{sid}", headers=headers).status_code == 429


def test_one_shot_evaluate_still_works():
    client, _ = session_client()
    assert client.post("/evaluate", json=SAMPLE).json()["evaluation"]["recommendation"] == "yes"
