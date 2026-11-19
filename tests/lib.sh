# SPDX-License-Identifier: MIT
# Helpers sourced by the shell tests: temporary directory, assertions, summary.
# The tests never use sudo and never touch the system; every temporary file lives in TEST_TMP.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$HERE")
FIXTURES=$HERE/fixtures
CHECKS=0
FAILS=0

TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/vpnks-test.XXXXXX") || exit 1

# Remove the temporary directory, but only if it really is one of ours under ${TMPDIR:-/tmp}.
cleanup_tmp() {
    local base=${TMPDIR:-/tmp}
    base=${base%/}
    [[ -n ${TEST_TMP:-} && $TEST_TMP == "$base"/vpnks-test.* && -d $TEST_TMP ]] || return 0
    rm -rf -- "$TEST_TMP"
}
trap cleanup_tmp EXIT

fail() {
    CHECKS=$((CHECKS + 1))
    FAILS=$((FAILS + 1))
    echo "FAIL: $*"
}

pass() {
    CHECKS=$((CHECKS + 1))
}

# assert_eq NAME GOT WANT
assert_eq() {
    if [[ $2 == "$3" ]]; then
        pass
    else
        fail "$1: got '$2', expected '$3'"
    fi
}

# assert_contains NAME TEXT NEEDLE (fixed string)
assert_contains() {
    if [[ $2 == *"$3"* ]]; then
        pass
    else
        fail "$1: expected to find '$3' in:"$'\n'"$2"
    fi
}

# assert_not_contains NAME TEXT NEEDLE (fixed string)
assert_not_contains() {
    if [[ $2 != *"$3"* ]]; then
        pass
    else
        fail "$1: did not expect '$3' in:"$'\n'"$2"
    fi
}

# assert_ok NAME CONDITION-STATUS: pass when the status is 0
assert_ok() {
    if [[ $2 == 0 ]]; then
        pass
    else
        fail "$1"
    fi
}

# skip REASON: the whole test is skipped (status 77, "SKIP (reason)" as the last line)
skip() {
    echo "SKIP ($1)"
    exit 77
}

finish() {
    if ((FAILS)); then
        echo "FAILURES: $FAILS of $CHECKS checks"
        exit 1
    fi
    echo "ALL OK ($CHECKS checks)"
    exit 0
}
