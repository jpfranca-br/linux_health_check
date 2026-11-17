#!/usr/bin/env python3
"""vps-health: weekly server check-up with Claude diagnostics and Telegram approval.

  vpshealth.py weekly   collect data, analyse with Claude, send report + proposed fixes
  vpshealth.py bot      Telegram listener: applies the fixes when you answer "fix"
  vpshealth.py test     send a test Telegram message
  vpshealth.py discover show which services/ports would be monitored

Runs as root (from systemd). Claude itself runs as an unprivileged user with no tools:
it only reads the report and picks fixes from the fixed ACTIONS list below, so it can
never run arbitrary commands.
"""
import datetime as dt
import glob
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import threading
import time

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("STATE_DIR", "/var/lib/vps-health")
PENDING = os.path.join(STATE, "pending.json")
OFFSET = os.path.join(STATE, "tg_offset")
REBOOT_MARK = os.path.join(STATE, "rebooted.json")
HISTORY = os.path.join(STATE, "history")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
CLAUDE_USER = os.environ.get("CLAUDE_USER", "")
CLAUDE_HOME = pwd.getpwnam(CLAUDE_USER).pw_dir if CLAUDE_USER else ""
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", f"{CLAUDE_HOME}/.local/bin/claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
LANGUAGE = os.environ.get("REPORT_LANGUAGE", "English")
EXTRA_WATCH = os.environ.get("WATCH_SERVICES", "").split()     # optional: always watch these
IGNORE = set(os.environ.get("IGNORE_SERVICES", "").split())    # optional: never watch these
NOTES_FILE = os.environ.get("NOTES_FILE", "/etc/vps-health/notes.md")
HOST = os.environ.get("SERVER_NAME") or os.uname().nodename
PENDING_MAX_AGE = 7 * 86400
NEVER_RESTART = {"ssh", "sshd", "systemd-networkd", "systemd-resolved", "dbus", "vps-health-bot"}
# OS plumbing that listens on ports but isn't "an app on this server"
BASE_SERVICES = NEVER_RESTART | {"systemd-timesyncd", "chrony", "chronyd", "cups", "cups-browsed", "avahi-daemon",
                                 "rpcbind", "snapd", "multipathd", "systemd-journald", "systemd-logind", "rsyslog",
                                 "cloud-init", "qemu-guest-agent", "polkit", "ModemManager", "fwupd"}
NON_HTTP_PORTS = {22, 25, 53, 110, 143, 465, 587, 993, 995, 1883, 8883, 3306, 5432, 6379, 11211, 27017, 5672, 9092}

APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a"}
APT_OPTS = ["-y", "-q", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]

# The only things that can ever be executed. Claude picks from these by id.
ACTIONS = {
    "apt_upgrade": ("Install pending package updates",
                    [["apt-get", "update", "-q"], ["apt-get", *APT_OPTS, "--with-new-pkgs", "upgrade"]]),
    "apt_autoremove": ("Remove unused packages / old kernels", [["apt-get", *APT_OPTS, "autoremove", "--purge"]]),
    "apt_clean": ("Clear the apt download cache", [["apt-get", "clean"]]),
    "snap_refresh": ("Update snap packages", [["snap", "refresh"]]),
    "journal_vacuum": ("Shrink the system journal (keep 14 days / 200 MB)",
                       [["journalctl", "--vacuum-time=14d", "--vacuum-size=200M"]]),
    "needrestart": ("Restart services still using old libraries", [["needrestart", "-r", "a"]]),
    "reset_failed": ("Clear failed-unit markers", [["systemctl", "reset-failed"]]),
    "restart_service": ("Restart a service", None),  # arg = unit name
}

SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "attention", "critical"]},
        "summary": {"type": "string", "description": "Telegram-ready overview, plain text, short bullet lines with emoji"},
        "actions": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string", "enum": list(ACTIONS)},
            "arg": {"type": "string"},
            "reason": {"type": "string"}}, "required": ["id", "reason"]}},
        "reboot_recommended": {"type": "boolean"},
        "reboot_reason": {"type": "string"},
        "manual": {"type": "array", "items": {"type": "string"},
                   "description": "Issues that need a human, with the suggested command"},
    },
    "required": ["status", "summary", "actions", "reboot_recommended", "manual"],
}


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- telegram
def tg(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=70)
    data = r.json()
    if not data.get("ok"):
        log("telegram error", method, data)
    return data


