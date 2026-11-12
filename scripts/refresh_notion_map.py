"""Refresh context/notion_map.json from the Notion search API.

Run manually or via the notion-map systemd timer. The map is a cached index
of everything the integration can see, condensed into the agent's system
prompt so routing doesn't need live searches.

Usage: python scripts/refresh_notion_map.py
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.tools.notion import BASE, NOTION_VERSION, _title_of  # noqa: E402


async def fetch_all() -> list[dict]:
    headers = {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": NOTION_VERSION,
    }
    items: list[dict] = []
    titles: dict[str, str] = {}
    cursor = None
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            resp = await http.post(f"{BASE}/search", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            for r in data["results"]:
                title = _title_of(r)
                titles[r["id"]] = title
                parent = r.get("parent", {})
                items.append(
                    {
                        "id": r["id"],
                        "title": title,
                        "type": r["object"],
                        "parent_id": parent.get("page_id") or parent.get("database_id"),
                    }
                )
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    # Second pass: resolve parent ids to titles where we saw the parent.
    for item in items:
        pid = item.pop("parent_id", None)
        item["parent"] = titles.get(pid) if pid else None
    return items


def main() -> None:
    if not settings.notion_token:
        sys.exit("NOTION_TOKEN is not set — fill in .env first")
    items = asyncio.run(fetch_all())
    out = settings.context_dir / "notion_map.json"
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "items": items,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(items)} items to {out}")
    if not items:
        print(
            "WARNING: 0 items — did you share any pages with the integration? "
            "(Notion page -> ... menu -> Connections -> your integration)"
        )


if __name__ == "__main__":
    main()
