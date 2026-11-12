"""Anthropic tool-use loop for one capture."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import anthropic

from app import notion_oauth, tools
from app.config import settings
from app.tools import approval, history
from app.tools.memory import read_learned

log = logging.getLogger("ring.agent")

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


@dataclass
class AgentResult:
    summary: str
    trace: list = field(default_factory=list)
    tokens_used: int = 0
    cached_tokens: int = 0  # input tokens served from the prompt cache
    pending_approval: dict | None = None  # set when request_approval was called


CONTRACT = """\
You are the action agent for a voice-capture ring. The user records a short
voice memo; you receive the transcript and must take the right action, then
report exactly what you did.

Classify the capture as one of: task, event, note, question, other.
- task -> TickTick (ticktick_add_task). Convert spoken dates to an
  explicit local due (YYYY-MM-DD, or YYYY-MM-DDTHH:MM when a time is
  given) from the current local time below. To change a task that already
  exists (new due date, rename, different list, priority...), edit it with
  ticktick_update_task — never create a duplicate. "I did that" / "never
  mind, done" -> ticktick_complete_task.
- event (something happening at a specific time: appointment, meeting,
  dinner, flight) -> Google Calendar (calendar_create_event). A to-do
  with a deadline is a task, not an event.
- note -> Notion. Find the right page or database AND the right place on
  it (see "Notion" below). Only if still ambiguous after searching, put it
  on the Ideas page.
- question -> answer it yourself and send the answer with the notify tool.
- other/unintelligible -> append the raw transcript to the Notion Inbox page
  so nothing is lost, and say so.

The transcript comes from a small on-device speech model and may contain
recognition errors. Normalize obvious ones (e.g. "tick tock" -> TickTick,
"no shun" -> Notion) but NEVER invent content that isn't clearly implied.
When wording is garbled, capture it as faithfully as possible rather than
paraphrasing creatively.

Notion:
- The notion-* MCP tools (notion-search, notion-fetch, notion-create-pages,
  notion-update-page) are the primary Notion interface
  when present. The legacy notion_* tools are the fallback if the MCP tools
  are absent or return authorization errors — say so in your summary.
