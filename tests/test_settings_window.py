# SPDX-License-Identifier: MIT
"""SettingsWindow: the combo, and the order of the Save steps (helper -> settings file -> nmcli ->
systemctl -> local settings) for the NetworkManager backend and the Hiddify backend. The commands
are recorded by a stub, nothing real is started. Needs a display."""

import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import AsyncStub, FakeClient, Results, vpn_conn  # noqa: E402

vt = common.load_tray()
common.require_display()
from gi.repository import Gdk  # noqa: E402

T = Results("test_settings_window")
check, eq = T.check, T.eq

HELPER = "/usr/sbin/vpn-killswitch-server"
KS_UNIT = "vpn-killswitch.service"
SUDO_HELPER = ["sudo", "-n", HELPER]
MODE_PRESENT = os.path.join(common.TMP, "mode")
MODE_ABSENT = os.path.join(common.TMP, "no-such-dir", "mode")
with open(MODE_PRESENT, "w") as f:
    f.write("servers\n")

PASSWORD_HINT = "No passwordless permission to change the VPN profile."
cmds = []        # every command in the order it was started (nmcli up too: StubTray.start_vpn)
plan = {}        # scripted results
seen = {}        # the settings file as it was when a command started


class StubTray:
    """The parts of Tray the Settings window touches."""

    def __init__(self, backend, uuid, active, client):
        self.backend, self.vpn_uuid = backend, uuid
        self.notify_popup = self.notify_sound = True
        self.ks_active = False
        self.client = client
        self.expect_down_until = 0.0
        self.active = set(active)
        self.calls = []
        self.refreshes = 0

    def vpn_connections(self):
        return [(c.id, c.uuid) for c in self.client.connections]

    def refresh_killswitch(self):
        self.refreshes += 1

    def find_active_uuid(self, uuid):
        return object() if uuid in self.active else None

    def set_vpn(self, uuid):
        self.calls.append(("set_vpn", uuid))
        self.vpn_uuid, self.backend = uuid, "nm"

    def set_backend(self, backend):
        self.calls.append(("set_backend", backend))
        self.backend = backend

    def start_vpn(self, uuid):
        self.calls.append(("start_vpn", uuid))
        cmds.append(["nmcli", "connection", "up", "uuid", uuid])


def state():
    return (vt.load_settings(), vt.load_backend())


def handler(argv):
    cmds.append(list(argv))
    if argv[:3] == SUDO_HELPER:
        seen["helper"] = state()
        return plan.get("helper", (True, "", ""))
    if argv[:3] == ["nmcli", "connection", "down"]:
        seen["down"] = state()
        return (True, "", "")
    if argv[:3] == ["sudo", "-n", "systemctl"]:
        want = argv[3] == "enable"
        result = plan.get("ks", (True, "", ""))
        if result[0] and plan.get("ks_effective", True):
            win.tray.ks_active = want
        return result
    raise AssertionError("unexpected command: %r" % (argv,))


vt.run_async = AsyncStub(handler)
available = [False]
vt.hiddify_available = lambda: available[0]
win = None


def conns():
    return [vpn_conn("Alpha", "OLD", gateway="old.example.org"),
            vpn_conn("Beta", "NEW", gateway="new.example.org"),
            vpn_conn("Gamma", "MULTI", remote="a.example.org:1194:udp, b.example.org:443:tcp"),
            vpn_conn("Nohost", "NOHOST")]


def make(backend="nm", uuid="OLD", active=("OLD",), hiddify=False, ks=False, mode=True, profiles=None):
    """A Settings window on a stub tray. The settings file is whatever the test prepared."""
    global win
    available[0] = hiddify
    vt.MODE_FILE = MODE_PRESENT if mode else MODE_ABSENT
    cmds.clear()
    plan.clear()
    seen.clear()
    client = FakeClient(connections=conns() if profiles is None else profiles)
    tray = StubTray(backend, uuid, active, client)
    tray.ks_active = ks
    win = vt.SettingsWindow(tray)
    return win


def close(w):
    if not w.closed:
        w.destroy()
    common.pump()


