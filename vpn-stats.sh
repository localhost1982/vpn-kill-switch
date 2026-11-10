#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# VPN drop and internet-loss statistics for the last N hours.
# The VPN profile comes from the tray Settings file (or --vpn NAME).
# Data source: the systemd journal (journalctl). Output: Markdown tables.
# Usage: ./vpn-stats.sh [hours] [--events] [--vpn NAME]   (details: ./vpn-stats.sh --help)

set -euo pipefail

# The decimal point in awk output must not depend on the locale.
export LC_NUMERIC=C

DEFAULT_HOURS=1
SETTINGS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/vpn-killswitch/settings.conf"

# --- Journal line patterns (the part after "unit[pid]: ") -------------------
# The same set is used both in grep (counting) and in awk (via ENVIRON).
# RE_START, RE_UP and RE_FAIL depend on the VPN profile name and are set by
# build_name_patterns() once the name is known.
export RE_NOAGENT="secrets: failed to request VPN secrets"
export RE_LCP="pppd\\[[0-9]+\\]: No response to [0-9]+ echo-requests"
export RE_TERM="pppd\\[[0-9]+\\]: Terminating on signal"
export RE_CONN="pppd\\[[0-9]+\\]: Connection terminated\\."
export RE_WIFI_RESET="secondaries -> failed \\(reason 'secondary-connection-failed'"
export RE_WIFI_DOWN="device \\(wl[^)]*\\): state change: activated -> "
export RE_SLEEP="kernel: PM: suspend entry"
export RE_WAKE="kernel: PM: suspend exit"
export RE_NM_STOP="systemd\\[1\\]: Stopped NetworkManager\\.service"
export RE_BOOT="kernel: Linux version "
export RE_HELLO="message=recv message_type=avpMsgTypeHello"

usage() {
    cat <<'USAGE'
Usage: vpn-stats.sh [hours] [--events] [--vpn NAME] [-h|--help]

VPN drop and internet-loss statistics for the last N hours
(Markdown tables, data source: the systemd journal).

The VPN profile is the one selected in the tray Settings window, stored in
${XDG_CONFIG_HOME:-~/.config}/vpn-killswitch/settings.conf ([vpn] uuid = ...).
Use --vpn NAME to look at another profile instead (e.g. its history).
Drop causes (LCP, on command, other) are detected for pppd-based profiles
(L2TP, PPTP); for other VPN types only connection events are counted.

  hours      period in hours, default 1. Both a decimal point and a decimal comma
             are accepted (1.5, 1,5). Step is 0.5 h: the value is rounded
             half up to the nearest half hour (0.7 -> 0.5, 0.75 -> 1, 1.2 -> 1,
             1.3 -> 1.5). If the result after rounding is 0 or the value is not
             a number, 1 h is used.
  --vpn NAME use the NetworkManager VPN profile NAME instead of the one from the
             settings file
  --events   after the tables, print a chronology of events (connections, drops,
             suspends, NetworkManager restarts, boots, connection failures)
  -h, --help show this help

Examples:
  ./vpn-stats.sh            # the last hour
  ./vpn-stats.sh 0.5        # the last half hour
  ./vpn-stats.sh 1,5        # an hour and a half
  ./vpn-stats.sh 3 --events # three hours with the chronology
  ./vpn-stats.sh 6 --vpn "Work VPN"  # another profile

sudo is not needed, except for the row with the kill switch blocked-packet counter:
without passwordless sudo it will show "n/a (sudo required)". The script never asks for a password.
USAGE
}

