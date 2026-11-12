"""Notification buttons: signed URLs, the /action endpoint, the worker's
undo / approval flows, the approval gate, and tap-to-open links."""

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app import actions, agent, db, links, tools
from app.agent import AgentResult
from app.config import settings
from app.tools import notify
from app.worker import Worker, approval_transcription
from tests.test_agent import fake_client, response, text_block, tool_block  # noqa: F401

TASK_RESULT = (
    "created task t1 (list_id p1): 'Buy milk', due: none, "
    "url: https://ticktick.com/webapp/#p/p1/tasks/t1"
)
TASK_TRACE = [
    {"tool": "ticktick_list_projects", "input": {}, "result": "...", "is_error": False},
    {"tool": "ticktick_add_task", "input": {"title": "Buy milk"}, "result": TASK_RESULT,
     "is_error": False},
]


@pytest.fixture(autouse=True)
def base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://ring.test")


@pytest.fixture
def notifications(monkeypatch):
    sent: list[dict] = []

    async def fake_notify(message, title=None, tags=None, **kwargs):
        sent.append({"message": message, "title": title, "tags": tags, **kwargs})
        return "ok"

    monkeypatch.setattr("app.tools.notify.send", fake_notify)
    return sent


def split(url: str) -> tuple[str, dict]:
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return parsed.path, q


async def run_next(capture_id: str) -> None:
    row = db.claim_next_received()
    assert row is not None and row["id"] == capture_id
    await Worker()._process(row)


# --- signing -----------------------------------------------------------------

def test_signed_url_roundtrip_and_tamper():
    path, q = split(actions.url("undo", "c1"))
    assert path == "/action/undo/c1"
    exp = int(q["exp"])
    assert actions.verify("undo", "c1", exp, q["sig"])
    assert not actions.verify("undo", "c2", exp, q["sig"])  # other capture
    assert not actions.verify("approve", "c1", exp, q["sig"])  # other action
    assert not actions.verify("undo", "c1", exp + 1, q["sig"])  # extended expiry
    assert not actions.verify("undo", "c1", int(time.time()) - 1,
                              actions.sign("undo", "c1", int(time.time()) - 1))


def test_button_never_contains_webhook_secret():
    button = actions.button("Undo", "undo", "c1")
    assert settings.webhook_secret not in json.dumps(button)
    assert button["action"] == "http" and button["method"] == "POST" and button["clear"]


# --- links ---------------------------------------------------------------------

def test_primary_link_per_tool():
    assert links.primary_link(TASK_TRACE) == "https://ticktick.com/webapp/#p/p1/tasks/t1"
    event = [{"tool": "calendar_create_event", "input": {}, "is_error": False,
              "result": "created event e1: 'Dentist' at x, url: https://www.google.com/calendar/event?eid=abc"}]
    assert links.primary_link(event) == "https://www.google.com/calendar/event?eid=abc"
    mcp_create = [{"tool": "notion-create-pages", "input": {}, "is_error": False,
                   "result": '{"pages":[{"id":"x","url":"https://www.notion.so/Idea-abc123"}]}'}]
    assert links.primary_link(mcp_create) == "https://www.notion.so/Idea-abc123"
    mcp_update = [{"tool": "notion-update-page", "is_error": False, "result": "ok",
                   "input": {"data": {"page_id": "1234-abcd", "command": "insert_content_after"}}}]
    assert links.primary_link(mcp_update) == "https://www.notion.so/1234abcd"
    append = [{"tool": "notion_append", "input": {"page_id": "aa-bb"}, "is_error": False,
               "result": "appended 1 block(s) to page aa-bb"}]
    assert links.primary_link(append) == "https://www.notion.so/aabb"


