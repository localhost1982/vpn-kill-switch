#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# vpn-killswitch-server.sh and killswitch.nft, tested inside a private user + network namespace
# (`unshare -rn`): the nftables table lives only in that namespace and disappears with it, the live
# table of the machine is never touched. The helper's state directory is a temporary VKS_PREFIX.
# No sudo. Skipped (status 77) where unshare -rn or nft is unavailable.
set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

HELPER=$ROOT/vpn-killswitch-server.sh
NFT_FILE=$ROOT/killswitch.nft

if [[ ${1:-} != --inner ]]; then
    command -v unshare >/dev/null 2>&1 || skip "unshare is not installed"
    command -v nft >/dev/null 2>&1 || skip "nft is not installed"
    unshare -rn true >/dev/null 2>&1 || skip "unshare -rn is not permitted here"
    unshare -rn nft list tables >/dev/null 2>&1 || skip "nft does not work in a user namespace here"

    # outside the namespace the helper refuses to run (only checked as a normal user)
    if ((EUID != 0)); then
        mkdir -p "$TEST_TMP/outer"
        out=$(VKS_PREFIX=$TEST_TMP/outer "$HELPER" show 2>&1)
        assert_eq "not root: exit status" "$?" 1
        assert_contains "not root: message" "$out" "must be run as root"
        assert_eq "not root: nothing written" "$(ls -A "$TEST_TMP/outer")" ""
    fi

    inner_out=$(unshare -rn bash "${BASH_SOURCE[0]}" --inner 2>&1)
    inner=$?
    case $inner in
        0) ;;
        77) exit 77 ;;
        *) fail "the checks inside the namespace failed (status $inner)"$'\n'"$inner_out" ;;
    esac
    # the inner stage's own summary line is folded into ours
    inner_checks=$(sed -n 's/^ALL OK (\([0-9]*\) checks)$/\1/p' <<<"$inner_out")
    grep -v '^ALL OK' <<<"$inner_out"
    CHECKS=$((CHECKS + ${inner_checks:-0}))
    finish
fi

# ======================= inside `unshare -rn` (root, private network namespace) =====================
H_OUT=""
H_RC=0
# h ARGS...: run the helper with the current environment; output in H_OUT, status in H_RC
h() {
    H_OUT=$("$HELPER" "$@" 2>&1)
    H_RC=$?
}
chain() { nft list chain inet killswitch vpn_allow 2>&1; }
accept_rules() { chain | grep -c ' accept' || true; }
load_table() { nft -f "$NFT_FILE"; }

PREFIX=$TEST_TMP/prefix
mkdir -p "$PREFIX"
export VKS_PREFIX=$PREFIX
CONF=$PREFIX/etc/vpn-killswitch
EMPTY_CG=$TEST_TMP/cgroup-empty
mkdir -p "$EMPTY_CG"

# ---- the nft file itself
load_table
assert_ok "killswitch.nft loads" $?
load_table
assert_ok "killswitch.nft loads again (idempotent)" $?
assert_eq "the vpn_allow chain starts empty" "$(accept_rules)" 0
out=$(nft list chain inet killswitch output)
assert_contains "output chain jumps to vpn_allow" "$out" "jump vpn_allow"
assert_contains "output chain logs with the tray's prefix" "$out" 'log prefix "KS-DROP: "'
assert_contains "forward chain logs with the tray's prefix" "$(nft list chain inet killswitch forward)" 'log prefix "KS-DROP-FWD: "'
assert_contains "output chain ends in a counted drop" "$out" "counter packets 0 bytes 0 drop"
prefix_in_tray=$(grep -m1 '^DROP_LOG_PREFIX' "$ROOT/tray/vpn-tray.py" | sed 's/^[^"]*"\([^"]*\)".*/\1/')
assert_eq "the tray's DROP_LOG_PREFIX matches the nft file" "$prefix_in_tray" "KS-DROP"

# ---- nothing configured yet
h show
assert_contains "show: not configured" "$H_OUT" "Mode: not configured"
h refresh
assert_eq "refresh without a mode: exit status" "$H_RC" 1
assert_contains "refresh without a mode: message" "$H_OUT" "No VPN server configured yet."
h apply
assert_eq "apply without a mode: exit status" "$H_RC" 0
assert_eq "apply without a mode: the chain stays empty" "$(accept_rules)" 0

