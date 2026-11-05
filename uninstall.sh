#!/usr/bin/env bash
# Выключает и удаляет VPN kill switch (nftables + systemd).
set -euo pipefail

NFT_DST="/etc/nftables.d/killswitch.nft"
UNIT_DST="/etc/systemd/system/vpn-killswitch.service"
UNIT_NAME="vpn-killswitch.service"

if [[ $EUID -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

echo "==> Остановка и отключение сервиса"
if systemctl list-unit-files "$UNIT_NAME" --no-legend 2>/dev/null | grep -q "$UNIT_NAME"; then
    systemctl disable --now "$UNIT_NAME" || true
else
    echo "Сервис $UNIT_NAME не установлен, пропускаю."
fi

echo "==> Удаление таблицы nftables (если осталась)"
if nft list table inet killswitch >/dev/null 2>&1; then
    nft delete table inet killswitch
fi

echo "==> Удаление файлов"
rm -f "$NFT_DST" "$UNIT_DST"
rmdir /etc/nftables.d 2>/dev/null || true
systemctl daemon-reload

echo "==> Проверка"
if nft list table inet killswitch >/dev/null 2>&1; then
    echo "Таблица inet killswitch всё ещё есть!" >&2
    exit 1
fi
echo "Kill switch выключен и удалён. Трафик снова ходит без ограничений."
