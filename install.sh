#!/usr/bin/env bash
# Installs and enables the VPN kill switch (nftables + systemd).
# Takes killswitch.nft and vpn-killswitch.service from the script's directory.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NFT_SRC="$SRC_DIR/killswitch.nft"
UNIT_SRC="$SRC_DIR/vpn-killswitch.service"

NFT_DST="/etc/nftables.d/killswitch.nft"
UNIT_DST="/etc/systemd/system/vpn-killswitch.service"
UNIT_NAME="vpn-killswitch.service"

if [[ $EUID -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

for f in "$NFT_SRC" "$UNIT_SRC"; do
    if [[ ! -f "$f" ]]; then
        echo "File not found: $f" >&2
        exit 1
    fi
done

echo "==> Checking rules syntax"
nft -c -f "$NFT_SRC"

echo "==> Copying files"
install -D -m 0644 "$NFT_SRC" "$NFT_DST"
install -D -m 0644 "$UNIT_SRC" "$UNIT_DST"

echo "==> Enabling the service"
systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

echo "==> Status"
systemctl --no-pager --lines=0 status "$UNIT_NAME" || true
echo
nft list table inet killswitch

echo
echo "Kill switch enabled. Internet is now reachable only through VPN-L2TP (ppp*)."
echo "To disable: $SRC_DIR/uninstall.sh"
