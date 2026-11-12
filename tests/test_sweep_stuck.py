import importlib.util
from pathlib import Path

import pytest

from app import db

spec = importlib.util.spec_from_file_location(
    "sweep", Path(__file__).resolve().parent.parent / "scripts" / "sweep.py"
)
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)


@pytest.fixture
def notifications(monkeypatch):
    sent = []

    async def fake_notify(message, title=None, tags=None):
        sent.append({"message": message, "title": title})
        return "ok"

    monkeypatch.setattr("app.tools.notify.send", fake_notify)
    return sent


def stuck_capture(capture_id, text, status="failed"):
    db.insert_capture(capture_id, 0, text, None)
    if status != "received":
        db.finish_capture(capture_id, status, summary="error: boom")
    # Age it past the 10-minute grace period.
    conn = db._connect()
    conn.execute("UPDATE captures SET created_at = created_at - 3600000 WHERE id = ?",
                 (capture_id,))
    conn.commit()


async def test_stuck_capture_is_reported_on_two_sweeps_only(notifications):
    stuck_capture("aaaaaaaa1", "buy oat milk")
    for _ in range(3):
        await sweep.main()
    assert len(notifications) == 2
    assert all("buy oat milk" in n["message"] for n in notifications)


async def test_new_failure_still_reported_after_old_one_goes_quiet(notifications):
    stuck_capture("aaaaaaaa1", "buy oat milk")
    await sweep.main()
    await sweep.main()
    stuck_capture("bbbbbbbb2", "call the dentist", status="received")
    await sweep.main()
    assert len(notifications) == 3
    assert "call the dentist" in notifications[2]["message"]
    assert "buy oat milk" not in notifications[2]["message"]


async def test_done_and_fresh_captures_are_not_reported(notifications):
    stuck_capture("cccccccc3", "all good", status="done")
    db.insert_capture("dddddddd4", 0, "just arrived", None)
    await sweep.main()
    assert notifications == []


async def test_failed_send_does_not_use_up_an_alert(monkeypatch):
    stuck_capture("aaaaaaaa1", "buy oat milk")

    async def broken_notify(*args, **kwargs):
        raise RuntimeError("ntfy down")

    monkeypatch.setattr("app.tools.notify.send", broken_notify)
    with pytest.raises(RuntimeError):
        await sweep.main()
    assert db.get_capture("aaaaaaaa1")["sweep_alerts"] == 0
