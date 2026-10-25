import psycopg
from fakes import FakeEmbedder, FakeMessages, evaluation, fake_client
from fastapi.testclient import TestClient
from test_api import SAMPLE

from interview_evaluator.api import app, get_client
from interview_evaluator.embeddings import get_embedder, get_optional_embedder

SCORES = {c: 3 for c in SAMPLE["rubric"]}


def make_client(messages: FakeMessages | None = None, embedder=None) -> tuple[TestClient, FakeMessages]:
    messages = messages or FakeMessages(parsed=evaluation(SCORES))
    embedder = embedder or FakeEmbedder()
    app.dependency_overrides[get_client] = lambda: fake_client(messages)
    app.dependency_overrides[get_optional_embedder] = lambda: embedder
    app.dependency_overrides[get_embedder] = lambda: embedder
    return TestClient(app), messages


def evaluate(client: TestClient, body: dict = SAMPLE) -> dict:
    res = client.post("/evaluate", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_evaluate_stores_and_returns_id(db_url):
    client, _ = make_client()
    result = evaluate(client)
    assert result["id"] and result["calibration_example_ids"] == []
    assert result["evaluation"]["recommendation"] == "yes"

    stored = client.get(f"/evaluations/{result['id']}").json()
    assert stored["request"]["transcript"] == SAMPLE["transcript"]
    assert stored["request"]["rubric"] == SAMPLE["rubric"]
    assert stored["evaluation"] == result["evaluation"]
    assert stored["approved"] is False and stored["rubric_id"] is None

    with psycopg.connect(db_url) as conn:
        dims = conn.execute("SELECT vector_dims(embedding) FROM evaluations").fetchone()[0]
    assert dims == 1024


def test_evaluate_stores_without_embeddings_when_unconfigured(db_url):
    client, _ = make_client()
    app.dependency_overrides[get_optional_embedder] = lambda: None
    result = evaluate(client)
    assert client.get(f"/evaluations/{result['id']}").status_code == 200


def test_session_end_persists(db_url):
    client, _ = make_client()
    sid = client.post("/sessions", json={k: SAMPLE[k] for k in ("job_description", "rubric")}).json()["session_id"]
    client.post(f"/sessions/{sid}/qa", json=SAMPLE["transcript"][0])
    final = client.post(f"/sessions/{sid}/end").json()
    stored = client.get(f"/evaluations/{final['id']}").json()
    assert len(stored["request"]["transcript"]) == 1


def test_delete_removes_evaluation(db_url):
    client, _ = make_client()
    eid = evaluate(client)["id"]
    assert client.delete(f"/evaluations/{eid}").status_code == 204
    assert client.get(f"/evaluations/{eid}").status_code == 404
    assert client.delete(f"/evaluations/{eid}").status_code == 404


def test_approve_as_is_copies_model_scores(db_url):
    client, _ = make_client()
    eid = evaluate(client)["id"]
    res = client.post(f"/evaluations/{eid}/approve")
    assert res.status_code == 200
    stored = res.json()
    assert stored["approved"] is True and stored["approved_at"]
    assert stored["approved_scores"] == SCORES
    assert stored["approved_recommendation"] == "yes"


def test_approve_with_corrections_keeps_model_result(db_url):
    client, _ = make_client()
    eid = evaluate(client)["id"]
    first = SAMPLE["rubric"][0]
    res = client.post(f"/evaluations/{eid}/approve", json={"scores": {first: 5}, "recommendation": "strong_yes"})
    stored = res.json()
    assert stored["approved_scores"] == SCORES | {first: 5}
    assert stored["approved_recommendation"] == "strong_yes"
    assert stored["evaluation"]["recommendation"] == "yes"  # model result untouched
    assert all(s["score"] == 3 for s in stored["evaluation"]["scores"])


def test_approve_rejects_out_of_range_and_unknown_competencies(db_url):
    client, _ = make_client()
    eid = evaluate(client)["id"]
    first = SAMPLE["rubric"][0]
    assert client.post(f"/evaluations/{eid}/approve", json={"scores": {first: 6}}).status_code == 422
    assert client.post(f"/evaluations/{eid}/approve", json={"scores": {first: 0}}).status_code == 422
    res = client.post(f"/evaluations/{eid}/approve", json={"scores": {"Juggling": 4}})
    assert res.status_code == 422 and "Juggling" in res.json()["detail"]
    assert client.get(f"/evaluations/{eid}").json()["approved"] is False


def test_unapprove_clears_approval(db_url):
    client, _ = make_client()
    eid = evaluate(client)["id"]
    client.post(f"/evaluations/{eid}/approve", json={"recommendation": "no"})
    stored = client.post(f"/evaluations/{eid}/unapprove").json()
    assert stored["approved"] is False
    assert stored["approved_scores"] is None and stored["approved_recommendation"] is None
    assert stored["approved_at"] is None


def test_approve_unknown_evaluation_is_404(db_url):
    client, _ = make_client()
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/evaluations/{missing}/approve").status_code == 404
    assert client.post(f"/evaluations/{missing}/unapprove").status_code == 404


def test_evaluation_endpoints_are_503_without_a_database():
    client, _ = make_client()
    assert client.get("/evaluations/00000000-0000-0000-0000-000000000000").status_code == 503
