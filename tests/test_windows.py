# SPDX-License-Identifier: MIT
"""The journal windows (Dropped packets, VPN drops for both backends), the About dialog and the
Tray's one-window-at-a-time handling. The journal is a fixture served by a stubbed run_async.
Needs a display."""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import AsyncStub, Clock, FakeClient, Results, make_tray, quiet, vpn_conn  # noqa: E402

vt = common.load_tray()
common.require_display()
from gi.repository import Gdk, Gtk  # noqa: E402

T = Results("test_windows")
check, eq = T.check, T.eq
clock = Clock()
vt.time = clock

DROPS = common.journal_lines("ks-drop.journal")
NM_JOURNAL = common.journal_lines("nm-pppd.journal")
HID_JOURNAL = common.journal_lines("hiddify.journal")
STARTED = 1795114000.0       # 2026-11-19 18:46:40 UTC


class WinTray:
    def __init__(self, backend="nm", name="My VPN", active=False):
        self.backend, self.name, self.active, self.started = backend, name, active, STARTED

    def vpn_name(self):
        return self.name

    def vpn_activated(self):
        return self.active


def text(lines):
    return "\n".join(lines) + "\n" if lines else ""


def is_kernel_query(argv):
    return "_TRANSPORT=kernel" in argv


def key_event(keyval):
    ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    ev.key.keyval = keyval
    return ev


def rows(window, ncols=None):
    return [list(r)[:ncols] if ncols else list(r) for r in window.view.get_model()]


def titles(window):
    return [c.get_title() for c in window.view.get_columns()]


BASE_DROPS = ["journalctl", "-q", "-o", "short-unix", "--no-pager", "--grep", "^KS-DROP"]
BASE_VPN = ["journalctl", "-q", "-o", "short-unix", "--no-pager"]
PERIOD_ARGS = {"1h": ["--since", "-1h"], "6h": ["--since", "-6h"], "24h": ["--since", "-24h"],
               "session": ["--since", "2026-11-19 18:46:40"], "boot": ["-b"], "all": []}
PERIOD_LABELS = ["Last hour", "Last 6 hours", "Last 24 hours", "This session", "Since boot", "All time"]


# ================= Dropped packets =========================================================
stub = AsyncStub(lambda argv: (True, "", text(DROPS)))
vt.run_async = stub
w = vt.DropsWindow(WinTray())
eq("title", w.get_title(), "Dropped packets")
eq("the first query is 'since boot'", stub.calls, [BASE_DROPS + ["-b", "_TRANSPORT=kernel"]])
eq("period labels", [r[0] for r in w.period_combo.get_model()], PERIOD_LABELS)
eq("default period", w.period_combo.get_active_id(), "boot")
eq("status", w.status.get_text(), "7 packets logged in this period · logging is rate-limited to 10/s")
check("controls usable after the load", not w.loading and w.period_combo.get_sensitive() and w.btn_refresh.get_sensitive()
      and w.group_check.get_sensitive())
eq("grouped is the default view", (w.mode, w.group_check.get_active(), w.resolve_check.get_active()), ("grouped", True, False))
eq("grouped columns", titles(w), list(vt.DropsWindow.GROUPED_COLUMNS))
eq("default sort: newest first", w.sorted_model.get_sort_column_id(), (7, Gtk.SortType.DESCENDING))
t = vt.fmt_time
want = [
    [t(1795114536.0), "1", "TCP", "9.9.9.9:80", "wlan0", "root", ""],               # the glued old prefix
    [t(1795114535.0), "1", "TCP", "93.184.216.34:443", "wlan0", "", ""],             # the forward chain, no UID
    [t(1795114534.0), "1", "ICMP", "1.1.1.1 (8/0)", "wlan0", "root", ""],
    [t(1795114533.0), "1", "UDP", "8.8.8.8:53", "eth0", "root", ""],
    [t(1795114532.0), "3", "TCP", "93.184.216.34:443", "wlan0", "root", ""],         # three packets of one flow
]
eq("grouped rows", rows(w, 7), want)
eq("grouped: epoch / count / dst in the hidden columns", [(r[7], r[8], r[9]) for r in rows(w)],
   [(1795114536.0, 1, "9.9.9.9"), (1795114535.0, 1, "93.184.216.34"), (1795114534.0, 1, "1.1.1.1"),
    (1795114533.0, 1, "8.8.8.8"), (1795114532.0, 3, "93.184.216.34")])

