#!/usr/bin/env python3
# VPN icon in the GNOME top bar: state of the selected VPN profile, uptime, kill switch
# state with the number of logged dropped packets, number of VPN drops. Uses the AppIndicator protocol (StatusNotifierItem).
# Run: ./vpn-tray.py (normally via the user unit vpn-tray.service).

import configparser
import fcntl
import getpass
import ipaddress
import os
import pwd
import queue
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("NM", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, NM  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402

KILLSWITCH_UNIT = "vpn-killswitch.service"
WATCHDOG_UNIT = "vpn-watchdog.service"  # user unit (systemctl --user): keeps the selected VPN profile connected
HELPER = "/usr/local/sbin/vpn-killswitch-server"  # root helper: writes the VPN server IPs for the kill switch
SETTINGS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "vpn-killswitch",
    "settings.conf",
)

ICON_ACTIVE = "org.gnome.Settings-network-symbolic"  # the "Network" icon from GNOME Settings
ICON_ACTIVATING = "network-wired-acquiring-symbolic"
ICON_OFF = "network-wired-disconnected-symbolic"

# pppd line patterns. `journalctl -o cat` prints only the message text,
# without the "pppd[PID]:" prefix; the process is selected via _COMM=pppd.
RE_LCP = re.compile(r"^No response to [0-9]+ echo-requests", re.M)
RE_TERM = re.compile(r"^Terminating on signal", re.M)
RE_CONN = re.compile(r"^Connection terminated\.", re.M)

FAST_SECONDS = 5        # VPN state, uptime (fallback polling)
KILLSWITCH_SECONDS = 60  # kill switch state and the number of logged dropped packets
JOURNAL_SECONDS = 60    # VPN drops from the journal

# Prefix of the kernel log lines written by the `log` rules in killswitch.nft
# ("KS-DROP: " for the output chain, "KS-DROP-FWD: " for the forward chain) and their rate limit.
DROP_LOG_PREFIX = "KS-DROP"
DROP_LOG_LIMIT = "10/s"

