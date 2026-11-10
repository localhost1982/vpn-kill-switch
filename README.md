# VPN kill switch for NetworkManager VPN profiles

Blocks internet access that bypasses the VPN. While the VPN tunnel is down or
not yet established, the only traffic that leaves the machine is what the VPN itself
needs to come up, plus local-network traffic. Everything else is dropped.

Works with any NetworkManager VPN profile: L2TP/IPsec, PPTP, OpenVPN, WireGuard, IKEv2
and so on. The profile is not hard-coded: you choose it in the Settings window of the
tray icon, and that choice drives the tray, the watchdog, the statistics script and the
VPN server addresses the kill switch lets through.

Built for Ubuntu with NetworkManager. Implementation: a single nftables table, a systemd
unit that loads it at boot and a small root helper that keeps the VPN server addresses
up to date.

Repository: <https://github.com/localhost1982/vpn-kill-switch>

## Contents

| File | Purpose |
|---|---|
| `killswitch.nft` | Generic nftables rules. Installed to `/etc/nftables.d/killswitch.nft`. |
| `vpn-killswitch.service` | systemd unit that loads the rules before the network comes up. Installed to `/etc/systemd/system/`. |
| `vpn-killswitch-server.sh` | Root helper that resolves the VPN server host names and writes the allowed server addresses. Installed to `/usr/local/sbin/vpn-killswitch-server`. |
| `LICENSE` | The MIT License. |
| `install.sh` | Installation and enabling. Offers a menu of the NetworkManager VPN profiles (or `--vpn NAME`) and sets up the kill switch, the tray and the watchdog for the chosen one. |
| `uninstall.sh` | Disabling and complete removal. |
| `vpn-stats.sh` | Statistics of VPN drops and internet outages of the selected profile for the last N hours. |
| `tray/vpn-tray.py` | VPN icon in the GNOME top bar with a statistics menu, VPN drop notifications and the Settings window (VPN profile, kill switch, watchdog, notifications). |
| `tray/vpn-tray.service` | `systemd --user` unit that starts the icon after you log in to the session. |
| `tray/install-tray.sh` | Dependency check, sudoers check, installation and enabling of the icon unit. |
| `tray/uninstall-tray.sh` | Stops and removes the icon unit. |
| `watchdog/vpn-watchdog.sh` | Keeps the selected VPN profile connected: checks every 3 s and brings it up if it is down. |
| `watchdog/vpn-watchdog.service` | `systemd --user` unit that runs the watchdog. |
| `watchdog/install-watchdog.sh` | Installation and enabling of the watchdog unit. |
| `watchdog/uninstall-watchdog.sh` | Stops and removes the watchdog unit. |

## Why you need it

A VPN has two unpleasant failure modes.

1. **The tunnel "hangs".** The tunnel interface (for example `ppp1`) is alive and the
   default route points into it, but the server is silent. Traffic goes into the void.
   This is inconvenient, but nothing leaks out.
2. **The tunnel dies and NetworkManager notices.** It removes the tunnel interface, and the default
   route falls back to Wi-Fi. From then on all traffic, DNS included, goes straight
   to the internet until the VPN is reconnected by hand. This is the leak.

The kill switch covers the second case: even when the default route points
to Wi-Fi, the rules let nothing out except VPN service packets.

## How it works

The rules live in a separate table, `inet killswitch`, with two chains. They are generic:
nothing in them depends on a particular VPN profile or protocol.