# ---- set with IPv4 literals
h set 1.2.3.4 5.6.7.8
assert_eq "set: exit status" "$H_RC" 0
assert_contains "set: summary" "$H_OUT" "VPN servers: 1.2.3.4 (1.2.3.4); 5.6.7.8 (5.6.7.8)"
assert_eq "set: mode file" "$(<"$CONF/mode")" servers
assert_eq "set: hosts file" "$(<"$CONF/servers")" $'1.2.3.4\n5.6.7.8'
assert_eq "set: addresses file" "$(<"$CONF/addresses")" $'1.2.3.4\n5.6.7.8'
assert_eq "set: one accept rule" "$(accept_rules)" 1
assert_contains "set: the rule" "$(chain)" 'ip daddr { 1.2.3.4, 5.6.7.8 } accept comment "VPN server"'
assert_eq "set: file permissions" "$(stat -c %a "$CONF/mode" "$CONF/servers" "$CONF/addresses" | sort -u)" 644
assert_eq "set: no temporary files left" "$(ls -A "$CONF" | sort | tr '\n' ' ')" "addresses mode servers "
h show
assert_contains "show: servers mode" "$H_OUT" "Mode: servers"
assert_contains "show: hosts listed" "$H_OUT" $'Hosts ('
assert_contains "show: the rule" "$H_OUT" "ip daddr { 1.2.3.4, 5.6.7.8 } accept"

# ---- apply is idempotent
before=$(chain)
h apply
assert_eq "apply: exit status" "$H_RC" 0
h apply
assert_eq "apply twice: exit status" "$H_RC" 0
assert_eq "apply twice: the chain is unchanged" "$(chain)" "$before"
assert_eq "apply twice: still one accept rule" "$(accept_rules)" 1
# a reload of the table empties the exceptions until apply puts them back (what the unit's ExecStartPost does)
load_table
assert_eq "table reload: exceptions are gone" "$(accept_rules)" 0
h apply
assert_eq "apply restores them" "$(chain)" "$before"

# ---- set replaces, normalises leading zeros, resolves a name without the network
h set 9.9.9.9
assert_eq "set again: only the new address" "$(<"$CONF/addresses")" 9.9.9.9
assert_contains "set again: the rule" "$(chain)" "ip daddr 9.9.9.9 accept"
assert_not_contains "set again: the old addresses are gone" "$(chain)" "1.2.3.4"
h set 001.002.003.004 9.9.9.9 9.9.9.9
assert_eq "set: leading zeros are normalised, duplicates dropped" "$(<"$CONF/addresses")" $'1.2.3.4\n9.9.9.9'
assert_eq "set: hosts are kept as typed" "$(<"$CONF/servers")" $'001.002.003.004\n9.9.9.9\n9.9.9.9'
if getent ahosts localhost 2>/dev/null | awk '$2=="STREAM" && $1 ~ /^[0-9.]+$/ {f=1} END {exit !f}'; then
    h set localhost
    assert_eq "set with a host name: exit status" "$H_RC" 0
    assert_contains "set with a host name: summary" "$H_OUT" "VPN servers: localhost (127.0.0.1)"
    assert_contains "set with a host name: the rule" "$(chain)" "ip daddr 127.0.0.1 accept"
fi

# ---- refresh re-resolves the saved hosts
h set 1.2.3.4 5.6.7.8
echo 7.7.7.7 >"$CONF/addresses"
h refresh
assert_eq "refresh: exit status" "$H_RC" 0
assert_contains "refresh: summary" "$H_OUT" "VPN servers: 1.2.3.4 (1.2.3.4); 5.6.7.8 (5.6.7.8)"
assert_eq "refresh: the addresses file is rewritten" "$(<"$CONF/addresses")" $'1.2.3.4\n5.6.7.8'
assert_contains "refresh: the rule is updated" "$(chain)" "ip daddr { 1.2.3.4, 5.6.7.8 } accept"

# ---- a damaged state is rejected and the loaded rules stay
good=$(chain)
for bad in "not-an-ip" "999.1.1.1" "1.2.3" "1.2.3.4;rm"; do
    echo "$bad" >"$CONF/addresses"
    h apply
    assert_eq "corrupted addresses '$bad': exit status" "$H_RC" 1
    assert_contains "corrupted addresses '$bad': message" "$H_OUT" "Invalid address"
    assert_eq "corrupted addresses '$bad': the loaded rules are unchanged" "$(chain)" "$good"