def prep(uuid="OLD", backend="nm", watchdog=True, notify=(True, True)):
    common.reset_settings(vt, uuid=uuid, backend=backend, notify=notify, watchdog=watchdog)


def snap():
    return common.settings_snapshot(vt)


def ks_argv(action):
    return ["sudo", "-n", "systemctl", action, "--now", KS_UNIT]


# ================= the combo ===============================================
prep()
w = make()
model = w.combo.get_model()
eq("combo texts", [r[0] for r in model], ["Alpha", "Beta", "Gamma", "Nohost"])
eq("combo ids", [r[1] for r in model], ["OLD", "NEW", "MULTI", "NOHOST"])
eq("selected profile is preselected", w.combo.get_active_id(), "OLD")
check("combo is sensitive", w.combo.get_sensitive())
eq("kill switch switch follows the unit", w.switch.get_active(), False)
eq("watchdog switch follows the file", w.switch_wd.get_active(), True)
eq("notify switches follow the tray", (w.switch_popup.get_active(), w.switch_sound.get_active()), (True, True))
check("watchdog row is usable with a NetworkManager profile",
      w.switch_wd.get_sensitive() and w.wd_row.subtitle_label.get_text() == "Reconnect the selected VPN profile automatically when it drops.")
check("Save is the default button", w.get_default_widget() is w.btn_save)
check("no error shown", not w.error_label.get_visible())
close(w)

w = make(ks=True)
eq("kill switch on at open", w.switch.get_active(), True)
close(w)

w = make(uuid=None, active=())
eq("no profile selected: nothing is selected in the combo", w.combo.get_active_id(), None)
close(w)
w = make(uuid="GONE", active=())
eq("stored profile no longer exists: nothing selected", w.combo.get_active_id(), None)
close(w)

w = make(hiddify=True)
eq("Hiddify offered when its service exists", [r[1] for r in w.combo.get_model()], ["OLD", "NEW", "MULTI", "NOHOST", "hiddify"])
eq("Hiddify entry text", list(w.combo.get_model())[-1][0], "Hiddify")
eq("the NetworkManager profile stays selected", w.combo.get_active_id(), "OLD")
close(w)
w = make(hiddify=False)
eq("no Hiddify entry without the service", [r[1] for r in w.combo.get_model()], ["OLD", "NEW", "MULTI", "NOHOST"])
close(w)
prep(backend="hiddify")
w = make(backend="hiddify", hiddify=False)
eq("Hiddify stays in the list while it is the backend", w.combo.get_active_id(), "hiddify")
check("watchdog greyed at open with Hiddify", not w.switch_wd.get_sensitive())
close(w)
w = make(backend="hiddify", hiddify=True)
eq("Hiddify preselected", w.combo.get_active_id(), "hiddify")
close(w)

prep()
w = make(profiles=[])
eq("no profiles at all: the placeholder row", [(r[0], r[1]) for r in w.combo.get_model()], [("No VPN profiles in NetworkManager", "")])
check("no profiles: the combo is disabled", not w.combo.get_sensitive())
w.on_save()
check("no profiles, nothing changed: Save closes", w.closed and cmds == [])
w = make(profiles=[], hiddify=True)
eq("no profiles but Hiddify: only Hiddify", [r[1] for r in w.combo.get_model()], ["hiddify"])
check("... and the combo is enabled", w.combo.get_sensitive())
close(w)

# ================= Cancel, Escape, Save with nothing changed ======================
prep()
before = common.read_settings_text(vt)
w = make()
w.btn_cancel.emit("clicked")
check("Cancel closes", w.closed)
w = make()
ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
ev.key.keyval = Gdk.KEY_a
w.emit("key-press-event", ev)
check("another key does not close", not w.closed)
ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
ev.key.keyval = Gdk.KEY_Escape
w.emit("key-press-event", ev)
check("Escape closes (= Cancel)", w.closed)
w = make()
w.btn_save.emit("clicked")
check("Save with nothing changed closes at once", w.closed)
eq("... runs nothing", cmds, [])
eq("... writes nothing", common.read_settings_text(vt), before)
eq("... touches no tray state", win.tray.calls, [])