# --- Parsing the "hours" argument ----------------------------------------------
# Result: globals HALFHOURS (number of half hours, >= 1) and NOTE (a note or empty).
parse_hours() {
    local raw=$1 norm
    NOTE=""
    norm=${raw//,/.}
    if [[ ! $norm =~ ^([0-9]+\.?[0-9]*|\.[0-9]+)$ ]]; then
        NOTE="Argument \"${raw}\" is not a number, using the default of ${DEFAULT_HOURS} h."
        HALFHOURS=$((DEFAULT_HOURS * 2))
        return
    fi
    # Round half up to the nearest 0.5 h: int(h*2 + 0.5).
    HALFHOURS=$(awk -v h="$norm" 'BEGIN { printf "%d", h * 2 + 0.5 }')
    if (( HALFHOURS == 0 )); then
        NOTE="Value ${raw} is zero after rounding, using the default of ${DEFAULT_HOURS} h."
        HALFHOURS=$((DEFAULT_HOURS * 2))
    fi
}

# Period label: 0.5 h, 1 h, 1.5 h, 2 h (without ".0").
period_label() {
    local hh=$1
    if (( hh % 2 == 0 )); then
        printf '%d h' $((hh / 2))
    else
        printf '%d.5 h' $((hh / 2))
    fi
}

# Number of journal lines matching a regular expression (always a single number).
count() {
    grep -cE -- "$1" "$JOURNAL" || true
}

# Seconds -> "12s" / "3m 05s" / "1h 02m".
fmt_duration() {
    local s=$1
    if (( s < 60 )); then
        printf '%ds' "$s"
    elif (( s < 3600 )); then
        printf '%dm %02ds' $((s / 60)) $((s % 60))
    else
        printf '%dh %02dm' $((s / 3600)) $(((s % 3600) / 60))
    fi
}

# Epoch -> local time.
fmt_time() { date -d "@$1" +%H:%M:%S; }
fmt_datetime() { date -d "@$1" '+%Y-%m-%d %H:%M'; }

# Determine VPN_NAME and VPN_UUID: --vpn NAME (VPN_NAME_ARG) overrides the settings file.
resolve_vpn() {
    VPN_NAME=""
    VPN_UUID=""
    if [[ -n $VPN_NAME_ARG ]]; then
        VPN_NAME=$VPN_NAME_ARG
        VPN_UUID=$(nmcli -g connection.uuid connection show "$VPN_NAME" 2>/dev/null | head -n1 || true)
    else
        if [[ -r $SETTINGS_FILE ]]; then
            VPN_UUID=$(sed -n 's/^[[:space:]]*uuid[[:space:]]*=[[:space:]]*//p' "$SETTINGS_FILE" | head -n1 || true)
            VPN_UUID=${VPN_UUID%%[[:space:]]*}
        fi
        if [[ -n $VPN_UUID ]]; then
            VPN_NAME=$(nmcli -g connection.id connection show "$VPN_UUID" 2>/dev/null | head -n1 || true)
        fi
    fi
    if [[ -z $VPN_NAME ]]; then
        echo "No VPN profile selected. Choose one in the tray Settings window or pass --vpn NAME." >&2
        exit 1
    fi
}

# Journal patterns that contain the profile name (escaped for ERE).
build_name_patterns() {
    local name_re
    name_re=$(printf '%s' "$VPN_NAME" | sed 's/[][\.*^$+?(){}|/\\]/\\&/g')
    export RE_START="vpn\\[[^]]*\"${name_re}\"\\]: starting "
    export RE_UP="policy: set '${name_re}' \\([a-z0-9]+\\) as default for IPv4"
    export RE_FAIL="vpn\\[[^]]*\"${name_re}\"\\]: failed to connect"
}

# Whether the VPN is connected right now (0 = yes): by UUID when known, else by name.
vpn_active() {
    local list
    if [[ -n $VPN_UUID ]]; then
        list=$(nmcli -t -f UUID connection show --active 2>/dev/null || true)
        grep -Fxq -- "$VPN_UUID" <<<"$list"
    else
        list=$(nmcli -t -f NAME connection show --active 2>/dev/null || true)
        grep -Fxq -- "$VPN_NAME" <<<"$list"
    fi
}

# systemd unit state -> "active" / "inactive" / "unavailable" (if there is no answer).
unit_state() {
    local out
    out=$("$@" 2>/dev/null || true)
    case $out in
        active) echo "active" ;;
        "")     echo "unavailable" ;;
        *)      echo "inactive" ;;
    esac
}

# Number of packets blocked by the kill switch; without passwordless sudo -> "n/a (sudo required)".
killswitch_drops() {
    local out pkts=""
    if out=$(sudo -n nft list chain inet killswitch output 2>/dev/null); then
        pkts=$(grep -E 'drop' <<<"$out" | grep -oE 'counter packets [0-9]+' | head -n1 | awk '{ print $3 }' || true)
    fi
    if [[ -n $pkts ]]; then
        echo "$pkts"
    else
        echo "n/a (sudo required)"
    fi
}

