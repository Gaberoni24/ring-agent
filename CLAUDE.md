# CLAUDE.md — guide for agents working on this repo

A personal voice-capture agent: the Pebble Index 01 ring sends a
transcript to a small server, Claude decides what it means, and the result
lands in **TickTick** (tasks), **Google Calendar** (events) or **Notion**
(notes), with an **ntfy** push notification confirming it. Once set up it
is used daily — treat the owner's server as production.

The owner may not be an infrastructure person: explain server/DNS/OAuth
steps in plain language, give click-by-click instructions for anything they
must do in a web console, and ask before destructive actions on the server.

Server-specific values (hostname, SSH user, key path) live in the owner's
`.env` as `RING_HOST`, `RING_SSH_KEY`, `RING_DOMAIN`, `PUBLIC_BASE_URL`.
Read them from there (check presence, never print values).

## How a capture flows

```
ring → Pebble phone app (on-device speech-to-text, transcript only)
  → POST https://<your-host>/webhook  (Bearer WEBHOOK_SECRET)
  → Caddy (TLS) → uvicorn app.main:app on 127.0.0.1:8000
  → row saved in captures.db, HTTP 200 returned immediately
  → worker.py picks it up → agent.py runs a Claude tool-use loop
  → tools in app/tools/ call TickTick / Google Calendar / Notion
  → final summary (or failure) pushed via ntfy
     (tap opens the created item; Undo button; risky actions instead send
      an urgent Approve/Deny notification)

notification button → phone POSTs /action/{undo|approve|deny}/<id>?exp&sig
  → child capture '<id>:undo' / '<id>:approved' queued → worker runs it
```

The phone never retries, so `/webhook` must stay trivial and always up;
all real work happens in the background worker, and every outcome is
notified.

## Where things live

| Path | What it is |
|---|---|
| `app/main.py` | FastAPI: `/webhook`, `/action/...` (notification buttons), `/health`, `/notion/oauth/callback` |
| `app/worker.py` | Background consumer; always sends an ntfy result. Runs ring captures plus button children (`kind` = undo / approved); re-sends approvals after 12 h, expires them after 3 days |
| `app/actions.py` | HMAC-signed button URLs (key derived from `WEBHOOK_SECRET`; the secret never appears in a notification) |
| `app/links.py` | Reads a tool trace: tap target (`primary_link`) and what changed (`mutations`, decides whether Undo is offered) |
| `app/tools/approval.py` | `request_approval` tool; the hard gate for `APPROVAL_REQUIRED_TOOLS` is in `tools.dispatch` |
| `app/agent.py` | Claude loop. `CONTRACT` = built-in routing rules; system prompt also includes `context/CONTEXT.md` (or `CONTEXT.example.md` if missing), `context/LEARNED.md`, the Notion map, the current time, recent captures |
| `app/config.py` | All settings (read from `.env`; blank values mean "use the default") |
| `app/tools/ticktick.py` | TickTick add/update/complete/list (Open API, personal token) |
| `app/tools/gcal.py` | Google Calendar create/list events |
| `app/tools/notion.py` | Legacy Notion tools (fallback); the primary Notion path is Notion's hosted MCP server, wired in `agent.py` + `app/notion_oauth.py` |
| `app/tools/history.py` | Recent captures in the prompt + `recall_history` tool, so "actually, make that Friday" works |
| `app/tools/memory.py` | `remember_convention` tool → appends to `context/LEARNED.md` |
| `app/tools/notify.py` | ntfy push |
| `context/CONTEXT.example.md` | Template for the owner's routing rules. The owner's real rules go in `context/CONTEXT.md` (gitignored). Edit that for behaviour changes, not the code |
| `scripts/` | `provision.sh` (one-time server setup), `deploy.sh`, `captures.sh` (view recent captures), Notion/Google auth helpers, sweep |
| `deploy/` | Caddyfile template + systemd units |
| `README.md` | Full from-scratch setup guide (Oracle Cloud or any Linux server) |

## Common changes

- **Change how things are routed** (which list, default dates, what counts
  as an event): edit `context/CONTEXT.md`, then deploy. Before editing,
  check the server's `LEARNED.md` (see below) so you don't contradict a rule
  the owner spoke to the ring.
- **Add a new ability / integration:** create `app/tools/<name>.py` that
  defines async handlers and calls `register(schema, handler)` (copy the
  pattern in `ticktick.py`); raise `ToolError` for failures the agent should
  see. Import it at the bottom of `app/tools/__init__.py`, add a routing
  line to `CONTRACT` in `app/agent.py`, add any token to `config.py` and
  `.env.example`, and write tests in `tests/test_<name>.py` using `respx`
  to fake HTTP. Lookup-only tools that clutter history go in
  `_SKIP_IN_TRACE` in `history.py`.
- **Change what needs approval:** edit "Needs my approval" in
  `context/CONTEXT.md` (judgement rules), or `APPROVAL_REQUIRED_TOOLS` in
  `.env` (tools that are hard-blocked without an Approve tap). The owner can
  also say a rule to the ring ("from now on ask before ...") → LEARNED.md.
