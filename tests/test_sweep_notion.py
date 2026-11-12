import importlib.util
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from app import notion_oauth

spec = importlib.util.spec_from_file_location(
    "sweep", Path(__file__).resolve().parent.parent / "scripts" / "sweep.py"
)
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)

INTROSPECT = "https://mcp.notion.com/introspect"


def write_token(**overrides):
    now = int(time.time())
    data = {
        "client_id": "cid",
        "token_endpoint": "https://mcp.notion.com/token",
        "introspection_endpoint": INTROSPECT,
        "access_token": "AT",
        "refresh_token": "RT",
        "obtained_at": now,
        "expires_in": 28800,
        "expires_at": now + 28800,
        "first_authorized_at": now,
        "refresh_count": 0,
        "last_error": None,
        "log": [],
    }
    data.update(overrides)
    notion_oauth.token_path().write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def notifications(monkeypatch):
    sent = []

    async def fake_notify(message, title=None, tags=None):
        sent.append({"message": message, "title": title})
        return "ok"

    monkeypatch.setattr("app.tools.notify.send", fake_notify)
    return sent


async def test_unconfigured_is_silent(notifications):
    await sweep.check_notion_mcp_token()
    assert notifications == []


@respx.mock
async def test_healthy_token_is_silent(notifications):
    write_token()
    respx.post(INTROSPECT).mock(
        return_value=httpx.Response(200, json={"active": True, "exp": int(time.time()) + 100 * 86400})
    )
    await sweep.check_notion_mcp_token()
    assert notifications == []


@respx.mock
async def test_warns_two_weeks_before_refresh_expiry(notifications):
    write_token()
    respx.post(INTROSPECT).mock(
        return_value=httpx.Response(200, json={"active": True, "exp": int(time.time()) + 5 * 86400 + 60})
    )
    await sweep.check_notion_mcp_token()
    assert len(notifications) == 1
    assert "expires in 5 day(s)" in notifications[0]["message"]
    assert "notion_oauth.py begin" in notifications[0]["message"]


@respx.mock
async def test_warns_on_dead_token_or_failed_refresh(notifications):
    write_token(last_error={"at": int(time.time()), "error": "token endpoint 400: invalid_grant"})
    respx.post(INTROSPECT).mock(return_value=httpx.Response(200, json={"active": False}))
    await sweep.check_notion_mcp_token()
    msg = notifications[0]["message"]
    assert "invalid_grant" in msg and "already dead" in msg


@respx.mock
async def test_introspection_outage_does_not_notify(notifications):
    write_token()
    respx.post(INTROSPECT).mock(return_value=httpx.Response(503))
    await sweep.check_notion_mcp_token()
    assert notifications == []
