"""TickTick tools (Open API v1, personal API token).

Get a token at ticktick.com/webapp -> avatar -> Settings -> Account -> API
Token. TickTick does not parse natural-language dates, so
the agent passes ISO local dates/times and they are converted here using
RING_TZ."""

from datetime import datetime

from app.config import settings
from app.tools import ToolError, http, register

BASE = "https://api.ticktick.com/open/v1"
WEB = "https://ticktick.com/webapp/#p"

PRIORITIES = {"none": 0, "low": 1, "medium": 3, "high": 5}


def _headers() -> dict:
    if not settings.ticktick_api_token:
        raise ToolError("TickTick is not configured (TICKTICK_API_TOKEN missing)")
    return {"Authorization": f"Bearer {settings.ticktick_api_token}"}


# Project (list) names rarely change; cache for the process lifetime and
# refresh only when a lookup misses.
_project_cache: list[dict] | None = None


async def _projects(force: bool = False) -> list[dict]:
    global _project_cache
    if _project_cache is None or force:
        resp = await http.get(f"{BASE}/project", headers=_headers())
        resp.raise_for_status()
        _project_cache = [p for p in resp.json() if not p.get("closed")]
    return _project_cache


async def list_projects(tool_input: dict) -> str:
    projects = await _projects(force=True)
    lines = ["- Inbox (default when no list is given)"]
    lines += [f"- {p['name']} (id {p['id']})" for p in projects]
    return "\n".join(lines)


def _norm(name: str) -> str:
    """Compare list names ignoring case, emoji and punctuation, so
    'unsorted tasks' finds '📌Unsorted Tasks'."""
    return " ".join("".join(c if c.isalnum() else " " for c in name.lower()).split())


async def _project_id(name: str, on_miss: str) -> str | None:
    """Resolve a list name to its id; None means the Inbox."""
    wanted = _norm(name)
    if wanted == "inbox":
        return None
    match = next((p for p in await _projects() if _norm(p["name"]) == wanted), None)
    if match is None:  # cache may be stale
        match = next(
            (p for p in await _projects(force=True) if _norm(p["name"]) == wanted),
            None,
        )
    if match is None:
        names = ", ".join(["Inbox"] + [p["name"] for p in await _projects()])
        raise ToolError(f"no TickTick list named {name!r}; available: {names}. {on_miss}")
    return match["id"]


def _ticktick_time(value: str) -> tuple[str, bool]:
    """'2026-10-17' (all-day) or '2026-10-17T16:00' (timed), local to
    RING_TZ -> (TickTick timestamp, is_all_day)."""
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(
            f"bad due {value!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM (local time)"
        ) from exc
    all_day = len(value) <= 10
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=settings.tz)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S%z"), all_day


def _fields(tool_input: dict) -> dict:
    """Editable task fields shared by create and update."""
    payload: dict = {}
    for key in ("title", "content"):
        if tool_input.get(key) is not None:
            payload[key] = tool_input[key]
    if tool_input.get("due"):
        stamp, all_day = _ticktick_time(tool_input["due"])
        payload.update(
            startDate=stamp, dueDate=stamp, isAllDay=all_day, timeZone=settings.ring_tz
        )
        if not all_day:
            payload["reminders"] = ["TRIGGER:PT0S"]  # ping at the due time
    if tool_input.get("priority"):
        level = str(tool_input["priority"]).lower()
        if level not in PRIORITIES:
            raise ToolError(f"priority must be one of {', '.join(PRIORITIES)}")
        payload["priority"] = PRIORITIES[level]
    if tool_input.get("tags") is not None:
        payload["tags"] = tool_input["tags"]
    if tool_input.get("repeat"):
        payload["repeatFlag"] = tool_input["repeat"]
    return payload


def _local_due(task: dict) -> str:
    """A task's due in the same local format the tools accept, so a
    'previously' value can be passed straight back to restore it."""
    raw = task.get("dueDate")
    if not raw:
        return "none"
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return raw
    local = parsed.astimezone(settings.tz)
    return local.strftime("%Y-%m-%d" if task.get("isAllDay") else "%Y-%m-%dT%H:%M")


def _previously(task: dict) -> str:
    """Pre-edit values, recorded in the result so Undo can restore them."""
    level = next((k for k, v in PRIORITIES.items() if v == task.get("priority", 0)), "none")
    return (
        f"previously: title={task.get('title')!r}, due={_local_due(task)}, "
        f"list_id={task.get('projectId')}, priority={level}, tags={task.get('tags') or []}"
    )


def _describe(task: dict) -> str:
    due = task.get("dueDate") or "no due date"
    url = f"{WEB}/{task['projectId']}/tasks/{task['id']}"
    return (
        f"{task['id']} (list_id {task['projectId']}): {task.get('title')!r}, "
        f"due: {due}, url: {url}"
    )


async def add_task(tool_input: dict) -> str:
    payload = _fields(tool_input)
    if tool_input.get("project"):
        project_id = await _project_id(
            tool_input["project"],
            "Task NOT created — retry with a valid list or omit it for Inbox.",
        )
        if project_id:
            payload["projectId"] = project_id
    resp = await http.post(f"{BASE}/task", headers=_headers(), json=payload)
    resp.raise_for_status()
    return f"created task {_describe(resp.json())}"


