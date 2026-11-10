#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Disables and removes the VPN kill switch (nftables + systemd).
set -euo pipefail

NFT_DST="/etc/nftables.d/killswitch.nft"
SERVER_NFT="/etc/nftables.d/killswitch-server.nft"
HELPER_DST="/usr/local/sbin/vpn-killswitch-server"
CONF_DIR="/etc/vpn-killswitch"
UNIT_DST="/etc/systemd/system/vpn-killswitch.service"
UNIT_NAME="vpn-killswitch.service"

if [[ $EUID -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

echo "==> Stopping and disabling the service"
if systemctl list-unit-files "$UNIT_NAME" --no-legend 2>/dev/null | grep -q "$UNIT_NAME"; then
    systemctl disable --now "$UNIT_NAME" || true
else
    echo "Service $UNIT_NAME is not installed, skipping."
fi

echo "==> Removing the nftables table (if still present)"
if nft list table inet killswitch >/dev/null 2>&1; then
    nft delete table inet killswitch
fi

echo "==> Removing files"
rm -f "$NFT_DST" "$SERVER_NFT" "$UNIT_DST" "$HELPER_DST"
rm -rf "$CONF_DIR"
rmdir /etc/nftables.d 2>/dev/null || true
systemctl daemon-reload

echo "==> Verification"
if nft list table inet killswitch >/dev/null 2>&1; then
    echo "Table inet killswitch still exists!" >&2
    exit 1
fi
echo "Kill switch disabled and removed. Traffic is no longer restricted."