done
printf '1.2.3.4\n\n  5.6.7.8\r\n' >"$CONF/addresses"
h apply
assert_eq "blank lines and CRLF in the addresses file are tolerated" "$H_RC" 0
assert_eq "... and the rule is the same" "$(chain)" "$good"
echo bogus >"$CONF/mode"
h apply
assert_eq "invalid mode: exit status" "$H_RC" 1
assert_contains "invalid mode: message" "$H_OUT" "Invalid mode"
assert_eq "invalid mode: the loaded rules are unchanged" "$(chain)" "$good"
echo servers >"$CONF/mode"
: >"$CONF/servers"
h refresh
assert_eq "servers mode without saved hosts: exit status" "$H_RC" 1
assert_contains "servers mode without saved hosts: message" "$H_OUT" "No saved hosts"
printf '1.2.3.4\n5.6.7.8\n' >"$CONF/servers"

# ---- invalid hosts: exit 2 and nothing written
P2=$TEST_TMP/prefix2
mkdir -p "$P2"
for bad in "bad host" "-x" "a;b" "999.1.1.1" "" "a..b" "-bad.example.org" "host_name" "1.2.3.4/32" $'a\nb' '$(id)' '`id`' "a b"; do
    VKS_PREFIX=$P2 h set "$bad"
    assert_eq "invalid host '$bad': exit status" "$H_RC" 2
done
assert_contains "invalid host: message" "$H_OUT" "Invalid host"
VKS_PREFIX=$P2 h set 1.2.3.4 "bad host"
assert_eq "a valid host next to an invalid one: exit status" "$H_RC" 2
assert_eq "nothing is written for an invalid request" "$(ls -A "$P2")" ""
h set
assert_eq "set without hosts: exit status" "$H_RC" 2
assert_contains "set without hosts: usage" "$H_OUT" "Usage:"
assert_contains "the state is untouched by all of that" "$(<"$CONF/servers")" "1.2.3.4"

# ---- Hiddify mode
h set-hiddify extra
assert_eq "set-hiddify with an argument: exit status" "$H_RC" 2
# the service is not running (an empty cgroup root): no rule
VKS_CGROUP_ROOT=$EMPTY_CG h set-hiddify
assert_eq "set-hiddify, service not running: exit status" "$H_RC" 0
assert_contains "set-hiddify, service not running: message" "$H_OUT" "the Hiddify service is not running"
assert_eq "set-hiddify: mode file" "$(<"$CONF/mode")" hiddify
assert_eq "set-hiddify, service not running: the exceptions are removed" "$(accept_rules)" 0
VKS_CGROUP_ROOT=$EMPTY_CG h show
assert_contains "show: Hiddify mode" "$H_OUT" "Mode: Hiddify (HiddifyAdminBG.service)"
assert_contains "show: cgroup absent" "$H_OUT" "Service cgroup: absent"
VKS_CGROUP_ROOT=$EMPTY_CG h apply
assert_eq "apply in Hiddify mode, service not running" "$H_RC" 0
VKS_CGROUP_ROOT=$EMPTY_CG h refresh
assert_eq "refresh in Hiddify mode" "$H_RC" 0
assert_eq "... still no rule" "$(accept_rules)" 0

