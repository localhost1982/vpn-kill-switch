# SPDX-License-Identifier: MIT
"""Tray behaviour with fake NetworkManager objects, for both backends: the menu labels, Reconnect VPN,
the drop notifications (stubbed libnotify and subprocess), the kill switch line and the VPN drops
line. The Tray is built without its constructor, so no D-Bus, panel or display is needed."""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import (AsyncStub, Clock, FakeActive, FakeClient, FakeConnection, FakeDevice, FakeNotify,  # noqa: E402
                    FakeShutil, FakeSubprocess, Results, RunStub, make_tray, quiet, vpn_conn)

vt = common.load_tray()
from gi.repository import NM  # noqa: E402

T = Results("test_tray_state")
check, eq = T.check, T.eq
AS = NM.ActiveConnectionState
DS = NM.DeviceState

clock = Clock()
vt.time = clock          # the tray's time.time() is ours; strftime / localtime stay real
BASE = clock.now


def hms(ts):
    import time
    return time.strftime("%H:%M:%S", time.localtime(ts))


def nm_tray(active_state=None, conn=None, uuid="U1", client_ok=True):
    """A tray with the NetworkManager backend, a profile "Alpha" (U1), optionally active."""
    conn = conn or vpn_conn("Alpha", "U1", gateway="vpn.example.org")
    active = [] if active_state is None else [FakeActive("U1", active_state)]
    client = FakeClient([conn], active) if client_ok else None
    t = make_tray(vt, "nm", uuid, client)
    t.update_conn()
    return t


def labels(t):
    return t.item_vpn.get_label(), t.last_icon, t.last_title


# ================= NetworkManager backend: the VPN line ====================================
common.reset_settings(vt)
t = nm_tray(uuid=None)
t.refresh_vpn()
eq("not selected", labels(t), ("VPN: not selected", vt.ICON_OFF, "VPN not selected"))
check("not selected: Reconnect insensitive, detector reset", t.item_reconnect.sens is False and t.vpn_prev is None)

t = make_tray(vt, "nm", "GONE", FakeClient([vpn_conn("Alpha", "U1")]))
t.update_conn()
t.refresh_vpn()
eq("profile not found", labels(t), ("VPN: profile not found", vt.ICON_OFF, "VPN profile not found"))
check("profile not found: Reconnect is still offered (the uuid is selected)", t.item_reconnect.sens is True)

conn = FakeConnection("Alpha", "U1", "vpn", timestamp=int(BASE) - 125)
t = nm_tray(AS.ACTIVATED, conn)
t.refresh_vpn()
eq("connected with the duration", labels(t), ("VPN: connected 2m 05s (Alpha)", vt.ICON_ACTIVE, "VPN connected"))
eq("connected: the detector knows", t.vpn_prev, True)
for ts, want in ((None, "VPN: connected (Alpha)"), (0, "VPN: connected (Alpha)"), (int(BASE) + 99, "VPN: connected 0s (Alpha)"),
                 (int(BASE) - 3700, "VPN: connected 1h 01m (Alpha)")):
    t = nm_tray(AS.ACTIVATED, FakeConnection("Alpha", "U1", "vpn", timestamp=ts))
    t.refresh_vpn()
    eq("connected, timestamp %r" % ts, t.item_vpn.get_label(), want)
t = nm_tray(AS.ACTIVATED, FakeConnection("Alpha", "U1", "vpn", timestamp=RuntimeError("no timestamp")))
with quiet() as err:
    t.refresh_vpn()
eq("connected, unreadable timestamp", t.item_vpn.get_label(), "VPN: connected (Alpha)")
check("... logged", "connection.timestamp" in err.getvalue())

t = nm_tray(AS.ACTIVATING)
t.refresh_vpn()
eq("connecting", labels(t), ("VPN: connecting… (Alpha)", vt.ICON_ACTIVATING, "VPN connecting"))
eq("connecting counts as not connected for the detector", t.vpn_prev, False)
for state in (None, AS.DEACTIVATED, AS.DEACTIVATING, AS.UNKNOWN):
    t = nm_tray(state)
    t.refresh_vpn()
    eq("disconnected (%s)" % state, labels(t), ("VPN: disconnected (Alpha)", vt.ICON_OFF, "VPN disconnected"))