# raw view
w.group_check.set_active(False)
eq("raw mode", w.mode, "raw")
eq("raw columns", titles(w), list(vt.DropsWindow.RAW_COLUMNS))
raw = rows(w, 9)
eq("raw: one row per packet (the unrelated line is ignored)", len(raw), 7)
eq("raw: newest first", [r[0] for r in raw], [t(x) for x in (1795114536.0, 1795114535.0, 1795114534.0, 1795114533.0, 1795114532.0, 1795114531.5, 1795114531.1)])
eq("raw: glued prefix row", raw[0][1:], ["TCP", "192.168.1.5:1111", "9.9.9.9:80", "wlan0", "root", "DF SYN", "output", ""])
eq("raw: forward row", raw[1][1:], ["TCP", "172.17.0.2:50000", "93.184.216.34:443", "wlan0", "", "DF SYN", "forward", ""])
eq("raw: ICMP row has no source port", raw[2][1:], ["ICMP", "192.168.1.5", "1.1.1.1 (8/0)", "wlan0", "root", "DF", "output", ""])
eq("raw: UDP row", raw[3][1:5], ["UDP", "10.0.0.2:5353", "8.8.8.8:53", "eth0"])
eq("raw: sorted by time descending by default", w.sorted_model.get_sort_column_id(), (9, Gtk.SortType.DESCENDING))
w.group_check.set_active(True)
eq("back to grouped: the same rows", rows(w, 7), want)
# the user's sort order is kept across the toggle
w.view.get_columns()[1].clicked()      # Count
col = w.sorted_model.get_sort_column_id()
w.group_check.set_active(False)
w.group_check.set_active(True)
eq("a chosen sort order survives a view switch", w.sorted_model.get_sort_column_id(), col)
eq("the Count sort is by number", col[0], 8)

# hostnames: only from the cache (no DNS in a test)
w.host_cache = {ip: "host-" + ip.replace(".", "-") for ip in ("93.184.216.34", "8.8.8.8", "1.1.1.1", "9.9.9.9")}
eq("Host column empty while resolving is off", {r[6] for r in rows(w)}, {""})
w.resolve_check.set_active(True)
eq("resolving: nothing left to look up", w.resolving, set())
eq("Host column filled from the cache", {(r[3], r[6]) for r in rows(w, 7)},
   {("9.9.9.9:80", "host-9-9-9-9"), ("93.184.216.34:443", "host-93-184-216-34"), ("1.1.1.1 (8/0)", "host-1-1-1-1"),
    ("8.8.8.8:53", "host-8-8-8-8")})
w.group_check.set_active(False)
eq("raw Host column", {r[8] for r in rows(w, 9)} - {""}, {"host-9-9-9-9", "host-93-184-216-34", "host-1-1-1-1", "host-8-8-8-8"})
w.resolve_check.set_active(False)
eq("unchecking empties the Host column", {r[8] for r in rows(w, 9)}, {""})

# refresh and periods
stub.calls.clear()
w.btn_refresh.emit("clicked")
eq("Refresh re-runs the query", stub.calls, [BASE_DROPS + ["-b", "_TRANSPORT=kernel"]])
stub.calls.clear()
for pid in ("1h", "6h", "24h", "session", "all", "boot"):
    w.period_combo.set_active_id(pid)
    eq("period %s" % pid, stub.calls[-1], BASE_DROPS + PERIOD_ARGS[pid] + ["_TRANSPORT=kernel"])
eq("one query per period change", len(stub.calls), 6)
check("Escape closes the window", not w.closed)
w.emit("key-press-event", key_event(Gdk.KEY_a))
check("another key does not", not w.closed)
w.emit("key-press-event", key_event(Gdk.KEY_Escape))
check("Escape closes the window", w.closed)