# `journalctl -k -o short-unix`: "<epoch.micro> <host> kernel: KS-DROP: IN= OUT=wlan0 SRC=..."
# journald may glue the prefix to the first field ("KS-DROPIN="), hence the optional ":" and spaces.
RE_DROP_LINE = re.compile(
    r"^(?P<ts>\d+(?:\.\d+)?)\s+\S+\s+kernel:\s+(?P<msg>" + re.escape(DROP_LOG_PREFIX)
    + r"(?P<fwd>-FWD)?:?\s*(?P<rest>.*?))\s*$"
)
DROP_TCP_FLAGS = ("SYN", "ACK", "FIN", "RST", "PSH", "URG", "DF")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fmt_duration(s):
    """Seconds -> "12s" / "3m 05s" / "1h 02m" / "1d 03h"."""
    s = int(s)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    if s < 86400:
        return "%dh %02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd %02dh" % (s // 86400, (s % 86400) // 3600)


def plural_packets(n):
    """1 packet, 0 or 2+ packets."""
    return "packet" if n == 1 else "packets"


def run(cmd, timeout=5):
    """Run an external command. Returns (code, stdout) or (None, "") on error."""
    try:
        r = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout
    except Exception as e:  # timeout, command not found, etc.
        log("%s: %s" % (" ".join(cmd), e))
        return None, ""


def unit_active(argv_prefix, unit):
    """`<argv_prefix> is-active <unit>`: True if the unit is active, False for any other state,
    None if systemctl could not be run. argv_prefix is ["systemctl"] or ["systemctl", "--user"]."""
    rc, out = run([*argv_prefix, "is-active", unit])
    if rc is None:
        return None
    return out.strip() == "active"


def run_async(argv, callback, want_stdout=False):
    """Run a command without blocking the GTK main loop.

    callback(ok, stderr) is called from the main loop: ok is True if the command exited with
    status 0 (or False if it failed to start). With want_stdout=True the callback is
    callback(ok, stderr, stdout) instead. Returns the Gio.Subprocess (or None).
    """
    def finish(ok, stderr, stdout):
        if want_stdout:
            callback(ok, stderr or "", stdout or "")
        else:
            callback(ok, stderr or "")

    def done(proc, res):
        stdout = ""
        stderr = ""
        ok = False
        try:
            _, stdout, stderr = proc.communicate_utf8_finish(res)
            ok = proc.get_if_exited() and proc.get_exit_status() == 0
        except Exception as e:
            log("%s: %s" % (" ".join(argv), e))
            stderr = str(e)
        finish(ok, stderr, stdout)

    try:
        proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        proc.communicate_utf8_async(None, None, done)
        return proc
    except Exception as e:
        log("%s: %s" % (" ".join(argv), e))
        GLib.idle_add(lambda: finish(False, str(e), "") and GLib.SOURCE_REMOVE)
        return None


# --- dropped packets (the kernel log lines written by the `log` rules) ---

def parse_drop_line(line):
    """One `journalctl -k -q -o short-unix` line -> dict describing the dropped packet, or None.

    Keys: time (float epoch), chain ("output" / "forward"), out, src, dst, proto, spt, dpt,
    icmp_type, icmp_code, flags (SYN/ACK/... in order of appearance), uid, raw (message text).
    """
    m = RE_DROP_LINE.match(line.strip())
    if not m:
        return None
    fields = {}
    flags = []
    inner = False  # inside "[...]": the quoted packet of an ICMP error, not the dropped packet itself
    for tok in m.group("rest").split():
        starts_inner = tok.startswith("[")
        if not inner and not starts_inner:
            if "=" in tok:
                key, _, value = tok.partition("=")
                fields.setdefault(key, value)
            elif tok in DROP_TCP_FLAGS:
                flags.append(tok)
        if starts_inner:
            inner = True
        if inner and tok.endswith("]"):
            inner = False

    def num(key):
        try:
            return int(fields[key])
        except (KeyError, ValueError):
            return None

    return {
        "time": float(m.group("ts")),
        "chain": "forward" if m.group("fwd") else "output",
        "out": fields.get("OUT", ""),
        "src": fields.get("SRC", ""),
        "dst": fields.get("DST", ""),
        "proto": fields.get("PROTO", ""),
        "spt": num("SPT"),
        "dpt": num("DPT"),
        "icmp_type": num("TYPE"),
        "icmp_code": num("CODE"),
        "flags": flags,
        "uid": num("UID"),
        "raw": m.group("msg"),
    }


def fmt_dst(rec):
    """"ip:port" for TCP/UDP, "ip (type/code)" for ICMP, otherwise just the IP."""
    dst = rec["dst"]
    proto = rec["proto"]
    if proto in ("TCP", "UDP") and rec.get("dpt") is not None:
        if ":" in dst:  # IPv6 literal
            dst = "[%s]" % dst
        return "%s:%d" % (dst, rec["dpt"])
    if proto.startswith("ICMP") and rec.get("icmp_type") is not None:
        code = rec.get("icmp_code")
        return "%s (%d/%s)" % (dst, rec["icmp_type"], "?" if code is None else code)
    return dst


def fmt_user(uid):
    """User name for a UID (or the number if it has no name); "" if unknown."""
    if uid is None:
        return ""
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return str(uid)


def group_drops(records):
    """Group identical flows. Returns [dict] with the key fields plus count, first and last (epoch)."""
    groups = {}
    for r in records:
        key = (r["proto"], r["dst"], r["dpt"], r["out"], r["uid"], r["chain"], r["icmp_type"], r["icmp_code"])
        g = groups.get(key)
        if g is None:
            groups[key] = {
                "proto": r["proto"], "dst": r["dst"], "dpt": r["dpt"], "out": r["out"],
                "uid": r["uid"], "chain": r["chain"],
                "icmp_type": r["icmp_type"], "icmp_code": r["icmp_code"],
                "count": 1, "first": r["time"], "last": r["time"],
            }
        else:
            g["count"] += 1
            g["first"] = min(g["first"], r["time"])
            g["last"] = max(g["last"], r["time"])
    return sorted(groups.values(), key=lambda g: g["last"], reverse=True)


def count_logged_drops():
    """Number of dropped packets logged in the kernel journal since boot, or None if it cannot be read.

    Definition (shared with the "Since boot" period of DropsWindow, so the two numbers can never
    differ): the number of journal lines matching `--grep ^KS-DROP` among kernel messages of the
    current boot that parse_drop_line() accepts. The query is the same as the window's, except that
    only the count is needed. journalctl exits with 1 when --grep matches nothing.
    """
    rc, out = run(["journalctl", "-q", "-o", "short-unix", "--no-pager",
                   "--grep", "^" + DROP_LOG_PREFIX, "-b", "_TRANSPORT=kernel"], timeout=10)
    if rc == 0 or (rc == 1 and not out.strip()):
        return sum(1 for line in out.splitlines() if parse_drop_line(line) is not None)
    return None


def fmt_time(ts):
    """Epoch -> "HH:MM:SS" for today, "YYYY-MM-DD HH:MM:SS" otherwise (local time)."""
    t = time.localtime(ts)
    now = time.localtime()
    if (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("%H:%M:%S", t)
    return time.strftime("%Y-%m-%d %H:%M:%S", t)


# --- settings (shared with the watchdog and the stats script; the tray is the only writer) ---

def load_settings():
    """UUID of the selected VPN profile from SETTINGS_PATH, or None."""
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(SETTINGS_PATH)
        uuid = cp.get("vpn", "uuid", fallback="").strip()
    except Exception as e:  # unreadable or malformed file
        log("%s: %s" % (SETTINGS_PATH, e))
        return None
    return uuid or None


def save_settings(uuid):
    """Write the selected VPN profile UUID atomically (temp file + rename)."""
    d = os.path.dirname(SETTINGS_PATH)
    os.makedirs(d, mode=0o700, exist_ok=True)
    cp = configparser.ConfigParser(interpolation=None)
    cp["vpn"] = {"uuid": uuid}
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            cp.write(f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, SETTINGS_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def host_from_endpoint(entry):
    """"host", "host:port", "host:port:proto", "[v6]:port" or a bare IPv6 literal -> host."""
    entry = entry.strip()
    if entry.startswith("["):
        end = entry.find("]")
        if end > 0:
            return entry[1:end]
    try:
        ipaddress.IPv6Address(entry)  # a bare IPv6 literal: its colons are not a port separator
        return entry
    except ValueError:
        pass
    return entry.split(":", 1)[0]


def vpn_server_hosts(conn):
    """Server hosts (names or IPs) of a NetworkManager VPN / WireGuard profile; [] if unknown."""
    hosts = []
    try:
        ctype = conn.get_connection_type()
        if ctype == "vpn":
            s = conn.get_setting_vpn()
            for key in ("gateway", "remote", "address"):
                value = (s.get_data_item(key) or "").strip() if s is not None else ""
                if not value:
                    continue
                if key == "remote":  # OpenVPN: several entries separated by commas / whitespace
                    hosts = [host_from_endpoint(e) for e in re.split(r"[,\s]+", value) if e]
                else:
                    hosts = [host_from_endpoint(value)]
                break
        elif ctype == "wireguard":
            s = conn.get_setting(NM.SettingWireGuard)
            if s is not None:
                for i in range(s.get_peers_len()):
                    endpoint = s.get_peer(i).get_endpoint()
                    if endpoint:
                        hosts.append(host_from_endpoint(endpoint))
    except Exception as e:
        log("vpn_server_hosts: %s" % e)
        return []
    out = []
    for h in hosts:
        if h and h not in out:
            out.append(h)
    return out


def switch_row(title, subtitle):
    """A Settings row: bold title, dim subtitle and a Gtk.Switch on the right. Returns (row, switch)."""
    title_label = Gtk.Label(halign=Gtk.Align.START, hexpand=True)
    title_label.set_markup("<b>%s</b>" % title)
    title_label.set_xalign(0)
    subtitle_label = Gtk.Label(label=subtitle, halign=Gtk.Align.START, hexpand=True)
    subtitle_label.get_style_context().add_class("dim-label")
    subtitle_label.set_line_wrap(True)
    subtitle_label.set_xalign(0)

    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
    texts.pack_start(title_label, False, False, 0)
    texts.pack_start(subtitle_label, False, False, 0)

    switch = Gtk.Switch(valign=Gtk.Align.CENTER, hexpand=False)

    row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    row_box.set_border_width(12)
    row_box.pack_start(texts, True, True, 0)
    row_box.pack_start(switch, False, False, 0)

    row = Gtk.ListBoxRow(activatable=False, can_focus=False)
    row.add(row_box)
    return row, switch


class Tray:
    def __init__(self):
        self.started = time.time()  # "This session" in the Dropped packets window starts here
        self.client = None
        try:
            self.client = NM.Client.new(None)
        except Exception as e:
            log("NetworkManager unavailable: %s" % e)

        self.vpn_uuid = load_settings()  # UUID of the selected VPN profile, None if not selected
        self.vpn_conn = None    # its NM.RemoteConnection (None if not selected or deleted)
        self.ac = None          # active connection of the selected profile (if any)
        self.ac_handler = 0
        self.procs = []         # background nmcli processes, to avoid zombies
        self.last_icon = None
        self.last_title = None

        self.ks_active = None   # True/False, None if `systemctl is-active` failed to run
        self.settings_win = None  # settings window while it is open
        self.drops_win = None   # "Dropped packets" window while it is open
        self.ks_text = "Kill switch: checking…"
        self.drops_text = "VPN drops since boot: checking…"

        self.indicator = AppIndicator.Indicator.new(
            "vpn-tray",
            ICON_OFF,
            AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

        # Informational items stay sensitive (otherwise they are greyed out) and do nothing.
        self.item_vpn = Gtk.MenuItem(label="VPN: …")
        self.item_uptime = Gtk.MenuItem(label="Uptime: …")
        self.item_ks = Gtk.MenuItem(label=self.ks_text)
        # Clicking the kill switch line opens the "Dropped packets" window.
        self.item_ks.connect("activate", self.open_drops_window)
        self.item_drops = Gtk.MenuItem(label=self.drops_text)
        self.item_reconnect = Gtk.MenuItem(label="Reconnect VPN")
        self.item_reconnect.connect("activate", self.on_reconnect)
        item_settings = Gtk.MenuItem(label="Settings…")
        item_settings.connect("activate", self.on_settings)
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda *_: Gtk.main_quit())

        self.menu = Gtk.Menu()
        for item in (self.item_vpn, self.item_uptime, self.item_ks, self.item_drops):
            self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(self.item_reconnect)
        self.menu.append(item_settings)
        self.menu.append(item_quit)
        self.menu.connect("show", self.on_menu_show)
        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        if self.client is not None:
            self.client.connect("notify::active-connections", lambda *_: self.refresh_vpn())
            self.client.connect("connection-added", lambda *_: self.refresh_conn())
            self.client.connect("connection-removed", lambda *_: self.refresh_conn())

        self.update_conn()
        self.refresh_vpn()
        self.refresh_uptime()
        self.refresh_killswitch()
        self.refresh_drops()

        GLib.timeout_add_seconds(FAST_SECONDS, self.tick_fast)
        GLib.timeout_add_seconds(KILLSWITCH_SECONDS, self.tick_killswitch)
        GLib.timeout_add_seconds(JOURNAL_SECONDS, self.tick_journal)

    # --- timers and menu events ---------------------------------------------

    def tick_fast(self):
        self.refresh_vpn()
        self.refresh_uptime()
        self.procs = [p for p in self.procs if p.poll() is None]
        return True

    def tick_killswitch(self):
        self.refresh_killswitch()
        return True

    def tick_journal(self):
        self.refresh_drops()
        return True

    def on_menu_show(self, *_):
        self.refresh_vpn()
        self.refresh_uptime()
        self.refresh_killswitch()

    def on_reconnect(self, *_):
        """Reconnect: `nmcli connection up` on an active profile only reports "already active",
        so an active connection is brought down first and then up again."""
        uuid = self.vpn_uuid
        if not uuid:
            return
        if self.find_active_uuid(uuid) is not None:
            run_async(["nmcli", "connection", "down", "uuid", uuid],
                      lambda ok, stderr: self.start_vpn(uuid))
        else:
            self.start_vpn(uuid)

    def start_vpn(self, uuid):
        """Start `nmcli connection up` in the background (not awaited)."""
        try:
            p = subprocess.Popen(
                ["nmcli", "connection", "up", "uuid", uuid],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.procs.append(p)
        except Exception as e:
            log("nmcli: %s" % e)

    def on_settings(self, *_):
        if self.settings_win is not None:
            self.settings_win.present()
            return
        win = SettingsWindow(self)
        win.connect("destroy", self.on_settings_destroy)
        self.settings_win = win

    def on_settings_destroy(self, *_):
        self.settings_win = None

    def open_drops_window(self, *_):
        if self.drops_win is not None:
            self.drops_win.present()
            return
        win = DropsWindow(self)
        win.connect("destroy", self.on_drops_destroy)
        self.drops_win = win

    def on_drops_destroy(self, *_):
        self.drops_win = None

    # --- VPN ----------------------------------------------------------------

    def vpn_connections(self):
        """[(name, uuid)] of all VPN and WireGuard profiles, sorted by name."""
        if self.client is None:
            return []
        out = []
        for c in self.client.get_connections():
            if c.get_connection_type() in ("vpn", "wireguard"):
                out.append((c.get_id(), c.get_uuid()))
        out.sort(key=lambda x: x[0].lower())
        return out

    def vpn_name(self):
        return self.vpn_conn.get_id() if self.vpn_conn is not None else None

    def update_conn(self):
        """Re-resolve the selected profile and update the Reconnect item."""
        self.vpn_conn = None
        if self.client is not None and self.vpn_uuid:
            try:
                self.vpn_conn = self.client.get_connection_by_uuid(self.vpn_uuid)
            except Exception as e:
                log("connection by uuid: %s" % e)
        self.item_reconnect.set_sensitive(bool(self.vpn_uuid))

    def refresh_conn(self):
        self.update_conn()
        self.refresh_vpn()

    def set_vpn(self, uuid):
        """The selection changed (the settings file is already written)."""
        self.vpn_uuid = uuid
        self.refresh_conn()

    def find_active_uuid(self, uuid):
        if self.client is None or not uuid:
            return None
        for ac in self.client.get_active_connections():
            if ac.get_uuid() == uuid:
                return ac
        return None

    def find_active(self):
        return self.find_active_uuid(self.vpn_uuid)

    def watch_active(self, ac):
        """Subscribe to state changes of the active connection that was found."""
        if ac is self.ac:
            return
        if self.ac is not None and self.ac_handler:
            try:
                self.ac.disconnect(self.ac_handler)
            except Exception:
                pass
        self.ac = ac
        self.ac_handler = 0
        if ac is not None:
            self.ac_handler = ac.connect("notify::state", lambda *_: self.refresh_vpn())

    def activation_time(self):
        """Epoch of the last successful activation of the profile, or None."""
        try:
            ts = self.vpn_conn.get_setting_connection().get_timestamp()
            return int(ts) if ts else None
        except Exception as e:
            log("connection.timestamp: %s" % e)
            return None

    def refresh_vpn(self):
        if not self.vpn_uuid:
            self.watch_active(None)
            self.set_state(ICON_OFF, "VPN: not selected", "VPN not selected")
            return
        name = self.vpn_name()
        if self.client is None:
            self.set_state(ICON_OFF, "VPN: state unknown" + (" (%s)" % name if name else ""), "VPN: no data")
            return
        if name is None:  # the selected profile was deleted from NetworkManager
            self.watch_active(None)
            self.set_state(ICON_OFF, "VPN: profile not found", "VPN profile not found")
            return
        try:
            ac = self.find_active()
            self.watch_active(ac)
            state = ac.get_state() if ac is not None else None
        except Exception as e:
            log("VPN state: %s" % e)
            self.set_state(ICON_OFF, "VPN: state unknown (%s)" % name, "VPN: no data")
            return

        if state == NM.ActiveConnectionState.ACTIVATED:
            text = "VPN: connected"
            ts = self.activation_time()
            if ts is not None:
                text += " " + fmt_duration(max(0, time.time() - ts))
            self.set_state(ICON_ACTIVE, "%s (%s)" % (text, name), "VPN connected")
        elif state == NM.ActiveConnectionState.ACTIVATING:
            self.set_state(ICON_ACTIVATING, "VPN: connecting… (%s)" % name, "VPN connecting")
        else:
            self.set_state(ICON_OFF, "VPN: disconnected (%s)" % name, "VPN disconnected")

    def set_state(self, icon, label, title):
        if self.item_vpn.get_label() != label:
            self.item_vpn.set_label(label)
        if icon != self.last_icon:
            self.indicator.set_icon_full(icon, title)
            self.last_icon = icon
        if title != self.last_title:
            self.indicator.set_title(title)
            self.last_title = title

    # --- uptime -------------------------------------------------------------

    def refresh_uptime(self):
        try:
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            text = "Uptime: " + fmt_duration(up)
        except Exception as e:
            log("/proc/uptime: %s" % e)
            text = "Uptime: no data"
        if self.item_uptime.get_label() != text:
            self.item_uptime.set_label(text)

    # --- kill switch --------------------------------------------------------

    def refresh_killswitch(self):
        rc, out = run(["systemctl", "is-active", KILLSWITCH_UNIT])
        if rc is None:
            self.ks_active = None
            text = "Kill switch: status unknown"
        elif out.strip() != "active":
            self.ks_active = False
            text = "Kill switch: OFF"
        else:
            self.ks_active = True
            pkts = count_logged_drops()
            if pkts is None:
                text = "Kill switch: active, journal unavailable"
            else:
                text = "Kill switch: active, %d %s dropped" % (pkts, plural_packets(pkts))
        if self.item_ks.get_label() != text:
            self.item_ks.set_label(text)
        if self.settings_win is not None:
            self.settings_win.sync()

    # --- VPN drops ----------------------------------------------------------

    def refresh_drops(self):
        rc, out = run(["journalctl", "-b", "-q", "-o", "cat", "_COMM=pppd"])
        if rc != 0:
            text = "VPN drops since boot: no data"
        else:
            lcp = len(RE_LCP.findall(out))
            sig = len(RE_TERM.findall(out))
            conn = len(RE_CONN.findall(out))
            total = max(conn, lcp + sig)
            text = "VPN drops since boot: %d (server silent: %d)" % (total, lcp)
        if self.item_drops.get_label() != text:
            self.item_drops.set_label(text)


class SettingsWindow(Gtk.Window):
    """Settings window: VPN profile, kill switch and watchdog toggles. Applied by the "Save" button."""

    def __init__(self, tray):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.tray = tray
        self.baseline = False   # kill switch state in the system at open time / last poll
        self.wd_baseline = False  # watchdog state in the system at open time / after a failed step
        self.wd_want = False    # the watchdog switch position taken at Save
        self.wd_changed = False  # whether the watchdog needs a change (set by on_save)
        self.busy = False       # a Save sequence (sudo / nmcli commands) is still running
        self.closed = False
        self.proc = None        # reference to the Gio.Subprocess while it runs

        self.set_title("VPN kill-switch Settings")
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_resizable(False)
        self.set_default_size(520, -1)
        self.set_icon_name(ICON_ACTIVE)
        self.set_border_width(18)
        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key_press)

        # Row 1: VPN profile.
        vpn_title = Gtk.Label(halign=Gtk.Align.START, hexpand=True)
        vpn_title.set_markup("<b>VPN connection</b>")
        vpn_title.set_xalign(0)
        vpn_subtitle = Gtk.Label(
            label="Profile the tray, the watchdog, the statistics and the kill switch follow.",
            halign=Gtk.Align.START,
            hexpand=True,
        )
        vpn_subtitle.get_style_context().add_class("dim-label")
        vpn_subtitle.set_line_wrap(True)
        vpn_subtitle.set_xalign(0)

        vpn_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        vpn_texts.pack_start(vpn_title, False, False, 0)
        vpn_texts.pack_start(vpn_subtitle, False, False, 0)

        self.combo = Gtk.ComboBoxText(valign=Gtk.Align.CENTER, hexpand=False)
        profiles = tray.vpn_connections()
        self.has_profiles = bool(profiles)
        if profiles:
            for name, uuid in profiles:
                self.combo.append(uuid, name)
            if tray.vpn_uuid:
                self.combo.set_active_id(tray.vpn_uuid)
        else:
            self.combo.append("", "No VPN profiles in NetworkManager")
            self.combo.set_active(0)
        self.combo.set_sensitive(self.has_profiles)

        vpn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        vpn_box.set_border_width(12)
        vpn_box.pack_start(vpn_texts, True, True, 0)
        vpn_box.pack_start(self.combo, False, False, 0)

        vpn_row = Gtk.ListBoxRow(activatable=False, can_focus=False)
        vpn_row.add(vpn_box)

        # Row 2: kill switch. Row 3: watchdog.
        row, self.switch = switch_row(
            "Kill switch",
            "Block traffic outside the VPN. The setting persists across reboots.",
        )
        wd_row, self.switch_wd = switch_row(
            "Watchdog",
            "Reconnect the selected VPN profile automatically when it drops.",
        )
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.get_style_context().add_class("frame")
        listbox.add(vpn_row)
        listbox.add(row)
        listbox.add(wd_row)

        self.error_label = Gtk.Label(halign=Gtk.Align.START)
        self.error_label.set_line_wrap(True)
        self.error_label.set_xalign(0)
        self.error_label.set_selectable(True)
        self.error_label.set_no_show_all(True)
        self.error_label.hide()

        self.btn_cancel = Gtk.Button(label="Cancel")
        self.btn_cancel.connect("clicked", self.on_cancel)
        self.btn_save = Gtk.Button(label="Save")
        self.btn_save.get_style_context().add_class("suggested-action")
        self.btn_save.set_can_default(True)
        self.btn_save.connect("clicked", self.on_save)

        buttons = Gtk.ButtonBox(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_layout(Gtk.ButtonBoxStyle.END)
        buttons.add(self.btn_cancel)
        buttons.add(self.btn_save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.pack_start(listbox, False, False, 0)
        box.pack_start(self.error_label, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        self.set_default(self.btn_save)  # Enter = "Save"

        # Take the initial state from the current unit status, without running commands.
        self.tray.refresh_killswitch()
        self.baseline = bool(self.tray.ks_active)
        self.switch.set_active(self.baseline)
        # The watchdog is a user unit: read it directly (a quick `systemctl --user is-active`).
        # None (systemctl could not be run) counts as "off". Only the kill switch is re-polled.
        self.wd_baseline = bool(unit_active(["systemctl", "--user"], WATCHDOG_UNIT))
        self.switch_wd.set_active(self.wd_baseline)

        self.show_all()

    def on_destroy(self, *_):
        self.closed = True

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:  # Escape = "Cancel"
            self.on_cancel()
            return True
        return False

    def on_cancel(self, *_):
        self.destroy()

    def sync(self):
        """Called from Tray.refresh_killswitch: update the switch position (the combo is left alone)."""
        if self.closed or self.busy:
            return
        cur = bool(self.tray.ks_active)
        if self.switch.get_active() == self.baseline:  # the user has not touched the switch
            self.switch.set_active(cur)
        self.baseline = cur

    def show_error(self, text):
        self.error_label.set_text(text)
        self.error_label.show()

    def clear_error(self):
        self.error_label.hide()

    def set_controls_sensitive(self, value):
        self.switch.set_sensitive(value)
        self.switch_wd.set_sensitive(value)
        self.combo.set_sensitive(value and self.has_profiles)
        self.btn_cancel.set_sensitive(value)
        self.btn_save.set_sensitive(value)

    # --- Save: the steps run one after another, asynchronously ---------------

    def on_save(self, *_):
        if self.busy or self.closed:
            return
        new_uuid = self.combo.get_active_id() or None
        vpn_changed = new_uuid is not None and new_uuid != self.tray.vpn_uuid
        want = self.switch.get_active()
        ks_changed = want != self.baseline
        self.wd_want = self.switch_wd.get_active()
        self.wd_changed = self.wd_want != self.wd_baseline
        if not vpn_changed and not ks_changed and not self.wd_changed:  # no changes
            self.destroy()
            return
        self.set_controls_sensitive(False)
        self.busy = True
        self.clear_error()
        if vpn_changed:
            self.step_server(new_uuid, want, ks_changed)
        else:
            self.step_killswitch(want, ks_changed)

    def fail(self, text):
        """Stop the sequence and show the error; the controls are usable again."""
        self.busy = False
        if self.closed:
            return
        self.set_controls_sensitive(True)
        self.show_error(text)

    # Step 1: pass the server of the new profile to the kill switch (before anything is saved).
    def step_server(self, new_uuid, want, ks_changed):
        conn = None
        if self.tray.client is not None:
            conn = self.tray.client.get_connection_by_uuid(new_uuid)
        hosts = vpn_server_hosts(conn) if conn is not None else []
        if not hosts:
            self.fail("Cannot determine the VPN server for this profile; the kill switch was not updated.")
            return
        self.proc = run_async(
            ["sudo", "-n", HELPER, "set", *hosts],
            lambda ok, stderr: self.on_server_done(ok, stderr, new_uuid, want, ks_changed),
        )

    def on_server_done(self, ok, stderr, new_uuid, want, ks_changed):
        self.proc = None
        if not ok:
            stderr = (stderr or "").strip()
            if "password" in stderr.lower():
                self.fail(
                    "No passwordless permission to change the VPN profile. "
                    "Add this line to /etc/sudoers.d/vpn-stats:\n"
                    "%s ALL=(root) NOPASSWD: %s *" % (getpass.getuser(), HELPER)
                )
            else:
                self.fail("Failed to update the kill switch server list: " + stderr[:300])
            return
        # Step 2: remember the selection (the watchdog and the stats script read the file).
        old = self.tray.vpn_uuid
        try:
            save_settings(new_uuid)
        except Exception as e:
            log("save_settings: %s" % e)
            self.fail("Failed to save the settings: %s" % e)
            return
        self.tray.set_vpn(new_uuid)
        # Step 3: drop the old profile (if it is up), then start the new one in the background.
        if old and self.tray.find_active_uuid(old) is not None:
            self.proc = run_async(
                ["nmcli", "connection", "down", "uuid", old],
                lambda ok, stderr: self.on_down_done(new_uuid, want, ks_changed),
            )
        else:
            self.on_down_done(new_uuid, want, ks_changed)

    def on_down_done(self, new_uuid, want, ks_changed):
        self.proc = None
        self.tray.start_vpn(new_uuid)  # not awaited
        self.step_killswitch(want, ks_changed)

    # Step 4: the kill switch toggle. If it fails, the VPN change from steps 1-3 stays applied
    # (the profile is saved, the server list is updated); only the error is shown and the window
    # stays open. A retry then sees no VPN change. The watchdog step (5) is not attempted after
    # a failed kill switch step: only the kill switch error is shown, and a retry repeats the
    # kill switch step and then runs the watchdog step.
    def step_killswitch(self, want, ks_changed):
        if not ks_changed:
            self.step_watchdog()
            return
        action = "enable" if want else "disable"  # with --now: immediately and on subsequent boots
        self.proc = run_async(
            ["sudo", "-n", "systemctl", action, "--now", KILLSWITCH_UNIT],
            lambda ok, stderr: self.on_killswitch_done(want, ok, stderr),
        )

    def on_killswitch_done(self, want, ok, stderr):
        self.proc = None
        self.finish(want, ok, stderr)

    # Step 5: the watchdog toggle (a user unit, no sudo). If it fails, the earlier steps (the VPN
    # profile, the kill switch) stay applied; only the error is shown and the window stays open.
    # A retry then repeats only this step.
    def step_watchdog(self):
        if not self.wd_changed:
            self.finish_ok()
            return
        action = "enable" if self.wd_want else "disable"  # with --now: immediately and at the next login
        self.proc = run_async(
            ["systemctl", "--user", action, "--now", WATCHDOG_UNIT],
            self.on_watchdog_done,
        )

    def on_watchdog_done(self, ok, stderr):
        self.proc = None
        if ok:
            self.wd_baseline = self.wd_want
            self.finish_ok()
            return
        # Re-read the real state so that a retry compares against it.
        self.wd_baseline = bool(unit_active(["systemctl", "--user"], WATCHDOG_UNIT))
        self.busy = False
        if self.closed:
            return
        self.set_controls_sensitive(True)
        stderr = (stderr or "").strip()
        verb = "enable" if self.wd_want else "disable"
        msg = "Failed to %s the watchdog: %s" % (verb, stderr[:300] or "the unit did not change state")
        if "not found" in stderr or "No such file" in stderr or "does not exist" in stderr:
            msg += " Install it with watchdog/install-watchdog.sh."  # the unit file is not installed
        self.show_error(msg)

    def finish_ok(self):
        self.tray.refresh_killswitch()
        self.busy = False
        if not self.closed:
            self.destroy()

    def finish(self, want, ok, stderr):
        """The kill switch systemctl command finished (or failed to start): refresh the status,
        then continue with the watchdog step or show the error."""
        # busy is still True: sync() inside refresh_killswitch does not touch the switch,
        # so the user's choice stays in place until the result is shown.
        self.tray.refresh_killswitch()  # updates the menu line and tray.ks_active
        self.baseline = bool(self.tray.ks_active)
        if ok and self.tray.ks_active == want:
            self.step_watchdog()  # busy stays True until the whole sequence is over
            return
        self.busy = False
        if self.closed:
            return
        self.set_controls_sensitive(True)
        stderr = (stderr or "").strip()
        if "password" in stderr.lower():
            user = getpass.getuser()
            self.show_error(
                "No passwordless permission to manage the kill switch. "
                "Add these lines to /etc/sudoers.d/vpn-stats:\n"
                "%s ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now %s\n"
                "%s ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now %s" % (user, KILLSWITCH_UNIT, user, KILLSWITCH_UNIT)
            )
        else:
            verb = "enable" if want else "disable"
            if not stderr:
                stderr = "the unit did not reach the requested state"
            self.show_error("Failed to %s the kill switch: %s" % (verb, stderr[:300]))


class DropsWindow(Gtk.Window):
    """Dropped packets: the kernel log lines of the kill switch `log` rules, read from the journal."""

    PERIODS = (  # id, label, journalctl arguments
        ("1h", "Last hour", ["--since", "-1h"]),
        ("6h", "Last 6 hours", ["--since", "-6h"]),
        ("24h", "Last 24 hours", ["--since", "-24h"]),
        ("session", "This session", None),  # since the tray started, computed in session_args()
        ("boot", "Since boot", ["-b"]),
    )
    # Column titles and the store column each header sorts by. The store rows are
    # [display strings..., epoch (float), count (int), dst IP (str)]; Host is always a display column.
    GROUPED_COLUMNS = ("Last seen", "Count", "Proto", "Destination", "Interface", "User", "Host")
    RAW_COLUMNS = ("Time", "Proto", "Source", "Destination", "Interface", "User", "Flags", "Chain", "Host")
    RESOLVE_THREADS = 4
    RESOLVE_TIMEOUT = 2.0

    def __init__(self, tray):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.tray = tray
        self.closed = False
        self.loading = False
        self.proc = None            # reference to the Gio.Subprocess while it runs
        self.records = []           # parsed log lines of the last load
        self.store = None           # the Gtk.ListStore behind the current table
        self.sorted_model = None
        self.sort_state = {}        # view mode -> (sort column, order), kept across reloads
        self.host_cache = {}        # dst IP -> host name ("" if it did not resolve), for the window's lifetime
        self.resolving = set()      # dst IPs being resolved right now
        self.host_update_id = 0     # pending GLib source that refreshes the Host column

        self.set_title("Dropped packets")
        self.set_default_size(820, 440)
        self.set_resizable(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name(ICON_ACTIVE)
        self.set_border_width(12)
        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key_press)

        # Top row: period, options, buttons.
        self.period_combo = Gtk.ComboBoxText()
        for pid, label, _args in self.PERIODS:
            self.period_combo.append(pid, label)
        self.period_combo.set_active_id("boot")
        self.group_check = Gtk.CheckButton(label="Group identical flows")
        self.group_check.set_active(True)
        self.resolve_check = Gtk.CheckButton(label="Resolve hostnames")
        self.resolve_check.set_active(False)
        self.btn_refresh = Gtk.Button(label="Refresh")
        btn_close = Gtk.Button(label="Close")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top.pack_start(Gtk.Label(label="Period:"), False, False, 0)
        top.pack_start(self.period_combo, False, False, 0)
        top.pack_start(self.group_check, False, False, 6)
        top.pack_start(self.resolve_check, False, False, 0)
        top.pack_start(Gtk.Box(), True, True, 0)  # spacer
        top.pack_start(self.btn_refresh, False, False, 0)
        top.pack_start(btn_close, False, False, 0)

        # Middle: the table.
        self.view = Gtk.TreeView()
        self.view.set_headers_clickable(True)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(self.view)

        # Bottom: status.
        self.status = Gtk.Label(halign=Gtk.Align.START)
        self.status.set_xalign(0)
        self.status.set_line_wrap(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(top, False, False, 0)
        box.pack_start(scroll, True, True, 0)
        box.pack_start(self.status, False, False, 0)
        self.add(box)

        self.period_combo.connect("changed", lambda *_: self.reload())
        self.group_check.connect("toggled", self.on_group_toggled)
        self.resolve_check.connect("toggled", self.on_resolve_toggled)
        self.btn_refresh.connect("clicked", lambda *_: self.reload())
        btn_close.connect("clicked", lambda *_: self.destroy())

        self.render()
        self.show_all()
        self.reload()

    def on_destroy(self, *_):
        self.closed = True
        if self.host_update_id:
            GLib.source_remove(self.host_update_id)
            self.host_update_id = 0

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    # --- loading -------------------------------------------------------------

    def set_loading(self, value):
        self.loading = value
        for w in (self.period_combo, self.group_check, self.resolve_check, self.btn_refresh):
            w.set_sensitive(not value)

    def session_args(self):
        """journalctl arguments for "This session": since this tray process started watching."""
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.tray.started))
        return ["--since", since]

    def reload(self):
        if self.closed or self.loading:
            return
        period_args = next(a for pid, _l, a in self.PERIODS if pid == self.period_combo.get_active_id())
        if period_args is None:
            period_args = self.session_args()
        self.set_loading(True)
        self.status.set_text("Loading…")
        # `_TRANSPORT=kernel` instead of `-k`: `-k` implies `-b`, which would cut the
        # longer periods at the last boot. "Since boot" passes `-b` explicitly.
        argv = ["journalctl", "-q", "-o", "short-unix", "--no-pager",
                "--grep", "^" + DROP_LOG_PREFIX, *period_args, "_TRANSPORT=kernel"]
        self.proc = run_async(argv, self.on_loaded, want_stdout=True)

    def on_loaded(self, ok, stderr, stdout):
        self.proc = None
        if self.closed:
            return
        self.set_loading(False)
        stderr = (stderr or "").strip()
        if not ok and (stderr or (stdout or "").strip()):
            log("journalctl: %s" % stderr)
            self.records = []
            self.render()
            self.status.set_text("Failed to read the journal: %s" % (stderr[:200] or "unexpected exit status"))
            return
        # ok, or exit status 1 with no output: journalctl found no line matching --grep
        recs = [r for r in map(parse_drop_line, (stdout or "").splitlines()) if r is not None]
        self.records = recs
        self.render()
        if recs:
            self.status.set_text("%d %s logged in this period · logging is rate-limited to %s" % (
                len(recs), plural_packets(len(recs)), DROP_LOG_LIMIT))
        else:
            self.status.set_text("No dropped packets logged in this period")
        if self.resolve_check.get_active():
            self.start_resolve()

    # --- table ---------------------------------------------------------------

    def on_group_toggled(self, *_):
        if not self.loading:
            self.render()

    def hostname(self, dst_ip):
        if not self.resolve_check.get_active():
            return ""
        return self.host_cache.get(dst_ip, "")

    def render(self):
        """Fill the table from self.records in the grouped or the raw mode."""
        grouped = self.group_check.get_active()
        self.remember_sort()
        if grouped:
            # [Last seen, Count, Proto, Destination, Interface, User, Host | epoch, count, dst IP]
            store = Gtk.ListStore(str, str, str, str, str, str, str, float, int, str)
            for g in group_drops(self.records):
                store.append([fmt_time(g["last"]), str(g["count"]), g["proto"], fmt_dst(g), g["out"],
                              fmt_user(g["uid"]), self.hostname(g["dst"]), g["last"], g["count"], g["dst"]])
            titles, sort_ids, default_sort = self.GROUPED_COLUMNS, [7, 8, 2, 3, 4, 5, 6], (7, Gtk.SortType.DESCENDING)
        else:
            # [Time, Proto, Source, Destination, Interface, User, Flags, Chain, Host | epoch, count, dst IP]
            store = Gtk.ListStore(str, str, str, str, str, str, str, str, str, float, int, str)
            for r in sorted(self.records, key=lambda r: r["time"], reverse=True):
                src = r["src"]
                if r["proto"] in ("TCP", "UDP") and r["spt"] is not None:
                    src = "[%s]:%d" % (src, r["spt"]) if ":" in src else "%s:%d" % (src, r["spt"])
                store.append([fmt_time(r["time"]), r["proto"], src, fmt_dst(r), r["out"], fmt_user(r["uid"]),
                              " ".join(r["flags"]), r["chain"], self.hostname(r["dst"]), r["time"], 1, r["dst"]])
            titles, sort_ids, default_sort = self.RAW_COLUMNS, [9, 1, 2, 3, 4, 5, 6, 7, 8], (9, Gtk.SortType.DESCENDING)
        self.mode = "grouped" if grouped else "raw"
        self.host_col = len(titles) - 1
        self.dst_col = 9 if grouped else 11
        self.store = store
        # The view sorts through a TreeModelSort, so updating the Host column of the plain store
        # never reorders the rows while they are being walked.
        self.sorted_model = Gtk.TreeModelSort(model=store)
        for col in self.view.get_columns():
            self.view.remove_column(col)
        for i, (title, sort_id) in enumerate(zip(titles, sort_ids)):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            col.set_resizable(True)
            col.set_sort_column_id(sort_id)
            self.view.append_column(col)
        self.sorted_model.set_sort_column_id(*self.sort_state.get(self.mode, default_sort))
        self.view.set_model(self.sorted_model)

    def remember_sort(self):
        """Keep the sort order the user picked in the current mode across re-renders."""
        if self.sorted_model is None:
            return
        col, order = self.sorted_model.get_sort_column_id()  # (None, None) while unsorted
        if col is not None and col >= 0:
            self.sort_state[self.mode] = (col, order)

    # --- hostname resolution -------------------------------------------------

    def on_resolve_toggled(self, *_):
        if self.resolve_check.get_active():
            self.start_resolve()
        self.apply_hosts()

    def start_resolve(self):
        """Resolve every distinct destination not in the cache, in daemon threads (never blocks the UI)."""
        todo = []
        for r in sorted(self.records, key=lambda r: r["time"], reverse=True):
            ip = r["dst"]
            if ip and ip not in self.host_cache and ip not in self.resolving and ip not in todo:
                todo.append(ip)
        if not todo:
            return
        self.resolving.update(todo)
        pending = queue.Queue()
        for ip in todo:
            pending.put(ip)
        for _ in range(min(self.RESOLVE_THREADS, len(todo))):
            threading.Thread(target=self.resolve_worker, args=(pending,), daemon=True).start()

    def resolve_worker(self, pending):
        """Thread body: no GTK calls here, results go to the main loop through GLib.idle_add."""
        socket.setdefaulttimeout(self.RESOLVE_TIMEOUT)
        while not self.closed:
            try:
                ip = pending.get_nowait()
            except queue.Empty:
                return
            try:
                name = socket.gethostbyaddr(ip)[0]
            except (OSError, UnicodeError):  # socket.herror / gaierror / timeout are OSError subclasses
                name = ""
            GLib.idle_add(self.on_resolved, ip, name)

    def on_resolved(self, ip, name):
        self.resolving.discard(ip)
        self.host_cache[ip] = name
        if not self.closed and self.resolve_check.get_active() and not self.host_update_id:
            self.host_update_id = GLib.timeout_add(300, self.apply_hosts_timer)  # batch the updates
        return GLib.SOURCE_REMOVE

    def apply_hosts_timer(self):
        self.host_update_id = 0
        self.apply_hosts()
        return GLib.SOURCE_REMOVE

    def apply_hosts(self):
        """Set the Host column of the current rows from the cache (empty when resolving is off)."""
        if self.closed or self.store is None:
            return
        for row in self.store:
            host = self.hostname(row[self.dst_col])
            if row[self.host_col] != host:
                row[self.host_col] = host


def acquire_lock():
    """Do not start a second instance: otherwise the panel would show two icons."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    f = open(os.path.join(runtime, "vpn-tray.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("vpn-tray is already running, exiting.")
        sys.exit(0)
    return f


def main():
    lock = acquire_lock()  # keep the reference until exit
    tray = Tray()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: Gtk.main_quit() or GLib.SOURCE_REMOVE)
    Gtk.main()
    del tray, lock


if __name__ == "__main__":
    main()
