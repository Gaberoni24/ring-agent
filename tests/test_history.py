import json

from app import db
from app.tools import history


def system_text(blocks):
    return "".join(b["text"] for b in blocks)


def finished(capture_id, transcription, summary, trace=None, status="done", at=1):
    db.insert_capture(capture_id, at, transcription, None)
    assert db.claim_next_received()["id"] == capture_id
    db.finish_capture(
        capture_id,
        status,
        summary=summary,
        tool_trace=json.dumps(trace) if trace is not None else None,
        tokens_used=1,
    )


def test_recent_excludes_unfinished_and_current():
    finished("old", "add milk", "TickTick: milk -> Inbox")
    db.insert_capture("cur", 2, "actually put that in Groceries", None)
    db.claim_next_received()  # 'cur' is now processing
    db.insert_capture("queued", 3, "later", None)

    ids = [r["id"] for r in db.recent_captures(10, exclude_id="cur")]
    assert ids == ["old"]


def test_recent_is_newest_first_and_limited():
    for i in range(4):
        finished(f"c{i}", f"say {i}", f"done {i}")
    rows = db.recent_captures(2)
    assert [r["id"] for r in rows] == ["c3", "c2"]


def test_render_includes_transcript_outcome_and_tool_ids():
    finished(
        "c1",
        "add review PCB to ticktick",
        "TickTick: 'Review PCB' -> Inbox, due today",
        trace=[
            {"tool": "ticktick_list_projects", "input": {}, "result": "- Inbox (id 1)", "is_error": False},
            {
                "tool": "ticktick_add_task",
                "input": {"content": "Review PCB"},
                "result": "created task 987: 'Review PCB', url: https://ticktick.com/t/987",
                "is_error": False,
            },
        ],
    )
    text = history.render_recent()
    assert 'User said: "add review PCB to ticktick"' in text
    assert "Outcome: TickTick: 'Review PCB' -> Inbox, due today" in text
    assert "ticktick_add_task -> created task 987" in text
    assert "ticktick_list_projects" not in text  # lookups are noise in the compact view
    assert "ago" in text or "just now" in text


def test_render_marks_failures():
    finished("f1", "garbled", "error: boom", status="failed")
    assert "[FAILED]" in history.render_recent()


def test_render_empty_and_unavailable():
    assert history.render_recent() == "(none)"
    assert history.render([]) == "(none)"


async def test_recall_tool_searches_and_returns_full_trace():
    finished(
        "a",
        "add buy solder",
        "TickTick: solder -> Hardware",
        trace=[{"tool": "ticktick_add_task", "input": {"content": "buy solder"}, "result": "created task 42", "is_error": False}],
    )
    finished("b", "note about cats", "Notion: appended to Ideas")

    out = await history.recall({"query": "solder"})
    assert "buy solder" in out and "cats" not in out
    assert 'ticktick_add_task({"content": "buy solder"}) -> created task 42' in out

    out = await history.recall({"query": "task 42"})  # matches inside tool results too
    assert "buy solder" in out

    assert "no captures matching" in await history.recall({"query": "zzz"})
    assert "cats" in await history.recall({})  # no query -> most recent


async def test_recent_history_in_system_prompt_excludes_self():
    from types import SimpleNamespace

    from app import agent

    finished("prev", "add call mom to ticktick", "TickTick: 'Call mom' -> Inbox, due today")
    db.insert_capture("now", 5, "actually make that due Friday", None)
    db.claim_next_received()

    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    msgs = SimpleNamespace(create=create)
    agent._client = SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs))
    try:
        await agent.run_capture("actually make that due Friday", capture_id="now")
    finally:
        agent._client = None

    system = system_text(requests[0]["system"])
    assert "# Recent captures" in system
    assert 'User said: "add call mom to ticktick"' in system
    assert 'User said: "actually make that due Friday"' not in system
    assert "recall_history" in [t["name"] for t in requests[0]["tools"]]
    assert "INDEPENDENT of the ones before" in system
    assert "ALWAYS take a safe default action" in system
