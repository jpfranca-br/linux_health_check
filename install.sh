#!/usr/bin/env bash
# vps-health installer and updater.
#
#   sudo bash install.sh [--name NAME] [--notes FILE]
#
# New server: asks for the Telegram bot token (one bot per server), your chat ID
# (found automatically), the Claude token (same one on every server) and the schedule.
# Existing install: keeps /etc/vps-health/config.env and just updates the code.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/vps-health/config.env
NOTES=/etc/vps-health/notes.md
NAME_ARG="" NOTES_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME_ARG="$2"; shift 2 ;;
    --notes) NOTES_ARG="$(readlink -f "$2")"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "Run it with sudo: sudo bash $0"; exit 1; }

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ask() { local v; read -r -p "$1${2:+ [$2]}: " v </dev/tty; echo "${v:-${2:-}}"; }
ask_secret() { local v; read -r -s -p "$1: " v </dev/tty; printf '\n' >/dev/tty; echo "$v"; }
tg() { curl -s -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/$1"; }

say "1/6 Packages"
need=()
for p in python3 python3-requests curl; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
if [ ${#need[@]} -gt 0 ]; then
  apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${need[@]}"
fi
echo "ok"

NEW=1
if [ -f "$CONF" ]; then set -a; . "$CONF"; set +a; NEW=0; fi

say "2/6 Claude CLI"
CLAUDE_USER="${CLAUDE_USER:-${SUDO_USER:-}}"
if [ -z "$CLAUDE_USER" ] || [ "$CLAUDE_USER" = root ]; then
  CLAUDE_USER="$(ask "Normal (non-root) user to run Claude as")"
fi
id "$CLAUDE_USER" >/dev/null 2>&1 || { echo "User $CLAUDE_USER does not exist"; exit 1; }
CLAUDE_HOME="$(getent passwd "$CLAUDE_USER" | cut -d: -f6)"
CLAUDE_BIN="${CLAUDE_BIN:-$CLAUDE_HOME/.local/bin/claude}"
if [ ! -x "$CLAUDE_BIN" ]; then
  echo "Installing Claude CLI for $CLAUDE_USER…"
  runuser -u "$CLAUDE_USER" -- bash -c 'cd ~ && curl -fsSL https://claude.ai/install.sh | bash' >/dev/null
fi
runuser -u "$CLAUDE_USER" -- "$CLAUDE_BIN" --version

say "3/6 Settings"
if [ "$NEW" = 0 ]; then echo "Keeping the existing settings in $CONF"; fi
SERVER_NAME="${NAME_ARG:-${SERVER_NAME:-}}"
[ -n "$SERVER_NAME" ] || SERVER_NAME="$(ask "Name for this server in Telegram messages" "$(hostname)")"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "Each server needs its OWN Telegram bot. In Telegram, message @BotFather, send /newbot and copy the token."
  TELEGRAM_BOT_TOKEN="$(ask_secret "Bot token")"
fi
BOT="$(tg getMe | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")')"
[ -n "$BOT" ] || { echo "Telegram rejected that bot token."; exit 1; }
echo "Bot: @$BOT"
if [ "$NEW" = 1 ] && tg "getUpdates?timeout=0&limit=1" | grep -q '"error_code":409'; then
  echo "@$BOT is already in use by another server. Create a new bot for this one."; exit 1
fi

if [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  TELEGRAM_CHAT_ID="$(ask "Your Telegram chat ID (same on every server; leave empty to detect it)")"
fi
if [ -z "$TELEGRAM_CHAT_ID" ]; then
  echo "Open @$BOT in Telegram and press Start (or send it any message). Waiting up to 3 minutes…"
  for _ in $(seq 36); do
    TELEGRAM_CHAT_ID="$(tg getUpdates | python3 -c '
import json,sys
for u in json.load(sys.stdin).get("result", []):
    c = (u.get("message") or {}).get("chat", {})
    if c.get("type") == "private": print(c["id"]); break')"
    [ -n "$TELEGRAM_CHAT_ID" ] && break
    sleep 5
  done
  [ -n "$TELEGRAM_CHAT_ID" ] || { echo "No message arrived. Run the installer again."; exit 1; }
fi
echo "Chat ID: $TELEGRAM_CHAT_ID"

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Paste your Claude token. The same token works on all your servers."
  echo "(To create one, run on any machine:  claude setup-token)"
  CLAUDE_CODE_OAUTH_TOKEN="$(ask_secret "Claude token (empty = basic checks without AI)")"
fi

if [ -z "${SCHEDULE:-}" ]; then
  SCHEDULE="$(ask "When to run (systemd calendar format)" "Sun *-*-* 19:00:00 America/Sao_Paulo")"
fi
systemd-analyze calendar "$SCHEDULE" >/dev/null || { echo "Invalid schedule: $SCHEDULE"; exit 1; }

install -d -m 700 /etc/vps-health /var/lib/vps-health
umask 077
cat > "$CONF" <<EOF
# vps-health settings for this server. After editing: sudo systemctl restart vps-health-bot
SERVER_NAME="$SERVER_NAME"
TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID"
CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
CLAUDE_USER="$CLAUDE_USER"
CLAUDE_BIN="$CLAUDE_BIN"
CLAUDE_MODEL="${CLAUDE_MODEL:-sonnet}"
REPORT_LANGUAGE="${REPORT_LANGUAGE:-English}"
# When the check-up runs (re-run install.sh after changing it)
SCHEDULE="$SCHEDULE"
# Services are found automatically. Optional overrides (space-separated unit names):
WATCH_SERVICES="${WATCH_SERVICES:-}"
IGNORE_SERVICES="${IGNORE_SERVICES:-}"
EOF
umask 022
echo "Saved $CONF"

say "4/6 Files"
install -d -m 755 /opt/vps-health
install -m 755 "$SRC/vpshealth.py" "$SRC/collect.sh" /opt/vps-health/
if [ -n "$NOTES_ARG" ]; then
  install -m 600 "$NOTES_ARG" "$NOTES"; echo "Notes: copied $NOTES_ARG"
elif [ ! -f "$NOTES" ]; then
  install -m 600 "$SRC/notes/template.md" "$NOTES"; echo "Notes: template created (edit $NOTES)"
else
  echo "Notes: keeping $NOTES"
fi

say "5/6 Services"
install -m 644 "$SRC/vps-health-bot.service" "$SRC/vps-health-weekly.service" /etc/systemd/system/
cat > /etc/systemd/system/vps-health-weekly.timer <<EOF
[Unit]
Description=Run the vps-health check-up ($SCHEDULE)

[Timer]
OnCalendar=$SCHEDULE
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now vps-health-weekly.timer >/dev/null
systemctl enable vps-health-bot.service >/dev/null
systemctl restart vps-health-bot.service
sleep 2
echo "bot: $(systemctl is-active vps-health-bot)   next check-up: $(systemctl show -p NextElapseUSecRealtime --value vps-health-weekly.timer)"

say "6/6 What will be monitored"
set -a; . "$CONF"; set +a
python3 /opt/vps-health/vpshealth.py discover
python3 /opt/vps-health/vpshealth.py test && echo "Test message sent to Telegram."
echo
echo "Done. Send /check to @$BOT to get a report now."
echo "Tell the AI what's normal on this server in $NOTES (no restart needed)."
