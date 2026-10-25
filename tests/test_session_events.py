import json
import threading

import anthropic
import httpx
from fakes import FakeMessages, evaluation
from test_sessions import QAS, sent_qa_count, session_client, start

from interview_evaluator.api import app, get_runner, get_session_store
from interview_evaluator.security import get_service_api_key
from interview_evaluator.sessions import run_in_thread, wait_until_idle


def read_event(lines) -> dict:
    """Reads one SSE event (skipping keepalive comments) from an iter_lines() iterator."""
    event = {}
    for line in lines:
        if not line:
            if event:
                return event
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(": ")
        event[field] = value
    raise AssertionError("stream ended before a complete event")


def typed(event: dict) -> tuple[str, dict]:
    return event["event"], json.loads(event["data"])


def test_stream_emits_scoring_then_scorecard_after_each_add_and_ended():
    client, _ = session_client()
    sid = start(client)
    with client.stream("GET", f"/sessions/{sid}/events") as res:
        assert res.headers["content-type"].startswith("text/event-stream")
        lines = res.iter_lines()
        for n in (1, 2):
            client.post(f"/sessions/{sid}/qa", json=QAS[n - 1])
            assert typed(read_event(lines)) == ("scoring", {"qa_count": n})
            kind, data = typed(read_event(lines))
            assert kind == "scorecard" and data["qa_count"] == n
            assert data["evaluation"]["recommendation"] == "yes"
        client.post(f"/sessions/{sid}/end")
        kind, data = typed(read_event(lines))
        assert kind == "ended" and data["qa_count"] == 2
        assert list(lines) == [""] or list(lines) == []  # the stream closes after `ended`


def test_three_quick_adds_coalesce_into_two_evaluations():
    gate, entered = threading.Event(), threading.Semaphore(0)

    class BlockingMessages(FakeMessages):
        def parse(self, **kwargs):
            entered.release()
            assert gate.wait(5)
            return super().parse(**kwargs)

    app.dependency_overrides[get_runner] = lambda: run_in_thread
    client, messages = session_client(BlockingMessages())
    sid = start(client)

    client.post(f"/sessions/{sid}/qa", json=QAS[0])
    assert entered.acquire(timeout=5)  # first evaluation is now in flight
    client.post(f"/sessions/{sid}/qa", json=QAS[1])
    client.post(f"/sessions/{sid}/qa", json=QAS[2])
    gate.set()

    session = app.dependency_overrides[get_session_store]().get(sid)
    assert wait_until_idle(session, timeout=5)
    assert [sent_qa_count(c) for c in messages.calls] == [1, 3]
    assert client.get(f"/sessions/{sid}").json()["evaluated_qa_count"] == 3


def test_llm_error_becomes_error_event_and_session_keeps_working():
    rate_limited = anthropic.RateLimitError(
        "slow down",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com")),
        body=None,
    )
    client, _ = session_client(FakeMessages(parsed=[rate_limited, evaluation({"Python": 4})]))
    sid = start(client)
    with client.stream("GET", f"/sessions/{sid}/events") as res:
        lines = res.iter_lines()
        client.post(f"/sessions/{sid}/qa", json=QAS[0])
        assert typed(read_event(lines)) == ("scoring", {"qa_count": 1})
        kind, data = typed(read_event(lines))
        assert kind == "error" and data["qa_count"] == 1 and data["status"] == 429
        assert "rate limit" in data["detail"].lower()

        assert client.post(f"/sessions/{sid}/qa", json=QAS[1]).status_code == 202
        assert typed(read_event(lines)) == ("scoring", {"qa_count": 2})
        assert typed(read_event(lines))[0] == "scorecard"


def test_reconnect_replays_events_after_last_event_id():
    client, _ = session_client()
    sid = start(client)
    client.post(f"/sessions/{sid}/qa", json=QAS[0])  # scoring (id 1), scorecard (id 2)
    with client.stream("GET", f"/sessions/{sid}/events", headers={"Last-Event-ID": "1"}) as res:
        event = read_event(res.iter_lines())
        assert event["id"] == "2" and event["event"] == "scorecard"


def test_query_key_is_accepted_only_on_the_events_endpoint():
    client, _ = session_client()
    sid = start(client)
    app.dependency_overrides[get_service_api_key] = lambda: "k"

    assert client.get(f"/sessions/{sid}?key=k").status_code == 401
    assert client.post(f"/sessions/{sid}/qa?key=k", json=QAS[0]).status_code == 401
    assert client.get(f"/sessions/{sid}/events").status_code == 401
    with client.stream("GET", f"/sessions/{sid}/events?key=k") as res:
        assert res.status_code == 200
    with client.stream("GET", f"/sessions/{sid}/events", headers={"X-API-Key": "k"}) as res:
        assert res.status_code == 200