**The `output` chain** (the laptop's own traffic). The following pass without restrictions:

- `lo`;
- the tunnel interfaces `ppp*`, `tun*`, `tap*` and `wg*` (L2TP/IPsec, PPTP, OpenVPN,
  WireGuard and so on);
- `docker0`, `br-*`, `veth*`, that is, Docker.

For any other interface (Wi-Fi, Ethernet, USB modem, anything that appears later)
only the following is allowed:

- DHCP (UDP 68 → 67);
- any traffic to the VPN server addresses (the `vpn_server` set): IKE/ESP, L2TP, PPTP/GRE,
  OpenVPN, WireGuard. This is a single rule, `ip daddr @vpn_server accept`, so no
  protocol-specific ports are needed. The set holds IPv4 addresses only;
- local networks: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `224.0.0.0/4`;
- IPv6 link-local (`fe80::/10`, `ff00::/8`), so that NDP works.

Everything else is dropped by the `counter drop` rule. Its counter shows
how many packets have been blocked.

Before each `drop` there is a `log` rule: `limit rate 10/second burst 20 packets log prefix "KS-DROP: "
flags skuid level info`. It writes the dropped packets to the kernel log (and so to the journal),
at most 10 lines per second (a burst of 20), so a flood of blocked packets cannot fill the journal.
The counter is not limited and stays exact. For the `output` chain the line also carries the UID of
the sending user (`flags skuid`). To see what was blocked, use the tray's "Dropped packets" window
(see "Tray icon") or:

```bash
journalctl -k --grep KS-DROP
```

**The `forward` chain** (traffic the laptop forwards, i.e. Docker containers).
Containers may go out through the tunnel interfaces, talk to each other over the Docker bridges and reach
the local network. They cannot reach the internet directly, bypassing the VPN. Its `drop` is preceded by
the same kind of rate-limited `log` rule, with the prefix `KS-DROP-FWD: ` (no UID: the sender is a container).

The table does not depend on whether NetworkManager noticed the VPN failure. The rules
are static and always in effect.

### Where the VPN server addresses come from

`killswitch.nft` starts with `include "/etc/nftables.d/killswitch-server.nft"`, a generated
file with a single line of the form `define vpn_servers = { 203.0.113.10 }`, and builds the
`vpn_server` set from `$vpn_servers`. That file must not be edited by hand. It is written by
the helper `/usr/local/sbin/vpn-killswitch-server` (installed from `vpn-killswitch-server.sh`),
which runs as root:

```
vpn-killswitch-server set HOST [HOST...]   resolve the VPN server host(s) and update the kill switch
vpn-killswitch-server refresh              re-resolve the saved hosts and update the kill switch
vpn-killswitch-server show                 show the saved hosts and the current define line
```

- `HOST` is a host name or an IPv4 address. The hosts are validated, all of them are
  resolved first, and nothing is written if any host cannot be resolved.
- The saved hosts (one per line) are kept in `/etc/vpn-killswitch/servers`, the resolved
  addresses in `/etc/nftables.d/killswitch-server.nft`. `set` replaces the whole list.
- The new rules are checked with `nft -c`. If nft rejects them, the previous files are
  restored. If `vpn-killswitch.service` is active, it is reloaded; otherwise the helper
  prints `Kill switch is not active; files updated, rules will apply on next start.`
- Exit codes: 0 ok, 1 general error, 2 invalid usage or host, 3 cannot resolve a host,
  4 nft rejected the new rules, 5 the service reload failed.
- For testing without root: `VKS_PREFIX=/some/dir` is prepended to all `/etc` paths and
  `VKS_DRY_RUN=1` skips the root check, `nft -c` and `systemctl`.

The server addresses are updated in two ways:

- on install, `./install.sh` calls `set` with the server hosts of the VPN profile you pick (or with the hosts given by `--server`);
- when you pick another VPN profile and press `Save` in the tray Settings window, the tray
  calls `set` with the server hosts of that profile (see "Settings").

Host names are resolved when `set` runs. If the server's IP address changes later,
run `sudo vpn-killswitch-server refresh` (see "Changing the rules").

### Why not `nftables.service`

The stock `/etc/nftables.conf` starts with `flush ruleset`. If you enable
`nftables.service`, it wipes the Docker rules (Docker works through
iptables-nft) and ufw at boot. That is why a dedicated unit is used: it loads only
its own table and leaves the others alone. The rules file is idempotent: on every load
the `inet killswitch` table is recreated, and all other tables stay as they were.

## Requirements

- nftables (`nft`), usually already installed in Ubuntu;
- systemd;
- a VPN through NetworkManager with a tunnel interface named `ppp*`, `tun*`, `tap*` or `wg*`
  (nm-l2tp, nm-pptp, OpenVPN, WireGuard, strongSwan IKEv2 with a virtual interface and so on).
  If your VPN uses another interface name, add it to the `oifname` lists in both chains of
  `killswitch.nft`;
- ufw is disabled or does not interfere (the kill switch table works independently of it).

## Installation

Get the sources. The tray and the watchdog run from this directory, so keep it in a
permanent place:

```bash
git clone https://github.com/localhost1982/vpn-kill-switch.git
```

```bash
cd vpn-kill-switch
```

### 1. The kill switch

```bash
./install.sh
```

The script re-runs itself through `sudo` and shows a menu of the VPN and WireGuard profiles
that NetworkManager knows, sorted by name. Each line reads `NAME  (type, server host)`:

```
Select the VPN profile for the kill switch, the tray and the watchdog:
  Home WireGuard  (wireguard, wg.example.net)
  Office L2TP  (vpn, vpn.example.com)
  Office OpenVPN  (vpn, vpn.example.com)
```

Move with `Up`/`Down` (or `k`/`j`) and press `Enter`; no typing is needed. The current line is
highlighted. `Esc`, `q` or `Ctrl-C` abort the installation (exit code 130). If a profile was
already saved in the settings file, it is preselected.

For the chosen profile the script:

1. finds the VPN server host(s) the same way the tray does (see "Settings"): for a VPN profile the
   first non-empty of `gateway`, `remote`, `address`; for a WireGuard profile the peer endpoints
   (read from NetworkManager over D-Bus, because `nmcli` does not show them). If no host can be
   determined it stops with `Cannot determine the VPN server of profile NAME; run with --server HOST.`;
2. copies the rules, the unit and the helper, resolves the server host(s), checks the rules syntax
   (`vpn-killswitch-server set HOST...`), enables the service and shows the loaded table;
3. saves the profile UUID for the tray, the watchdog and the stats script to
   `~/.config/vpn-killswitch/settings.conf` of the user who ran `sudo` (the directory is created
   with mode 0700, the file is owned by that user; see "Settings"). It prints
   `Selected VPN profile: NAME (UUID)` and `Settings written to <path>`;
4. restarts the tray (`systemctl --user try-restart vpn-tray.service` as that user) if it is
   running, so that it picks up the new profile, and says whether it did. The watchdog reads
   the file every few seconds and needs no restart.

If the script is not started through `sudo` from a user account (for example from a root shell),
the settings file is not written and a note says so; the kill switch itself is still installed.

Options:

- `--vpn NAME_OR_UUID`: choose the profile without the menu, by its exact name or UUID. Use it in
  scripts and over a connection without a terminal;
- `--server HOST`: the host name or IPv4 address that must stay reachable outside the tunnel
  (repeatable: `--server HOST --server HOST`). It overrides the servers found in the profile. On its
  own (without `--vpn`) it skips the menu and writes no profile: this is the old behaviour;
- `--dry-run`: does not use `sudo` and changes nothing. It lists the profiles, shows the menu (or uses
  `--vpn`), resolves the servers and prints every command it would run (`install`, the helper `set`,
  the settings file, `systemctl`) with a `[dry-run]` prefix;
- `-h`, `--help`: help (needs no `sudo`).

Without a terminal and without `--vpn` or `--server` the script stops with
`No terminal for the profile menu; run with --vpn NAME or --server HOST.` If NetworkManager has no
VPN profile it stops with
`No VPN profiles found in NetworkManager; create one first or run with --server HOST.`

To re-install (for example after editing `killswitch.nft`), run `./install.sh` again and press
`Enter` on the preselected profile, or run `./install.sh --vpn NAME`. The server list is replaced by
the servers of the chosen profile.

To find the current address of a host:

```bash
getent hosts <vpn-gateway-host>
```

### 2. The tray icon

```bash
./tray/install-tray.sh
```

See "Tray icon" below for the requirements. The VPN profile picked in step 1 is already
selected; open `Settings…` in the tray menu to change it.

### 3. The watchdog

```bash
./watchdog/install-watchdog.sh
```

See "Watchdog" below. If you already run a watchdog of your own for the same VPN, disable it
first: two watchdogs would fight each other.

### 4. The sudoers rules

The tray runs a few commands through `sudo -n` (it must never ask for a password), and
`vpn-stats.sh` reads the nftables counter through `sudo -n` too. Create one sudoers file with all
four rules (replace `youruser` with your user name):

```bash
sudo visudo -f /etc/sudoers.d/vpn-stats
```

```
youruser ALL=(root) NOPASSWD: /usr/sbin/nft list chain inet killswitch output
youruser ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now vpn-killswitch.service
youruser ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now vpn-killswitch.service
youruser ALL=(root) NOPASSWD: /usr/local/sbin/vpn-killswitch-server *
```

What each line enables:

1. `nft list chain inet killswitch output`: reading the exact nftables blocked-packet counter. Only
   `vpn-stats.sh` needs it (the "Blocked by kill switch, packets" row); the tray does not use it:
   the `Kill switch: …` menu line counts the packets logged in the kernel journal. The rule allows
   exactly one command. Without it `vpn-stats.sh` shows `n/a (sudo required)` in that row and
   everything else works.
2. `systemctl enable --now vpn-killswitch.service`: turning the kill switch on with the toggle
   in Settings.
3. `systemctl disable --now vpn-killswitch.service`: turning it off with the toggle.
4. `vpn-killswitch-server *`: changing the VPN profile in Settings (the tray runs the helper's
   `set` command). It also lets you run `sudo vpn-killswitch-server refresh` without a password.
   The helper validates every host argument, but note that this line lets the user change which
   servers are reachable outside the tunnel.

`install-tray.sh` checks these permissions and prints a warning with the missing lines.
Without lines 2-3 or 4, `Save` in Settings does not work (see "Settings").

To also read the `forward` chain (dropped traffic of Docker containers), add one more
line to the same file:

```
youruser ALL=(root) NOPASSWD: /usr/sbin/nft list chain inet killswitch forward
```

Currently the icon does not read this chain; the line is only needed for future
use or for manual queries.

## Verification

With the VPN **off**:

```bash
curl -m 5 https://example.com
```

It should fail with a timeout.

```bash
ping -c 2 192.168.100.1
```

The router should respond (the local network is allowed).

Then connect the VPN and repeat the `curl`. It should work.

To view the rules and the blocked-packet counters:

```bash
sudo nft list table inet killswitch
```

To see which VPN servers are allowed:

```bash
sudo vpn-killswitch-server show
```

Service status:

```bash
systemctl status vpn-killswitch.service
```

## Removal

```bash
./uninstall.sh
```

Stops the service, deletes the table and erases the files: the rules and the unit, the helper
`/usr/local/sbin/vpn-killswitch-server`, the generated `/etc/nftables.d/killswitch-server.nft`
and the folder `/etc/vpn-killswitch/` with the saved servers. Traffic
immediately starts flowing without restrictions.

The tray and the watchdog are user-level and are removed separately, as a regular user:

```bash
./tray/uninstall-tray.sh
./watchdog/uninstall-watchdog.sh
```

None of the scripts touches the settings file `~/.config/vpn-killswitch/settings.conf`
(the selected VPN profile) or your sudoers file. To remove the settings:

```bash
rm -r ~/.config/vpn-killswitch
```

To disable the kill switch temporarily without removing it:

```bash
sudo systemctl stop vpn-killswitch.service
```

To enable it again:

```bash
sudo systemctl start vpn-killswitch.service
```

## Changing the rules

Edit `killswitch.nft` in this folder and run `./install.sh` again.
Or edit `/etc/nftables.d/killswitch.nft` directly and reload:

```bash
sudo systemctl reload vpn-killswitch.service
```

Do not edit `/etc/nftables.d/killswitch-server.nft`: it is generated by the helper.

Typical changes:

- **The VPN server IP changed.** Run `sudo vpn-killswitch-server refresh`: it re-resolves the
  saved hosts and reloads the rules. To change the servers themselves, run
  `sudo vpn-killswitch-server set HOST [HOST...]` (or pick the profile in the tray Settings).
  Several hosts are allowed.
- **A tunnel interface with another name.** Add it to the `oifname { "ppp*", "tun*", "tap*", "wg*" }`
  lists in both chains. No protocol-specific ports are needed: all traffic to the server is allowed.
- **Disallow the local network.** Remove the `ip daddr @lan accept` line. You will then
  need DNS for the VPN server name: add the line
  `<server IP> <vpn-gateway-host>` to `/etc/hosts`. The router, printers and local
  development will become unreachable without the VPN.
- **Allow something specific to bypass the VPN**, for example NTP: add a line such as
  `udp dport 123 accept` before `counter drop`.

## Things to know

- **The network icon shows "limited connectivity"** even when the VPN works.
  NetworkManager checks the internet for each interface separately: the request
  to `connectivity-check.ubuntu.com` (TCP 80) goes out over Wi-Fi, bypassing the tunnel, and is blocked.
  These make up almost all the packets in the `drop` counter: 8 SYNs every five minutes.
  Nothing leaks. To get rid of both the icon state and these packets, disable the check:

  ```bash
  printf '[connectivity]\nenabled=false\n' | sudo tee /etc/NetworkManager/conf.d/90-no-connectivity-check.conf >/dev/null && sudo nmcli general reload conf
  ```

  Side effect: NetworkManager will stop detecting captive portals, but
  with the kill switch they would not open anyway.
- **The kernel journal grows while the VPN is down.** Every dropped packet is logged, but the `log`
  rules are rate-limited (10/s, burst 20), so the journal grows by at most 10 lines per second while
  the VPN is down, and not at all while traffic flows through the tunnel.
- **DNS while the VPN is off.** The local network is allowed, so application queries
  go to the local resolver (usually the router). This is needed to resolve the VPN
  server name before connecting. If such a leak is unacceptable, see "Disallow the local network".
- **Public Wi-Fi with a captive portal.** The login page lies outside the local
  network and will be blocked. Stop the service while you log in, then start it again.
- **A network whose resolver is not in a private range.** The VPN server name will not resolve
  and the VPN will not connect. Solution: an entry in `/etc/hosts`.
- **VPN autoconnect.** The kill switch does not reconnect the VPN by itself. To make the VPN
  come up automatically after login and after sleep, either install the watchdog (see "Watchdog"),
  which follows the profile selected in Settings, or set autoconnect on the profile:

  ```bash
  nmcli connection modify "My VPN" connection.autoconnect yes
  ```

- **Dead tunnel detection.** By default pppd does not check whether the link is alive,
  and a hung VPN stays hung forever. For pppd-based profiles (L2TP, PPTP), to make NetworkManager
  drop it on its own after about half a minute:

  ```bash
  nmcli connection modify "My VPN" +vpn.data lcp-echo-interval=10 +vpn.data lcp-echo-failure=3
  ```

  Together with autoconnect or the watchdog this gives automatic VPN recovery, and the kill switch
  covers the window between the drop and the recovery.

## Drop statistics

```bash
./vpn-stats.sh [hours] [--events] [--vpn NAME]
```

The script reads the systemd journal for the last N hours and prints two Markdown tables.

**The profile** is the one selected in the tray Settings, read from
`~/.config/vpn-killswitch/settings.conf` (`[vpn] uuid = ...`; `$XDG_CONFIG_HOME` is respected).
`--vpn NAME` takes another NetworkManager profile by its name instead, for example to look at its
history. If neither is available, the script exits with
`No VPN profile selected. Choose one in the tray Settings window or pass --vpn NAME.`

**Drop causes are detected from pppd.** The causes below (LCP, on command, other) and the drop
counts come from pppd messages, so they exist only for pppd-based profiles (L2TP, PPTP). For other
VPN types (OpenVPN, WireGuard, IKEv2 and so on) only the connection events are counted: attempts,
successful and failed connections.

**"Statistics"** is built from journal events:

| Rows | What they are based on |
|---|---|
| VPN drops, total | pppd `Connection terminated`. If there are fewer of them than the causes below, the sum of the causes is used. |
| server stopped responding (LCP) | `No response to 3 echo-requests`. |
| on command: sleep, nmcli down | `Terminating on signal`. NetworkManager sends it on `nmcli connection down` and on suspend. |
| other | the remainder: drops that have neither of the two causes above. |
| VPN connection attempts / Successful connections / Failed connections (timeout) | NetworkManager messages of the selected profile: `starting`, `policy: set '<profile>' ... as default`, `failed to connect`. |
| Password request errors (no agent) | `failed to request VPN secrets` (no password agent). |
| Total time without VPN / Longest outage | computed from the order of events: from a drop to the next successful connection. Suspend time is not counted. Nothing is counted before the first VPN event in the window. If the VPN is not connected right now, an unclosed outage is counted up to the current moment. |
| Hello from L2TP server | `avpMsgTypeHello` in the kl2tpd debug output, and the time of the last one. Only L2TP profiles produce it. |
| Wi-Fi: disconnects / Wi-Fi: resets after VPN failure | `state change: activated -> ...` and `secondary-connection-failed` on a `wl*` interface. |
| Suspends / NetworkManager restarts / System boots | `PM: suspend entry`, `Stopped NetworkManager.service`, `Linux version`. |
| Last drops | up to five most recent drops with the cause: `LCP`, `sleep`, `command` or `other`. |

**"Now"**: whether the selected profile is connected (the row is named after the profile),
the state of the kill switch (`Kill switch (vpn-killswitch.service)`), of the watchdog
(`Watchdog (vpn-watchdog.service)`) and the number of packets dropped by the kill switch.

**The period** is given in hours. The default is 1 hour. Both a decimal point and a decimal comma
are accepted. The step is half an hour: the value is rounded half up to the nearest 0.5 h
(0.7 gives 0.5, 0.75 gives 1, 1.2 gives 1, 1.3 gives 1.5). If the result is 0
or the value is not a number, 1 hour is used and an explanatory note is printed before the tables.

```bash
./vpn-stats.sh            # the last hour
./vpn-stats.sh 0.5        # the last half hour
./vpn-stats.sh 1,5        # an hour and a half
./vpn-stats.sh 3          # three hours
./vpn-stats.sh 6 --vpn "My VPN"   # another profile
```

The `--events` flag adds a chronology: connections, drops, suspend and wake,
NetworkManager restarts, boots, connection failures and password request failures.

```bash
./vpn-stats.sh 4 --events
```

`sudo` is not needed. The exception: the "Blocked by kill switch, packets" row reads the counter
through `sudo -n nft`. Without passwordless `sudo` it will show `n/a (sudo required)`.
The script never asks for a password.

That row is the exact nftables counter: it is not rate-limited, but it is reset to 0 on every rules
load (a reboot, `./install.sh`, a reload of the service). The tray shows a different number: the
packets logged in the kernel journal since boot (see "Tray icon"). The two may differ.

## Tray icon

A permanent indicator in the GNOME top bar: the icon shows whether the VPN is alive, and the menu
collects the statistics that you would otherwise have to open a terminal for. The VPN it shows
is the profile selected in the Settings window.

```bash
./tray/install-tray.sh
```

**Icon:**

| Icon | When |
|---|---|
| `org.gnome.Settings-network-symbolic` | the selected VPN profile is connected (the "Network" icon from GNOME Settings) |
| `network-wired-acquiring-symbolic` | the profile is connecting |
| `network-wired-disconnected-symbolic` | the profile is not connected, or no profile is selected |

**Menu** (the lines only display data and are refreshed when the menu is opened and by timers;
clicking the `Kill switch: …` line opens the "Dropped packets" window and clicking the
`VPN drops since boot: …` line opens the "VPN drops" window, see below):

- `VPN: connected 4h 12m (My VPN)` or `VPN: disconnected (My VPN)` (`My VPN` stands for the name of the selected
  profile; while connecting: `VPN: connecting… (My VPN)`). The time is counted
  from the last successful activation of the profile. If no profile is selected, the line reads
  `VPN: not selected`; if the selected profile was deleted from NetworkManager, `VPN: profile not found`;
  `VPN: state unknown (My VPN)` means that NetworkManager does not answer;
- `Uptime: 5h 01m`. System uptime;
- `Kill switch: active, 0 packets dropped`, `Kill switch: OFF` or
  `Kill switch: active, journal unavailable`. The number is the count of packets logged in the kernel
  journal since boot (the `KS-DROP` lines), identical to the `Since boot` period of the "Dropped
  packets" window. A rules reload does not reset it, but it counts only logged packets: the log is
  rate-limited to 10 lines per second, so during a burst the exact nftables counter (shown by
  `vpn-stats.sh`) can be higher. Clicking this line opens the "Dropped packets" window (see below);
- `VPN drops since boot: 1 (server silent: 0)`. The logic is the same as in `vpn-stats.sh`:
  the total is the larger of the number of `Connection terminated` and the sum of the causes, and the
  number in parentheses is the count of `No response to N echo-requests`. These are pppd
  messages, so for VPN types without pppd the line stays at 0. Clicking this line opens the
  "VPN drops" window (see below);
- `Reconnect VPN` brings the selected profile down (if it is active) and up again with `nmcli`, in the background
  (it is unavailable while no profile is selected);
- `Settings…` opens the settings window (see "Settings" below);
- `About` opens the About window: the program name, the version with the commit hash and date, a short description, the project and settings paths, the license and the author;
- `Quit` closes the icon (the unit is not restarted in that case, see below).

The icon writes durations in short forms: `12s`, `3m 05s`, `1h 02m`,
and for days `1d 03h`.

**Where the data comes from:**

| Indicator | Source | Refresh |
|---|---|---|
| VPN state | libnm (`NM.Client`), the active-connections change signal | on event and every 5 s |
| VPN uptime | `connection.timestamp` of the selected profile | when the menu is opened and every 5 s |
| System uptime | `/proc/uptime` | when the menu is opened and every 5 s |
| Kill switch enabled | `systemctl is-active vpn-killswitch.service` | when the menu is opened and every 60 s |
| Packets dropped | `journalctl -q -o short-unix --no-pager --grep ^KS-DROP -b _TRANSPORT=kernel`, the number of lines (no `sudo`) | when the menu is opened and every 60 s |
| VPN drops | `journalctl -b -q -o cat _COMM=pppd` | every 60 s |
| Dropped packets window | `journalctl -q -o short-unix --grep ^KS-DROP _TRANSPORT=kernel` | when the window is opened, on `Refresh` and when the period changes |
| VPN drops window | two `journalctl -q -o short-unix` queries: pppd and NetworkManager lines, then the kernel suspend, wake and boot lines | when the window is opened, on `Refresh` and when the period changes |

### Dropped packets window

A click on the `Kill switch: …` menu line opens the "Dropped packets" window
in the center of the screen. Choosing it again brings the open window to the front; Escape and `Close`
close it, and the icon keeps running. It shows which packets the kill switch blocked, taken from the
`KS-DROP` kernel log lines written by the `log` rules (see "How it works"). The window needs read
access to the kernel journal (the user must be in the `adm` or `systemd-journal` group).

- **Period**: `Last hour`, `Last 6 hours`, `Last 24 hours`, `This session` (since the tray icon
  was started), `Since boot` (the default) or `All time` (the whole persistent journal, all boots). The list is
  reloaded when the period changes. The longer periods read the persistent kernel journal, so they
  can reach back before the last boot;
- **Group identical flows** (on by default): one row per combination of protocol, destination
  (address and port, or ICMP type/code), interface, user and chain, with the number of logged packets
  and the time of the last one. The columns are `Last seen`, `Count`, `Proto`, `Destination`,
  `Interface`, `User`, `Host`. With the option off the table lists every logged packet:
  `Time`, `Proto`, `Source`, `Destination`, `Interface`, `User`, `Flags` (`SYN`, `ACK`, `FIN`, `RST`,
  `PSH`, `URG`, `DF`), `Chain` (`output` or `forward`), `Host`. The newest rows come first; click a
  column header to sort by it. Times are `HH:MM:SS` for today and `YYYY-MM-DD HH:MM:SS` otherwise.
  `User` is the user that sent the packet (the UID from the log line, shown as a name when it has one;
  empty for forwarded packets and for lines logged without a UID);
- **Resolve hostnames** (off by default): looks up the host name of every distinct destination address
  in the background (reverse DNS, about 2 s per address at most) and fills the `Host` column. The window
  never waits for it; results are cached while the window stays open. Note that this sends reverse DNS
  queries for the destinations to your resolver. Turning the option off clears the column;
- **Refresh** reloads the list. The controls are unavailable while it loads.

The line at the bottom reads, for example,
`27 packets logged in this period · logging is rate-limited to 10/s` (`1 packet` for a single one),
or `No dropped packets logged in this period`. While the list loads it reads `Loading…`; if the
journal cannot be read, `Failed to read the journal: …`.

With the `Since boot` period the number in this line is the same as the number in the
`Kill switch: …` menu line. Keep in mind that both count log lines, and the log is rate-limited to
10 lines per second, so during a burst the real number of dropped packets can be higher. The exact
nftables counter (`vpn-stats.sh`, `sudo nft list table inet killswitch`) is not shown in the tray; it
starts from zero whenever the rules are loaded, while the journal keeps its lines across reloads.
Packets dropped before the `log` rules were installed are not visible in the journal at all.

### VPN drops window

A click on the `VPN drops since boot: …` menu line opens the "VPN drops" window in the center of the
screen: the chronology of the drops and reconnections of the selected VPN profile. It shows when the
VPN dropped, why, when it came back and how long it was down. Choosing the line again brings the open
window to the front; Escape and `Close` close it. The window needs read access to the journal (the user
must be in the `adm` or `systemd-journal` group). If no profile is selected in Settings, it reads
`No VPN profile selected`.

- **Period**: the same as in the "Dropped packets" window: `Last hour`, `Last 6 hours`, `Last 24 hours`,
  `This session` (since the tray icon was started), `Since boot` (the default) or `All time` (the whole
  persistent journal). The list is reloaded
  when the period changes; **Refresh** reloads it, **Close** closes the window. The controls are
  unavailable while it loads;
- **Columns**: `Time` (`HH:MM:SS` for today, `YYYY-MM-DD HH:MM:SS` otherwise), `Event`, `Details` and
  `Downtime`. The newest rows come first; click a column header to sort by it. Drops are in bold.

**Events:**

| Event | Details | Journal line |
|---|---|---|
| `Drop` | `server stopped responding (LCP)` | pppd `No response to N echo-requests` |
| `Drop` | `on command` | pppd `Terminating on signal` (`nmcli connection down`, the watchdog, a manual stop) |
| `Drop` | `suspend` | the same line within 5 s of a suspend |
| `Drop` | `other` | pppd `Connection terminated.` without any of the causes above |
| `Connected` | `default route via ppp0` (the interface name) | NetworkManager `policy: set '<profile>' (ppp0) as default for IPv4` |
| `Connecting` | `starting` | NetworkManager `vpn[...,"<profile>"]: starting ...` |
| `Connect failed` | `timeout` | NetworkManager `vpn[...,"<profile>"]: failed to connect` |
| `Connect failed` | `no password agent` | NetworkManager `failed to request VPN secrets` |
| `Suspend`, `Wake` | | kernel `PM: suspend entry`, `PM: suspend exit` |
| `NetworkManager restart` | | systemd `Stopped NetworkManager.service` |
| `Boot` | | kernel `Linux version` |

The data comes from two journal queries one after the other: the lines of pppd and NetworkManager
(plus the systemd lines about `NetworkManager.service`), then the kernel suspend, wake and boot lines.
They are merged by time.

**Downtime** is filled in only for the drop that started an outage: the time from that drop to the next
successful connection (`Connected`), without the time the computer was suspended. While the VPN is
still down it reads `ongoing`. Further drops before the VPN is back belong to the same outage and
have an empty `Downtime`, like all the other events. A pppd message that repeats a drop already
listed (`Connection terminated.` after `No response` or `Terminating` of the same pppd process) is not
a new row. A drop with no `Connected` after it while the VPN is connected now (for example the old
tunnel closing after the new one came up) has an empty `Downtime` too.

The line at the bottom reads, for example,
`7 drops (server silent: 2) · total time without VPN: 8m 34s · longest: 3m 51s`
(`1 drop` for a single one), or `No VPN drops in this period` (the other events are still listed).
While the list loads it reads `Loading…`; if the journal cannot be read, `Failed to read the journal: …`.

The logic is the same as in `vpn-stats.sh` (see "Drop statistics"): the same patterns, the same drop
total (the larger of the number of `Connection terminated` and the sum of the causes, as in the menu
line), the same outage state machine for the downtime. The `Drop` rows are the individual drops, so in
rare cases their number can differ from the total in the bottom line. Drop causes exist only for pppd-based
profiles (L2TP, PPTP); for other VPN types the window lists only the connection events (`Connecting`,
`Connected`, `Connect failed`), suspends and boots.

### Drop notifications

When the selected VPN profile drops, the tray shows a GNOME notification and plays a sound. A drop is
the profile going from connected to anything else (disconnected, or connecting again), seen by the same
check that refreshes the icon (on libnm events and every 5 s). It is not reported when:

- the tray itself took the VPN down: `Reconnect VPN` or a profile change in Settings (for 15 s after
  the command);
- the computer is suspended or has just resumed: the VPN usually reconnects by itself, so drops
  during suspend and for 20 s after resume are ignored (logind `PrepareForSleep`);
- the tray started while the VPN was already down (there is no earlier connected state);
- no profile is selected, the profile was deleted, or NetworkManager does not answer.

The notification reads `VPN dropped` / `My VPN disconnected at 14:03:27. Traffic outside the VPN is blocked
by the kill switch.`; if the kill switch is not active (or its state is unknown) the second sentence is
`The kill switch is OFF: traffic is NOT protected.` The tray keeps one notification object, so a new
drop replaces the previous text instead of stacking banners. When the profile is connected again, the same
notification is replaced by `VPN reconnected` / `My VPN is back after 2m 05s.`, without a sound (only if
the popup was shown for that drop).

The sound is the `network-connectivity-lost` event of the freedesktop sound theme, played with
`canberra-gtk-play`; if that is not installed, `pw-play` plays `network-connectivity-lost.oga` from
`/usr/share/sounds/freedesktop/stereo/`. At most one sound is played per 30 s, so a flapping VPN does
not beep continuously (the notification is still updated). The sound is played by the tray itself, not
by the notification, so it does not depend on the banner: GNOME's Do Not Disturb hides the banner, and
the sound is still played (if you also turned off system sounds in GNOME Settings, `canberra-gtk-play`
follows that). Both parts can be switched off in Settings; without `gir1.2-notify-0.7` the tray runs without
the notification and only logs one line.

### Requirements

- the packages `python3-gi`, `gir1.2-gtk-3.0`, `gir1.2-nm-1.0`, `gir1.2-ayatanaappindicator3-0.1`
  (usually already installed in Ubuntu; `install-tray.sh` checks for them and, if any are missing,
  prints the `sudo apt install ...` command; it does not run `apt` itself);
- optional, for the VPN drop notification and sound (see "Drop notifications"): `gir1.2-notify-0.7`
  and `gnome-session-canberra` (`canberra-gtk-play`). Without them the tray works as before;
  `install-tray.sh` only warns about each missing one;
- the AppIndicator extension enabled in GNOME Shell (in Ubuntu this is
  `ubuntu-appindicators@ubuntu.com`). Without it the icon will not appear;
- at least one VPN or WireGuard profile in NetworkManager (it is chosen in Settings);
- the kill switch installed (otherwise the line will show `OFF`) and the sudoers rules
  from "Installation".

### Installation and removal

```bash
./tray/install-tray.sh
./tray/uninstall-tray.sh
```

Both scripts are run as a regular user, without `sudo`. Installation puts the unit
at `~/.config/systemd/user/vpn-tray.service` with the absolute path to `vpn-tray.py` from
this folder, then runs `systemctl --user daemon-reload` and `enable --now`. If the
repository is moved, run `install-tray.sh` again. Removal stops the
unit, disables it and deletes the file.

Autostart is provided by the unit: it is bound to `graphical-session.target` and starts
after you log in to GNOME. If the application crashes, systemd restarts it after 5 s.
Status and log:

```bash
systemctl --user status vpn-tray.service
journalctl --user -u vpn-tray.service
```

While the icon is running as a unit, a second instance started manually exits immediately.

## Settings

The `Settings…` menu item opens a separate "VPN kill-switch Settings" window in the center of
the screen (a second window is not created: choosing the item again brings the open one
to the front). Closing the window does not stop the icon. It contains five rows:

- **VPN connection**: a dropdown with all VPN and WireGuard profiles of NetworkManager, sorted by
  name. This is the profile that the tray, the watchdog, the statistics and the kill switch follow.
  If NetworkManager has no such profiles, the dropdown is disabled and shows
  `No VPN profiles in NetworkManager`;
- **Kill switch**: a toggle that enables or disables `vpn-killswitch.service`;
- **Watchdog** (`Reconnect the selected VPN profile automatically when it drops.`): a toggle that
  enables or disables the user unit `vpn-watchdog.service` (see "Watchdog"). It runs
  `systemctl --user enable --now vpn-watchdog.service` or `systemctl --user disable --now
  vpn-watchdog.service`, without `sudo`. The unit is enabled or disabled, not only started or stopped,
  so the choice persists across logins;
- **Drop notification** (`Show a notification when the VPN drops.`) and **Drop sound**
  (`Play a sound when the VPN drops.`): toggles for the two parts of "Drop notifications". Both are on
  by default. They run no command: `Save` writes them to the settings file and the tray applies
  them at once. They are written as the last step of `Save`, after the VPN profile, the kill switch and the
  watchdog; if one of those steps fails, they are saved when a retry succeeds.

The choice is stored in `~/.config/vpn-killswitch/settings.conf` (`$XDG_CONFIG_HOME` is respected):

```
[vpn]
uuid = 00000000-0000-0000-0000-000000000000

[notify]
popup = true
sound = true
```

The file holds the UUID of the profile, so renaming the profile does not matter. The `[notify]` section
is optional (a missing section or key means `true`). The tray and
`install.sh` (which saves the profile you pick in its menu) write the file, each changing only its own
part and keeping the rest (`install.sh` replaces just the `uuid` line, so your `[notify]` choices survive a
reinstall; the tray rewrites the file through a parser, which drops comments); the watchdog and
`vpn-stats.sh` read it. Until a profile is selected the file does
not exist, and the tray shows `VPN: not selected` with the `network-wired-disconnected-symbolic` icon
and a disabled `Reconnect VPN`; the watchdog does nothing.

The controls only select the desired state and do not run anything by themselves.
At the bottom right there are two buttons: `Cancel` and `Save` (Enter triggers `Save`).
`Cancel` (as well as Escape and the close button) closes the window without changes and without
running any commands. If nothing was changed, `Save` simply closes the window.

**What `Save` does.** The steps run one after another, and while they run the controls are
unavailable:

1. If the VPN connection was changed: the tray takes the server hosts of the new profile and runs
   `sudo -n /usr/local/sbin/vpn-killswitch-server set HOST...`. This updates the addresses that the
   kill switch lets through, before anything else is saved. Where the hosts come from: for a VPN
   profile the first non-empty of `gateway`, `remote`, `address` (OpenVPN may list several `remote`
   entries; `host:port` forms are reduced to the host); for a WireGuard profile the endpoints of all peers.
2. The selection is written to the settings file.
3. If the old profile is active, `nmcli connection down uuid OLD` is run and awaited. Then
   `nmcli connection up uuid NEW` is started in the background (it is not awaited, so the window
   does not wait for the VPN to come up).
4. If the kill switch toggle was changed: `sudo -n systemctl enable --now vpn-killswitch.service` (to
   enable) or `sudo -n systemctl disable --now vpn-killswitch.service` (to disable).
5. If the watchdog toggle was changed: `systemctl --user enable --now vpn-watchdog.service` (to
   enable) or `systemctl --user disable --now vpn-watchdog.service` (to disable). No `sudo`.

After a successful save the window closes.

**If something fails**, the window stays open, a hint appears under the rows, the controls
become available again, and after fixing the problem you can press `Save` again:

- Step 1 fails: nothing is saved and the old VPN is not touched. The messages are
  `Cannot determine the VPN server for this profile; the kill switch was not updated.` (the profile
  has no known server host), `Failed to update the kill switch server list: ...` (the helper's error
  text, for example that the host cannot be resolved), or, when `sudo` wants a password, the hint
  `No passwordless permission to change the VPN profile.` with the sudoers line to add.
- Step 2 fails (`Failed to save the settings: ...`): the VPN is not switched.
- Step 4 fails: the VPN change from steps 1-3 stays applied (the profile is saved, the server list is
  updated and the new VPN is being connected); only the error is shown, for example
  `Failed to enable the kill switch: ...` or, without the rules, the hint
  `No passwordless permission to manage the kill switch.` with the two sudoers lines. The toggle stays
  in the selected position. A retry no longer sees a VPN change and repeats only this step. The
  watchdog step is not attempted after a failed kill switch step: only the kill switch error is
  shown, and a retry repeats the kill switch step and then runs the watchdog step.
- Step 5 fails: the earlier steps (VPN profile, kill switch) stay applied; the window stays open with
  `Failed to enable the watchdog: ...` or `Failed to disable the watchdog: ...` (systemd's error
  text, or `the unit did not change state` if there is none). If the unit file is not installed
  (the message says `not found`, `No such file` or `does not exist`), the hint
  ` Install it with watchdog/install-watchdog.sh.` is appended. A retry repeats only this step.

The initial position of the kill switch toggle is taken from `systemctl is-active`; as long as the user has not
touched it, it is updated together with the `Kill switch: …` line in the menu. The state persists
across reboots: a disabled kill switch will not turn itself back on until it is enabled again (in
this window or with `sudo systemctl enable --now vpn-killswitch.service`). The watchdog toggle is read
once, when the window opens, from `systemctl --user is-active vpn-watchdog.service` (any state other
than `active` counts as off); it is not updated while the window is open.

Under Wayland an application cannot position its own window, so the unit starts
the tray with `GDK_BACKEND=x11` (through XWayland): this does not affect the icon, and the window
opens in the center.

Passwordless `sudo -n` works only if the sudoers rules from "Installation" are in place. Without
them `Save` will not work: the window will stay open and show a hint with the missing lines.
`install-tray.sh` checks this and prints a warning if the permissions are missing.

## Watchdog

The watchdog keeps the VPN profile selected in Settings connected. It can be switched on and off
with the `Watchdog` toggle in the Settings window (see "Settings"; the unit must be installed first
with `install-watchdog.sh`). Every 3 seconds it reads the
profile UUID from `~/.config/vpn-killswitch/settings.conf` (`$XDG_CONFIG_HOME` is respected) and, if
that profile is not among the active NetworkManager connections, runs
`nmcli connection up uuid <UUID>` (and writes `watchdog: bringing up <UUID>` to the journal). The file
is read on every iteration, so a profile change in Settings takes effect within seconds without
restarting anything. While no profile is selected, there is nothing to do. The kill switch covers
the time between a drop and the reconnect.

```bash
./watchdog/install-watchdog.sh
./watchdog/uninstall-watchdog.sh
```

Both scripts are run as a regular user, without `sudo`. Installation puts the unit at
`~/.config/systemd/user/vpn-watchdog.service` with the absolute path to `vpn-watchdog.sh` from this
folder, then runs `systemctl --user daemon-reload` and `enable --now`. If the repository is moved, run
`install-watchdog.sh` again. Removal stops the unit, disables it and deletes the file; the VPN
connection stays up. The unit restarts after 5 s if the script crashes. There is deliberately no
`ExecStop`: stopping the watchdog does not bring the VPN down.

Because the profile is brought up whenever it is down, a manual `nmcli connection down` is undone within
a few seconds. To keep the VPN off, stop the watchdog first:

```bash
systemctl --user stop vpn-watchdog.service
```

Status and log:

```bash
systemctl --user status vpn-watchdog.service
journalctl --user -u vpn-watchdog.service
```

Two watchdogs must not run at once: disable any other watchdog you run for the same VPN.

For tests, two environment variables are supported when running `./watchdog/vpn-watchdog.sh` by hand:
`VPN_WATCHDOG_DRY_RUN=1` prints the `nmcli` command instead of running it, and `VPN_WATCHDOG_ONCE=1`
does a single iteration and exits.

```bash
VPN_WATCHDOG_DRY_RUN=1 VPN_WATCHDOG_ONCE=1 ./watchdog/vpn-watchdog.sh
```

## Troubleshooting

**The VPN does not connect after installation.** Look at the counter on the `drop` rule
in `sudo nft list table inet killswitch`. If it grows during a connection attempt,
most likely the server IP has changed or the name does not resolve. Check
`getent hosts <vpn-gateway-host>` and `sudo vpn-killswitch-server show`.

If IPsec comes up (the journal shows `strongSwan IPsec connection is up`), but then
`kl2tpd` reports `operation not permitted` and the connection fails with a timeout, then
traffic to the server is being blocked before encryption: the address the VPN talks to
is most likely missing from the allowed servers. Compare it with the output of
`sudo vpn-killswitch-server show`.

**Something is blocked and you do not know what.** Open the tray's "Dropped packets" window (click the
`Kill switch: …` menu line) and look at the destinations, or run:

```bash
journalctl -k --grep KS-DROP
```

Each line shows the interface (`OUT=`), the source and destination addresses, the protocol and ports,
and for the `output` chain the sender (`UID=`). If the window says `Failed to read the journal`, the
user cannot read the kernel journal: add yourself to the `adm` group and log in again. If nothing is listed
although the counter grows, the rules were installed without the `log` rules: run `./install.sh` again.

**The VPN server IP changed.** The addresses are resolved when they are saved, so a new IP
of the same host name is not known to the kill switch. Run:

```bash
sudo vpn-killswitch-server refresh
```

It re-resolves the saved hosts, checks and reloads the rules.

**`Save` in Settings fails with a sudoers hint.** The tray runs `sudo -n` and cannot ask for a
password. Add the lines from the hint (or all four from "Installation", "The sudoers rules") to
`/etc/sudoers.d/vpn-stats` with `sudo visudo -f /etc/sudoers.d/vpn-stats` and press `Save` again.
If the helper is missing (the kill switch is not installed), run `./install.sh` first.

**The tray shows `VPN: not selected` or `VPN: profile not found`.** The first means that no
profile is chosen yet (no settings file): open `Settings…`, select the VPN connection and press
`Save`. The second means that the selected profile was deleted from NetworkManager: choose another
one in `Settings…`.

**`vpn-stats.sh` prints `No VPN profile selected.`** Select a profile in the tray Settings or pass
`--vpn NAME`.

**No internet while the VPN is connected.** Make sure the VPN interface is named
`ppp*`, `tun*`, `tap*` or `wg*`: `ip -br link`. If the VPN uses another name, see
"A tunnel interface with another name".

**Docker containers cannot reach the internet.** This is by design while the VPN is off.
With the VPN on, check that the default route points to the tunnel interface:
`ip route show default`.

**The rules disappeared after a reboot.** Check that the service is enabled:
`systemctl is-enabled vpn-killswitch.service`. If you enabled `nftables.service`,
its `flush ruleset` deletes the table. Disable it or remove `flush ruleset`.

## License

MIT, see [LICENSE](LICENSE).
