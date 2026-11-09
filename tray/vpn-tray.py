#!/usr/bin/env python3
# VPN-L2TP icon in the GNOME top bar: VPN state, uptime, kill switch counter,
# number of VPN drops. Uses the AppIndicator protocol (StatusNotifierItem).
# Run: ./vpn-tray.py (normally via the user unit vpn-tray.service).

import fcntl
import getpass
import os
import re
import signal
import subprocess
import sys
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("NM", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, NM  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402

VPN_NAME = "VPN-L2TP"
KILLSWITCH_UNIT = "vpn-killswitch.service"

ICON_ACTIVE = "org.gnome.Settings-network-symbolic"  # the "Network" icon from GNOME Settings
ICON_ACTIVATING = "network-wired-acquiring-symbolic"
ICON_OFF = "network-wired-disconnected-symbolic"

# pppd line patterns. `journalctl -o cat` prints only the message text,
# without the "pppd[PID]:" prefix; the process is selected via _COMM=pppd.
RE_LCP = re.compile(r"^No response to [0-9]+ echo-requests", re.M)
RE_TERM = re.compile(r"^Terminating on signal", re.M)
RE_CONN = re.compile(r"^Connection terminated\.", re.M)

FAST_SECONDS = 5        # VPN state, uptime (fallback polling)
KILLSWITCH_SECONDS = 60  # kill switch and packet counter (each sudo poll writes to the journal)
JOURNAL_SECONDS = 60    # VPN drops from the journal


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


class Tray:
    def __init__(self):
        self.client = None
        try:
            self.client = NM.Client.new(None)
        except Exception as e:
            log("NetworkManager unavailable: %s" % e)

        self.ac = None          # active VPN-L2TP connection (if any)
        self.ac_handler = 0
        self.procs = []         # background nmcli processes, to avoid zombies
        self.last_icon = None
        self.last_title = None

        self.ks_active = None   # True/False, None if `systemctl is-active` failed to run
        self.settings_win = None  # settings window while it is open
        self.ks_text = "Kill switch: checking…"
        self.drops_text = "VPN drops since boot: checking…"

        self.indicator = AppIndicator.Indicator.new(
            "vpn-l2tp-tray",
            ICON_OFF,
            AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

        # Informational items stay sensitive (otherwise they are greyed out) and do nothing.
        self.item_vpn = Gtk.MenuItem(label="VPN-L2TP: …")
        self.item_uptime = Gtk.MenuItem(label="Uptime: …")
        self.item_ks = Gtk.MenuItem(label=self.ks_text)
        self.item_drops = Gtk.MenuItem(label=self.drops_text)
        item_reconnect = Gtk.MenuItem(label="Reconnect VPN")
        item_reconnect.connect("activate", self.on_reconnect)
        item_settings = Gtk.MenuItem(label="Settings…")
        item_settings.connect("activate", self.on_settings)
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda *_: Gtk.main_quit())

        self.menu = Gtk.Menu()
        for item in (self.item_vpn, self.item_uptime, self.item_ks, self.item_drops):
            self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(item_reconnect)
        self.menu.append(item_settings)
        self.menu.append(item_quit)
        self.menu.connect("show", self.on_menu_show)
        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        if self.client is not None:
            self.client.connect("notify::active-connections", lambda *_: self.refresh_vpn())

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
        try:
            p = subprocess.Popen(
                ["nmcli", "connection", "up", VPN_NAME],
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

    # --- VPN ----------------------------------------------------------------

    def find_active(self):
        if self.client is None:
            return None
        for ac in self.client.get_active_connections():
            if ac.get_id() == VPN_NAME:
                return ac
        return None

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
            conn = self.client.get_connection_by_id(VPN_NAME)
            ts = conn.get_setting_connection().get_timestamp()
            return int(ts) if ts else None
        except Exception as e:
            log("connection.timestamp: %s" % e)
            return None

    def refresh_vpn(self):
        if self.client is None:
            self.set_state(ICON_OFF, "VPN-L2TP: state unknown", "VPN: no data")
            return
        try:
            ac = self.find_active()
            self.watch_active(ac)
            state = ac.get_state() if ac is not None else None
        except Exception as e:
            log("VPN state: %s" % e)
            self.set_state(ICON_OFF, "VPN-L2TP: state unknown", "VPN: no data")
            return

        if state == NM.ActiveConnectionState.ACTIVATED:
            text = "VPN-L2TP: connected"
            ts = self.activation_time()
            if ts is not None:
                text += " " + fmt_duration(max(0, time.time() - ts))
            self.set_state(ICON_ACTIVE, text, "VPN connected")
        elif state == NM.ActiveConnectionState.ACTIVATING:
            self.set_state(ICON_ACTIVATING, "VPN-L2TP: connecting…", "VPN connecting")
        else:
            self.set_state(ICON_OFF, "VPN-L2TP: disconnected", "VPN disconnected")

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
            pkts = self.read_drop_counter()
            if pkts is None:
                text = "Kill switch: active, counter unavailable"
            else:
                text = "Kill switch: active, %d %s dropped" % (pkts, plural_packets(pkts))
        if self.item_ks.get_label() != text:
            self.item_ks.set_label(text)
        if self.settings_win is not None:
            self.settings_win.sync()

    def read_drop_counter(self):
        rc, out = run(["sudo", "-n", "nft", "list", "chain", "inet", "killswitch", "output"])
        if rc != 0:
            return None
        for line in out.splitlines():
            if " drop" in line:
                m = re.search(r"counter packets (\d+)", line)
                if m:
                    return int(m.group(1))
        return None

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
    """Settings window: for now a single kill switch toggle. Applied by the "Save" button."""

    def __init__(self, tray):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.tray = tray
        self.baseline = False   # kill switch state in the system at open time / last poll
        self.busy = False       # the systemctl command is still running
        self.closed = False
        self.proc = None        # reference to the Gio.Subprocess while it runs

        self.set_title("VPN-L2TP Settings")
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_resizable(False)
        self.set_default_size(440, -1)
        self.set_icon_name(ICON_ACTIVE)
        self.set_border_width(18)
        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key_press)

        title = Gtk.Label(halign=Gtk.Align.START, hexpand=True)
        title.set_markup("<b>Kill switch</b>")
        title.set_xalign(0)
        subtitle = Gtk.Label(
            label="Block traffic outside the VPN. The setting persists across reboots.",
            halign=Gtk.Align.START,
            hexpand=True,
        )
        subtitle.get_style_context().add_class("dim-label")
        subtitle.set_line_wrap(True)
        subtitle.set_xalign(0)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        texts.pack_start(title, False, False, 0)
        texts.pack_start(subtitle, False, False, 0)

        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER, hexpand=False)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_border_width(12)
        row_box.pack_start(texts, True, True, 0)
        row_box.pack_start(self.switch, False, False, 0)

        row = Gtk.ListBoxRow()
        row.add(row_box)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.get_style_context().add_class("frame")
        listbox.add(row)

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
        """Called from Tray.refresh_killswitch: update the switch position."""
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
        self.btn_cancel.set_sensitive(value)
        self.btn_save.set_sensitive(value)

    def on_save(self, *_):
        if self.busy or self.closed:
            return
        want = self.switch.get_active()
        if want == self.baseline:  # no changes
            self.destroy()
            return
        action = "enable" if want else "disable"  # with --now: immediately and on subsequent boots
        self.set_controls_sensitive(False)
        self.busy = True
        self.clear_error()
        try:
            self.proc = Gio.Subprocess.new(
                ["sudo", "-n", "systemctl", action, "--now", KILLSWITCH_UNIT],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
            self.proc.communicate_utf8_async(None, None, self.on_command_done, want)
        except Exception as e:
            log("sudo systemctl %s: %s" % (action, e))
            self.proc = None
            self.finish(want, False, str(e))

    def on_command_done(self, proc, res, want):
        stderr = ""
        ok = False
        try:
            _, _, stderr = proc.communicate_utf8_finish(res)
            ok = proc.get_if_exited() and proc.get_exit_status() == 0
        except Exception as e:
            log("sudo systemctl: %s" % e)
            stderr = str(e)
        self.proc = None
        self.finish(want, ok, stderr or "")

    def finish(self, want, ok, stderr):
        """The command finished (or failed to start): refresh the status and show the result."""
        # busy is still True: sync() inside refresh_killswitch does not touch the switch,
        # so the user's choice stays in place until the result is shown.
        self.tray.refresh_killswitch()  # updates the menu line and tray.ks_active
        self.busy = False
        if self.closed:
            return
        self.baseline = bool(self.tray.ks_active)
        if ok and self.tray.ks_active == want:
            self.destroy()
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
