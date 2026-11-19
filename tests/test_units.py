# SPDX-License-Identifier: MIT
"""Pure functions of the tray: formatting, endpoint / server parsing, dropped-packet lines, the drop
detector, the settings file and the version / "About" paths. No display needed."""

import os
import shutil
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import Results, FakeConnection, FakeWireGuard, RunStub, quiet, vpn_conn  # noqa: E402

vt = common.load_tray()

T = Results("test_units")
check, eq = T.check, T.eq

# ---- fmt_duration
for secs, want in [(0, "0s"), (1, "1s"), (59, "59s"), (60, "1m 00s"), (185, "3m 05s"), (3599, "59m 59s"),
                   (3600, "1h 00m"), (3720, "1h 02m"), (86399, "23h 59m"), (86400, "1d 00h"),
                   (97200, "1d 03h"), (12.9, "12s")]:
    eq("fmt_duration(%s)" % secs, vt.fmt_duration(secs), want)

# ---- plural_packets
eq("plural 1", vt.plural_packets(1), "packet")
eq("plural 0", vt.plural_packets(0), "packets")
eq("plural 2", vt.plural_packets(2), "packets")

# ---- host_from_endpoint
for entry, want in [
    ("vpn.example.org", "vpn.example.org"),
    ("vpn.example.org:1194", "vpn.example.org"),
    ("1.2.3.4:1194:udp", "1.2.3.4"),
    ("  1.2.3.4  ", "1.2.3.4"),
    ("[2001:db8::1]:51820", "2001:db8::1"),
    ("[::1]", "::1"),
    ("2001:db8::1", "2001:db8::1"),
    ("fe80::1", "fe80::1"),
    ("", ""),
]:
    eq("host_from_endpoint(%r)" % entry, vt.host_from_endpoint(entry), want)

# ---- vpn_server_hosts
eq("gateway", vt.vpn_server_hosts(vpn_conn("a", "u", gateway="vpn.example.org")), ["vpn.example.org"])
eq("gateway with port", vt.vpn_server_hosts(vpn_conn("a", "u", gateway="1.2.3.4:500")), ["1.2.3.4"])
eq("remote list", vt.vpn_server_hosts(vpn_conn("a", "u", remote="a.example.org:1194:udp, b.example.org:443:tcp  c.example.org")),
   ["a.example.org", "b.example.org", "c.example.org"])
eq("remote dedup", vt.vpn_server_hosts(vpn_conn("a", "u", remote="a.example.org:1,a.example.org:2")), ["a.example.org"])
eq("address", vt.vpn_server_hosts(vpn_conn("a", "u", address="10.1.1.1")), ["10.1.1.1"])
eq("gateway wins over remote", vt.vpn_server_hosts(vpn_conn("a", "u", gateway="g.example.org", remote="r.example.org")),
   ["g.example.org"])
eq("blank gateway falls to remote", vt.vpn_server_hosts(vpn_conn("a", "u", gateway="  ", remote="r.example.org:1")),
   ["r.example.org"])
eq("vpn without data items", vt.vpn_server_hosts(vpn_conn("a", "u")), [])
eq("vpn without setting", vt.vpn_server_hosts(FakeConnection("a", "u", "vpn", vpn=None)), [])
wg = FakeConnection("wg", "w", "wireguard",
                    wg=FakeWireGuard(["1.2.3.4:51820", "[2001:db8::1]:51820", "", "1.2.3.4:51821", "wg.example.org:51820"]))
eq("wireguard peers", vt.vpn_server_hosts(wg), ["1.2.3.4", "2001:db8::1", "wg.example.org"])
eq("wireguard no peers", vt.vpn_server_hosts(FakeConnection("wg", "w", "wireguard", wg=FakeWireGuard([]))), [])
eq("wireguard no setting", vt.vpn_server_hosts(FakeConnection("wg", "w", "wireguard", wg=None)), [])
eq("other connection type", vt.vpn_server_hosts(FakeConnection("e", "e", "802-3-ethernet")), [])
with quiet() as err:
    eq("broken connection", vt.vpn_server_hosts(FakeConnection("b", "b", broken=True)), [])
check("broken connection logged", "vpn_server_hosts" in err.getvalue())

# ---- parse_drop_line
P = vt.parse_drop_line
tcp = P("1795114531.123456 host kernel: KS-DROP: IN= OUT=wlan0 SRC=192.168.1.5 DST=93.184.216.34 LEN=60 TOS=0x00 "
        "PREC=0x00 TTL=64 ID=1234 DF PROTO=TCP SPT=43210 DPT=443 WINDOW=64240 RES=0x00 SYN URGP=0 UID=1000 GID=1000")
