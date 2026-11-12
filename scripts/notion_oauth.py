"""Manage the Notion MCP OAuth token used by the agent's MCP connector.

Run ON THE SERVER (the token file lives next to .env there):

  python scripts/notion_oauth.py begin       # prints the URL to open (any device)
  python scripts/notion_oauth.py status      # lifetime, refresh log, last error
  python scripts/notion_oauth.py introspect  # ask Notion what the tokens' real expiry is
  python scripts/notion_oauth.py refresh     # force a refresh; reports rotation + new lifetime
  python scripts/notion_oauth.py probe       # call the MCP server with the token (tools/list)

The authorization completes when the browser is redirected to
<PUBLIC_BASE_URL>/notion/oauth/callback, which the running app serves.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import notion_oauth  # noqa: E402
from app.config import settings  # noqa: E402


def _ts(epoch) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _fmt_seconds(s) -> str:
    if s is None:
        return "unknown"
    s = int(s)
    sign = "-" if s < 0 else ""
    s = abs(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{d}d"] if d else []
    parts += [f"{h}h", f"{m}m", f"{s}s"]
    return sign + " ".join(parts)


def cmd_begin() -> None:
    url = notion_oauth.begin()
    print("Open this URL (phone is fine) and approve access for ring-agent:\n")
    print(url)
    print(f"\nIt will redirect to {notion_oauth.redirect_uri()} - the app must be running.")


def cmd_status() -> None:
    st = notion_oauth.status()
    if not st["configured"]:
        print("not configured - run: python scripts/notion_oauth.py begin")
        return
    print(f"enabled:            {st['enabled']}")
    print(f"client_id:          {st['client_id']}")
    print(f"first authorized:   {_ts(st['first_authorized_at'])}")
    print(f"token obtained:     {_ts(st['obtained_at'])}")
    print(f"reported expires_in {_fmt_seconds(st['expires_in_reported'])}")
    print(f"time until expiry:  {_fmt_seconds(st['seconds_until_expiry'])}")
    print(f"refresh token:      {'yes' if st['has_refresh_token'] else 'NO'}")
    print(f"refreshes so far:   {st['refresh_count']}")
    if st["last_error"]:
        print(f"LAST ERROR ({_ts(st['last_error']['at'])}): {st['last_error']['error']}")
    print("log:")
    for e in st["log"]:
        rot = " (refresh token rotated)" if e.get("refresh_token_rotated") else ""
        print(f"  {_ts(e['at'])}  {e['event']:<9} expires_in={_fmt_seconds(e.get('expires_in'))}{rot}")


def cmd_introspect() -> None:
    now = int(time.time())
    for which in ("access_token", "refresh_token"):
        try:
            info = notion_oauth.introspect(which)
        except notion_oauth.OAuthError as exc:
            print(f"{which}: introspection failed: {exc}")
            continue
        exp = info.get("exp")
        line = f"{which}: active={info.get('active')}"
        if exp:
            line += f" exp={_ts(exp)} (in {_fmt_seconds(exp - now)})"
        if info.get("iat"):
            line += f" iat={_ts(info['iat'])}"
        extra = {k: v for k, v in info.items() if k not in ("active", "exp", "iat")}
        if extra:
            line += f" {json.dumps(extra)}"
        print(line)


def cmd_refresh() -> None:
    before = notion_oauth.status()
    print(f"before: {_fmt_seconds(before.get('seconds_until_expiry'))} left, "
          f"{before.get('refresh_count', 0)} refreshes")
    t0 = time.time()
    data = notion_oauth.refresh()
    print(f"refresh OK in {time.time() - t0:.2f}s: new access token expires_in="
          f"{_fmt_seconds(data.get('expires_in'))}, refresh token rotated="
          f"{data['log'][-1]['refresh_token_rotated']}")


def cmd_probe() -> None:
    token = notion_oauth.get_access_token()
    if not token:
        print("no usable token (disabled, missing, or refresh failed)")
        sys.exit(1)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ring-agent-probe", "version": "0"},
        },
    }
    with httpx.Client(timeout=30) as http:
        r = http.post(settings.notion_mcp_url, headers=headers, json=init)
        print(f"initialize -> HTTP {r.status_code}")
        if r.status_code >= 400:
            print(r.text[:500])
            sys.exit(1)
        session = r.headers.get("mcp-session-id")
        if session:
            headers["Mcp-Session-Id"] = session
        http.post(settings.notion_mcp_url, headers=headers,
                  json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = http.post(settings.notion_mcp_url, headers=headers,
                      json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        print(f"tools/list -> HTTP {r.status_code}")
        body = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            body = "".join(line[5:] for line in body.splitlines() if line.startswith("data:"))
        try:
            tools = json.loads(body)["result"]["tools"]
        except (ValueError, KeyError):
            print(body[:800])
            sys.exit(1)
        print(f"{len(tools)} tools: " + ", ".join(t["name"] for t in tools))


COMMANDS = {
    "begin": cmd_begin,
    "status": cmd_status,
    "introspect": cmd_introspect,
    "refresh": cmd_refresh,
    "probe": cmd_probe,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd not in COMMANDS:
        sys.exit(f"usage: notion_oauth.py {{{'|'.join(COMMANDS)}}}")
    try:
        COMMANDS[cmd]()
    except notion_oauth.OAuthError as exc:
        sys.exit(f"error: {exc}")
