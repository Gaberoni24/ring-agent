"""Conversation history: what the ring said before and what was done.

Captures are one-shot — the user can't answer a clarifying question — so
the only way to resolve "actually, put that in Hardware" or "same as the
last one" is the log of previous captures. The last few are injected into
every system prompt (render_recent); recall_history lets the agent reach
further back or search when the recent block isn't enough."""

import json
import time

from app import db
from app.config import settings
from app.tools import register

RECENT_IN_PROMPT = 6
TRACE_RESULT_CHARS = 160
MAX_TRACE_ENTRIES = 5
_SKIP_IN_TRACE = {"ticktick_list_projects", "calendar_list_events", "notion_search", "notion-search", "notion-fetch"}  # lookups


def _relative(ms: int, now_ms: int) -> str:
    delta = max(0, now_ms - ms) // 1000
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} d ago"


def _absolute(ms: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000, settings.tz).strftime("%a %Y-%m-%d %H:%M")


def _trace_lines(raw: str | None, full: bool) -> list[str]:
    if not raw:
        return []
    try:
        trace = json.loads(raw)
    except (TypeError, ValueError):
        return []
    lines = []
    for entry in trace:
        name = entry.get("tool", "?")
        if not full and name in _SKIP_IN_TRACE:
            continue
        result = " ".join(str(entry.get("result", "")).split())
        if not full:
            result = result[:TRACE_RESULT_CHARS]
        flag = " [ERROR]" if entry.get("is_error") else ""
        if full:
            args = json.dumps(entry.get("input", {}), ensure_ascii=False)
            lines.append(f"    {name}({args}){flag} -> {result}")
        else:
            lines.append(f"    {name}{flag} -> {result}")
    if not full and len(lines) > MAX_TRACE_ENTRIES:
        lines = lines[:MAX_TRACE_ENTRIES] + [f"    ... {len(lines) - MAX_TRACE_ENTRIES} more"]
    return lines


def render(rows, full: bool = False) -> str:
    """Render capture rows (newest first) as a compact transcript."""
    if not rows:
        return "(none)"
    now_ms = int(time.time() * 1000)
    out = []
    for row in rows:
        when = f"{_relative(row['created_at'], now_ms)} ({_absolute(row['created_at'])})"
        status = "" if row["status"] == "done" else f" [{row['status'].upper()}]"
        keys = row.keys()
        if "kind" in keys and row["kind"] != "ring":
            status += f" [{row['kind'].upper()} via phone button, of {row['parent_id']}]"
        if "pending_approval" in keys and row["pending_approval"]:
            status += " [AWAITING APPROVAL]"
        out.append(f"[{when}]{status}")
        out.append(f'  User said: "{row["transcription"] or ""}"')
        out.append(f"  Outcome: {row['summary'] or '(no summary)'}")
        out.extend(_trace_lines(row["tool_trace"], full))
    return "\n".join(out)


def render_recent(exclude_id: str | None = None) -> str:
    try:
        rows = db.recent_captures(RECENT_IN_PROMPT, exclude_id=exclude_id)
    except Exception:  # db not initialised, corrupt, etc. — never block a capture
        return "(history unavailable)"
    return render(rows)


async def recall(tool_input: dict) -> str:
    query = (tool_input.get("query") or "").strip()
    limit = max(1, min(int(tool_input.get("limit") or 10), 30))
    rows = db.search_captures(query, limit) if query else db.recent_captures(limit)
    if not rows:
        return f"no captures matching {query!r}" if query else "no earlier captures"
    return render(rows, full=True)


register(
    {
        "name": "recall_history",
        "description": (
            "Look up earlier ring captures: what the user said, what you did, "
            "and the full tool calls with ids/urls. The most recent few are "
            "already in your instructions; use this to go further back or to "
            "search by keyword (e.g. a task name) when the user refers to "
            "something you can't see. Use this BEFORE deciding a capture is "
            "ambiguous."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to match in transcripts, summaries or tool results; omit for the most recent captures",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max captures to return (default 10, max 30)",
                },
            },
        },
    },
    recall,
)
