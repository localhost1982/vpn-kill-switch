#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Manages the VPN server addresses allowed by the kill switch.
# Installed as /usr/local/sbin/vpn-killswitch-server (run as root, e.g. via sudo).
#
# Usage:
#   vpn-killswitch-server set HOST [HOST...]   resolve hosts, update the rules
#   vpn-killswitch-server refresh              re-resolve the saved hosts
#   vpn-killswitch-server show                 show saved hosts and current rules
#   vpn-killswitch-server -h                   this help
#
# Exit codes: 0 ok, 1 general error, 2 invalid usage/host,
#             3 cannot resolve a host, 4 nft rejected the new rules,
#             5 the service reload failed.
#
# Testing without root: VKS_PREFIX=/some/dir is prepended to all /etc paths,
# VKS_DRY_RUN=1 skips the root check, `nft -c` and `systemctl`.
set -euo pipefail

export PATH="/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"

PREFIX="${VKS_PREFIX:-}"
DRY_RUN="${VKS_DRY_RUN:-0}"

CONF_DIR="$PREFIX/etc/vpn-killswitch"
HOSTS_FILE="$CONF_DIR/servers"
SERVER_NFT="$PREFIX/etc/nftables.d/killswitch-server.nft"
MAIN_NFT="$PREFIX/etc/nftables.d/killswitch.nft"
UNIT="vpn-killswitch.service"

HOST_RE='^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'
IPV4_RE='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

