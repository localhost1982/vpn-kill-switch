#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Manages what the kill switch allows outside the VPN tunnel: either the VPN
# server addresses (NetworkManager VPN profile) or the traffic of the Hiddify
# service. Installed as /usr/sbin/vpn-killswitch-server (run as root, e.g. via
# sudo).
#
# The exceptions live in the nftables chain `inet killswitch vpn_allow`, which
# is empty in killswitch.nft and filled by `apply`. The state is kept in
# /etc/vpn-killswitch/: mode (servers|hiddify), servers (host names) and
# addresses (IPv4 addresses).
#
# Usage:
#   vpn-killswitch-server set HOST [HOST...]   resolve hosts, mode "servers"
#   vpn-killswitch-server set-hiddify          mode "hiddify"
#   vpn-killswitch-server apply                load the saved state into the rules
#   vpn-killswitch-server refresh              re-resolve the saved hosts / re-apply
#   vpn-killswitch-server show                 show the mode and the current rules
#   vpn-killswitch-server -h                   this help
#
# Exit codes: 0 ok, 1 general error, 2 invalid usage/host,
#             3 cannot resolve a host, 4 nft rejected the new rules.
#
# Testing without root: VKS_PREFIX=/some/dir is prepended to all /etc paths,
# VKS_CGROUP_ROOT replaces /sys/fs/cgroup, VKS_DRY_RUN=1 skips the root check
# and systemctl and prints the nft script instead of loading it.
set -euo pipefail
shopt -s inherit_errexit

export PATH="/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"

PREFIX="${VKS_PREFIX:-}"
DRY_RUN="${VKS_DRY_RUN:-0}"

CONF_DIR="$PREFIX/etc/vpn-killswitch"
MODE_FILE="$CONF_DIR/mode"
HOSTS_FILE="$CONF_DIR/servers"
ADDR_FILE="$CONF_DIR/addresses"
UNIT="vpn-killswitch.service"
HOOK_UNIT="vpn-killswitch-hiddify.service"
HIDDIFY_UNIT="HiddifyAdminBG.service"
HIDDIFY_CGROUP="system.slice/HiddifyAdminBG.service"
CGROUP_ROOT="${VKS_CGROUP_ROOT:-/sys/fs/cgroup}"

HOST_RE='^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'
IPV4_RE='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