# statuses
def drops_window(result):
    vt.run_async = AsyncStub(lambda argv: result)
    win = vt.DropsWindow(WinTray())
    out = (win.status.get_text(), len(rows(win)), win.loading, win.period_combo.get_sensitive(), win.records)
    win.destroy()
    return out


one = DROPS[1:2]
eq("one packet: singular", drops_window((True, "", text(one)))[:2], ("1 packet logged in this period · logging is rate-limited to 10/s", 1))
eq("exit 1 without output: nothing logged", drops_window((False, "", ""))[:2], ("No dropped packets logged in this period", 0))
eq("ok without output", drops_window((True, "", ""))[:2], ("No dropped packets logged in this period", 0))
eq("only unparsable lines", drops_window((True, "", "kernel junk\nmore junk\n"))[:2], ("No dropped packets logged in this period", 0))
with quiet():
    got = drops_window((False, "Failed to open the journal: boom\n", ""))
eq("journalctl error", got[:2], ("Failed to read the journal: Failed to open the journal: boom", 0))
check("... controls usable again", got[2] is False and got[3] is True)
with quiet():
    eq("a long error is cut at 200 characters", drops_window((False, "e" * 300, ""))[0], "Failed to read the journal: " + "e" * 200)
    eq("output without an exit status 0", drops_window((False, "", "oops\n"))[0], "Failed to read the journal: unexpected exit status")
# an error after a good load clears the table
vt.run_async = AsyncStub(lambda argv: (True, "", text(DROPS)))
w = vt.DropsWindow(WinTray())
eq("loaded", len(rows(w)), 5)
vt.run_async = AsyncStub(lambda argv: (False, "denied", ""))
with quiet():
    w.reload()
eq("a later failure empties the table", (len(rows(w)), w.status.get_text()), (0, "Failed to read the journal: denied"))
w.destroy()

# ================= VPN drops: NetworkManager backend ===========================================
def vpn_handler(journal):
    kernel = [l for l in journal if "kernel:" in l]
    main = [l for l in journal if "kernel:" not in l]
    return lambda argv: (True, "", text(kernel if is_kernel_query(argv) else main))


MAIN_Q = ["_COMM=pppd", "+", "_SYSTEMD_UNIT=NetworkManager.service", "+", "UNIT=NetworkManager.service"]
KERNEL_Q = ["_TRANSPORT=kernel", "--grep", "PM: suspend (entry|exit)|^Linux version "]
clock.now = 5100.0
stub = AsyncStub(vpn_handler(NM_JOURNAL))
vt.run_async = stub
w = vt.VpnDropsWindow(WinTray("nm", "My VPN", active=False))
eq("VPN drops: title", w.get_title(), "VPN drops")
eq("VPN drops: the two queries", stub.calls, [BASE_VPN + ["-b"] + MAIN_Q, BASE_VPN + ["-b"] + KERNEL_Q])
eq("VPN drops: columns", titles(w), ["Time", "Event", "Details", "Downtime"])
eq("VPN drops: period labels", [r[0] for r in w.period_combo.get_model()], PERIOD_LABELS)
eq("VPN drops: status", w.status.get_text(),
   "6 drops (server silent: 2) · total time without VPN: 4m 45s · longest: 2m 00s")
check("VPN drops: controls usable", not w.loading and w.period_combo.get_sensitive() and w.btn_refresh.get_sensitive())
eq("VPN drops: default sort is the time, newest first", w.sorted_model.get_sort_column_id(), (4, Gtk.SortType.DESCENDING))