- **A new tool that changes things** must be added to `MUTATING` in
  `app/links.py` (so Undo is offered and the tap opens it) and covered in
  `UNDO_INSTRUCTIONS` in `agent.py`. Tools whose results feed Undo should
  report ids and, for edits, the previous values.
- **Change the Claude model:** `MODEL` / `ESCALATION_MODEL` in `.env`
  (defaults in `config.py`).
- **Upgrade packages:** bump versions in `requirements.txt`, run the tests.

## Test, deploy, sync

```bash
.venv/Scripts/python -m pytest -q     # Windows; .venv/bin/python on macOS/Linux
bash scripts/deploy.sh                # reads RING_HOST / RING_SSH_KEY from .env
```

Add `PUSH_SECRETS=1` to the deploy when `.env` or `google_token.json`
changed (uploads them with chmod 600). Deploy restarts the service and
checks `/health`. All tests must pass before deploying.

To try a change end to end, POST a fake capture (README "Test it") and
read the outcome with `bash scripts/captures.sh`. That creates real items
in the owner's apps — say what will be created and tell them to delete
test items.

## The server

- Typically Oracle Cloud Always Free (VM.Standard.E2.1.Micro: 1 GB RAM,
  no swap) or any Ubuntu 24.04 box. SSH as `ubuntu` with sudo.
- App in `/opt/ring-agent`, runs as user `ring`, systemd unit `ring-agent`.
  Timers: `ring-agent-sweep` (daily stuck-capture check), `notion-map`
  (refreshes the Notion page map every 4 h).
- **Server-only state (not in git, never overwritten by deploy):** `.env`,
  `captures.db` (every capture with summary + full tool trace),
  `context/LEARNED.md` (rules spoken to the ring), `context/notion_map.json`,
  `notion_mcp_token.json`, `google_token.json`, `backups/` (nightly).
- Security updates install nightly and the server reboots itself at
  08:30 UTC when one needs it (`/etc/apt/apt.conf.d/52ring-agent-reboot`);
  ~90 s downtime. Deploy runs `pip-audit --fix`, bumping Python packages
  with known vulnerabilities.
- Logs: `ssh <RING_HOST> "sudo journalctl -u ring-agent -n 100"`.

## Rules

- **Never print secrets** (`.env` values, tokens, the ntfy topic, the
  webhook secret). Check presence with lengths; pass them via shell
  variables, never echo them.
- Never commit `.env`, `google_client_secret.json`, `google_token.json`,
  token JSON files, or `context/CONTEXT.md` (all gitignored — keep it that way).
- Keep LF line endings (`.gitattributes` enforces it; shell scripts and
  systemd units break with CRLF). On Windows, Python `write_text` writes
  CRLF — write bytes or use the Edit tool.

## Gotchas already learned (don't rediscover them)

- **Oracle firewall:** Oracle's Ubuntu image ships iptables rules ending in
  REJECT, loaded by `iptables-persistent`. Installing `ufw` removes that
  package (apt conflict). `provision.sh` detects `/etc/iptables/rules.v4`
  and keeps iptables-persistent there. Ports 80/443 must also be open in the
  Oracle console's VCN security list.
- **Oracle idle reclamation:** Always Free instances that are idle for 7
  days (CPU p95 < 20% and network < 20%) can be stopped. Ring traffic never
  counts as busy. The fix is upgrading the account to Pay As You Go (still
  $0 within Always Free limits).
- **TickTick** does not parse natural-language dates; the agent sends ISO
  local dates and `ticktick.py` converts them with `RING_TZ`. List names
  can contain emoji, so lookup ignores emoji/punctuation/case. A task's
  `list_id` is required to update/complete it (it's in the "created task"
  result in history).
- **Google Calendar** rejects datetimes without seconds (`10:00` → 400);
  `gcal.py` always sends `HH:MM:SS`. The Google Cloud OAuth app must stay
  **In production** — in "Testing" its refresh token dies after 7 days. If
  the token breaks: `scripts/setup_google.py` on the laptop, then deploy with
  `PUSH_SECRETS=1`.
- **Notion:** the internal integration (`NOTION_TOKEN`) only sees pages
  shared with it. The MCP OAuth token (`scripts/notion_oauth.py
  begin|status`) is the primary path; the sweep warns via ntfy when it
  needs re-approval.
- **Anthropic API** is pay-per-use credit (separate from Claude.ai). A task
  costs a few cents; Notion notes ~10–15¢ because the agent reads the page
  first. "Credit balance is too low" errors mean the owner must top up at
  console.anthropic.com.
- **ntfy buttons:** max 3 per notification; `http` buttons POST from the
  phone, a non-2xx reply shows as a failed tap. There is no typed-reply
  field. The TickTick tap link (`ticktick.com/webapp/#p/...`) may open the
  browser rather than the app on Android.
- **Undo** logs to the Notion page `NOTION_UNDO_PAGE_ID`. Completed
  TickTick tasks can't be reopened via the API.
- **Prompt injection:** tool results (calendar invites from others, Notion
  pages) are untrusted text. `remember_convention` only saves directly when
  the owner's own transcript sounds like a rule (`RULE_CUES` in `memory.py`);
  otherwise the agent must go through `request_approval`.
- Claude.ai connectors (e.g. Google Calendar in the Claude app) cannot be
  used by this agent — it calls the APIs with its own credentials.