t = nm_tray(client_ok=False)
t.refresh_vpn()
eq("NetworkManager unavailable", labels(t), ("VPN: state unknown", vt.ICON_OFF, "VPN: no data"))
check("... the detector is told 'unknown'", t.vpn_prev is None)
t = nm_tray(AS.ACTIVATED)
t.refresh_vpn()
t.client = None                      # the connection is still known, NetworkManager went away
t.refresh_vpn()
eq("NetworkManager gone later keeps the name", labels(t), ("VPN: state unknown (Alpha)", vt.ICON_OFF, "VPN: no data"))
t = nm_tray(AS.UNKNOWN)
t.client.active[0].state = RuntimeError("dbus went away")
with quiet() as err:
    t.refresh_vpn()
eq("state cannot be read", labels(t), ("VPN: state unknown (Alpha)", vt.ICON_OFF, "VPN: no data"))
check("... logged", "VPN state" in err.getvalue())
with quiet():
    eq("vpn_activated on error", t.vpn_activated(), False)

# state subscription: a change of the active connection refreshes the line, a replaced one is unsubscribed
t = nm_tray(AS.ACTIVATED)
t.refresh_vpn()
act = t.client.active[0]
eq("subscribed to the active connection", act.handler_count(), 1)
t.refresh_vpn()
eq("not subscribed twice", act.handler_count(), 1)
act.emit_state(AS.DEACTIVATED)
eq("a state change refreshes the line", t.item_vpn.get_label(), "VPN: disconnected (Alpha)")
t.client.active = []
t.refresh_vpn()
eq("the active connection is gone: unsubscribed", (act.handler_count(), t.ac), (0, None))
new_act = FakeActive("U1", AS.ACTIVATED)
t.client.active = [new_act]
t.refresh_vpn()
eq("a new active connection is subscribed", (new_act.handler_count(), t.item_vpn.get_label()), (1, "VPN: connected (Alpha)"))
other = FakeActive("U1", AS.ACTIVATING)
t.client.active = [other]
t.refresh_vpn()
eq("replaced: the old one is unsubscribed", (new_act.handler_count(), other.handler_count()), (0, 1))
# indicator calls only happen on a change
t = nm_tray(AS.ACTIVATED)
for _ in range(3):
    t.refresh_vpn()
eq("icon and title are set once while nothing changes", (len(t.indicator.icons), len(t.indicator.titles)), (1, 1))
t.client.active[0].emit_state(AS.DEACTIVATED)
eq("... and again on a change", (len(t.indicator.icons), len(t.indicator.titles)), (2, 2))
t = nm_tray(AS.ACTIVATED)
t.refresh_vpn()
eq("vpn_name / vpn_activated", (t.vpn_name(), t.vpn_activated()), ("Alpha", True))
t.client.active[0].state = AS.ACTIVATING
eq("activating is not activated", t.vpn_activated(), False)
t.client.active = []
eq("no active connection", t.vpn_activated(), False)

# set_vpn / set_backend start the detector over
t = nm_tray(AS.ACTIVATED)
t.refresh_vpn()
t.refresh_conn = lambda: None
t.vpn_prev, t.drop_popup = True, True
t.set_vpn("U2")
eq("set_vpn", (t.vpn_uuid, t.backend, t.vpn_prev, t.drop_popup), ("U2", "nm", None, False))
t.backend, t.vpn_prev, t.drop_popup = "nm", True, True
t.set_backend("hiddify")
eq("set_backend hiddify", (t.backend, t.vpn_prev, t.drop_popup), ("hiddify", None, False))
t.set_backend("anything else")
eq("set_backend normalises", t.backend, "nm")

# update_conn
t = nm_tray(AS.ACTIVATED)
check("update_conn resolves the profile", t.vpn_conn is t.client.connections[0] and t.item_reconnect.sens is True)
t.vpn_uuid = "ZZ"
t.update_conn()
check("update_conn: unknown uuid", t.vpn_conn is None and t.item_reconnect.sens is True)
t.client.get_connection_by_uuid = lambda u: (_ for _ in ()).throw(RuntimeError("dbus"))
with quiet() as err:
    t.update_conn()
check("update_conn survives an error", t.vpn_conn is None and "connection by uuid" in err.getvalue())

# ================= Reconnect VPN ==========================================================
events = []
vt.run_async = AsyncStub(log=events)
vt.subprocess = FakeSubprocess(log=events)
UP = ["nmcli", "connection", "up", "uuid", "U1"]
DOWN = ["nmcli", "connection", "down", "uuid", "U1"]

