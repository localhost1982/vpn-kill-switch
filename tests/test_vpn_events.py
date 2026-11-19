# SPDX-License-Identifier: MIT
"""vpn_events(): the pppd / NetworkManager journal logic behind the "VPN drops" window and the
NetworkManager-profile numbers (the same rules as vpn-stats.sh). No display needed."""

import os
import random
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import Results  # noqa: E402

vt = common.load_tray()
T = Results("test_vpn_events")
check, eq = T.check, T.eq

LINES = common.journal_lines("nm-pppd.journal")
NAME = "My VPN"


def rows(events):
    """(time, kind, detail, downtime, opens_gap for drops)."""
    return [(e["time"], e["kind"], e["detail"], e["downtime"], e.get("opens_gap") if e["kind"] == "Drop" else None)
            for e in events]


LCP, CMD, SUSP, OTHER = ("server stopped responding (LCP)", "on command", "suspend", "other")
CONNECTED = "default route via ppp0"

EXPECTED = [
    (100.0, "Boot", "", None, None),
    (110.0, "Connecting", "starting", None, None),
    (120.0, "Connected", CONNECTED, None, None),
    (1000.0, "Drop", LCP, 25, True),
    (1010.0, "Connecting", "starting", None, None),
    (1025.0, "Connected", CONNECTED, None, None),
    (2000.0, "Drop", CMD, 30, True),
    (2030.0, "Connected", CONNECTED, None, None),
    (3000.0, "Suspend", "", None, None),
    (3002.0, "Drop", SUSP, 10, True),          # 3300 -> 3310 only: the 298 s asleep do not count
    (3300.0, "Wake", "", None, None),
    (3310.0, "Connected", CONNECTED, None, None),
    (3400.0, "NetworkManager restart", "", None, None),
    (4000.0, "Drop", OTHER, 120, True),         # "Connection terminated" without an LCP / signal line
    (4010.0, "Connecting", "starting", None, None),
    (4040.0, "Connect failed", "timeout", None, None),
    (4050.0, "Connecting", "starting", None, None),
    (4055.0, "Connect failed", "no password agent", None, None),
    (4100.0, "Connecting", "starting", None, None),
    (4120.0, "Connected", CONNECTED, None, None),
    (5000.0, "Drop", CMD, None, True),          # still down at "now" (5100): ongoing
    (5010.0, "Drop", LCP, None, False),         # a second drop of the same outage: no downtime of its own
]

# the whole synthetic journal, the profile is down at the end
ev, s = vt.vpn_events(LINES, NAME, 5100, False)
eq("rows (ongoing outage)", rows(ev), EXPECTED)
eq("summary (ongoing outage)", s, {
    "drops": 6, "lcp": 2, "signal": 3, "other": 1,
    "attempts": 5, "successes": 5, "failures": 1, "noagent": 1,
    "downtime_total": 285, "downtime_longest": 120,   # 25 + 30 + 10 + 120 + 100 (up to now)
})

# the same journal with the VPN connected now: the open outage is not counted
ev2, s2 = vt.vpn_events(LINES, NAME, 5100, True)
want = [r[:4] + ((False,) if r[1] == "Drop" and r[0] == 5000.0 else r[4:]) for r in EXPECTED]
eq("rows (VPN up at the end)", rows(ev2), want)
eq("summary (VPN up at the end)", (s2["drops"], s2["downtime_total"], s2["downtime_longest"]), (6, 185, 120))

# the order of the lines does not matter
shuffled = list(LINES)
random.Random(7).shuffle(shuffled)
ev3, s3 = vt.vpn_events(shuffled, NAME, 5100, False)
eq("shuffled input: same rows", rows(ev3), EXPECTED)
eq("shuffled input: same summary", s3, s)

# empty / junk input
ev, s = vt.vpn_events([], NAME, 100, False)
eq("empty: no rows", ev, [])
eq("empty: summary", s, {"drops": 0, "lcp": 0, "signal": 0, "other": 0, "attempts": 0, "successes": 0,
                         "failures": 0, "noagent": 0, "downtime_total": 0, "downtime_longest": 0})
ev, s = vt.vpn_events(["", "-- Journal begins at Mon --", "nonsense", "x 1 pppd[1]: Terminating on signal 15",
                       "100 host unrelated: message"], NAME, 200, False)
eq("junk lines are ignored", (ev, s["drops"]), ([], 0))

# lines of another profile are ignored (the profile-independent pppd lines are not)
other = vt.vpn_events(LINES, "Other VPN", 5100, False)
eq("another profile: its own start / up / failure", (other[1]["attempts"], other[1]["successes"], other[1]["failures"]), (1, 1, 1))
eq("another profile: no 'My VPN' rows",
   [r for r in rows(other[0]) if r[1] in ("Connecting", "Connected", "Connect failed") and r[2] != "no password agent"][:3],
   [(700.0, "Connecting", "starting", None, None), (710.0, "Connected", "default route via tun1", None, None),
    (720.0, "Connect failed", "timeout", None, None)])
