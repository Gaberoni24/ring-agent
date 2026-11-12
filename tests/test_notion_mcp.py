import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import anthropic
import httpx
import pytest
import respx

from app import agent, notion_oauth
from app.config import settings

META = {
    "issuer": "https://mcp.notion.com",
    "authorization_endpoint": "https://mcp.notion.com/authorize",
    "token_endpoint": "https://mcp.notion.com/token",
    "registration_endpoint": "https://mcp.notion.com/register",
    "introspection_endpoint": "https://mcp.notion.com/introspect",
}


def system_text(blocks):
    return "".join(b["text"] for b in blocks)


def write_token(**overrides):
    now = int(time.time())
    data = {
        "client_id": "cid",
        "client_secret": None,
        "redirect_uri": notion_oauth.redirect_uri(),
        "token_endpoint": META["token_endpoint"],
        "introspection_endpoint": META["introspection_endpoint"],
        "access_token": "AT1",
        "refresh_token": "RT1",
        "obtained_at": now,
        "expires_in": 3600,
        "expires_at": now + 3600,
        "first_authorized_at": now,
        "refresh_count": 0,
        "log": [],
    }
    data.update(overrides)
    notion_oauth.token_path().write_text(json.dumps(data), encoding="utf-8")
    return data


# --- OAuth flow ---------------------------------------------------------------


