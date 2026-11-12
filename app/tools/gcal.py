"""Google Calendar tools (Calendar API v3).

Auth: run scripts/setup_google.py once on your laptop, then copy the
resulting google_token.json to the server. The token is refreshed here as
needed and written back so the refresh survives restarts. Until the token
exists, the tools degrade gracefully: the agent is told to put the event in
TickTick instead of dropping it."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings
from app.tools import ToolError, http, register

BASE = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

NOT_CONFIGURED = (
    "Google Calendar is not configured yet. Do not retry. Instead, create "
    "a TickTick task for this event (title = event title, due = its "
    "date/time) and say in your summary that Calendar is not set up."
)

_creds = None


def _token_path() -> Path:
    path = Path(settings.google_token_file)
    return path if path.is_absolute() else settings.data_dir / path


async def _access_token() -> str | None:
    """A valid access token, refreshing it if needed; None if unconfigured."""
    global _creds
    if _creds is None:
        path = _token_path()
        if not path.exists():
            return None
        from google.oauth2.credentials import Credentials

        _creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not _creds.valid:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request

        try:
            await asyncio.to_thread(_creds.refresh, Request())
        except RefreshError as exc:
            raise ToolError(
                f"Google token refresh failed ({exc}). Re-run scripts/setup_google.py "
                "and copy the new google_token.json to the server. Meanwhile put "
                "the event in TickTick and say so."
            ) from exc
        _token_path().write_text(_creds.to_json(), encoding="utf-8")
    return _creds.token


def _when(value: str) -> dict:
    """'2026-10-17' -> all-day; '2026-10-17T16:00' -> timed in RING_TZ."""
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"bad date {value!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM") from exc
    if len(value) <= 10:
        return {"date": value}
    # The API rejects datetimes without seconds; RFC 3339 needs HH:MM:SS.
    return {"dateTime": parsed.isoformat(timespec="seconds"), "timeZone": settings.ring_tz}


async def _request(method: str, path: str, **kwargs) -> dict:
    token = await _access_token()
    if token is None:
        raise ToolError(NOT_CONFIGURED)
    resp = await http.request(
        method, f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    resp.raise_for_status()
    return resp.json()


def _calendar() -> str:
    return settings.google_calendar_id


async def create_event(tool_input: dict) -> str:
    start = _when(tool_input["start"])
    if tool_input.get("end"):
        end = _when(tool_input["end"])
    elif "date" in start:  # all-day: Google's end date is exclusive
        nxt = datetime.fromisoformat(start["date"]) + timedelta(days=1)
        end = {"date": nxt.date().isoformat()}
    else:
        nxt = datetime.fromisoformat(start["dateTime"]) + timedelta(hours=1)
        end = {"dateTime": nxt.isoformat(timespec="seconds"), "timeZone": settings.ring_tz}
    body: dict = {"summary": tool_input["title"], "start": start, "end": end}
    for key in ("description", "location"):
        if tool_input.get(key):
            body[key] = tool_input[key]
    event = await _request("POST", f"/calendars/{_calendar()}/events", json=body)
    when = event["start"].get("dateTime") or event["start"].get("date")
    return f"created event {event['id']}: {event.get('summary')!r} at {when}, url: {event.get('htmlLink')}"


async def delete_event(tool_input: dict) -> str:
    token = await _access_token()
    if token is None:
        raise ToolError("Google Calendar is not configured; the event cannot be deleted.")
    event_id = str(tool_input["event_id"]).strip()
    resp = await http.delete(
        f"{BASE}/calendars/{_calendar()}/events/{event_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code in (404, 410):  # 410 = already deleted
        raise ToolError(f"no calendar event {event_id} (already deleted?)")
    resp.raise_for_status()
    return f"deleted event {event_id}"


async def list_events(tool_input: dict) -> str:
    to = tool_input.get("to_date") or tool_input["from_date"]
    try:
        start = datetime.fromisoformat(tool_input["from_date"]).replace(tzinfo=settings.tz)
        end = datetime.fromisoformat(to).replace(tzinfo=settings.tz) + timedelta(days=1)
    except ValueError as exc:
        raise ToolError("from_date/to_date must be YYYY-MM-DD") from exc
    data = await _request(
        "GET",
        f"/calendars/{_calendar()}/events",
        params={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 50,
        },
    )
    items = data.get("items", [])
    if not items:
        return "no events in that range"
    return "\n".join(
        f"- {e['start'].get('dateTime') or e['start'].get('date')}: "
        f"{e.get('summary', '(no title)')} (id {e['id']})"
        for e in items
    )


register(
    {
        "name": "calendar_create_event",
        "description": (
            "Create a Google Calendar event. start/end are local dates "
            "(YYYY-MM-DD, all-day) or datetimes (YYYY-MM-DDTHH:MM). end is "
            "optional: defaults to 1 hour after start (or one full day)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "location": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title", "start"],
        },
    },
    create_event,
)

register(
    {
        "name": "calendar_delete_event",
        "description": (
            "Delete a Google Calendar event by id. Needs the user's approval "
            "(it is refused otherwise) unless this run is an Undo or an "
            "approved plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
    },
    delete_event,
)

register(
    {
        "name": "calendar_list_events",
        "description": (
            "List Google Calendar events between two local dates (inclusive). "
            "Use for questions like 'what's on tomorrow?' — then answer with notify."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD, defaults to from_date"},
            },
            "required": ["from_date"],
        },
    },
    list_events,
)