LCP, CMD, SUSP, OTHER = ("server stopped responding (LCP)", "on command", "suspend", "other")
CONNECTED = "default route via ppp0"
EXPECT = [   # time, event, details, downtime text (oldest first)
    (100, "Boot", "", ""), (110, "Connecting", "starting", ""), (120, "Connected", CONNECTED, ""),
    (1000, "Drop", LCP, "25s"), (1010, "Connecting", "starting", ""), (1025, "Connected", CONNECTED, ""),
    (2000, "Drop", CMD, "30s"), (2030, "Connected", CONNECTED, ""), (3000, "Suspend", "", ""),
    (3002, "Drop", SUSP, "10s"), (3300, "Wake", "", ""), (3310, "Connected", CONNECTED, ""),
    (3400, "NetworkManager restart", "", ""), (4000, "Drop", OTHER, "2m 00s"), (4010, "Connecting", "starting", ""),
    (4040, "Connect failed", "timeout", ""), (4050, "Connecting", "starting", ""),
    (4055, "Connect failed", "no password agent", ""), (4100, "Connecting", "starting", ""),
    (4120, "Connected", CONNECTED, ""), (5000, "Drop", CMD, "ongoing"), (5010, "Drop", LCP, ""),
]
want = [[vt.fmt_time(ts), kind, detail, dt] for ts, kind, detail, dt in reversed(EXPECT)]
eq("VPN drops: rows, newest first", rows(w, 4), want)
eq("VPN drops: only the Drop rows are bold", [(r[1], r[5]) for r in rows(w) if (r[5] == 700) != (r[1] == "Drop")], [])
eq("VPN drops: the number of bold rows", sum(1 for r in rows(w) if r[5] == 700), 6)
eq("VPN drops: the hidden time column is the epoch", [r[4] for r in rows(w)][:3], [5010.0, 5000.0, 4120.0])

# sorting survives a reload
w.view.get_columns()[1].clicked()      # Event
sort = w.sorted_model.get_sort_column_id()
eq("Event column sorts by its text", sort[0], 1)
stub.calls.clear()
w.btn_refresh.emit("clicked")
eq("Refresh re-runs both queries", len(stub.calls), 2)
eq("the sort order is kept after a reload", w.sorted_model.get_sort_column_id(), sort)
eq("... and the rows are sorted by event", [r[1] for r in rows(w)], sorted([e[1] for e in EXPECT]))
w.view.get_columns()[3].clicked()      # Downtime: "ongoing" is the longest
w.view.get_columns()[3].clicked()      # clicked twice = descending
check("Downtime sorts by seconds, 'ongoing' last-longest", rows(w, 4)[0][3] == "ongoing", rows(w, 4)[:2])

# periods
stub.calls.clear()
for pid in ("1h", "6h", "24h", "session", "all", "boot"):
    w.period_combo.set_active_id(pid)
    eq("VPN drops period %s" % pid, stub.calls[-2:], [BASE_VPN + PERIOD_ARGS[pid] + MAIN_Q, BASE_VPN + PERIOD_ARGS[pid] + KERNEL_Q])
w.emit("key-press-event", key_event(Gdk.KEY_Escape))
check("VPN drops: Escape closes", w.closed)

# the VPN is up now: the open outage is not counted
vt.run_async = AsyncStub(vpn_handler(NM_JOURNAL))
w = vt.VpnDropsWindow(WinTray("nm", "My VPN", active=True))
eq("VPN up now: status", w.status.get_text(), "6 drops (server silent: 2) · total time without VPN: 3m 05s · longest: 2m 00s")
eq("VPN up now: no 'ongoing' row", [r[3] for r in rows(w, 4) if r[3] == "ongoing"], [])
w.destroy()

# another profile
vt.run_async = AsyncStub(vpn_handler(NM_JOURNAL))
w = vt.VpnDropsWindow(WinTray("nm", "Other VPN"))
eq("another profile's own rows", [(r[1], r[2]) for r in rows(w) if r[1] in ("Connecting", "Connected", "Connect failed") and "ppp0" not in r[2]
                                  and "no password" not in r[2]][-3:], [("Connect failed", "timeout"), ("Connected", "default route via tun1"), ("Connecting", "starting")])
w.destroy()

# singular, empty, errors, no profile
def pm(*pairs):
    return ["%.6f host %s" % p for p in pairs]