# the dry run with a (temporary) cgroup directory present prints the rule instead of loading it
FAKE_CG=$TEST_TMP/cgroup-fake
mkdir -p "$FAKE_CG/system.slice/HiddifyAdminBG.service"
before=$(chain)
VKS_DRY_RUN=1 VKS_CGROUP_ROOT=$FAKE_CG h apply
assert_eq "dry-run apply: exit status" "$H_RC" 0
assert_eq "dry-run apply: the script" "$H_OUT" "[dry-run] flush chain inet killswitch vpn_allow"$'\n''[dry-run] add rule inet killswitch vpn_allow socket cgroupv2 level 2 "system.slice/HiddifyAdminBG.service" accept comment "Hiddify service"'
VKS_DRY_RUN=1 VKS_CGROUP_ROOT=$FAKE_CG h set-hiddify
assert_eq "dry-run set-hiddify: exit status" "$H_RC" 0
assert_contains "dry-run set-hiddify: the unit that would be enabled" "$H_OUT" "[dry-run] would run: systemctl enable vpn-killswitch-hiddify.service"
assert_contains "dry-run set-hiddify: the rule" "$H_OUT" 'socket cgroupv2 level 2 "system.slice/HiddifyAdminBG.service" accept'
assert_contains "dry-run set-hiddify: message" "$H_OUT" "traffic of HiddifyAdminBG.service is allowed"
VKS_DRY_RUN=1 VKS_CGROUP_ROOT=$EMPTY_CG h apply
assert_eq "dry-run apply without the cgroup: only the flush" "$H_OUT" "[dry-run] flush chain inet killswitch vpn_allow"
VKS_DRY_RUN=1 VKS_CGROUP_ROOT=$FAKE_CG h show
assert_contains "dry-run show: cgroup present" "$H_OUT" "Service cgroup: present ($FAKE_CG/system.slice/HiddifyAdminBG.service)"
assert_not_contains "dry-run show: no live rules are listed" "$H_OUT" "Rules (inet killswitch vpn_allow)"
VKS_DRY_RUN=1 h set 1.2.3.4
assert_contains "dry-run set: the unit that would be disabled" "$H_OUT" "[dry-run] would run: systemctl disable --now vpn-killswitch-hiddify.service"
assert_contains "dry-run set: the rule" "$H_OUT" '[dry-run] add rule inet killswitch vpn_allow ip daddr { 1.2.3.4 } accept comment "VPN server"'
assert_eq "dry runs never change the live chain" "$(chain)" "$before"

# the real Hiddify cgroup, if the service is running on this machine (the rule is loaded into the private table)
REAL_CG=/sys/fs/cgroup/system.slice/HiddifyAdminBG.service
if [[ -d $REAL_CG ]]; then
    h set-hiddify
    if ((H_RC == 4)); then
        echo "note: nft refused the cgroup rule in this namespace: $H_OUT"
    else
        assert_eq "set-hiddify with the real cgroup: exit status" "$H_RC" 0
        assert_contains "set-hiddify with the real cgroup: message" "$H_OUT" "traffic of HiddifyAdminBG.service is allowed"
        assert_contains "set-hiddify with the real cgroup: the rule" "$(chain)" 'socket cgroupv2 level 2 "system.slice/HiddifyAdminBG.service" accept comment "Hiddify service"'
        assert_eq "set-hiddify with the real cgroup: one accept rule" "$(accept_rules)" 1
        h show
        assert_contains "show: cgroup present" "$H_OUT" "Service cgroup: present"
        h apply
        assert_eq "apply again" "$H_RC" 0
        assert_eq "apply again: still one rule" "$(accept_rules)" 1
        h set 1.2.3.4
        assert_not_contains "back to servers: the cgroup rule is gone" "$(chain)" "cgroupv2"
        assert_contains "back to servers: the address rule" "$(chain)" "ip daddr 1.2.3.4 accept"
    fi
else
    echo "note: $REAL_CG does not exist, the real-cgroup case is not tested"
fi

# ---- the kill switch table is not loaded
nft delete table inet killswitch
h apply
assert_eq "table not loaded: apply exits 0" "$H_RC" 0
assert_contains "table not loaded: apply says so" "$H_OUT" "Kill switch is not active; nothing to apply."
h set 1.2.3.4
assert_eq "table not loaded: set exits 0" "$H_RC" 0
assert_eq "table not loaded: the state is still saved" "$(<"$CONF/addresses")" 1.2.3.4
h refresh
assert_eq "table not loaded: refresh exits 0" "$H_RC" 0
h show
assert_contains "table not loaded: show says so" "$H_OUT" "kill switch is not active"
assert_eq "the helper did not create the table" "$(nft list tables | grep -c killswitch || true)" 0

# ---- usage
for a in -h --help help; do
    h "$a"
    assert_eq "$a: exit status" "$H_RC" 0
    assert_contains "$a: usage" "$H_OUT" "Usage:"
done
h
assert_eq "no command: exit status" "$H_RC" 2
h bogus
assert_eq "unknown command: exit status" "$H_RC" 2
assert_contains "unknown command: message" "$H_OUT" "Unknown command: bogus"
h apply extra
assert_eq "apply with an argument: exit status" "$H_RC" 2
h show extra
assert_eq "show with an argument: exit status" "$H_RC" 2

finish
