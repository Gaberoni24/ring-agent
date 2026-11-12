"""Background consumer: received captures -> agent loop -> done/failed.

Single consumer, in-process. Every capture ends with an ntfy push — success
or failure — because the ring gives the user no other feedback on this path.

Besides ring captures it runs the children that notification buttons queue
(see app/main.py /action): 'undo' reverses a capture, 'approved' carries out
a plan the user approved. Pending approvals are re-sent and expired here too.
"""

import asyncio
import json
import logging
import re
import traceback
from datetime import datetime

import anthropic

from app import actions, agent, db, links
from app.config import settings
from app.tools import history, notify, notion

log = logging.getLogger("ring.worker")

POLL_SECONDS = 30  # fallback poll in case a wake() is ever missed
APPROVAL_REMIND_MS = 12 * 3600 * 1000
APPROVAL_EXPIRE_MS = actions.TTL_SECONDS["approve"] * 1000
URGENT = 5  # ntfy max priority: pop-over, long vibration
# ntfy renders the first tag as an emoji in front of the title, so titles
# themselves carry no emoji (that showed up doubled: "✅ ✅ Ring").

_URL = re.compile(r"\s*[(<]?https?://[^\s)>]+[)>]?")


def plain(text: str) -> str:
    """Notification body: no links (the tap opens the item) or markdown."""
    text = _URL.sub("", text).replace("**", "").replace("`", "")
    return "\n".join(line.rstrip(" :,") for line in text.splitlines()).strip()