eq("tcp fields", {k: tcp[k] for k in ("chain", "out", "src", "dst", "proto", "spt", "dpt", "uid", "icmp_type", "icmp_code")},
   {"chain": "output", "out": "wlan0", "src": "192.168.1.5", "dst": "93.184.216.34", "proto": "TCP", "spt": 43210,
    "dpt": 443, "uid": 1000, "icmp_type": None, "icmp_code": None})
eq("tcp flags in order", tcp["flags"], ["DF", "SYN"])
eq("tcp time", tcp["time"], 1795114531.123456)
check("tcp raw", tcp["raw"].startswith("KS-DROP: IN= OUT=wlan0") and tcp["raw"].endswith("GID=1000"), tcp["raw"])
udp = P("1795114533.000000 host kernel: KS-DROP: IN= OUT=eth0 SRC=10.0.0.2 DST=8.8.8.8 LEN=60 PROTO=UDP SPT=5353 "
        "DPT=53 LEN=40 UID=0 GID=0")
eq("udp", (udp["proto"], udp["spt"], udp["dpt"], udp["uid"], udp["flags"], udp["out"]), ("UDP", 5353, 53, 0, [], "eth0"))
icmp = P("1795114534.000000 host kernel: KS-DROP: IN= OUT=wlan0 SRC=1.1.1.2 DST=1.1.1.1 LEN=84 DF PROTO=ICMP TYPE=8 "
         "CODE=0 ID=7 SEQ=1 UID=0 GID=0")
eq("icmp", (icmp["proto"], icmp["icmp_type"], icmp["icmp_code"], icmp["spt"], icmp["dpt"]), ("ICMP", 8, 0, None, None))
icmp_err = P("1795114534.5 host kernel: KS-DROP: IN= OUT=wlan0 SRC=1.1.1.2 DST=1.1.1.1 PROTO=ICMP TYPE=3 CODE=1 "
             "[SRC=1.1.1.1 DST=2.2.2.2 LEN=28 PROTO=UDP SPT=1 DPT=2 ] UID=5")
eq("icmp error: the quoted packet is ignored", (icmp_err["proto"], icmp_err["spt"], icmp_err["dpt"], icmp_err["dst"], icmp_err["uid"]),
   ("ICMP", None, None, "1.1.1.1", 5))
fwd = P("1795114535.000000 host kernel: KS-DROP-FWD: IN=docker0 OUT=wlan0 SRC=172.17.0.2 DST=93.184.216.34 PROTO=TCP "
        "SPT=50000 DPT=443 SYN URGP=0")
eq("forward chain", (fwd["chain"], fwd["uid"], fwd["out"], fwd["dpt"], fwd["flags"]), ("forward", None, "wlan0", 443, ["SYN"]))
glued = P("1795114536.000000 host kernel: KS-DROPIN= OUT=wlan0 SRC=192.168.1.5 DST=9.9.9.9 PROTO=TCP SPT=1 DPT=80 UID=0")
check("glued old prefix", glued is not None and glued["out"] == "wlan0" and glued["dst"] == "9.9.9.9" and glued["chain"] == "output")
eq("no timestamp fraction", P("1795114536 host kernel: KS-DROP: OUT=eth0 DST=1.1.1.1")["time"], 1795114536.0)
for bad in ("", "1795114537.0 host kernel: something unrelated", "garbage KS-DROP: OUT=eth0",
            "1795114537.0 host sshd[1]: KS-DROP: OUT=eth0", "1795114537.0 host kernel: not KS-DROP: OUT=eth0"):
    eq("not a drop line %r" % bad, P(bad), None)
eq("bad number fields", P("1.0 h kernel: KS-DROP: OUT=eth0 PROTO=TCP SPT=x DPT=")["dpt"], None)