eq("unknown profile: nothing of start / up / failure",
   (lambda sm: (sm["attempts"], sm["successes"], sm["failures"]))(vt.vpn_events(LINES, "No Such VPN", 5100, False)[1]), (0, 0, 0))

# ---- small journals

def mk(*pairs):
    """[(time, text)] -> journal lines."""
    return ["%.6f host %s" % (t, text) for t, text in pairs]


UP = "NetworkManager[1]: <info>  [1.0] policy: set 'My VPN' (ppp0) as default for IPv4 routing and DNS"
LCPL = "pppd[%d]: No response to 4 echo-requests"
TERM = "pppd[%d]: Terminating on signal 15"
CONN = "pppd[%d]: Connection terminated."
SLEEP = "kernel: PM: suspend entry (deep)"
WAKE = "kernel: PM: suspend exit"


def drops(journal, now=10**6, active=False):
    ev, sm = vt.vpn_events(journal, NAME, now, active)
    return [(e["time"], e["detail"]) for e in ev if e["kind"] == "Drop"], sm


# LCP drop and command drop
d, sm = drops(mk((10, UP), (100, LCPL % 5), (100.2, CONN % 5), (110, UP), (200, TERM % 6), (200.1, CONN % 6), (230, UP)))
eq("LCP and command drops", d, [(100, LCP), (200, CMD)])
eq("LCP / command counts", (sm["drops"], sm["lcp"], sm["signal"], sm["other"]), (2, 1, 1, 0))
eq("downtimes 10 + 30", (sm["downtime_total"], sm["downtime_longest"]), (40, 30))

# duplicate "Connection terminated" of the same pppd: one row
ev, _ = vt.vpn_events(mk((10, UP), (100, TERM % 7), (100.1, CONN % 7), (100.2, CONN % 7), (150, UP)), NAME, 999, False)
eq("same pid: one Drop row", [e["kind"] for e in ev], ["Connected", "Drop", "Connected"])
# ... but another pppd PID is another drop
ev, _ = vt.vpn_events(mk((10, UP), (100, CONN % 7), (101, CONN % 8)), NAME, 999, True)
eq("different pids: two Drop rows", [e["kind"] for e in ev], ["Connected", "Drop", "Drop"])
eq("only the first drop of an outage opens it", [e.get("opens_gap") for e in ev if e["kind"] == "Drop"], [False, False])
ev, _ = vt.vpn_events(mk((10, UP), (100, CONN % 7), (101, CONN % 8)), NAME, 999, False)
eq("outage still open and VPN down: first drop opens it", [e.get("opens_gap") for e in ev if e["kind"] == "Drop"], [True, False])
# the PID is remembered across an intervening line
d, _ = drops(mk((10, UP), (100, LCPL % 9), (105, UP), (200, CONN % 9)))
eq("a later 'Connection terminated' of an old PID is no new drop", d, [(100, LCP)])

# a drop within 5 s of a suspend is a suspend drop (before or after it), 6 s is a command
for off, want in ((0, SUSP), (2, SUSP), (5, SUSP), (5.1, CMD), (-3, SUSP), (-5, SUSP), (-6, CMD)):
    d, _ = drops(mk((10, UP), (1000, SLEEP), (1000 + off, TERM % 3), (1000 + off + 0.1, CONN % 3), (2000, WAKE), (2010, UP)))
    eq("terminate %+.1f s from the suspend" % off, d, [(1000 + off, want)])

# suspend inside an outage is not downtime
ev, sm = vt.vpn_events(mk((10, UP), (100, LCPL % 4), (120, SLEEP), (620, WAKE), (625, UP)), NAME, 10**6, False)
eq("suspended time is cut out", next(e["downtime"] for e in ev if e["kind"] == "Drop"), 25)   # 20 + 5
eq("suspend / wake rows", [e["kind"] for e in ev], ["Connected", "Drop", "Suspend", "Wake", "Connected"])
# ongoing outage, still asleep at the end: counted up to the suspend only
ev, sm = vt.vpn_events(mk((10, UP), (100, LCPL % 4), (120, SLEEP)), NAME, 10**6, False)
eq("still asleep at the end", (sm["downtime_total"], [e["downtime"] for e in ev if e["kind"] == "Drop"]), (20, [None]))
# ongoing outage after a wake: counted from the wake
ev, sm = vt.vpn_events(mk((10, UP), (100, LCPL % 4), (120, SLEEP), (620, WAKE)), NAME, 700, False)
eq("woke up, still down at now", sm["downtime_total"], 20 + 80)
# a second suspend line without a wake in between changes nothing
ev, sm = vt.vpn_events(mk((10, UP), (100, LCPL % 4), (120, SLEEP), (130, SLEEP), (620, WAKE), (625, UP)), NAME, 10**6, False)
eq("double suspend line", sm["downtime_total"], 25)

