#!/usr/bin/env bash
# Удаление иконки VPN из трея: остановка и удаление пользовательского юнита.
# Запуск: ./tray/uninstall-tray.sh (от обычного пользователя, без sudo).

set -euo pipefail

UNIT="$HOME/.config/systemd/user/vpn-tray.service"

systemctl --user disable --now vpn-tray.service >/dev/null 2>&1 || true
rm -f "$UNIT"
systemctl --user daemon-reload

echo "Иконка в трее удалена: юнит vpn-tray.service остановлен и стёрт."
