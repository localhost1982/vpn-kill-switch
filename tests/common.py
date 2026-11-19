# SPDX-License-Identifier: MIT
"""Shared helpers of the test suite (see tests/README.md).

Importing this module prepares the environment: no bytecode is written, the time zone is UTC (so
formatted times are deterministic), GTK uses X11 and XDG_CONFIG_HOME points to a fresh temporary
directory, so the tray module never sees the real ~/.config/vpn-killswitch/settings.conf.
Nothing here runs a real external command: the tray's `run`, `run_async` and `subprocess` are
replaced by the stubs below.
"""

import sys

sys.dont_write_bytecode = True

import atexit  # noqa: E402
import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
# VPNKS_TRAY_PATH: run the suite against another copy of the tray (e.g. a deliberately broken one, to
# check that the tests can fail). Not meant for normal use.
TRAY_PATH = os.environ.get("VPNKS_TRAY_PATH") or os.path.join(ROOT, "tray", "vpn-tray.py")

os.environ["GDK_BACKEND"] = "x11"
os.environ["TZ"] = "UTC"
time.tzset()

TMP = tempfile.mkdtemp(prefix="vpnks-test-")
CONFIG_HOME = os.path.join(TMP, "config")
os.makedirs(CONFIG_HOME)
os.environ["XDG_CONFIG_HOME"] = CONFIG_HOME


def _cleanup():
    real = os.path.realpath(TMP)
    base = os.path.realpath(tempfile.gettempdir())
    if real.startswith(base + os.sep) and os.path.basename(real).startswith("vpnks-test-"):
        shutil.rmtree(real, ignore_errors=True)


atexit.register(_cleanup)


def tmpdir(name):
    """A fresh empty directory inside this process' temporary directory."""
    path = tempfile.mkdtemp(prefix=name + "-", dir=TMP)
    return path


def fixture(name):
    """Lines of a file of tests/fixtures (comment lines starting with # included)."""
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read().splitlines()


def journal_lines(name):
    """Journal lines of a fixture (the # comment lines dropped)."""
    return [line for line in fixture(name) if line.strip() and not line.startswith("#")]


# --- results ------------------------------------------------------------------

class Results:
    def __init__(self, name):
        self.name = name
        self.failures = []
        self.count = 0

    def check(self, name, cond, extra=""):
        self.count += 1
        if not cond:
            self.failures.append(name)
            print("FAIL: %s %s" % (name, extra), flush=True)
        return bool(cond)

    def eq(self, name, got, want):
        return self.check(name, got == want, "\n    got:  %r\n    want: %r" % (got, want))

    def done(self):
        if self.failures:
            print("FAILURES (%d of %d checks): %s" % (len(self.failures), self.count, ", ".join(self.failures)))
            sys.exit(1)
        print("ALL OK (%d checks)" % self.count)
        sys.exit(0)


def require_display():
    """Exit with the SKIP status (77) when GTK cannot open a display."""
    if not os.environ.get("DISPLAY"):
        print("SKIP (no DISPLAY)")
        sys.exit(77)
    import gi
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk
    if Gdk.Display.get_default() is None:
        print("SKIP (cannot open the display)")
        sys.exit(77)


@contextlib.contextmanager
def quiet():
    """Capture what the tray logs to stderr (its `log()` function)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


# --- the tray module -----------------------------------------------------------

def load_tray(path=TRAY_PATH, name="vpn_tray"):
    """Import tray/vpn-tray.py (the hyphen in the name rules out a normal import)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if os.path.realpath(path) == os.path.realpath(TRAY_PATH) or path.startswith(TMP):
        assert os.path.realpath(mod.SETTINGS_PATH).startswith(os.path.realpath(CONFIG_HOME)), \
            "the tray would use a settings file outside the test directory: %s" % mod.SETTINGS_PATH
    return mod


def reset_settings(vt, text=None, uuid=None, backend=None, notify=None, watchdog=None):
    """Replace the settings file: with `text` verbatim, or by save_settings() with the given values;
    with neither, remove it."""
    path = vt.SETTINGS_PATH
    assert os.path.realpath(path).startswith(os.path.realpath(CONFIG_HOME)), path
    if os.path.exists(path):
        os.unlink(path)
    if text is not None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    elif any(v is not None for v in (uuid, backend, notify, watchdog)):
        vt.save_settings(uuid, notify=notify, watchdog=watchdog, backend=backend)


def read_settings_text(vt):
    try:
        with open(vt.SETTINGS_PATH) as f:
            return f.read()
    except OSError:
        return None


def settings_snapshot(vt):
    """{section: {key: value}} of the settings file ({} when missing)."""
    import configparser
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(vt.SETTINGS_PATH)
    return {s: dict(cp[s]) for s in cp.sections()}


# --- clock -----------------------------------------------------------------------

class Clock:
    """Stands in for the `time` module of the tray (`vt.time = Clock()`): time() is controllable,
    everything else (strftime, localtime, ...) is the real thing."""

    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


