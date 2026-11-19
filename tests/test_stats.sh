#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# vpn-stats.sh on journal fixtures (VPN_STATS_JOURNAL_FILE / VPN_STATS_NOW / VPN_STATS_CONNECTED):
# the Hiddify backend (contract fixture), the NetworkManager backend (a synthetic pppd journal),
# option handling. nmcli, sudo and systemctl are replaced by stubs in PATH, so the output does not
# depend on the machine and nothing real is queried or changed.
set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT=$ROOT/vpn-stats.sh
HID=$FIXTURES/hiddify.journal
NM=$FIXTURES/nm-pppd.journal

BIN=$TEST_TMP/bin
mkdir -p "$BIN"
cat >"$BIN/sudo" <<'STUB'
#!/bin/sh
exit 1
STUB
cat >"$BIN/systemctl" <<'STUB'
#!/bin/sh
echo inactive
exit 3
STUB
cat >"$BIN/nmcli" <<'STUB'
#!/bin/sh
case "$*" in
    "-g connection.uuid connection show My VPN") echo uuid-1 ;;
    "-g connection.id connection show uuid-1") echo "My VPN" ;;
    *) exit 1 ;;
esac
STUB
chmod +x "$BIN/sudo" "$BIN/systemctl" "$BIN/nmcli"
export PATH=$BIN:$PATH TZ=UTC LC_ALL=C

