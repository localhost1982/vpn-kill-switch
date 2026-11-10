#!/usr/bin/env bash
# Installs and enables the VPN kill switch (nftables + systemd).
# Takes killswitch.nft, vpn-killswitch.service and vpn-killswitch-server.sh
# from the script's directory.
#
# The VPN profile that the kill switch, the tray, the watchdog and the stats
# script follow is chosen here: from an arrow-key menu, or with --vpn.
#
# Usage: ./install.sh                      (menu of the NetworkManager VPN profiles)
#        ./install.sh --vpn NAME_OR_UUID   (non-interactive choice)
#        ./install.sh --server HOST...     (explicit kill switch server hosts)
#        ./install.sh --dry-run ...        (print what would be done, change nothing)
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
TRAY_UNIT="vpn-tray.service"

DRY_RUN=0
VPN_ARG=""
SERVERS=()

# Profiles found in NetworkManager (parallel arrays, sorted by name).
P_NAMES=()
P_UUIDS=()
P_TYPES=()
P_HOSTS=() # space-separated server hosts of the profile; "" when unknown

# Result of the selection.
SEL_NAME=""
SEL_UUID=""
SEL_TYPE=""

# The user whose tray/watchdog settings are written.
TARGET_USER=""
TARGET_GROUP=""
HOME_DIR=""
SETTINGS_FILE=""

