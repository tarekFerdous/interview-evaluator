import httpx
from fakes import FakeMessages, FakeTokenIssuer, evaluation, segmentation
from test_session_events import read_event, typed
from test_sessions import session_client, start

from interview_evaluator import speech
from interview_evaluator.api import app
from interview_evaluator.speech import AzureTokenIssuer, SpeechConfig, get_token_issuer

UTTERANCES = [
    {"speaker": "Speaker 1", "text": "Tell me about something you shipped."},
    {"speaker": "Speaker 2", "text": "I shipped a FastAPI service to production."},
]


def test_token_endpoint_returns_token_and_region_only():
    client, _ = session_client()
    app.dependency_overrides[get_token_issuer] = lambda: FakeTokenIssuer()
    res = client.post("/speech/token")
    assert res.status_code == 200
    assert res.json() == {"token": "fake-short-lived-token", "region": "westeurope", "expires_in_seconds": 540}


def test_azure_token_issuer_keeps_the_key_server_side(monkeypatch):
    sent = {}

    def fake_post(url, headers, timeout):
        sent.update(url=url, headers=headers)
        return httpx.Response(200, text="tok-123")

    monkeypatch.setattr(speech.httpx, "post", fake_post)
    issuer = AzureTokenIssuer(SpeechConfig(key="SECRET-KEY", region="westeurope", endpoint="https://x.example"))
    app.dependency_overrides[get_token_issuer] = lambda: issuer
    client, _ = session_client()
    res = client.post("/speech/token")
    assert sent["url"] == "https://x.example/sts/v1.0/issueToken"
    assert sent["headers"]["Ocp-Apim-Subscription-Key"] == "SECRET-KEY"
    assert res.json()["token"] == "tok-123" and "SECRET-KEY" not in res.text