# ---- fmt_dst / fmt_user / fmt_time / tilde
eq("fmt_dst tcp", vt.fmt_dst(tcp), "93.184.216.34:443")
eq("fmt_dst v6", vt.fmt_dst({"dst": "2001:db8::1", "proto": "TCP", "dpt": 443}), "[2001:db8::1]:443")
eq("fmt_dst icmp", vt.fmt_dst(icmp), "1.1.1.1 (8/0)")
eq("fmt_dst icmp no code", vt.fmt_dst({"dst": "1.1.1.1", "proto": "ICMP", "icmp_type": 3, "icmp_code": None}), "1.1.1.1 (3/?)")
eq("fmt_dst other", vt.fmt_dst({"dst": "1.1.1.1", "proto": "GRE", "dpt": None}), "1.1.1.1")
eq("fmt_dst tcp without port", vt.fmt_dst({"dst": "1.1.1.1", "proto": "TCP", "dpt": None}), "1.1.1.1")
eq("fmt_user None", vt.fmt_user(None), "")
eq("fmt_user 0", vt.fmt_user(0), "root")
eq("fmt_user unknown", vt.fmt_user(1234567891), "1234567891")
eq("fmt_time old", vt.fmt_time(100), "1970-01-01 00:01:40")
now = time.time()
eq("fmt_time today", vt.fmt_time(now), time.strftime("%H:%M:%S", time.localtime(now)))
eq("fmt_time days ago", vt.fmt_time(now - 3 * 86400), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3 * 86400)))
home = os.environ.get("HOME")
os.environ["HOME"] = "/home/someone"
eq("tilde inside", vt.tilde("/home/someone/x/y"), "~/x/y")
eq("tilde home itself", vt.tilde("/home/someone"), "~")
eq("tilde sibling prefix", vt.tilde("/home/someone2/x"), "/home/someone2/x")
eq("tilde outside", vt.tilde("/etc/x"), "/etc/x")
if home is None:
    del os.environ["HOME"]
else:
    os.environ["HOME"] = home


# ---- group_drops
def rec(t, dst="1.1.1.1", dpt=443, proto="TCP", out="wlan0", uid=0, chain="output", itype=None, icode=None):
    return {"time": t, "dst": dst, "dpt": dpt, "proto": proto, "out": out, "uid": uid, "chain": chain,
            "icmp_type": itype, "icmp_code": icode}


eq("group_drops empty", vt.group_drops([]), [])
groups = vt.group_drops([
    rec(10), rec(30), rec(20),                       # one flow, 3 packets
    rec(15, dpt=80),                                 # another port
    rec(40, chain="forward"),                        # same flow, other chain
    rec(50, proto="ICMP", dpt=None, itype=8, icode=0), rec(45, proto="ICMP", dpt=None, itype=0, icode=0),
    rec(5, uid=1000),                                # another user
])
eq("group count", len(groups), 6)
main = next(g for g in groups if g["dpt"] == 443 and g["chain"] == "output" and g["uid"] == 0 and g["proto"] == "TCP")
eq("main group", (main["count"], main["first"], main["last"]), (3, 10, 30))
eq("groups newest first", [g["last"] for g in groups], [50, 45, 40, 30, 15, 5])
eq("all packets counted", sum(g["count"] for g in groups), 8)

# ---- drop_event
D = vt.drop_event
eq("drop", D(True, False, 100, 0, False, 0), "drop")
eq("reconnect", D(False, True, 100, 0, False, 0), "reconnect")
eq("no change connected", D(True, True, 100, 0, False, 0), None)
eq("no change down", D(False, False, 100, 0, False, 0), None)
eq("first observation up", D(None, True, 100, 0, False, 0), None)
eq("first observation down", D(None, False, 100, 0, False, 0), None)
eq("state becomes unknown", D(True, None, 100, 0, False, 0), None)
eq("expected down window", D(True, False, 100, 115, False, 0), None)
eq("expected down window edge", D(True, False, 115, 115, False, 0), "drop")
eq("sleeping", D(True, False, 100, 0, True, 0), None)
eq("quiet after resume", D(True, False, 100, 0, False, 120), None)
eq("quiet after resume edge", D(True, False, 120, 0, False, 120), "drop")
eq("reconnect while sleeping", D(False, True, 100, 115, True, 120), "reconnect")

# ---- settings
reset = lambda text=None: common.reset_settings(vt, text)  # noqa: E731
reset()
eq("defaults: uuid", vt.load_settings(), None)
eq("defaults: notify", vt.load_notify_settings(), (True, True))
eq("defaults: watchdog", vt.load_watchdog_enabled(), True)
eq("defaults: backend", vt.load_backend(), "nm")
eq("defaults: iface", vt.hiddify_iface(), "tun0")

reset("[vpn]\nuuid = abc-123\nbackend = Hiddify\n[notify]\npopup = false\nsound = yes\n[watchdog]\nenabled = off\n"
      "[hiddify]\ninterface = wg0\n")
