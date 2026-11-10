#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Install the VPN watchdog: enable the user unit.
# Run: ./watchdog/install-watchdog.sh (as a regular user, without sudo).

set -euo pipefail

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/vpn-watchdog.service"

if (( EUID == 0 )); then
    echo "Run as a regular user, not root: the unit is installed into ~/.config." >&2
    exit 1
fi

if ! command -v nmcli >/dev/null 2>&1; then
    echo "nmcli not found (NetworkManager is required)." >&2
    exit 1
fi

# An older fixed-profile watchdog would fight with this one.
if systemctl --user is-enabled old-vpn-watchdog.service >/dev/null 2>&1 \
    || systemctl --user is-active old-vpn-watchdog.service >/dev/null 2>&1; then
    echo "An older watchdog old-vpn-watchdog.service is enabled; it keeps reconnecting a fixed profile. Disable it: systemctl --user disable --now old-vpn-watchdog.service" >&2
fi

# The unit with the script path substituted in.
ESC=${DIR//\\/\\\\}
ESC=${ESC//&/\\&}
ESC=${ESC//|/\\|}
mkdir -p "$UNIT_DIR"
sed "s|@WATCHDOG_DIR@|$ESC|" "$DIR/vpn-watchdog.service" >"$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now vpn-watchdog.service

echo "vpn-watchdog.service: $(systemctl --user is-active vpn-watchdog.service || true)"