class Worker:
    def __init__(self) -> None:
        self._wake: asyncio.Event | None = None

    def start(self) -> asyncio.Task:
        self._wake = asyncio.Event()
        self._wake.set()  # drain anything already queued at startup
        return asyncio.get_running_loop().create_task(self._run())

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()
            while (row := db.claim_next_received()) is not None:
                await self._process(row)
            try:
                await self.check_approvals()
            except Exception:
                log.exception("approval check failed")

    async def _process(self, row) -> None:
        capture_id = row["id"]
        kind = row["kind"]
        transcription = row["transcription"]
        log.info("processing %s capture %s: %r", kind, capture_id, transcription)

        if kind == "ring" and not transcription:
            # Recording-only mode: nothing to act on (no server-side STT).
            db.finish_capture(capture_id, "done", summary="audio-only capture archived")
            await self._notify_safe(
                "Audio-only capture received — archived, no action taken "
                "(switch webhook payload mode to 'Both' for actions).",
                title="Ring capture",
                tags="floppy_disk",
            )
            return

        parent = db.get_capture(row["parent_id"]) if row["parent_id"] else None
        try:
            if kind == "undo":
                prompt = agent.UNDO_INSTRUCTIONS.format(
                    capture=history.render([parent], full=True)
                )
                result = await agent.run_capture(prompt, capture_id=capture_id, mode="undo")
            elif kind == "approved":
                plan = json.loads(transcription.split("\n", 1)[1])
                prompt = agent.APPROVED_INSTRUCTIONS.format(
                    action=plan["action"], details=plan["details"],
                    transcription=parent["transcription"] if parent else "",
                )
                result = await agent.run_capture(prompt, capture_id=capture_id, mode="approved")
            else:
                result = await agent.run_capture(
                    transcription, capture_id=capture_id, mode="normal"
                )
        except Exception as exc:
            log.error("capture %s failed: %s", capture_id, exc)
            log.debug("%s", traceback.format_exc())
            db.finish_capture(
                capture_id,
                "failed",
                summary=f"error: {exc}",
                tool_trace=None,
            )
            hint = ""
            if isinstance(exc, anthropic.AuthenticationError):
                hint = (
                    "\n⚠️ Anthropic rejected the API key — it may have been "
                    "revoked or expired. Fix: update "
                    "ANTHROPIC_API_KEY in /opt/ring-agent/.env, then "
                    "`systemctl restart ring-agent`."
                )
            what = {"undo": "Undo", "approved": "Approved action"}.get(kind, "Capture")
            await self._notify_safe(
                f"{what} failed: {exc}{hint}\n"
                f'Transcript: "{transcription}"\n'
                "Capture is saved on the server but will NOT auto-retry.",
                title=f"Ring {what.lower()} failed",
                tags="x",
            )
            return

        db.finish_capture(
            capture_id,
            "done",
            summary=result.summary,
            tool_trace=json.dumps(result.trace),
            tokens_used=result.tokens_used,
        )

        if kind == "undo":
            await self._finish_undo(parent, result)
        elif result.pending_approval:
            plan = {
                **result.pending_approval,
                "link": links.primary_link(result.trace),
                "requested_at": db._now(),
            }
            db.set_pending_approval(capture_id, json.dumps(plan))
            await self._send_approval(capture_id, plan, result.summary)
        else:
            buttons = []
            if actions.enabled() and links.mutations(result.trace):
                buttons.append(actions.button("↩️ Undo", "undo", capture_id))
            # The agent reports a task it couldn't do with "FAILED:"; that is
            # a failed task even though the capture itself ran fine.
            failed = result.summary.lstrip().upper().startswith("FAILED")
            body = re.sub(r"^\s*FAILED:?\s*", "", result.summary, flags=re.I) if failed else result.summary
            await self._notify_safe(
                plain(body),
                title="Ring: didn't work" if failed else "Ring",
                tags="x" if failed else "white_check_mark",
                click=links.primary_link(result.trace),
                actions=buttons,
            )

    async def _finish_undo(self, parent, result) -> None:
        """Log the undo on the Notion 'Undone' page, then tell the user."""
        logged = ""
        click = None
        if settings.notion_undo_page_id:
            stamp = datetime.now(settings.tz).strftime("%a %b %d %H:%M")
            said = " ".join((parent["transcription"] or "").split())
            was = " ".join((parent["summary"] or "").split())
            undone = " ".join(result.summary.split())
            try:
                await notion.append({
                    "page_id": settings.notion_undo_page_id,
                    "markdown": f'- ↩️ {stamp} — Undone: "{said}" — was: {was} — reversal: {undone}',
                })
                click = links.notion_url(settings.notion_undo_page_id)
            except Exception as exc:
                log.error("undo log to Notion failed: %s", exc)
                logged = f"\n(Could not log this on the Notion Undone page: {exc})"
        else:
            logged = "\n(NOTION_UNDO_PAGE_ID is not set, so this was not logged in Notion.)"
        await self._notify_safe(
            plain(result.summary) + logged, title="Undone", tags="leftwards_arrow_with_hook",
            click=click,
        )

    async def _send_approval(self, capture_id: str, plan: dict, summary: str,
                             reminder: bool = False) -> None:
        buttons = []
        if actions.enabled():
            buttons = [
                actions.button("✅ Approve", "approve", capture_id),
                actions.button("🚫 Deny", "deny", capture_id),
            ]
        prefix = "Reminder: still waiting for you.\n" if reminder else ""
        # Only the plain message; `details` holds the ids for the approved run.
        await self._notify_safe(
            prefix + plain(plan.get("message") or plan["action"]),
            title=f"Approve? {plan['action']}",
            tags="warning",
            click=plan.get("link"),
            actions=buttons,
            priority=URGENT,
        )

    async def check_approvals(self) -> None:
        """Re-send approvals still waiting after 12 h (in case the
        notification was swiped away) and expire them with the buttons."""
        now = db._now()
        for row in db.pending_approvals():
            plan = json.loads(row["pending_approval"])
            age = now - plan.get("requested_at", now)
            if age > APPROVAL_EXPIRE_MS:
                if db.take_pending_approval(row["id"]) is not None:
                    await self._notify_safe(
                        f"Nothing was done: {plan['action']}",
                        title="Approval expired", tags="hourglass",
                    )
            elif now - plan.get("reminded_at", plan.get("requested_at", now)) > APPROVAL_REMIND_MS:
                plan["reminded_at"] = now
                # Bookkeeping first: a tap during the send must not be undone.
                if db.update_pending_approval(row["id"], json.dumps(plan)):
                    await self._send_approval(row["id"], plan, row["summary"] or "",
                                              reminder=True)

    async def _notify_safe(self, message: str, title: str, tags: str, **kwargs) -> None:
        try:
            await notify.send(message, title=title, tags=tags, **kwargs)
        except Exception as exc:
            log.error("ntfy notification failed: %s", exc)


def approval_transcription(plan: dict) -> str:
    """Text stored on an 'approved' child: a readable first line for
    history, then the plan JSON the worker executes."""
    return f"Approved: {plan['action']}\n" + json.dumps(
        {"action": plan["action"], "details": plan["details"]}
    )