eq("load uuid", vt.load_settings(), "abc-123")
eq("load backend case-insensitive", vt.load_backend(), "hiddify")
eq("load notify", vt.load_notify_settings(), (False, True))
eq("load watchdog off", vt.load_watchdog_enabled(), False)
eq("load iface", vt.hiddify_iface(), "wg0")
reset("[vpn]\nuuid =\nbackend = bogus\n[hiddify]\ninterface =\n")
eq("empty uuid", vt.load_settings(), None)
eq("bad backend", vt.load_backend(), "nm")
eq("empty iface", vt.hiddify_iface(), "tun0")
reset("[vpn]\nbackend =   hiddify  \n")
eq("backend with spaces", vt.load_backend(), "hiddify")
with quiet() as err:
    reset("[notify]\npopup = maybe\nsound = 0\n")
    eq("bad notify bool: popup defaults to True, sound is read", vt.load_notify_settings(), (True, False))
    reset("[watchdog]\nenabled = perhaps\n")
    eq("bad watchdog bool", vt.load_watchdog_enabled(), True)
check("bad values are logged", "popup" in err.getvalue() and "enabled" in err.getvalue())
with quiet():
    reset("this is not an ini file\n[[\n\x00")
    eq("garbage: uuid", vt.load_settings(), None)
    eq("garbage: notify", vt.load_notify_settings(), (True, True))
    eq("garbage: watchdog", vt.load_watchdog_enabled(), True)
    eq("garbage: backend", vt.load_backend(), "nm")
    eq("garbage: iface", vt.hiddify_iface(), "tun0")

# save_settings: every kind of update keeps everything else
FULL = ("[vpn]\nuuid = OLD\nbackend = nm\nextra = keep\n[notify]\npopup = true\nsound = false\n[watchdog]\nenabled = false\n"
        "[hiddify]\ninterface = wg0\n[custom]\nkey = 100%\n")
base = {"vpn": {"uuid": "OLD", "backend": "nm", "extra": "keep"}, "notify": {"popup": "true", "sound": "false"},
        "watchdog": {"enabled": "false"}, "hiddify": {"interface": "wg0"}, "custom": {"key": "100%"}}


def updated(changes, **kw):
    reset(FULL)
    vt.save_settings(**kw)
    want = {s: dict(v) for s, v in base.items()}
    for (sec, key), value in changes.items():
        want[sec][key] = value
    return common.settings_snapshot(vt), want


got, want = updated({("vpn", "uuid"): "NEW"}, uuid="NEW")
eq("save uuid only", got, want)
got, want = updated({("vpn", "backend"): "hiddify"}, backend="hiddify")
eq("save backend only", got, want)
got, want = updated({("vpn", "backend"): "nm"}, backend="garbage")
eq("save backend garbage -> nm", got, want)
got, want = updated({("notify", "popup"): "false", ("notify", "sound"): "true"}, notify=(False, True))
eq("save notify only", got, want)
got, want = updated({("watchdog", "enabled"): "true"}, watchdog=True)
eq("save watchdog only", got, want)
got, want = updated({("vpn", "uuid"): "N", ("vpn", "backend"): "hiddify", ("notify", "popup"): "false",
                     ("notify", "sound"): "false", ("watchdog", "enabled"): "true"},
                    uuid="N", backend="hiddify", notify=(False, False), watchdog=True)
eq("save everything at once", got, want)
reset(FULL)
vt.save_settings()
eq("save nothing keeps all", common.settings_snapshot(vt), base)
reset(FULL)
vt.save_settings("a%b")
eq("uuid with a percent sign", vt.load_settings(), "a%b")
check("percent in another section survives", common.settings_snapshot(vt)["custom"]["key"] == "100%")

reset()
shutil.rmtree(os.path.dirname(vt.SETTINGS_PATH))   # a path inside the test's temporary directory
vt.save_settings("U", notify=(True, False), watchdog=False, backend="hiddify")
eq("created from nothing", common.settings_snapshot(vt),
   {"vpn": {"uuid": "U", "backend": "hiddify"}, "notify": {"popup": "true", "sound": "false"}, "watchdog": {"enabled": "false"}})
eq("file mode", oct(os.stat(vt.SETTINGS_PATH).st_mode & 0o777), "0o644")
eq("directory mode", oct(os.stat(os.path.dirname(vt.SETTINGS_PATH)).st_mode & 0o777), "0o700")
eq("no temp files left", [n for n in os.listdir(os.path.dirname(vt.SETTINGS_PATH)) if n != "settings.conf"], [])
with quiet():
    reset("garbage without section\n")
    vt.save_settings("Z")
