# VPN kill switch for VPN-L2TP

Blocks internet access that bypasses the VPN. While the VPN-L2TP tunnel is down or
not yet established, the only traffic that leaves the machine is what the VPN itself
needs to come up, plus local-network traffic. Everything else is dropped.

Built for Ubuntu with NetworkManager and the nm-l2tp plugin (strongSwan + kl2tpd + pppd).
Implementation: a single nftables table and a systemd unit that loads it at boot.

## Contents

| File | Purpose |
|---|---|
| `killswitch.nft` | nftables rules. Installed to `/etc/nftables.d/killswitch.nft`. |
| `vpn-killswitch.service` | systemd unit that loads the rules before the network comes up. Installed to `/etc/systemd/system/`. |
| `install.sh` | Installation and enabling. |
| `uninstall.sh` | Disabling and complete removal. |
| `vpn-stats.sh` | Statistics of VPN-L2TP drops and internet outages for the last N hours. |
| `tray/vpn-tray.py` | VPN icon in the GNOME top bar with a statistics menu. |
| `tray/vpn-tray.service` | `systemd --user` unit that starts the icon after you log in to the session. |
| `tray/install-tray.sh` | Dependency check, installation and enabling of the icon unit. |
| `tray/uninstall-tray.sh` | Stops and removes the icon unit. |

## Why you need it

VPN-L2TP has two unpleasant failure modes.

1. **The tunnel "hangs".** The `ppp1` interface is alive and the default route points
   into it, but the server is silent. Traffic goes into the void. This is inconvenient,
   but nothing leaks out.
2. **The tunnel dies and NetworkManager notices.** It removes `ppp1`, and the default
   route falls back to Wi-Fi. From then on all traffic, DNS included, goes straight
   to the internet until the VPN is reconnected by hand. This is the leak.

The kill switch covers the second case: even when the default route points
to Wi-Fi, the rules let nothing out except VPN service packets.

## How it works

The rules live in a separate table, `inet killswitch`, with two chains.

