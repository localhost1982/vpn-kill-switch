<!-- SPDX-License-Identifier: MIT -->
# Tests

A repeatable test suite for the tray, the root helper, the watchdog, the statistics script and the
packaging files. Run everything with:

```
tests/run-all.sh
```

It prints one line per test (`ok`, `FAIL` or `SKIP`), then a summary, and exits non-zero if anything
failed. It takes a few seconds. A single test can be run on its own, e.g. `python3 tests/test_units.py`
or `bash tests/test_stats.sh`; each one prints `ALL OK (N checks)` at the end and exits non-zero on a
failure (`77` means "skipped").

## Requirements

- Python 3 with `python3-gi` and the GIR bindings the tray itself needs (Gtk 3, NM, AyatanaAppIndicator3;
  `gir1.2-notify-0.7` is not needed, libnotify is replaced by a fake).
- A graphical session for `test_settings_window.py` and `test_windows.py` (`DISPLAY` set; the suite
  forces `GDK_BACKEND=x11`). Without `DISPLAY` they are reported as `SKIP (no DISPLAY)`. Those two tests
  open and close real GTK windows, so windows flash on the screen for a moment while they run.
- `bash`, `awk`, `grep`, `sed`, `find`. Optional: `nft` and `unshare` (user namespaces) for
  `test_helper.sh` and the nftables check of `test_syntax.sh`, `visudo` for the sudoers check, `dash`
  for the maintainer scripts. A missing optional tool skips that part with a note.

## What the tests never do

- They never use `sudo`, `systemctl`, `nmcli` or `journalctl` against the real system, and never start,
  stop or change a VPN or Hiddify. The tray's `run`, `run_async`, `subprocess`, `shutil` and libnotify
  are replaced by recording stubs; the shell tests put stub `nmcli`, `sudo` and `systemctl` programs
  first in `PATH`. (The only real processes are harmless ones, such as `sh -c 'echo ...'`, used to
  test `run()` / `run_async()`.)
- They never read or write the real `~/.config/vpn-killswitch/settings.conf`: `XDG_CONFIG_HOME` is set
  to a fresh temporary directory before the tray module is imported (and the import is refused if the
  settings path ends up elsewhere).
- They never touch the live nftables table: the kill switch file and the helper are loaded only inside
  `unshare -rn` (a private user and network namespace that vanishes with the test), with the helper's
  state directory under a temporary `VKS_PREFIX`.
- They never run the real tray (no second indicator appears), and leave no `__pycache__` behind
  (`PYTHONDONTWRITEBYTECODE=1`, `sys.dont_write_bytecode`); `run-all.sh` checks that at the end.
- Temporary directories are made with `mktemp -d` under `${TMPDIR:-/tmp}` and removed on exit only if the
  path is really one of those.

## Files