eq("unparseable file is replaced", common.settings_snapshot(vt), {"vpn": {"uuid": "Z"}})
# a failed write leaves the old file and no temp file
if os.geteuid() != 0:
    reset(FULL)
    d = os.path.dirname(vt.SETTINGS_PATH)
    os.chmod(d, 0o500)
    try:
        failed = False
        try:
            vt.save_settings("X")
        except OSError:
            failed = True
    finally:
        os.chmod(d, 0o700)
    check("save_settings raises when the directory is read-only", failed)
    eq("old file kept after a failed save", common.settings_snapshot(vt), base)
    eq("no temp files after a failed save", sorted(os.listdir(d)), ["settings.conf"])

# ---- version / installed / about_paths
real_run = vt.run
git_calls = RunStub(lambda argv: (0, "abc1234 2026-01-02\n"))
vt.run = git_calls
home_dir = common.tmpdir("home")
os.environ["HOME"] = home_dir
proj = os.path.join(home_dir, "proj")
os.makedirs(os.path.join(proj, "tray"))
os.makedirs(os.path.join(proj, ".git"))
with open(os.path.join(proj, "VERSION"), "w") as f:
    f.write("9.9.9\n")
saved_paths = (vt.SCRIPT_DIR, vt.PROJECT_DIR, vt.SETTINGS_PATH)
vt.SCRIPT_DIR, vt.PROJECT_DIR = os.path.join(proj, "tray"), proj
vt.SETTINGS_PATH = os.path.join(home_dir, ".config", "vpn-killswitch", "settings.conf")
vt._app_version = None
eq("checkout: installed()", vt.installed(), False)
eq("checkout: read_version_file", vt.read_version_file(), "9.9.9")
eq("checkout: app_version with git", vt.app_version(), "9.9.9 (abc1234, 2026-01-02)")
vt.app_version()
eq("app_version is cached", len(git_calls.calls), 1)
eq("git is asked for the last commit", git_calls.calls[0][:3], ["git", "-C", proj])
eq("checkout: about_paths", vt.about_paths(), "Project: ~/proj (see README.md)\nSettings: ~/.config/vpn-killswitch/settings.conf")
vt._app_version = None
vt.run = RunStub(lambda argv: (1, ""))
eq("checkout: git failure -> plain version", vt.app_version(), "9.9.9")
vt._app_version = None
vt.run = RunStub(lambda argv: (0, "garbage\n"))
eq("checkout: odd git output -> plain version", vt.app_version(), "9.9.9")
shutil.rmtree(os.path.join(proj, ".git"))
vt._app_version = None
git_calls = RunStub(lambda argv: (0, "abc1234 2026-01-02\n"))
vt.run = git_calls
eq("no .git: plain version, git not run", (vt.app_version(), git_calls.calls), ("9.9.9", []))
os.unlink(os.path.join(proj, "VERSION"))
eq("no VERSION anywhere", vt.read_version_file(), "unknown")
with open(os.path.join(proj, "VERSION"), "w") as f:
    f.write("\n")
eq("empty VERSION", vt.read_version_file(), "unknown")
vt.run = real_run
vt.SCRIPT_DIR, vt.PROJECT_DIR, vt.SETTINGS_PATH = saved_paths
if home is None:
    del os.environ["HOME"]
else:
    os.environ["HOME"] = home

# the real checkout
real = common.load_tray(name="vpn_tray_checkout")
with open(os.path.join(common.ROOT, "VERSION")) as f:
    real_version = f.readline().strip()
eq("real checkout: PROJECT_DIR", os.path.realpath(real.PROJECT_DIR), os.path.realpath(common.ROOT))
eq("real checkout: installed()", real.installed(), False)
eq("real checkout: VERSION", real.read_version_file(), real_version)
check("VERSION looks like a version", real_version.count(".") == 2 and real_version.replace(".", "").isdigit(), real_version)

# a simulated installed layout: the tray next to a VERSION file
lib = common.tmpdir("lib")
shutil.copy(common.TRAY_PATH, os.path.join(lib, "vpn-tray.py"))
with open(os.path.join(lib, "VERSION"), "w") as f:
    f.write("1.2.3\n")
inst = common.load_tray(os.path.join(lib, "vpn-tray.py"), name="vpn_tray_installed")


def _no_git(argv):
    raise AssertionError("git must not run for an installed tray")


