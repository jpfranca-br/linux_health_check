#!/usr/bin/env bash
# Collects a health snapshot of this server. Runs as root. Read-only.
export LC_ALL=C
sec() { printf '\n===== %s =====\n' "$1"; }
run() { timeout 60 "$@" 2>&1; }

sec "HOST"
hostname; run uptime; uname -r
echo "Running kernel: $(uname -r)"
echo "Newest installed kernel: $(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's#.*/vmlinuz-##' | sort -V | tail -1)"
grep PRETTY_NAME /etc/os-release

sec "REBOOT REQUIRED"
if [ -f /var/run/reboot-required ]; then echo "YES"; cat /var/run/reboot-required.pkgs 2>/dev/null; else echo "no"; fi

sec "PENDING APT UPDATES"
run apt-get update -qq >/dev/null
run apt list --upgradable 2>/dev/null | grep -v '^Listing'
echo "Security updates: $(apt list --upgradable 2>/dev/null | grep -c -- '-security')"

sec "UNATTENDED-UPGRADES (last lines)"
tail -n 15 /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null || echo "no log"

sec "SERVICES NEEDING RESTART (needrestart)"
run needrestart -b -r l 2>/dev/null | grep -E 'NEEDRESTART-(KSTA|SVC)' || echo "needrestart unavailable"

sec "SNAP REFRESHES"
command -v snap >/dev/null && run snap refresh --list || echo "no snap"

sec "MEMORY"
free -h
echo; echo "Top memory processes:"
ps -eo pid,user,rss,pcpu,etime,comm --sort=-rss | head -12
echo; echo "OOM kills in last 7 days:"
journalctl -k --since "-7d" --no-pager 2>/dev/null | grep -i -E 'out of memory|oom-kill|killed process' | tail -10 || true

sec "CPU / LOAD"
nproc; cat /proc/loadavg
ps -eo pid,user,pcpu,rss,etime,comm --sort=-pcpu | head -8

sec "DISK"
df -h -x tmpfs -x devtmpfs -x squashfs -x overlay
echo; df -i -x tmpfs -x devtmpfs -x squashfs -x overlay
echo; echo "Big dirs:"
du -xsh /var/log /var/cache/apt /var/lib/snapd /tmp /home/* /opt/* 2>/dev/null | sort -rh | head -15
echo; journalctl --disk-usage 2>/dev/null

sec "SYSTEMD FAILED UNITS"
systemctl --failed --no-pager --no-legend || true

sec "KEY SERVICES"
for s in ${WATCH_SERVICES:-}; do
  printf '%-16s active=%s enabled=%s restarts=%s\n' "$s" \
    "$(systemctl is-active "$s" 2>/dev/null)" "$(systemctl is-enabled "$s" 2>/dev/null)" \
    "$(systemctl show -p NRestarts --value "$s" 2>/dev/null)"
done

sec "LOCAL HTTP CHECKS"
[ -z "${HTTP_TARGETS:-}" ] && echo "no HTTP-looking ports found"
for t in ${HTTP_TARGETS:-}; do
  IFS='|' read -r unit host port <<<"$t"
  printf '%-16s %s:%s -> HTTP %s\n' "$unit" "$host" "$port" \
    "$(curl -gs -o /dev/null -m 8 -w '%{http_code}' "http://$host:$port/" 2>/dev/null)"
done

sec "LISTENING PORTS"
ss -tulpn 2>/dev/null | awk 'NR==1 || /LISTEN|UNCONN/' | head -30

sec "FIREWALL"
run ufw status verbose | head -30

sec "SSH SECURITY (last 7 days)"
echo "Failed SSH auth attempts: $(journalctl -u ssh -u sshd --since '-7d' --no-pager 2>/dev/null | grep -c -E 'Failed|Invalid user')"
echo "Top offending IPs:"
journalctl -u ssh -u sshd --since '-7d' --no-pager 2>/dev/null | grep -oE 'from [0-9.]+' | sort | uniq -c | sort -rn | head -5
echo "Successful logins:"
journalctl -u ssh -u sshd --since '-7d' --no-pager 2>/dev/null | grep 'Accepted' | awk '{print $1,$2,$3,$9,$11}' | tail -10
command -v fail2ban-client >/dev/null && run fail2ban-client status || echo "fail2ban not installed"

sec "TLS CERTIFICATES"
if command -v certbot >/dev/null; then run certbot certificates 2>/dev/null | grep -E 'Certificate Name|Expiry' ; else echo "no certbot"; fi

sec "JOURNAL ERRORS (priority err+, last 7 days, deduped)"
journalctl -p err --since "-7d" --no-pager -o cat 2>/dev/null | sed -E 's/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -25

sec "TIME SYNC"
timedatectl 2>/dev/null | grep -E 'synchronized|NTP|Time zone'
