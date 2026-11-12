from types import SimpleNamespace

import pytest

from app import agent
from app.config import settings


def system_text(blocks):
    return "".join(b["text"] for b in blocks)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, tool_input, block_id="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def response(stop_reason, content):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


@pytest.fixture
def fake_client(monkeypatch):
    """Scriptable stand-in for AsyncAnthropic: pops responses in order and
    records the request kwargs."""

    class FakeClient:
        def __init__(self):
            self.responses = []
            self.requests = []
            self.messages = SimpleNamespace(create=self._create)
            self.beta = SimpleNamespace(messages=self.messages)

        async def _create(self, **kwargs):
            self.requests.append(kwargs)
            return self.responses.pop(0)

    fake = FakeClient()
    monkeypatch.setattr(agent, "_client", fake)
    return fake


async def test_tool_loop_then_summary(fake_client, monkeypatch):
    dispatched = []

    async def fake_dispatch(name, tool_input):
        dispatched.append((name, tool_input))
        return "created task 1", False

    monkeypatch.setattr("app.tools.dispatch", fake_dispatch)
    fake_client.responses = [
        response("tool_use", [tool_block("ticktick_add_task", {"title": "buy milk"})]),
        response("end_turn", [text_block("TickTick: 'buy milk' -> Inbox")]),
    ]

    result = await agent.run_capture("add buy milk to ticktick")

    assert result.summary == "TickTick: 'buy milk' -> Inbox"
    assert dispatched == [("ticktick_add_task", {"title": "buy milk"})]
    assert result.trace[0]["result"] == "created task 1"
    assert result.tokens_used == 300  # two calls x 150

    # Second request must carry the tool_result back to the model
    followup = fake_client.requests[1]["messages"]
    assert followup[-1]["content"][0]["type"] == "tool_result"
    assert followup[-1]["content"][0]["tool_use_id"] == "tu_1"


async def test_max_turns_bail_out(fake_client, monkeypatch):
    async def fake_dispatch(name, tool_input):
        return "ok", False

    monkeypatch.setattr("app.tools.dispatch", fake_dispatch)
    fake_client.responses = [
        response("tool_use", [tool_block("ticktick_list_projects", {}, f"tu_{i}")])
        for i in range(settings.max_agent_turns)
    ]

    result = await agent.run_capture("loop forever")

    assert len(fake_client.requests) == settings.max_agent_turns
    assert "tool-call limit" in result.summary
    assert len(result.trace) == settings.max_agent_turns


async def test_system_prompt_contains_context_and_time(fake_client, tmp_path):
    ctx_dir = settings.context_dir
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / "CONTEXT.md").write_text("hardware notes -> Index Ring page", encoding="utf-8")
    (ctx_dir / "notion_map.json").write_text(
        '{"items": [{"title": "Index Ring", "type": "page", "id": "abc", "parent": "Projects"}]}',
        encoding="utf-8",
    )
    fake_client.responses = [response("end_turn", [text_block("noted")])]

    await agent.run_capture("note: mic gain low")

    system = system_text(fake_client.requests[0]["system"])
    assert "hardware notes -> Index Ring page" in system
    assert "In Projects:" in system and "  - Index Ring [page] id=abc" in system
    assert settings.ring_tz in system
    assert fake_client.requests[0]["tools"], "tool schemas must be passed"


async def test_escalate_tool_reruns_on_advanced_model(fake_client):
    # Outer (sonnet) call asks to escalate; nested (fable) call answers;
    # outer call then summarizes.
    fake_client.responses = [
        response("tool_use", [tool_block("escalate", {"task": "hard question"})]),
        response("end_turn", [text_block("deep answer")]),
        response("end_turn", [text_block("Answered via advanced model")]),
    ]

    result = await agent.run_capture("think hard about this hard question")

    models = [req["model"] for req in fake_client.requests]
    assert models == [
        settings.model,
        settings.escalation_model,
        settings.model,
    ]
    assert result.summary == "Answered via advanced model"
    assert result.trace[0]["tool"] == "escalate"
    assert f"[{settings.escalation_model}] deep answer" in result.trace[0]["result"]


async def test_learned_conventions_injected_into_prompt(fake_client):
    from app import tools
    from app.tools import memory

    tools.spoken.set("remember: ideas go under the Ideas page")
    await memory.remember({"convention": "Ideas go under the Ideas page."})
    fake_client.responses = [response("end_turn", [text_block("ok")])]

    await agent.run_capture("hello")

    system = system_text(fake_client.requests[0]["system"])
    assert "Ideas go under the Ideas page." in system


async def test_missing_context_files_degrade_gracefully(fake_client):
    fake_client.responses = [response("end_turn", [text_block("ok")])]
    await agent.run_capture("hello")
    system = system_text(fake_client.requests[0]["system"])
    assert "no CONTEXT.md found" in system
    assert "workspace map" in system


async def test_context_example_used_until_own_context_exists(fake_client):
    ctx_dir = settings.context_dir
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / "CONTEXT.example.md").write_text("template rules", encoding="utf-8")
    fake_client.responses = [response("end_turn", [text_block("ok")])] * 2

    await agent.run_capture("hello")
    assert "template rules" in system_text(fake_client.requests[0]["system"])

    (ctx_dir / "CONTEXT.md").write_text("my own rules", encoding="utf-8")
    await agent.run_capture("hello again")
    system = system_text(fake_client.requests[1]["system"])
    assert "my own rules" in system and "template rules" not in system


def test_blank_env_values_fall_back_to_defaults(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("RING_TZ", "")
    monkeypatch.setenv("NOTION_MCP_ENABLED", "")
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "")
    s = Settings(_env_file=None)
    assert s.ring_tz == "UTC"
    assert s.notion_mcp_enabled is True
    assert "ticktick_delete_task" in s.approval_required


async def test_prompt_cache_breakpoint_isolates_volatile_content(fake_client):
    fake_client.responses = [response("end_turn", [text_block("ok")])]
    await agent.run_capture("hello")
    blocks = fake_client.requests[0]["system"]
    assert len(blocks) == 2
    stable, volatile = blocks
    assert stable["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in volatile
    # Clock and history change per request and must not poison the cache.
    assert "Current local time" not in stable["text"]
    assert "Recent captures" not in stable["text"]
    assert "Current local time" in volatile["text"]
    assert "Recent captures" in volatile["text"]
    assert "voice-capture ring" in stable["text"]  # contract is cached


async def test_cache_reads_are_accounted(fake_client):
    r = response("end_turn", [text_block("ok")])
    r.usage = SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_read_input_tokens=25000,
        cache_creation_input_tokens=0,
    )
    fake_client.responses = [r]
    result = await agent.run_capture("hello")
    assert result.cached_tokens == 25000
    assert result.tokens_used == 25150


async def test_summary_excludes_narration_before_server_side_tools(fake_client):
    """MCP calls run inside the final response; text before them is the model
    thinking aloud and must not end up in the notification."""
    fake_client.responses = [
        response("end_turn", [
            text_block("The page is a simple bullet list. I'll add the new idea at the end."),
            SimpleNamespace(type="mcp_tool_use", id="m1", name="notion-update-page",
                            input={"page_id": "p1"}, server_name="notion"),
            SimpleNamespace(type="mcp_tool_result", tool_use_id="m1",
                            content=[{"type": "text", "text": "ok"}], is_error=False),
            text_block('Notion: added "Blog post idea" to Ideas'),
        ]),
    ]
    result = await agent.run_capture("blog post idea")
    assert result.summary == 'Notion: added "Blog post idea" to Ideas'
