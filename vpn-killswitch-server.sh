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
# Routing exception: in mode "servers" `apply` also keeps one policy-routing rule
# per saved VPN server address, `ip -4 rule add pref 8999 to ADDRESS/32 lookup main`,
# so that the VPN's own packets never enter the Hiddify (sing-box) tunnel, whose
# rules start at priority 9000. In mode "hiddify" (or with no mode) no such rule
# exists. `apply` synchronises these rules first, even when the nftables table is
# not loaded.
#
# Usage:
#   vpn-killswitch-server set HOST [HOST...]   resolve hosts, mode "servers"
#   vpn-killswitch-server set-hiddify          mode "hiddify"
#   vpn-killswitch-server apply                load the saved state into the rules
#   vpn-killswitch-server refresh              re-resolve the saved hosts / re-apply
#
# When DNS is unavailable, `set` and `refresh` fall back to the saved addresses:
#   - `set` does so only if the requested hosts are the same set as the saved hosts
#     (order does not matter) and the saved addresses file is valid and not empty; the
#     saved files are kept, the mode becomes "servers" and the rules are applied.
#     Otherwise it fails with exit 3 and writes nothing.
#   - `refresh` (mode "servers") keeps the saved addresses and applies them; with no
#     saved addresses it fails with exit 3.
#   IPv4 literals never need DNS.
#   vpn-killswitch-server show                 show the mode, the nftables rules and the routing rules
#   vpn-killswitch-server clear-routes         remove the routing rules (pref 8999); used on package removal
#   vpn-killswitch-server -h                   this help
#
# Exit codes: 0 ok, 1 general error, 2 invalid usage/host,
#             3 cannot resolve a host, 4 nft rejected the new rules.
#
# Testing without root: VKS_PREFIX=/some/dir is prepended to all /etc paths,
# VKS_CGROUP_ROOT replaces /sys/fs/cgroup, VKS_DRY_RUN=1 skips the root check
# and systemctl and prints the nft script and the `ip` commands (prefixed with
# "[dry-run] ") instead of running them,
# VKS_RESOLVE_FAIL="host1 host2" makes the name lookup of the listed hosts fail
# (simulates unavailable DNS; honoured only for testing; IPv4 literals are not affected).
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

# Priority of the routing exception rules. Must stay below 9000, the first priority
# that sing-box/Hiddify uses for its own rules.
ROUTE_PREF=8999

HOST_RE='^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'
IPV4_RE='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