def send(text, buttons=None):
    for i in range(0, max(len(text), 1), 4000):  # Telegram limit is 4096
        chunk = text[i:i + 4000]
        last = i + 4000 >= len(text)
        params = {"chat_id": CHAT_ID, "text": chunk, "disable_web_page_preview": True}
        if buttons and last:
            params["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
        tg("sendMessage", **params)


# ---------------------------------------------------------------- discovery
def unit_props(unit, *props):
    out = subprocess.run(["systemctl", "show", f"{unit}.service", *[f"-p{p}" for p in props]],
                         capture_output=True, text=True).stdout
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def discover():
    """Find the apps on this server: own units in /etc/systemd/system + whatever owns a listening port.
    Returns {unit: {"desc": str, "ports": [(addr, port), ...]}}."""
    units = {}
    for f in sorted(glob.glob("/etc/systemd/system/*.service")):
        name = os.path.basename(f)[:-8]
        if os.path.islink(f) or name.endswith("@") or name.startswith("vps-health"):
            continue
        p = unit_props(name, "Type", "UnitFileState", "Description")
        if p.get("UnitFileState") == "enabled" and p.get("Type") != "oneshot":
            units[name] = {"desc": p.get("Description", ""), "ports": []}
    out = subprocess.run(["ss", "-tlnpH"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        addr, _, port = cols[3].rpartition(":")
        for pid in set(re.findall(r"pid=(\d+)", line)):
            try:
                cg = open(f"/proc/{pid}/cgroup").read()
            except OSError:
                continue
            m = re.search(r"/system\.slice/(?:[^/\n]+/)*([^/\n]+)\.service", cg)
            if not m:
                continue
            name = m.group(1)
            if name in BASE_SERVICES or name.startswith("vps-health"):
                continue
            u = units.setdefault(name, {"desc": unit_props(name, "Description").get("Description", ""), "ports": []})
            if (addr, port) not in u["ports"]:
                u["ports"].append((addr, port))
    for name in EXTRA_WATCH:
        units.setdefault(name, {"desc": unit_props(name, "Description").get("Description", ""), "ports": []})
    return {k: v for k, v in sorted(units.items()) if k not in IGNORE}


def watched():
    return list(discover())


def http_targets(units):
    """unit|host|port for every TCP port of a watched app that might speak HTTP."""
    seen, out = set(), []
    for name, u in units.items():
        for addr, port in u["ports"]:
            if not port.isdigit() or int(port) in NON_HTTP_PORTS or port in seen:
                continue
            seen.add(port)
            host = addr.split("%")[0]
            host = "127.0.0.1" if host in ("0.0.0.0", "*", "[::]", "") else host
            out.append(f"{name}|{host}|{port}")
    return out


def inventory(units):
    lines = []
    for name, u in units.items():
        ports = ", ".join(f"{a}:{p}" for a, p in u["ports"]) or "no listening port"
        lines.append(f"- {name}: {u['desc']} ({ports})")
    return "\n".join(lines) or "- (no custom services found)"


def read_notes():
    try:
        return open(NOTES_FILE).read().strip() or "(none)"
    except OSError:
        return "(none)"


# ---------------------------------------------------------------- collect + analyse
def collect(units):
    env = {**os.environ, "WATCH_SERVICES": " ".join(units), "HTTP_TARGETS": " ".join(http_targets(units))}
    p = subprocess.run(["bash", os.path.join(BASE, "collect.sh")], capture_output=True, text=True,
                       timeout=600, env=env)
    head = "===== DISCOVERED SERVICES =====\n" + inventory(units) + "\n"
    return head + p.stdout + (("\n[collector stderr]\n" + p.stderr[-2000:]) if p.stderr.strip() else "")


def apt_counts():
    """(installable, phased) from a simulated upgrade. `apt list --upgradable` would also count
    phased updates, which apt deliberately holds back, so fixing them is impossible."""
    out = subprocess.run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"],
                         capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}).stdout
    installable = len(re.findall(r"^Inst ", out, re.M))
    m = re.search(r"deferred due to phasing:\n((?:  .*\n)+)", out)
    phased = len(m.group(1).split()) if m else 0
    return installable, phased


def failed_units():
    out = subprocess.run(["systemctl", "--failed", "--no-legend", "--plain"], capture_output=True, text=True).stdout
    return [l.split()[0].removesuffix(".service") for l in out.splitlines() if l.strip()]


def machine():
    mem = int(re.search(r"MemTotal:\s+(\d+)", open("/proc/meminfo").read()).group(1)) / 1048576
    osname = re.search(r'PRETTY_NAME="([^"]+)"', open("/etc/os-release").read())
    return f"{osname.group(1) if osname else 'Linux'}, {os.cpu_count()} vCPU, {mem:.1f} GB RAM"


PROMPT = """You are the sysadmin for the server "{host}" ({machine}).

Apps found on it (auto-discovered systemd services and their listening ports):
{inventory}

Owner's notes about this server. Trust them: anything they describe as intended/accepted is NOT an
issue unless the snapshot shows it changed or got worse.
---
{notes}
---

Below is this week's health snapshot. Diagnose it like an experienced Linux admin:
updates (esp. security), reboot needs, memory/swap/OOM, disk/inodes, failed or flapping services,
log errors worth caring about, SSH brute force, certificates, time sync. Ignore harmless noise.
Updates: only "Installable updates" can be fixed with apt_upgrade. Phased updates are held back by
Ubuntu on purpose and install by themselves later: mention them at most briefly, never as a problem.
In LOCAL HTTP CHECKS any HTTP status (even 404) means the app answers; 000 means no answer.

Return JSON matching the schema:
- summary: a concise overview for Telegram in {lang}, plain text (no markdown), max ~15 short lines,
  emoji bullets, include the key numbers (installable/security updates, RAM, disk %, failed units).
- actions: ONLY fixes from this catalogue that are actually useful now (empty list if nothing to do):
{catalog}
  restart_service needs "arg" = one of: {services}
- reboot_recommended: true only if a reboot is genuinely needed (reboot-required flag, new kernel, or
  apt_upgrade is very likely to require one). Explain in reboot_reason.
- manual: things that need a human decision (config changes, security concerns), with a suggested command.

===== SNAPSHOT =====
{data}
"""


def analyse(data, units):
    services = sorted((set(units) | set(failed_units())) - NEVER_RESTART)
    catalog = "\n".join(f"    {k}: {v[0]}" for k, v in ACTIONS.items())
    prompt = PROMPT.format(host=HOST, machine=machine(), inventory=inventory(units), notes=read_notes(),
                           lang=LANGUAGE, catalog=catalog, services=", ".join(services), data=data)
    env = {"HOME": CLAUDE_HOME, "PATH": "/usr/local/bin:/usr/bin:/bin",
           "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
           "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")}
    env = {k: v for k, v in env.items() if v}
    cmd = ["runuser", "-u", CLAUDE_USER, "--", "env", *[f"{k}={v}" for k, v in env.items()],
           CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL, "--tools", "", "--no-session-persistence",
           "--output-format", "json", "--json-schema", json.dumps(SCHEMA)]
    if os.geteuid() != 0:  # manual test run as the Claude user
        cmd = cmd[cmd.index("env"):]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600, cwd="/tmp")
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-800:]}")
    out = json.loads(p.stdout)
    result = out.get("structured_output")
    if result is None:
        txt = out.get("result", "")
        result = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    return result


