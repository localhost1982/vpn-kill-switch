#!/usr/bin/env bash
# Устанавливает и включает VPN kill switch (nftables + systemd).
# Берёт killswitch.nft и vpn-killswitch.service из папки скрипта.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NFT_SRC="$SRC_DIR/killswitch.nft"
UNIT_SRC="$SRC_DIR/vpn-killswitch.service"

NFT_DST="/etc/nftables.d/killswitch.nft"
UNIT_DST="/etc/systemd/system/vpn-killswitch.service"
UNIT_NAME="vpn-killswitch.service"

if [[ $EUID -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

for f in "$NFT_SRC" "$UNIT_SRC"; do
    if [[ ! -f "$f" ]]; then
        echo "Не найден файл: $f" >&2
        exit 1
    fi
done

echo "==> Проверка синтаксиса правил"
nft -c -f "$NFT_SRC"

echo "==> Копирование файлов"
install -D -m 0644 "$NFT_SRC" "$NFT_DST"
install -D -m 0644 "$UNIT_SRC" "$UNIT_DST"

echo "==> Включение сервиса"
systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

echo "==> Состояние"
systemctl --no-pager --lines=0 status "$UNIT_NAME" || true
echo
nft list table inet killswitch

echo
echo "Kill switch включён. Интернет теперь только через VPN-L2TP (ppp*)."
echo "Отключить: $SRC_DIR/uninstall.sh"
