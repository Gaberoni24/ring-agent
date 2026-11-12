import json

import pytest
import respx
from httpx import Response

from app import tools
from app.config import settings
from app.tools import ToolError, ticktick

BASE = "https://api.ticktick.com/open/v1"
PROJECTS = [
    {"id": "p-hw", "name": "Hardware", "closed": False},
    {"id": "p-old", "name": "Archive", "closed": True},
]


@pytest.fixture(autouse=True)
def eastern(monkeypatch):
    monkeypatch.setattr(settings, "ring_tz", "America/New_York")


@respx.mock
async def test_add_task_resolves_list_and_timed_due():
    respx.get(f"{BASE}/project").mock(return_value=Response(200, json=PROJECTS))
    created = respx.post(f"{BASE}/task").mock(
        return_value=Response(
            200,
            json={
                "id": "t1",
                "projectId": "p-hw",
                "title": "Review PCB layout",
                "dueDate": "2026-10-17T20:00:00+0000",
            },
        )
    )

    result = await ticktick.add_task(
        {
            "title": "Review PCB layout",
            "project": "hardware",
            "due": "2026-10-17T16:00",
            "priority": "high",
        }
    )

    sent = json.loads(created.calls[0].request.content)
    assert sent == {
        "title": "Review PCB layout",
        "startDate": "2026-10-17T16:00:00-0400",
        "dueDate": "2026-10-17T16:00:00-0400",
        "isAllDay": False,
        "timeZone": "America/New_York",
        "reminders": ["TRIGGER:PT0S"],
        "priority": 5,
        "projectId": "p-hw",
    }
    auth = created.calls[0].request.headers["authorization"]
    assert auth == f"Bearer {settings.ticktick_api_token}"
    assert "created task t1 (list_id p-hw)" in result
    assert "ticktick.com/webapp/#p/p-hw/tasks/t1" in result


@respx.mock
async def test_add_task_all_day_defaults_to_inbox():
    created = respx.post(f"{BASE}/task").mock(
        return_value=Response(200, json={"id": "t2", "projectId": "inbox123", "title": "x"})
    )
    await ticktick.add_task({"title": "x", "due": "2026-10-17", "project": "Inbox"})
    sent = json.loads(created.calls[0].request.content)
    assert "projectId" not in sent
    assert sent["isAllDay"] is True and "reminders" not in sent
    assert sent["dueDate"] == "2026-10-17T00:00:00-0400"


@respx.mock
async def test_unknown_list_is_tool_error_and_closed_lists_hidden():
    respx.get(f"{BASE}/project").mock(return_value=Response(200, json=PROJECTS))
    with pytest.raises(ToolError, match="no TickTick list named 'Archive'") as exc:
        await ticktick.add_task({"title": "x", "project": "Archive"})
    assert "available: Inbox, Hardware." in str(exc.value)


async def test_bad_due_is_tool_error():
    with pytest.raises(ToolError, match="bad due"):
        await ticktick.add_task({"title": "x", "due": "next friday"})


@respx.mock
async def test_update_task_edits_then_moves():
    respx.get(f"{BASE}/project").mock(return_value=Response(200, json=PROJECTS))
    edited = respx.post(f"{BASE}/task/t1").mock(return_value=Response(200, json={}))
    moved = respx.post(f"{BASE}/task/move").mock(return_value=Response(200, json=[]))
    respx.get(f"{BASE}/project/inbox1/task/t1").mock(
        return_value=Response(200, json={
            "id": "t1", "projectId": "inbox1", "title": "Call mum", "priority": 3,
            "dueDate": "2026-10-20T04:00:00.000+0000", "isAllDay": True,
        })
    )
    respx.get(f"{BASE}/project/p-hw/task/t1").mock(
        return_value=Response(200, json={"id": "t1", "projectId": "p-hw", "title": "Call mom"})
    )

    result = await ticktick.update_task(
        {"task_id": "t1", "list_id": "inbox1", "title": "Call mom", "project": "Hardware"}
    )

    assert json.loads(edited.calls[0].request.content) == {
        "title": "Call mom",
        "id": "t1",
        "projectId": "inbox1",
    }
    assert json.loads(moved.calls[0].request.content) == [
        {"fromProjectId": "inbox1", "toProjectId": "p-hw", "taskId": "t1"}
    ]
    assert "updated task t1 (list_id p-hw)" in result and "-> Hardware" in result
    # Pre-edit values in local tool format, so Undo can pass them straight back.
    assert "previously: title='Call mum', due=2026-10-20, list_id=inbox1, priority=medium" in result


@respx.mock
async def test_update_task_bad_list_changes_nothing():
    respx.get(f"{BASE}/project").mock(return_value=Response(200, json=PROJECTS))
    edited = respx.post(f"{BASE}/task/t1")
    with pytest.raises(ToolError, match="no TickTick list"):
        await ticktick.update_task(
            {"task_id": "t1", "list_id": "p-hw", "priority": "low", "project": "Garden"}
        )
    assert not edited.called


@respx.mock
async def test_update_task_unknown_id_is_tool_error():
    respx.get(f"{BASE}/project/p/task/nope").mock(return_value=Response(404))
    with pytest.raises(ToolError, match="no TickTick task nope"):
        await ticktick.update_task({"task_id": "nope", "list_id": "p", "title": "x"})


async def test_update_task_requires_a_change():
    with pytest.raises(ToolError, match="nothing to change"):
        await ticktick.update_task({"task_id": "t1", "list_id": "p"})


@respx.mock
async def test_complete_task():
    done = respx.post(f"{BASE}/project/p-hw/task/t1/complete").mock(
        return_value=Response(200)
    )
    assert await ticktick.complete_task({"task_id": "t1", "list_id": "p-hw"}) == "completed task t1"
    assert done.called


@respx.mock
async def test_dispatch_reports_http_errors_to_agent():
    respx.post(f"{BASE}/task").mock(return_value=Response(403, text="forbidden"))
    result, is_error = await tools.dispatch("ticktick_add_task", {"title": "x"})
    assert is_error is True
    assert "403" in result and "forbidden" in result


async def test_unconfigured_ticktick_is_tool_error(monkeypatch):
    monkeypatch.setattr(settings, "ticktick_api_token", "")
    result, is_error = await tools.dispatch("ticktick_add_task", {"title": "x"})
    assert is_error is True and "not configured" in result


@respx.mock
async def test_list_name_match_ignores_emoji_and_case():
    respx.get(f"{BASE}/project").mock(
        return_value=Response(200, json=[{"id": "p-u", "name": "📌Unsorted Tasks"}])
    )
    created = respx.post(f"{BASE}/task").mock(
        return_value=Response(200, json={"id": "t3", "projectId": "p-u", "title": "x"})
    )
    await ticktick.add_task({"title": "x", "project": "unsorted tasks"})
    assert json.loads(created.calls[0].request.content)["projectId"] == "p-u"
