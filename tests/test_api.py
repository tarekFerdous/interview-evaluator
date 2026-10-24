"""API tests with a fake Anthropic client, so they run without an API key or network."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from interview_evaluator.api import app, get_client
from interview_evaluator.models import CompetencyScore, Evaluation

SAMPLE = json.loads((Path(__file__).parent.parent / "examples" / "sample_request.json").read_text())

CANNED = Evaluation(
    scores=[CompetencyScore(competency="Python", score=4, evidence="FastAPI service", reasoning="ok")],
    strengths=["Shipped end to end"],
    concerns=["Passive on disagreement"],
    red_flags=[],
    follow_up_questions=["Tell me about a time you pushed back."],
    recommendation="yes",
    summary="Solid builder.",
)


class FakeMessages:
    def __init__(self, stop_reason="end_turn", parsed=CANNED):
        self.stop_reason, self.parsed, self.calls = stop_reason, parsed, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason, stop_details=None, parsed_output=self.parsed
        )


def client_with(messages: FakeMessages) -> TestClient:
    fake = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    app.dependency_overrides[get_client] = lambda: fake
    return TestClient(app)


def test_evaluate_returns_structured_evaluation():
    messages = FakeMessages()
    res = client_with(messages).post("/evaluate", json=SAMPLE)

    assert res.status_code == 200
    assert res.json()["recommendation"] == "yes"
    sent = messages.calls[0]["messages"][0]["content"]
    assert "<transcript>" in sent and "I usually just go with what the lead says." in sent


def test_refusal_maps_to_422():
    res = client_with(FakeMessages(stop_reason="refusal", parsed=None)).post("/evaluate", json=SAMPLE)
    assert res.status_code == 422


def test_rejects_empty_transcript():
    res = client_with(FakeMessages()).post("/evaluate", json={**SAMPLE, "transcript": []})
    assert res.status_code == 422