def fallback_plan(data, err, units):
    """Simple rules used when Claude is unavailable."""
    acts, manual = [], [f"AI analysis failed: {err}"[:300]]
    m = re.search(r"Security updates \(installable\): (\d+)", data)
    pending = int((re.search(r"Installable updates: (\d+)", data) or [0, 0])[1])
    if pending:
        acts.append({"id": "apt_upgrade", "reason": f"{pending} updates pending"})
    for u in failed_units():
        if u in units:
            acts.append({"id": "restart_service", "arg": u, "reason": "service failed"})
    reboot = "REBOOT REQUIRED =====\nYES" in data
    disk = max([int(x) for x in re.findall(r"\s(\d+)% /", data)] or [0])
    mem = re.search(r"Mem:\s+(\S+)\s+(\S+)", data)
    summary = (f"⚠️ AI unavailable, basic checks only\n"
               f"📦 Installable updates: {pending} (security: {m.group(1) if m else '?'})\n"
               f"💾 Disk /: {disk}%\n🧠 RAM used: {mem.group(2) if mem else '?'} of {mem.group(1) if mem else '?'}\n"
               f"❌ Failed units: {', '.join(failed_units()) or 'none'}\n🔄 Reboot required: {'yes' if reboot else 'no'}")
    return {"status": "attention", "summary": summary, "actions": acts, "reboot_recommended": reboot,
            "reboot_reason": "reboot-required flag is set" if reboot else "", "manual": manual}