- Be decisive about the target. notion-search is semantic; a close match
  in meaning or a near-match in title (typos, synonyms — "cool places in
  SF" IS "My favorite neighborhoods in SF") is the target. Never ask
  whether it is; act. The workspace map is a hint of what exists, not the
  limit of it.
- Before adding to an existing page, notion-fetch it and read its
  structure. If it has headings/sections, put the new content under the
  section that fits, in the format already used there (use
  notion-update-page to replace the heading line with the heading line
  followed by the new content). Append at the end only when no section
  fits. Never restructure or rewrite existing content.
- Databases (tagged [database] in the map, or data sources in search
  results) take rows via notion-create-pages with properties (title plus
  whatever the learned conventions specify) — never subpages under a
  database. Legacy fallback: notion_add_database_row.

Rules:
- When the user corrects you or states a standing rule ("remember...",
  "from now on...", "always..."), save it with remember_convention AND apply
  it immediately.
- When the user asks for deep thinking ("think hard", "use the big model")
  or the request needs real reasoning (multi-step planning, tricky
  questions), hand the ENTIRE task to the escalate tool and relay its
  result. Don't escalate routine captures.
- Each capture is, by default, INDEPENDENT of the ones before it. The
  recent-captures list below is reference material, not an ongoing
  conversation: do not assume a capture relates to the previous one, do not
  merge it into an earlier note or task, and do not let earlier topics
  colour how you route it. Treat a capture as a follow-up ONLY when it
  explicitly refers back — "actually, put that in Hardware", "same project
  as the last one", "no, Friday not Thursday", "add to that note: ...",
  "yes, the second one" — and the referenced capture is recent. Then
  resolve "that"/"it"/"the last one" against it, reuse the ids/urls from
  its tool results, and act.
- The user usually cannot reply. If a capture is confusing, first check
  history (recent list, then recall_history) for the intent. If it is still
  genuinely ambiguous, ALWAYS take a safe default action so nothing is lost
  (the most likely reading, or the durable fallback: the default TickTick
  list from the user's conventions / Notion Inbox), and only then, if it would truly change the
  outcome, end your summary with one short question. If the user does
  answer via the next capture, that answer will refer back to this one —
  handle it as a follow-up.
- When a follow-up corrects an earlier action, fix the earlier result in
  place where a tool allows it (ticktick_update_task with the ids from the
  history trace). Otherwise do the corrected action and state plainly in
  your summary anything from the earlier action you could not undo.
- If a tool reports it is not configured or fails, do not silently drop the
  capture — fall back to a durable location (the default TickTick list from the
  user's conventions, or Notion Inbox)
  and mention the fallback in your summary.
- Do not call notify for your final summary; it is delivered automatically.

Approval:
- Some actions need the user's approval first: the user's "Needs my
  approval" conventions (CONTEXT.md and learned conventions) say which.
  Delete tools always do and refuse to run without it. For such an action,
  do the safe parts of the capture first, then call request_approval once
  with exactly what you will do (ids included — the plan is executed later
  from that text alone) and STOP without doing the risky part. Your summary
  says what is waiting for approval.
- Ordinary captures never need approval: creating a task, event or note,
  appending to a page, editing something you just created, completing one
  task. Do not ask for those.
- A message starting with [APPROVED] or [UNDO] comes from the user tapping
  a button on their phone, not from the ring; that tap is the approval.

Your FINAL message is shown to the user as a phone notification. Keep it
to one or two short lines in plain words saying exactly what was done and
where, e.g.:
  TickTick: "Review PCB layout" -> Hardware, due Friday
  Calendar: "Dentist" Tue Oct 21 10:00-11:00
  Notion: appended note to Index Alpha / Field Notes
No ids, no URLs, no markdown: tapping the notification already opens the
item, and the ids are kept in the tool trace for follow-ups. If something
failed, start the summary with "FAILED:" and say in plain words what was
attempted."""


MAP_MAX_LINES = 140
MAP_MAX_PER_PARENT = 6


def _render_notion_map() -> str:
    path = settings.context_dir / "notion_map.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(workspace map unavailable — use notion_search)"
    items = data.get("items", [])
    if not items:
        return "(workspace map empty — use notion_search)"
    # Hierarchy first: every top-level page/database (these are the stable
    # containers), then children grouped under their parent, most-populated
    # parents first. The old edit-time order made the cut-off a lottery.
    top = [i for i in items if not i.get("parent")]
    children: dict[str, list] = {}
    for i in items:
        if i.get("parent"):
            children.setdefault(i["parent"], []).append(i)
    lines = ["Top level:"]
    for i in top:
        n = len(children.get(i["title"], []))
        sub = f" ({n} items)" if n else ""
        lines.append(f"- {i['title']} [{i['type']}] id={i['id']}{sub}")
    # Every database, wherever it lives: routing needs to know what takes
    # rows. Children of pages are only sampled — notion-search finds the rest.
    nested_dbs = [i for i in items if i["type"] == "database" and i.get("parent")]
    if nested_dbs:
        lines.append("Databases inside pages:")
        for i in nested_dbs:
            lines.append(f"- {i['title']} [database] id={i['id']}, in: {i['parent']}")
    budget = MAP_MAX_LINES - len(lines)
    for parent, kids in sorted(children.items(), key=lambda kv: -len(kv[1])):
        if budget <= 1:
            lines.append("- ... (more not shown; use notion-search)")
            break
        lines.append(f"In {parent}:")
        shown = kids[: max(1, min(len(kids), budget - 1, MAP_MAX_PER_PARENT))]
        for i in shown:
            lines.append(f"  - {i['title']} [{i['type']}] id={i['id']}")
        if len(kids) > len(shown):
            lines.append(f"  - ... {len(kids) - len(shown)} more")
        budget -= len(shown) + 2
    return "\n".join(lines)


def _read_context_md() -> str:
    """Your own context/CONTEXT.md if it exists, otherwise the shipped
    context/CONTEXT.example.md template."""
    for name in ("CONTEXT.md", "CONTEXT.example.md"):
        try:
            return (settings.context_dir / name).read_text(encoding="utf-8")
        except OSError:
            continue
    return "(no CONTEXT.md found)"


def build_system_prompt(capture_id: str | None = None) -> list[dict]:
    """Two blocks: a large stable one (contract, conventions, map) carrying
    the prompt-cache breakpoint, then the per-capture volatile tail (clock,
    recent history). Tools render before system, so the breakpoint caches
    the tool schemas too. Anything that changes per request must stay in
    the second block or it silently defeats the cache."""
    now = datetime.now(settings.tz)
    learned = read_learned() or "(none yet)"
    stable = (
        f"{CONTRACT}\n\n"
        f"# User conventions (CONTEXT.md)\n{_read_context_md()}\n\n"
        f"# Learned conventions (saved by you via remember_convention)\n"
        f"{learned}\n\n"
        f"# Notion workspace map (cached)\n{_render_notion_map()}"
    )
    volatile = (
        f"Current local time: {now.strftime('%A, %Y-%m-%d %H:%M')} "
        f"({settings.ring_tz})\n\n"
        f"# Recent captures, newest first (reference only — captures are "
        f"independent unless the new one explicitly refers back)\n"
        f"{history.render_recent(exclude_id=capture_id)}"
    )
    return [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatile},
    ]


# Notion hosted MCP tools the agent may use (allowlist; the server exposes
# ~30 including agents/attachments/views we never want it touching). Each
# schema costs 1-5k tokens per iteration, so only what captures need.
NOTION_MCP_TOOLS = [
    "notion-search",
    "notion-fetch",
    "notion-create-pages",
    "notion-update-page",
]
MCP_BETA = "mcp-client-2025-11-20"


def _mcp_request_kwargs() -> dict:
    """Extra Messages-API kwargs to attach Notion's MCP server, or {} when
    there is no usable token (-> legacy notion_* tools only)."""
    token = notion_oauth.get_access_token()
    if not token:
        return {}
    return {
        "mcp_servers": [
            {
                "type": "url",
                "url": settings.notion_mcp_url,
                "name": "notion",
                "authorization_token": token,
            }
        ],
        "tools": [
            {
                "type": "mcp_toolset",
                "mcp_server_name": "notion",
                "default_config": {"enabled": False},
                "configs": {name: {"enabled": True} for name in NOTION_MCP_TOOLS},
            }
        ],
        "betas": [MCP_BETA],
    }


def _mcp_result_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text:
            parts.append(text)
    return "\n".join(parts)


def _final_text(content) -> str:
    """The summary: only the text after the last tool block. Server-side
    MCP calls land in the same response as the summary, and any text before
    them is the model narrating its work ("I'll add it at the end."), which
    must not reach the notification."""
    parts: list[str] = []
    for block in content:
        if block.type == "text":
            parts.append(block.text)
        else:
            parts = []
    return "\n".join(p.strip() for p in parts if p.strip())


_escalating = False  # single-consumer worker, so a plain flag is enough
_current_capture_id: str | None = None  # so an escalated run sees the same history


async def _escalate(tool_input: dict) -> str:
    """Re-run a task on the highest-tier model and return its summary."""
    global _escalating
    if _escalating:
        return "already running on the advanced model — handle the task yourself"
    _escalating = True
    try:
        result = await run_capture(
            tool_input["task"],
            model=settings.escalation_model,
            capture_id=_current_capture_id,
        )
        return f"[{settings.escalation_model}] {result.summary}"
    finally:
        _escalating = False


tools.register(
    {
        "name": "escalate",
        "description": (
            "Hand the entire task to the most capable model for deep "
            "reasoning. Pass the full transcript plus any context it needs. "
            "Use only when the user requests it or the task genuinely needs "
            "heavy thinking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Complete task description for the advanced model",
                }
            },
            "required": ["task"],
        },
    },
    _escalate,
)


UNDO_INSTRUCTIONS = """[UNDO] The user tapped Undo on the notification for the capture below.
Reverse every change it made, newest first, using the ids in its tool trace:
- created TickTick task -> ticktick_delete_task
- updated TickTick task -> ticktick_update_task back to the "previously"
  values in its result (title, due, priority, tags; project = the list
  named by the previous list_id)
- completed TickTick task -> it cannot be reopened through the API; say so
- created calendar event -> calendar_delete_event
- created Notion page or database row -> notion_trash_page; if that fails
  (not shared with the integration), rename it with notion-update-page so
  its title starts with "[UNDONE] " and say the user should delete it
- content added to an existing Notion page -> notion-fetch the page, then
  remove exactly the added content with notion-update-page (nothing else)
- saved conventions (remember_convention) and notifications: leave them
Do not redo or reinterpret the original request, do not touch anything
else, and do not call request_approval. Your final summary lists each
reversal and anything you could not reverse.

Capture to undo:
{capture}"""

APPROVED_INSTRUCTIONS = """[APPROVED] The user tapped Approve for this plan. Carry it out now, exactly
as written and nothing more. Do not call request_approval.

Plan: {action}
{details}

Original capture: {transcription!r}"""


async def run_capture(
    transcription: str,
    model: str | None = None,
    capture_id: str | None = None,
    mode: str | None = None,
) -> AgentResult:
    """mode: 'normal' (a ring capture), 'approved' or 'undo' (started by a
    notification button). None = inherit, for nested runs (escalate)."""
    if mode is None:
        return await _run_loop(transcription, model, capture_id)
    mode_token = tools.run_mode.set(mode)
    spoken_token = tools.spoken.set(transcription if mode == "normal" else "")
    pending_token = approval.pending.set(None)
    try:
        result = await _run_loop(transcription, model, capture_id)
        result.pending_approval = approval.pending.get()
        return result
    finally:
        approval.pending.reset(pending_token)
        tools.spoken.reset(spoken_token)
        tools.run_mode.reset(mode_token)


async def _run_loop(
    transcription: str, model: str | None, capture_id: str | None
) -> AgentResult:
    global _current_capture_id
    _current_capture_id = capture_id
    client = _get_client()
    system = build_system_prompt(capture_id)
    messages: list[dict] = [{"role": "user", "content": transcription}]
    trace: list = []
    tokens = 0
    cached_tokens = 0
    mcp = _mcp_request_kwargs()

    for _turn in range(settings.max_agent_turns):
        request = {
            "model": model or settings.model,
            "max_tokens": 2048,
            "system": system,
            "tools": tools.schemas() + mcp.get("tools", []),
            "messages": messages,
        }
        try:
            response = await client.beta.messages.create(
                **request,
                **{k: v for k, v in mcp.items() if k != "tools"},
            )
        except anthropic.BadRequestError:
            if not mcp:
                raise
            # Most likely the MCP connector rejected the server/token. Fall
            # back to the legacy Notion tools rather than losing the capture.
            log.warning("Notion MCP request rejected; retrying without MCP", exc_info=True)
            mcp = {}
            request["tools"] = tools.schemas()
            response = await client.beta.messages.create(**request)
        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
        tokens += usage.input_tokens + usage.output_tokens + cache_read + cache_write
        cached_tokens += cache_read
        log.info(
            "usage: in=%d out=%d cache_read=%d cache_write=%d",
            usage.input_tokens, usage.output_tokens, cache_read, cache_write,
        )

        # MCP tools ran server-side inside this response; record them.
        pending_mcp: dict[str, dict] = {}
        for block in response.content:
            if block.type == "mcp_tool_use":
                entry = {
                    "tool": block.name,
                    "input": block.input,
                    "result": "",
                    "is_error": False,
                    "server": getattr(block, "server_name", None),
                }
                trace.append(entry)
                pending_mcp[block.id] = entry
            elif block.type == "mcp_tool_result":
                entry = pending_mcp.get(block.tool_use_id)
                if entry is not None:
                    entry["result"] = _mcp_result_text(block.content)[:4000]
                    entry["is_error"] = bool(getattr(block, "is_error", False))
                    log.info(
                        "mcp %s(%s) -> %s", entry["tool"], entry["input"], entry["result"][:200]
                    )

        if response.stop_reason == "pause_turn":
            # Server-side tool work paused; hand the partial turn back.
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            text = _final_text(response.content)
            return AgentResult(
                summary=text.strip() or "(agent produced no summary)",
                trace=trace,
                tokens_used=tokens,
                cached_tokens=cached_tokens,
            )

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result_text, is_error = await tools.dispatch(block.name, block.input)
            log.info("tool %s(%s) -> %s", block.name, block.input, result_text[:200])
            trace.append(
                {
                    "tool": block.name,
                    "input": block.input,
                    "result": result_text,
                    "is_error": is_error,
                }
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})

    return AgentResult(
        summary=(
            "Reached the tool-call limit without a final summary — actions may "
            "have been taken; check the tool trace on the server."
        ),
        trace=trace,
        tokens_used=tokens,
        cached_tokens=cached_tokens,
    )