UPL = "NetworkManager[1]: <info>  [1.0] policy: set 'My VPN' (ppp0) as default for IPv4 routing and DNS"
vt.run_async = AsyncStub(vpn_handler(pm((10, UPL), (100, "pppd[5]: No response to 4 echo-requests"), (100.2, "pppd[5]: Connection terminated."), (130, UPL))))
w = vt.VpnDropsWindow(WinTray())
eq("one drop: singular", w.status.get_text(), "1 drop (server silent: 1) · total time without VPN: 30s · longest: 30s")
w.destroy()
vt.run_async = AsyncStub(vpn_handler(pm((10, UPL))))
w = vt.VpnDropsWindow(WinTray())
eq("no drops", (w.status.get_text(), len(rows(w))), ("No VPN drops in this period", 1))
w.destroy()
vt.run_async = AsyncStub(vpn_handler([]))
w = vt.VpnDropsWindow(WinTray())
eq("empty journal", (w.status.get_text(), len(rows(w))), ("No VPN drops in this period", 0))
w.destroy()
vt.run_async = AsyncStub(lambda argv: (False, "", ""))
w = vt.VpnDropsWindow(WinTray())
eq("journalctl exit 1 without output is an empty result", w.status.get_text(), "No VPN drops in this period")
w.destroy()
for failing, label in ((lambda a: not is_kernel_query(a), "the main query fails"), (is_kernel_query, "the kernel query fails")):
    ok_handler = vpn_handler(NM_JOURNAL)
    stub = AsyncStub(lambda argv, failing=failing, ok_handler=ok_handler: (False, "Failed to seek: boom\n", "") if failing(argv) else ok_handler(argv))
    vt.run_async = stub
    with quiet():
        w = vt.VpnDropsWindow(WinTray())
    eq("%s: status" % label, w.status.get_text(), "Failed to read the journal: Failed to seek: boom")
    check("%s: empty table, usable controls" % label, len(rows(w)) == 0 and not w.loading and w.period_combo.get_sensitive() and w.summary is None)
    eq("%s: queries run" % label, len(stub.calls), 1 if label.startswith("the main") else 2)
    w.destroy()

stub = AsyncStub()
vt.run_async = stub
w = vt.VpnDropsWindow(WinTray("nm", None))
eq("no profile selected", (w.status.get_text(), len(rows(w)), stub.calls), ("No VPN profile selected", 0, []))
check("... controls stay usable", not w.loading and w.period_combo.get_sensitive())
w.btn_refresh.emit("clicked")
eq("... Refresh asks nothing", stub.calls, [])
w.destroy()

# ================= VPN drops: Hiddify backend ===================================================
HID_MAIN_Q = vt.VpnDropsWindow.MATCHES_HIDDIFY
eq("the Hiddify matches", HID_MAIN_Q, ["_SYSTEMD_UNIT=NetworkManager.service", "+", "UNIT=NetworkManager.service", "+", "_SYSTEMD_UNIT=HiddifyAdminBG.service"])
clock.now = 5200.0
stub = AsyncStub(vpn_handler(HID_JOURNAL))
vt.run_async = stub
w = vt.VpnDropsWindow(WinTray("hiddify", "Hiddify", active=False))
eq("Hiddify: the two queries", stub.calls, [BASE_VPN + ["-b"] + HID_MAIN_Q, BASE_VPN + ["-b"] + KERNEL_Q])
eq("Hiddify: status", w.status.get_text(),
   "4 drops (on command: 1, suspend: 1, other: 2) · total time without VPN: 3m 10s · longest: 1m 40s")
UPH = "tunnel tun0"
HEXPECT = [(1000, "Boot", "", ""), (1010.1, "Connected", UPH, ""), (2000.2, "Drop", "on command", "30s"), (2030, "Connected", UPH, ""),
           (3000, "Suspend", "", ""), (3001, "Drop", "suspend", "10s"), (3601, "Wake", "", ""), (3611, "Connected", UPH, ""),
           (4000, "Drop", "other", "50s"), (4005, "Connect failed", "Hiddify core failed to start", ""), (4050, "Connected", UPH, ""),
           (5100, "Drop", "other", "ongoing")]
