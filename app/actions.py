"""Signed URLs for ntfy notification buttons (Undo / Approve / Deny).

A button makes the phone POST straight to this server. Anyone who knows the
ntfy topic can read the notification, so a button URL must never carry the
webhook secret: each one is an HMAC over (kind, capture id, expiry), good for
exactly that action on exactly that capture until it expires."""

import hashlib
import hmac
import time
from urllib.parse import quote

from app.config import settings

KINDS = ("undo", "approve", "deny")
TTL_SECONDS = {"undo": 7 * 86400, "approve": 3 * 86400, "deny": 3 * 86400}


def _key() -> bytes:
    # Derived, so no extra secret to manage and the webhook secret itself
    # never leaves the server.
    return hmac.new(settings.webhook_secret.encode(), b"ntfy-actions", hashlib.sha256).digest()


def sign(kind: str, capture_id: str, exp: int) -> str:
    msg = f"{kind}|{capture_id}|{exp}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def verify(kind: str, capture_id: str, exp: int, sig: str) -> bool:
    if not settings.webhook_secret or kind not in KINDS:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sign(kind, capture_id, exp), sig)


def url(kind: str, capture_id: str) -> str:
    exp = int(time.time()) + TTL_SECONDS[kind]
    base = settings.public_base_url.rstrip("/")
    return f"{base}/action/{kind}/{quote(capture_id, safe='')}?exp={exp}&sig={sign(kind, capture_id, exp)}"


def button(label: str, kind: str, capture_id: str) -> dict:
    """An ntfy 'http' action: the phone POSTs in the background, then the
    notification is dismissed."""
    return {"action": "http", "label": label, "url": url(kind, capture_id),
            "method": "POST", "clear": True}


def enabled() -> bool:
    return bool(settings.public_base_url and settings.webhook_secret)