usage() {
    cat <<USAGE
Usage:
  ${0##*/} set HOST [HOST...]   resolve the VPN server host(s) and allow them (mode "servers")
  ${0##*/} set-hiddify          allow the traffic of $HIDDIFY_UNIT (mode "hiddify")
  ${0##*/} apply                load the saved state into the running kill switch (no DNS)
  ${0##*/} refresh              re-resolve the saved hosts (or re-apply) and update the kill switch
  ${0##*/} show                 show the mode and the current rules
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

# True when systemd is the running init (systemctl is usable).
systemd_running() {
    [[ -d "$PREFIX/run/systemd/system" ]]
}

# systemctl calls whose failure is not an error.
systemctl_soft() {
    if [[ "$DRY_RUN" == 1 ]]; then
        echo "[dry-run] would run: systemctl $*"
    elif systemd_running; then
        systemctl "$@" >/dev/null 2>&1 || true
    fi
}

hiddify_cgroup_exists() {
    [[ -d "$CGROUP_ROOT/$HIDDIFY_CGROUP" ]]
}

# Prints the saved mode (servers|hiddify) or nothing when none is configured.
read_mode() {
    local m=""
    [[ -f "$MODE_FILE" ]] || return 0
    IFS= read -r m <"$MODE_FILE" || true
    m="${m//[$'\r\t ']/}"
    case "$m" in
        servers | hiddify) echo "$m" ;;
        "") ;;
        *) die 1 "Invalid mode in $MODE_FILE: $m" ;;
    esac
}

# Prints the saved addresses (validated and normalized), one per line.
read_addresses() {
    local a n
    [[ -f "$ADDR_FILE" ]] || return 0
    while IFS= read -r a || [[ -n "$a" ]]; do
        a="${a//[$'\r\t ']/}"
        [[ -n "$a" ]] || continue
        [[ "$a" =~ $IPV4_RE ]] || die 1 "Invalid address in $ADDR_FILE: $a"
        n="$(normalize_ipv4 "$a")" || die 1 "Invalid address in $ADDR_FILE: $a"
        echo "$n"
    done <"$ADDR_FILE"
}

# Prints the nft script that rebuilds the chain vpn_allow from the saved state.
build_script() {
    local mode ip="" addr_out
    local -a addrs=()
    mode="$(read_mode)"
    echo "flush chain inet killswitch vpn_allow"
    case "$mode" in
        servers)
            # Not a process substitution: a die inside must fail the whole script.
            addr_out="$(read_addresses)"
            if [[ -n "$addr_out" ]]; then
                mapfile -t addrs <<<"$addr_out"
                ip="$(printf '%s\n' "${addrs[@]}" | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g')"
                echo "add rule inet killswitch vpn_allow ip daddr { $ip } accept comment \"VPN server\""
            fi
            ;;
        hiddify)
            if hiddify_cgroup_exists; then
                echo "add rule inet killswitch vpn_allow socket cgroupv2 level 2 \"$HIDDIFY_CGROUP\" accept comment \"Hiddify service\""
            fi
            ;;
    esac
}

cmd_apply() {
    local script
    script="$(build_script)"
    if [[ "$DRY_RUN" == 1 ]]; then
        sed 's/^/[dry-run] /' <<<"$script"
        return 0
    fi
    if ! nft list chain inet killswitch vpn_allow >/dev/null 2>&1; then
        echo "Kill switch is not active; nothing to apply."
        return 0
    fi
    # One transaction: the chain is never seen half-filled and a failure
    # leaves the previous rules in place.
    nft -f - <<<"$script" || die 4 "nft rejected the new rules; the previous rules are unchanged."
}

# Resolves all hosts into the globals ALL_IPS (array) and SUMMARY.
# Dies before anything is written if a host is invalid or cannot be resolved.
resolve_all() {
    local h ip joined
    local -a host_ips
    ALL_IPS=()
    SUMMARY=""
    for h in "$@"; do
        validate_host "$h"
    done
    for h in "$@"; do
        mapfile -t host_ips < <(resolve_host "$h")
        if ((${#host_ips[@]} == 0)); then
            die 3 "Cannot resolve $h"
        fi
        for ip in "${host_ips[@]}"; do
            ALL_IPS+=("$ip")
        done
        joined="$(printf '%s, ' "${host_ips[@]}")"
        SUMMARY+="${SUMMARY:+; }$h (${joined%, })"
    done
}

write_addresses() {
    printf '%s\n' "${ALL_IPS[@]}" | awk '!seen[$0]++' | write_atomic "$ADDR_FILE"
}

cmd_set() {
    (($# >= 1)) || { usage >&2; exit 2; }
    resolve_all "$@"
    printf '%s\n' "$@" | write_atomic "$HOSTS_FILE"
    write_addresses
    echo servers | write_atomic "$MODE_FILE"
    systemctl_soft disable --now "$HOOK_UNIT"
    cmd_apply
    echo "VPN servers: $SUMMARY"
}

cmd_set_hiddify() {
    (($# == 0)) || { usage >&2; exit 2; }
    echo hiddify | write_atomic "$MODE_FILE"
    systemctl_soft enable "$HOOK_UNIT"
    cmd_apply
    if hiddify_cgroup_exists; then
        echo "Kill switch mode: Hiddify (traffic of $HIDDIFY_UNIT is allowed)"
    else
        echo "Kill switch mode: Hiddify (the Hiddify service is not running; its traffic will be allowed when it starts)"
    fi
}

cmd_refresh() {
    local -a hosts=()
    local h mode
    mode="$(read_mode)"
    case "$mode" in
        servers)
            if [[ -s "$HOSTS_FILE" ]]; then
                while IFS= read -r h || [[ -n "$h" ]]; do
                    h="${h//[$'\r\t ']/}"
                    [[ -n "$h" ]] && hosts+=("$h")
                done <"$HOSTS_FILE"
            fi
            ((${#hosts[@]})) || die 1 "No saved hosts in $HOSTS_FILE; run: ${0##*/} set HOST..."
            resolve_all "${hosts[@]}"
            write_addresses
            cmd_apply
            echo "VPN servers: $SUMMARY"
            ;;
        hiddify)
            cmd_apply
            ;;
        *)
            die 1 "No VPN server configured yet."
            ;;
    esac
}

cmd_show() {
    local mode
    mode="$(read_mode)"
    case "$mode" in
        servers)
            echo "Mode: servers"
            echo "Hosts ($HOSTS_FILE):"
            if [[ -s "$HOSTS_FILE" ]]; then sed 's/^/  /' "$HOSTS_FILE"; else echo "  none"; fi
            echo "Addresses ($ADDR_FILE):"
            if [[ -s "$ADDR_FILE" ]]; then sed 's/^/  /' "$ADDR_FILE"; else echo "  none"; fi
            ;;
        hiddify)
            echo "Mode: Hiddify ($HIDDIFY_UNIT)"
            if hiddify_cgroup_exists; then
                echo "Service cgroup: present ($CGROUP_ROOT/$HIDDIFY_CGROUP)"
            else
                echo "Service cgroup: absent (the service is not running)"
            fi
            ;;
        *)
            echo "Mode: not configured"
            ;;
    esac
    if [[ "$DRY_RUN" == 1 ]]; then
        return 0
    fi
    echo "Rules (inet killswitch vpn_allow):"
    if nft list chain inet killswitch vpn_allow >/dev/null 2>&1; then
        nft list chain inet killswitch vpn_allow | sed 's/^/  /'
    else
        echo "  kill switch is not active"
    fi
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        -h | --help | help)
            usage
            exit 0
            ;;
        set | set-hiddify | apply | refresh | show) ;;
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
        set-hiddify) cmd_set_hiddify "$@" ;;
        apply | refresh | show)
            (($# == 0)) || { usage >&2; exit 2; }
            case "$cmd" in
                apply) cmd_apply ;;
                refresh) cmd_refresh ;;
                show) cmd_show ;;
            esac
            ;;
    esac
}

main "$@"