# the kill switch switch follows the unit until the user touches it
w = make()
w.tray.ks_active = True
w.sync()
eq("sync follows the unit", w.switch.get_active(), True)
w.switch.set_active(False)          # the user moved it
w.tray.ks_active = True
w.sync()
eq("sync leaves a touched switch alone", w.switch.get_active(), False)
w.busy = True
w.tray.ks_active = False
w.sync()
eq("sync does nothing while a Save runs", w.baseline, True)
w.busy = False
close(w)

# ================= NetworkManager backend: a new profile =============================
prep()
w = make(active=("OLD",))
w.combo.set_active_id("NEW")
w.on_save()
eq("profile change: helper, settings, down OLD, up NEW",
   cmds, [SUDO_HELPER + ["set", "new.example.org"], ["nmcli", "connection", "down", "uuid", "OLD"],
          ["nmcli", "connection", "up", "uuid", "NEW"]])
eq("settings still the old profile while the helper runs", seen["helper"], ("OLD", "nm"))
eq("settings already the new profile when the old one goes down", seen["down"], ("NEW", "nm"))
eq("the tray switched, then started the profile", w.tray.calls, [("set_vpn", "NEW"), ("start_vpn", "NEW")])
check("the old profile's down is announced as expected (no drop notification)", w.tray.expect_down_until > time.time() + 5)
eq("settings: uuid and backend", (vt.load_settings(), vt.load_backend()), ("NEW", "nm"))
eq("other sections kept", {k: v for k, v in snap().items() if k != "vpn"},
   {"notify": {"popup": "true", "sound": "true"}, "watchdog": {"enabled": "true"}})
check("window closed", w.closed)

prep()
w = make(active=())
w.combo.set_active_id("NEW")
w.on_save()
eq("old profile not active: no nmcli down", cmds, [SUDO_HELPER + ["set", "new.example.org"], ["nmcli", "connection", "up", "uuid", "NEW"]])
eq("... and no expected-down window", w.tray.expect_down_until, 0.0)

prep()
w = make(active=("OLD",))
w.combo.set_active_id("MULTI")
w.on_save()
eq("every server host is passed to the helper", cmds[0], SUDO_HELPER + ["set", "a.example.org", "b.example.org"])

prep()
common.reset_settings(vt)
w = make(uuid=None, active=())
w.combo.set_active_id("NEW")
w.on_save()
eq("first selection ever", (cmds, vt.load_settings()),
   ([SUDO_HELPER + ["set", "new.example.org"], ["nmcli", "connection", "up", "uuid", "NEW"]], "NEW"))

# helper failures save nothing
prep()
original = common.read_settings_text(vt)
for stderr, check_msg in (
    ("sudo: a password is required", lambda m: m.startswith(PASSWORD_HINT) and '"sudo" group' in m and "/etc/sudoers.d/vpn-kill-switch" in m),
    ("SUDO: A PASSWORD IS REQUIRED", lambda m: m.startswith(PASSWORD_HINT)),
    ("sudo: /usr/sbin/vpn-killswitch-server: command not found",
     lambda m: m.startswith("Failed to update the kill switch server list: sudo: /usr/sbin/vpn-killswitch-server: command not found")
     and m.endswith("vpn-killswitch-server is not installed; install the vpn-kill-switch package.")),
    ("sudo: /usr/sbin/vpn-killswitch-server: No such file or directory", lambda m: m.endswith("install the vpn-kill-switch package.")),
    ("Cannot resolve vpn.example.org", lambda m: m == "Failed to update the kill switch server list: Cannot resolve vpn.example.org"),
    ("x" * 500, lambda m: m == "Failed to update the kill switch server list: " + "x" * 300),
    ("", lambda m: m.strip() == "Failed to update the kill switch server list:"),
):
    w = make(active=("OLD",))
    w.combo.set_active_id("NEW")
    plan["helper"] = (False, stderr, "")
    w.on_save()
    msg = w.error_label.get_text()
    check("helper failure %r: right message" % stderr[:30], check_msg(msg), msg)
    eq("helper failure %r: only the helper ran" % stderr[:30], cmds, [SUDO_HELPER + ["set", "new.example.org"]])
    eq("helper failure %r: nothing saved" % stderr[:30], common.read_settings_text(vt), original)
    eq("helper failure %r: the tray is untouched" % stderr[:30], (w.tray.calls, w.tray.backend, w.tray.vpn_uuid, w.tray.expect_down_until),
       ([], "nm", "OLD", 0.0))
    check("helper failure %r: error visible, window usable" % stderr[:30],
          w.error_label.get_visible() and not w.closed and not w.busy and w.btn_save.get_sensitive() and w.combo.get_sensitive())
    close(w)