@respx.mock
def test_begin_registers_client_and_builds_pkce_url():
    respx.get("https://mcp.notion.com/.well-known/oauth-authorization-server").mock(
        return_value=httpx.Response(200, json=META)
    )
    reg = respx.post(META["registration_endpoint"]).mock(
        return_value=httpx.Response(201, json={"client_id": "new-client"})
    )

    url = notion_oauth.begin()

    body = json.loads(reg.calls[0].request.content)
    assert body["redirect_uris"] == [settings.public_base_url + "/notion/oauth/callback"]
    assert "refresh_token" in body["grant_types"]
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["new-client"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["resource"] == ["https://mcp.notion.com"]
    pending = json.loads(notion_oauth.pending_path().read_text())
    assert pending["state"] == q["state"][0]
    assert pending["code_verifier"]


@respx.mock
def test_complete_exchanges_code_and_stores_tokens():
    respx.get("https://mcp.notion.com/.well-known/oauth-authorization-server").mock(
        return_value=httpx.Response(200, json=META)
    )
    respx.post(META["registration_endpoint"]).mock(
        return_value=httpx.Response(201, json={"client_id": "cid"})
    )
    tok = respx.post(META["token_endpoint"]).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "token_type": "Bearer"},
        )
    )
    url = notion_oauth.begin()
    state = parse_qs(urlparse(url).query)["state"][0]

    with pytest.raises(notion_oauth.OAuthError):
        notion_oauth.complete("code123", "wrong-state")

    data = notion_oauth.complete("code123", state)

    form = parse_qs(tok.calls[0].request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["code123"]
    assert form["code_verifier"]
    assert data["access_token"] == "AT" and data["refresh_token"] == "RT"
    assert data["expires_at"] >= int(time.time()) + 3590
    assert data["log"][-1]["event"] == "authorize"
    assert not notion_oauth.pending_path().exists()
    assert notion_oauth.get_access_token() == "AT"


@respx.mock
def test_refresh_rotates_and_logs():
    write_token()
    respx.post(META["token_endpoint"]).mock(
        return_value=httpx.Response(
            200, json={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 7200}
        )
    )
    data = notion_oauth.refresh()
    assert data["access_token"] == "AT2"
    assert data["refresh_token"] == "RT2"
    assert data["refresh_count"] == 1
    assert data["log"][-1] == {
        "event": "refresh",
        "at": data["obtained_at"],
        "expires_in": 7200,
        "refresh_token_rotated": True,
    }


@respx.mock
def test_refresh_keeps_old_refresh_token_when_not_rotated():
    write_token()
    respx.post(META["token_endpoint"]).mock(
        return_value=httpx.Response(200, json={"access_token": "AT2", "expires_in": 60})
    )
    data = notion_oauth.refresh()
    assert data["refresh_token"] == "RT1"
    assert data["log"][-1]["refresh_token_rotated"] is False


@respx.mock
def test_get_access_token_refreshes_near_expiry_and_degrades_on_failure():
    write_token(expires_at=int(time.time()) + 10)
    route = respx.post(META["token_endpoint"]).mock(
        return_value=httpx.Response(200, json={"access_token": "FRESH", "expires_in": 3600})
    )
    assert notion_oauth.get_access_token() == "FRESH"
    assert route.called

    # Now expired again and the refresh fails -> None (agent falls back)
    write_token(expires_at=int(time.time()) - 1)
    respx.post(META["token_endpoint"]).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    assert notion_oauth.get_access_token() is None
    st = notion_oauth.status()
    assert "invalid_grant" in st["last_error"]["error"]


def test_get_access_token_none_when_unconfigured_or_disabled(monkeypatch):
    assert notion_oauth.get_access_token() is None
    write_token()
    assert notion_oauth.get_access_token() == "AT1"
    monkeypatch.setattr(settings, "notion_mcp_enabled", False)
    assert notion_oauth.get_access_token() is None


# --- callback route -----------------------------------------------------------


def test_callback_route(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    async def fake_notify(message, title=None, tags=None):
        return "ok"

    monkeypatch.setattr("app.tools.notify.send", fake_notify)

    def fake_complete(code, state):
        if state != "good":
            raise notion_oauth.OAuthError("state mismatch")
        return {"expires_in": 3600, "refresh_token": "RT"}

    monkeypatch.setattr(notion_oauth, "complete", fake_complete)
    with TestClient(app) as c:
        ok = c.get("/notion/oauth/callback", params={"code": "x", "state": "good"})
        assert ok.status_code == 200 and "Notion connected" in ok.text
        bad = c.get("/notion/oauth/callback", params={"code": "x", "state": "evil"})
        assert bad.status_code == 400 and "rejected" in bad.text
        denied = c.get("/notion/oauth/callback", params={"error": "access_denied"})
        assert denied.status_code == 400
        # Query text is shown on the page escaped, never as live HTML.
        xss = c.get("/notion/oauth/callback",
                    params={"error": "x", "error_description": "<script>alert(1)</script>"})
        assert "<script>" not in xss.text and "&lt;script&gt;" in xss.text


# --- agent wiring -------------------------------------------------------------


def response(stop_reason, content):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


@pytest.fixture
def fake_client(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.responses = []
            self.requests = []
            self.messages = SimpleNamespace(create=self._create)
            self.beta = SimpleNamespace(messages=self.messages)

        async def _create(self, **kwargs):
            self.requests.append(kwargs)
            nxt = self.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

    fake = FakeClient()
    monkeypatch.setattr(agent, "_client", fake)
    return fake


async def test_no_token_means_no_mcp(fake_client):
    fake_client.responses = [response("end_turn", [SimpleNamespace(type="text", text="ok")])]
    await agent.run_capture("hello")
    req = fake_client.requests[0]
    assert "mcp_servers" not in req and "betas" not in req
    assert all(t.get("type") != "mcp_toolset" for t in req["tools"])


async def test_mcp_attached_with_allowlist_and_trace(fake_client):
    write_token()
    fake_client.responses = [
        response(
            "end_turn",
            [
                SimpleNamespace(
                    type="mcp_tool_use", id="m1", name="notion-search",
                    server_name="notion", input={"query": "cool places SF"},
                ),
                SimpleNamespace(
                    type="mcp_tool_result", tool_use_id="m1", is_error=False,
                    content=[SimpleNamespace(type="text", text="My favorite neighborhoods in SF")],
                ),
                SimpleNamespace(type="text", text="Notion: added under Gorgeous houses"),
            ],
        )
    ]

    result = await agent.run_capture("add bryant st to cool places in SF")

    req = fake_client.requests[0]
    assert req["betas"] == [agent.MCP_BETA]
    assert req["mcp_servers"][0]["authorization_token"] == "AT1"
    assert req["mcp_servers"][0]["url"] == settings.notion_mcp_url
    toolset = next(t for t in req["tools"] if t.get("type") == "mcp_toolset")
    assert toolset["default_config"] == {"enabled": False}
    assert set(toolset["configs"]) == set(agent.NOTION_MCP_TOOLS)
    assert "notion-fetch" in toolset["configs"]
    assert result.summary == "Notion: added under Gorgeous houses"
    assert result.trace == [
        {
            "tool": "notion-search",
            "input": {"query": "cool places SF"},
            "result": "My favorite neighborhoods in SF",
            "is_error": False,
            "server": "notion",
        }
    ]


async def test_pause_turn_continues(fake_client):
    write_token()
    fake_client.responses = [
        response("pause_turn", [SimpleNamespace(type="text", text="working...")]),
        response("end_turn", [SimpleNamespace(type="text", text="done")]),
    ]
    result = await agent.run_capture("x")
    assert result.summary == "done"
    assert len(fake_client.requests) == 2
    assert fake_client.requests[1]["messages"][-1]["role"] == "assistant"


async def test_bad_request_with_mcp_falls_back_to_legacy_tools(fake_client):
    write_token()
    err = anthropic.BadRequestError(
        message="mcp server unreachable",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
        body=None,
    )
    fake_client.responses = [err, response("end_turn", [SimpleNamespace(type="text", text="ok")])]
    result = await agent.run_capture("x")
    assert result.summary == "ok"
    assert "mcp_servers" in fake_client.requests[0]
    assert "mcp_servers" not in fake_client.requests[1]
    assert all(t.get("type") != "mcp_toolset" for t in fake_client.requests[1]["tools"])


async def test_system_prompt_has_notion_placement_rules(fake_client):
    fake_client.responses = [response("end_turn", [SimpleNamespace(type="text", text="ok")])]
    await agent.run_capture("x")
    system = system_text(fake_client.requests[0]["system"])
    assert "notion-fetch it and read its" in system
    assert "Never ask" in system


def test_notion_map_renders_hierarchy_and_caps():
    ctx = settings.context_dir
    ctx.mkdir(parents=True, exist_ok=True)
    items = [{"title": "Ideas", "type": "database", "id": "i", "parent": None}]
    items += [{"title": f"idea {n}", "type": "page", "id": f"p{n}", "parent": "Ideas"} for n in range(60)]
    items += [{"title": "Songbook", "type": "page", "id": "s", "parent": None}]
    (ctx / "notion_map.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    text = agent._render_notion_map()
    lines = text.splitlines()
    assert lines[0] == "Top level:"
    assert "- Ideas [database] id=i (60 items)" in lines
    assert "- Songbook [page] id=s" in lines[:3]
    assert "In Ideas:" in lines
    assert f"  - ... {60 - agent.MAP_MAX_PER_PARENT} more" in lines