# --- awk: journal parsing -----------------------------------------------------
# Input: the journal in short-unix format. Output: tab-separated lines:
#   E <epoch> <description>   event for the --events mode
#   D <epoch> <reason>        VPN drop (LCP / sleep / command / other)
#   G <total> <max> <from> <to>  outages: total, longest (seconds) and its bounds
# Parameters: -v now=<epoch> -v active=<0|1>; patterns come from the environment (RE_*).
read -r -d '' AWK_PROG <<'AWK' || true
function add(t, kind, pid) { n++; T[n] = t; K[n] = kind; P[n] = pid }
function pppd_pid(   s) {
    if (match($0, /pppd\[[0-9]+\]/)) { s = substr($0, RSTART + 5, RLENGTH - 6); return s }
    return ""
}
function close_gap(end,   dur) {
    dur = gacc
    total += dur
    if (dur > longest) { longest = dur; lstart = gstart; lend = end }
}
BEGIN {
    re_up = ENVIRON["RE_UP"];       re_fail = ENVIRON["RE_FAIL"]
    re_noagent = ENVIRON["RE_NOAGENT"]
    re_lcp = ENVIRON["RE_LCP"];     re_term = ENVIRON["RE_TERM"]
    re_conn = ENVIRON["RE_CONN"];   re_sleep = ENVIRON["RE_SLEEP"]
    re_wake = ENVIRON["RE_WAKE"];   re_nm = ENVIRON["RE_NM_STOP"]
    re_boot = ENVIRON["RE_BOOT"]
}
# First pass: collect significant events in chronological order.
{
    t = $1 + 0
    if      ($0 ~ re_up)      add(t, "UP", "")
    else if ($0 ~ re_lcp)     add(t, "LCP", pppd_pid())
    else if ($0 ~ re_term)    add(t, "TERM", pppd_pid())
    else if ($0 ~ re_conn)    add(t, "CONN", pppd_pid())
    else if ($0 ~ re_fail)    add(t, "FAIL", "")
    else if ($0 ~ re_noagent) add(t, "NOAGENT", "")
    else if ($0 ~ re_sleep)   add(t, "SLEEP", "")
    else if ($0 ~ re_wake)    add(t, "WAKE", "")
    else if ($0 ~ re_nm)      add(t, "NMSTOP", "")
    else if ($0 ~ re_boot)    add(t, "BOOT", "")
}
END {
    # Suspend moments are needed to tell a sleep-caused "Terminating" from a manual one.
    ns = 0
    for (i = 1; i <= n; i++) if (K[i] == "SLEEP") SL[++ns] = T[i]

    # Drop classification. "Connection terminated" after "No response" or
    # "Terminating" of the same pppd is a continuation of the same drop (isdup).
    for (i = 1; i <= n; i++) {
        k = K[i]; isdup[i] = 0; R[i] = ""
        if (k == "LCP") { R[i] = "LCP"; seen[P[i]] = 1 }
        else if (k == "TERM") {
            R[i] = "command"
            for (j = 1; j <= ns; j++) {
                d = T[i] - SL[j]; if (d < 0) d = -d
                if (d <= 5) { R[i] = "sleep"; break }
            }
            seen[P[i]] = 1
        } else if (k == "CONN") {
            if (P[i] in seen) isdup[i] = 1; else R[i] = "other"
            seen[P[i]] = 1
        }
    }

    # Outage state machine. known: whether the VPN state is known (nothing is
    # counted before the first VPN event in the window); down: VPN is down; asleep: laptop is asleep.
    known = 0; down = 0; asleep = 0; total = 0; longest = 0; lstart = 0; lend = 0
    for (i = 1; i <= n; i++) {
        k = K[i]; t = T[i]
        if (k == "UP") {
            if (!known) { known = 1; down = 0 }
            else if (down) {
                if (!asleep) gacc += t - cs
                close_gap(t); down = 0
            }
        } else if ((k == "LCP" || k == "TERM" || (k == "CONN" && !isdup[i])) && !down) {
            known = 1; down = 1; gstart = t; gacc = 0; cs = t
        } else if (k == "SLEEP") {
            if (!asleep) { if (down) gacc += t - cs; asleep = 1 }
        } else if (k == "WAKE") {
            if (asleep) { asleep = 0; if (down) cs = t }
        }
    }
    # Unclosed outage: counted up to "now", only if the VPN is not connected now.
    if (down && !active) {
        if (!asleep) gacc += now - cs
        close_gap(now)
    }

    # Output of events and drops.
    for (i = 1; i <= n; i++) {
        k = K[i]; t = T[i]; txt = ""
        if (k == "UP") txt = "VPN connected"
        else if (k == "LCP" || k == "TERM" || (k == "CONN" && !isdup[i])) {
            if (R[i] == "LCP") txt = "VPN drop: server stopped responding (LCP)"
            else if (R[i] == "sleep") txt = "VPN drop: suspend"
            else if (R[i] == "command") txt = "VPN drop: stopped on command"
            else txt = "VPN drop: tunnel terminated (other)"
            printf "D\t%.0f\t%s\n", int(t), R[i]
        }
        else if (k == "SLEEP") txt = "suspend"
        else if (k == "WAKE") txt = "wake"
        else if (k == "NMSTOP") txt = "NetworkManager restart"
        else if (k == "BOOT") txt = "system boot"
        else if (k == "FAIL") txt = "VPN connection failed (timeout)"
        else if (k == "NOAGENT") txt = "VPN password request failed (no agent)"
        if (txt != "") printf "E\t%.0f\t%s\n", int(t), txt
    }
    printf "G\t%.0f\t%.0f\t%.0f\t%.0f\n", total, longest, lstart, lend
}
AWK