def sanitize(actions, units):
    allowed_svc = set(units) | set(failed_units())
    clean, seen = [], set()
    for a in actions or []:
        aid, arg = a.get("id"), (a.get("arg") or "").removesuffix(".service")
        if aid not in ACTIONS:
            continue
        if aid == "restart_service":
            if not re.fullmatch(r"[A-Za-z0-9@._-]+", arg) or arg not in allowed_svc or arg in NEVER_RESTART:
                continue
        else:
            arg = ""
        if (aid, arg) in seen:
            continue
        seen.add((aid, arg))
        clean.append({"id": aid, "arg": arg, "reason": a.get("reason", "")})
    # updates first, service restarts after
    order = list(ACTIONS)
    return sorted(clean, key=lambda a: order.index(a["id"]))


def describe(a):
    label = ACTIONS[a["id"]][0]
    return f"{label} {a['arg']}".strip() + (f" — {a['reason']}" if a.get("reason") else "")


def weekly():
    os.makedirs(HISTORY, exist_ok=True)
    units = discover()
    data = collect(units)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(HISTORY, f"{stamp}.txt"), "w") as f:
        f.write(data)
    try:
        plan = analyse(data, units)
    except Exception as e:  # noqa: BLE001
        log("analysis failed:", e)
        plan = fallback_plan(data, e, units)
    plan["actions"] = sanitize(plan.get("actions"), units)
    if apt_counts()[0] == 0:  # nothing apt would actually install (e.g. only phased updates)
        plan["actions"] = [a for a in plan["actions"] if a["id"] != "apt_upgrade"]
    plan["created"] = time.time()
    with open(os.path.join(HISTORY, f"{stamp}.json"), "w") as f:
        json.dump(plan, f, indent=1)

    icon = {"ok": "🟢", "attention": "🟡", "critical": "🔴"}.get(plan.get("status"), "⚪")
    msg = [f"🖥 {HOST} weekly check-up — {dt.datetime.now():%a %d %b %Y}", f"{icon} Status: {plan.get('status', '?').upper()}",
           "", plan.get("summary", "").strip()]
    if plan["actions"]:
        msg += ["", "🔧 Proposed fixes:"] + [f"{i}. {describe(a)}" for i, a in enumerate(plan["actions"], 1)]
    if plan.get("reboot_recommended"):
        msg += ["", f"🔄 Reboot recommended: {plan.get('reboot_reason', '')}"]
    if plan.get("manual"):
        msg += ["", "👤 Needs your decision:"] + [f"• {m}" for m in plan["manual"]]

    if plan["actions"] or plan.get("reboot_recommended"):
        with open(PENDING, "w") as f:
            json.dump(plan, f)
        msg += ["", 'Reply "fix" to apply, "fix and reboot" to also reboot, or "ignore".']
        buttons = [("✅ Fix", "fix")]
        if plan.get("reboot_recommended"):
            buttons.append(("✅ Fix + reboot", "fixreboot"))
        buttons.append(("❌ Ignore", "ignore"))
        send("\n".join(msg), buttons)
    else:
        if os.path.exists(PENDING):
            os.remove(PENDING)
        msg += ["", "Nothing to fix this week 👍"]
        send("\n".join(msg))


