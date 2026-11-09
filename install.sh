#!/usr/bin/env bash
# Installs and enables the VPN kill switch (nftables + systemd).
# Takes killswitch.nft, vpn-killswitch.service and vpn-killswitch-server.sh
# from the script's directory.
#
# Usage: ./install.sh --server HOST [--server HOST...]
#        ./install.sh            (re-install; the VPN server is already configured)
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NFT_SRC="$SRC_DIR/killswitch.nft"
UNIT_SRC="$SRC_DIR/vpn-killswitch.service"
HELPER_SRC="$SRC_DIR/vpn-killswitch-server.sh"

NFT_DST="/etc/nftables.d/killswitch.nft"
SERVER_NFT="/etc/nftables.d/killswitch-server.nft"
UNIT_DST="/etc/systemd/system/vpn-killswitch.service"
HELPER_DST="/usr/local/sbin/vpn-killswitch-server"
UNIT_NAME="vpn-killswitch.service"

usage() {
    cat <<USAGE
Usage: ${0##*/} [--server HOST]... [-h]

  --server HOST   VPN gateway host name or IPv4 address that stays reachable
                  outside the tunnel (repeatable). Required on the first
                  install; the VPN profile itself is chosen in the tray app.
  -h, --help      show this help
USAGE
}

# Help needs no root.
for a in "$@"; do
    if [[ $a == -h || $a == --help ]]; then
        usage
        exit 0
    fi
done

# Re-exec under sudo before parsing: the loop below consumes "$@".
if [[ $EUID -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

SERVERS=()
while (($#)); do
    case "$1" in
        --server)
            if (($# < 2)) || [[ "$2" == -* ]]; then
                echo "--server needs a host argument" >&2
                usage >&2
                exit 2
            fi
            SERVERS+=("$2")
            shift 2
            ;;
        --server=*)
            if [[ -z "${1#--server=}" ]]; then
                echo "--server needs a host argument" >&2
                exit 2
            fi
            SERVERS+=("${1#--server=}")
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done


for f in "$NFT_SRC" "$UNIT_SRC" "$HELPER_SRC"; do
    if [[ ! -f "$f" ]]; then
        echo "File not found: $f" >&2
        exit 1
    fi
done

if ((${#SERVERS[@]} == 0)) && [[ ! -f "$SERVER_NFT" ]]; then
    echo "No VPN server configured yet. Run: ./install.sh --server <vpn-gateway-host>" >&2
    exit 1
fi

# The rules include the generated server file, so the syntax can be checked
# up front only when that file already exists. Otherwise the helper checks it.
if [[ -f "$SERVER_NFT" ]]; then
    echo "==> Checking rules syntax"
    nft -c -f "$NFT_SRC"
fi

echo "==> Copying files"
install -D -m 0644 "$NFT_SRC" "$NFT_DST"
install -D -m 0644 "$UNIT_SRC" "$UNIT_DST"
install -D -m 0755 "$HELPER_SRC" "$HELPER_DST"

# The unit file was just copied: let systemd see it before the helper reloads the unit.
systemctl daemon-reload

if ((${#SERVERS[@]} > 0)); then
    echo "==> Configuring the VPN server (resolving and checking rules syntax)"
    if ! "$HELPER_DST" set "${SERVERS[@]}"; then
        # Do not leave rules behind that cannot be loaded without the server file.
        if [[ ! -f "$SERVER_NFT" ]]; then
            rm -f "$NFT_DST" "$UNIT_DST"
        fi
        exit 1
    fi
fi

echo "==> Enabling the service"
systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"
# Already running (re-install): apply the freshly copied rules atomically.
systemctl reload "$UNIT_NAME"

echo "==> Status"
systemctl --no-pager --lines=0 status "$UNIT_NAME" || true
echo
nft list table inet killswitch

echo
echo "Kill switch enabled. Internet is now reachable only through the VPN tunnel."
echo "To disable: $SRC_DIR/uninstall.sh"