def test_speech_token_requires_configuration(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    client, _ = session_client()
    assert client.post("/speech/token").status_code == 503


def test_segment_appends_qa_and_triggers_scorecard_event():
    messages = FakeMessages(parsed=[segmentation(1), evaluation({"Python": 4}),
                                    segmentation(1), evaluation({"Python": 5})])
    client, _ = session_client(messages)
    sid = start(client)
    with client.stream("GET", f"/sessions/{sid}/events") as res:
        lines = res.iter_lines()
        seg = client.post(f"/sessions/{sid}/segment", json={"utterances": UTTERANCES})
        assert seg.status_code == 200
        assert seg.json() == {"qa": [{"question": "Question 1?", "answer": "Answer 1."}],
                              "qa_count": 1, "interviewer_speaker": "Speaker 1", "unanswered_from": None}
        assert typed(read_event(lines)) == ("scoring", {"qa_count": 1})
        kind, data = typed(read_event(lines))
        assert kind == "scorecard" and data["qa_count"] == 1

        client.post(f"/sessions/{sid}/segment", json={"utterances": UTTERANCES})
        assert typed(read_event(lines))[0] == "scoring"
        assert typed(read_event(lines))[1]["qa_count"] == 2

    first, second = messages.calls[0], messages.calls[2]
    assert "1. [Speaker 2] I shipped a FastAPI service" in first["messages"][0]["content"]
    assert "The interviewer is" not in first["messages"][0]["content"]
    assert "labels are unreliable" not in first["messages"][0]["content"]
    assert second["messages"][0]["content"].startswith("The interviewer is Speaker 1.")
    assert len(client.get(f"/sessions/{sid}").json()["transcript"]) == 2


def test_replace_last_qa_rescores_with_the_fix():
    messages = FakeMessages(parsed=[evaluation({"Python": 2}), evaluation({"Python": 4})])
    client, _ = session_client(messages)
    sid = start(client)
    client.post(f"/sessions/{sid}/qa", json={"question": "Q?", "answer": "half an answ"})
    res = client.put(f"/sessions/{sid}/qa/last", json={"question": "Q?", "answer": "The full answer."})
    assert res.status_code == 200 and res.json() == {"qa_count": 1}
    state = client.get(f"/sessions/{sid}").json()
    assert state["transcript"][-1]["answer"] == "The full answer."
    assert state["evaluated_qa_count"] == 1 and state["latest_evaluation"]["scores"][0]["score"] == 4
    assert "The full answer." in messages.calls[1]["messages"][0]["content"][1]["text"]


def test_replace_last_qa_needs_a_qa():
    client, _ = session_client()
    assert client.put(f"/sessions/{start(client)}/qa/last", json={"question": "q", "answer": "a"}).status_code == 422


def test_segment_with_no_complete_exchange_adds_nothing():
    messages = FakeMessages(parsed=[segmentation(0)])
    client, _ = session_client(messages)
    sid = start(client)
    res = client.post(f"/sessions/{sid}/segment", json={"utterances": UTTERANCES[:1]})
    assert res.json()["qa"] == [] and res.json()["qa_count"] == 0
    assert len(messages.calls) == 1  # no re-score was triggered


SINGLE_VOICE = [
    {"speaker": "Guest-1", "text": "Tell me about something you shipped."},
    {"speaker": "Guest-1", "text": "I shipped a FastAPI service to production."},
    {"speaker": "Unknown", "text": "And how did you test it?"},
]


def test_single_label_exchange_is_split_by_content_and_does_not_fix_the_interviewer():
    messages = FakeMessages(parsed=[segmentation(1, interviewer="Guest-1", unanswered_from=2),
                                    evaluation({"Python": 4})])
    client, _ = session_client(messages)
    sid = start(client)
    res = client.post(f"/sessions/{sid}/segment", json={"utterances": SINGLE_VOICE})
    assert res.status_code == 200
    body = res.json()
    assert body["qa_count"] == 1 and body["unanswered_from"] == 2
    assert body["interviewer_speaker"] is None
    assert "labels are unreliable" in messages.calls[0]["messages"][0]["content"]


def test_multi_label_exchange_after_a_single_label_one_fixes_the_interviewer():
    messages = FakeMessages(parsed=[segmentation(0, interviewer="Guest-1"),
                                    segmentation(0, interviewer="Speaker 1")])
    client, _ = session_client(messages)
    sid = start(client)
    client.post(f"/sessions/{sid}/segment", json={"utterances": SINGLE_VOICE[:2]})
    res = client.post(f"/sessions/{sid}/segment", json={"utterances": UTTERANCES})
    assert res.json()["interviewer_speaker"] == "Speaker 1"


def test_client_known_interviewer_wins_over_the_model_guess():
    messages = FakeMessages(parsed=[segmentation(0, interviewer="Candidate"),
                                    segmentation(0, interviewer="Candidate")])
    client, _ = session_client(messages)
    sid = start(client)
    remote = [{"speaker": "Interviewer", "text": "Why this role?"},
              {"speaker": "Candidate", "text": "Because I like evals."}]
    res = client.post(f"/sessions/{sid}/segment", json={"utterances": remote, "interviewer_speaker": "Interviewer"})
    assert res.json()["interviewer_speaker"] == "Interviewer"
    assert messages.calls[0]["messages"][0]["content"].startswith("The interviewer is Interviewer.")
    # an interviewer-only send in remote mode still pins the known label
    sid2 = start(client)
    res = client.post(f"/sessions/{sid2}/segment",
                      json={"utterances": remote[:1], "interviewer_speaker": "Interviewer"})
    assert res.json()["interviewer_speaker"] == "Interviewer"


def test_remote_label_is_used_in_the_prompt_after_room_mode_fixed_another():
    messages = FakeMessages(parsed=[segmentation(0, interviewer="Speaker 1"), segmentation(0)])
    client, _ = session_client(messages)
    sid = start(client)
    client.post(f"/sessions/{sid}/segment", json={"utterances": UTTERANCES})
    remote = [{"speaker": "Interviewer", "text": "Why this role?"}, {"speaker": "Candidate", "text": "Evals."}]
    client.post(f"/sessions/{sid}/segment", json={"utterances": remote, "interviewer_speaker": "Interviewer"})
    assert messages.calls[1]["messages"][0]["content"].startswith("The interviewer is Interviewer.")


def test_out_of_range_unanswered_from_is_ignored():
    client, _ = session_client(FakeMessages(parsed=[segmentation(0, unanswered_from=7)]))
    sid = start(client)
    assert client.post(f"/sessions/{sid}/segment", json={"utterances": UTTERANCES}).json()["unanswered_from"] is None