def test_primary_link_skips_errors_lookups_and_deletes():
    trace = TASK_TRACE + [
        {"tool": "calendar_create_event", "input": {}, "result": "HTTP 400", "is_error": True},
        {"tool": "ticktick_delete_task", "input": {"task_id": "t9", "list_id": "p"},
         "result": "deleted task t9", "is_error": False},
    ]
    assert links.primary_link(trace) == "https://ticktick.com/webapp/#p/p1/tasks/t1"
    question = [{"tool": "calendar_list_events", "input": {}, "result": "...", "is_error": False}]
    assert links.primary_link(question) is None
    assert links.mutations(question) == []


# --- notify payload ------------------------------------------------------------

@respx.mock
async def test_ntfy_payload_carries_click_actions_priority():
    route = respx.post("https://ntfy.sh").mock(return_value=Response(200))
    await notify.send("m", title="t", click="https://x", priority=5,
                      actions=[{"action": "http", "label": "Undo", "url": "u"}])
    body = json.loads(route.calls[0].request.content)
    assert body["click"] == "https://x" and body["priority"] == 5
    assert body["actions"][0]["label"] == "Undo"


# --- worker ----------------------------------------------------------------------

async def test_success_has_click_and_undo_button(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, mode=None):
        assert mode == "normal"
        return AgentResult(summary="TickTick: Buy milk", trace=TASK_TRACE)

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("c1", 1, "buy milk", None)
    await run_next("c1")
    note = notifications[0]
    assert note["click"] == "https://ticktick.com/webapp/#p/p1/tasks/t1"
    [button] = note["actions"]
    assert button["label"].endswith("Undo") and "/action/undo/c1?" in button["url"]


async def test_question_has_no_click_or_undo(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, mode=None):
        return AgentResult(summary="answered", trace=[])

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("q1", 1, "what's on today", None)
    await run_next("q1")
    assert notifications[0]["click"] is None and notifications[0]["actions"] == []


async def test_approval_request_sends_urgent_notification(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, mode=None):
        return AgentResult(summary="Waiting for approval", trace=[],
                           pending_approval={"action": "Delete 2 tasks", "message": "Delete the 2 dentist tasks.", "details": "ids t1, t2 in list p1"})

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("a1", 1, "delete my dentist tasks", None)
    await run_next("a1")

    stored = json.loads(db.get_capture("a1")["pending_approval"])
    assert stored["action"] == "Delete 2 tasks" and stored["requested_at"] > 0
    note = notifications[0]
    assert note["priority"] == 5 and note["title"] == "Approve? Delete 2 tasks" and note["tags"] == "warning"
    assert [b["label"] for b in note["actions"]] == ["✅ Approve", "🚫 Deny"]


async def test_undo_runs_in_undo_mode_and_logs_to_notion(monkeypatch, notifications):
    monkeypatch.setattr(settings, "notion_undo_page_id", "undo-page")
    seen = {}

    async def fake_run(transcription, capture_id=None, mode=None):
        seen.update(transcription=transcription, mode=mode)
        return AgentResult(summary="deleted task t1", trace=[])

    appended = []

    async def fake_append(tool_input):
        appended.append(tool_input)
        return "ok"

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    monkeypatch.setattr("app.tools.notion.append", fake_append)
    db.insert_capture("c1", 1, "buy milk", None)
    db.finish_capture("c1", "done", summary="TickTick: Buy milk", tool_trace=json.dumps(TASK_TRACE))
    db.insert_child("c1", "undo", 'Undo: "buy milk"')
    await run_next("c1:undo")

    assert seen["mode"] == "undo"
    assert seen["transcription"].startswith("[UNDO]") and "created task t1" in seen["transcription"]
    assert appended[0]["page_id"] == "undo-page"
    assert 'Undone: "buy milk"' in appended[0]["markdown"]
    assert notifications[0]["title"] == "Undone"
    assert notifications[0]["click"] == "https://www.notion.so/undopage"


