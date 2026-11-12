#!/usr/bin/env bash
# Show the most recent captures on the server: what was said, the outcome,
# and each tool call. Read-only; prints no secrets.
#   bash scripts/captures.sh        # last 5
#   bash scripts/captures.sh 20     # last 20
set -euo pipefail

cd "$(dirname "$0")/.."
N="${1:-5}"
# RING_HOST / RING_SSH_KEY come from the environment or .env.
envval() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n1 | tr -d '\r"' || true; }
HOST="${RING_HOST:-$(envval RING_HOST)}"
HOST="${HOST:?set RING_HOST=user@host in .env}"
KEY="${RING_SSH_KEY:-$(envval RING_SSH_KEY)}"
SSH_OPTS=()
if [ -n "$KEY" ]; then
    SSH_OPTS=(-i "${KEY/#\~/$HOME}")
fi

ssh "${SSH_OPTS[@]}" "$HOST" "sudo python3 - $N" <<'EOF'
import json, sqlite3, sys
from datetime import datetime

db = sqlite3.connect("/opt/ring-agent/captures.db")
rows = db.execute(
    "select created_at, status, transcription, summary, tool_trace, tokens_used "
    "from captures order by created_at desc limit ?",
    (int(sys.argv[1]),),
).fetchall()
for created, status, said, summary, trace, tokens in rows:
    when = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== {when}  [{status}]  tokens={tokens}")
    print(f"  said:    {said}")
    print(f"  outcome: {summary}")
    for t in json.loads(trace or "[]"):
        err = "ERROR " if t.get("is_error") else ""
        args = json.dumps(t.get("input", {}), ensure_ascii=False)[:150]
        print(f"  - {t['tool']} {args} -> {err}{str(t.get('result', ''))[:150]}")
    print()
EOF
