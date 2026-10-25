import io
import os
import struct
import wave

import pytest
from fakes import FakeMessages, FakeTranscriber, fake_client, segmentation
from fastapi.testclient import TestClient

from interview_evaluator import transcriptions
from interview_evaluator.api import app, get_client
from interview_evaluator.security import RateLimiter, get_rate_limiter, get_service_api_key
from interview_evaluator.speech import AzureTranscriber, DiarizedTranscript, get_speech_config, get_transcriber


def make_client(transcriber=None, messages=None) -> tuple[TestClient, FakeTranscriber, FakeMessages]:
    transcriber = transcriber or FakeTranscriber()
    messages = messages or FakeMessages(parsed=segmentation(2))
    app.dependency_overrides[get_transcriber] = lambda: transcriber
    app.dependency_overrides[get_client] = lambda: fake_client(messages)
    return TestClient(app), transcriber, messages


def upload(client: TestClient, data: bytes = b"RIFF....WAVEfmt ", name="call.wav", ctype="audio/wav"):
    return client.post("/transcriptions", files={"file": (name, data, ctype)})


def test_upload_reaches_done_with_diarized_and_qa():
    client, transcriber, messages = make_client()
    res = upload(client)
    assert res.status_code == 202
    job = client.get(f"/transcriptions/{res.json()['id']}").json()
    assert job["status"] == "done"
    assert [u["speaker"] for u in job["diarized"]] == ["Speaker 1", "Speaker 2", "Speaker 1", "Speaker 2"]
    assert job["qa"] == [{"question": "Question 1?", "answer": "Answer 1."},
                         {"question": "Question 2?", "answer": "Answer 2."}]
    assert job["interviewer_speaker"] == "Speaker 1" and job["warning"] is None
    assert transcriber.calls[0][1:] == ("audio/wav", "call.wav")

    sent = messages.calls[0]["messages"][0]["content"]
    assert "[Speaker 1] Tell me about something you shipped." in sent
    assert "[Speaker 2] I shipped a FastAPI service to production." in sent


@pytest.mark.parametrize("name,ctype,expected", [
    ("call.mp3", "audio/mpeg", "audio/mpeg"),
    ("call.m4a", "audio/x-m4a", "audio/mp4"),
    ("call.m4a", "application/octet-stream", "audio/mp4"),  # browsers sometimes send a generic type
])
def test_supported_formats(name, ctype, expected):
    client, transcriber, _ = make_client()
    assert upload(client, name=name, ctype=ctype).status_code == 202
    assert transcriber.calls[0][1] == expected


def test_unsupported_type_is_415():
    client, transcriber, _ = make_client()
    res = upload(client, name="notes.txt", ctype="text/plain")
    assert res.status_code == 415 and "wav, mp3 or m4a" in res.json()["detail"]
    assert transcriber.calls == []


def test_oversized_upload_is_413(monkeypatch):
    monkeypatch.setattr(transcriptions, "MAX_UPLOAD_BYTES", 10)
    client, transcriber, _ = make_client()
    assert upload(client, data=b"x" * 11).status_code == 413
    assert transcriber.calls == []


def test_transcriber_error_marks_job_error():
    client, _, _ = make_client(FakeTranscriber(error=transcriptions.TranscriptionError("Azure Speech returned 400: bad audio")))
    job = client.get(f"/transcriptions/{upload(client).json()['id']}").json()
    assert job["status"] == "error" and "bad audio" in job["error"]


def test_too_long_recording_marks_job_error():
    client, _, _ = make_client(FakeTranscriber(duration=61 * 60))
    job = client.get(f"/transcriptions/{upload(client).json()['id']}").json()
    assert job["status"] == "error" and "60 minutes" in job["error"]


def test_more_than_30_pairs_sets_warning():
    client, _, _ = make_client(messages=FakeMessages(parsed=segmentation(31)))
    job = client.get(f"/transcriptions/{upload(client).json()['id']}").json()
    assert job["status"] == "done" and len(job["qa"]) == 31
    assert "31 Q&A pairs" in job["warning"] and "30" in job["warning"]


def test_unknown_job_is_404():
    client, _, _ = make_client()
    assert client.get("/transcriptions/nope").status_code == 404


def test_requires_key_and_is_rate_limited():
    client, _, _ = make_client()
    app.dependency_overrides[get_service_api_key] = lambda: "k"
    assert upload(client).status_code == 401
    limiter = RateLimiter(1)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    assert client.post("/transcriptions", headers={"X-API-Key": "k"},
                       files={"file": ("a.wav", b"x", "audio/wav")}).status_code == 202
    assert client.post("/transcriptions", headers={"X-API-Key": "k"},
                       files={"file": ("a.wav", b"x", "audio/wav")}).status_code == 429


def test_speech_unconfigured_is_503(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    client, _, _ = make_client()
    del app.dependency_overrides[get_transcriber]
    assert upload(client).status_code == 503


@pytest.mark.skipif(not os.environ.get("AZURE_SPEECH_KEY"), reason="AZURE_SPEECH_KEY not set (real Azure test)")
def test_real_azure_fast_transcription_contract():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:  # 1 s of silence, 16 kHz mono
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(struct.pack("<16000h", *([0] * 16000)))
    result = AzureTranscriber(get_speech_config()).transcribe(buf.getvalue(), "audio/wav", "silence.wav")
    assert isinstance(result, DiarizedTranscript)
    assert 0.5 < result.duration_seconds < 2
