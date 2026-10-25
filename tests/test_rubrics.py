import uuid

import pytest
from fakes import FakeEmbedder, FakeMessages, fake_client
from fastapi.testclient import TestClient
from test_api import SAMPLE

from interview_evaluator.api import app, get_client
from interview_evaluator.embeddings import get_embedder

BACKEND = {
    "name": "Backend Engineer L4",
    "role_description": "Builds backend Python services on Postgres.",
    "competencies": ["API design", "Databases"],
}
DATA = {
    "name": "Data Engineer L3",
    "role_description": "Owns data pipelines and data quality.",
    "competencies": ["Pipelines", "Data quality"],
}


def make_client(embedder: FakeEmbedder | None = None) -> tuple[TestClient, FakeMessages]:
    messages = FakeMessages()
    app.dependency_overrides[get_client] = lambda: fake_client(messages)
    app.dependency_overrides[get_embedder] = lambda: embedder or FakeEmbedder()
    return TestClient(app), messages


# ---- no database needed


def test_rubric_endpoints_are_503_without_a_database():
    client, _ = make_client()
    assert client.get("/rubrics").status_code == 503
    assert client.post("/rubrics", json=BACKEND).status_code == 503


@pytest.mark.parametrize("change", [{"rubric_id": str(uuid.uuid4())}, {"rubric": None}])
def test_evaluate_needs_exactly_one_rubric_source(change):
    client, _ = make_client()
    res = client.post("/evaluate", json={**SAMPLE, **change})
    assert res.status_code == 422
    assert "exactly one of `rubric` or `rubric_id`" in res.text


# ---- with a database


def test_crud_round_trip_and_duplicate_name(db_url):
    client, _ = make_client()
    created = client.post("/rubrics", json=BACKEND)
    assert created.status_code == 201
    rid = created.json()["id"]
    assert client.get(f"/rubrics/{rid}").json()["competencies"] == BACKEND["competencies"]
    assert [r["name"] for r in client.get("/rubrics").json()] == [BACKEND["name"]]

    assert client.post("/rubrics", json=BACKEND).status_code == 409

    updated = client.put(f"/rubrics/{rid}", json={**BACKEND, "competencies": ["API design"]})
    assert updated.status_code == 200 and updated.json()["competencies"] == ["API design"]
    client.post("/rubrics", json=DATA)
    assert client.put(f"/rubrics/{rid}", json={**BACKEND, "name": DATA["name"]}).status_code == 409

    assert client.delete(f"/rubrics/{rid}").status_code == 204
    assert client.get(f"/rubrics/{rid}").status_code == 404
    assert client.delete(f"/rubrics/{rid}").status_code == 404


def test_evaluate_with_rubric_id_sends_stored_competencies(db_url):
    client, messages = make_client()
    rid = client.post("/rubrics", json=BACKEND).json()["id"]
    body = {k: v for k, v in SAMPLE.items() if k != "rubric"} | {"rubric_id": rid}
    assert client.post("/evaluate", json=body).status_code == 200
    prefix = messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "- API design\n- Databases" in prefix


def test_unknown_rubric_id_is_404(db_url):
    client, _ = make_client()
    body = {k: v for k, v in SAMPLE.items() if k != "rubric"} | {"rubric_id": str(uuid.uuid4())}
    assert client.post("/evaluate", json=body).status_code == 404
    session = {"job_description": SAMPLE["job_description"], "rubric_id": body["rubric_id"]}
    assert client.post("/sessions", json=session).status_code == 404


def test_session_started_from_saved_rubric_uses_its_competencies(db_url):
    client, _ = make_client()
    rid = client.post("/rubrics", json=DATA).json()["id"]
    sid = client.post("/sessions", json={"job_description": SAMPLE["job_description"], "rubric_id": rid}).json()["session_id"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["rubric"] == DATA["competencies"] and state["rubric_id"] == rid


def test_suggest_returns_nearest_rubrics_first(db_url):
    embedder = FakeEmbedder(keywords=["pipeline", "backend"])
    client, _ = make_client(embedder)
    client.post("/rubrics", json=BACKEND)
    client.post("/rubrics", json=DATA)

    res = client.post("/rubrics/suggest", json={"job_description": "We need someone for our data pipeline team.", "limit": 2})
    assert res.status_code == 200
    ranked = res.json()
    assert [s["rubric"]["name"] for s in ranked] == [DATA["name"], BACKEND["name"]]
    assert ranked[0]["similarity"] == pytest.approx(1.0) and ranked[1]["similarity"] == pytest.approx(0.0)
    assert embedder.calls[-1][1] == "query"