t = nm_tray(AS.ACTIVATED)
t.on_reconnect()
eq("active profile: down, then up", events, [("async", DOWN), ("popen", UP)])
eq("... the down is expected (no drop popup)", t.expect_down_until, clock.now + vt.EXPECT_DOWN_SECONDS)
eq("... the process is kept to be reaped", len(t.procs), 1)
events.clear()
t = nm_tray(AS.ACTIVATING)
t.on_reconnect()
eq("a connecting profile is brought down first too", events, [("async", DOWN), ("popen", UP)])
events.clear()
t = nm_tray(None)
t.on_reconnect()
eq("inactive profile: only up", (events, t.expect_down_until), ([("popen", UP)], 0.0))
events.clear()
t = nm_tray(uuid=None)
check("no profile: the item is insensitive", t.item_reconnect.sens is False)
t.on_reconnect()
eq("no profile: nothing happens", events, [])
t = make_tray(vt, "hiddify", "U1", FakeClient([vpn_conn("Alpha", "U1")]))
t.update_conn()
check("Hiddify: the item is insensitive, even with a stored profile", t.item_reconnect.sens is False and t.vpn_conn is None)
t.on_reconnect()
eq("Hiddify: Reconnect does nothing", events, [])
t = nm_tray(AS.ACTIVATED)
t.client = None
t.on_reconnect()
eq("NetworkManager unavailable: up without down", events, [("popen", UP)])
events.clear()
vt.subprocess.popen_error = FileNotFoundError("nmcli")
t = nm_tray(None)
with quiet() as err:
    t.start_vpn("U1")
check("start_vpn survives a missing nmcli", t.procs == [] and "nmcli" in err.getvalue())
vt.subprocess.popen_error = None

# ================= Hiddify backend: the VPN line ================================================
common.reset_settings(vt)
vt.time = clock


def h_tray(state=DS.ACTIVATED, iface="tun0", client_ok=True):
    dev = None if state is None else FakeDevice(state)
    client = FakeClient([], [], {iface: dev} if dev else {}) if client_ok else None
    t = make_tray(vt, "hiddify", "U1", client)
    t.hiddify_since = lambda: clock.now - 723
    t.update_conn()
    return t


t = h_tray()
t.refresh_vpn()
eq("Hiddify connected with the duration", labels(t), ("VPN: connected 12m 03s (Hiddify)", vt.ICON_ACTIVE, "VPN connected"))
eq("Hiddify: vpn_name / activated", (t.vpn_name(), t.vpn_activated(), t.vpn_prev), ("Hiddify", True, True))
check("Hiddify: Reconnect insensitive", t.item_reconnect.sens is False)
check("Hiddify: no NetworkManager profile is watched", t.ac is None and t.hdev is t.client.devices["tun0"])
t.hiddify_since = lambda: None
t.refresh_vpn()
eq("Hiddify: no duration without the interface time", t.item_vpn.get_label(), "VPN: connected (Hiddify)")
for state in (DS.DISCONNECTED, DS.UNMANAGED, DS.UNAVAILABLE, DS.PREPARE, None):
    t = h_tray(state)
    t.refresh_vpn()
    eq("Hiddify disconnected (%s)" % state, labels(t), ("VPN: disconnected (Hiddify)", vt.ICON_OFF, "VPN disconnected"))
t = h_tray(client_ok=False)
t.refresh_vpn()
eq("Hiddify, NetworkManager unavailable", labels(t), ("VPN: state unknown (Hiddify)", vt.ICON_OFF, "VPN: no data"))
check("... detector 'unknown'", t.vpn_prev is None)
t = h_tray()
t.client.get_device_by_iface = lambda i: (_ for _ in ()).throw(RuntimeError("dbus"))
with quiet() as err:
    t.refresh_vpn()
eq("Hiddify, device lookup fails", labels(t), ("VPN: state unknown (Hiddify)", vt.ICON_OFF, "VPN: no data"))
common.reset_settings(vt, text="[hiddify]\ninterface = wg0\n")
t = h_tray(iface="wg0")
t.refresh_vpn()
eq("the interface comes from the settings", t.item_vpn.get_label(), "VPN: connected 12m 03s (Hiddify)")
common.reset_settings(vt)