# --- GTK loop ----------------------------------------------------------------------

def pump(rounds=20):
    """Run pending GLib main loop work (idle callbacks, process results)."""
    from gi.repository import GLib
    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        while ctx.pending():
            ctx.iteration(False)


def pump_until(cond, timeout=5.0):
    """Iterate the main loop until cond() is true; False on timeout."""
    from gi.repository import GLib
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            return False
        ctx.iteration(False)
        time.sleep(0.005)
    return True


# --- fake NetworkManager objects ---------------------------------------------------------

class _Signals:
    """connect / disconnect / emit for the fakes that the tray subscribes to ("notify::state")."""

    def _init_signals(self):
        self.handlers = {}
        self._next = 1

    def connect(self, sig, cb):
        hid = self._next
        self._next += 1
        self.handlers[hid] = (sig, cb)
        return hid

    def disconnect(self, hid):
        self.handlers.pop(hid, None)

    def emit_state(self, state):
        self.state = state
        for sig, cb in list(self.handlers.values()):
            if sig == "notify::state":
                cb()

    def handler_count(self):
        return len(self.handlers)


class FakeVpnSetting:
    def __init__(self, **items):
        self.items = items

    def get_data_item(self, key):
        return self.items.get(key)


class FakePeer:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def get_endpoint(self):
        return self.endpoint


class FakeWireGuard:
    def __init__(self, endpoints):
        self.peers = [FakePeer(e) for e in endpoints]

    def get_peers_len(self):
        return len(self.peers)

    def get_peer(self, i):
        return self.peers[i]


class FakeConnSetting:
    def __init__(self, timestamp):
        self.timestamp = timestamp

    def get_timestamp(self):
        if isinstance(self.timestamp, Exception):
            raise self.timestamp
        return self.timestamp


class FakeConnection:
    """NM.RemoteConnection: id, uuid, type, the VPN data items or the WireGuard peers."""

    def __init__(self, id, uuid, ctype="vpn", vpn=None, wg=None, timestamp=None, broken=False):
        self.id, self.uuid, self.ctype = id, uuid, ctype
        self.vpn, self.wg, self.timestamp, self.broken = vpn, wg, timestamp, broken

    def get_id(self):
        return self.id

    def get_uuid(self):
        return self.uuid

    def get_connection_type(self):
        if self.broken:
            raise RuntimeError("broken connection")
        return self.ctype

    def get_setting_vpn(self):
        return self.vpn

    def get_setting(self, _klass):
        return self.wg

    def get_setting_connection(self):
        return FakeConnSetting(self.timestamp)


def vpn_conn(id, uuid, **data):
    """A "vpn" connection with the given data items (gateway=..., remote=..., address=...)."""
    return FakeConnection(id, uuid, "vpn", vpn=FakeVpnSetting(**data))


class FakeActive(_Signals):
    """NM.ActiveConnection."""

    def __init__(self, uuid, state):
        self.uuid, self.state = uuid, state
        self._init_signals()

    def get_uuid(self):
        return self.uuid

    def get_state(self):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


class FakeDevice(_Signals):
    """NM.Device."""

    def __init__(self, state):
        self.state = state
        self._init_signals()

    def get_state(self):
        return self.state


class FakeClient:
    """NM.Client."""

    def __init__(self, connections=(), active=(), devices=None):
        self.connections = list(connections)
        self.active = list(active)
        self.devices = dict(devices or {})   # iface -> FakeDevice

    def get_connections(self):
        return list(self.connections)

    def get_connection_by_uuid(self, uuid):
        return next((c for c in self.connections if c.uuid == uuid), None)

    def get_active_connections(self):
        return list(self.active)

    def get_device_by_iface(self, iface):
        return self.devices.get(iface)


# --- fake widgets of the tray menu -----------------------------------------------------------

class FakeItem:
    """Gtk.MenuItem: label and sensitivity."""

    def __init__(self, label=""):
        self.label = label
        self.sens = True

    def get_label(self):
        return self.label

    def set_label(self, label):
        self.label = label

    def set_sensitive(self, value):
        self.sens = value


class FakeIndicator:
    def __init__(self):
        self.icons = []
        self.titles = []

    def set_icon_full(self, icon, title):
        self.icons.append((icon, title))

    def set_title(self, title):
        self.titles.append(title)


