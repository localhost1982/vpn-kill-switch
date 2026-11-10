#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Builds dist/vpn-kill-switch_<version>_all.deb with plain dpkg-deb.
set -euo pipefail

usage() {
    cat <<USAGE
Usage: ${0##*/} [-h]

Builds dist/vpn-kill-switch_<version>_all.deb from the repository (version from
the VERSION file). Needs dpkg-deb; validates the sudoers file with visudo when
available.
USAGE
}

case "${1:-}" in
-h | --help)
    usage
    exit 0
    ;;
'') ;;
*)
    usage >&2
    exit 2
    ;;
esac

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

die() {
    echo "error: $*" >&2
    exit 1
}

command -v dpkg-deb >/dev/null || die "dpkg-deb not found"

VERSION=$(tr -d '[:space:]' <VERSION)
[[ $VERSION =~ ^[0-9][0-9A-Za-z.+~-]*$ ]] || die "bad version in VERSION: '$VERSION'"

# Source -> destination -> mode
FILES=(
    "tray/vpn-tray.py|usr/lib/vpn-kill-switch/vpn-tray.py|0755"
    "watchdog/vpn-watchdog.sh|usr/lib/vpn-kill-switch/vpn-watchdog.sh|0755"
    "VERSION|usr/lib/vpn-kill-switch/VERSION|0644"
    "vpn-stats.sh|usr/bin/vpn-stats|0755"
    "vpn-killswitch-server.sh|usr/sbin/vpn-killswitch-server|0755"
    "vpn-killswitch.service|usr/lib/systemd/system/vpn-killswitch.service|0644"
    "tray/vpn-tray.service|usr/lib/systemd/user/vpn-tray.service|0644"
    "watchdog/vpn-watchdog.service|usr/lib/systemd/user/vpn-watchdog.service|0644"
    "killswitch.nft|etc/nftables.d/killswitch.nft|0644"
    "packaging/sudoers|etc/sudoers.d/vpn-kill-switch|0440"
    "README.md|usr/share/doc/vpn-kill-switch/README.md|0644"
    "LICENSE|usr/share/doc/vpn-kill-switch/copyright|0644"
    "CHANGELOG.md|usr/share/doc/vpn-kill-switch/CHANGELOG.md|0644"
)
MAINTAINER_SCRIPTS=(preinst postinst prerm postrm)

# Screenshots shown in the README (docs/screenshots/*.png), installed next to it so that the relative
# image links of the installed README resolve. A missing file (e.g. tray-menu.png) is not an error.
shopt -s nullglob
for shot in docs/screenshots/*.png; do
    FILES+=("$shot|usr/share/doc/vpn-kill-switch/$shot|0644")
done
shopt -u nullglob

# ---- checks -----------------------------------------------------------------
for entry in "${FILES[@]}"; do
    [[ -f ${entry%%|*} ]] || die "missing source file: ${entry%%|*}"
done
for s in "${MAINTAINER_SCRIPTS[@]}" control.in conffiles; do
    [[ -f packaging/$s ]] || die "missing packaging/$s"
done

if command -v visudo >/dev/null; then
    visudo -cf packaging/sudoers >/dev/null || die "packaging/sudoers failed visudo -c"
else
    echo "warning: visudo not found, sudoers not validated" >&2
fi

for f in watchdog/vpn-watchdog.sh vpn-stats.sh vpn-killswitch-server.sh packaging/build-deb.sh; do
    bash -n "$f" || die "bash -n failed: $f"
done
for s in "${MAINTAINER_SCRIPTS[@]}"; do
    sh -n "packaging/$s" || die "sh -n failed: packaging/$s"
done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Compile into the temp dir so that no __pycache__ appears in the tree.
PYTHONPYCACHEPREFIX="$TMP/pycache" python3 -m py_compile tray/vpn-tray.py \
    || die "py_compile failed: tray/vpn-tray.py"

for u in vpn-killswitch.service tray/vpn-tray.service watchdog/vpn-watchdog.service; do
    if grep -q '@' "$u"; then
        die "unit $u still contains '@' placeholders"
    fi
done

# ---- staging ----------------------------------------------------------------
STAGE="$TMP/stage"
mkdir -p "$STAGE/DEBIAN"

for entry in "${FILES[@]}"; do
    IFS='|' read -r src dst mode <<<"$entry"
    install -D -m "$mode" "$src" "$STAGE/$dst"
done
# Directories 0755 (install -D may inherit umask).
find "$STAGE" -type d -exec chmod 0755 {} +

INSTALLED_SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@INSTALLED_SIZE@/$INSTALLED_SIZE/" \
    packaging/control.in >"$STAGE/DEBIAN/control"
grep -qE '@[A-Z_]+@' "$STAGE/DEBIAN/control" && die "control still contains '@' placeholders"
install -m 0644 packaging/conffiles "$STAGE/DEBIAN/conffiles"
for s in "${MAINTAINER_SCRIPTS[@]}"; do
    install -m 0755 "packaging/$s" "$STAGE/DEBIAN/$s"
done

# md5sums: every file except DEBIAN/* and conffiles.
(
    cd "$STAGE"
    find . -path ./DEBIAN -prune -o -type f -print | LC_ALL=C sort | sed 's#^\./##' |
        grep -vxF -f <(sed 's#^/##' "$ROOT/packaging/conffiles") |
        xargs -d '\n' md5sum
) >"$STAGE/DEBIAN/md5sums"
chmod 0644 "$STAGE/DEBIAN/md5sums"

# ---- build ------------------------------------------------------------------
mkdir -p dist
OUT="dist/vpn-kill-switch_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null
echo "Built: $ROOT/$OUT"
dpkg-deb -I "$OUT"