usage() {
    cat <<USAGE
Usage:
  ${0##*/} set HOST [HOST...]   resolve the VPN server host(s) and allow them (mode "servers");
                                  if DNS fails and the hosts are the saved ones, the saved addresses are used
  ${0##*/} set-hiddify          allow the traffic of $HIDDIFY_UNIT (mode "hiddify")
  ${0##*/} apply                load the saved state into the running kill switch (no DNS)
  ${0##*/} refresh              re-resolve the saved hosts (or re-apply) and update the kill switch;
                                  if DNS fails, the saved addresses are kept
  ${0##*/} show                 show the mode and the current rules (nftables and routing)
  ${0##*/} clear-routes         remove the routing rules (pref $ROUTE_PREF) that keep the VPN server traffic out of the Hiddify tunnel
  ${0##*/} -h                   show this help

HOST is a hostname or an IPv4 address. Must be run as root.
In mode "servers", set/apply/refresh also keep "ip -4 rule pref $ROUTE_PREF to ADDRESS/32 lookup main"
for every saved VPN server address; in mode "hiddify" these rules are removed.
VKS_DRY_RUN=1 prints the nft script and the ip commands instead of running them.
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

# True when the test hook VKS_RESOLVE_FAIL lists the host.
resolve_forced_to_fail() {
    local -a list=()
    local f
    read -r -a list <<<"${VKS_RESOLVE_FAIL:-}" || true
    for f in "${list[@]}"; do
        [[ "$f" == "$1" ]] && return 0
    done
    return 1
}

# Prints the IPv4 addresses of a host, one per line, deduplicated.
resolve_host() {
    local h="$1"
    if [[ "$h" =~ $IPV4_RE ]]; then
        normalize_ipv4 "$h"
        return
    fi
    if resolve_forced_to_fail "$h"; then
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

# Runs `ip` (or only prints the command in a dry run).
ip_cmd() {
    if [[ "$DRY_RUN" == 1 ]]; then
        echo "[dry-run] ip $*"
    else
        ip "$@"
    fi
}

# Removes every policy-routing rule with our priority. Never fails.
delete_route_rules() {
    local i
    if [[ "$DRY_RUN" == 1 ]]; then
        ip_cmd -4 rule del pref "$ROUTE_PREF"
        return 0
    fi
    if ! command -v ip >/dev/null 2>&1; then
        echo "warning: ip not found; the routing rules (pref $ROUTE_PREF) were not touched." >&2
        return 0
    fi
    for ((i = 0; i < 64; i++)); do
        ip -4 rule del pref "$ROUTE_PREF" >/dev/null 2>&1 || break
    done
    return 0
}

# Makes the rules with priority $ROUTE_PREF equal to the wanted set: one
# `to ADDRESS/32 lookup main` per saved address in mode "servers", none otherwise.
# Idempotent; problems are warnings on stderr, never a failure.
sync_route_rules() {
    local mode a n
    local -a addrs=()
    if [[ "$DRY_RUN" != 1 ]] && ! command -v ip >/dev/null 2>&1; then
        echo "warning: ip not found; the routing rules (pref $ROUTE_PREF) were not updated." >&2
        return 0
    fi
    # An unreadable mode is reported by build_script; leave the rules as they are.
    if ! mode="$(read_mode 2>/dev/null)"; then
        echo "warning: invalid mode in $MODE_FILE; the routing rules (pref $ROUTE_PREF) were not updated." >&2
        return 0
    fi
    if [[ "$mode" == servers && -f "$ADDR_FILE" ]]; then
        while IFS= read -r a || [[ -n "$a" ]]; do
            a="${a//[$'\r\t ']/}"
            [[ -n "$a" ]] || continue
            if ! [[ "$a" =~ $IPV4_RE ]] || ! n="$(normalize_ipv4 "$a")"; then
                echo "warning: ignoring invalid address in $ADDR_FILE: $a" >&2
                continue
            fi
            addrs+=("$n")
        done <"$ADDR_FILE"
    fi
    delete_route_rules
    if ((${#addrs[@]})); then
        while IFS= read -r n; do
            ip_cmd -4 rule add pref "$ROUTE_PREF" to "$n/32" lookup main ||
                echo "warning: could not add the routing rule for $n." >&2
        done < <(printf '%s\n' "${addrs[@]}" | awk '!seen[$0]++')
    fi
    return 0
}

cmd_clear_routes() {
    delete_route_rules
}

cmd_apply() {
    local script
    sync_route_rules
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

# Resolves all hosts into the globals ALL_IPS (array), SUMMARY and FAILED_HOSTS
# (the hosts that gave no address). Dies before anything is written only if a
# host is invalid; the caller decides what to do about the hosts that failed.
resolve_all() {
    local h ip joined
    local -a host_ips
    ALL_IPS=()
    FAILED_HOSTS=()
    SUMMARY=""
    for h in "$@"; do
        validate_host "$h"
    done
    for h in "$@"; do
        mapfile -t host_ips < <(resolve_host "$h")
        if ((${#host_ips[@]} == 0)); then
            FAILED_HOSTS+=("$h")
            continue
        fi
        for ip in "${host_ips[@]}"; do
            ALL_IPS+=("$ip")
        done
        joined="$(printf '%s, ' "${host_ips[@]}")"
        SUMMARY+="${SUMMARY:+; }$h (${joined%, })"
    done
}

# Prints the saved host names (blank lines and whitespace dropped), one per line.
read_saved_hosts() {
    local h
    [[ -s "$HOSTS_FILE" ]] || return 0
    while IFS= read -r h || [[ -n "$h" ]]; do
        h="${h//[$'\r\t ']/}"
        [[ -n "$h" ]] && echo "$h"
    done <"$HOSTS_FILE"
    return 0
}

# Succeeds when the saved addresses are valid and not empty; then SAVED_SUMMARY
# holds them as "IP, IP". A damaged file counts as "no saved addresses".
load_saved_addresses() {
    local out
    SAVED_SUMMARY=""
    out="$(read_addresses 2>/dev/null)" || return 1
    [[ -n "$out" ]] || return 1
    SAVED_SUMMARY="$(awk '!seen[$0]++' <<<"$out" | paste -sd, - | sed 's/,/, /g')"
}

# Dies with exit 3 for the first host that did not resolve.
die_cannot_resolve() {
    die 3 "Cannot resolve ${FAILED_HOSTS[0]}"
}

# The failed hosts as "HOST, HOST".
failed_hosts_text() {
    local IFS=,
    local text="${FAILED_HOSTS[*]}"
    echo "${text//,/, }"
}

write_addresses() {
    printf '%s\n' "${ALL_IPS[@]}" | awk '!seen[$0]++' | write_atomic "$ADDR_FILE"
}

cmd_set() {
    local requested saved hosts_text
    (($# >= 1)) || { usage >&2; exit 2; }
    resolve_all "$@"
    if ((${#FAILED_HOSTS[@]})); then
        # DNS fallback: only for the same set of hosts as the saved one, and only
        # with usable saved addresses. Nothing is written in that case.
        requested="$(printf '%s\n' "$@" | LC_ALL=C sort -u)"
        saved="$(read_saved_hosts | LC_ALL=C sort -u)"
        if [[ -z "$saved" || "$requested" != "$saved" ]] || ! load_saved_addresses; then
            die_cannot_resolve
        fi
        echo "DNS lookup failed for $(failed_hosts_text); using the saved addresses." >&2
        hosts_text="$(printf '%s, ' "$@")"
        SUMMARY="${hosts_text%, } (saved: $SAVED_SUMMARY)"
    else
        printf '%s\n' "$@" | write_atomic "$HOSTS_FILE"
        write_addresses
    fi
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
    local mode
    mode="$(read_mode)"
    case "$mode" in
        servers)
            mapfile -t hosts < <(read_saved_hosts)
            ((${#hosts[@]})) || die 1 "No saved hosts in $HOSTS_FILE; run: ${0##*/} set HOST..."
            resolve_all "${hosts[@]}"
            if ((${#FAILED_HOSTS[@]})); then
                # DNS fallback: keep the saved addresses (the state stays consistent).
                load_saved_addresses || die_cannot_resolve
                echo "DNS lookup failed for $(failed_hosts_text); keeping the saved addresses." >&2
                SUMMARY="$(printf '%s, ' "${hosts[@]}")"
                SUMMARY="${SUMMARY%, } (saved: $SAVED_SUMMARY)"
            else
                write_addresses
            fi
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
    echo "Routing rules (pref $ROUTE_PREF):"
    if [[ "$DRY_RUN" == 1 ]]; then
        echo "  (not queried in a dry run)"
    else
        local rules=""
        if command -v ip >/dev/null 2>&1; then
            rules="$(ip -4 rule show pref "$ROUTE_PREF" 2>/dev/null || true)"
        fi
        if [[ -n "$rules" ]]; then sed 's/^/  /' <<<"$rules"; else echo "  none"; fi
    fi
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
        set | set-hiddify | apply | refresh | show | clear-routes) ;;
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
        apply | refresh | show | clear-routes)
            (($# == 0)) || { usage >&2; exit 2; }
            case "$cmd" in
                clear-routes) cmd_clear_routes ;;
                apply) cmd_apply ;;
                refresh) cmd_refresh ;;
                show) cmd_show ;;
            esac
            ;;
    esac
}

main "$@"
