"""FastAPI receiver for Pebble Index ring webhooks.

Contract with the phone (see coredevices/mobileapp INDEX_WEBHOOK_API.md):
multipart/form-data with fields `transcription` and/or `audio`
(<recordingId>.m4a), plus `recordedAt` (unix ms) and `client`.

The one job of /webhook is to never lose a capture: authenticate, persist,
return 200 fast. The phone has no persistent retry queue, so anything slow
or fallible (the agent loop) happens after the response, in the worker.
"""

import hashlib
import hmac
import html
import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app import actions, db, links, notion_oauth
from app.config import settings
from app.tools import notify
from app.worker import Worker, approval_transcription

log = logging.getLogger("ring.main")

worker = Worker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    db.init(settings.db_path)
    requeued = db.requeue_stale_processing()
    if requeued:
        log.info("requeued %d stale 'processing' captures", requeued)
    task = worker.start()
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

# Capture ids become file names under audio/, so only plain names are
# taken from the upload; anything else (e.g. "../x") gets a hashed id.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _authorized(request: Request) -> bool:
    if not settings.webhook_secret:
        return False  # unconfigured server accepts nothing
    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.webhook_secret}"
    return hmac.compare_digest(header.encode(), expected.encode())


@app.post("/webhook")
async def webhook(
    request: Request,
    transcription: str | None = Form(None),
    audio: UploadFile | None = File(None),
    recordedAt: str | None = Form(None),
    client: str | None = Form(None),
):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        recorded_at = int(recordedAt) if recordedAt else 0
    except ValueError:
        recorded_at = 0

    # Idempotency key: the ring names the audio part <recordingId>.m4a.
    # Transcription-only mode has no id, so synthesize a stable one.
    capture_id = ""
    if audio is not None and audio.filename:
        capture_id = audio.filename.rsplit(".", 1)[0]
    if not _SAFE_ID.fullmatch(capture_id):
        key = f"{transcription or ''}|{recorded_at}"
        if capture_id:
            key += f"|{capture_id}"
        capture_id = hashlib.sha256(key.encode()).hexdigest()[:32]

    audio_path = None
    if audio is not None:
        audio_path = str(settings.audio_dir / f"{capture_id}.m4a")
        data = await audio.read()
        with open(audio_path, "wb") as f:
            f.write(data)

    inserted = db.insert_capture(capture_id, recorded_at, transcription, audio_path)
    if inserted:
        worker.wake()
    else:
        log.info("duplicate delivery for capture %s ignored", capture_id)

    return {"ok": True, "id": capture_id, "duplicate": not inserted}


@app.post("/action/{kind}/{capture_id}")
async def action(kind: str, capture_id: str, exp: int = 0, sig: str = ""):
    """A notification button tapped on the phone (see app/actions.py). Like
    /webhook it only records and queues; the worker does the work and sends
    the result. Non-2xx responses make the ntfy app show the tap failed."""
    if not actions.verify(kind, capture_id, exp, sig):
        return JSONResponse({"error": "invalid or expired link"}, status_code=403)
    parent = db.get_capture(capture_id)
    if parent is None:
        return JSONResponse({"error": "unknown capture"}, status_code=404)

    if kind == "undo":
        if parent["status"] != "done" or not links.mutations(parent["tool_trace"]):
            return JSONResponse({"error": "nothing to undo"}, status_code=409)
        queued = db.insert_child(capture_id, "undo", f'Undo: "{parent["transcription"]}"')
        if queued:
            worker.wake()
        return {"ok": True, "queued": queued}

    raw = db.take_pending_approval(capture_id)
    if raw is None:
        return JSONResponse({"error": "already decided or expired"}, status_code=409)
    plan = json.loads(raw)
    if kind == "approve":
        db.insert_child(capture_id, "approved", approval_transcription(plan))
        worker.wake()
        return {"ok": True, "queued": True}

    db.insert_child(
        capture_id, "denied", f"Denied: {plan['action']}",
        status="done", summary=f"User denied: {plan['action']} — nothing was done.",
    )
    try:
        await notify.send(
            f"Nothing was done: {plan['action']}", title="Denied", tags="no_entry_sign"
        )
    except Exception as exc:
        log.error("ntfy notification failed: %s", exc)
    return {"ok": True, "queued": False}


@app.get("/notion/oauth/callback", response_class=HTMLResponse)
async def notion_oauth_callback(
    code: str | None = None, state: str | None = None, error: str | None = None,
    error_description: str | None = None,
):
    """Second half of the Notion MCP OAuth flow (see app/notion_oauth.py).
    Safe to expose: the exchange only succeeds against the pending state
    created by scripts/notion_oauth.py begin, and the code is single-use."""
    if error:
        detail = html.escape(f"{error}: {error_description or ''}")
        return HTMLResponse(f"<h1>Notion authorization failed</h1><p>{detail}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Missing code</h1>", status_code=400)
    try:
        data = notion_oauth.complete(code, state or "")
    except notion_oauth.OAuthError as exc:
        log.warning("notion oauth callback rejected: %s", exc)
        return HTMLResponse(f"<h1>Notion authorization rejected</h1><p>{html.escape(str(exc))}</p>", status_code=400)
    except Exception as exc:  # token endpoint down etc.
        log.error("notion oauth exchange failed: %s", exc)
        return HTMLResponse("<h1>Token exchange failed</h1><p>Check server logs.</p>", status_code=502)
    log.info("Notion MCP authorized; access token expires_in=%s", data.get("expires_in"))
    return HTMLResponse(
        "<h1>Notion connected to ring-agent</h1>"
        f"<p>Access token lifetime: {data.get('expires_in')} s. "
        f"Refresh token: {'yes' if data.get('refresh_token') else 'NO'}.</p>"
        "<p>You can close this tab.</p>"
    )


@app.get("/health")
async def health():
    try:
        depth = db.queue_depth()
    except Exception as exc:  # pragma: no cover - db must be very broken
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, "queue_depth": depth}