async def test_approved_child_executes_plan(monkeypatch, notifications):
    seen = {}

    async def fake_run(transcription, capture_id=None, mode=None):
        seen.update(transcription=transcription, mode=mode)
        return AgentResult(summary="deleted", trace=[
            {"tool": "ticktick_delete_task", "input": {}, "result": "deleted task t1",
             "is_error": False}])

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("a1", 1, "delete dentist task", None)
    db.finish_capture("a1", "done", summary="waiting for approval")
    db.insert_child("a1", "approved", approval_transcription(
        {"action": "Delete dentist", "details": "task t1 list p1"}))
    await run_next("a1:approved")
    assert seen["mode"] == "approved"
    assert "Plan: Delete dentist\ntask t1 list p1" in seen["transcription"]
    assert "delete dentist task" in seen["transcription"]


async def test_stale_approval_reminded_then_expired(notifications):
    db.insert_capture("a1", 1, "delete it", None)
    old = db._now() - 13 * 3600 * 1000
    db.set_pending_approval("a1", json.dumps({"action": "X", "details": "d", "requested_at": old}))
    w = Worker()
    await w.check_approvals()
    assert notifications[-1]["message"].startswith("Reminder")
    await w.check_approvals()  # just reminded: nothing new
    assert len(notifications) == 1

    ancient = db._now() - 4 * 86400 * 1000
    db.set_pending_approval("a1", json.dumps({"action": "X", "details": "d", "requested_at": ancient}))
    await w.check_approvals()
    assert notifications[-1]["title"] == "Approval expired"
    assert db.get_capture("a1")["pending_approval"] is None


# --- gate ----------------------------------------------------------------------

async def test_delete_tools_blocked_unless_approved(monkeypatch):
    calls = []

    async def fake_delete(tool_input):
        calls.append(tool_input)
        return "deleted"

    schema, _ = tools._registry["ticktick_delete_task"]
    monkeypatch.setitem(tools._registry, "ticktick_delete_task", (schema, fake_delete))
    result, is_error = await tools.dispatch("ticktick_delete_task", {"task_id": "t", "list_id": "p"})
    assert is_error and "request_approval" in result and calls == []

    for mode in ("approved", "undo"):
        token = tools.run_mode.set(mode)
        try:
            assert await tools.dispatch("ticktick_delete_task", {}) == ("deleted", False)
        finally:
            tools.run_mode.reset(token)


async def test_request_approval_surfaces_on_result_and_resets(fake_client):  # noqa: F811
    fake_client.responses = [
        response("tool_use", [tool_block("request_approval",
                                         {"action": "Delete 5 tasks", "details": "ids..."})]),
        response("end_turn", [text_block("Waiting for approval")]),
        response("end_turn", [text_block("Added task")]),
    ]
    result = await agent.run_capture("delete everything", mode="normal")
    assert result.pending_approval == {"action": "Delete 5 tasks", "message": "Delete 5 tasks", "details": "ids..."}
    nxt = await agent.run_capture("add a task", mode="normal")
    assert nxt.pending_approval is None
    assert tools.run_mode.get() == "normal"


async def test_request_approval_refused_in_approved_run(fake_client):  # noqa: F811
    fake_client.responses = [
        response("tool_use", [tool_block("request_approval", {"action": "a", "details": "d"})]),
        response("end_turn", [text_block("done")]),
    ]
    result = await agent.run_capture("[APPROVED] ...", mode="approved")
    assert result.pending_approval is None
    assert result.trace[0]["is_error"] is True


# --- endpoint -------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, mode=None):
        return AgentResult(summary=f"{mode}: done", trace=[])

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    from app.main import app

    with TestClient(app) as c:
        yield c


def tap(client, kind, capture_id, **override):
    path, q = split(actions.url(kind, capture_id))
    q.update(override)
    return client.post(path, params=q)


def test_endpoint_rejects_bad_signature(client):
    db.insert_capture("c1", 1, "x", None)
    assert tap(client, "undo", "c1", sig="0" * 64).status_code == 403
    assert client.post("/action/undo/c1").status_code == 403


def test_undo_tap_queues_once(client):
    db.insert_capture("c1", 1, "buy milk", None)
    db.finish_capture("c1", "done", summary="s", tool_trace=json.dumps(TASK_TRACE))
    first = tap(client, "undo", "c1")
    assert first.status_code == 200 and first.json()["queued"] is True
    second = tap(client, "undo", "c1")
    assert second.status_code == 200 and second.json()["queued"] is False
    assert db.get_capture("c1:undo")["parent_id"] == "c1"


