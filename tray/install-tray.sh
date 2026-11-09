#!/usr/bin/env bash
# Установка иконки VPN в трее: проверка зависимостей и включение пользовательского юнита.
# Запуск: ./tray/install-tray.sh (от обычного пользователя, без sudo).

set -euo pipefail

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/vpn-tray.service"

if (( EUID == 0 )); then
    echo "Запускайте от обычного пользователя, не от root: юнит ставится в ~/.config." >&2
    exit 1
fi

# 1. Привязки Python для GTK, libnm и AppIndicator.
if ! python3 -c "
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('NM', '1.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, NM, AyatanaAppIndicator3
" >/dev/null 2>&1; then
    echo "Не хватает привязок Python для GTK, NetworkManager или AppIndicator." >&2
    echo "Установите пакеты (скрипт сам apt не запускает):" >&2
    echo "  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-nm-1.0 gir1.2-ayatanaappindicator3-0.1" >&2
    exit 1
fi

# 2. Доступ без пароля: счётчик kill switch и управление юнитом из окна настроек.
if ! sudo -n nft list chain inet killswitch output >/dev/null 2>&1; then
    echo "Предупреждение: sudo -n nft list chain inet killswitch output не проходит." >&2
    echo "Иконка будет работать, но вместо счётчика покажет «нет доступа к счётчику»." >&2
    echo "Чтобы счётчик заработал, выполните: sudo visudo -f /etc/sudoers.d/vpn-stats" >&2
    echo "и добавьте строку:" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/sbin/nft list chain inet killswitch output" >&2
    echo "(если kill switch ещё не установлен, сначала выполните ./install.sh)" >&2
fi

# Переключатель в окне «Настройки…» запускает systemctl enable/disable --now через sudo -n.
if ! sudo -n -l /usr/bin/systemctl enable --now vpn-killswitch.service >/dev/null 2>&1 \
    || ! sudo -n -l /usr/bin/systemctl disable --now vpn-killswitch.service >/dev/null 2>&1; then
    echo "Предупреждение: переключатель kill switch в окне «Настройки…» не сможет включать и выключать юнит." >&2
    echo "Чтобы он заработал, выполните: sudo visudo -f /etc/sudoers.d/vpn-stats" >&2
    echo "и добавьте две строки:" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now vpn-killswitch.service" >&2
    echo "  $USER ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now vpn-killswitch.service" >&2
fi

# 3. Юнит с подставленным путём к скрипту.
ESC=${DIR//\\/\\\\}
ESC=${ESC//&/\\&}
ESC=${ESC//|/\\|}
mkdir -p "$UNIT_DIR"
sed "s|@TRAY_DIR@|$ESC|" "$DIR/vpn-tray.service" >"$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now vpn-tray.service

echo "vpn-tray.service: $(systemctl --user is-active vpn-tray.service || true)"