eq("Hiddify: rows, newest first", rows(w, 4), [[vt.fmt_time(ts), k, d, dt] for ts, k, d, dt in reversed(HEXPECT)])
eq("Hiddify: bold rows are the drops", sorted((r[1], r[5]) for r in rows(w) if r[5] == 700), [("Drop", 700)] * 4)
stub.calls.clear()
for pid in ("1h", "6h", "24h", "session", "all", "boot"):
    w.period_combo.set_active_id(pid)
    eq("Hiddify period %s" % pid, stub.calls[-2:], [BASE_VPN + PERIOD_ARGS[pid] + HID_MAIN_Q, BASE_VPN + PERIOD_ARGS[pid] + KERNEL_Q])
w.destroy()
vt.run_async = AsyncStub(vpn_handler(HID_JOURNAL))
w = vt.VpnDropsWindow(WinTray("hiddify", "Hiddify", active=True))
eq("Hiddify, tunnel up now", (w.status.get_text(), [r[3] for r in rows(w, 4) if r[3] == "ongoing"]),
   ("4 drops (on command: 1, suspend: 1, other: 2) · total time without VPN: 1m 30s · longest: 50s", []))
w.destroy()
HNMU = "NetworkManager[1]: <info> device (tun0): state change: secondaries -> activated (reason 'x')"
HNMD = "NetworkManager[1]: <info> device (tun0): state change: activated -> unmanaged (reason 'x')"
vt.run_async = AsyncStub(vpn_handler(pm((10, HNMU), (100, HNMD), (160, HNMU))))
w = vt.VpnDropsWindow(WinTray("hiddify", "Hiddify"))
eq("Hiddify, one drop: singular", w.status.get_text(), "1 drop (on command: 0, suspend: 0, other: 1) · total time without VPN: 1m 00s · longest: 1m 00s")
w.destroy()
vt.run_async = AsyncStub(vpn_handler(pm((10, HNMU))))
w = vt.VpnDropsWindow(WinTray("hiddify", "Hiddify"))
eq("Hiddify, no drops", w.status.get_text(), "No VPN drops in this period")
w.destroy()
vt.run_async = AsyncStub(lambda argv: (False, "no access\n", ""))
with quiet():
    w = vt.VpnDropsWindow(WinTray("hiddify", "Hiddify"))
eq("Hiddify, journal error", w.status.get_text(), "Failed to read the journal: no access")
w.destroy()
# the interface from the settings
common.reset_settings(vt, text="[hiddify]\ninterface = wg0\n")
vt.run_async = AsyncStub(vpn_handler(HID_JOURNAL))
w = vt.VpnDropsWindow(WinTray("hiddify", "Hiddify"))
eq("Hiddify: the configured interface is the one analysed", w.status.get_text(), "No VPN drops in this period")
w.destroy()
common.reset_settings(vt)

# ================= About ========================================================================
vt._app_version = "1.1.0 (abc1234, 2026-11-19)"
dlg = vt.build_about_dialog()
eq("About: name", dlg.get_program_name(), "VPN kill switch")
eq("About: version", dlg.get_version(), "1.1.0 (abc1234, 2026-11-19)")
eq("About: summary", dlg.get_comments(), vt.ABOUT_SUMMARY)
eq("About: copyright", dlg.get_copyright(), "Copyright © 2026 Ivan Nikolajtsev")
eq("About: license type", dlg.get_license_type(), Gtk.License.MIT_X11)
eq("About: authors", dlg.get_authors(), ["Ivan Nikolajtsev"])
eq("About: website", (dlg.get_website(), dlg.get_website_label()), (vt.APP_WEBSITE, "Repository"))
check("About: website is an https link", vt.APP_WEBSITE.startswith("https://"))
eq("About: icon", (dlg.get_logo_icon_name(), dlg.get_icon_name()), (vt.ICON_ACTIVE, vt.ICON_ACTIVE))
check("About: the extra labels exist (the dialog layout was understood)", hasattr(dlg, "about_components") and hasattr(dlg, "about_paths"))
comp, paths = dlg.about_components, dlg.about_paths
eq("About: component list text", comp.get_text(), vt.ABOUT_COMPONENTS)
check("About: component list is left-aligned, one line per item", comp.get_xalign() == 0 and comp.get_justify() == Gtk.Justification.LEFT
      and not comp.get_line_wrap(), (comp.get_xalign(), comp.get_justify()))