# the device subscription
t = h_tray()
t.refresh_vpn()
dev = t.client.devices["tun0"]
eq("subscribed to the device", dev.handler_count(), 1)
t.refresh_vpn()
eq("... once", dev.handler_count(), 1)
dev.emit_state(DS.UNMANAGED)
eq("a device state change refreshes the line", t.item_vpn.get_label(), "VPN: disconnected (Hiddify)")
t.client.devices = {}
t.refresh_vpn()
eq("device removed: disconnected and unsubscribed", (t.item_vpn.get_label(), dev.handler_count(), t.hdev), ("VPN: disconnected (Hiddify)", 0, None))
new_dev = FakeDevice(DS.ACTIVATED)
t.client.devices = {"tun0": new_dev}
t.refresh_vpn()
eq("device created: connected and subscribed", (t.item_vpn.get_label()[:14], new_dev.handler_count()), ("VPN: connected", 1))
t.backend = "nm"
t.vpn_uuid = None
t.refresh_vpn()
eq("switching to NetworkManager unsubscribes the device", (new_dev.handler_count(), t.hdev), (0, None))
t = h_tray()
n = []
t.refresh_vpn = lambda: n.append(1)
t.refresh_hiddify_event()
t.backend = "nm"
t.refresh_hiddify_event()
eq("device-added / removed refresh only the Hiddify backend", n, [1])

# the tunnel's creation time
common.reset_settings(vt, text="[hiddify]\ninterface = lo\n")
t = make_tray(vt, "hiddify", "U1", None)
ts = vt.Tray.hiddify_since(t)
check("hiddify_since reads the interface's ctime", isinstance(ts, float) and ts > 1e9, ts)
common.reset_settings(vt, text="[hiddify]\ninterface = no-such-if0\n")
with quiet() as err:
    eq("hiddify_since without the interface", vt.Tray.hiddify_since(t), None)
check("... logged", "tunnel creation time" in err.getvalue())
common.reset_settings(vt)

# ================= drop notifications ========================================================
sound_file = os.path.join(common.TMP, "drop.oga")
with open(sound_file, "wb") as f:
    f.write(b"x")
vt.DROP_SOUND_FILE = sound_file
CANBERRA = ["canberra-gtk-play", "-i", "network-connectivity-lost", "-d", "VPN dropped"]
TAIL_ON = "Traffic outside the VPN is blocked by the kill switch."
TAIL_OFF = "The kill switch is OFF: traffic is NOT protected."


def ntray(popup=True, sound=True, ks=True, programs=("canberra-gtk-play", "pw-play"), backend="nm"):
    global notify
    clock.now = BASE
    notify = FakeNotify()
    vt.Notify = notify
    vt.subprocess = FakeSubprocess()
    vt.shutil = FakeShutil(programs)
    t = make_tray(vt, backend, "U1", None)
    t.notify_popup, t.notify_sound, t.ks_active, t.notify_ok = popup, sound, ks, True
    return t


def shown(texts=False):
    return [(s, b) if texts else (s, b, i) for s, b, i, _o in notify.shown]


# drop -> one popup and one sound, reconnect -> the same popup, no sound
t = ntray()
t.track_vpn(True, "Alpha")
eq("first observation: nothing", (notify.shown, vt.subprocess.popen_calls), ([], []))
clock.advance(5)
t.track_vpn(False, "Alpha")
eq("drop: one popup", shown(), [("VPN dropped", "Alpha disconnected at %s. %s" % (hms(clock.now), TAIL_ON), vt.ICON_OFF)])
eq("drop: one sound", vt.subprocess.popen_calls, [CANBERRA])
check("drop: bookkeeping", t.drop_popup is True and t.drop_at == clock.now and t.last_sound_at == clock.now and len(t.procs) == 1)
eq("the popup is a normal-urgency libnotify notification", (len(notify.objects), notify.objects[0].urgency), (1, FakeNotify.Urgency.NORMAL))
first_drop = clock.now
clock.advance(65)
t.track_vpn(True, "Alpha")
eq("reconnect: the same notification, updated", (len(notify.objects), notify.objects[0].updates, len(notify.shown),
                                                   shown()[1], notify.shown[1][3] is notify.shown[0][3]),
   (1, 1, 2, ("VPN reconnected", "Alpha is back after 1m 05s.", vt.ICON_ACTIVE), True))
eq("reconnect: no sound", len(vt.subprocess.popen_calls), 1)
check("reconnect: the drop popup is consumed", t.drop_popup is False)
t.track_vpn(True, "Alpha")
eq("a repeated 'connected' is nothing", len(notify.shown), 2)

# the sound is rate-limited to one per 30 s, the popup is not
t = ntray()
t.track_vpn(True, "Alpha")
clock.advance(5)
t.track_vpn(False, "Alpha")
sound_at = clock.now
clock.advance(5)
t.track_vpn(True, "Alpha")
clock.now = sound_at + 29.9
t.track_vpn(False, "Alpha")
eq("second drop within 30 s: popup yes", (len(notify.shown), notify.shown[-1][0]), (3, "VPN dropped"))
eq("second drop within 30 s: no sound", len(vt.subprocess.popen_calls), 1)
clock.advance(0.05)
t.track_vpn(True, "Alpha")
clock.now = sound_at + 30.0
t.track_vpn(False, "Alpha")
eq("a drop 30 s after the last sound: sound again", (len(vt.subprocess.popen_calls), t.last_sound_at), (2, sound_at + 30.0))
eq("... and still a single Notification object", len(notify.objects), 1)

