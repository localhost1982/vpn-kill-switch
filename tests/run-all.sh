#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Run the whole test suite: one line per test (ok / FAIL / SKIP), a summary, a non-zero exit status
# if anything failed. See tests/README.md. Needs no sudo and changes nothing outside a temporary
# directory; the GUI tests need a graphical session (DISPLAY) and are skipped without one.
set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT" || exit 1

export GDK_BACKEND=x11
export PYTHONDONTWRITEBYTECODE=1

# name : needs a display
TESTS=(
    "test_syntax.sh:0"
    "test_units.py:0"
    "test_vpn_events.py:0"
    "test_hiddify_events.py:0"
    "test_tray_state.py:0"
    "test_settings_window.py:1"
    "test_windows.py:1"
    "test_stats.sh:0"
    "test_helper.sh:0"
    "test_watchdog.sh:0"
)

passed=0
failed=0
skipped=0
started=$SECONDS

run_one() { # NAME
    local name=$1 out=$TEST_TMP/$1.out rc t0=$SECONDS
    case $name in
        *.py) timeout 120 python3 -B "$HERE/$name" >"$out" 2>&1 ;;
        *.sh) timeout 120 bash "$HERE/$name" >"$out" 2>&1 ;;
    esac
    rc=$?
    local took=$((SECONDS - t0))
    case $rc in
        0)
            printf 'ok    %-26s %3ss  %s\n' "$name" "$took" "$(tail -n1 "$out")"
            passed=$((passed + 1))
            ;;
        77)
            printf 'SKIP  %-26s       %s\n' "$name" "$(tail -n1 "$out" | sed 's/^SKIP *//')"
            skipped=$((skipped + 1))
            ;;
        *)
            printf 'FAIL  %-26s %3ss  (exit status %s)\n' "$name" "$took" "$rc"
            sed 's/^/      | /' "$out"
            failed=$((failed + 1))
            ;;
    esac
}

for entry in "${TESTS[@]}"; do
    name=${entry%%:*}
    needs_display=${entry##*:}
    if [[ $needs_display == 1 && -z ${DISPLAY:-} ]]; then
        printf 'SKIP  %-26s       (no DISPLAY)\n' "$name"
        skipped=$((skipped + 1))
        continue
    fi
    run_one "$name"
done

# the suite must leave the checkout clean
if find "$ROOT/tests" "$ROOT/tray" -name __pycache__ 2>/dev/null | grep -q .; then
    printf 'FAIL  %-26s       %s\n' "repo-clean" "__pycache__ found in tests/ or tray/ (remove it; the suite itself writes none)"
    failed=$((failed + 1))
else
    printf 'ok    %-26s\n' "repo-clean (no __pycache__)"
    passed=$((passed + 1))
fi

echo
echo "$passed passed, $failed failed, $skipped skipped in $((SECONDS - started))s"
((failed == 0))