# the window recovers: fix the helper and save again
w = make(active=("OLD",))
w.combo.set_active_id("NEW")
plan["helper"] = (False, "boom", "")
w.on_save()
plan["helper"] = (True, "", "")
cmds.clear()
w.on_save()
check("retry after a helper failure works", w.closed and vt.load_settings() == "NEW" and len(cmds) == 3, cmds)

# no server can be determined
prep()
original = common.read_settings_text(vt)
w = make()
w.combo.set_active_id("NOHOST")
w.on_save()
eq("profile without a server: the error", w.error_label.get_text(),
   "Cannot determine the VPN server for this profile; the kill switch was not updated.")
eq("profile without a server: nothing ran or was saved", (cmds, common.read_settings_text(vt), w.tray.calls), ([], original, []))
check("profile without a server: window usable", not w.closed and not w.busy and w.btn_save.get_sensitive())
w.combo.set_active_id("NEW")
w.tray.client.connections.remove(w.tray.client.get_connection_by_uuid("NEW"))   # deleted meanwhile
w.on_save()
eq("deleted profile: the same error, nothing ran", (w.error_label.get_text().startswith("Cannot determine the VPN server"), cmds), (True, []))
close(w)
w = make(active=())
w.tray.client = None
w.combo.set_active_id("NEW")
w.on_save()
eq("no NetworkManager client: the same error", (w.error_label.get_text().startswith("Cannot determine"), cmds), (True, []))
close(w)

# ================= the kill switch toggle ====================================
prep()
original = common.read_settings_text(vt)
w = make(ks=False)
w.switch.set_active(True)
w.on_save()
eq("kill switch on, nothing else", cmds, [ks_argv("enable")])
eq("... the tray refreshed the unit state", w.tray.ks_active, True)
check("... window closed, settings untouched", w.closed and common.read_settings_text(vt) == original and w.tray.calls == [])
w = make(ks=True)
w.switch.set_active(False)
w.on_save()
eq("kill switch off", cmds, [ks_argv("disable")])
check("... closed", w.closed)
w = make(ks=False, mode=False)
w.switch.set_active(False)
w.switch.set_active(True)
w.switch.set_active(False)
w.on_save()
check("switch moved there and back: no change", w.closed and cmds == [])
w = make(ks=True, mode=False)
w.switch.set_active(False)
w.on_save()
eq("off with a missing mode file needs no helper", cmds, [ks_argv("disable")])

# no profile selected: the kill switch cannot know the server
w = make(uuid=None, active=())
w.switch.set_active(True)
w.on_save()
eq("kill switch on without a VPN profile", w.error_label.get_text(),
   "Select a VPN profile first: the kill switch needs to know the VPN server.")
check("... nothing ran, window usable", cmds == [] and not w.busy and not w.closed and w.btn_save.get_sensitive())
w.combo.set_active_id("NEW")
w.on_save()
eq("... then picking one works: helper, up, enable", cmds,
   [SUDO_HELPER + ["set", "new.example.org"], ["nmcli", "connection", "up", "uuid", "NEW"], ks_argv("enable")])
check("... and the error is gone", not w.error_label.get_visible() and w.closed)

# a profile is selected but the mode file is missing: the helper runs first, nothing else changes
prep()
original = common.read_settings_text(vt)
w = make(ks=False, mode=False)
w.switch.set_active(True)
w.on_save()
eq("kill switch on, mode file missing: helper, then enable", cmds, [SUDO_HELPER + ["set", "old.example.org"], ks_argv("enable")])
check("... no nmcli, no tray change, settings untouched",
      not any(c[0] == "nmcli" for c in cmds) and w.tray.calls == [] and common.read_settings_text(vt) == original and w.closed)