# the tray's own reconnect / profile change
t = ntray()
t.track_vpn(True, "Alpha")
t.expect_down_until = clock.now + vt.EXPECT_DOWN_SECONDS
t.track_vpn(False, "Alpha")
eq("tray's own reconnect: no popup, no sound", (notify.shown, vt.subprocess.popen_calls, t.drop_popup), ([], [], False))
t.track_vpn(True, "Alpha")
eq("... and no 'reconnected' popup either", notify.shown, [])
t.refresh_conn = lambda: None
t.track_vpn(True, "Alpha")
t.set_vpn("U2")
t.track_vpn(False, "Alpha2")
eq("profile change: the detector starts over", (notify.shown, vt.subprocess.popen_calls), ([], []))
clock.advance(vt.EXPECT_DOWN_SECONDS + 1)
t.track_vpn(True, "Alpha2")
clock.advance(1)
t.track_vpn(False, "Alpha2")
eq("... after the expected-down window a drop is a drop", len(notify.shown), 1)

# sleep, and the quiet period after resume
class Params:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        if isinstance(self.value, Exception):
            raise self.value
        return (self.value,)


t = ntray()
t.track_vpn(True, "Alpha")
t.on_prepare_for_sleep(None, None, None, None, None, Params(True))
eq("PrepareForSleep(true)", t.sleeping, True)
t.track_vpn(False, "Alpha")
eq("a drop while suspending is nothing", (notify.shown, vt.subprocess.popen_calls), ([], []))
clock.advance(600)
t.on_prepare_for_sleep(None, None, None, None, None, Params(False))
eq("PrepareForSleep(false)", (t.sleeping, t.quiet_until), (False, clock.now + vt.RESUME_QUIET_SECONDS))
clock.advance(8)
t.track_vpn(True, "Alpha")
eq("reconnect after resume: no popup (no drop was shown)", notify.shown, [])
clock.advance(10)
t.track_vpn(False, "Alpha")
eq("a drop within 20 s after resume: nothing", (notify.shown, vt.subprocess.popen_calls), ([], []))
clock.advance(1)
t.track_vpn(True, "Alpha")
clock.advance(1)
t.track_vpn(False, "Alpha")
eq("a drop after the quiet period: popup and sound", (len(notify.shown), len(vt.subprocess.popen_calls)), (1, 1))
with quiet() as err:
    t.on_prepare_for_sleep(None, None, None, None, None, Params(RuntimeError("bad variant")))
check("a broken PrepareForSleep signal is logged and ignored", t.sleeping is False and "PrepareForSleep" in err.getvalue())

# popup off / sound off / both off
t = ntray(popup=False, sound=True)
t.track_vpn(True, "Alpha")
t.track_vpn(False, "Alpha")
eq("popup off: sound only", (notify.shown, vt.subprocess.popen_calls), ([], [CANBERRA]))
check("popup off: no popup recorded", t.drop_popup is False)
clock.advance(1)
t.track_vpn(True, "Alpha")
eq("popup off: no 'reconnected' popup", notify.shown, [])
t = ntray(popup=True, sound=False)
t.track_vpn(True, "Alpha")
t.track_vpn(False, "Alpha")
clock.advance(7)
t.track_vpn(True, "Alpha")
eq("sound off: popups only", (len(notify.shown), vt.subprocess.popen_calls, shown()[1][0]), (2, [], "VPN reconnected"))
t = ntray(popup=False, sound=False)
t.track_vpn(True, "Alpha")
t.track_vpn(False, "Alpha")
t.track_vpn(True, "Alpha")
eq("both off: nothing at all", (notify.shown, vt.subprocess.popen_calls, notify.objects), ([], [], []))

# the kill switch off variant
for ks in (False, None):
    t = ntray(ks=ks)
    t.track_vpn(True, "Alpha")
    t.track_vpn(False, "Alpha")
    eq("kill switch %r: the warning body" % ks, shown()[0][1], "Alpha disconnected at %s. %s" % (hms(clock.now), TAIL_OFF))