# --- Main code ----------------------------------------------------------------
SHOW_EVENTS=0
VPN_NAME_ARG=""
ARG_HOURS=""
HAVE_ARG=0

while (( $# > 0 )); do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --events)  SHOW_EVENTS=1 ;;
        --vpn)
            if (( $# < 2 )) || [[ -z $2 ]]; then
                echo "Option --vpn requires a profile name" >&2
                usage >&2
                exit 2
            fi
            VPN_NAME_ARG=$2
            shift ;;
        -[0-9.,]*|[0-9.,]*|[!-]*)
            # Negative numbers and any non-flag are treated as the "hours" value,
            # so that "-1" gets a note rather than an error.
            if (( HAVE_ARG )); then
                echo "Extra argument: $1" >&2
                usage >&2
                exit 2
            fi
            ARG_HOURS=$1; HAVE_ARG=1 ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2 ;;
    esac
    shift
done

if (( HAVE_ARG )); then
    parse_hours "$ARG_HOURS"
else
    NOTE=""
    HALFHOURS=$((DEFAULT_HOURS * 2))
fi
MINUTES=$((HALFHOURS * 30))

resolve_vpn
build_name_patterns

JOURNAL=$(mktemp)
trap 'rm -f "$JOURNAL"' EXIT

NOW=$(date +%s)
if ! journalctl --since "-${MINUTES}min" -o short-unix --no-pager -q >"$JOURNAL" 2>/dev/null; then
    echo "Failed to read the journal (journalctl)." >&2
    exit 1
fi

START=$((NOW - MINUTES * 60))
if [[ $(date -d "@$START" +%F) == "$(date -d "@$NOW" +%F)" ]]; then
    PERIOD="$(fmt_datetime "$START") – $(date -d "@$NOW" +%H:%M)"
else
    PERIOD="$(fmt_datetime "$START") – $(fmt_datetime "$NOW")"
fi

if vpn_active; then VPN_ACTIVE=1; else VPN_ACTIVE=0; fi

# Parse the journal with awk: events, drops, outages.
RESULT=$(awk -v now="$NOW" -v active="$VPN_ACTIVE" "$AWK_PROG" "$JOURNAL")

# Counters.
LCP=$(count "$RE_LCP")
SIGNAL=$(count "$RE_TERM")
CONN=$(count "$RE_CONN")
TOTAL=$(( CONN > LCP + SIGNAL ? CONN : LCP + SIGNAL ))
OTHER=$((TOTAL - LCP - SIGNAL))
STARTS=$(count "$RE_START")
OKS=$(count "$RE_UP")
FAILS=$(count "$RE_FAIL")
NOAGENTS=$(count "$RE_NOAGENT")
HELLOS=$(count "$RE_HELLO")
WIFI_DOWN=$(count "$RE_WIFI_DOWN")
WIFI_RESET=$(count "$RE_WIFI_RESET")
SLEEPS=$(count "$RE_SLEEP")
NM_RESTARTS=$(count "$RE_NM_STOP")
BOOTS=$(count "$RE_BOOT")

# Outages.
IFS=$'\t' read -r _ DOWN_TOTAL DOWN_MAX GAP_FROM GAP_TO < <(awk -F'\t' '$1 == "G"' <<<"$RESULT")
DOWN_TOTAL_TXT=$(fmt_duration "$DOWN_TOTAL")
DOWN_MAX_TXT=$(fmt_duration "$DOWN_MAX")
if (( DOWN_MAX > 0 )); then
    DOWN_MAX_TXT+=" ($(fmt_time "$GAP_FROM") – $(fmt_time "$GAP_TO"))"
fi

# The last Hello from the server.
HELLO_TXT="$HELLOS"
if (( HELLOS > 0 )); then
    LAST_HELLO=$(grep -E -- "$RE_HELLO" "$JOURNAL" | tail -n1 | cut -d' ' -f1 || true)
    HELLO_TXT+=" (last $(fmt_time "${LAST_HELLO%%.*}"))"
fi

# The last 5 drops, newest first.
RECENT=""
while IFS=$'\t' read -r _ ts reason; do
    [[ -n $ts ]] || continue
    RECENT+="${RECENT:+, }$(fmt_time "$ts") ($reason)"
done < <(awk -F'\t' '$1 == "D"' <<<"$RESULT" | tail -n 5 | tac)
[[ -n $RECENT ]] || RECENT="—"

# --- Output -------------------------------------------------------------------
if [[ -n $NOTE ]]; then
    printf '%s\n\n' "$NOTE"
fi

printf '**Period:** %s (%s)\n\n' "$PERIOD" "$(period_label "$HALFHOURS")"

echo "### Statistics"
echo
echo "| Metric | Value |"
echo "|---|---:|"
echo "| VPN drops, total | $TOTAL |"
echo "|   • server stopped responding (LCP) | $LCP |"
echo "|   • on command: sleep, nmcli down | $SIGNAL |"
echo "|   • other | $OTHER |"
echo "| VPN connection attempts | $STARTS |"
echo "| Successful connections | $OKS |"
echo "| Failed connections (timeout) | $FAILS |"
echo "| Password request errors (no agent) | $NOAGENTS |"
echo "| Total time without VPN | $DOWN_TOTAL_TXT |"
echo "| Longest outage | $DOWN_MAX_TXT |"
echo "| Hello from L2TP server | $HELLO_TXT |"
echo "| Wi-Fi: disconnects | $WIFI_DOWN |"
echo "| Wi-Fi: resets after VPN failure | $WIFI_RESET |"
echo "| Suspends | $SLEEPS |"
echo "| NetworkManager restarts | $NM_RESTARTS |"
echo "| System boots | $BOOTS |"
echo "| Last drops | $RECENT |"
echo

if (( VPN_ACTIVE )); then VPN_NOW="connected"; else VPN_NOW="disconnected"; fi
echo "### Now"
echo
echo "| Metric | Value |"
echo "|---|---:|"
echo "| $VPN_NAME | $VPN_NOW |"
echo "| Kill switch (vpn-killswitch.service) | $(unit_state systemctl is-active vpn-killswitch.service) |"
echo "| Watchdog (vpn-watchdog.service) | $(unit_state systemctl --user is-active vpn-watchdog.service) |"
echo "| Blocked by kill switch, packets | $(killswitch_drops) |"

if (( SHOW_EVENTS )); then
    echo
    echo "**Events:**"
    echo
    EV_COUNT=0
    while IFS=$'\t' read -r _ ts text; do
        [[ -n $ts ]] || continue
        EV_COUNT=$((EV_COUNT + 1))
        printf -- '- %s  %s\n' "$(fmt_time "$ts")" "$text"
    done < <(awk -F'\t' '$1 == "E"' <<<"$RESULT")
    if (( EV_COUNT == 0 )); then
        echo "- no events"
    fi
fi

exit 0