async def update_task(tool_input: dict) -> str:
    task_id = str(tool_input["task_id"]).strip()
    list_id = str(tool_input["list_id"]).strip()
    payload = _fields(tool_input)
    project = tool_input.get("project")
    if not payload and not project:
        raise ToolError("nothing to change — pass at least one field to update")
    # Resolve the target list first so a bad name doesn't leave a half-applied edit.
    target = None
    if project:
        target = await _project_id(project, "Task NOT changed — retry with a valid list.")
        if target is None:
            raise ToolError("moving a task back to the Inbox is not supported; pick a list")

    resp = await http.get(f"{BASE}/project/{list_id}/task/{task_id}", headers=_headers())
    if resp.status_code == 404:
        raise ToolError(
            f"no TickTick task {task_id} in list {list_id}; find both ids with recall_history"
        )
    resp.raise_for_status()
    before = resp.json()

    if payload:
        payload.update(id=task_id, projectId=list_id)
        resp = await http.post(f"{BASE}/task/{task_id}", headers=_headers(), json=payload)
        if resp.status_code == 404:
            raise ToolError(
                f"no TickTick task {task_id} in list {list_id}; find both ids with recall_history"
            )
        resp.raise_for_status()
    if target and target != list_id:
        resp = await http.post(
            f"{BASE}/task/move",
            headers=_headers(),
            json=[{"fromProjectId": list_id, "toProjectId": target, "taskId": task_id}],
        )
        resp.raise_for_status()
        list_id = target

    resp = await http.get(f"{BASE}/project/{list_id}/task/{task_id}", headers=_headers())
    resp.raise_for_status()
    moved = f" -> {project}" if project else ""
    return f"updated task {_describe(resp.json())}{moved}; {_previously(before)}"


async def complete_task(tool_input: dict) -> str:
    task_id = str(tool_input["task_id"]).strip()
    list_id = str(tool_input["list_id"]).strip()
    resp = await http.post(
        f"{BASE}/project/{list_id}/task/{task_id}/complete", headers=_headers()
    )
    if resp.status_code == 404:
        raise ToolError(f"no TickTick task {task_id} in list {list_id}")
    resp.raise_for_status()
    return f"completed task {task_id}"


async def delete_task(tool_input: dict) -> str:
    task_id = str(tool_input["task_id"]).strip()
    list_id = str(tool_input["list_id"]).strip()
    resp = await http.delete(f"{BASE}/project/{list_id}/task/{task_id}", headers=_headers())
    if resp.status_code == 404:
        raise ToolError(f"no TickTick task {task_id} in list {list_id}")
    resp.raise_for_status()
    return f"deleted task {task_id} (list_id {list_id})"


_EDITABLE = {
    "title": {"type": "string", "description": "Task title"},
    "content": {"type": "string", "description": "Optional longer notes"},
    "due": {
        "type": "string",
        "description": "Local date YYYY-MM-DD (all-day) or datetime YYYY-MM-DDTHH:MM (timed, reminds at that time)",
    },
    "project": {"type": "string", "description": "Existing TickTick list name"},
    "tags": {"type": "array", "items": {"type": "string"}},
    "priority": {"type": "string", "enum": list(PRIORITIES)},
    "repeat": {
        "type": "string",
        "description": "iCal RRULE for recurring tasks, e.g. RRULE:FREQ=WEEKLY;BYDAY=MO",
    },
}

register(
    {
        "name": "ticktick_add_task",
        "description": (
            "Create a task in TickTick. Convert spoken dates ('tomorrow', "
            "'friday 4pm') to an explicit local due using the current time "
            "in your instructions; omit due when no date was given unless the "
            "user's conventions say otherwise. project must be an existing "
            "list name (emoji may be omitted); omit it only for the Inbox."
        ),
        "input_schema": {
            "type": "object",
            "properties": _EDITABLE,
            "required": ["title"],
        },
    },
    add_task,
)

register(
    {
        "name": "ticktick_update_task",
        "description": (
            "Edit an existing TickTick task in place — use this (never a new "
            "task) when the user corrects or adjusts a task you created. Get "
            "task_id and list_id from the 'created task <id> (list_id <id>)' "
            "result in history (recent captures or recall_history). Pass ONLY "
            "the fields to change; others are left as-is. title replaces the "
            "title; tags replaces the whole tag list; project moves the task "
            "to that existing list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "list_id": {"type": "string", "description": "The task's current list_id"},
                **_EDITABLE,
            },
            "required": ["task_id", "list_id"],
        },
    },
    update_task,
)

register(
    {
        "name": "ticktick_complete_task",
        "description": (
            "Mark a TickTick task done (e.g. 'I already did that'). Needs "
            "task_id and list_id from history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "list_id": {"type": "string"}},
            "required": ["task_id", "list_id"],
        },
    },
    complete_task,
)

register(
    {
        "name": "ticktick_delete_task",
        "description": (
            "Permanently delete a TickTick task. Needs the user's approval "
            "(it is refused otherwise) unless this run is an Undo or an "
            "approved plan. Needs task_id and list_id from history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "list_id": {"type": "string"}},
            "required": ["task_id", "list_id"],
        },
    },
    delete_task,
)

register(
    {
        "name": "ticktick_list_projects",
        "description": "List TickTick lists (name and id). Use to check what exists before routing a task.",
        "input_schema": {"type": "object", "properties": {}},
    },
    list_projects,
)
