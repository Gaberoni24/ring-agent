"""Push notifications via ntfy.sh."""

from app.config import settings
from app.tools import ToolError, http, register


async def send(
    message: str,
    title: str | None = None,
    tags: str | None = None,
    *,
    click: str | None = None,
    actions: list[dict] | None = None,
    priority: int | None = None,
) -> str:
    """click: URL opened when the notification body is tapped. actions: up
    to 3 ntfy buttons (see app/actions.py). priority: 1-5, 5 = urgent."""
    if not settings.ntfy_topic:
        raise ToolError("ntfy is not configured (NTFY_TOPIC missing)")
    # JSON publish endpoint (POST to server root): unlike header-based
    # publishing it is UTF-8 safe, so emoji in titles are fine.
    payload: dict = {"topic": settings.ntfy_topic, "message": message}
    if title:
        payload["title"] = title
    if tags:
        payload["tags"] = tags.split(",")
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions[:3]
    if priority:
        payload["priority"] = priority
    resp = await http.post(settings.ntfy_server, json=payload)
    resp.raise_for_status()
    return "notification sent"


async def _tool_send(tool_input: dict) -> str:
    return await send(tool_input["message"], title=tool_input.get("title"))


register(
    {
        "name": "notify",
        "description": (
            "Send a push notification to the user's phone. Use this to answer "
            "questions directly. Do NOT use it for the final action summary — "
            "that is sent automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["message"],
        },
    },
    _tool_send,
)
