"""Tool registry for the agent loop.

Each tool is (Anthropic tool schema, async handler taking the tool_input
dict and returning a string result). Handlers raise ToolError for failures
the agent should see and react to; anything else bubbles up and fails the
capture."""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

import httpx

from app.config import settings

# "normal" for a ring capture; "approved" / "undo" when the run was started
# by a notification button, which is itself the user's approval.
run_mode: ContextVar[str] = ContextVar("run_mode", default="normal")
# What the user actually said for this run (the ring transcript). Tool
# results (calendar invites, Notion pages) can contain text written by
# others; checks that must only follow the user's own words read this.
spoken: ContextVar[str] = ContextVar("spoken", default="")

NEEDS_APPROVAL = (
    "BLOCKED: {name} needs the user's approval first. Call request_approval "
    "describing exactly what you will do (with the ids), then stop."
)


class ToolError(Exception):
    """Failure the agent should be told about (bad id, API 4xx, etc.)."""


# One shared client for all outbound API calls.
http = httpx.AsyncClient(timeout=30)

Handler = Callable[[dict], Awaitable[str]]
_registry: dict[str, tuple[dict, Handler]] = {}


def register(schema: dict, handler: Handler) -> None:
    _registry[schema["name"]] = (schema, handler)


def schemas() -> list[dict]:
    return [schema for schema, _ in _registry.values()]


async def dispatch(name: str, tool_input: dict) -> tuple[str, bool]:
    """Run a tool. Returns (result_text, is_error)."""
    if name not in _registry:
        return f"unknown tool: {name}", True
    if name in settings.approval_required and run_mode.get() == "normal":
        return NEEDS_APPROVAL.format(name=name), True
    _, handler = _registry[name]
    try:
        return await handler(tool_input), False
    except ToolError as exc:
        return str(exc), True
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        return f"{name} HTTP {exc.response.status_code}: {body}", True


# Importing the modules registers their tools.
from app.tools import approval, gcal, history, memory, notify, notion, ticktick  # noqa: E402,F401
