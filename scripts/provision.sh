#!/usr/bin/env bash
# One-time VPS bootstrap for the ring agent. Idempotent — safe to re-run.
# Run as root on a fresh Ubuntu 24.04 LTS box (Oracle Cloud: via sudo as
# the default ubuntu user):
#   sudo bash provision.sh
set -euo pipefail

APP_DIR=/opt/ring-agent

# Oracle Cloud Ubuntu images ship iptables rules in /etc/iptables/rules.v4
# (loaded at boot by iptables-persistent) ending in a REJECT. ufw and
# iptables-persistent conflict in apt (installing one removes the other),
# so pick exactly one: keep Oracle's iptables setup there, ufw elsewhere.
if [ -f /etc/iptables/rules.v4 ]; then
    FIREWALL_PKG=iptables-persistent
else
    FIREWALL_PKG=ufw
fi

echo "== packages (firewall: $FIREWALL_PKG) =="
apt-get update
if [ "$FIREWALL_PKG" = iptables-persistent ]; then
    # Non-interactive install; autosave keeps the currently loaded rules.
    echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" | debconf-set-selections
    echo "iptables-persistent iptables-persistent/autosave_v6 boolean true" | debconf-set-selections
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12 python3.12-venv python3-pip \
    "$FIREWALL_PKG" curl debian-keyring debian-archive-keyring apt-transport-https \
    rsync sqlite3

echo "== caddy (official repo) =="
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
fi

echo "== firewall =="
if [ "$FIREWALL_PKG" = iptables-persistent ]; then
    # Open 80/443 just above Oracle's REJECT (rules appended after it would
    # never match) and persist. The VCN security list in the Oracle console
    # must ALSO allow 80/443 — see RUNBOOK.md.
    for port in 80 443; do
        if ! iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null; then
            pos=$(iptables -L INPUT --line-numbers | awk '/REJECT/ {print $1; exit}')
            if [ -n "$pos" ]; then
                iptables -I INPUT "$pos" -p tcp -m state --state NEW --dport "$port" -j ACCEPT
            else
                iptables -A INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT
            fi
        fi
    done
    netfilter-persistent save
else
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
fi

echo "== ring user + dirs =="
id -u ring &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin ring
mkdir -p "$APP_DIR"/{audio,backups}
chown -R ring:ring "$APP_DIR"
chmod 750 "$APP_DIR"  # captures, tokens and backups: ring (and root) only

echo "== venv =="
if [ ! -d "$APP_DIR/.venv" ]; then
    sudo -u ring python3.12 -m venv "$APP_DIR/.venv"
fi

if [ ! -f "$APP_DIR/.env" ]; then
    echo "NOTE: create $APP_DIR/.env from .env.example, then: chmod 600 $APP_DIR/.env && chown ring:ring $APP_DIR/.env"
fi

echo "== nightly backup (db + audio tarball, 14 kept) =="
cat > /etc/cron.daily/ring-agent-backup <<'EOF'
#!/bin/sh
set -e
cd /opt/ring-agent
sqlite3 captures.db ".backup backups/captures-$(date +%F).db"
tar czf "backups/audio-$(date +%F).tar.gz" audio 2>/dev/null || true
ls -t backups/captures-*.db 2>/dev/null | tail -n +15 | xargs -r rm
ls -t backups/audio-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm
EOF
chmod +x /etc/cron.daily/ring-agent-backup

echo "== reboot after security updates that need it =="
# unattended-upgrades installs security fixes nightly, but kernel/libc fixes
# only take effect after a reboot. 08:30 UTC = 3:30-4:30am US Eastern; change
# it to a time you are asleep in your timezone. Services come back on boot.
cat > /etc/apt/apt.conf.d/52ring-agent-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "08:30";
EOF

echo "== disable unused network services =="
# rpcbind (NFS helper) ships enabled on Oracle's image; nothing here uses it.
systemctl mask --now rpcbind.service rpcbind.socket 2>/dev/null || true

echo
echo "Provisioning done. Next (see RUNBOOK.md):"
echo "  1. Put the app code in $APP_DIR (scripts/deploy.sh from your machine)"
echo "  2. Create $APP_DIR/.env (chmod 600, owner ring)"
echo "  3. Edit /etc/caddy/Caddyfile with your hostname; systemctl reload caddy"
echo "  4. systemctl enable --now ring-agent ring-agent-sweep.timer notion-map.timer"
