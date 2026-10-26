"""API tests with a fake Anthropic client, so they run without an API key or network."""

import json
from pathlib import Path

from fakes import FakeMessages, fake_client
from fastapi.testclient import TestClient

from interview_evaluator.api import app, get_client

SAMPLE = json.loads((Path(__file__).parent.parent / "examples" / "sample_request.json").read_text())


def client_with(messages: FakeMessages) -> TestClient:
    app.dependency_overrides[get_client] = lambda: fake_client(messages)
    return TestClient(app)


def test_evaluate_returns_structured_evaluation():
    messages = FakeMessages()
    res = client_with(messages).post("/evaluate", json=SAMPLE)

    assert res.status_code == 200
    assert res.json()["evaluation"]["recommendation"] == "yes"
    assert res.json()["id"] is None  # no persistence configured
    sent = "".join(block["text"] for block in messages.calls[0]["messages"][0]["content"])
    assert "<transcript>" in sent and "I usually just go with what the lead says." in sent


def test_request_caches_system_and_prefix_with_transcript_last():
    messages = FakeMessages()
    client_with(messages).post("/evaluate", json=SAMPLE)
    call = messages.calls[0]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    prefix, transcript = call["messages"][0]["content"]
    assert prefix["cache_control"] == {"type": "ephemeral"} and "<rubric>" in prefix["text"]
    assert "cache_control" not in transcript and transcript["text"].startswith("<transcript>")


def test_refusal_maps_to_422():
    res = client_with(FakeMessages(stop_reason="refusal", parsed=None)).post("/evaluate", json=SAMPLE)
    assert res.status_code == 422


def test_rejects_empty_transcript():
    res = client_with(FakeMessages()).post("/evaluate", json={**SAMPLE, "transcript": []})
    assert res.status_code == 422


LONG_RUBRIC = [
    "Root-cause analysis: breaks a problem into testable hypotheses",
    "SQL and data wrangling: cleans and joins messy operational data",
]


def test_shortened_competency_names_come_back_as_the_rubric_text():
    from fakes import evaluation

    messages = FakeMessages(parsed=evaluation({"Root-cause analysis": 4, "sql and data wrangling": 5}))
    res = client_with(messages).post("/evaluate", json=SAMPLE | {"rubric": LONG_RUBRIC})
    assert [s["competency"] for s in res.json()["evaluation"]["scores"]] == LONG_RUBRIC


def test_match_competency():
    from interview_evaluator.evaluator import match_competency

    assert match_competency(LONG_RUBRIC[0], LONG_RUBRIC) == LONG_RUBRIC[0]
    assert match_competency("Root-cause analysis", LONG_RUBRIC) == LONG_RUBRIC[0]
    assert match_competency("Root cause analysis", LONG_RUBRIC) == LONG_RUBRIC[0]
    assert match_competency("SQL and data", LONG_RUBRIC) == LONG_RUBRIC[1]
    assert match_competency("Juggling", LONG_RUBRIC) is None
    assert match_competency("", LONG_RUBRIC) is None
