# vps-health

Weekly server check-up: collects updates, memory, disk, services, logs and security data,
lets Claude diagnose it, and sends you a Telegram report with **Fix / Fix + reboot / Ignore** buttons.
Claude only picks from a fixed list of safe fixes (apt upgrade/autoremove/clean, snap refresh,
journal vacuum, needrestart, restarting app services). It never runs arbitrary commands.

## Add a new server (Ubuntu/Debian)

1. In Telegram, create a **new bot** for that server: message @BotFather, send `/newbot`, copy the token.
   (Each server needs its own bot; your chat ID and Claude token are the same everywhere.)
2. On the server, as your normal (sudo) user:
   ```bash
   git clone https://github.com/jpfranca-br/linux_health_check.git ~/vps-health
   sudo bash ~/vps-health/install.sh --name MYSERVER
   ```
   It installs Claude CLI if needed and asks for the bot token, your chat ID (detected automatically
   when you message the bot), a Claude token (`claude setup-token`, one token works for all servers)
   and the schedule, then sends a test message.
3. Optional: pass your own notes file with `--notes FILE`.
4. Describe the server in `/etc/vps-health/notes.md` (what runs there, what's intentional),
   so the AI doesn't flag normal things. Start from `notes/template.md`.

## What gets monitored

Found automatically on every run: your own enabled services in `/etc/systemd/system`,
plus any service listening on a port (except OS plumbing like ssh). Ports that look like
web apps get an HTTP check. To see the list: `sudo bash -c 'set -a; . /etc/vps-health/config.env; python3 /opt/vps-health/vpshealth.py discover'`.
Override with `WATCH_SERVICES` / `IGNORE_SERVICES` in `/etc/vps-health/config.env`.

## Telegram commands

`fix` · `fix and reboot` · `reboot` · `ignore` · `/status` · `/check` (Portuguese works too: corrigir, reiniciar).

## Update / change settings

```bash
cd ~/vps-health && git pull && sudo bash install.sh
```
Re-running the installer keeps the existing settings.
Settings: `/etc/vps-health/config.env` · reports: `/var/lib/vps-health/history/` ·
logs: `sudo journalctl -u vps-health-bot -u vps-health-weekly -n 50`

## Requirements

Ubuntu or Debian with systemd, a Claude subscription (or an Anthropic API key in `ANTHROPIC_API_KEY`), and Telegram.
The Claude token from `claude setup-token` lasts one year; renew it in config.env on each server.