**The `output` chain** (the laptop's own traffic). The following pass without restrictions:

- `lo`;
- `ppp*`, that is, the VPN-L2TP tunnel;
- `docker0`, `br-*`, `veth*`, that is, Docker.

For any other interface (Wi-Fi, Ethernet, USB modem, anything that appears later)
only the following is allowed:

- DHCP (UDP 68 → 67);
- UDP 500 and 4500 to the VPN server address (IKE and IPsec NAT-T);
- UDP 1701 to the VPN server address. This is L2TP itself: netfilter sees it in `output`
  before the kernel encrypts it into ESP, so without this rule L2TP is dropped even
  though IPsec comes up;
- local networks: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `224.0.0.0/4`;
- IPv6 link-local (`fe80::/10`, `ff00::/8`), so that NDP works.

Everything else is dropped by the `counter drop` rule. Its counter shows
how many packets have been blocked.

**The `forward` chain** (traffic the laptop forwards, i.e. Docker containers).
Containers may go out through `ppp*`, talk to each other over the Docker bridges and reach
the local network. They cannot reach the internet directly, bypassing the VPN.

The table does not depend on whether NetworkManager noticed the VPN failure. The rules
are static and always in effect.

### Why not `nftables.service`

The stock `/etc/nftables.conf` starts with `flush ruleset`. If you enable
`nftables.service`, it wipes the Docker rules (Docker works through
iptables-nft) and ufw at boot. That is why a dedicated unit is used: it loads only
its own table and leaves the others alone. The rules file is idempotent: on every load
the `inet killswitch` table is recreated, and all other tables stay as they were.

## Requirements

- nftables (`nft`), usually already installed in Ubuntu;
- systemd;
- a VPN through NetworkManager with a `ppp*` interface (nm-l2tp, nm-pptp). For OpenVPN
  or WireGuard, replace `ppp*` with `tun*` or the name of the WireGuard interface;
- ufw is disabled or does not interfere (the kill switch table works independently of it).

## Installation

```bash
./install.sh
```

The script re-runs itself through `sudo`, checks the rules syntax, copies
the files, enables the service and shows the loaded table.

Before the first run, check the VPN server address in `killswitch.nft`:

```
set vpn_server {
    type ipv4_addr
    elements = { 203.0.113.10 }
}
```

To find the current address:

```bash
getent hosts vpn.example.com
```

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

Then connect VPN-L2TP and repeat the `curl`. It should work.

To view the rules and the blocked-packet counters:

```bash
sudo nft list table inet killswitch
```

Service status:

```bash
systemctl status vpn-killswitch.service
```

## Removal

```bash
./uninstall.sh
```

Stops the service, deletes the table and erases the files from `/etc`. Traffic
immediately starts flowing without restrictions.

To disable it temporarily without removing it:

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

Typical changes:

- **The VPN server IP changed.** Update `elements` in the `vpn_server` set. You can
  list several addresses separated by commas.
- **A different VPN type.** Replace `oifname "ppp*"` with the right interface in both
  chains, and ports 500 and 4500 with your VPN's port (for example, UDP 1194 for OpenVPN).
- **Disallow the local network.** Remove the `ip daddr @lan accept` line. You will then
  need DNS for the VPN server name: add the line
  `203.0.113.10 vpn.example.com` to `/etc/hosts`. The router, printers and local
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
- **DNS while the VPN is off.** The local network is allowed, so application queries
  go to the local resolver (usually the router). This is needed to resolve the VPN
  server name before connecting. If such a leak is unacceptable, see "Disallow the local network".
- **Public Wi-Fi with a captive portal.** The login page lies outside the local
  network and will be blocked. Stop the service while you log in, then start it again.
- **A network whose resolver is not in a private range.** The VPN server name will not resolve
  and the VPN will not connect. Solution: an entry in `/etc/hosts`.
- **VPN autoconnect.** The kill switch does not reconnect the VPN by itself. To make VPN-L2TP
  come up automatically after login and after sleep:

  ```bash
  nmcli connection modify VPN-L2TP connection.autoconnect yes
  ```

- **Dead tunnel detection.** By default pppd does not check whether the link is alive,
  and a hung VPN stays hung forever. To make NetworkManager drop it on its own after about
  half a minute:

  ```bash
  nmcli connection modify VPN-L2TP +vpn.data lcp-echo-interval=10 +vpn.data lcp-echo-failure=3
  ```

  Together with autoconnect this gives automatic VPN recovery, and the kill switch
  covers the window between the drop and the recovery.

## Drop statistics

```bash
./vpn-stats.sh [hours] [--events]
```

The script reads the systemd journal for the last N hours and prints two Markdown tables.

**"Statistics"** is built from journal events:

| Rows | What they are based on |
|---|---|
| VPN drops, total | pppd `Connection terminated`. If there are fewer of them than the causes below, the sum of the causes is used. |
| server stopped responding (LCP) | `No response to 3 echo-requests`. |
| on command: sleep, nmcli down, watchdog | `Terminating on signal`. NetworkManager sends it on `nmcli connection down`, on suspend and when the watchdog is stopped. |
| other | the remainder: drops that have neither of the two causes above. |
| VPN connection attempts / Successful connections / Failed connections (timeout) | `starting l2tp`, `policy: set 'VPN-L2TP' ... as default`, `failed to connect`. |
| Password request errors (no agent) | `failed to request VPN secrets` (no password agent). |
| Total time without VPN / Longest outage | computed from the order of events: from a drop to the next successful connection. Suspend time is not counted. Nothing is counted before the first VPN event in the window. If VPN-L2TP is not connected right now, an unclosed outage is counted up to the current moment. |
| Hello from L2TP server | `avpMsgTypeHello` in the kl2tpd debug output, and the time of the last one. |
| Wi-Fi: disconnects / Wi-Fi: resets after VPN failure | `state change: activated -> ...` and `secondary-connection-failed` on a `wl*` interface. |
| Suspends / NetworkManager restarts / System boots | `PM: suspend entry`, `Stopped NetworkManager.service`, `Linux version`. |
| Last drops | up to five most recent drops with the cause: `LCP`, `sleep`, `command` or `other`. |

**"Now"**: whether VPN-L2TP is connected, the state of `vpn-killswitch.service` and of the watchdog
`old-vpn-watchdog.service`, and the number of packets dropped by the kill switch.

**The period** is given in hours. The default is 1 hour. Both a decimal point and a decimal comma
are accepted. The step is half an hour: the value is rounded half up to the nearest 0.5 h
(0.7 gives 0.5, 0.75 gives 1, 1.2 gives 1, 1.3 gives 1.5). If the result is 0
or the value is not a number, 1 hour is used and an explanatory note is printed before the tables.

```bash
./vpn-stats.sh            # the last hour
./vpn-stats.sh 0.5        # the last half hour
./vpn-stats.sh 1,5        # an hour and a half
./vpn-stats.sh 3          # three hours
```

The `--events` flag adds a chronology: connections, drops, suspend and wake,
NetworkManager restarts, boots, connection failures and password request failures.

```bash
./vpn-stats.sh 4 --events
```

`sudo` is not needed. The exception: the "Blocked by kill switch, packets" row reads the counter
through `sudo -n nft`. Without passwordless `sudo` it will show `n/a (sudo required)`.
The script never asks for a password.

## Tray icon

A permanent indicator in the GNOME top bar: the icon shows whether the VPN is alive, and the menu
collects the statistics that you would otherwise have to open a terminal for.

```bash
./tray/install-tray.sh
```

**Icon:**

| Icon | When |
|---|---|
| `org.gnome.Settings-network-symbolic` | VPN-L2TP is connected (the "Network" icon from GNOME Settings) |
| `network-wired-acquiring-symbolic` | VPN-L2TP is connecting |
| `network-wired-disconnected-symbolic` | VPN-L2TP is not connected |

**Menu** (the first four lines only display data and are refreshed when the menu
is opened and by timers):

- `VPN-L2TP: connected 4h 12m` or `VPN-L2TP: disconnected`. The time is counted
  from the last successful activation of the profile;
- `Uptime: 5h 01m`. System uptime;
- `Kill switch: active, 0 packets dropped`, `Kill switch: OFF` or
  `Kill switch: active, counter unavailable`;
- `VPN drops since boot: 1 (server silent: 0)`. The logic is the same as in `vpn-stats.sh`:
  the total is the larger of the number of `Connection terminated` and the sum of the causes, and the
  number in parentheses is the count of `No response to N echo-requests`;
- `Reconnect VPN` runs `nmcli connection up VPN-L2TP` in the background;
- `Settings…` opens the settings window (see "Settings window" below);
- `Quit` closes the icon (the unit is not restarted in that case, see below).

The icon writes durations in short forms: `12s`, `3m 05s`, `1h 02m`,
and for days `1d 03h`.

**Where the data comes from:**

| Indicator | Source | Refresh |
|---|---|---|
| VPN state | libnm (`NM.Client`), the active-connections change signal | on event and every 5 s |
| VPN uptime | `connection.timestamp` of the VPN-L2TP profile | when the menu is opened and every 5 s |
| System uptime | `/proc/uptime` | when the menu is opened and every 5 s |
| Kill switch enabled | `systemctl is-active vpn-killswitch.service` | when the menu is opened and every 60 s |
| Packets dropped | `sudo -n nft list chain inet killswitch output`, `counter packets` in the line with `drop` | when the menu is opened and every 60 s |
| VPN drops | `journalctl -b -q -o cat _COMM=pppd` | every 60 s |

### Requirements

- the packages `python3-gi`, `gir1.2-gtk-3.0`, `gir1.2-nm-1.0`, `gir1.2-ayatanaappindicator3-0.1`
  (usually already installed in Ubuntu; `install-tray.sh` checks for them and, if any are missing,
  prints the `sudo apt install ...` command; it does not run `apt` itself);
- the AppIndicator extension enabled in GNOME Shell (in Ubuntu this is
  `ubuntu-appindicators@ubuntu.com`). Without it the icon will not appear;
- a NetworkManager profile named `VPN-L2TP`;
- the kill switch installed (otherwise the line will show `OFF`).

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

### Why the counter needs a sudoers rule

The packet counter is read with the `nft` command, which needs root. The menu must not ask
for a password, so the same sudoers rule as for `vpn-stats.sh` is used
(it may already exist):

```bash
sudo visudo -f /etc/sudoers.d/vpn-stats
```

```
youruser ALL=(root) NOPASSWD: /usr/sbin/nft list chain inet killswitch output
```

The rule allows exactly one command. Without it the other menu lines work,
and the kill switch line will show `counter unavailable`.

To also read the `forward` chain (dropped traffic of Docker containers), add a second
line to the same file:

```
youruser ALL=(root) NOPASSWD: /usr/sbin/nft list chain inet killswitch forward
```

Currently the icon does not read this chain; the line is only needed for future
use or for manual queries.

### Settings window

The `Settings…` menu item opens a separate "VPN-L2TP Settings" window in the center of
the screen (a second window is not created: choosing the item again brings the open one
to the front). Closing the window does not stop the icon. For now it contains a single
toggle, **Kill switch**.

The toggle only selects the desired state and does not run anything by itself.
At the bottom right there are two buttons: `Cancel` and `Save` (Enter triggers `Save`).
On `Save`, if the state has changed, the following is run:
`sudo -n systemctl enable --now vpn-killswitch.service` (to enable) or
`sudo -n systemctl disable --now vpn-killswitch.service` (to disable); while the command
is running, the buttons are unavailable. After a successful save the window closes; if
the state has not changed, `Save` simply closes the window. `Cancel` (as well as Escape
and the close button) closes the window without changes and without running any commands. On an error the window
stays open, a hint appears under the toggle (for example, about the
sudoers rules), the toggle stays in the selected position, and after fixing the problem you can
press `Save` again. The initial toggle position is taken from
`systemctl is-active`; as long as the user has not touched it, it is updated together with the
`Kill switch: …` line in the menu. The state persists across reboots:
a disabled kill switch will not turn itself back on until it is enabled again (in this window
or with `sudo systemctl enable --now vpn-killswitch.service`).

Under Wayland an application cannot position its own window, so the unit starts
the tray with `GDK_BACKEND=x11` (through XWayland): this does not affect the icon, and the window
opens in the center.

Passwordless `sudo -n` works only if the sudoers rules are in place. Add two
lines to the same file as for the counter:

```bash
sudo visudo -f /etc/sudoers.d/vpn-stats
```

```
youruser ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now vpn-killswitch.service
youruser ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now vpn-killswitch.service
```

Without them `Save` will not work: the window will stay open and show a hint
with these lines. `install-tray.sh` checks this and prints a warning
if the permissions are missing.

## Troubleshooting

**The VPN does not connect after installation.** Look at the counter on the `drop` rule
in `sudo nft list table inet killswitch`. If it grows during a connection attempt,
most likely the server IP has changed or the name does not resolve. Check
`getent hosts vpn.example.com` and the `vpn_server` set.

If IPsec comes up (the journal shows `strongSwan IPsec connection is up`), but then
`kl2tpd` reports `operation not permitted` and the connection fails with a timeout, then
UDP 1701 is being blocked before encryption. Check that the rule for port 1701
is in place.

**No internet while the VPN is connected.** Make sure the VPN interface is named
`ppp*`: `ip -br link | grep ppp`. If the VPN is of another type, see "A different VPN type".

**Docker containers cannot reach the internet.** This is by design while the VPN is off.
With the VPN on, check that the default route points to `ppp*`:
`ip route show default`.

**The rules disappeared after a reboot.** Check that the service is enabled:
`systemctl is-enabled vpn-killswitch.service`. If you enabled `nftables.service`,
its `flush ruleset` deletes the table. Disable it or remove `flush ruleset`.
