"""Sweep: notify about captures that never completed.

Run by the ring-agent-sweep timer (daily + shortly after boot). Anything
still 'received' or 'failed' after 10 minutes gets surfaced via ntfy so
nothing rots silently — the ring has no retry queue, so a stuck row is the
only remaining trace of a capture. Each one is reported on MAX_ALERTS
sweeps, then left alone (it stays in captures.db).

Usage: python scripts/sweep.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, notion_oauth  # noqa: E402
from app.config import settings  # noqa: E402
from app.tools import notify  # noqa: E402

TEN_MINUTES_MS = 10 * 60 * 1000
MAX_ALERTS = 2
REAUTH_WARNING_DAYS = 14


async def check_notion_mcp_token() -> None:
    """Warn ahead of the Notion MCP refresh token's hard expiry, and when
    refreshes are failing. The agent falls back to the legacy Notion tools
    either way, so this is about restoring the good path, not uptime."""
    st = notion_oauth.status()
    if not st.get("configured") or not st.get("enabled"):
        return
    problems = []
    if st.get("last_error"):
        problems.append(f"last refresh failed: {st['last_error']['error'][:200]}")
    try:
        left = notion_oauth.refresh_token_expires_in()
    except Exception as exc:  # introspection endpoint down, network, etc.
        print(f"sweep: notion introspection failed: {exc}")
        left = None
    if left is not None and left < REAUTH_WARNING_DAYS * 86400:
        days = max(0, left // 86400)
        problems.append(
            "refresh token expires in "
            + (f"{days} day(s)" if left > 0 else "the past (already dead)")
        )
    if not problems:
        print("sweep: notion mcp token ok")
        return
    message = (
        "Notion MCP needs re-authorization:\n- " + "\n- ".join(problems)
        + "\nRun on the server: python scripts/notion_oauth.py begin, then open the URL."
        " Until then Notion captures use the legacy tools (no in-page placement)."
    )
    print(message)
    await notify.send(message, title="Notion MCP re-auth needed", tags="key")


async def main() -> None:
    db.init(settings.db_path)
    try:
        await check_notion_mcp_token()
    except Exception as exc:  # never let the token check block the stuck sweep
        print(f"sweep: notion token check errored: {exc}")
    stuck = db.stuck_captures(TEN_MINUTES_MS, MAX_ALERTS)
    if not stuck:
        print("sweep: nothing stuck")
        return
    lines = []
    for row in stuck:
        text = (row["transcription"] or "(audio only)")[:80]
        lines.append(f'- [{row["status"]}] "{text}" ({row["id"][:8]})')
    message = (
        f"{len(stuck)} capture(s) never completed and will not auto-retry:\n"
        + "\n".join(lines)
        + "\nInspect captures.db on the server to recover them."
    )
    print(message)
    await notify.send(message, title="Ring captures stuck", tags="warning")
    db.count_sweep_alert([row["id"] for row in stuck])


if __name__ == "__main__":
    asyncio.run(main())
