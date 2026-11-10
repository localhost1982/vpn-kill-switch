#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Install the VPN tray icon: check dependencies and enable the user unit.
# Run: ./tray/install-tray.sh (as a regular user, without sudo).

set -euo pipefail

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/vpn-tray.service"

if (( EUID == 0 )); then
    echo "Run as a regular user, not root: the unit is installed into ~/.config." >&2
    exit 1
fi

# 1. Python bindings for GTK, libnm and AppIndicator.
if ! python3 -c "
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('NM', '1.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, NM, AyatanaAppIndicator3
" >/dev/null 2>&1; then
    echo "Missing Python bindings for GTK, NetworkManager or AppIndicator." >&2
    echo "Install the packages (this script does not run apt itself):" >&2
    echo "  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-nm-1.0 gir1.2-ayatanaappindicator3-0.1" >&2
    exit 1
fi

# Optional: VPN drop notifications and sound. Not fatal, the tray works without them.
if ! python3 -c "
import gi
gi.require_version('Notify', '0.7')
from gi.repository import Notify
" >/dev/null 2>&1; then
    echo "Warning: gir1.2-notify-0.7 is missing; the tray will not show VPN drop notifications. Install it: sudo apt install gir1.2-notify-0.7" >&2
fi
if ! command -v canberra-gtk-play >/dev/null 2>&1; then
    echo "Warning: canberra-gtk-play is missing; the tray will not play a sound on VPN drops. Install it: sudo apt install gnome-session-canberra" >&2
fi

# 2. Passwordless access: unit management and the VPN profile switch from the settings window
#    (needed by the tray), and reading the nftables counter (needed only by vpn-stats.sh;
#    the tray counts the dropped packets logged in the journal and needs no sudo for that).
ok_counter=1; ok_unit=1; ok_helper=1
sudo -n nft list chain inet killswitch output >/dev/null 2>&1 || ok_counter=0
{ sudo -n -l /usr/bin/systemctl enable --now vpn-killswitch.service >/dev/null 2>&1 \
    && sudo -n -l /usr/bin/systemctl disable --now vpn-killswitch.service >/dev/null 2>&1; } || ok_unit=0
sudo -n -l /usr/local/sbin/vpn-killswitch-server >/dev/null 2>&1 || ok_helper=0

if (( !ok_counter || !ok_unit || !ok_helper )); then
    echo "Warning: some passwordless sudo permissions are missing:" >&2
    (( ok_counter )) || echo "  - without the nft permission vpn-stats.sh shows \"n/a (sudo required)\" in the \"Blocked by kill switch, packets\" row (the tray does not need it);" >&2
    (( ok_unit )) || echo "  - without the systemctl permissions the Kill switch toggle in Settings… cannot enable or disable the unit;" >&2
    (( ok_helper )) || echo "  - without the vpn-killswitch-server permission the VPN connection cannot be changed in Settings…" >&2
    echo "To fix this run: sudo visudo -f /etc/sudoers.d/vpn-stats" >&2
    echo "and add these lines:" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/sbin/nft list chain inet killswitch output" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now vpn-killswitch.service" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now vpn-killswitch.service" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/local/sbin/vpn-killswitch-server *" >&2
    echo "(if the kill switch is not installed yet, run ./install.sh first)" >&2
fi

# 3. The unit with the script path substituted in.
ESC=${DIR//\\/\\\\}
ESC=${ESC//&/\\&}
ESC=${ESC//|/\\|}
mkdir -p "$UNIT_DIR"
sed "s|@TRAY_DIR@|$ESC|" "$DIR/vpn-tray.service" >"$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now vpn-tray.service

echo "vpn-tray.service: $(systemctl --user is-active vpn-tray.service || true)"
