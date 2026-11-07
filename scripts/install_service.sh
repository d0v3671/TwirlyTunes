#!/usr/bin/env bash
# Run ONLY after the --test-outputs, --dry-run, and --run checks pass.
set -euo pipefail
if [[ "$(id -u)" != "0" ]]; then
  echo 'Run: sudo ./install_service.sh' >&2
  exit 1
fi
TARGET_USER="${SUDO_USER:-babydev}"
if [[ "$TARGET_USER" == "root" ]]; then
  echo 'Do not install the service as root; use sudo from the babydev account.' >&2
  exit 1
fi
HOME_DIR="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$DIR" != "$HOME_DIR"/* ]]; then
  echo 'Place this package inside the regular Pi user home folder first.' >&2
  exit 1
fi
if [[ ! -x "$DIR/scripts/start.sh" ]]; then
  echo 'start.sh is missing or not executable' >&2
  exit 1
fi
if [[ ! -x "$DIR/.venv/bin/python" && ! -x "$HOME_DIR/TwirlyTunes_Pi_Deployment/.venv/bin/python" ]]; then
  echo 'Python virtual environment missing; create/install it before enabling auto-start.' >&2
  exit 1
fi
cat > /etc/systemd/system/twirlytunes.service <<UNIT
[Unit]
Description=TwirlyTunes cry pattern detection and timed soothing
Documentation=man:systemd.service(5)
Wants=sound.target
After=sound.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=${TARGET_USER}
Group=${TARGET_USER}
SupplementaryGroups=audio dialout
WorkingDirectory=${DIR}
Environment=HOME=${HOME_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=/bin/bash ${DIR}/scripts/start.sh --run
Restart=on-failure
RestartSec=12
TimeoutStopSec=8
KillSignal=SIGTERM
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now twirlytunes.service
systemctl --no-pager status twirlytunes.service || true
printf '\nTwirlyTunes enabled at boot. Monitor: sudo journalctl -u twirlytunes -f\n'
