import json

import respx
from httpx import Response

from app import tools
from app.config import settings
from app.tools import gcal

EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class FakeCreds:
    valid = True
    token = "goog-token"


def configured(monkeypatch):
    monkeypatch.setattr(gcal, "_creds", FakeCreds())
    monkeypatch.setattr(settings, "ring_tz", "America/New_York")


async def test_unconfigured_redirects_to_ticktick():
    result, is_error = await tools.dispatch(
        "calendar_create_event", {"title": "dentist", "start": "2026-10-20T10:00"}
    )
    assert is_error is True
    assert "not configured" in result and "TickTick" in result


@respx.mock
async def test_create_timed_event_defaults_to_one_hour(monkeypatch):
    configured(monkeypatch)
    created = respx.post(EVENTS).mock(
        return_value=Response(
            200,
            json={
                "id": "e1",
                "summary": "Dentist",
                "start": {"dateTime": "2026-10-20T10:00:00-04:00"},
                "htmlLink": "https://calendar.google.com/event?eid=e1",
            },
        )
    )

    result = await gcal.create_event({"title": "Dentist", "start": "2026-10-20T10:00"})

    sent = json.loads(created.calls[0].request.content)
    assert sent == {
        "summary": "Dentist",
        "start": {"dateTime": "2026-10-20T10:00:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-20T11:00:00", "timeZone": "America/New_York"},
    }
    assert created.calls[0].request.headers["authorization"] == "Bearer goog-token"
    assert "created event e1" in result and "calendar.google.com" in result


@respx.mock
async def test_create_all_day_event_end_is_exclusive(monkeypatch):
    configured(monkeypatch)
    created = respx.post(EVENTS).mock(
        return_value=Response(200, json={"id": "e2", "start": {"date": "2026-10-20"}})
    )
    await gcal.create_event({"title": "Trip", "start": "2026-10-20"})
    sent = json.loads(created.calls[0].request.content)
    assert sent["start"] == {"date": "2026-10-20"}
    assert sent["end"] == {"date": "2026-10-21"}


@respx.mock
async def test_list_events(monkeypatch):
    configured(monkeypatch)
    listed = respx.get(EVENTS).mock(
        return_value=Response(
            200,
            json={"items": [{"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-10-20T09:00:00-04:00"}}]},
        )
    )
    result = await gcal.list_events({"from_date": "2026-10-20"})
    params = listed.calls[0].request.url.params
    assert params["timeMin"] == "2026-10-20T00:00:00-04:00"
    assert params["timeMax"] == "2026-10-21T00:00:00-04:00"
    assert "Standup" in result


async def test_bad_date_is_tool_error(monkeypatch):
    configured(monkeypatch)
    result, is_error = await tools.dispatch("calendar_create_event", {"title": "x", "start": "tuesday"})
    assert is_error is True and "bad date" in result
