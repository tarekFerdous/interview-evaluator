import json

from fastapi.testclient import TestClient
from test_api import SAMPLE

from interview_evaluator.api import app
from interview_evaluator.security import get_service_api_key


def client_requiring_key() -> TestClient:
    app.dependency_overrides[get_service_api_key] = lambda: "s3cret"
    return TestClient(app)


def test_ui_serves_html_without_a_key():
    res = client_requiring_key().get("/ui/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "Interview Evaluator" in res.text


def test_live_panel_has_the_send_exchange_control():
    assert 'id="mic-send"' in client_requiring_key().get("/ui/").text


def test_live_panel_offers_remote_and_room_audio_modes():
    html = client_requiring_key().get("/ui/").text
    assert 'value="remote"' in html and 'value="room"' in html


def test_static_assets_are_served():
    client = client_requiring_key()
    js, css = client.get("/ui/app.js"), client.get("/ui/app.css")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")


def test_example_matches_sample_request():
    res = client_requiring_key().get("/ui/example.json")
    assert json.loads(res.text) == SAMPLE


def test_root_redirects_to_ui_and_docs_still_work():
    client = client_requiring_key()
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 307 and res.headers["location"] == "/ui/"
    assert client.get("/docs").status_code == 200