# ---------------------------------------------------------------- applying fixes
LOCK = threading.Lock()


def run_cmd(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env={**os.environ, **APT_ENV})
    return p.returncode, (p.stdout + p.stderr).strip()


def apply_plan(plan):
    lines = ["🔧 Applying fixes…"]
    send(lines[0])
    report = []
    for a in plan["actions"]:
        cmds = ACTIONS[a["id"]][1] or [["systemctl", "restart", a["arg"]]]
        ok, tail = True, ""
        for c in cmds:
            if not shutil.which(c[0]):
                ok, tail = False, f"{c[0]} not installed"
                break
            rc, out = run_cmd(c)
            if rc != 0:
                ok, tail = False, "\n".join(out.splitlines()[-4:])
                break
            tail = out.splitlines()[-1] if out else ""
        report.append(f"{'✅' if ok else '❌'} {ACTIONS[a['id']][0]} {a['arg']}".strip()
                      + (f"\n   {tail[:300]}" if tail and not ok else ""))
    # after-check
    reboot_req = os.path.exists("/var/run/reboot-required")
    upg, phased = apt_counts()
    failed = failed_units()
    svc = ", ".join(f"{s} {'🟢' if subprocess.run(['systemctl', 'is-active', '--quiet', s]).returncode == 0 else '🔴'}"
                    for s in watched())
    report += ["", f"Updates still installable: {upg}" + (f" (+{phased} phased, Ubuntu releases them later)" if phased else ""), f"Failed units: {', '.join(failed) or 'none'}",
               f"Services: {svc}", f"Reboot required now: {'yes' if reboot_req else 'no'}"]
    return "\n".join(report), reboot_req


def ack_updates():
    """Tell Telegram we've consumed everything so a 'reboot' reply is never replayed after boot."""
    off = read_offset()
    if off:
        tg("getUpdates", offset=off, timeout=0)


def do_reboot(reason):
    with open(REBOOT_MARK, "w") as f:
        json.dump({"at": time.time(), "reason": reason}, f)
    ack_updates()
    send("🔄 Rebooting now. I'll message you when the server is back.")
    time.sleep(3)
    subprocess.run(["systemctl", "reboot"])


def handle(cmd):
    """cmd: fix | fixreboot | reboot | noreboot | ignore | check | status | help"""
    plan = None
    if os.path.exists(PENDING):
        with open(PENDING) as f:
            plan = json.load(f)
        if time.time() - plan.get("created", 0) > PENDING_MAX_AGE:
            plan = None

    if cmd in ("fix", "fixreboot"):
        if not plan:
            send("Nothing pending to fix. Send /check to run a check-up now.")
            return
        if not LOCK.acquire(blocking=False):
            send("Already working on it…")
            return
        try:
            os.remove(PENDING)
            result, reboot_req = apply_plan(plan) if plan["actions"] else ("No package/service fixes needed.", True)
            send("Done.\n\n" + result)
            if cmd == "fixreboot":
                do_reboot("approved with fixes")
            elif reboot_req or plan.get("reboot_recommended"):
                send("A reboot is recommended. Reboot now?", [("🔄 Reboot now", "reboot"), ("Later", "noreboot")])
        finally:
            if LOCK.locked():
                LOCK.release()
    elif cmd == "reboot":
        do_reboot("requested via Telegram")
    elif cmd == "noreboot":
        send("OK, no reboot. Send \"reboot\" whenever you want.")
    elif cmd == "ignore":
        if os.path.exists(PENDING):
            os.remove(PENDING)
        send("OK, ignored until next week's check.")
    elif cmd == "check":
        send("🔍 Running a check-up now, takes a minute or two…")
        threading.Thread(target=lambda: subprocess.run([sys.executable, __file__, "weekly"]), daemon=True).start()
    elif cmd == "status":
        up = subprocess.run(["uptime", "-p"], capture_output=True, text=True).stdout.strip()
        mem = subprocess.run(["free", "-h"], capture_output=True, text=True).stdout.splitlines()[1].split()
        disk = subprocess.run(["df", "-h", "--output=pcent", "/"], capture_output=True, text=True).stdout.split()[-1]
        svc = "\n".join(f"{'🟢' if subprocess.run(['systemctl', 'is-active', '--quiet', s]).returncode == 0 else '🔴'} {s}"
                        for s in watched())
        send(f"🖥 {HOST}: {up}\n🧠 RAM {mem[2]} / {mem[1]}\n💾 Disk / {disk}\n"
             f"❌ Failed: {', '.join(failed_units()) or 'none'}\n{svc}"
             + ("\n\n🔧 A fix plan is pending — reply \"fix\"." if plan else ""))
    else:
        send('Commands: "fix", "fix and reboot", "reboot", "ignore", /status, /check')


