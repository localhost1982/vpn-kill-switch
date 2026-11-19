#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Static checks: shell syntax of every script, the maintainer scripts with their own shell, the Python
# files (byte-compiled into a temporary directory, so no __pycache__ is left), the sudoers file,
# the nftables file (loaded only inside a private namespace), unit files without placeholders,
# licence headers.
set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT" || exit 1

rel() { printf '%s' "${1#"$ROOT"/}"; }

# every *.sh of the repository (build output and .git excluded)
mapfile -t SH_FILES < <(find "$ROOT" \( -path "$ROOT/.git" -o -path "$ROOT/dist" -o -path "$ROOT/.idea" \) -prune -o -name '*.sh' -type f -print | sort)
((${#SH_FILES[@]} > 0)) && pass || fail "no shell scripts found"
for f in "${SH_FILES[@]}"; do
    if bash -n "$f" 2>"$TEST_TMP/err"; then pass; else fail "bash -n $(rel "$f"): $(<"$TEST_TMP/err")"; fi
done

# maintainer scripts: parsed with the shell of their shebang
for f in "$ROOT"/packaging/{preinst,postinst,prerm,postrm}; do
    [[ -f $f ]] || { fail "missing $(rel "$f")"; continue; }
    shebang=$(head -n1 "$f")
    case $shebang in
        '#!/bin/sh'*) shell=sh ;;
        '#!/bin/bash'* | '#!/usr/bin/env bash'*) shell=bash ;;
        *) fail "$(rel "$f"): unexpected shebang '$shebang'"; continue ;;
    esac
    if "$shell" -n "$f" 2>"$TEST_TMP/err"; then pass; else fail "$shell -n $(rel "$f"): $(<"$TEST_TMP/err")"; fi
    [[ -x $f ]] && pass || fail "$(rel "$f") is not executable"
done
# a POSIX shell must be able to parse the /bin/sh scripts
if command -v dash >/dev/null 2>&1; then
    for f in "$ROOT"/packaging/{preinst,postinst,prerm,postrm}; do
        [[ $(head -n1 "$f") == '#!/bin/sh'* ]] || continue
        if dash -n "$f" 2>"$TEST_TMP/err"; then pass; else fail "dash -n $(rel "$f"): $(<"$TEST_TMP/err")"; fi
    done
fi

# the scripts that users run are executable
for f in vpn-stats.sh vpn-killswitch-server.sh watchdog/vpn-watchdog.sh tray/vpn-tray.py packaging/build-deb.sh; do
    [[ -x $ROOT/$f ]] && pass || fail "$f is not executable"
done

# Python: compile into a temporary directory (nothing is written next to the sources)
mapfile -t PY_FILES < <(find "$ROOT/tray" "$ROOT/tests" -name '*.py' -type f | sort)
for f in "${PY_FILES[@]}"; do
    if python3 -B - "$f" "$TEST_TMP/pyc" <<'PY' 2>"$TEST_TMP/err"; then pass; else fail "py_compile $(rel "$f"): $(<"$TEST_TMP/err")"; fi
import os, py_compile, sys
src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
py_compile.compile(src, cfile=os.path.join(out, os.path.basename(src) + "c"), doraise=True)
PY
done
if find "$ROOT/tray" "$ROOT/tests" -name __pycache__ | grep -q .; then
    echo "note: __pycache__ exists (left by an earlier run without PYTHONDONTWRITEBYTECODE)"
fi

# sudoers
if command -v visudo >/dev/null 2>&1; then
    if out=$(visudo -cf "$ROOT/packaging/sudoers" 2>&1); then pass; else fail "visudo -cf packaging/sudoers: $out"; fi
else
    echo "note: visudo is not installed, packaging/sudoers is not checked"
fi
# ... and it names exactly the paths the tray and the units use
sudoers=$(<"$ROOT/packaging/sudoers")
assert_contains "sudoers: helper" "$sudoers" "/usr/sbin/vpn-killswitch-server *"
assert_contains "sudoers: enable" "$sudoers" "/usr/bin/systemctl enable --now vpn-killswitch.service"
assert_contains "sudoers: disable" "$sudoers" "/usr/bin/systemctl disable --now vpn-killswitch.service"
assert_contains "sudoers: live rules" "$sudoers" "/usr/sbin/nft list chain inet killswitch output"
assert_contains "the tray calls the helper by that path" "$(<"$ROOT/tray/vpn-tray.py")" 'HELPER = "/usr/sbin/vpn-killswitch-server"'

# nftables: parse and load the file in a private namespace
if command -v nft >/dev/null 2>&1 && command -v unshare >/dev/null 2>&1 && unshare -rn true >/dev/null 2>&1; then
    if out=$(unshare -rn nft -c -f "$ROOT/killswitch.nft" 2>&1); then pass; else fail "nft -c -f killswitch.nft: $out"; fi
    if out=$(unshare -rn nft -f "$ROOT/killswitch.nft" 2>&1); then pass; else fail "nft -f killswitch.nft: $out"; fi
else
    echo "note: nft or unshare -rn is not available, killswitch.nft is not loaded"
fi

# unit files: no unsubstituted placeholders, the expected sections
mapfile -t UNITS < <(find "$ROOT" \( -path "$ROOT/.git" -o -path "$ROOT/dist" \) -prune -o -name '*.service' -type f -print | sort)
((${#UNITS[@]} >= 4)) && pass || fail "expected at least 4 unit files, found ${#UNITS[@]}"
for f in "${UNITS[@]}"; do
    if grep -nE '@[A-Za-z0-9_]*@|^[^#]*@' "$f" >"$TEST_TMP/err"; then
        fail "$(rel "$f") contains an @ placeholder: $(<"$TEST_TMP/err")"
    else
        pass
    fi
    for section in '[Unit]' '[Service]'; do
        grep -qxF "$section" "$f" && pass || fail "$(rel "$f") has no $section"
    done
done
# the units start the installed paths
for u in vpn-killswitch.service vpn-killswitch-hiddify.service; do
    grep -q '/usr/sbin/vpn-killswitch-server' "$ROOT/$u" && pass || fail "$u does not call /usr/sbin/vpn-killswitch-server"
done
grep -q 'vpn-tray.py' "$ROOT/tray/vpn-tray.service" && pass || fail "tray unit does not start vpn-tray.py"
grep -q 'vpn-watchdog.sh' "$ROOT/watchdog/vpn-watchdog.service" && pass || fail "watchdog unit does not start vpn-watchdog.sh"
# the Hiddify unit must not be enabled by the package itself (the helper does that)
grep -q 'vpn-killswitch-hiddify' "$ROOT/packaging/postinst" && fail "postinst enables the Hiddify unit; only the helper may do that" || pass

# licence headers
for f in "${SH_FILES[@]}" "${PY_FILES[@]}" "$ROOT"/packaging/{preinst,postinst,prerm,postrm,sudoers} "${UNITS[@]}" \
    "$ROOT/killswitch.nft"; do
    if head -n3 "$f" | grep -q 'SPDX-License-Identifier: MIT'; then pass; else fail "$(rel "$f") has no SPDX header"; fi
done

# VERSION
version=$(head -n1 "$ROOT/VERSION")
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && pass || fail "VERSION '$version' is not x.y.z"

finish
