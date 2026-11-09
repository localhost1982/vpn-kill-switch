#!/usr/bin/env bash
# VPN watchdog: keeps the VPN profile selected in the tray Settings connected.
# Every 3 seconds it reads the profile UUID from
# $XDG_CONFIG_HOME/vpn-killswitch/settings.conf (default ~/.config/...) and brings
# the profile up if it is not among the active NetworkManager connections.
# No profile selected = nothing to do.
# Environment (for tests): VPN_WATCHDOG_DRY_RUN=1 prints the nmcli command instead of
# running it; VPN_WATCHDOG_ONCE=1 does a single iteration.
# Run: ./vpn-watchdog.sh (normally via the user unit vpn-watchdog.service).

SETTINGS="${XDG_CONFIG_HOME:-$HOME/.config}/vpn-killswitch/settings.conf"
STEP=3

while true; do
    UUID=""
    if [[ -r "$SETTINGS" ]]; then
        UUID=$(sed -n 's/^[[:space:]]*uuid[[:space:]]*=[[:space:]]*//p' "$SETTINGS" | head -n 1 | tr -d '[:space:]')
    fi

    if [[ -n "$UUID" ]] && ! nmcli -t -f UUID connection show --active 2>/dev/null | grep -Fxq "$UUID"; then
        echo "watchdog: bringing up $UUID" >&2
        if [[ "${VPN_WATCHDOG_DRY_RUN:-}" == 1 ]]; then
            echo "nmcli connection up uuid $UUID"
        else
            nmcli connection up uuid "$UUID" || true
        fi
    fi

    [[ "${VPN_WATCHDOG_ONCE:-}" == 1 ]] && break
    sleep "$STEP"
done