t = ntray()
t.track_vpn(True, None)
t.track_vpn(False, None)
clock.advance(3)
t.track_vpn(True, None)
eq("no profile name: 'VPN'", (shown()[0][1].startswith("VPN disconnected at"), shown()[1][1]), (True, "VPN is back after 3s."))

# the sound program
t = ntray(programs=("pw-play",))
t.track_vpn(True, "A")
t.track_vpn(False, "A")
eq("pw-play fallback with the theme file", vt.subprocess.popen_calls, [["pw-play", sound_file]])
vt.DROP_SOUND_FILE = os.path.join(common.TMP, "missing.oga")
t = ntray(programs=("pw-play",))
with quiet() as err:
    t.track_vpn(True, "A")
    t.track_vpn(False, "A")
    clock.advance(1)
    t.track_vpn(True, "A")
    clock.advance(31)
    t.track_vpn(False, "A")
check("pw-play without the file: no sound, warned once", vt.subprocess.popen_calls == [] and t.sound_warned
      and err.getvalue().count("no canberra-gtk-play or pw-play") == 1 and t.last_sound_at == 0.0, err.getvalue())
eq("... the popups still work", len(notify.shown), 3)
vt.DROP_SOUND_FILE = sound_file
t = ntray(programs=())
with quiet() as err:
    t.track_vpn(True, "A")
    t.track_vpn(False, "A")
check("no sound program at all: no sound, a warning", vt.subprocess.popen_calls == [] and "no sound" in err.getvalue())
t = ntray(programs=("canberra-gtk-play",))
vt.subprocess.popen_error = OSError("exec failed")
with quiet() as err:
    t.track_vpn(True, "A")
    t.track_vpn(False, "A")
check("the sound program fails to start: logged, the limiter is not armed", t.last_sound_at == 0.0 and "canberra-gtk-play" in err.getvalue())
check("... but the popup was shown", len(notify.shown) == 1)
vt.subprocess.popen_error = None
clock.advance(1)
t.track_vpn(True, "A")
t.track_vpn(False, "A")
eq("... and the next drop tries again", vt.subprocess.popen_calls, [CANBERRA])

# libnotify missing / failing
t = ntray()
t.notify_ok = False
t.track_vpn(True, "A")
t.track_vpn(False, "A")
t.track_vpn(True, "A")
eq("no libnotify: no popup (and no 'reconnected'), the sound still plays", (notify.objects, len(vt.subprocess.popen_calls), t.drop_popup),
   ([], 1, False))
t = ntray()
notify.fail_new = True
with quiet() as err:
    t.track_vpn(True, "A")
    t.track_vpn(False, "A")
check("a failing notification daemon is logged and survived", t.drop_popup is False and "notification" in err.getvalue()
      and len(vt.subprocess.popen_calls) == 1)


def boom(*a, **k):
    raise RuntimeError("boom")


t = ntray()
t.show_notification = boom
t.track_vpn(True, "A")
with quiet() as err:
    t.track_vpn(False, "A")
check("track_vpn never raises", "drop notification" in err.getvalue() and t.vpn_prev is False)

# end to end through the fakes: NetworkManager backend
t = ntray()
t.client = FakeClient([vpn_conn("Alpha", "U1")], [FakeActive("U1", AS.ACTIVATED)])
t.update_conn()
t.refresh_vpn()
act = t.client.active[0]
act.emit_state(AS.DEACTIVATED)
clock.advance(40)
act.emit_state(AS.ACTIVATED)
eq("NM end to end: drop and reconnect popups", [s for s, _b, _i in shown()], ["VPN dropped", "VPN reconnected"])
eq("NM end to end: the labels followed", t.item_vpn.get_label(), "VPN: connected (Alpha)")
t.client.active = []
t.refresh_vpn()
eq("NM end to end: the active connection disappearing is a drop", [s for s, _b, _i in shown()][-1], "VPN dropped")

# end to end: Hiddify backend
t = ntray(backend="hiddify")
t.client = FakeClient([], [], {"tun0": FakeDevice(DS.ACTIVATED)})
t.hiddify_since = lambda: None
t.refresh_vpn()
dev = t.client.devices["tun0"]
dev.emit_state(DS.UNMANAGED)
clock.advance(30)
dev.emit_state(DS.ACTIVATED)
eq("Hiddify end to end", [(s, " ".join(b.split()[:2])) for s, b, _i in shown()],
   [("VPN dropped", "Hiddify disconnected"), ("VPN reconnected", "Hiddify is")])
