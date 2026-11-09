#!/usr/bin/env bash
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

# 2. Passwordless access: the kill switch counter and unit management from the settings window.
if ! sudo -n nft list chain inet killswitch output >/dev/null 2>&1; then
    echo "Warning: sudo -n nft list chain inet killswitch output fails." >&2
    echo "The tray icon will work but will show \"counter unavailable\" instead of the counter." >&2
    echo "To enable the counter run: sudo visudo -f /etc/sudoers.d/vpn-stats" >&2
    echo "and add the line:" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/sbin/nft list chain inet killswitch output" >&2
    echo "(if the kill switch is not installed yet, run ./install.sh first)" >&2
fi

# The toggle in the "Settings…" window runs systemctl enable/disable --now via sudo -n.
if ! sudo -n -l /usr/bin/systemctl enable --now vpn-killswitch.service >/dev/null 2>&1 \
    || ! sudo -n -l /usr/bin/systemctl disable --now vpn-killswitch.service >/dev/null 2>&1; then
    echo "Warning: the Kill switch toggle in the Settings… window will not be able to enable or disable the unit." >&2
    echo "To make it work run: sudo visudo -f /etc/sudoers.d/vpn-stats" >&2
    echo "and add these two lines:" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now vpn-killswitch.service" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now vpn-killswitch.service" >&2
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