mkcfg() { # NAME [settings text]: a config home; prints its path
    local d=$TEST_TMP/cfg-$1
    mkdir -p "$d/vpn-killswitch"
    [[ $# -ge 2 ]] && printf '%s' "$2" >"$d/vpn-killswitch/settings.conf"
    echo "$d"
}
CFG_NONE=$(mkcfg none)
CFG_HID=$(mkcfg hid $'[vpn]\nbackend = hiddify\n')
CFG_WG=$(mkcfg wg $'[vpn]\nbackend = hiddify\n[hiddify]\ninterface = wg0\n')
CFG_NM=$(mkcfg nm $'[vpn]\nuuid = uuid-1\nbackend = nm\n')
CFG_BOGUS=$(mkcfg bogus $'[vpn]\nuuid = uuid-1\nbackend = bogus\n')

# run CFG CONNECTED JOURNAL NOW ARGS...: output on stdout, the exit status in RC
RC=0
run() {
    local cfg=$1 conn=$2 journal=$3 now=$4 out
    shift 4
    out=$(XDG_CONFIG_HOME=$cfg VPN_STATS_JOURNAL_FILE=$journal VPN_STATS_NOW=$now VPN_STATS_CONNECTED=$conn "$SCRIPT" "$@" 2>&1)
    RC=$?
    printf '%s\n' "$out"
}

# row OUTPUT LABEL: the value of a table row "| LABEL | value |"
row() {
    grep -F -- "| $2 |" <<<"$1" | head -n1 | sed 's/^.*| \([^|]*\) |$/\1/'
}
check_row() { # NAME OUTPUT LABEL EXPECTED
    assert_eq "$1: $3" "$(row "$2" "$3")" "$4"
}

# ---- Hiddify backend, the contract fixture ----------------------------------------------
o=$(run "$CFG_HID" 0 "$HID" 5200 24)
assert_eq "hiddify: exit status" "$RC" 0
check_row hid "$o" "VPN drops, total" 4
check_row hid "$o" "  • on command (Hiddify stopped)" 1
check_row hid "$o" "  • suspend" 1
check_row hid "$o" "  • other" 2
check_row hid "$o" "VPN connection attempts" 5
check_row hid "$o" "Successful connections" 4
check_row hid "$o" "Failed connections (Hiddify core failed to start)" 1
check_row hid "$o" "Total time without VPN" "3m 10s"
check_row hid "$o" "Longest outage" "1m 40s (01:25:00 – 01:26:40)"
check_row hid "$o" "Suspends" 1
check_row hid "$o" "System boots" 1
check_row hid "$o" "NetworkManager restarts" 0
check_row hid "$o" "Wi-Fi: disconnects" 1
check_row hid "$o" "Last drops" "01:25:00 (other), 01:06:40 (other), 00:50:01 (suspend), 00:33:20 (command)"
check_row hid "$o" "Hiddify" disconnected
check_row hid "$o" "Watchdog (vpn-watchdog.service)" "not used with Hiddify"
check_row hid "$o" "Blocked by kill switch, packets" "n/a (sudo required)"
assert_contains "hiddify: kill switch mode row" "$o" "| Kill switch mode | "
assert_not_contains "hiddify: no pppd rows" "$o" "server stopped responding (LCP)"
assert_not_contains "hiddify: no password row" "$o" "Password request errors"
assert_not_contains "hiddify: no L2TP row" "$o" "Hello from L2TP server"

o=$(run "$CFG_HID" 1 "$HID" 5200 24)
check_row hid-up "$o" "VPN drops, total" 4
check_row hid-up "$o" "Total time without VPN" "1m 30s"
check_row hid-up "$o" "Longest outage" "50s (01:06:40 – 01:07:30)"
check_row hid-up "$o" "Hiddify" connected

# --hiddify without a settings file, and the flag winning over the file
o=$(run "$CFG_NONE" 0 "$HID" 5200 24 --hiddify)
check_row flag "$o" "VPN drops, total" 4
o=$(run "$CFG_NM" 0 "$HID" 5200 24 --hiddify)
check_row flag-over-nm "$o" "VPN drops, total" 4
o=$(run "$CFG_HID" 0 "$HID" 5200 1.5)
assert_contains "hours argument: period label" "$o" "(1.5 h)"

# another interface in the settings: no tunnel events, the start failure is still seen
o=$(run "$CFG_WG" 0 "$HID" 5200 24)
check_row wg0 "$o" "VPN drops, total" 0
check_row wg0 "$o" "Successful connections" 0
check_row wg0 "$o" "Failed connections (Hiddify core failed to start)" 1
check_row wg0 "$o" "Total time without VPN" "0s"
check_row wg0 "$o" "Last drops" "—"

# the chronology
o=$(run "$CFG_HID" 0 "$HID" 5200 24 --events)
for l in "system boot" "Hiddify connected" "Hiddify drop: on command" "suspend" "Hiddify drop: suspend" "wake" \
    "Hiddify drop: other" "Hiddify failed to start"; do
    assert_contains "hiddify events: $l" "$o" "  $l"$'\n'
done
assert_eq "hiddify events: connected" "$(grep -c 'Hiddify connected$' <<<"$o")" 4
assert_eq "hiddify events: drops" "$(grep -c 'Hiddify drop:' <<<"$o")" 4

# ---- NetworkManager backend, the synthetic pppd journal -------------------------------------
nm_checks() { # NAME OUTPUT
    local n=$1 o=$2
    check_row "$n" "$o" "VPN drops, total" 6
    check_row "$n" "$o" "  • server stopped responding (LCP)" 2
    check_row "$n" "$o" "  • on command: sleep, nmcli down" 3
    check_row "$n" "$o" "  • other" 1
    check_row "$n" "$o" "VPN connection attempts" 5
    check_row "$n" "$o" "Successful connections" 5
    check_row "$n" "$o" "Failed connections (timeout)" 1
    check_row "$n" "$o" "Password request errors (no agent)" 1
    check_row "$n" "$o" "Hello from L2TP server" "2 (last 01:15:00)"
    check_row "$n" "$o" "Wi-Fi: disconnects" 1
    check_row "$n" "$o" "Wi-Fi: resets after VPN failure" 1
    check_row "$n" "$o" "Suspends" 1
    check_row "$n" "$o" "NetworkManager restarts" 1
    check_row "$n" "$o" "System boots" 1
    check_row "$n" "$o" "Last drops" "01:23:30 (LCP), 01:23:20 (command), 01:06:40 (other), 00:50:02 (sleep), 00:33:20 (command)"
    check_row "$n" "$o" "Blocked by kill switch, packets" "n/a (sudo required)"
    assert_not_contains "$n: no Hiddify rows" "$o" "Hiddify"
}

o=$(run "$CFG_NONE" 0 "$NM" 5100 24 --vpn "My VPN")
assert_eq "nm --vpn: exit status" "$RC" 0
nm_checks nm-down "$o"
check_row nm-down "$o" "Total time without VPN" "4m 45s"
check_row nm-down "$o" "Longest outage" "2m 00s (01:06:40 – 01:08:40)"
check_row nm-down "$o" "My VPN" disconnected
check_row nm-down "$o" "Watchdog (vpn-watchdog.service)" inactive

o=$(run "$CFG_NONE" 1 "$NM" 5100 24 --vpn "My VPN")
check_row nm-up "$o" "VPN drops, total" 6
check_row nm-up "$o" "Total time without VPN" "3m 05s"
check_row nm-up "$o" "Longest outage" "2m 00s (01:06:40 – 01:08:40)"
check_row nm-up "$o" "My VPN" connected

# the profile from the settings file (uuid -> name through nmcli), and an invalid backend value
o=$(run "$CFG_NM" 0 "$NM" 5100 24)
nm_checks nm-settings "$o"
check_row nm-settings "$o" "My VPN" disconnected
o=$(run "$CFG_BOGUS" 0 "$NM" 5100 24)
check_row nm-bogus-backend "$o" "VPN drops, total" 6

# the profile name is matched exactly: another profile has none of this profile's connections
o=$(run "$CFG_NONE" 0 "$NM" 5100 24 --vpn "Other VPN")
check_row nm-other "$o" "VPN connection attempts" 1
check_row nm-other "$o" "Successful connections" 1
check_row nm-other "$o" "Failed connections (timeout)" 1

# watchdog switched off in the settings
CFG_WD=$(mkcfg wd $'[vpn]\nuuid = uuid-1\n[watchdog]\nenabled = false\n')
o=$(run "$CFG_WD" 0 "$NM" 5100 24)
check_row nm-wd-off "$o" "Watchdog (vpn-watchdog.service)" "disabled in settings"

# the chronology
o=$(run "$CFG_NONE" 0 "$NM" 5100 24 --vpn "My VPN" --events)
assert_eq "nm events: connected" "$(grep -c 'VPN connected$' <<<"$o")" 5
assert_eq "nm events: drops" "$(grep -c 'VPN drop:' <<<"$o")" 6
for l in "VPN drop: server stopped responding (LCP)" "VPN drop: stopped on command" "VPN drop: suspend" \
    "VPN drop: tunnel terminated (other)" "VPN connection failed (timeout)" "VPN password request failed (no agent)" \
    "NetworkManager restart" "suspend" "wake" "system boot"; do
    assert_contains "nm events: $l" "$o" "  $l"$'\n'
done

# ---- hours and options ------------------------------------------------------------------
period() { run "$CFG_NONE" 0 "$NM" 5100 "$@" --vpn "My VPN" | grep -F '**Period:**'; }
assert_contains "default period is 1 h" "$(period)" "(1 h)"
assert_contains "0.7 rounds to 0.5 h" "$(period 0.7)" "(0.5 h)"
assert_contains "0.75 rounds to 1 h" "$(period 0.75)" "(1 h)"
assert_contains "1,5 with a decimal comma" "$(period 1,5)" "(1.5 h)"
assert_contains "1.3 rounds to 1.5 h" "$(period 1.3)" "(1.5 h)"
o=$(run "$CFG_NONE" 0 "$NM" 5100 abc --vpn "My VPN")
assert_contains "not a number: note" "$o" 'Argument "abc" is not a number, using the default of 1 h.'
o=$(run "$CFG_NONE" 0 "$NM" 5100 0 --vpn "My VPN")
assert_contains "zero: note" "$o" "is zero after rounding"

o=$(run "$CFG_NONE" 0 "$NM" 5100 --help)
assert_eq "--help: exit status" "$RC" 0
assert_contains "--help: usage" "$o" "Usage:"
assert_contains "--help mentions the Hiddify option" "$o" "--hiddify"
o=$(run "$CFG_NONE" 0 "$NM" 5100 -h)
assert_eq "-h: exit status" "$RC" 0

expect_rc() { # NAME WANTED-STATUS ARGS...
    local name=$1 want=$2
    shift 2
    run "$CFG_NONE" 0 "$NM" 5100 "$@" >"$TEST_TMP/out"
    assert_eq "$name: exit status" "$RC" "$want"
}
expect_rc "--hiddify --vpn" 2 --hiddify --vpn X
assert_contains "--hiddify --vpn: message" "$(cat "$TEST_TMP/out")" "cannot be used together"
expect_rc "--vpn --hiddify" 2 --vpn X --hiddify
expect_rc "--vpn without a name" 2 --vpn
expect_rc "unknown option" 2 --bogus
expect_rc "extra argument" 2 1 2
expect_rc "no profile selected" 1
assert_contains "no profile selected: message" "$(cat "$TEST_TMP/out")" "No VPN profile selected"
o=$(run "$CFG_NONE" 0 "$TEST_TMP/no-such-journal" 5100 24 --vpn "My VPN")
assert_eq "unreadable journal file: exit status" "$RC" 1
assert_contains "unreadable journal file: message" "$o" "Failed to read the journal file"

finish
