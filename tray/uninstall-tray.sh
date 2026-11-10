#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Remove the VPN tray icon: stop and delete the user unit.
# Run: ./tray/uninstall-tray.sh (as a regular user, without sudo).

set -euo pipefail

UNIT="$HOME/.config/systemd/user/vpn-tray.service"

systemctl --user disable --now vpn-tray.service >/dev/null 2>&1 || true
rm -f "$UNIT"
systemctl --user daemon-reload

echo "Tray icon removed: vpn-tray.service stopped and deleted."
