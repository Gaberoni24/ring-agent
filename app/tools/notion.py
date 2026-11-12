"""Notion API tools + a minimal markdown -> blocks converter.

The integration token only sees pages explicitly shared with it — if a
search comes back empty, that's the first thing to suspect."""

import re

from app.config import settings
from app.tools import ToolError, http, register

BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers() -> dict:
    if not settings.notion_token:
        raise ToolError("Notion is not configured (NOTION_TOKEN missing)")
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": NOTION_VERSION,
    }


def _rich_text(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text}}]


def markdown_to_blocks(markdown: str) -> list[dict]:
    """Paragraphs, '- ' bullets, '- [ ]'/'- [x]' todos, '#'-'###' headings.
    Anything else becomes a paragraph. Good enough for voice notes."""
    blocks: list[dict] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if m := re.match(r"^(#{1,3})\s+(.*)", stripped):
            level = len(m.group(1))
            blocks.append(
                {
                    "object": "block",
                    "type": f"heading_{level}",
                    f"heading_{level}": {"rich_text": _rich_text(m.group(2))},
                }
            )
        elif m := re.match(r"^- \[( |x|X)\]\s+(.*)", stripped):
            blocks.append(
                {
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": _rich_text(m.group(2)),
                        "checked": m.group(1).lower() == "x",
                    },
                }
            )
        elif m := re.match(r"^[-*]\s+(.*)", stripped):
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": _rich_text(m.group(1))},
                }
            )
        else:
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _rich_text(stripped)},
                }
            )
    return blocks


def _title_of(result: dict) -> str:
    if result["object"] == "database":
        parts = result.get("title", [])
    else:
        title_prop = next(
            (p for p in result.get("properties", {}).values() if p.get("type") == "title"),
            None,
        )
        parts = title_prop["title"] if title_prop else []
    return "".join(p.get("plain_text", "") for p in parts) or "(untitled)"


async def search(tool_input: dict) -> str:
    resp = await http.post(
        f"{BASE}/search",
        headers=_headers(),
        json={"query": tool_input["query"], "page_size": 10},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return (
            "no results — the page may exist but not be shared with the "
            "integration; fall back to the Inbox page"
        )
    return "\n".join(
        f"- [{r['object']}] {_title_of(r)} (id {r['id']})" for r in results
    )


async def append(tool_input: dict) -> str:
    blocks = markdown_to_blocks(tool_input["markdown"])
    if not blocks:
        raise ToolError("markdown produced no content blocks")
    resp = await http.patch(
        f"{BASE}/blocks/{tool_input['page_id']}/children",
        headers=_headers(),
        json={"children": blocks},
    )
    resp.raise_for_status()
    return f"appended {len(blocks)} block(s) to page {tool_input['page_id']}"


async def add_database_row(tool_input: dict) -> str:
    headers = _headers()
    db_id = tool_input["database_id"]
    # The title property's name varies per database ("Name", "Title", ...) —
    # read the schema to find it.
    resp = await http.get(f"{BASE}/databases/{db_id}", headers=headers)
    resp.raise_for_status()
    props = resp.json().get("properties", {})
    title_prop = next(
        (name for name, p in props.items() if p.get("type") == "title"), None
    )
    if title_prop is None:
        raise ToolError(f"database {db_id} has no title property")

    resp = await http.post(
        f"{BASE}/pages",
        headers=headers,
        json={
            "parent": {"database_id": db_id},
            "properties": {title_prop: {"title": _rich_text(tool_input["title"])}},
            "children": markdown_to_blocks(tool_input.get("markdown", "")),
        },
    )
    resp.raise_for_status()
    row = resp.json()
    return (
        f"added row {row['id']}: {tool_input['title']!r} to database {db_id}, "
        f"url: {row.get('url', '')}"
    )


async def create_page(tool_input: dict) -> str:
    resp = await http.post(
        f"{BASE}/pages",
        headers=_headers(),
        json={
            "parent": {"page_id": tool_input["parent_id"]},
            "properties": {"title": {"title": _rich_text(tool_input["title"])}},
            "children": markdown_to_blocks(tool_input.get("markdown", "")),
        },
    )
    resp.raise_for_status()
    page = resp.json()
    return f"created page {page['id']}: {tool_input['title']!r}, url: {page.get('url', '')}"


async def trash_page(tool_input: dict) -> str:
    page_id = tool_input["page_id"]
    resp = await http.patch(
        f"{BASE}/pages/{page_id}", headers=_headers(), json={"archived": True}
    )
    if resp.status_code == 404:
        raise ToolError(
            f"page {page_id} not found or not shared with the integration — "
            "it cannot be trashed through this tool"
        )
    resp.raise_for_status()
    return f"moved page {page_id} to trash"


register(
    {
        "name": "notion_search",
        "description": (
            "Search Notion pages and databases by title. Prefer the workspace "
            "map in your context; use this only when the map doesn't have what "
            "you need."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    search,
)

register(
    {
        "name": "notion_append",
        "description": (
            "Append markdown content (paragraphs, bullets, '- [ ]' todos, "
            "#/## headings) to an existing Notion page by id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "markdown": {"type": "string"},
            },
            "required": ["page_id", "markdown"],
        },
    },
    append,
)

register(
    {
        "name": "notion_add_database_row",
        "description": (
            "Add an item (row) to a Notion database. Use this — NOT "
            "notion_create_page — whenever the target is a database; check "
            "the workspace map's [database] tag. Optional markdown becomes "
            "the item's page content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "database_id": {"type": "string"},
                "title": {"type": "string"},
                "markdown": {"type": "string"},
            },
            "required": ["database_id", "title"],
        },
    },
    add_database_row,
)

register(
    {
        "name": "notion_create_page",
        "description": "Create a new Notion page under a parent page, with optional markdown body.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "string"},
                "title": {"type": "string"},
                "markdown": {"type": "string"},
            },
            "required": ["parent_id", "title"],
        },
    },
    create_page,
)

register(
    {
        "name": "notion_trash_page",
        "description": (
            "Move a Notion page or database row to the trash. Only works for "
            "pages shared with the integration. Needs the user's approval "
            "(it is refused otherwise) unless this run is an Undo or an "
            "approved plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"page_id": {"type": "string"}},
            "required": ["page_id"],
        },
    },
    trash_page,
)