check("About: the list names the four parts", all(w_ in comp.get_text() for w_ in ("Kill switch", "Watchdog", "Tray icon", "Statistics")))
eq("About: paths text", paths.get_text(), vt.about_paths())
check("About: paths are centered and wrap", paths.get_justify() == Gtk.Justification.CENTER and paths.get_line_wrap())
box = comp.get_parent()
kids = box.get_children()
i = kids.index(comp)
check("About: summary, then components, then paths, in one box",
      isinstance(kids[i - 1], Gtk.Label) and kids[i - 1].get_text() == vt.ABOUT_SUMMARY and kids[i + 1] is paths, [type(k).__name__ for k in kids])
check("About: the extra labels are visible", comp.get_visible() and paths.get_visible())
eq("About: paths for a checkout", paths.get_text().split("\n")[0].startswith("Project: "), True)
destroyed = []
dlg.connect("destroy", lambda *_: destroyed.append(1))
dlg.emit("response", Gtk.ResponseType.CLOSE)
eq("About: any response destroys the dialog", destroyed, [1])
vt._app_version = None

# ================= the Tray opens one window at a time ===========================================
with quiet():
    vt._app_version = "1.1.0"
t = make_tray(vt, "nm", "U1", FakeClient([vpn_conn("Alpha", "U1", gateway="vpn.example.org")], []))
t.update_conn()
t.on_about()
first = t.about_win
check("About: opened and visible", isinstance(first, Gtk.AboutDialog) and first.get_visible())
t.on_about()
check("About: the second click raises the same window", t.about_win is first)
first.emit("response", Gtk.ResponseType.CLOSE)
common.pump()
check("About: closing forgets it", t.about_win is None)

vt.run_async = AsyncStub(lambda argv: (True, "", text(DROPS)))
t.open_drops_window()
first = t.drops_win
check("Dropped packets: opened with this tray", isinstance(first, vt.DropsWindow) and first.tray is t)
t.open_drops_window()
check("Dropped packets: one window", t.drops_win is first)
first.destroy()
check("Dropped packets: closing forgets it", t.drops_win is None)

vt.run_async = AsyncStub(vpn_handler(NM_JOURNAL))
t.open_vpn_drops_window()
first = t.vpn_drops_win
check("VPN drops: opened for the selected profile", isinstance(first, vt.VpnDropsWindow) and first.name == "Alpha")
t.open_vpn_drops_window()
check("VPN drops: one window", t.vpn_drops_win is first)
first.destroy()
check("VPN drops: closing forgets it", t.vpn_drops_win is None)
t.backend = "hiddify"
t.client.devices = {}
vt.run_async = AsyncStub(vpn_handler(HID_JOURNAL))
t.open_vpn_drops_window()
check("VPN drops: for Hiddify", t.vpn_drops_win.name == "Hiddify" and t.vpn_drops_win.backend == "hiddify")
t.vpn_drops_win.destroy()
t.backend = "nm"

vt.hiddify_available = lambda: False
vt.run = common.RunStub(lambda argv: (3, "inactive\n"))
t.on_settings()
first = t.settings_win
check("Settings: opened", isinstance(first, vt.SettingsWindow) and first.tray is t)
t.on_settings()
check("Settings: one window", t.settings_win is first)
eq("Settings: the combo lists the tray's profiles", [r[1] for r in first.combo.get_model()], ["U1"])
t.ks_active = True
first.sync()
first.destroy()
check("Settings: closing forgets it", t.settings_win is None)

T.done()