w = make(ks=False, mode=False)
w.switch.set_active(True)
plan["helper"] = (False, "sudo: a password is required", "")
w.on_save()
check("... a failing helper stops before systemctl", cmds == [SUDO_HELPER + ["set", "old.example.org"]]
      and w.error_label.get_text().startswith(PASSWORD_HINT) and not w.closed)
close(w)

# systemctl failures
for plan_ks, effective, want_prefix in (
    ((False, "boom", ""), True, "Failed to enable the kill switch: boom"),
    ((False, "sudo: a password is required", ""), True, "No passwordless permission to manage the kill switch."),
    ((True, "", ""), False, "Failed to enable the kill switch: the unit did not reach the requested state"),
    ((False, "", ""), True, "Failed to enable the kill switch: the unit did not reach the requested state"),
):
    w = make(ks=False)
    plan["ks"] = plan_ks
    plan["ks_effective"] = effective
    w.switch.set_active(True)
    w.on_save()
    msg = w.error_label.get_text()
    check("systemctl failure %r: message" % (plan_ks,), msg.startswith(want_prefix), msg)
    check("systemctl failure %r: window usable, switch stays" % (plan_ks,),
          not w.closed and not w.busy and w.btn_save.get_sensitive() and w.switch.get_active() and w.error_label.get_visible())
    close(w)
w = make(ks=True)
plan["ks"] = (False, "boom", "")
w.switch.set_active(False)
w.on_save()
eq("disable failure", w.error_label.get_text(), "Failed to disable the kill switch: boom")
close(w)

# ================= watchdog and notification switches: the settings file only ============
prep()
w = make()
w.switch_wd.set_active(False)
w.on_save()
eq("watchdog off: no command", cmds, [])
eq("watchdog off: the file", snap(), {"vpn": {"uuid": "OLD", "backend": "nm"}, "notify": {"popup": "true", "sound": "true"},
                                      "watchdog": {"enabled": "false"}})
check("watchdog off: closed, tray notification state untouched", w.closed and (w.tray.notify_popup, w.tray.notify_sound) == (True, True))
w = make()
eq("watchdog switch shows the saved value", w.switch_wd.get_active(), False)
w.switch_wd.set_active(True)
w.on_save()
eq("watchdog back on", (cmds, snap()["watchdog"]), ([], {"enabled": "true"}))
w = make()
w.switch_popup.set_active(False)
w.on_save()
eq("popup off", (cmds, snap()["notify"], (w.tray.notify_popup, w.tray.notify_sound)),
   ([], {"popup": "false", "sound": "true"}, (False, True)))
w = make()
w.switch_sound.set_active(False)
w.switch_popup.set_active(False)
w.on_save()
eq("both off", (cmds, snap()["notify"], (w.tray.notify_popup, w.tray.notify_sound)), ([], {"popup": "false", "sound": "false"}, (False, False)))
eq("vpn section untouched by the switches", snap()["vpn"], {"uuid": "OLD", "backend": "nm"})
prep()
w = make()
w.switch_wd.set_active(False)
w.switch_sound.set_active(False)
w.switch_popup.set_active(True)
w.on_save()
eq("watchdog and sound in one Save", (cmds, snap()["notify"], snap()["watchdog"], (w.tray.notify_popup, w.tray.notify_sound)),
   ([], {"popup": "true", "sound": "false"}, {"enabled": "false"}, (True, False)))

# the order of steps: kill switch first, the local settings only after it worked
prep()
w = make(active=("OLD",), ks=False)
w.combo.set_active_id("NEW")
w.switch.set_active(True)
w.switch_wd.set_active(False)
w.switch_sound.set_active(False)
plan["ks"] = (False, "boom", "")
w.on_save()
eq("failed kill switch step: earlier steps ran",
   cmds, [SUDO_HELPER + ["set", "new.example.org"], ["nmcli", "connection", "down", "uuid", "OLD"],
          ["nmcli", "connection", "up", "uuid", "NEW"], ks_argv("enable")])
