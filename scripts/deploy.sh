#!/usr/bin/env bash
# Deploy the app to the server from your computer (Git Bash/WSL/macOS/Linux)
# and restart it:
#   bash scripts/deploy.sh
#
# Reads RING_HOST, RING_SSH_KEY and RING_DOMAIN from .env (or from the
# environment, which wins). Works with root or a sudo-capable user (Oracle
# Cloud's default "ubuntu").
#   RING_HOST=ubuntu@my-ring.duckdns.org  SSH login (required)
#   RING_SSH_KEY=~/.ssh/ring_server        SSH private key to use
#   RING_DOMAIN=my-ring.duckdns.org        install deploy/Caddyfile for this hostname
#   PUSH_SECRETS=1                         upload local .env and google_token.json
set -euo pipefail

cd "$(dirname "$0")/.."

# Read one KEY=value line from .env without executing the file.
envval() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n1 | tr -d '\r"' || true; }
RING_HOST="${RING_HOST:-$(envval RING_HOST)}"
RING_SSH_KEY="${RING_SSH_KEY:-$(envval RING_SSH_KEY)}"
RING_DOMAIN="${RING_DOMAIN:-$(envval RING_DOMAIN)}"

HOST="${RING_HOST:?set RING_HOST=user@host in .env}"
APP_DIR=/opt/ring-agent
SSH_OPTS=()
if [ -n "${RING_SSH_KEY:-}" ]; then
    SSH_OPTS=(-i "${RING_SSH_KEY/#\~/$HOME}")
fi
export RSYNC_RSH="ssh ${SSH_OPTS[*]}"
remote() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

echo "== sync code =="
if command -v rsync >/dev/null && remote "command -v rsync" >/dev/null 2>&1; then
    rsync -az --delete --rsync-path="sudo rsync" \
        --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
        --exclude '.env' --exclude 'captures.db*' --exclude 'audio/' \
        --exclude 'backups/' --exclude 'context/notion_map.json' \
        --exclude 'context/LEARNED.md' \
        app pyproject.toml requirements.txt scripts deploy context "$HOST:$APP_DIR/"
else
    # Windows dev machines usually lack rsync — tar over ssh instead.
    # No --delete semantics; stale .py files are cleaned server-side below.
    remote "sudo find $APP_DIR/app $APP_DIR/scripts $APP_DIR/deploy -type f 2>/dev/null | sudo xargs -r rm -f"
    tar czf - \
        --exclude '__pycache__' --exclude 'context/notion_map.json' \
        --exclude 'context/LEARNED.md' \
        app pyproject.toml requirements.txt scripts deploy context \
        | remote "sudo tar xzf - -C $APP_DIR"
fi

if [ "${PUSH_SECRETS:-}" = "1" ]; then
    echo "== upload secrets =="
    for f in .env google_token.json; do
        if [ -f "$f" ]; then
            remote "sudo tee $APP_DIR/$f >/dev/null && sudo chmod 600 $APP_DIR/$f" < "$f"
            echo "uploaded $f"
        fi
    done
fi

if [ -n "${RING_DOMAIN:-}" ]; then
    echo "== caddy for $RING_DOMAIN =="
    sed "s/ring\.example\.com/$RING_DOMAIN/" deploy/Caddyfile \
        | remote "sudo tee /etc/caddy/Caddyfile >/dev/null && sudo systemctl reload caddy"
fi

echo "== install deps + units =="
remote "sudo bash -c 'set -e
    chown -R ring:ring $APP_DIR
    chmod 750 $APP_DIR
    chmod -R o-rwx $APP_DIR
    sudo -u ring $APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt -e $APP_DIR pip-audit
    # pip install never upgrades what is already there, so packages with
    # published vulnerabilities are bumped to their first fixed version here.
    sudo -u ring $APP_DIR/.venv/bin/pip-audit --fix --skip-editable --progress-spinner off \
        || echo WARNING: pip-audit could not fix everything, see above
    cp $APP_DIR/deploy/ring-agent.service $APP_DIR/deploy/ring-agent-sweep.* $APP_DIR/deploy/notion-map.* /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now ring-agent ring-agent-sweep.timer notion-map.timer
    systemctl restart ring-agent
'"

echo "== health check =="
sleep 8  # slow single-vCPU boxes need a moment to boot uvicorn
remote "curl -fsS http://127.0.0.1:8000/health" && echo && echo "deploy OK"
