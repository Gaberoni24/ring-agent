"""OAuth 2.1 client for Notion's hosted MCP server (https://mcp.notion.com).

Notion MCP is OAuth-only (no integration tokens), and Anthropic's MCP
connector expects *us* to hand it a valid bearer token. So this module owns
the whole lifecycle:

  begin()    -> dynamic client registration + PKCE, returns the URL to open
  complete() -> the redirect lands on /notion/oauth/callback, which calls this
  refresh()  -> refresh_token grant; every refresh is logged so we can see
                how long tokens actually live
  get_access_token() -> what the agent calls; refreshes when close to expiry
                and returns None (-> legacy tools) if anything is wrong

State lives in two JSON files under settings.data_dir (next to .env):
  notion_mcp_pending.json  one in-flight authorization (state + verifier)
  notion_mcp_token.json    tokens + client credentials + refresh log
"""

import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.config import settings

log = logging.getLogger("ring.notion_oauth")

ISSUER = "https://mcp.notion.com"
RESOURCE = "https://mcp.notion.com"
CALLBACK_PATH = "/notion/oauth/callback"
REFRESH_MARGIN_SECONDS = 300  # refresh when less than this remains
MAX_LOG_ENTRIES = 50


class OAuthError(Exception):
    pass


def token_path() -> Path:
    return settings.data_dir / "notion_mcp_token.json"


def pending_path() -> Path:
    return settings.data_dir / "notion_mcp_pending.json"


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def redirect_uri() -> str:
    return settings.public_base_url.rstrip("/") + CALLBACK_PATH


# --- discovery + registration -------------------------------------------------


def discover() -> dict:
    resp = httpx.get(f"{ISSUER}/.well-known/oauth-authorization-server", timeout=20)
    resp.raise_for_status()
    return resp.json()