eq("... the new profile stays saved", (vt.load_settings(), vt.load_backend(), w.tray.calls), ("NEW", "nm", [("set_vpn", "NEW"), ("start_vpn", "NEW")]))
eq("... the local settings are not written yet", (snap()["watchdog"], snap()["notify"]), ({"enabled": "true"}, {"popup": "true", "sound": "true"}))
eq("... the error", w.error_label.get_text(), "Failed to enable the kill switch: boom")
check("... window open, switches where the user put them",
      not w.closed and not w.busy and w.switch.get_active() and not w.switch_wd.get_active() and not w.switch_sound.get_active())
cmds.clear()
plan["ks"] = (True, "", "")
w.on_save()
eq("retry: only the kill switch step", cmds, [ks_argv("enable")])
eq("retry: the local settings are written now", (snap()["watchdog"], snap()["notify"], w.tray.notify_sound), ({"enabled": "false"}, {"popup": "true", "sound": "false"}, False))
check("retry: closed", w.closed)

# a failing settings write after a good kill switch step
prep()
w = make(ks=False)
w.switch.set_active(True)
w.switch_wd.set_active(False)
real_save = vt.save_settings


def broken_save(*a, **k):
    raise OSError("disk full")


vt.save_settings = broken_save
with common.quiet():
    w.on_save()
vt.save_settings = real_save
eq("settings write failure", (cmds, w.error_label.get_text()), ([ks_argv("enable")], "Failed to save the settings: disk full"))
check("... window usable, baseline untouched", not w.closed and not w.busy and w.wd_baseline is True)
close(w)
prep()
w = make(active=("OLD",))
w.combo.set_active_id("NEW")
vt.save_settings = broken_save
with common.quiet():
    w.on_save()
vt.save_settings = real_save
eq("profile saved failure: error, the old profile is not stopped", (w.error_label.get_text(), len(cmds), w.tray.calls),
   ("Failed to save the settings: disk full", 1, []))
close(w)

# ================= Hiddify backend ======================================================
prep()
w = make(hiddify=True)
subtitle = lambda: w.wd_row.subtitle_label.get_text()  # noqa: E731
check("watchdog row normal", w.switch_wd.get_sensitive() and subtitle() == "Reconnect the selected VPN profile automatically when it drops.")
w.combo.set_active_id("hiddify")
check("choosing Hiddify greys the watchdog row", not w.switch_wd.get_sensitive()
      and subtitle() == "Not used with Hiddify: it reconnects by itself.", subtitle())
check("... the stored value is untouched", vt.load_watchdog_enabled() is True and w.switch_wd.get_active() is True)
w.combo.set_active_id("NEW")
check("choosing a profile again restores it", w.switch_wd.get_sensitive() and subtitle().startswith("Reconnect the selected"))
close(w)

# NetworkManager -> Hiddify
prep()
w = make(hiddify=True, active=("OLD",))
w.combo.set_active_id("hiddify")
w.on_save()
eq("NM -> Hiddify: helper set-hiddify, then the old profile goes down, no nmcli up",
   cmds, [SUDO_HELPER + ["set-hiddify"], ["nmcli", "connection", "down", "uuid", "OLD"]])
eq("... order against the settings file", (seen["helper"], seen["down"]), (("OLD", "nm"), ("OLD", "hiddify")))
eq("... backend saved, the profile uuid is kept", (vt.load_backend(), vt.load_settings()), ("hiddify", "OLD"))
eq("... the tray was told", w.tray.calls, [("set_backend", "hiddify")])
check("... expected down, closed", w.tray.expect_down_until > time.time() + 5 and w.closed)
prep()
w = make(hiddify=True, active=())
w.combo.set_active_id("hiddify")
w.on_save()
eq("NM -> Hiddify with the profile down: only the helper", cmds, [SUDO_HELPER + ["set-hiddify"]])
eq("... and no expected-down window", w.tray.expect_down_until, 0.0)

# the helper fails
prep()
original = common.read_settings_text(vt)
w = make(hiddify=True, active=("OLD",))
w.combo.set_active_id("hiddify")
plan["helper"] = (False, "sudo: a password is required", "")
w.on_save()
check("Hiddify, helper failure: nothing saved, tray untouched", common.read_settings_text(vt) == original and w.tray.calls == []
      and w.tray.backend == "nm" and len(cmds) == 1)
