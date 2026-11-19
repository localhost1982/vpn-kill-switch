# SPDX-License-Identifier: MIT
"""hiddify_events(): the Hiddify tunnel chronology (NetworkManager device lines, Hiddify service
markers, kernel suspend / boot lines) with the shared contract fixture. No display needed."""

import os
import random
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import Results  # noqa: E402

vt = common.load_tray()
T = Results("test_hiddify_events")
check, eq = T.check, T.eq

FIXTURE = common.journal_lines("hiddify.journal")


def rows(events):
    return [(e["time"], e["kind"], e["detail"], e["downtime"]) for e in events]


UPD = "tunnel tun0"
EXPECTED = [
    (1000.0, "Boot", "", None),
    (1010.1, "Connected", UPD, None),                 # the 1010.3 / 1010.5 flap is removed
    (2000.2, "Drop", "on command", 30),
    (2030.0, "Connected", UPD, None),
    (3000.0, "Suspend", "", None),
    (3001.0, "Drop", "suspend", 10),                   # 610 s minus the 600 s asleep
    (3601.0, "Wake", "", None),
    (3611.0, "Connected", UPD, None),
    (4000.0, "Drop", "other", 50),                     # the second DOWN at 4000.5 is ignored
    (4005.0, "Connect failed", "Hiddify core failed to start", None),
    (4050.0, "Connected", UPD, None),
    (5100.0, "Drop", "other", None),                   # ongoing (the wlan0 line at 5000 is ignored)
]

ev, s = vt.hiddify_events(FIXTURE, "tun0", 5200, False)
eq("contract rows", rows(ev), EXPECTED)
check("every Drop opens an outage (also the ongoing one)", all(e["opens_gap"] for e in ev if e["kind"] == "Drop"))
check("non-drop rows do not open one", not any(e["opens_gap"] for e in ev if e["kind"] != "Drop"))
eq("contract summary", s, {"drops": 4, "on_command": 1, "suspend": 1, "other": 2, "successes": 4, "failures": 1,
                           "attempts": 5, "downtime_total": 190, "downtime_longest": 100, "lcp": 0, "signal": 1})

ev2, s2 = vt.hiddify_events(FIXTURE, "tun0", 5200, True)
eq("tunnel connected now: drops, downtime", (s2["drops"], s2["downtime_total"], s2["downtime_longest"]), (4, 90, 50))
check("tunnel connected now: the last drop is not ongoing",
      ev2[-1]["kind"] == "Drop" and not ev2[-1]["opens_gap"] and ev2[-1]["downtime"] is None)

ev3, s3 = vt.hiddify_events(FIXTURE, "wg0", 5200, False)
check("other interface: no tunnel rows", s3["drops"] == 0 and not any(e["kind"] in ("Connected", "Drop") for e in ev3), s3)
eq("other interface: boot and suspend rows remain", [e["kind"] for e in ev3], ["Boot", "Suspend", "Wake", "Connect failed"])
ev3, _ = vt.hiddify_events(FIXTURE, "wlan0", 5200, False)
eq("wlan0: its lone DOWN comes after the boot (state down) and is ignored",
   [e["kind"] for e in ev3 if e["kind"] in ("Connected", "Drop")], [])

shuffled = list(FIXTURE)
random.Random(3).shuffle(shuffled)
eq("shuffled input: same rows", rows(vt.hiddify_events(shuffled, "tun0", 5200, False)[0]), EXPECTED)
eq("empty input", vt.hiddify_events([], "tun0", 100, False)[0], [])
eq("junk input", vt.hiddify_events(["", "garbage", "x y z"], "tun0", 100, False)[0], [])
eq("interface names are escaped", vt.hiddify_events(
    ["10.0 h NetworkManager[1]: device (tunX0): state change: secondaries -> activated (reason 'x')"], "tun.0", 100, True)[0], [])

# ---- small journals
NMU = "%s h NetworkManager[1]: <info> device (tun0): state change: secondaries -> activated (reason 'x')"
NMD = "%s h NetworkManager[1]: <info> device (tun0): state change: activated -> unmanaged (reason 'x')"
STOP = "%s h HiddifyCli[2]: 2026/11/19 22:27:59 [timing] stop: core is stopped after 49ms"
STARTOK = "%s h HiddifyCli[2]: adminbg: hcore.Start took 12ms (err=<nil>)"
STARTBAD = "%s h HiddifyCli[2]: adminbg: hcore.Start took 12ms (err=boom)"
SLEEP = "%s h kernel: PM: suspend entry (deep)"
WAKE = "%s h kernel: PM: suspend exit"
BOOT = "%s h kernel: Linux version 6.14.0"
NMSTOP = "%s h systemd[1]: Stopped NetworkManager.service - Network Manager."


def J(*items):
    return [fmt % t for fmt, t in items]


def kinds(events):
    return [(e["kind"], e["detail"]) for e in events]


# duplicated STOP markers (three lines of one marker) and a drop explained by it
e, s = vt.hiddify_events(J((STOP, 100.0), (STOP, 100.0001), (STOP, 100.5), (NMD, 100.6), (NMU, 110.0), (NMD, 200.0), (NMU, 300.0)),
                         "tun0", 400, True)
eq("duplicate markers and causes", (kinds(e), s["on_command"], s["other"]),
   ([("Drop", "on command"), ("Connected", UPD), ("Drop", "other"), ("Connected", UPD)], 1, 1))
eq("STOP exactly 5 s away explains a drop", kinds(vt.hiddify_events(J((STOP, 100.0), (NMD, 105.0)), "tun0", 200, True)[0]),
   [("Drop", "on command")])
eq("STOP 5 s after the drop too", kinds(vt.hiddify_events(J((NMD, 100.0), (STOP, 105.0)), "tun0", 200, True)[0]),
   [("Drop", "on command")])
