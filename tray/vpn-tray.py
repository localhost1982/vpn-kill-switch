#!/usr/bin/env python3
# Иконка VPN-L2TP в верхней панели GNOME: состояние VPN, аптайм, счётчик kill switch,
# число падений VPN. Протокол AppIndicator (StatusNotifierItem).
# Запуск: ./vpn-tray.py (обычно через пользовательский юнит vpn-tray.service).

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

ICON_ACTIVE = "org.gnome.Settings-network-symbolic"  # значок «Сеть» из Настроек GNOME
ICON_ACTIVATING = "network-wired-acquiring-symbolic"
ICON_OFF = "network-wired-disconnected-symbolic"

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

        self.ks_active = None   # True/False, None если `systemctl is-active` не выполнился
        self.settings_win = None  # окно настроек, пока оно открыто
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
        item_settings = Gtk.MenuItem(label="Настройки…")
        item_settings.connect("activate", self.on_settings)
        item_quit = Gtk.MenuItem(label="Выход")
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
            self.ks_active = None
            text = "Kill switch: статус неизвестен"
        elif out.strip() != "active":
            self.ks_active = False
            text = "Kill switch: ВЫКЛЮЧЕН"
        else:
            self.ks_active = True
            pkts = self.read_drop_counter()
            if pkts is None:
                text = "Kill switch: активен, нет доступа к счётчику"
            else:
                text = "Kill switch: активен, отброшено %d %s" % (pkts, plural_packets(pkts))
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


class SettingsWindow(Gtk.Window):
    """Окно настроек: пока один переключатель kill switch. Применяется по кнопке «Сохранить»."""

    def __init__(self, tray):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.tray = tray
        self.baseline = False   # положение kill switch в системе на момент открытия/последнего опроса
        self.busy = False       # команда systemctl ещё выполняется
        self.closed = False
        self.proc = None        # ссылка на Gio.Subprocess, пока он работает

        self.set_title("Настройки VPN-L2TP")
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
            label="Блокировать трафик в обход VPN. Положение сохраняется после перезагрузки.",
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

        self.btn_cancel = Gtk.Button(label="Отмена")
        self.btn_cancel.connect("clicked", self.on_cancel)
        self.btn_save = Gtk.Button(label="Сохранить")
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
        self.set_default(self.btn_save)  # Enter = «Сохранить»

        # Начальное состояние берём из актуального статуса юнита, без запуска команд.
        self.tray.refresh_killswitch()
        self.baseline = bool(self.tray.ks_active)
        self.switch.set_active(self.baseline)

        self.show_all()

    def on_destroy(self, *_):
        self.closed = True

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:  # Escape = «Отмена»
            self.on_cancel()
            return True
        return False

    def on_cancel(self, *_):
        self.destroy()

    def sync(self):
        """Вызывается из Tray.refresh_killswitch: подтянуть положение переключателя."""
        if self.closed or self.busy:
            return
        cur = bool(self.tray.ks_active)
        if self.switch.get_active() == self.baseline:  # пользователь переключатель не трогал
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
        if want == self.baseline:  # изменений нет
            self.destroy()
            return
        action = "enable" if want else "disable"  # с --now: сразу и на следующие загрузки
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
        """Команда закончилась (или не запустилась): обновить статус и показать итог."""
        # busy ещё True: sync() внутри refresh_killswitch переключатель не трогает,
        # выбор пользователя остаётся на месте до вывода результата.
        self.tray.refresh_killswitch()  # обновит строку меню и tray.ks_active
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
                "Нет прав на управление kill switch без пароля. "
                "Добавьте в /etc/sudoers.d/vpn-stats строки:\n"
                "%s ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now %s\n"
                "%s ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now %s" % (user, KILLSWITCH_UNIT, user, KILLSWITCH_UNIT)
            )
        else:
            verb = "включить" if want else "выключить"
            if not stderr:
                stderr = "юнит не перешёл в нужное состояние"
            self.show_error("Не удалось %s kill switch: %s" % (verb, stderr[:300]))


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