check("... sudo hint", w.error_label.get_text().startswith(PASSWORD_HINT), w.error_label.get_text())
check("... window usable and the watchdog row still greyed", not w.closed and not w.busy and w.btn_save.get_sensitive()
      and not w.switch_wd.get_sensitive())
plan["helper"] = (False, "boom", "")
w.on_save()
eq("... generic text", w.error_label.get_text(), "Failed to update the kill switch server list: boom")
plan["helper"] = (False, "No such file or directory", "")
w.on_save()
check("... helper missing hint", "vpn-killswitch-server is not installed" in w.error_label.get_text())
close(w)

# Hiddify -> NetworkManager
prep("OLD", "hiddify")
w = make(backend="hiddify", hiddify=True, active=())
w.combo.set_active_id("NEW")
w.on_save()
eq("Hiddify -> NM: helper set, then up (nothing to take down)", cmds, [SUDO_HELPER + ["set", "new.example.org"], ["nmcli", "connection", "up", "uuid", "NEW"]])
eq("... order against the settings file", seen["helper"], ("OLD", "hiddify"))
eq("... saved", (vt.load_backend(), vt.load_settings(), w.tray.calls), ("nm", "NEW", [("set_vpn", "NEW"), ("start_vpn", "NEW")]))
prep("OLD", "hiddify")
w = make(backend="hiddify", hiddify=True, active=("OLD",))
w.combo.set_active_id("OLD")
w.on_save()
eq("Hiddify -> the stored NM profile is a change; an active profile is not taken down",
   (cmds, w.tray.calls), ([SUDO_HELPER + ["set", "old.example.org"], ["nmcli", "connection", "up", "uuid", "OLD"]],
                          [("set_vpn", "OLD"), ("start_vpn", "OLD")]))
eq("... backend back to nm", vt.load_backend(), "nm")

# Hiddify, nothing changed
prep("OLD", "hiddify")
original = common.read_settings_text(vt)
w = make(backend="hiddify", hiddify=True)
w.on_save()
check("Hiddify, nothing changed: closes, no command, no write", w.closed and cmds == [] and common.read_settings_text(vt) == original)

# the kill switch with Hiddify
prep("OLD", "hiddify")
w = make(backend="hiddify", hiddify=True, mode=False)
w.switch.set_active(True)
w.on_save()
eq("kill switch on, Hiddify, no mode file: set-hiddify, enable", cmds, [SUDO_HELPER + ["set-hiddify"], ks_argv("enable")])
eq("... no profile switch", w.tray.calls, [])
w = make(backend="hiddify", hiddify=True, mode=True)
w.switch.set_active(True)
w.on_save()
eq("kill switch on, Hiddify, mode file present: only systemctl", cmds, [ks_argv("enable")])
prep("OLD", "nm")
w = make(hiddify=True, mode=False)
w.switch.set_active(True)
w.on_save()
eq("kill switch on, NM profile, no mode file: set HOST", cmds[:1], [SUDO_HELPER + ["set", "old.example.org"]])
prep("OLD", "nm")
w = make(hiddify=True, mode=True, uuid=None, active=())
w.switch.set_active(True)
w.on_save()
check("kill switch on with no profile still needs a selection", w.error_label.get_text().startswith("Select a VPN profile first") and cmds == [])
w.combo.set_active_id("hiddify")
w.on_save()
eq("... choosing Hiddify (unsaved) satisfies the guard", cmds, [SUDO_HELPER + ["set-hiddify"], ks_argv("enable")])
eq("... and saves the backend", vt.load_backend(), "hiddify")

# the stored watchdog value is never changed by choosing Hiddify
prep("OLD", "nm", watchdog=True)
w = make(hiddify=True)
w.combo.set_active_id("hiddify")
w.on_save()
eq("watchdog value untouched by a Hiddify Save", vt.load_watchdog_enabled(), True)
prep("OLD", "nm", watchdog=False)
w = make(hiddify=True)
eq("a stored 'off' is shown while the row is greyed", w.switch_wd.get_active(), False)
close(w)

T.done()