def make_tray(vt, backend="nm", uuid="OLD", client=None):
    """A Tray without the constructor (which would talk to NetworkManager, D-Bus and the panel):
    every attribute the methods use, with fake menu items and indicator."""
    # Safety net: whatever a test does, no real popup, sound or process can come out of this tray.
    if not isinstance(vt.subprocess, FakeSubprocess):
        vt.subprocess = FakeSubprocess()
    if not isinstance(vt.shutil, FakeShutil):
        vt.shutil = FakeShutil(("canberra-gtk-play",))
    if not isinstance(vt.Notify, FakeNotify):
        vt.Notify = FakeNotify()
    t = vt.Tray.__new__(vt.Tray)
    t.started = time.time()
    t.client = client
    t.vpn_uuid = uuid
    t.backend = backend
    t.notify_popup, t.notify_sound = True, True
    t.vpn_prev = None
    t.expect_down_until = 0.0
    t.sleeping = False
    t.quiet_until = 0.0
    t.drop_at = None
    t.drop_popup = False
    t.last_sound_at = 0.0
    t.sound_warned = False
    t.notification = None
    t.notify_ok = False   # a test that wants popups sets it (the Notify module is a fake anyway)
    t.sys_bus = None
    t.vpn_conn = None
    t.ac = None
    t.ac_handler = 0
    t.hdev = None
    t.hdev_handler = 0
    t.procs = []
    t.last_icon = None
    t.last_title = None
    t.ks_active = True
    t.settings_win = None
    t.about_win = None
    t.drops_win = None
    t.vpn_drops_win = None
    t.ks_text = "Kill switch: checking…"
    t.drops_text = "VPN drops since boot: checking…"
    t.indicator = FakeIndicator()
    t.item_vpn = FakeItem("VPN: …")
    t.item_uptime = FakeItem("Uptime: …")
    t.item_ks = FakeItem(t.ks_text)
    t.item_drops = FakeItem(t.drops_text)
    t.item_reconnect = FakeItem("Reconnect VPN")
    return t


# --- subprocess stubs ---------------------------------------------------------------------------

def _result(res):
    """Normalise a handler result to (ok, stderr, stdout)."""
    if res is None:
        return True, "", ""
    if isinstance(res, bool):
        return res, "", ""
    res = tuple(res)
    return res[0], res[1] if len(res) > 1 else "", res[2] if len(res) > 2 else ""


class AsyncStub:
    """Replacement for run_async(): records the argv, answers at once (the callback runs before
    run_async returns). handler(argv) -> (ok, stderr, stdout) | (ok, stderr) | ok | None (= success).
    `log` (optional list) also receives ("async", argv) so the order against Popen is visible."""

    def __init__(self, handler=None, log=None):
        self.calls = []
        self.handler = handler
        self.log = log

    def __call__(self, argv, callback, want_stdout=False):
        argv = list(argv)
        self.calls.append(argv)
        if self.log is not None:
            self.log.append(("async", argv))
        res = self.handler(argv) if self.handler else None
        ok, stderr, stdout = _result(res)
        if want_stdout:
            callback(ok, stderr, stdout)
        else:
            callback(ok, stderr)
        return None


class RunStub:
    """Replacement for run(): handler(argv) -> (rc, stdout); records the calls."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, cmd, timeout=5):
        self.calls.append(list(cmd))
        return self.handler(list(cmd))


class FakeSubprocess:
    """Replacement for the `subprocess` module of the tray: Popen only records (and can fail);
    run() must never be reached (the tray's own run() is stubbed)."""

    DEVNULL = -3
    PIPE = -1

    def __init__(self, log=None):
        self.popen_calls = []
        self.popen_error = None
        outer = self

        class Popen:
            def __init__(self, argv, **kwargs):
                if outer.popen_error is not None:
                    raise outer.popen_error
                outer.popen_calls.append(list(argv))
                if log is not None:
                    log.append(("popen", list(argv)))

            def poll(self):
                return 0

        self.Popen = Popen

    def run(self, *a, **k):
        raise AssertionError("the test would start a real process: %r" % (a,))


class FakeShutil:
    """Replacement for the `shutil` module of the tray: which() knows only the given programs."""

    def __init__(self, present=()):
        self.present = set(present)

    def which(self, name):
        return "/usr/bin/" + name if name in self.present else None


class FakeNotification:
    def __init__(self, owner, summary, body, icon):
        self.owner, self.summary, self.body, self.icon = owner, summary, body, icon
        self.updates = 0
        self.urgency = None

    def set_urgency(self, urgency):
        self.urgency = urgency

    def update(self, summary, body, icon):
        self.summary, self.body, self.icon = summary, body, icon
        self.updates += 1

    def show(self):
        self.owner.shown.append((self.summary, self.body, self.icon, self))


class FakeNotify:
    """Replacement for the Notify module of the tray (libnotify)."""

    class Urgency:
        LOW, NORMAL, CRITICAL = 0, 1, 2

    def __init__(self):
        self.objects = []     # every Notification.new()
        self.shown = []       # (summary, body, icon, object) per show()
        self.fail_new = False
        owner = self

        class Notification:
            @staticmethod
            def new(summary, body, icon):
                if owner.fail_new:
                    raise RuntimeError("no notification daemon")
                obj = FakeNotification(owner, summary, body, icon)
                owner.objects.append(obj)
                return obj

        self.Notification = Notification

    def init(self, _name):
        return True
