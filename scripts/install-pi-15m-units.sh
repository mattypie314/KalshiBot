#!/bin/sh
# Copy KalshiBot15 systemd units onto the Pi and reload.
# Safe while live: does not enable a disabled timer; restarts only if already enabled.
# Does not touch kalshi-hourly.timer.
#
#   sudo /home/KalshiBot15/scripts/install-pi-15m-units.sh
set -e
if command -v readlink >/dev/null 2>&1; then
  here=$(readlink -f "$0" 2>/dev/null || printf '%s\n' "$0")
else
  here="$0"
fi
scripts_dir=$(CDPATH= cd -- "$(dirname "$here")" && pwd)
root=$(CDPATH= cd -- "$scripts_dir/.." && pwd)
service="$root/scripts/kalshi-15m.service"
timer="$root/scripts/kalshi-15m.timer"
if [ ! -f "$service" ] || [ ! -f "$timer" ]; then
  echo "install-pi-15m-units: missing $service or $timer" >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "install-pi-15m-units: rerun with sudo" >&2
  exit 1
fi
cp "$service" /etc/systemd/system/kalshi-15m.service
cp "$timer" /etc/systemd/system/kalshi-15m.timer
systemctl daemon-reload
if systemctl is-enabled kalshi-15m.timer >/dev/null 2>&1; then
  systemctl restart kalshi-15m.timer
  echo "Recopied units and restarted kalshi-15m.timer (was already enabled)."
else
  echo "Recopied units. Timer is still disabled (as shipped)."
fi
echo "OnCalendar + AccuracySec:"
systemctl cat kalshi-15m.timer | grep -E 'OnCalendar|AccuracySec|FragmentPath'
echo "Leave kalshi-hourly.timer alone."
echo "Next: systemctl list-timers kalshi-15m.timer --no-pager"
