import time

import pytest
from fastapi.testclient import TestClient

from app import db
from app.agent import AgentResult
from tests.conftest import auth


@pytest.fixture
def client(monkeypatch):
    """TestClient with the agent and ntfy stubbed out; real worker runs."""
    agent_calls: list[str] = []

    async def fake_run_capture(transcription, capture_id=None, **kw):
        agent_calls.append(transcription)
        return AgentResult(summary=f"did: {transcription}", trace=[], tokens_used=7)

    async def fake_notify(message, title=None, tags=None, **kw):
        return "ok"

    monkeypatch.setattr("app.agent.run_capture", fake_run_capture)
    monkeypatch.setattr("app.tools.notify.send", fake_notify)

    from app.main import app

    with TestClient(app) as c:
        c.agent_calls = agent_calls
        yield c


def wait_for_status(capture_id: str, status: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.get_capture(capture_id)
        if row is not None and row["status"] == status:
            return row
        time.sleep(0.02)
    row = db.get_capture(capture_id)
    raise AssertionError(
        f"capture {capture_id} never reached {status!r}; "
        f"now: {row['status'] if row else 'missing'}"
    )


def test_missing_auth_rejected(client):
    resp = client.post("/webhook", data={"transcription": "hi"})
    assert resp.status_code == 401


def test_wrong_secret_rejected(client):
    resp = client.post(
        "/webhook",
        data={"transcription": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_transcription_only_capture_processed(client):
    resp = client.post(
        "/webhook",
        data={
            "transcription": "add buy oat milk to ticktick for tomorrow",
            "recordedAt": "1752700000000",
            "client": "ring",
        },
        headers=auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["duplicate"] is False

    row = wait_for_status(body["id"], "done")
    assert row["summary"] == "did: add buy oat milk to ticktick for tomorrow"
    assert row["recorded_at"] == 1752700000000
    assert client.agent_calls == ["add buy oat milk to ticktick for tomorrow"]


def test_audio_filename_is_idempotency_key_and_audio_saved(client):
    from app.config import settings

    resp = client.post(
        "/webhook",
        data={"transcription": "note the mic gain", "recordedAt": "1752700000000"},
        files={"audio": ("rec-42.m4a", b"\x00fake-aac", "audio/mp4")},
        headers=auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "rec-42"
    saved = settings.audio_dir / "rec-42.m4a"
    assert saved.read_bytes() == b"\x00fake-aac"
    wait_for_status("rec-42", "done")


def test_duplicate_delivery_does_not_rerun_agent(client):
    form = {"transcription": "remember the thing", "recordedAt": "1752700000000"}
    first = client.post(
        "/webhook",
        data=form,
        files={"audio": ("rec-dup.m4a", b"a", "audio/mp4")},
        headers=auth(),
    )
    assert first.json()["duplicate"] is False
    wait_for_status("rec-dup", "done")

    second = client.post(
        "/webhook",
        data=form,
        files={"audio": ("rec-dup.m4a", b"a", "audio/mp4")},
        headers=auth(),
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    time.sleep(0.2)  # give a buggy worker a chance to double-run
    assert client.agent_calls == ["remember the thing"]
    assert db.get_capture("rec-dup")["status"] == "done"


@pytest.mark.parametrize("name", ["../context/evil.m4a", "a/b.m4a", "..\\x.m4a", ".m4a"])
def test_unsafe_audio_filename_gets_hashed_id(client, name):
    from app.config import settings

    resp = client.post(
        "/webhook",
        data={"transcription": "hello", "recordedAt": "1752700000000"},
        files={"audio": (name, b"x", "audio/mp4")},
        headers=auth(),
    )
    assert resp.status_code == 200
    capture_id = resp.json()["id"]
    assert len(capture_id) == 32 and capture_id.isalnum()
    assert (settings.audio_dir / f"{capture_id}.m4a").read_bytes() == b"x"
    assert not (settings.data_dir / "context" / "evil.m4a").exists()


def test_bare_secret_without_bearer_prefix_rejected(client):
    resp = client.post(
        "/webhook",
        data={"transcription": "bare header", "recordedAt": "1752700000000"},
        headers={"Authorization": "testsecret"},
    )
    assert resp.status_code == 401


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