def register_client(meta: dict, redirect: str) -> dict:
    """RFC 7591 dynamic registration. Returns {client_id, client_secret?}."""
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise OAuthError("authorization server does not support dynamic registration")
    resp = httpx.post(
        endpoint,
        json={
            "client_name": "ring-agent",
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        timeout=20,
    )
    if resp.status_code >= 400:
        raise OAuthError(f"registration failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    return {"client_id": data["client_id"], "client_secret": data.get("client_secret")}


# --- authorization code flow --------------------------------------------------


def begin() -> str:
    """Start an authorization. Returns the URL the user must open."""
    meta = discover()
    redirect = redirect_uri()
    existing = _load(token_path()) or {}
    if existing.get("client_id") and existing.get("redirect_uri") == redirect:
        client = {
            "client_id": existing["client_id"],
            "client_secret": existing.get("client_secret"),
        }
    else:
        client = register_client(meta, redirect)

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(32)
    _save(
        pending_path(),
        {
            "state": state,
            "code_verifier": verifier,
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret"),
            "redirect_uri": redirect,
            "token_endpoint": meta["token_endpoint"],
            "introspection_endpoint": meta.get("introspection_endpoint"),
            "created_at": int(time.time()),
        },
    )
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "default",
        "resource": RESOURCE,
    }
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


def _token_request(endpoint: str, data: dict, client_secret: str | None) -> dict:
    if client_secret:
        data = {**data, "client_secret": client_secret}
    resp = httpx.post(
        endpoint,
        data=data,
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if resp.status_code >= 400:
        raise OAuthError(f"token endpoint {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if "access_token" not in body:
        raise OAuthError(f"token response without access_token: {body}")
    return body


def _store_tokens(base: dict, body: dict, event: str) -> dict:
    now = int(time.time())
    expires_in = body.get("expires_in")
    old_refresh = base.get("refresh_token")
    new_refresh = body.get("refresh_token") or old_refresh
    entry = {
        "event": event,
        "at": now,
        "expires_in": expires_in,
        "refresh_token_rotated": bool(body.get("refresh_token"))
        and body.get("refresh_token") != old_refresh,
    }
    history = (base.get("log") or [])[-(MAX_LOG_ENTRIES - 1) :] + [entry]
    data = {
        "client_id": base["client_id"],
        "client_secret": base.get("client_secret"),
        "redirect_uri": base.get("redirect_uri"),
        "token_endpoint": base["token_endpoint"],
        "introspection_endpoint": base.get("introspection_endpoint"),
        "access_token": body["access_token"],
        "refresh_token": new_refresh,
        "token_type": body.get("token_type", "Bearer"),
        "scope": body.get("scope"),
        "obtained_at": now,
        "expires_in": expires_in,
        "expires_at": (now + int(expires_in)) if expires_in else None,
        "first_authorized_at": base.get("first_authorized_at") or now,
        "refresh_count": (base.get("refresh_count") or 0) + (1 if event == "refresh" else 0),
        "last_error": None,
        "log": history,
    }
    _save(token_path(), data)
    return data


def complete(code: str, state: str) -> dict:
    """Exchange the authorization code. Called by the callback route."""
    pending = _load(pending_path())
    if not pending:
        raise OAuthError("no authorization in progress - run scripts/notion_oauth.py begin")
    if not secrets.compare_digest(pending["state"], state or ""):
        raise OAuthError("state mismatch - possible stale or forged callback")
    if time.time() - pending["created_at"] > 3600:
        raise OAuthError("authorization expired - run begin again")
    body = _token_request(
        pending["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "client_id": pending["client_id"],
            "code_verifier": pending["code_verifier"],
            "resource": RESOURCE,
        },
        pending.get("client_secret"),
    )
    data = _store_tokens(pending, body, "authorize")
    try:
        pending_path().unlink()
    except OSError:
        pass
    return data


def refresh() -> dict:
    data = _load(token_path())
    if not data or not data.get("refresh_token"):
        raise OAuthError("no refresh token stored - authorize first")
    try:
        body = _token_request(
            data["token_endpoint"],
            {
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"],
                "client_id": data["client_id"],
                "resource": RESOURCE,
            },
            data.get("client_secret"),
        )
    except OAuthError as exc:
        data["last_error"] = {"at": int(time.time()), "error": str(exc)}
        _save(token_path(), data)
        raise
    return _store_tokens(data, body, "refresh")


def get_access_token() -> str | None:
    """Bearer token for the MCP connector, or None if MCP should be skipped."""
    if not settings.notion_mcp_enabled:
        return None
    data = _load(token_path())
    if not data or not data.get("access_token"):
        return None
    expires_at = data.get("expires_at")
    if expires_at and expires_at - time.time() < REFRESH_MARGIN_SECONDS:
        try:
            data = refresh()
        except Exception as exc:
            log.warning("Notion MCP token refresh failed: %s", exc)
            return None
    return data["access_token"]


# --- diagnostics --------------------------------------------------------------


def introspect(which: str = "access_token") -> dict:
    """Ask the authorization server what it thinks about a token. Notion
    exposes an introspection endpoint; this is how we measure real lifetimes."""
    data = _load(token_path())
    if not data:
        raise OAuthError("no token stored")
    endpoint = data.get("introspection_endpoint")
    if not endpoint:
        raise OAuthError("no introspection endpoint advertised")
    resp = httpx.post(
        endpoint,
        data={
            "token": data[which],
            "token_type_hint": which,
            "client_id": data["client_id"],
        },
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if resp.status_code >= 400:
        raise OAuthError(f"introspection {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def refresh_token_expires_in() -> int | None:
    """Seconds until the refresh token dies (from introspection), or None if
    unknown. Notion's refresh tokens have an absolute lifetime (~180 days
    from first authorization; rotation does not extend it), so this is the
    number that decides when the user must re-authorize."""
    info = introspect("refresh_token")
    if not info.get("active"):
        return 0
    exp = info.get("exp")
    return int(exp - time.time()) if exp else None


def status() -> dict:
    data = _load(token_path())
    if not data:
        return {"configured": False}
    now = int(time.time())
    return {
        "configured": True,
        "enabled": settings.notion_mcp_enabled,
        "client_id": data.get("client_id"),
        "obtained_at": data.get("obtained_at"),
        "expires_in_reported": data.get("expires_in"),
        "seconds_until_expiry": (data["expires_at"] - now) if data.get("expires_at") else None,
        "has_refresh_token": bool(data.get("refresh_token")),
        "refresh_count": data.get("refresh_count", 0),
        "first_authorized_at": data.get("first_authorized_at"),
        "last_error": data.get("last_error"),
        "log": data.get("log", []),
    }