def parse(text):
    t = text.lower().strip().lstrip("/").split("@")[0]
    words = re.findall(r"[a-zà-ú']+", t)
    has = lambda *stems: any(w.startswith(s) for w in words for s in stems)  # noqa: E731
    negative = bool(words) and (words[0] in ("no", "não", "nao", "nope", "don't", "dont", "not", "never", "later", "depois")
                                or has("ignor", "skip"))
    reboot = has("reboot", "reinici", "restart")
    fix = has("fix", "corrig", "correct", "aplic", "apply") or (words[:1] and words[0] in ("yes", "sim", "ok", "go"))
    if negative:
        return "noreboot" if reboot and not fix else "ignore"
    if fix and reboot:
        return "fixreboot"
    if fix:
        return "fix"
    if reboot:
        return "reboot"
    if has("check", "verific"):
        return "check"
    if has("status"):
        return "status"
    return "help"


def read_offset():
    try:
        return int(open(OFFSET).read().strip())
    except Exception:  # noqa: BLE001
        return 0


def bot():
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(REBOOT_MARK):
        time.sleep(45)  # let services settle
        os.remove(REBOOT_MARK)
        svc = "\n".join(f"{'🟢' if subprocess.run(['systemctl', 'is-active', '--quiet', s]).returncode == 0 else '🔴'} {s}"
                        for s in watched())
        send(f"✅ {HOST} is back online (kernel {os.uname().release}).\n{svc}\n"
             f"Failed units: {', '.join(failed_units()) or 'none'}")
    log("bot started")
    while True:
        try:
            data = tg("getUpdates", offset=read_offset(), timeout=50, allowed_updates=["message", "callback_query"])
        except Exception as e:  # noqa: BLE001
            log("poll error", e)
            time.sleep(10)
            continue
        for u in data.get("result", []):
            with open(OFFSET, "w") as f:  # persist first: never replay a command
                f.write(str(u["update_id"] + 1))
            if "callback_query" in u:
                q = u["callback_query"]
                tg("answerCallbackQuery", callback_query_id=q["id"])
                chat, cmd = str(q["message"]["chat"]["id"]), q.get("data", "")
                tg("editMessageReplyMarkup", chat_id=chat, message_id=q["message"]["message_id"])
            elif "message" in u and "text" in u["message"]:
                chat, cmd = str(u["message"]["chat"]["id"]), parse(u["message"]["text"])
            else:
                continue
            if chat != CHAT_ID:
                log("ignored message from chat", chat)
                continue
            log("command:", cmd)
            try:
                handle(cmd)
            except Exception as e:  # noqa: BLE001
                log("handler error", e)
                send(f"❌ Error: {e}")
        if not data.get("ok"):
            time.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        print(inventory(discover()))
        print("HTTP checks:", " ".join(http_targets(discover())) or "none")
        sys.exit()
    if not TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set (see /etc/vps-health/config.env)")
    mode = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    {"weekly": weekly, "bot": bot, "test": lambda: send(f"👋 vps-health on {HOST} is connected.")}[mode]()