eq("STOP more than 5 s away: other", kinds(vt.hiddify_events(J((STOP, 100.0), (NMD, 105.5)), "tun0", 200, True)[0]),
   [("Drop", "other")])
e, _ = vt.hiddify_events(J((SLEEP, 100.0), (STOP, 101.0), (NMD, 102.0)), "tun0", 200, True)
eq("on command wins over suspend", [x["detail"] for x in e if x["kind"] == "Drop"], ["on command"])
e, _ = vt.hiddify_events(J((SLEEP, 100.0), (NMD, 500.0), (WAKE, 900.0), (NMU, 910.0)), "tun0", 2000, True)
eq("a drop between suspend and wake is a suspend drop", [x["detail"] for x in e if x["kind"] == "Drop"], ["suspend"])
e, _ = vt.hiddify_events(J((SLEEP, 100.0), (WAKE, 200.0), (NMD, 210.0)), "tun0", 2000, True)
eq("a drop after the wake is not", [x["detail"] for x in e if x["kind"] == "Drop"], ["other"])
e, _ = vt.hiddify_events(J((SLEEP, 100.0), (WAKE, 200.0), (NMD, 200.0)), "tun0", 2000, True)
eq("a drop at the very wake line is still a suspend drop", [x["detail"] for x in e if x["kind"] == "Drop"], ["suspend"])
e, _ = vt.hiddify_events(J((NMD, 100.0), (SLEEP, 104.0), (WAKE, 300.0)), "tun0", 2000, True)
eq("a suspend within 5 s after the drop explains it", [x["detail"] for x in e if x["kind"] == "Drop"], ["suspend"])

# flaps: a DOWN directly followed by an UP < 1.5 s later is Hiddify recreating the interface
e, s = vt.hiddify_events(J((NMU, 10.0), (NMD, 10.2), (NMU, 10.4)), "tun0", 100, True)
eq("flap after connect", (kinds(e), s["drops"], s["successes"]), ([("Connected", UPD)], 0, 1))
e, s = vt.hiddify_events(J((NMU, 10.0), (NMD, 12.0), (NMU, 13.4)), "tun0", 100, True)
eq("1.4 s outage is a flap", (kinds(e), s["drops"]), ([("Connected", UPD)], 0))
e, s = vt.hiddify_events(J((NMU, 10.0), (NMD, 12.0), (NMU, 13.6)), "tun0", 100, True)
eq("1.6 s outage is a drop", (kinds(e), s["drops"], s["downtime_total"]),
   ([("Connected", UPD), ("Drop", "other"), ("Connected", UPD)], 1, 2))
e, s = vt.hiddify_events(J((NMU, 10.0), (NMD, 12.0), (NMU, 14.0)), "tun0", 100, True)
eq("2 s outage is a drop", (s["drops"], s["downtime_total"]), (1, 2))
# the flap is judged among tunnel lines only: another line in between does not break it
e, s = vt.hiddify_events(J((NMU, 10.0), (NMD, 12.0), (STOP, 12.5), (NMU, 13.0)), "tun0", 100, True)
eq("other lines between DOWN and UP do not matter", (kinds(e), s["drops"]), ([("Connected", UPD)], 0))

# state machine corners
e, s = vt.hiddify_events(J((NMU, 10.0), (NMU, 20.0), (NMD, 50.0), (NMD, 60.0), (NMU, 100.0)), "tun0", 200, True)
eq("repeated UP / DOWN are ignored", (kinds(e), s["downtime_total"]), ([("Connected", UPD), ("Drop", "other"), ("Connected", UPD)], 50))
e, s = vt.hiddify_events(J((NMD, 10.0), (NMU, 40.0)), "tun0", 100, True)
eq("DOWN as the first event is a drop", (kinds(e), s["downtime_total"]), ([("Drop", "other"), ("Connected", UPD)], 30))
e, s = vt.hiddify_events(J((STARTBAD, 10.0), (STARTOK, 20.0), (STARTBAD, 30.0)), "tun0", 100, True)
eq("only a start without err=<nil> is a failure", (kinds(e), s["failures"], s["attempts"]),
   ([("Connect failed", "Hiddify core failed to start")] * 2, 2, 2))
e, s = vt.hiddify_events(J((NMD, 100.0), (BOOT, 150.0), (NMU, 200.0)), "tun0", 300, True)
eq("a boot discards the open outage", (kinds(e), s["drops"], s["downtime_total"], e[0]["opens_gap"]),
   ([("Drop", "other"), ("Boot", ""), ("Connected", UPD)], 1, 0, False))
e, s = vt.hiddify_events(J((NMU, 10.0), (BOOT, 150.0), (NMD, 160.0)), "tun0", 300, False)
eq("a DOWN after a boot is ignored (state is already down)", (kinds(e), s["drops"]), ([("Connected", UPD), ("Boot", "")], 0))
e, s = vt.hiddify_events(J((NMU, 10.0), (NMSTOP, 20.0)), "tun0", 300, True)
eq("NetworkManager restart row", kinds(e), [("Connected", UPD), ("NetworkManager restart", "")])
# still asleep at the end with the tunnel down: counted up to the suspend only
e, s = vt.hiddify_events(J((NMU, 10.0), (NMD, 100.0), (SLEEP, 120.0)), "tun0", 5000, False)
eq("asleep at the end", (s["downtime_total"], e[1]["downtime"]), (20, None))
# a drop while asleep, ongoing after the wake: counted from the wake
e, s = vt.hiddify_events(J((NMU, 10.0), (SLEEP, 100.0), (NMD, 101.0), (WAKE, 700.0)), "tun0", 750, False)
eq("drop while asleep, still down 50 s after the wake", s["downtime_total"], 50)

T.done()
