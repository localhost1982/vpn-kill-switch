#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# watchdog/vpn-watchdog.sh in dry-run / single-iteration mode with scratch settings files.
# nmcli is a stub in PATH that only answers "which connections are active" (from FAKE_ACTIVE),
# so nothing real is queried and no connection is ever started.
set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WATCHDOG=$ROOT/watchdog/vpn-watchdog.sh
BIN=$TEST_TMP/bin
mkdir -p "$BIN"
cat >"$BIN/nmcli" <<'STUB'
#!/bin/sh
if [ "$*" = "-t -f UUID connection show --active" ]; then
    printf '%s\n' "${FAKE_ACTIVE:-}"
    exit 0
fi
echo "unexpected nmcli call: $*" >>"$FAKE_NMCLI_LOG"
exit 1
STUB
chmod +x "$BIN/nmcli"
export FAKE_NMCLI_LOG=$TEST_TMP/nmcli.unexpected

OUT=""
ERR=""
RC=0
n=0
# wd CONFIG-TEXT [ACTIVE-UUIDS]: run once; CONFIG-TEXT "-" = no settings file
wd() {
    local cfg=$TEST_TMP/cfg-$((n += 1))
    mkdir -p "$cfg/vpn-killswitch"
    [[ $1 == "-" ]] || printf '%s' "$1" >"$cfg/vpn-killswitch/settings.conf"
    OUT=$(XDG_CONFIG_HOME=$cfg VPN_WATCHDOG_DRY_RUN=1 VPN_WATCHDOG_ONCE=1 FAKE_ACTIVE="${2:-}" PATH=$BIN:$PATH \
        timeout 20 bash "$WATCHDOG" 2>"$TEST_TMP/err")
    RC=$?
    ERR=$(<"$TEST_TMP/err")
}
expect_up() { # NAME UUID CONFIG [ACTIVE]
    wd "$3" "${4:-}"
    assert_eq "$1: stdout" "$OUT" "nmcli connection up uuid $2"
    assert_contains "$1: stderr" "$ERR" "watchdog: bringing up $2"
    assert_eq "$1: exit status" "$RC" 0
}
expect_nothing() { # NAME CONFIG [ACTIVE]
    wd "$2" "${3:-}"
    assert_eq "$1: stdout" "$OUT" ""
    assert_eq "$1: stderr" "$ERR" ""
    assert_eq "$1: exit status" "$RC" 0
}

U=11111111-2222-3333-4444-555555555555

# the plain case
expect_up "nm, inactive profile" "$U" $'[vpn]\nuuid = '"$U"$'\nbackend = nm\n' "OTHER-UUID"
expect_up "no backend key (default nm)" "$U" $'[vpn]\nuuid = '"$U"$'\n'
expect_up "no spaces around =" "$U" $'[vpn]\nuuid='"$U"$'\n'
expect_up "spaces around the section name" "$U" $'[ vpn ]\n  uuid   =   '"$U"$'   \n'
expect_up "invalid backend value means nm" "$U" $'[vpn]\nuuid = '"$U"$'\nbackend = bogus\n'
expect_up "first uuid line wins" "$U" $'[vpn]\nuuid = '"$U"$'\nuuid = zzz\n'
expect_up "a longer active uuid is not this profile" "$U" $'[vpn]\nuuid = '"$U"$'\n' "${U}-and-more"$'\n'"x${U}"
expect_nothing "the profile is already active" $'[vpn]\nuuid = '"$U"$'\n' "OTHER"$'\n'"$U"

# the uuid is found after other sections
expect_up "uuid after other sections" "$U" $'[notify]\npopup = true\nsound = true\n[watchdog]\nenabled = true\n[hiddify]\ninterface = tun0\n[vpn]\nuuid = '"$U"$'\n'
expect_up "backend and uuid in any order" "$U" $'[vpn]\nbackend = nm\n[notify]\npopup = false\n[vpn]\nuuid = '"$U"$'\n'
expect_nothing "uuid in another section is ignored" $'[other]\nuuid = '"$U"$'\n[vpn]\nbackend = nm\n'
expect_nothing "commented uuid lines are ignored" $'[vpn]\n# uuid = '"$U"$'\n; uuid = '"$U"$'\n'
expect_nothing "key names are case-sensitive" $'[vpn]\nUUID = '"$U"$'\n'
expect_nothing "no settings file" "-"
expect_nothing "empty settings file" ""
expect_nothing "empty uuid" $'[vpn]\nuuid =\n'

# the watchdog switch
for v in false no 0 off FALSE Off No; do
    expect_nothing "watchdog disabled ($v)" $'[vpn]\nuuid = '"$U"$'\n[watchdog]\nenabled = '"$v"$'\n'
done
expect_nothing "watchdog disabled with spaces" $'[vpn]\nuuid = '"$U"$'\n[watchdog]\nenabled   =   false  \n'
expect_nothing "watchdog disabled before [vpn]" $'[watchdog]\nenabled = false\n[vpn]\nuuid = '"$U"$'\n'
for v in true yes 1 on TRUE perhaps ""; do
    expect_up "watchdog enabled ($v)" "$U" $'[vpn]\nuuid = '"$U"$'\n[watchdog]\nenabled = '"$v"$'\n'
done
expect_up "an 'enabled' key in another section is not the switch" "$U" $'[vpn]\nuuid = '"$U"$'\n[other]\nenabled = false\n'

# the Hiddify backend: the watchdog idles
expect_nothing "backend hiddify" $'[vpn]\nuuid = '"$U"$'\nbackend = hiddify\n'
expect_nothing "backend Hiddify (case)" $'[vpn]\nuuid = '"$U"$'\nbackend = Hiddify\n'
expect_nothing "backend hiddify with spaces" $'[vpn]\nuuid = '"$U"$'\nbackend =   hiddify   \n'
expect_nothing "backend hiddify, no uuid" $'[vpn]\nbackend = hiddify\n'
expect_nothing "backend hiddify and the watchdog on" $'[vpn]\nuuid = '"$U"$'\nbackend = hiddify\n[watchdog]\nenabled = true\n'
expect_up "backend hiddify only in another section" "$U" $'[other]\nbackend = hiddify\n[vpn]\nuuid = '"$U"$'\n'

# without the dry-run variable the script would run nmcli; the stub never saw anything but the query
if [[ -e $FAKE_NMCLI_LOG ]]; then
    fail "nmcli was called for something else than the active-connection query: $(<"$FAKE_NMCLI_LOG")"
else
    pass
fi

finish
