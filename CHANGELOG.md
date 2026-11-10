# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `CHANGELOG.md`.
- Screenshots and a "Status and limitations" section in the README.

### Changed

- The package also installs the screenshots and the changelog next to the README in
  `/usr/share/doc/vpn-kill-switch/`.

## [1.0.0] - 2026-11-10

First packaged release:
[vpn-kill-switch_1.0.0_all.deb](https://github.com/localhost1982/vpn-kill-switch/releases/download/v1.0.0/vpn-kill-switch_1.0.0_all.deb).

### Added

- **Kill switch.** An nftables table that lets traffic out only through the VPN tunnel
  (`ppp*`, `tun*`, `tap*`, `wg*`), to the VPN server, to the local network and for DHCP.
  Dropped packets are logged to the kernel journal (rate-limited).
- **VPN profile selection.** Any NetworkManager VPN or WireGuard profile can be chosen in the
  tray Settings window; the allowed VPN server addresses follow the selected profile through
  the `vpn-killswitch-server` helper.
- **Watchdog.** A user service that reconnects the selected profile when it drops; it can be
  switched off in Settings.
- **Tray icon** for GNOME (AppIndicator): VPN state and connection time, uptime, the number of
  dropped packets, the number of VPN drops, `Reconnect VPN`, `Settings…`, `About`.
- **Dropped packets window**: what the kill switch blocked, grouped by destination, with
  periods and optional host name resolution.
- **VPN drops window**: the chronology of drops and reconnections with the cause and the
  downtime.
- **Drop notifications**: a notification and a sound when the VPN drops, replaced by a
  "reconnected" notification when it is back.
- **`vpn-stats`**: drop and connection statistics from the system journal as Markdown tables.
- **Debian package** `vpn-kill-switch`: dependencies, systemd units, passwordless rights for
  the `sudo` group for four specific commands, migration from an earlier source install.

[Unreleased]: https://github.com/localhost1982/vpn-kill-switch/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/localhost1982/vpn-kill-switch/releases/tag/v1.0.0