usage() {
    cat <<USAGE
Usage: ${0##*/} [--vpn NAME_OR_UUID] [--server HOST]... [--dry-run] [-h]

Without options the script shows a menu of the NetworkManager VPN profiles
(Up/Down and Enter) and sets up the kill switch for the chosen one. The same
profile is saved for the tray, the watchdog and the stats script.

  --vpn NAME_OR_UUID  choose the VPN profile without the menu (exact name or UUID)
  --server HOST       VPN server host name or IPv4 address that stays reachable
                      outside the tunnel (repeatable). Overrides the servers
                      found in the profile. Without --vpn no profile is saved.
  --dry-run           do not use sudo and change nothing: show the menu (or use
                      --vpn), resolve the servers and print every command with
                      a "[dry-run]" prefix
  -h, --help          show this help
USAGE
}

die() {
    local code="$1"
    shift
    echo "$*" >&2
    exit "$code"
}

# Runs a command, or only prints it in --dry-run mode.
run() {
    if ((DRY_RUN)); then
        printf '[dry-run]'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# VPN profiles
# ---------------------------------------------------------------------------

# Fills P_NAMES/P_UUIDS/P_TYPES with the "vpn" and "wireguard" profiles, sorted
# by name. Parsing note: in terse mode (-t) nmcli escapes ':' and '\' inside a
# value as '\:' and '\\'. TYPE and UUID never contain either, so they are
# requested first and NAME last: "read" then puts everything after the second
# ':' (the whole name, colons included) into the last variable, and the name is
# unescaped afterwards.
list_profiles() {
    local type uuid name line
    local -a rows=()
    P_NAMES=()
    P_UUIDS=()
    P_TYPES=()
    P_HOSTS=()
    while IFS=: read -r type uuid name; do
        [[ $type == vpn || $type == wireguard ]] || continue
        name=${name//\\\\/$'\001'}
        name=${name//\\:/:}
        name=${name//$'\001'/\\}
        rows+=("$name"$'\t'"$uuid"$'\t'"$type")
    done < <(nmcli -t -f TYPE,UUID,NAME connection show 2>/dev/null || true)

    ((${#rows[@]})) || return 0
    while IFS= read -r line; do
        IFS=$'\t' read -r name uuid type <<<"$line"
        P_NAMES+=("$name")
        P_UUIDS+=("$uuid")
        P_TYPES+=("$type")
    done < <(printf '%s\n' "${rows[@]}" | LC_ALL=C.UTF-8 sort -f -t $'\t' -k1,1 -k2,2)
}

# "host", "host:port", "host:port:proto", "[v6]:port" or a bare IPv6 literal -> host
# (mirrors host_from_endpoint in tray/vpn-tray.py).
host_from_endpoint() {
    local e="$1" colons
    e="${e#"${e%%[![:space:]]*}"}"
    e="${e%"${e##*[![:space:]]}"}"
    if [[ $e == \[*\]* ]]; then
        e="${e#\[}"
        printf '%s\n' "${e%%\]*}"
        return
    fi
    colons="${e//[^:]/}"
    # A bare IPv6 literal: its colons are not a port separator. (An OpenVPN
    # "host:port:proto" entry always has non-hex letters in the proto.)
    if ((${#colons} >= 2)) && [[ $e =~ ^[0-9A-Fa-f:.]+$ ]]; then
        printf '%s\n' "$e"
        return
    fi
    printf '%s\n' "${e%%:*}"
}

# Prints the server hosts of a "vpn" profile, one per line (mirrors the "vpn"
# branch of vpn_server_hosts in tray/vpn-tray.py): the first non-empty of
# gateway, remote, address. "remote" (OpenVPN) may hold several entries.
vpn_hosts() {
    local uuid="$1" data line key val entry
    local -A kv=()
    # -e no: do not escape ':' in the value (nmcli would print "host\:1194").
    data="$(nmcli -e no -g vpn.data connection show uuid "$uuid" 2>/dev/null || true)"
    [[ -n $data ]] || return 0
    # The value is "key = value, key = value, ..."; split before every ", key = ".
    while IFS= read -r line; do
        [[ $line == *' = '* ]] || continue
        key="${line%% = *}"
        val="${line#* = }"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        kv["$key"]="$val"
    done < <(sed 's/, \([A-Za-z0-9_.-]\+\) = /\n\1 = /g' <<<"$data")

    for key in gateway remote address; do
        val="${kv[$key]:-}"
        [[ -n $val ]] || continue
        if [[ $key == remote ]]; then
            for entry in ${val//,/ }; do
                host_from_endpoint "$entry"
            done
        else
            host_from_endpoint "$val"
        fi
        return 0
    done
}

# Prints the peer endpoint hosts of a "wireguard" profile, one per line.
# nmcli does not show WireGuard peers at all ("nmcli connection show" lists only
# the interface settings), so the peers are read from NetworkManager over D-Bus
# (Settings.Connection.GetSettings; the endpoint is not a secret). Fallback:
# the "endpoint=" lines of the profile's keyfile (readable by root only).
wireguard_hosts() {
    local uuid="$1" path settings endpoint f
    local -a endpoints=()
    if command -v gdbus >/dev/null 2>&1; then
        path="$(gdbus call --system --dest org.freedesktop.NetworkManager \
            --object-path /org/freedesktop/NetworkManager/Settings \
            --method org.freedesktop.NetworkManager.Settings.GetConnectionByUuid "$uuid" 2>/dev/null |
            sed -n "s/.*'\(\/[^']*\)'.*/\1/p")" || path=""
        if [[ -n $path ]]; then
            settings="$(gdbus call --system --dest org.freedesktop.NetworkManager \
                --object-path "$path" \
                --method org.freedesktop.NetworkManager.Settings.Connection.GetSettings 2>/dev/null || true)"
            # "'endpoint': <'host:port'>" for every peer.
            while IFS= read -r endpoint; do
                [[ -n $endpoint ]] && endpoints+=("$endpoint")
            done < <(grep -o "'endpoint': <'[^']*'>" <<<"$settings" | sed "s/^'endpoint': <'//; s/'>\$//")
        fi
    fi
    if ((${#endpoints[@]} == 0)); then
        for f in /etc/NetworkManager/system-connections/* /run/NetworkManager/system-connections/*; do
            [[ -f $f && -r $f ]] || continue
            grep -qix "uuid=$uuid" "$f" 2>/dev/null || continue
            while IFS= read -r endpoint; do
                [[ -n $endpoint ]] && endpoints+=("$endpoint")
            done < <(sed -n 's/^[[:space:]]*endpoint[[:space:]]*=[[:space:]]*//p' "$f")
        done
    fi
    for endpoint in "${endpoints[@]}"; do
        host_from_endpoint "$endpoint"
    done
}

# Prints the deduplicated server hosts of profile index $1 on one line (space-separated).
profile_hosts() {
    local i="$1" h out=() seen=" "
    local -a all=()
    case "${P_TYPES[i]}" in
        vpn) mapfile -t all < <(vpn_hosts "${P_UUIDS[i]}") ;;
        wireguard) mapfile -t all < <(wireguard_hosts "${P_UUIDS[i]}") ;;
    esac
    for h in "${all[@]}"; do
        [[ -n $h && $seen != *" $h "* ]] || continue
        out+=("$h")
        seen+="$h "
    done
    echo "${out[*]}"
}

# ---------------------------------------------------------------------------
# The arrow-key menu
# ---------------------------------------------------------------------------

MENU_SEL=0
MENU_REV=""
MENU_SGR0=""
MENU_COLS=80
MENU_STTY=""
MENU_ACTIVE=0
MENU_LABELS=()

menu_restore() {
    if ((MENU_ACTIVE)); then
        MENU_ACTIVE=0
        printf '%s' "${MENU_CNORM:-$'\033[?25h'}"
        [[ -n $MENU_STTY ]] && stty "$MENU_STTY" 2>/dev/null || true
    fi
    return 0
}

menu_abort() {
    menu_restore
    echo
    echo "Aborted." >&2
    exit 130
}

menu_draw() {
    local i line max=$((MENU_COLS - 4))
    ((max >= 10)) || max=10
    for ((i = 0; i < ${#MENU_LABELS[@]}; i++)); do
        line="${MENU_LABELS[i]:0:max}"
        if ((i == MENU_SEL)); then
            if [[ -n $MENU_REV ]]; then
                printf '\r\033[K  %s%s%s\n' "$MENU_REV" "$line" "$MENU_SGR0"
            else
                printf '\r\033[K> %s\n' "$line"
            fi
        else
            printf '\r\033[K  %s\n' "$line"
        fi
    done
}

# Shows the menu for MENU_LABELS, starting at index $1. Up/Down (or k/j) move,
# Enter confirms (the index goes to MENU_SEL), Esc/q/Ctrl-C abort with exit 130.
menu_select() {
    local n=${#MENU_LABELS[@]} key rest
    MENU_SEL="$1"
    MENU_REV=""
    MENU_SGR0=""
    MENU_CNORM=""
    local civis=$'\033[?25l'
    if command -v tput >/dev/null 2>&1; then
        MENU_REV="$(tput rev 2>/dev/null || true)"
        MENU_SGR0="$(tput sgr0 2>/dev/null || true)"
        [[ -n $MENU_SGR0 ]] || MENU_REV=""
        civis="$(tput civis 2>/dev/null || true)"
        MENU_CNORM="$(tput cnorm 2>/dev/null || true)"
        MENU_COLS="$(tput cols 2>/dev/null || echo 80)"
    fi
    [[ -n $civis ]] || civis=$'\033[?25l'
    [[ -n $MENU_CNORM ]] || MENU_CNORM=$'\033[?25h'
    [[ $MENU_COLS =~ ^[0-9]+$ ]] || MENU_COLS=80

    MENU_STTY="$(stty -g 2>/dev/null || true)"
    MENU_ACTIVE=1
    trap menu_abort INT TERM HUP
    trap menu_restore EXIT

    echo "Select the VPN profile for the kill switch, the tray and the watchdog:"
    printf '%s' "$civis"
    menu_draw
    while true; do
        IFS= read -rsn1 key || menu_abort
        case "$key" in
            '') break ;; # Enter (newline)
            $'\r') break ;;
            $'\003' | q | Q) menu_abort ;;
            k | K) MENU_SEL=$(((MENU_SEL + n - 1) % n)) ;;
            j | J) MENU_SEL=$(((MENU_SEL + 1) % n)) ;;
            $'\033')
                rest=""
                IFS= read -rsn2 -t 0.2 rest || true
                case "$rest" in
                    '[A' | 'OA') MENU_SEL=$(((MENU_SEL + n - 1) % n)) ;;
                    '[B' | 'OB') MENU_SEL=$(((MENU_SEL + 1) % n)) ;;
                    '') menu_abort ;; # a bare Esc
                    *) ;;             # another escape sequence: ignore
                esac
                ;;
            *) continue ;;
        esac
        printf '\033[%dA' "$n"
        menu_draw
    done
    trap - INT TERM HUP EXIT
    menu_restore
}

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

# UUID saved in the target user's settings file, or "".
saved_uuid() {
    [[ -n $SETTINGS_FILE && -r $SETTINGS_FILE ]] || return 0
    sed -n 's/^[[:space:]]*uuid[[:space:]]*=[[:space:]]*//p' "$SETTINGS_FILE" | head -n 1 | tr -d '[:space:]'
}

# Sets SEL_NAME/SEL_UUID/SEL_TYPE and SEL_INDEX from --vpn.
select_by_arg() {
    local want="$1" i hit=-1 count=0
    for i in "${!P_UUIDS[@]}"; do
        if [[ ${P_UUIDS[i],,} == "${want,,}" ]]; then
            hit=$i
            count=1
            break
        fi
    done
    if ((hit < 0)); then
        for i in "${!P_NAMES[@]}"; do
            if [[ ${P_NAMES[i]} == "$want" ]]; then
                hit=$i
                count=$((count + 1))
            fi
        done
    fi
    ((hit >= 0)) || die 1 "VPN profile not found: $want (known VPN profiles: ${P_NAMES[*]})"
    ((count == 1)) || die 1 "Several VPN profiles are named $want; use the UUID."
    SEL_INDEX=$hit
}

choose_profile() {
    local i start=0 cur host_label
    if [[ -n $VPN_ARG ]]; then
        select_by_arg "$VPN_ARG"
        return
    fi
    cur="$(saved_uuid)"
    MENU_LABELS=()
    for i in "${!P_NAMES[@]}"; do
        P_HOSTS[i]="$(profile_hosts "$i")"
        host_label="${P_TYPES[i]}"
        [[ -n ${P_HOSTS[i]} ]] && host_label+=", ${P_HOSTS[i]// /, }"
        MENU_LABELS+=("${P_NAMES[i]}  ($host_label)")
        [[ -n $cur && ${P_UUIDS[i]} == "$cur" ]] && start=$i
    done
    menu_select "$start"
    SEL_INDEX=$MENU_SEL
}

# ---------------------------------------------------------------------------
# Settings file and tray
# ---------------------------------------------------------------------------

resolve_target_user() {
    TARGET_USER="${SUDO_USER:-}"
    # --dry-run runs without sudo: the invoking user is the target.
    if [[ -z $TARGET_USER && $EUID -ne 0 ]]; then
        TARGET_USER="$(id -un)"
    fi
    if [[ -z $TARGET_USER || $TARGET_USER == root ]]; then
        TARGET_USER=""
        return 0
    fi
    HOME_DIR="$(getent passwd "$TARGET_USER" | cut -d: -f6 || true)"
    [[ -n $HOME_DIR ]] || die 1 "Cannot find the home directory of $TARGET_USER."
    TARGET_GROUP="$(id -gn "$TARGET_USER")"
    SETTINGS_FILE="$HOME_DIR/.config/vpn-killswitch/settings.conf"
}

# Prints the settings file content with the `uuid` line of the [vpn] section set to $1. If the file exists
# everything else is kept as it is (the [notify] section the tray writes, other keys, comments): the uuid
# line is replaced, or added at the end of [vpn], or the [vpn] section is appended. Otherwise: a new file.
render_settings() {
    local uuid="$1"
    if [[ -n $SETTINGS_FILE && -r $SETTINGS_FILE ]]; then
        U="$uuid" awk '
            function flush() { printf "%s", held; held = "" }
            /^\[[^]]*\]/ {                      # a section header
                if (in_vpn && !done) { print "uuid = " ENVIRON["U"]; done = 1; flush() }
                in_vpn = ($0 ~ /^\[vpn\][ \t]*$/)
                if (in_vpn) seen = 1
                print; next
            }
            in_vpn && !done && /^[ \t]*$/ { held = held $0 "\n"; next }   # blank lines stay after the new uuid line
            in_vpn && /^[ \t]*uuid[ \t]*[=:]/ {
                if (!done) { flush(); print "uuid = " ENVIRON["U"]; done = 1 }
                next                                # a duplicate uuid line is dropped
            }
            { flush(); print }
            END {
                if (in_vpn && !done) { print "uuid = " ENVIRON["U"]; flush() }
                if (!seen) { if (NR > 0) print ""; print "[vpn]"; print "uuid = " ENVIRON["U"] }
            }
        ' "$SETTINGS_FILE"
    else
        printf '[vpn]\nuuid = %s\n' "$uuid"
    fi
}

write_settings() {
    local uuid="$1" dir tmp line
    dir="$(dirname "$SETTINGS_FILE")"
    if ((DRY_RUN)); then
        [[ -d $HOME_DIR/.config ]] || run install -d -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0700 "$HOME_DIR/.config"
        run install -d -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0700 "$dir"
        echo "[dry-run] write $SETTINGS_FILE (owner $TARGET_USER:$TARGET_GROUP, mode 0644):"
        while IFS= read -r line || [[ -n $line ]]; do
            printf '[dry-run]   %s\n' "$line"
        done < <(render_settings "$uuid")
        return
    fi
    # install -d chowns only the last component: create ~/.config first if missing.
    [[ -d $HOME_DIR/.config ]] || install -d -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0700 "$HOME_DIR/.config"
    install -d -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0700 "$dir"
    tmp="$(mktemp "$dir/.settings.XXXXXX")"
    if ! render_settings "$uuid" >"$tmp"; then
        rm -f -- "$tmp"
        die 1 "Cannot write $SETTINGS_FILE."
    fi
    chown "$TARGET_USER:$TARGET_GROUP" "$tmp"
    chmod 0644 "$tmp"
    mv -f -- "$tmp" "$SETTINGS_FILE"
    echo "Settings written to $SETTINGS_FILE"
}

# Restarts the tray (if it runs) so that it reads the new profile.
restart_tray() {
    local uid
    uid="$(id -u "$TARGET_USER")"
    local -a ctl=(runuser -u "$TARGET_USER" -- env "XDG_RUNTIME_DIR=/run/user/$uid" systemctl --user)
    if ((DRY_RUN)); then
        run "${ctl[@]}" try-restart "$TRAY_UNIT"
        return
    fi
    if "${ctl[@]}" is-active --quiet "$TRAY_UNIT" 2>/dev/null; then
        if "${ctl[@]}" try-restart "$TRAY_UNIT" 2>/dev/null; then
            echo "Tray restarted to pick up the new profile."
        else
            echo "Could not restart the tray; run: systemctl --user restart $TRAY_UNIT"
        fi
    else
        echo "The tray is not running; it will use the new profile when it starts."
    fi
}

# ---------------------------------------------------------------------------

main() {
    local a f

    # Help needs no root. --dry-run needs none either.
    for a in "$@"; do
        case "$a" in
            -h | --help)
                usage
                exit 0
                ;;
            --dry-run) DRY_RUN=1 ;;
        esac
    done

    # Re-exec under sudo before parsing: the loop below consumes "$@".
    if ((!DRY_RUN)) && [[ $EUID -ne 0 ]]; then
        exec sudo -- "$0" "$@"
    fi

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
            --vpn)
                if (($# < 2)) || [[ -z "$2" ]]; then
                    echo "--vpn needs a profile name or UUID" >&2
                    usage >&2
                    exit 2
                fi
                VPN_ARG="$2"
                shift 2
                ;;
            --vpn=*)
                if [[ -z "${1#--vpn=}" ]]; then
                    echo "--vpn needs a profile name or UUID" >&2
                    exit 2
                fi
                VPN_ARG="${1#--vpn=}"
                shift
                ;;
            --dry-run)
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

    resolve_target_user

    # --- choose the VPN profile ---
    local want_profile=0 hosts_str=""
    local -a HOSTS=()
    if [[ -n $VPN_ARG ]]; then
        want_profile=1
    elif ((${#SERVERS[@]} == 0)); then
        if [[ -t 0 && -t 1 ]]; then
            want_profile=1
        else
            die 1 "No terminal for the profile menu; run with --vpn NAME or --server HOST."
        fi
    fi

    if ((want_profile)); then
        list_profiles
        ((${#P_NAMES[@]})) || die 1 "No VPN profiles found in NetworkManager; create one first or run with --server HOST."
        SEL_INDEX=0
        choose_profile
        SEL_NAME="${P_NAMES[SEL_INDEX]}"
        SEL_UUID="${P_UUIDS[SEL_INDEX]}"
        SEL_TYPE="${P_TYPES[SEL_INDEX]}"
        echo "Selected VPN profile: $SEL_NAME ($SEL_UUID)"

        if ((${#SERVERS[@]} > 0)); then
            HOSTS=("${SERVERS[@]}") # explicit hosts win
        else
            hosts_str="$(profile_hosts "$SEL_INDEX")"
            [[ -n $hosts_str ]] || die 1 "Cannot determine the VPN server of profile $SEL_NAME; run with --server HOST."
            read -r -a HOSTS <<<"$hosts_str"
        fi
    else
        HOSTS=("${SERVERS[@]}")
    fi

    if ((want_profile)) && [[ -z $TARGET_USER ]]; then
        echo "Note: no regular user found (not started via sudo from a user account);" >&2
        echo "      the tray/watchdog settings file will not be written." >&2
    fi

    # The rules include the generated server file, so the syntax can be checked
    # up front only when that file already exists. Otherwise the helper checks it.
    if [[ -f "$SERVER_NFT" ]]; then
        echo "==> Checking rules syntax"
        run nft -c -f "$NFT_SRC"
    fi

    echo "==> Copying files"
    run install -D -m 0644 "$NFT_SRC" "$NFT_DST"
    run install -D -m 0644 "$UNIT_SRC" "$UNIT_DST"
    run install -D -m 0755 "$HELPER_SRC" "$HELPER_DST"

    # The unit file was just copied: let systemd see it before the helper reloads the unit.
    run systemctl daemon-reload

    if ((${#HOSTS[@]} > 0)); then
        echo "==> Configuring the VPN server (resolving and checking rules syntax)"
        if ((DRY_RUN)); then
            run "$HELPER_DST" set "${HOSTS[@]}"
        elif ! "$HELPER_DST" set "${HOSTS[@]}"; then
            # Do not leave rules behind that cannot be loaded without the server file.
            if [[ ! -f "$SERVER_NFT" ]]; then
                rm -f "$NFT_DST" "$UNIT_DST"
            fi
            exit 1
        fi
    fi

    if ((want_profile)) && [[ -n $TARGET_USER ]]; then
        echo "==> Saving the VPN profile for the tray, the watchdog and the stats script"
        write_settings "$SEL_UUID"
        restart_tray
    fi

    echo "==> Enabling the service"
    run systemctl daemon-reload
    run systemctl enable --now "$UNIT_NAME"
    # Already running (re-install): apply the freshly copied rules atomically.
    run systemctl reload "$UNIT_NAME"

    echo "==> Status"
    if ((DRY_RUN)); then
        run systemctl --no-pager --lines=0 status "$UNIT_NAME"
        run nft list table inet killswitch
        echo
        echo "[dry-run] nothing was changed."
        return 0
    fi
    systemctl --no-pager --lines=0 status "$UNIT_NAME" || true
    echo
    nft list table inet killswitch

    echo
    echo "Kill switch enabled. Internet is now reachable only through the VPN tunnel."
    echo "To disable: $SRC_DIR/uninstall.sh"
}

# Run only when executed, not when sourced (so the functions can be tested).
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