inst.run = RunStub(_no_git)
eq("installed: installed()", inst.installed(), True)
eq("installed: app_version", inst.app_version(), "1.2.3")
eq("installed: about_paths", inst.about_paths(),
   "Documentation: /usr/share/doc/vpn-kill-switch/README.md\nSettings: %s" % inst.tilde(inst.SETTINGS_PATH))

# ---- count_logged_drops / hiddify_available (with a stubbed run)
line = "1795114531.1 host kernel: KS-DROP: IN= OUT=wlan0 SRC=1.1.1.1 DST=2.2.2.2 PROTO=TCP SPT=1 DPT=2 UID=0\n"
vt.run = RunStub(lambda argv: (0, line * 2 + "no timestamp KS-DROP: x\n"))
eq("count_logged_drops counts parsed lines", vt.count_logged_drops(), 2)
q = vt.run.calls[0]
check("count_logged_drops query", q[:3] == ["journalctl", "-q", "-o"] and "--grep" in q and "^KS-DROP" in q
      and "-b" in q and q[-1] == "_TRANSPORT=kernel", q)
vt.run = RunStub(lambda argv: (1, ""))
eq("count_logged_drops no match", vt.count_logged_drops(), 0)
vt.run = RunStub(lambda argv: (1, line))
eq("count_logged_drops exit 1 with output", vt.count_logged_drops(), None)
vt.run = RunStub(lambda argv: (2, ""))
eq("count_logged_drops error", vt.count_logged_drops(), None)
vt.run = RunStub(lambda argv: (None, ""))
eq("count_logged_drops not run", vt.count_logged_drops(), None)
for rc, want in ((0, True), (1, False), (4, False), (None, False)):
    vt.run = RunStub(lambda argv, rc=rc: (rc, ""))
    eq("hiddify_available rc=%s" % rc, vt.hiddify_available(), want)
eq("hiddify_available asks for the unit", vt.run.calls[0], ["systemctl", "cat", "HiddifyAdminBG.service"])
vt.run = real_run

# ---- run() and run_async() with harmless real processes
eq("run ok", vt.run(["sh", "-c", "echo hi; echo err >&2"]), (0, "hi\n"))
eq("run exit status", vt.run(["sh", "-c", "exit 3"]), (3, ""))
with quiet():
    eq("run command not found", vt.run(["/nonexistent/command"]), (None, ""))
    eq("run timeout", vt.run(["sleep", "5"], timeout=0.2), (None, ""))


def run_async_result(argv, want_stdout):
    got = []
    vt.run_async(argv, lambda *a: got.append(a), want_stdout=want_stdout)
    check("run_async finished: %s" % argv[:1], common.pump_until(lambda: got, 5))
    return got[0] if got else None


eq("run_async success", run_async_result(["sh", "-c", "echo out; echo err >&2"], True), (True, "err\n", "out\n"))
eq("run_async failure", run_async_result(["sh", "-c", "echo err >&2; exit 3"], False), (False, "err\n"))
with quiet():
    res = run_async_result(["/nonexistent/command"], False)
check("run_async missing program: not ok, with the error text", res is not None and res[0] is False and res[1], res)
with quiet():
    res = run_async_result(["/nonexistent/command"], True)
check("run_async missing program with stdout", res is not None and res[0] is False and res[2] == "", res)

# ---- vpn_connections / find_active_uuid (NetworkManager fakes)
from gi.repository import NM  # noqa: E402
client = common.FakeClient(
    connections=[vpn_conn("beta", "u2"), vpn_conn("Alpha", "u1"), FakeConnection("wg", "u3", "wireguard"),
                 FakeConnection("eth", "u4", "802-3-ethernet"), FakeConnection("wifi", "u5", "802-11-wireless")],
    active=[common.FakeActive("u1", NM.ActiveConnectionState.ACTIVATED)])
t = common.make_tray(vt, client=client)
eq("vpn_connections sorted, VPN and WireGuard only", t.vpn_connections(), [("Alpha", "u1"), ("beta", "u2"), ("wg", "u3")])
t.client = None
eq("vpn_connections without NetworkManager", t.vpn_connections(), [])
t.client = client
check("find_active_uuid", t.find_active_uuid("u1") is client.active[0])
eq("find_active_uuid other", t.find_active_uuid("u2"), None)
eq("find_active_uuid empty uuid", t.find_active_uuid(""), None)
t.client = None
eq("find_active_uuid without client", t.find_active_uuid("u1"), None)

T.done()
