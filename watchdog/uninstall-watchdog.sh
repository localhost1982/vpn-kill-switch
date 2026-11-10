#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Remove the VPN watchdog: stop and delete the user unit (the VPN connection stays up).
# Run: ./watchdog/uninstall-watchdog.sh (as a regular user, without sudo).

set -euo pipefail

UNIT="$HOME/.config/systemd/user/vpn-watchdog.service"

systemctl --user disable --now vpn-watchdog.service >/dev/null 2>&1 || true
rm -f "$UNIT"
systemctl --user daemon-reload

echo "Watchdog removed: vpn-watchdog.service stopped and deleted."