eq("Hiddify: one sound", vt.subprocess.popen_calls, [CANBERRA])
t.expect_down_until = clock.now + 10
dev.emit_state(DS.UNMANAGED)
eq("Hiddify: an expected stop is no drop", len(notify.shown), 2)
t.client.devices = {}
t.refresh_vpn()
eq("Hiddify: a removed device (already down) is no new drop", len(notify.shown), 2)

# ================= kill switch line ============================================================
def ks_line(systemctl, journal=(0, ""), settings_win=None):
    t = make_tray(vt, "nm", "U1", None)
    t.settings_win = settings_win
    vt.run = RunStub(lambda argv: systemctl if argv[0] == "systemctl" else journal)
    t.refresh_killswitch()
    return t.item_ks.get_label(), t.ks_active, [c[0] for c in vt.run.calls]


DROP = "1795114531.1 host kernel: KS-DROP: IN= OUT=wlan0 SRC=1.1.1.1 DST=2.2.2.2 PROTO=TCP SPT=1 DPT=2 UID=0\n"
eq("status unknown", ks_line((None, "")), ("Kill switch: status unknown", None, ["systemctl"]))
eq("inactive", ks_line((3, "inactive\n")), ("Kill switch: OFF", False, ["systemctl"]))
eq("failed unit", ks_line((3, "failed\n")), ("Kill switch: OFF", False, ["systemctl"]))
eq("empty answer", ks_line((4, "")), ("Kill switch: OFF", False, ["systemctl"]))
eq("active, no drops", ks_line((0, "active\n"), (1, "")), ("Kill switch: active, 0 packets dropped", True, ["systemctl", "journalctl"]))
eq("active, one drop", ks_line((0, "active\n"), (0, DROP)), ("Kill switch: active, 1 packet dropped", True, ["systemctl", "journalctl"]))
eq("active, three drops", ks_line((0, "active\n"), (0, DROP * 3))[0], "Kill switch: active, 3 packets dropped")
eq("active, unparsable lines are not counted", ks_line((0, "active\n"), (0, DROP + "KS-DROP junk\n"))[0], "Kill switch: active, 1 packet dropped")
eq("active, journal fails", ks_line((0, "active\n"), (2, ""))[0], "Kill switch: active, journal unavailable")
eq("active, journalctl not run", ks_line((0, "active\n"), (None, ""))[0], "Kill switch: active, journal unavailable")
eq("active, journal exit 1 with output", ks_line((0, "active\n"), (1, DROP))[0], "Kill switch: active, journal unavailable")


class SettingsSpy:
    calls = 0

    def sync(self):
        SettingsSpy.calls += 1


ks_line((3, "inactive\n"), settings_win=SettingsSpy())
eq("an open Settings window is synced", SettingsSpy.calls, 1)
eq("the unit is asked by name", vt.run.calls[0], ["systemctl", "is-active", "vpn-killswitch.service"])
ks_line((0, "active\n"), settings_win=None)
check("the drop count query", vt.run.calls[1][:6] == ["journalctl", "-q", "-o", "short-unix", "--no-pager", "--grep"]
      and vt.run.calls[1][-1] == "_TRANSPORT=kernel", vt.run.calls[1])
t = make_tray(vt, "nm", "U1", None)
vt.run = RunStub(lambda argv: (3, "inactive\n"))
t.refresh_killswitch()
t.refresh_killswitch()
eq("the label is only set when it changes", t.item_ks.label, "Kill switch: OFF")

# ================= VPN drops line ================================================================
PPPD = ["journalctl", "-b", "-q", "-o", "cat", "_COMM=pppd"]


def nm_drops(result):
    t = make_tray(vt, "nm", "U1", None)
    vt.run = RunStub(lambda argv: result)
    t.refresh_drops()
    return t.item_drops.get_label(), vt.run.calls


eq("NM drops: LCP + signal + connection terminated", nm_drops((0, "No response to 4 echo-requests\nTerminating on signal 15\n"
                                                                     "Connection terminated.\nConnection terminated.\n")),
   ("VPN drops since boot: 2 (server silent: 1)", [PPPD]))
eq("NM drops: only 'Connection terminated'", nm_drops((0, "Connection terminated.\n" * 3))[0], "VPN drops since boot: 3 (server silent: 0)")
eq("NM drops: more signals than 'Connection terminated'", nm_drops((0, "No response to 4 echo-requests\n" * 2 + "Terminating on signal 15\n"))[0],
   "VPN drops since boot: 3 (server silent: 2)")
