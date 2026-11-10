#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# VPN watchdog: keeps the VPN profile selected in the tray Settings connected.
# Installed as /usr/lib/vpn-kill-switch/vpn-watchdog.sh and started by the user unit
# vpn-watchdog.service (enabled for all users by the package).
# Every 3 seconds it reads $XDG_CONFIG_HOME/vpn-killswitch/settings.conf (default
# ~/.config/...) and, if the profile whose UUID is in [vpn] uuid is not among the active
# NetworkManager connections, brings it up. No profile selected = nothing to do.
# The watchdog can be switched off per user with [watchdog] enabled = false in the same
# file (false/no/0/off; anything else or a missing key means enabled). The tray Settings
# window has a "Watchdog" switch that writes this setting. While it is off the script
# only sleeps and does not touch NetworkManager.
# Environment (for tests): VPN_WATCHDOG_DRY_RUN=1 prints the nmcli command instead of
# running it; VPN_WATCHDOG_ONCE=1 does a single iteration.

SETTINGS="${XDG_CONFIG_HOME:-$HOME/.config}/vpn-killswitch/settings.conf"
STEP=3

# ini_get SECTION KEY: the value of KEY in [SECTION] of the settings file (first match,
# surrounding whitespace removed); empty if missing or the file is unreadable.
ini_get() {
    [[ -r "$SETTINGS" ]] || return 0
    awk -v want_sec="$1" -v want_key="$2" '
        /^[[:space:]]*\[/ {
            line = $0
            sub(/^[[:space:]]*\[[[:space:]]*/, "", line)
            sub(/[[:space:]]*\].*$/, "", line)
            sec = line
            next
        }
        sec == want_sec && /^[[:space:]]*[^#;[:space:]][^=]*=/ {
            key = $0
            sub(/[[:space:]]*=.*$/, "", key)
            sub(/^[[:space:]]+/, "", key)
            if (key == want_key) {
                val = $0
                sub(/^[^=]*=[[:space:]]*/, "", val)
                gsub(/[[:space:]]+$/, "", val)
                print val
                exit
            }
        }
    ' "$SETTINGS"
}

while true; do
    ENABLED=$(ini_get watchdog enabled | tr '[:upper:]' '[:lower:]')
    case "$ENABLED" in
        false|no|0|off) ;;  # switched off in the tray Settings: nothing to do this time
        *)
            UUID=$(ini_get vpn uuid | tr -d '[:space:]')
            if [[ -n "$UUID" ]] && ! nmcli -t -f UUID connection show --active 2>/dev/null | grep -Fxq "$UUID"; then
                echo "watchdog: bringing up $UUID" >&2
                if [[ "${VPN_WATCHDOG_DRY_RUN:-}" == 1 ]]; then
                    echo "nmcli connection up uuid $UUID"
                else
                    nmcli connection up uuid "$UUID" || true
                fi
            fi
            ;;
    esac

    [[ "${VPN_WATCHDOG_ONCE:-}" == 1 ]] && break
    sleep "$STEP"
done