usage() {
    cat <<USAGE
Usage:
  ${0##*/} set HOST [HOST...]   resolve the VPN server host(s) and update the kill switch
  ${0##*/} refresh              re-resolve the saved hosts and update the kill switch
  ${0##*/} show                 show the saved hosts and the current define line
  ${0##*/} -h                   show this help

HOST is a hostname or an IPv4 address. Must be run as root.
USAGE
}

die() {
    local code="$1"
    shift
    echo "$*" >&2
    exit "$code"
}

TMP_FILES=()
cleanup() {
    if ((${#TMP_FILES[@]})); then
        rm -rf -- "${TMP_FILES[@]}"
    fi
}
trap cleanup EXIT

# Normalizes an IPv4 literal (drops leading zeros); fails if an octet is > 255.
normalize_ipv4() {
    local -a o
    local x out=()
    IFS=. read -r -a o <<<"$1"
    for x in "${o[@]}"; do
        x=$((10#$x))
        ((x <= 255)) || return 1
        out+=("$x")
    done
    local IFS=.
    echo "${out[*]}"
}

# Validates one host argument. This is the security boundary: the sudoers rule
# lets the tray pass arbitrary arguments.
validate_host() {
    local h="$1"
    if [[ "$h" == -* ]] || ! [[ "$h" =~ $HOST_RE ]]; then
        die 2 "Invalid host: $h"
    fi
    if [[ "$h" =~ $IPV4_RE ]]; then
        normalize_ipv4 "$h" >/dev/null || die 2 "Invalid host: $h"
    fi
}

# Prints the IPv4 addresses of a host, one per line, deduplicated.
resolve_host() {
    local h="$1"
    if [[ "$h" =~ $IPV4_RE ]]; then
        normalize_ipv4 "$h"
        return
    fi
    { getent ahosts "$h" 2>/dev/null || true; } |
        awk '$2=="STREAM" && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print $1}' |
        awk '!seen[$0]++'
}

# Writes stdin to $1 atomically (temp file in the same directory + mv).
write_atomic() {
    local dst="$1" dir tmp
    dir="$(dirname "$dst")"
    mkdir -p "$dir"
    tmp="$(mktemp "$dir/.${dst##*/}.XXXXXX")"
    TMP_FILES+=("$tmp")
    cat >"$tmp"
    chmod 0644 "$tmp"
    mv -f -- "$tmp" "$dst"
}

run() {
    if [[ "$DRY_RUN" == 1 ]]; then
        echo "[dry-run] would run: $*"
    else
        "$@"
    fi
}

# Resolves all hosts and updates the files and the running rules.
apply_hosts() {
    local h ip summary="" all_ips=() ips line
    local -a host_ips

    for h in "$@"; do
        validate_host "$h"
    done

    # Resolve everything first: nothing is written if any host fails.
    for h in "$@"; do
        mapfile -t host_ips < <(resolve_host "$h")
        if ((${#host_ips[@]} == 0)); then
            die 3 "Cannot resolve $h"
        fi
        for ip in "${host_ips[@]}"; do
            all_ips+=("$ip")
        done
        local joined
        joined="$(printf '%s, ' "${host_ips[@]}")"
        summary+="${summary:+; }$h (${joined%, })"
    done

    ips="$(printf '%s\n' "${all_ips[@]}" | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g')"
    line="define vpn_servers = { $ips }"

    # Back up the current state to restore it if nft rejects the new rules.
    local backup_dir had_server=0 had_hosts=0
    backup_dir="$(mktemp -d)"
    TMP_FILES+=("$backup_dir")
    if [[ -f "$SERVER_NFT" ]]; then
        cp -p -- "$SERVER_NFT" "$backup_dir/server.nft"
        had_server=1
    fi
    if [[ -f "$HOSTS_FILE" ]]; then
        cp -p -- "$HOSTS_FILE" "$backup_dir/servers"
        had_hosts=1
    fi

    printf '%s\n' "$@" | write_atomic "$HOSTS_FILE"
    printf '%s\n' "$line" | write_atomic "$SERVER_NFT"

    if [[ "$DRY_RUN" == 1 ]]; then
        echo "[dry-run] would run: nft -c -f $MAIN_NFT"
    elif ! nft -c -f "$MAIN_NFT"; then
        if ((had_server)); then
            write_atomic "$SERVER_NFT" <"$backup_dir/server.nft"
        else
            rm -f -- "$SERVER_NFT"
        fi
        if ((had_hosts)); then
            write_atomic "$HOSTS_FILE" <"$backup_dir/servers"
        else
            rm -f -- "$HOSTS_FILE"
        fi
        rmdir -- "$backup_dir" 2>/dev/null || true
        die 4 "nft rejected the new rules; previous configuration restored."
    fi
    rm -f -- "$backup_dir/server.nft" "$backup_dir/servers"
    rmdir -- "$backup_dir" 2>/dev/null || true

    if [[ "$DRY_RUN" == 1 ]]; then
        echo "[dry-run] would run: systemctl is-active --quiet $UNIT && systemctl reload $UNIT"
    elif systemctl is-active --quiet "$UNIT"; then
        systemctl reload "$UNIT" || die 5 "Failed to reload $UNIT."
    else
        echo "Kill switch is not active; files updated, rules will apply on next start."
    fi

    echo "VPN servers: $summary"
}

cmd_set() {
    (($# >= 1)) || { usage >&2; exit 2; }
    apply_hosts "$@"
}

cmd_refresh() {
    local -a hosts=()
    local h
    [[ -s "$HOSTS_FILE" ]] || die 1 "No saved hosts in $HOSTS_FILE; run: ${0##*/} set HOST..."
    while IFS= read -r h || [[ -n "$h" ]]; do
        h="${h//[$'\r\t ']/}"
        [[ -n "$h" ]] && hosts+=("$h")
    done <"$HOSTS_FILE"
    ((${#hosts[@]})) || die 1 "No saved hosts in $HOSTS_FILE; run: ${0##*/} set HOST..."
    apply_hosts "${hosts[@]}"
}

cmd_show() {
    echo "Hosts ($HOSTS_FILE):"
    if [[ -s "$HOSTS_FILE" ]]; then
        sed 's/^/  /' "$HOSTS_FILE"
    else
        echo "  not configured"
    fi
    echo "Rules ($SERVER_NFT):"
    if [[ -f "$SERVER_NFT" ]] && grep -q '^define vpn_servers' "$SERVER_NFT"; then
        grep '^define vpn_servers' "$SERVER_NFT" | sed 's/^/  /'
    else
        echo "  not configured"
    fi
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        -h | --help | help)
            usage
            exit 0
            ;;
        set | refresh | show) ;;
        "")
            usage >&2
            exit 2
            ;;
        *)
            echo "Unknown command: $cmd" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift

    if [[ "$DRY_RUN" != 1 && $EUID -ne 0 ]]; then
        die 1 "This command must be run as root (use sudo)."
    fi

    case "$cmd" in
        set) cmd_set "$@" ;;
        refresh) cmd_refresh ;;
        show) cmd_show ;;
    esac
}

main "$@"
