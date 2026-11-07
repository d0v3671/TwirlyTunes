#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" != "0" ]]; then
  echo 'Run: sudo ./uninstall_service.sh' >&2
  exit 1
fi
systemctl disable --now twirlytunes.service 2>/dev/null || true
rm -f /etc/systemd/system/twirlytunes.service
systemctl daemon-reload
systemctl reset-failed twirlytunes.service 2>/dev/null || true
echo 'Autostart disabled and service file removed; model/files are unchanged.'
