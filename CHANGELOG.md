# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] - 2026-11-19

### Added

- **Hiddify backend.** The tray Settings window can select `Hiddify` (its VPN mode, the system service
  `HiddifyAdminBG.service`) instead of a NetworkManager profile. The tray state, the "VPN drops" window,
  the statistics and the watchdog follow the Hiddify tunnel. The settings file gets `[vpn] backend`
  (`nm` or `hiddify`) and the optional `[hiddify] interface` (default `tun0`).
- `vpn-killswitch-hiddify.service`: refreshes the kill switch rule for the Hiddify service each time the
  service starts.
- `vpn-stats --hiddify`, the `Kill switch mode` row of its "Now" table and the Hiddify labels of `--events`.
- A test suite in `tests/` (not installed).
- `CHANGELOG.md`.
- Screenshots and a "Status and limitations" section in the README.

### Changed

- The VPN server addresses are no longer written to a generated file
  (`/etc/nftables.d/killswitch-server.nft`). The kill switch has an empty chain `vpn_allow`, which
  `vpn-killswitch-server` fills at runtime (`set`, `set-hiddify`, `apply`, `refresh`, `show`); the state is kept
  in `/etc/vpn-killswitch/` (`mode`, `servers`, `addresses`).
- The kill switch loads even when nothing is configured. With an empty `vpn_allow` chain, everything outside the
  tunnel interfaces is blocked, except the local network.
- The watchdog idles with the Hiddify backend: Hiddify reconnects by itself.
- Upgrading from 1.0.x is automatic: the addresses of the old generated file are moved to
  `/etc/vpn-killswitch/addresses`, the mode becomes `servers`, the old file is removed and the rules are reloaded.
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

[Unreleased]: https://github.com/localhost1982/vpn-kill-switch/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/localhost1982/vpn-kill-switch/releases/tag/v1.1.0
[1.0.0]: https://github.com/localhost1982/vpn-kill-switch/releases/tag/v1.0.0