| File | Covers |
|---|---|
| `run-all.sh` | Runs everything, summary, exit status; the `repo-clean` check. |
| `common.py` | Shared helpers: loads `tray/vpn-tray.py` (hyphenated name) with a temporary `XDG_CONFIG_HOME` (and `TZ=UTC`); fake NetworkManager client / connection / active connection / device; fake menu items and indicator; `make_tray()` (a `Tray` without its constructor); stubs for `run`, `run_async`, `subprocess`, `shutil`, libnotify; a controllable clock; GTK loop pumps; settings helpers. |
| `lib.sh` | Shell helpers: temporary directory with a safe cleanup trap, assertions, `skip`, `finish`. |
| `test_units.py` | Pure functions: `fmt_duration`, `plural_packets`, `host_from_endpoint`, `vpn_server_hosts`, `parse_drop_line` (TCP/UDP/ICMP, forward chain, glued old prefix), `fmt_dst`, `fmt_user`, `fmt_time`, `group_drops`, `drop_event`; the settings file (`load_*`, `save_settings` keeping every other section, garbage and missing files, a failed write); `app_version` / `installed` / `about_paths` for a checkout and a simulated installed layout; `count_logged_drops`, `hiddify_available`, `run`, `run_async` (also with a missing program), `vpn_connections`. |
| `test_vpn_events.py` | `vpn_events()`, the pppd / NetworkManager logic, on `fixtures/nm-pppd.journal` and small journals: LCP, command and "other" drops, duplicates of one pppd PID, the 5 s suspend window, suspended time inside an outage, failed connects, no-agent errors, ongoing outages with the VPN down or up, other profiles' lines, profile names with regex characters, every summary field. |
| `test_hiddify_events.py` | `hiddify_events()` on `fixtures/hiddify.journal` (the shared contract fixture with its expected rows and summary), marker de-duplication, flap debouncing, causes, boot / suspend / outage corners. |
| `test_tray_state.py` | Tray labels and behaviour with fake NetworkManager objects for both backends (not selected, profile not found, connected with duration, connecting, disconnected, NetworkManager unavailable, state subscriptions); Reconnect VPN; drop notifications (one popup and one sound per drop, the reconnect updates the same notification silently, 30 s sound limit, the tray's own reconnect / profile change, sleep and the 20 s quiet period, popup / sound off, kill switch OFF text, `pw-play` fallback, failures); the kill switch line; the VPN drops line for both backends; timers. |
| `test_settings_window.py` | The Settings window: combo contents and preselection; Save with nothing changed; the order helper -> settings file -> `nmcli down` -> `nmcli up` -> `systemctl` -> local settings; helper failures and their messages; kill switch toggle; watchdog and notification switches; a failed kill switch step and its retry; the Hiddify backend (entry only when available, both switch orders with `set-hiddify`, the greyed watchdog row). Needs a display. |
| `test_windows.py` | `DropsWindow` (grouped / raw views, statuses, all six periods, Escape, cached host names), `VpnDropsWindow` for both backends (rows, bold drops, statuses, queries, no profile selected, sorting), `build_about_dialog()`, and the Tray opening one window at a time. Needs a display. |
| `test_stats.sh` | `vpn-stats.sh` on the Hiddify and the NetworkManager fixtures (`VPN_STATS_JOURNAL_FILE`, `VPN_STATS_NOW`, `VPN_STATS_CONNECTED`): every table row, `--events`, hours parsing, `--hiddify` with `--vpn` (exit 2), `--help`, error exits. The numbers agree with the Python implementation. |
| `test_helper.sh` | `vpn-killswitch-server.sh` and `killswitch.nft` inside `unshare -rn`: loading the table, `set` (IPv4 literals, a host name, leading zeros), `apply` idempotence, `refresh`, corrupted state files, invalid hosts (exit 2, nothing written), `set-hiddify` with an absent cgroup, with the dry run, and with the real Hiddify cgroup when the service runs here, the table not loaded (exit 0), usage errors. |
| `test_watchdog.sh` | `watchdog/vpn-watchdog.sh` in dry-run / single-iteration mode: nm with an inactive or active profile, watchdog disabled (every spelling), backend hiddify, the uuid after other sections, comments, missing files. |
| `test_syntax.sh` | `bash -n` of every `.sh`, the maintainer scripts with their own shell, byte-compiling the Python files, `visudo -cf packaging/sudoers`, loading `killswitch.nft` in a private namespace, no `@` placeholders in unit files, SPDX headers, `VERSION`. |
| `fixtures/` | `hiddify.journal` (the contract fixture), `nm-pppd.journal` (a synthetic pppd / NetworkManager journal for the profile "My VPN"), `ks-drop.journal` (kill switch kernel log lines). Journal format: `journalctl -o short-unix`. |

## Notes

- Times in the tests are UTC (`TZ=UTC` is set by `common.py` and by `test_stats.sh`), so formatted
  times do not depend on the machine.
- To check that a test can fail, run one against a deliberately broken copy of the tray:
  `VPNKS_TRAY_PATH=/path/to/broken/vpn-tray.py python3 tests/test_tray_state.py` (the tests that look at
  the checkout layout will then complain too).
