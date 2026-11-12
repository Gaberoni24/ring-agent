"""Self-editing conventions: the agent can persist corrections the user
speaks ("remember: ...", "from now on ...").

Learned conventions live in context/LEARNED.md — separate from the
human-authored CONTEXT.md so code deploys never overwrite them. Both are
injected into every system prompt.

Because a saved rule steers every future capture, it is only saved
directly when the user's own words sound like a rule. Otherwise (say,
text in a calendar invite telling the agent to "remember" something) it
needs an Approve tap."""

import re
from datetime import datetime

from app.config import settings
from app.tools import ToolError, register, run_mode, spoken

RULE_CUES = re.compile(
    r"\b(remember|from now on|always|never|going forward|in the future|"
    r"next time|by default|should (?:go|have gone|be)|should've)\b",
    re.IGNORECASE,
)

NOT_ASKED = (
    "Not saved: the user's words for this capture don't ask for a standing "
    "rule. If they clearly want one, call request_approval with the exact "
    "rule; otherwise just do the capture."
)


def learned_path():
    return settings.context_dir / "LEARNED.md"


def read_learned() -> str:
    try:
        return learned_path().read_text(encoding="utf-8")
    except OSError:
        return ""


async def remember(tool_input: dict) -> str:
    mode = run_mode.get()
    if mode == "undo" or (mode == "normal" and not RULE_CUES.search(spoken.get())):
        raise ToolError(NOT_ASKED)
    text = " ".join(tool_input["convention"].split())
    if not text:
        return "nothing to remember"
    stamp = datetime.now(settings.tz).strftime("%Y-%m-%d")
    path = learned_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- ({stamp}) {text}\n")
    return f"saved convention: {text}"


register(
    {
        "name": "remember_convention",
        "description": (
            "Permanently save a routing/formatting correction to your own "
            "instructions. Use when the user says things like 'remember ...', "
            "'from now on ...', 'always/never ...', or corrects where "
            "something should have gone. State the rule generally, e.g. "
            "'Songbook additions are database rows in the Songbook database, "
            "not subpages.'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "convention": {
                    "type": "string",
                    "description": "The rule to remember, stated generally",
                }
            },
            "required": ["convention"],
        },
    },
    remember,
)
