from fakes import FakeEmbedder, FakeMessages, evaluation, fake_client
from fastapi.testclient import TestClient
from test_api import SAMPLE

from interview_evaluator.api import app, get_client
from interview_evaluator.embeddings import get_embedder, get_optional_embedder
from interview_evaluator.evaluator import SYSTEM_PROMPT

RUBRIC = ["API design", "Databases"]
MODEL_SCORES = {"API design": 3, "Databases": 3}
JD = "Backend engineer building Python services on Postgres."

# Marker in the text -> embedding direction. The query is marker-Q = [1, 0].
VECTORS = {
    "marker-Q": [1.0, 0.0],
    "marker-A": [1.0, 0.1],  # nearest
    "marker-B": [1.0, 0.5],
    "marker-C": [1.0, 1.0],
    "marker-D": [0.0, 1.0],  # farthest: excluded by the limit of 3
    "marker-E": [1.0, 0.0],  # identical to the query but never approved
    "marker-F": [1.0, 0.0],  # identical but approved under another rubric
}


def make_client() -> tuple[TestClient, FakeMessages]:
    messages = FakeMessages(parsed=evaluation(MODEL_SCORES))
    embedder = FakeEmbedder(vectors=VECTORS)
    app.dependency_overrides[get_client] = lambda: fake_client(messages)
    app.dependency_overrides[get_optional_embedder] = lambda: embedder
    app.dependency_overrides[get_embedder] = lambda: embedder
    return TestClient(app), messages


def new_rubric(client: TestClient, name: str) -> str:
    res = client.post("/rubrics", json={"name": name, "role_description": JD, "competencies": RUBRIC})
    return res.json()["id"]


def evaluate(client: TestClient, rubric_id: str, marker: str) -> dict:
    body = {
        "job_description": JD,
        "rubric_id": rubric_id,
        "transcript": [{"question": "Tell me about an API you designed.", "answer": f"Answer {marker}."}],
    }
    res = client.post("/evaluate", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def seed(client: TestClient) -> tuple[str, dict[str, str]]:
    rid, other = new_rubric(client, "Backend L4"), new_rubric(client, "Backend L5")
    ids = {}
    for m in "ABCD":
        ids[m] = evaluate(client, rid, f"marker-{m}")["id"]
        client.post(f"/evaluations/{ids[m]}/approve", json={"scores": {"API design": 5}, "recommendation": "strong_yes"})
    ids["E"] = evaluate(client, rid, "marker-E")["id"]
    ids["F"] = evaluate(client, other, "marker-F")["id"]
    client.post(f"/evaluations/{ids['F']}/approve")
    return rid, ids


def sent_text(call: dict) -> str:
    return "\n".join(block["text"] for block in call["messages"][0]["content"])


def test_retrieves_up_to_three_approved_same_rubric_nearest_first(db_url):
    client, messages = make_client()
    rid, ids = seed(client)

    result = evaluate(client, rid, "marker-Q")
    assert result["calibration_example_ids"] == [ids["A"], ids["B"], ids["C"]]
    assert client.get(f"/evaluations/{result['id']}").json()["calibration_example_ids"] == result["calibration_example_ids"]

    call = messages.calls[-1]
    content = call["messages"][0]["content"]
    assert len(content) == 3 and content[1]["text"].startswith("<calibration_examples>")
    assert content[1]["cache_control"] == {"type": "ephemeral"}  # part of the cached prefix
    assert "cache_control" not in content[2]  # candidate transcript stays last and uncached
    block = content[1]["text"]
    assert block.index("marker-A") < block.index("marker-B") < block.index("marker-C")
    assert "marker-D" not in block and "marker-E" not in block and "marker-F" not in block
    assert "- API design: 5" in block and "strong_yes" in block  # approved, not model, judgment
    assert "reasoning" not in block.lower()
    assert "calibration_examples" in call["system"][0]["text"]


def test_without_examples_the_prompt_matches_the_pre_rag_prompt(db_url):
    client, messages = make_client()
    rid = new_rubric(client, "Fresh rubric")
    result = evaluate(client, rid, "marker-Q")
    assert result["calibration_example_ids"] == []
    call = messages.calls[-1]
    assert call["system"][0]["text"] == SYSTEM_PROMPT
    assert len(call["messages"][0]["content"]) == 2
    assert "<calibration_examples>" not in sent_text(call)


def test_inline_rubric_never_uses_calibration(db_url):
    client, messages = make_client()
    seed(client)
    res = client.post("/evaluate", json={**SAMPLE, "transcript": [{"question": "q", "answer": "marker-Q"}]})
    assert res.json()["calibration_example_ids"] == []
    assert "<calibration_examples>" not in sent_text(messages.calls[-1])


def test_deleted_or_unapproved_evaluations_are_never_retrieved(db_url):
    client, _ = make_client()
    rid, ids = seed(client)
    client.post(f"/evaluations/{ids['A']}/unapprove")
    client.delete(f"/evaluations/{ids['B']}")
    result = evaluate(client, rid, "marker-Q")
    assert result["calibration_example_ids"] == [ids["C"], ids["D"]]


def test_live_session_from_saved_rubric_uses_calibration(db_url):
    client, messages = make_client()
    rid, ids = seed(client)
    sid = client.post("/sessions", json={"job_description": f"{JD} marker-Q", "rubric_id": rid}).json()["session_id"]
    client.post(f"/sessions/{sid}/qa", json={"question": "q", "answer": "a"})
    assert "<calibration_examples>" in sent_text(messages.calls[-1])
    final = client.post(f"/sessions/{sid}/end").json()
    assert final["calibration_example_ids"] == [ids["A"], ids["B"], ids["C"]]
