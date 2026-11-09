#!/usr/bin/env python3
# Иконка VPN-L2TP в верхней панели GNOME: состояние VPN, аптайм, счётчик kill switch,
# число падений VPN. Протокол AppIndicator (StatusNotifierItem).
# Запуск: ./vpn-tray.py (обычно через пользовательский юнит vpn-tray.service).

import fcntl
import os
import re
import signal
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("NM", "1.0")
from gi.repository import GLib, Gtk, NM  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402

VPN_NAME = "VPN-L2TP"
KILLSWITCH_UNIT = "vpn-killswitch.service"

ICON_ACTIVE = "network-vpn-symbolic"
ICON_ACTIVATING = "network-vpn-acquiring-symbolic"
ICON_OFF = "network-vpn-disconnected-symbolic"

# Шаблоны строк pppd. `journalctl -o cat` печатает только текст сообщения,
# без префикса «pppd[PID]:», процесс отбирается через _COMM=pppd.
RE_LCP = re.compile(r"^No response to [0-9]+ echo-requests", re.M)
RE_TERM = re.compile(r"^Terminating on signal", re.M)
RE_CONN = re.compile(r"^Connection terminated\.", re.M)

FAST_SECONDS = 5        # состояние VPN, аптайм (запасной опрос)
KILLSWITCH_SECONDS = 60  # kill switch и счётчик пакетов (каждый опрос через sudo пишет в журнал)
JOURNAL_SECONDS = 60    # падения VPN по журналу


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fmt_duration(s):
    """Секунды -> «12 с» / «3 мин 05 с» / «1 ч 02 мин» / «1 д 03 ч»."""
    s = int(s)
    if s < 60:
        return "%d с" % s
    if s < 3600:
        return "%d мин %02d с" % (s // 60, s % 60)
    if s < 86400:
        return "%d ч %02d мин" % (s // 3600, (s % 3600) // 60)
    return "%d д %02d ч" % (s // 86400, (s % 86400) // 3600)


def plural_packets(n):
    """1 пакет, 2-4 пакета, 5+ пакетов."""
    if n % 100 in (11, 12, 13, 14):
        return "пакетов"
    if n % 10 == 1:
        return "пакет"
    if n % 10 in (2, 3, 4):
        return "пакета"
    return "пакетов"


def run(cmd, timeout=5):
    """Запуск внешней команды. Возвращает (код, stdout) или (None, "") при ошибке."""
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
    except Exception as e:  # таймаут, нет команды и т. п.
        log("%s: %s" % (" ".join(cmd), e))
        return None, ""


class Tray:
    def __init__(self):
        self.client = None
        try:
            self.client = NM.Client.new(None)
        except Exception as e:
            log("NetworkManager недоступен: %s" % e)

        self.ac = None          # активное соединение VPN-L2TP (если есть)
        self.ac_handler = 0
        self.procs = []         # фоновые nmcli, чтобы не плодить зомби
        self.last_icon = None
        self.last_title = None

        self.ks_text = "Kill switch: проверяю…"
        self.drops_text = "Падений VPN с загрузки: проверяю…"

        self.indicator = AppIndicator.Indicator.new(
            "vpn-l2tp-tray",
            ICON_OFF,
            AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

        # Информационные пункты остаются «чувствительными» (иначе серые) и ничего не делают.
        self.item_vpn = Gtk.MenuItem(label="VPN-L2TP: …")
        self.item_uptime = Gtk.MenuItem(label="Ноутбук работает: …")
        self.item_ks = Gtk.MenuItem(label=self.ks_text)
        self.item_drops = Gtk.MenuItem(label=self.drops_text)
        item_reconnect = Gtk.MenuItem(label="Переподключить VPN")
        item_reconnect.connect("activate", self.on_reconnect)
        item_quit = Gtk.MenuItem(label="Выход")
        item_quit.connect("activate", lambda *_: Gtk.main_quit())

        self.menu = Gtk.Menu()
        for item in (self.item_vpn, self.item_uptime, self.item_ks, self.item_drops):
            self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(item_reconnect)
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

    # --- таймеры и события меню ---------------------------------------------

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

    # --- VPN ----------------------------------------------------------------

    def find_active(self):
        if self.client is None:
            return None
        for ac in self.client.get_active_connections():
            if ac.get_id() == VPN_NAME:
                return ac
        return None

    def watch_active(self, ac):
        """Подписка на смену состояния найденного активного соединения."""
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
        """Эпоха последней успешной активации профиля или None."""
        try:
            conn = self.client.get_connection_by_id(VPN_NAME)
            ts = conn.get_setting_connection().get_timestamp()
            return int(ts) if ts else None
        except Exception as e:
            log("connection.timestamp: %s" % e)
            return None

    def refresh_vpn(self):
        if self.client is None:
            self.set_state(ICON_OFF, "VPN-L2TP: состояние неизвестно", "VPN: нет данных")
            return
        try:
            ac = self.find_active()
            self.watch_active(ac)
            state = ac.get_state() if ac is not None else None
        except Exception as e:
            log("состояние VPN: %s" % e)
            self.set_state(ICON_OFF, "VPN-L2TP: состояние неизвестно", "VPN: нет данных")
            return

        if state == NM.ActiveConnectionState.ACTIVATED:
            text = "VPN-L2TP: подключён"
            ts = self.activation_time()
            if ts is not None:
                text += " " + fmt_duration(max(0, time.time() - ts))
            self.set_state(ICON_ACTIVE, text, "VPN подключён")
        elif state == NM.ActiveConnectionState.ACTIVATING:
            self.set_state(ICON_ACTIVATING, "VPN-L2TP: подключается…", "VPN подключается")
        else:
            self.set_state(ICON_OFF, "VPN-L2TP: не подключён", "VPN не подключён")

    def set_state(self, icon, label, title):
        if self.item_vpn.get_label() != label:
            self.item_vpn.set_label(label)
        if icon != self.last_icon:
            self.indicator.set_icon_full(icon, title)
            self.last_icon = icon
        if title != self.last_title:
            self.indicator.set_title(title)
            self.last_title = title

    # --- аптайм -------------------------------------------------------------

    def refresh_uptime(self):
        try:
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            text = "Ноутбук работает: " + fmt_duration(up)
        except Exception as e:
            log("/proc/uptime: %s" % e)
            text = "Ноутбук работает: нет данных"
        if self.item_uptime.get_label() != text:
            self.item_uptime.set_label(text)

    # --- kill switch --------------------------------------------------------

    def refresh_killswitch(self):
        rc, out = run(["systemctl", "is-active", KILLSWITCH_UNIT])
        if rc is None:
            text = "Kill switch: статус неизвестен"
        elif out.strip() != "active":
            text = "Kill switch: ВЫКЛЮЧЕН"
        else:
            pkts = self.read_drop_counter()
            if pkts is None:
                text = "Kill switch: активен, нет доступа к счётчику"
            else:
                text = "Kill switch: активен, отброшено %d %s" % (pkts, plural_packets(pkts))
        if self.item_ks.get_label() != text:
            self.item_ks.set_label(text)

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

    # --- падения VPN --------------------------------------------------------

    def refresh_drops(self):
        rc, out = run(["journalctl", "-b", "-q", "-o", "cat", "_COMM=pppd"])
        if rc != 0:
            text = "Падений VPN с загрузки: нет данных"
        else:
            lcp = len(RE_LCP.findall(out))
            sig = len(RE_TERM.findall(out))
            conn = len(RE_CONN.findall(out))
            total = max(conn, lcp + sig)
            text = "Падений VPN с загрузки: %d (сервер молчал: %d)" % (total, lcp)
        if self.item_drops.get_label() != text:
            self.item_drops.set_label(text)


def acquire_lock():
    """Второй экземпляр не запускаем: иначе в панели будет две иконки."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    f = open(os.path.join(runtime, "vpn-tray.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("vpn-tray уже запущен, выхожу.")
        sys.exit(0)
    return f


def main():
    lock = acquire_lock()  # держим ссылку до конца работы
    tray = Tray()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: Gtk.main_quit() or GLib.SOURCE_REMOVE)
    Gtk.main()
    del tray, lock


if __name__ == "__main__":
    main()
