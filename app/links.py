"""What a capture changed, read back from its tool trace.

Used for the notification's tap target (open the item just created) and to
decide whether a capture has anything to Undo."""

import json
import re

TICKTICK_WEB = "https://ticktick.com/webapp/#p"

# Tools that change something in the user's apps.
MUTATING = {
    "ticktick_add_task", "ticktick_update_task", "ticktick_complete_task",
    "ticktick_delete_task",
    "calendar_create_event", "calendar_delete_event",
    "notion_append", "notion_add_database_row", "notion_create_page",
    "notion_trash_page",
    "notion-create-pages", "notion-update-page",
}
# Deletions leave nothing to open.
_NO_LINK = {"ticktick_delete_task", "calendar_delete_event", "notion_trash_page"}

_URL_FIELD = re.compile(r"url: (\S+)")
_NOTION_URL = re.compile(r"https://(?:www\.)?notion\.so/[^\s\"'<>)\]]+")


def notion_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def _find_key(obj, key: str):
    """First value for `key` anywhere in a nested dict/list (MCP inputs nest)."""
    if isinstance(obj, dict):
        if isinstance(obj.get(key), str):
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found:
                return found
    return None


def _entry_link(entry: dict) -> str | None:
    tool = entry.get("tool", "")
    tool_input = entry.get("input") or {}
    result = str(entry.get("result") or "")
    if tool in _NO_LINK:
        return None
    if tool.startswith("notion-"):
        found = _NOTION_URL.search(result)
        if found:
            return found.group(0)
        page_id = _find_key(tool_input, "page_id")
        return notion_url(page_id) if page_id else None
    found = _URL_FIELD.search(result)
    if found and found.group(1) not in ("None", ""):
        return found.group(1)
    if tool.startswith("ticktick_") and tool_input.get("task_id") and tool_input.get("list_id"):
        return f"{TICKTICK_WEB}/{tool_input['list_id']}/tasks/{tool_input['task_id']}"
    if tool == "notion_append" and tool_input.get("page_id"):
        return notion_url(tool_input["page_id"])
    return None


def mutations(trace) -> list[dict]:
    """Successful mutating entries, in order. Accepts a list or trace JSON."""
    if isinstance(trace, str):
        try:
            trace = json.loads(trace)
        except ValueError:
            return []
    return [e for e in trace or [] if e.get("tool") in MUTATING and not e.get("is_error")]


def primary_link(trace) -> str | None:
    """URL of the most recent thing the capture created or changed."""
    for entry in reversed(mutations(trace)):
        link = _entry_link(entry)
        if link:
            return link
    return None