def test_undo_refused_when_nothing_changed(client):
    db.insert_capture("q1", 1, "what time is it", None)
    db.finish_capture("q1", "done", summary="s", tool_trace="[]")
    assert tap(client, "undo", "q1").status_code == 409


def test_approve_then_deny_only_first_wins(client):
    db.insert_capture("a1", 1, "delete it", None)
    db.finish_capture("a1", "done", summary="waiting")
    db.set_pending_approval("a1", json.dumps({"action": "Delete X", "details": "t1"}))
    assert tap(client, "approve", "a1").status_code == 200
    assert db.get_capture("a1:approved")["kind"] == "approved"
    assert tap(client, "deny", "a1").status_code == 409
    assert tap(client, "approve", "a1").status_code == 409


def test_deny_records_and_notifies(client, notifications):
    db.insert_capture("a2", 1, "delete it", None)
    db.finish_capture("a2", "done", summary="waiting")
    db.set_pending_approval("a2", json.dumps({"action": "Delete X", "details": "t1"}))
    assert tap(client, "deny", "a2").status_code == 200
    child = db.get_capture("a2:denied")
    assert child["status"] == "done" and "nothing was done" in child["summary"]
    assert any(n["title"] == "Denied" for n in notifications)


# --- reversal tools ---------------------------------------------------------------

@respx.mock
async def test_reversal_tools_hit_the_right_endpoints(monkeypatch):
    from app.tools import gcal, notion, ticktick

    tt = respx.delete("https://api.ticktick.com/open/v1/project/p1/task/t1").mock(
        return_value=Response(200))
    assert await ticktick.delete_task({"task_id": "t1", "list_id": "p1"}) == "deleted task t1 (list_id p1)"
    assert tt.called

    trash = respx.patch("https://api.notion.com/v1/pages/pg1").mock(return_value=Response(200, json={}))
    assert "trash" in await notion.trash_page({"page_id": "pg1"})
    assert json.loads(trash.calls[0].request.content) == {"archived": True}

    async def token():
        return "tok"

    monkeypatch.setattr(gcal, "_access_token", token)
    ev = respx.delete("https://www.googleapis.com/calendar/v3/calendars/primary/events/e1").mock(
        return_value=Response(204))
    assert await gcal.delete_event({"event_id": "e1"}) == "deleted event e1"
    assert ev.called


# --- readable notifications ---------------------------------------------------------

def test_plain_strips_links_and_markdown():
    from app.worker import plain

    text = 'TickTick: **"Go dancing"** -> Unsorted, due today https://ticktick.com/webapp/#p/1/tasks/2\nNotion: added (https://www.notion.so/abc)'
    assert plain(text) == 'TickTick: "Go dancing" -> Unsorted, due today\nNotion: added'


async def test_approval_shows_message_not_ids(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, mode=None):
        return AgentResult(summary="Waiting", trace=[], pending_approval={
            "action": "Delete 10 tasks", "message": "Delete the 10 test tasks.",
            "details": "task_id aaaa1111bbbb2222cccc3333 list_id list0001"})

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("a9", 1, "delete the tests", None)
    await run_next("a9")
    assert notifications[0]["message"] == "Delete the 10 test tasks."
    assert "aaaa1111" in json.loads(db.get_capture("a9")["pending_approval"])["details"]


async def test_agent_reported_failure_gets_red_x(monkeypatch, notifications):
    async def fake_run(transcription, capture_id=None, mode=None):
        return AgentResult(summary="FAILED: couldn't find a list named Garden", trace=[])

    monkeypatch.setattr("app.agent.run_capture", fake_run)
    db.insert_capture("f1", 1, "add weeding to garden", None)
    await run_next("f1")
    assert notifications[0]["tags"] == "x"
    assert notifications[0]["message"] == "couldn't find a list named Garden"
