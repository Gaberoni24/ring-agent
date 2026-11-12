"""request_approval: park a risky action until the user taps Approve.

The agent does any safe parts of the capture, calls this with a plan, and
stops. The worker turns the request into a high-priority notification with
Approve / Deny buttons; Approve queues a run that executes the plan."""

from contextvars import ContextVar

from app.tools import ToolError, register, run_mode

pending: ContextVar[dict | None] = ContextVar("pending_approval", default=None)


async def request_approval(tool_input: dict) -> str:
    if run_mode.get() != "normal":
        raise ToolError("Already approved by the user — go ahead and do it now.")
    pending.set({
        "action": tool_input["action"],
        "message": tool_input.get("message") or tool_input["action"],
        "details": tool_input["details"],
    })
    return (
        "Approval requested. Do NOT perform the risky part now. Finish with a "
        "summary that says what is waiting for approval."
    )


register(
    {
        "name": "request_approval",
        "description": (
            "Ask the user to approve a risky action (see 'Needs my approval' "
            "in their conventions) before doing it. Their phone shows Approve "
            "/ Deny with `action` as the title and `message` as the text. On "
            "Approve the plan is executed later from `details` alone, so put "
            "everything needed there: exact items, ids and list ids, new "
            "values. Call once, then stop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Short plain title, e.g. 'Delete 2 dentist tasks' (max ~40 chars)",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "What the user reads on their phone: one or two plain "
                        "sentences naming the items in human terms, e.g. 'Delete "
                        "the 10 \"This is a test\" tasks from Unsorted Tasks.' "
                        "No ids, no URLs, no markdown."
                    ),
                },
                "details": {
                    "type": "string",
                    "description": "For executing later (not shown to the user): exactly what will change, item by item with ids",
                },
            },
            "required": ["action", "message", "details"],
        },
    },
    request_approval,
)