# an "other" drop: only "Connection terminated"
d, sm = drops(mk((10, UP), (100, CONN % 3), (130, UP)))
eq("other drop", (d, sm["other"], sm["lcp"], sm["signal"]), ([(100, OTHER)], 1, 0, 0))

# failed connect: timeout and no agent, and their counts
ev, sm = vt.vpn_events(mk(
    (10, 'NetworkManager[1]: <info>  [1.0] vpn[0x1,"u","My VPN"]: starting l2tp'),
    (40, 'NetworkManager[1]: <info>  [1.0] vpn[0x1,"u","My VPN"]: failed to connect: \'Connect timeout\''),
    (50, 'NetworkManager[1]: <warn>  [1.0] secrets: failed to request VPN secrets #3: No agents were available for this request.')),
    NAME, 100, False)
eq("failed connects", rows(ev), [(10.0, "Connecting", "starting", None, None), (40.0, "Connect failed", "timeout", None, None),
                                 (50.0, "Connect failed", "no password agent", None, None)])
eq("failed connect counts", (sm["attempts"], sm["successes"], sm["failures"], sm["noagent"], sm["drops"]), (1, 0, 1, 1, 0))

# ongoing outage with VPN inactive vs active
j = mk((10, UP), (100, TERM % 3), (100.1, CONN % 3))
ev, sm = vt.vpn_events(j, NAME, 400, False)
eq("ongoing, VPN inactive: counted to now", (sm["downtime_total"], sm["downtime_longest"], ev[-1]["downtime"], ev[-1]["opens_gap"]), (300, 300, None, True))
ev, sm = vt.vpn_events(j, NAME, 400, True)
eq("ongoing, VPN active: not counted", (sm["downtime_total"], sm["downtime_longest"], ev[-1]["downtime"], ev[-1]["opens_gap"]), (0, 0, None, False))

# nothing is known before the first VPN event: a first UP is no downtime, a first drop opens an outage
ev, sm = vt.vpn_events(mk((10, UP)), NAME, 100, False)
eq("first event is a connection", (rows(ev), sm["downtime_total"], sm["successes"]), ([(10.0, "Connected", CONNECTED, None, None)], 0, 1))
ev, sm = vt.vpn_events(mk((50, TERM % 1), (50.1, CONN % 1), (80, UP)), NAME, 100, False)
eq("first event is a drop", (ev[0]["downtime"], sm["downtime_total"]), (30, 30))

# interface name in the "Connected" row follows the log line
ev, _ = vt.vpn_events(mk((10, "NetworkManager[1]: <info>  [1.0] policy: set 'My VPN' (tun3) as default for IPv4 routing and DNS")),
                      NAME, 100, False)
eq("interface in details", ev[0]["detail"], "default route via tun3")

# profile names with regular expression characters
for tricky in ("Work (v2) [a.b]+*", "A.B", "x|y", "a\\b", "VPN $1 ^2", "100%"):
    j = mk((10, 'NetworkManager[1]: <info>  [1.0] vpn[0x1,"u","%s"]: starting l2tp' % tricky),
           (20, "NetworkManager[1]: <info>  [1.0] policy: set '%s' (ppp0) as default for IPv4 routing and DNS" % tricky),
           (30, 'NetworkManager[1]: <info>  [1.0] vpn[0x1,"u","%s"]: failed to connect: \'x\'' % tricky))
    _, sm = vt.vpn_events(j, tricky, 100, False)
    eq("name %r matches itself" % tricky, (sm["attempts"], sm["successes"], sm["failures"]), (1, 1, 1))
j = mk((10, 'NetworkManager[1]: <info>  [1.0] vpn[0x1,"u","AxB"]: starting l2tp'),
       (20, "NetworkManager[1]: <info>  [1.0] policy: set 'AxB' (ppp0) as default for IPv4 routing and DNS"))
eq("'.' in a name is not a wildcard", vt.vpn_events(j, "A.B", 100, False)[1]["attempts"], 0)
j = mk((10, 'NetworkManager[1]: <info>  [1.0] vpn[0x1,"u","aa"]: starting l2tp'))
eq("'a+' does not match 'aa'", vt.vpn_events(j, "a+", 100, False)[1]["attempts"], 0)
eq("'(' alone does not break the pattern", vt.vpn_events(j, "(", 100, False)[1]["attempts"], 0)

# the patterns object (shared names with vpn-stats.sh)
check("vpn_patterns keys", set(vt.vpn_patterns("x")) == {"noagent", "lcp", "term", "conn", "sleep", "wake", "nmstop", "boot",
                                                         "start", "up", "fail"})

T.done()
