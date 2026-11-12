import pytest

from app import db
from app.agent import AgentResult
from app.worker import Worker


@pytest.fixture
def notifications(monkeypatch):
    sent: list[dict] = []

    async def fake_notify(message, title=None, tags=None, **kw):
        sent.append({"message": message, "title": title, "tags": tags})
        return "ok"

    monkeypatch.setattr("app.tools.notify.send", fake_notify)
    return sent


async def process_one(capture_id: str) -> None:
    row = db.claim_next_received()
    assert row is not None and row["id"] == capture_id
    await Worker()._process(row)


async def test_success_marks_done_and_notifies(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, **kw):
        return AgentResult(summary="TickTick: 'x' -> Inbox", trace=[{"tool": "t"}], tokens_used=42)

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("c1", 1, "add x", None)
    await process_one("c1")

    row = db.get_capture("c1")
    assert row["status"] == "done"
    assert row["tokens_used"] == 42
    assert "tool" in row["tool_trace"]
    assert notifications[0]["tags"] == "white_check_mark"
    assert notifications[0]["message"] == "TickTick: 'x' -> Inbox"


async def test_failure_marks_failed_and_notifies_loudly(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, **kw):
        raise RuntimeError("anthropic exploded")

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("c2", 1, "add y", None)
    await process_one("c2")

    row = db.get_capture("c2")
    assert row["status"] == "failed"
    assert "anthropic exploded" in row["summary"]
    note = notifications[0]
    assert note["tags"] == "x"
    assert "will NOT auto-retry" in note["message"]
    assert "add y" in note["message"]  # transcript included so nothing is lost


async def test_anthropic_auth_error_hints_expired_key(monkeypatch, notifications):
    import anthropic
    import httpx

    async def fake_run(transcription, capture_id=None, **kw):
        raise anthropic.AuthenticationError(
            message="invalid x-api-key",
            response=httpx.Response(
                401, request=httpx.Request("POST", "https://api.anthropic.com")
            ),
            body=None,
        )

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("c2b", 1, "add w", None)
    await process_one("c2b")

    assert db.get_capture("c2b")["status"] == "failed"
    msg = notifications[0]["message"]
    assert "rejected the API key" in msg and "ANTHROPIC_API_KEY" in msg


async def test_audio_only_capture_archived_without_agent(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, **kw):
        raise AssertionError("agent must not run without a transcript")

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("c3", 1, None, "/audio/c3.m4a")
    await process_one("c3")

    assert db.get_capture("c3")["status"] == "done"
    assert "archived" in notifications[0]["message"]


async def test_notify_failure_does_not_crash_worker(monkeypatch):
    async def fake_run(transcription, capture_id=None, **kw):
        return AgentResult(summary="ok")

    async def broken_notify(message, title=None, tags=None, **kw):
        raise RuntimeError("ntfy down")

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    monkeypatch.setattr("app.tools.notify.send", broken_notify)
    db.insert_capture("c4", 1, "add z", None)
    await process_one("c4")  # must not raise
    assert db.get_capture("c4")["status"] == "done"


def test_requeue_stale_processing():
    db.insert_capture("c5", 1, "stuck", None)
    assert db.claim_next_received()["id"] == "c5"
    assert db.get_capture("c5")["status"] == "processing"
    assert db.requeue_stale_processing() == 1
    assert db.get_capture("c5")["status"] == "received"