eq("NM drops: nothing", nm_drops((0, ""))[0], "VPN drops since boot: 0 (server silent: 0)")
eq("NM drops: indented lines are not pppd lines", nm_drops((0, "  Connection terminated.\n"))[0], "VPN drops since boot: 0 (server silent: 0)")
eq("NM drops: journalctl fails", nm_drops((1, ""))[0], "VPN drops since boot: no data")
eq("NM drops: journalctl missing", nm_drops((None, ""))[0], "VPN drops since boot: no data")

FIX = common.journal_lines("hiddify.journal")
KERNEL = [l for l in FIX if "kernel:" in l]
MAIN = [l for l in FIX if "kernel:" not in l]
BASE_ARGV = ["journalctl", "-b", "-q", "-o", "short-unix", "--no-pager"]


def h_drops(main=(0, None), kernel=(0, None), state=DS.UNMANAGED, client_ok=True):
    t = make_tray(vt, "hiddify", "U1", FakeClient([], [], {"tun0": FakeDevice(state)}) if client_ok else None)

    def handler(argv):
        if "_TRANSPORT=kernel" in argv:
            return (kernel[0], "\n".join(KERNEL) + "\n" if kernel[1] is None else kernel[1])
        return (main[0], "\n".join(MAIN) + "\n" if main[1] is None else main[1])

    vt.run = RunStub(handler)
    t.refresh_drops()
    return t.item_drops.get_label(), vt.run.calls


label, calls = h_drops()
eq("Hiddify drops: the contract fixture", label, "VPN drops since boot: 4")
eq("Hiddify drops: the queries", calls, [BASE_ARGV + vt.VpnDropsWindow.MATCHES_HIDDIFY, BASE_ARGV + vt.VpnDropsWindow.MATCHES_KERNEL])
check("Hiddify drops: the first query reads the tunnel, the service and NetworkManager lines",
      calls[0][-5:] == ["_SYSTEMD_UNIT=NetworkManager.service", "+", "UNIT=NetworkManager.service", "+", "_SYSTEMD_UNIT=HiddifyAdminBG.service"], calls[0])
eq("Hiddify drops: tunnel up now", h_drops(state=DS.ACTIVATED)[0], "VPN drops since boot: 4")
eq("Hiddify drops: NetworkManager unavailable", h_drops(client_ok=False)[0], "VPN drops since boot: 4")
eq("Hiddify drops: the kernel query exits 1 when nothing matches", h_drops(kernel=(1, ""))[0], "VPN drops since boot: 4")
eq("Hiddify drops: the main query fails", h_drops(main=(1, ""))[0], "VPN drops since boot: no data")
eq("Hiddify drops: the main query is not run", h_drops(main=(None, ""))[0], "VPN drops since boot: no data")
eq("Hiddify drops: the kernel query fails", h_drops(kernel=(2, ""))[0], "VPN drops since boot: no data")
eq("Hiddify drops: no journal lines", h_drops(main=(0, ""), kernel=(1, ""))[0], "VPN drops since boot: 0")
t = make_tray(vt, "hiddify", "U1", FakeClient())
t.hiddify_connected = lambda: (_ for _ in ()).throw(RuntimeError("dbus"))
vt.run = RunStub(lambda argv: (0, "\n".join(KERNEL if "_TRANSPORT=kernel" in argv else MAIN) + "\n"))
with quiet() as err:
    t.refresh_drops()
check("Hiddify drops: an unreadable device state counts as not connected", t.item_drops.get_label() == "VPN drops since boot: 4"
      and "VPN state" in err.getvalue())

# ================= timers and the menu =========================================================
t = make_tray(vt, "nm", "U1", None)
order = []
for name in ("refresh_vpn", "refresh_uptime", "refresh_killswitch", "refresh_drops"):
    setattr(t, name, lambda name=name: order.append(name))


class Proc:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


running = Proc(None)
t.procs = [Proc(0), running, Proc(1)]
eq("tick_fast", (t.tick_fast(), order, t.procs), (True, ["refresh_vpn", "refresh_uptime"], [running]))
order.clear()
eq("tick_killswitch", (t.tick_killswitch(), order), (True, ["refresh_killswitch"]))
order.clear()
eq("tick_journal", (t.tick_journal(), order), (True, ["refresh_drops"]))
order.clear()
t.on_menu_show()
eq("showing the menu refreshes the state", order, ["refresh_vpn", "refresh_uptime", "refresh_killswitch"])
t = make_tray(vt, "nm", "U1", None)
t.refresh_uptime()
check("uptime line", t.item_uptime.get_label().startswith("Uptime: ") and "no data" not in t.item_uptime.get_label(), t.item_uptime.get_label())

T.done()
