"""Spoofr, native PySide6 application shell.

The window is the map (MapScreen) with a Settings & Help sheet and the iPhone
QR hand-off on top. This file owns the device lifecycle — discovery, the
Trust dance, auto-connecting to phones you've used before, reconnecting — and
the window-level rules: keyboard shortcuts, following the system appearance,
the menu-bar item, and what closing or quitting does while a route runs.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QLineEdit, QMenuBar, QMessageBox, QVBoxLayout, QWidget,
)

from . import store, theme
from .bridge import DeviceBridge
from .mapview import MapScreen
from .portable_view import PortableController, PortableView
from .sidebar import Sidebar
from .wizard import DevModeWizard

_ARROWS = {Qt.Key.Key_Up: "Up", Qt.Key.Key_Down: "Down",
           Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right"}
BATTERY_WARN = 20          # percent, on battery, while a route runs


def battery() -> tuple[int, bool] | None:
    """(percent, on_battery) from pmset, or None on a desktop Mac / on failure."""
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                             timeout=3).stdout
    except Exception:
        return None
    m = re.search(r"(\d+)%", out)
    if not m:
        return None
    return int(m.group(1)), "Battery Power" in out


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle("Spoofr")
        self.resize(1200, 820)
        self.setMinimumSize(800, 600)
        self._wizard = None
        self._quitting = False
        self._suppressed: set[str] = set()     # serials not to auto-connect (manual disconnect)
        self._want_serial: str | None = None
        self._last_cards: list[dict] = []

        self.bridge = DeviceBridge()
        self.settings = store.load()
        self._migrate()
        # the bridge reattaches known phones over Wi-Fi by name, without usbmux
        self.bridge.known_devices = self.settings.setdefault("devices", {})

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.mapscreen = MapScreen(self.bridge, self.settings)
        root.addWidget(self.mapscreen)

        self.sidebar = Sidebar(self.settings, self)
        self.sidebar.move(-self.sidebar.width(), 0)
        self.sidebar.hide()

        self.portable = PortableController()
        self.portable_view = PortableView(self)
        self.portable_view.hide()

        # ---- wiring ----
        b = self.bridge
        b.phones.connect(self._on_phones)
        b.status.connect(self._on_status)
        b.hint.connect(self._on_hint)
        b.connected.connect(self._on_connected)
        b.failed.connect(self._on_failed)
        b.devModeRequired.connect(self._on_dev_mode_required)
        b.deviceLost.connect(self._on_device_lost)
        b.reconnecting.connect(lambda _n: self.mapscreen.on_reconnecting())
        b.wirelessResult.connect(self._on_wireless_result)
        b.heartbeat_source = self.mapscreen.heartbeat_point
        s = self.mapscreen
        s.openSettings.connect(self.sidebar.toggle)
        s.openPortable.connect(self._enter_portable)
        s.goWireless.connect(self._go_wireless)
        s.connectAsked.connect(self._connect)
        s.disconnectAsked.connect(self._disconnect)
        s.sessionChanged.connect(self._refresh_macui)
        s.progress.connect(self._on_progress)
        sb = self.sidebar
        sb.goWireless.connect(self._go_wireless)
        sb.openPortable.connect(lambda: (sb.close_menu(), self._enter_portable()))
        sb.brightnessChanged.connect(s.map.set_brightness)
        sb.pulseToggled.connect(s.set_pulsing)
        sb.jitterToggled.connect(s.set_jitter)
        sb.walkPadToggled.connect(s.set_walk_pad)
        sb.unitsChanged.connect(lambda _u: s._refresh_page())
        self.portable.qrReady.connect(self._on_qr_ready)
        self.portable.statusUpdate.connect(self._on_portable_status)
        self.portable.failed.connect(lambda m: self.portable_view.set_status(m, theme.RED))
        self.portable_view.copy_btn.clicked.connect(self._copy_portable_link)
        self.portable_view.back_btn.clicked.connect(self._leave_portable)

        # preferences
        s.map.set_brightness(self.settings.get("brightness", "Normal"))
        s.set_pulsing(self.settings.get("pulse", True))

        self._shortcuts()
        self._menubar()

        # follow the system appearance, live
        app = QApplication.instance()
        try:
            app.styleHints().colorSchemeChanged.connect(self._on_scheme)
        except Exception:
            pass

        # battery watch while a route runs
        self._batt = QTimer(self)
        self._batt.setInterval(60_000)
        self._batt.timeout.connect(self._check_battery)
        self._batt.start()

        # menu-bar item + panic hotkey (native, best-effort)
        self._macui = None
        QTimer.singleShot(300, self._install_macui)

        # a route left behind by the last run (quit, crash, restart)
        last = self.settings.get("last_device")
        if last:
            d = store.load_session(last)
            if d:
                QTimer.singleShot(0, lambda: self.mapscreen.restore_session(last, d))
        # where are we (this Mac, so the phone's real location)? which phones are here?
        self.mapscreen.locator.request()
        self.bridge.start_visibility()
        # clicking the Dock icon while the window is hidden brings it back (a
        # signal, not an app-wide event filter: that would run Python for every
        # mouse move while panning)
        app.applicationStateChanged.connect(self._on_app_state)

    def _migrate(self):
        """Settings from 1.0: a single active spoof, recents without names."""
        s = self.settings
        s.setdefault("auto_connect", True)
        old = s.pop("active_spoof", None)
        if old and not s.get("spoofs"):
            s["pending_spoof"] = old          # claimed by the first phone that connects

    # ------------------------------------------------------------ devices

    def _on_phones(self, cards: list):
        self._last_cards = cards
        serials = {c["serial"] for c in cards}
        self._suppressed &= serials           # an unplugged phone is forgiven
        sc = self.mapscreen
        sc.welcome.set_phones(cards)
        if sc.conn_state in ("connected", "connecting", "reconnecting"):
            return
        ready = [c for c in cards if c.get("state") == "ready"]
        sc.set_conn_state("ready" if cards else "disconnected",
                          cards[0]["link"] if cards else "")
        if self.portable_view.isVisible():
            return
        if not self.settings.get("auto_connect", True):
            return
        known = self.settings.get("devices", {})
        want_udid = sc.session_udid if sc.session is not None else None
        for c in ready:
            udid = c.get("udid", "")
            if c["serial"] not in self._suppressed and (udid in known or udid == want_udid):
                self._connect(c["serial"])
                return

    def _connect(self, serial: str = ""):
        if self.bridge.is_connected() or self.bridge._connecting:
            return
        self._suppressed.discard(serial)
        self._want_serial = serial or None
        self.mapscreen.welcome.set_connecting(serial or None)
        self.mapscreen.set_conn_state("connecting")
        self.bridge.connect(serial or None)

    def _on_connected(self, device):
        if self.portable_view.isVisible():
            # a connect that was already running when the phone took over
            self.bridge.drop_device()
            return
        udid = getattr(device, "udid", "") or getattr(device, "serial", "")
        devs = self.settings.setdefault("devices", {})
        prev = devs.get(udid) or {}
        up = getattr(device, "uptime", None)
        if up is not None:
            # the phone's uptime only goes backwards when it restarts. (Needing to
            # mount the developer image again is not a restart signal: iOS can
            # unmount it on its own, and treating that as a restart is what made a
            # plain replug drop the route.)
            device.restarted = prev.get("uptime") is not None and up + 1 < prev["uptime"]
        devs[udid] = {"name": device.name, "model": getattr(device, "model", ""),
                      "ios": device.ios, "last": time.time(), "uptime": up}
        self.settings["last_device"] = udid
        self.settings["wireless_on"] = bool(getattr(device, "wireless_on", False))
        pending = self.settings.pop("pending_spoof", None)
        if pending and udid:
            self.settings.setdefault("spoofs", {}).setdefault(udid, pending)
        store.save(self.settings)
        self.sidebar.refresh_wireless(self.settings["wireless_on"])
        self.mapscreen.welcome.set_connecting(None)
        self.mapscreen.on_connected(device)
        self._refresh_macui()

    def _on_failed(self, msg: str):
        self.mapscreen.set_conn_state("ready" if self._last_cards else "disconnected")
        self.mapscreen.welcome.set_error(self._want_serial, msg)
        if not self.mapscreen.welcome.isVisible():
            self.mapscreen.show_toast(msg)

    def _on_status(self, text: str, color: str):
        """Progress while connecting: shown on the pill (the card says Connecting…)."""
        if self.mapscreen.conn_state == "connecting":
            self.mapscreen.pill.set(text, color)

    def _on_hint(self, text: str):
        if text.startswith(("Lost the iPhone", "Restore failed", "Connect to")):
            self.mapscreen.show_toast(text)

    def _on_device_lost(self):
        self.mapscreen.on_device_lost()
        self._refresh_macui()

    def _disconnect(self, restore_first: bool):
        dev = self.bridge.device
        if dev is not None and getattr(dev, "serial", ""):
            self._suppressed.add(dev.serial)     # don't bounce straight back in
        if restore_first:
            def after():
                try:
                    self.bridge.restored.disconnect(after)
                except (RuntimeError, TypeError):
                    pass
                self._finish_disconnect()
            self.bridge.restored.connect(after)
            self.mapscreen.stop_spoofing()
            QTimer.singleShot(10_000, lambda: self.bridge.is_connected() and self._finish_disconnect())
        else:
            self._finish_disconnect()

    def _finish_disconnect(self):
        self.bridge.disconnect()
        self.mapscreen.on_disconnected()
        self._refresh_macui()

    def _on_dev_mode_required(self):
        self.mapscreen.set_conn_state("ready" if self._last_cards else "disconnected")
        self.mapscreen.welcome.set_connecting(None)
        if self._wizard is not None and self._wizard.isVisible():
            self._wizard.raise_()
            return
        self._wizard = DevModeWizard(self)
        self._wizard.accepted.connect(lambda: self._connect(self._want_serial or ""))
        self._wizard.show()

    # ------------------------------------------------------------ wireless

    def _go_wireless(self):
        self.sidebar.set_wireless_busy(True)
        self.mapscreen.show_toast("Setting up Wi-Fi control…")
        self.bridge.go_wireless()

    def _on_wireless_result(self, ok: bool, msg: str):
        self.sidebar.set_wireless_busy(False)
        self.sidebar.set_wireless_status(("✓  " if ok else "⚠  ") + msg, "good" if ok else "bad")
        self.mapscreen.show_toast("Wi-Fi control is on." if ok else msg)
        if ok:
            self.settings["wireless_on"] = True
            store.save(self.settings)
            if self.mapscreen.device:
                self.mapscreen.device["wireless_on"] = True
                self.mapscreen._refresh_page()

    # ------------------------------------------------------------ iPhone (QR) mode

    def _enter_portable(self):
        sc = self.mapscreen
        if sc.session is not None:
            r = QMessageBox.question(self, "Control from your iPhone",
                                     "Your route pauses while the phone is in control. Continue?")
            if r != QMessageBox.StandardButton.Yes:
                return
            sc._stash_session(pause=True)
        sc._walk_release()
        self.bridge.drop_device()             # hand the device to the phone; keep the tunnel
        sc.conn_state = "disconnected"
        sc.set_portable(True)
        self.portable_view.reset()
        self.portable_view.setGeometry(self.rect())
        self.portable_view.show(); self.portable_view.raise_()
        self.portable.start()

    def _leave_portable(self):
        self.portable.stop()
        self.portable_view.hide()
        self.mapscreen.set_portable(False)
        self._suppressed.clear()              # reconnect on the next discovery tick

    def _on_qr_ready(self, url: str):
        self.portable_view.show_qr(self.portable.qr_png(), url)
        self.portable_view.set_status("Waiting for your phone, scan the code", theme.AMBER)

    def _on_portable_status(self, st):
        if not self.portable_view.isVisible():
            return
        if st and st.get("connected"):
            nm = st.get("name") or "iPhone"
            self.portable_view.set_status(f"{nm} connected, controlling from your phone", theme.GREEN)
        else:
            self.portable_view.set_status("Waiting for your phone, scan the code", theme.AMBER)

    def _copy_portable_link(self):
        url = self.portable.url()
        if url:
            QApplication.clipboard().setText(url)
            self.portable_view.copy_btn.setText("Copied!")
            QTimer.singleShot(1200, lambda: self.portable_view.copy_btn.setText("Copy link"))

    # ------------------------------------------------------------ keyboard

    def _shortcuts(self):
        sc = self.mapscreen

        def bind(seq, fn, when_typing=False):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.ShortcutContext.WindowShortcut)
            s.activated.connect(lambda: fn() if when_typing or not self._typing() else None)
            return s

        bind("Ctrl+F", sc.focus_search, when_typing=True)          # ⌘F on macOS
        bind("Ctrl+.", sc.stop_spoofing, when_typing=True)         # ⌘.
        bind("Space", sc.toggle_pause)
        bind("T", sc.teleport_pin)
        bind("R", sc.route_pin)

    def _typing(self) -> bool:
        return isinstance(QApplication.focusWidget(), QLineEdit) or \
            QApplication.focusWidget().__class__.__name__ in ("QSpinBox", "QDoubleSpinBox")

    def _menubar(self):
        mb = QMenuBar(self)          # on macOS this becomes the global menu bar
        app_menu = mb.addMenu("Spoofr")
        about = QAction("About Spoofr", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(lambda: self.sidebar.open_menu())
        prefs = QAction("Settings…", self)
        prefs.setMenuRole(QAction.MenuRole.PreferencesRole)
        prefs.setShortcut(QKeySequence("Ctrl+,"))
        prefs.triggered.connect(self.sidebar.open_menu)
        quit_ = QAction("Quit Spoofr", self)
        quit_.setMenuRole(QAction.MenuRole.QuitRole)
        quit_.setShortcut(QKeySequence.StandardKey.Quit)
        quit_.triggered.connect(self.quit_app)
        for a in (about, prefs, quit_):
            app_menu.addAction(a)
        # the keys themselves are QShortcuts (see _shortcuts): giving these the same
        # sequences would make Qt treat both as ambiguous and fire neither
        route = mb.addMenu("Route")
        for text, fn in (("Teleport to Pin  (T)", self.mapscreen.teleport_pin),
                         ("Route to Pin  (R)", self.mapscreen.route_pin),
                         ("Pause or Resume  (Space)", self.mapscreen.toggle_pause),
                         ("Stop Spoofing  (⌘.)", self.mapscreen.stop_spoofing)):
            a = QAction(text, self)
            a.triggered.connect(fn)
            route.addAction(a)
        self._mb = mb

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            if self.sidebar.is_open():
                self.sidebar.close_menu()
            else:
                self.mapscreen.escape()
            e.accept(); return
        if not e.isAutoRepeat():
            k = _ARROWS.get(e.key())
            if k and not self._typing():
                self.mapscreen.key_walk(k, True)
                e.accept(); return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if not e.isAutoRepeat():
            k = _ARROWS.get(e.key())
            if k:
                self.mapscreen.key_walk(k, False)
                e.accept(); return
        super().keyReleaseEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "sidebar"):
            self.sidebar.fit_height(self.height())
            self.sidebar.move(0 if self.sidebar.is_open() else -self.sidebar.width(), 0)
        if hasattr(self, "portable_view"):
            self.portable_view.setGeometry(self.rect())

    # ------------------------------------------------------------ appearance

    def _on_scheme(self, *_):
        app = QApplication.instance()
        theme.apply(app)
        app.setWindowIcon(app_icon())
        self.sidebar.retheme()
        self.mapscreen.retheme()

    # ------------------------------------------------------------ battery

    def _check_battery(self):
        if self.mapscreen.session is None or self.mapscreen.runner is None:
            self.mapscreen.set_battery_warning("")
            return
        b = battery()
        if b and b[1] and b[0] <= BATTERY_WARN:
            text = (f"Battery at {b[0]}%. Plug in the Mac: if it shuts down, the route "
                    "stops moving the phone.")
            if not getattr(self, "_warned_batt", False):
                self.mapscreen.show_toast(f"Battery low ({b[0]}%). Plug in to keep the route running.")
                self._warned_batt = True
            self.mapscreen.set_battery_warning(text)
        else:
            self._warned_batt = False
            self.mapscreen.set_battery_warning("")

    # ------------------------------------------------------------ menu-bar item + panic hotkey

    def _install_macui(self):
        try:
            import macui
            self._macui = macui.install(self)
        except Exception:
            self._macui = None

    def _refresh_macui(self):
        if self._macui:
            try:
                self._macui.rebuildMenu()
            except Exception:
                pass

    def _on_progress(self, text: str):
        if self._macui:
            try:
                self._macui.setProgress(text)
            except Exception:
                pass

    def _post(self, fn):
        """macui (Cocoa callbacks) marshals actions here; run on the Qt loop."""
        QTimer.singleShot(0, fn)

    @property
    def saved(self):
        return self.settings.get("saved", [])

    def menu_state(self) -> dict:
        """What the menu-bar item shows."""
        sc = self.mapscreen
        s = sc.session
        st = {"route": None, "connected": sc.conn_state == "connected",
              "spoofing": sc.spoof is not None, "name": (sc.device or {}).get("name", "")}
        if s is not None:
            t = sc._last_tick
            st["route"] = {"name": s.name, "paused": s.paused,
                           "pct": int((t.fraction if t else s.fraction()) * 100),
                           "left": t.remaining_s if t else s.remaining_s(),
                           "running": sc.runner is not None}
        return st

    def route_pause_toggle(self):
        self.mapscreen.toggle_pause()

    def route_stop(self):
        self._raise_window()
        self.mapscreen.stop_route()

    def _use_place(self, lat: float, lon: float, name: str = ""):
        self.mapscreen.use_place({"lat": lat, "lon": lon, "name": name})

    def restore_real_gps(self):
        self.mapscreen.stop_spoofing()

    def _raise_window(self):
        try:
            self.showNormal(); self.raise_(); self.activateWindow()
        except Exception:
            pass

    # ------------------------------------------------------------ closing / quitting

    def _on_app_state(self, state):
        if (state == Qt.ApplicationState.ApplicationActive and not self.isVisible()
                and not self._quitting):
            QTimer.singleShot(0, self._raise_window)

    def quit_app(self):
        sc = self.mapscreen
        if sc.session is not None and sc.runner is not None and not self._quitting:
            box = QMessageBox(self)
            box.setWindowTitle("Quit Spoofr")
            box.setText("Quitting will stop your route.")
            box.setInformativeText("Keep it running in the menu bar instead?")
            keep = box.addButton("Keep running", QMessageBox.ButtonRole.AcceptRole)
            quit_ = box.addButton("Quit", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() is keep:
                QApplication.instance().setQuitOnLastWindowClosed(False)
                self.hide()
                return
            if box.clickedButton() is not quit_:
                return
        self._quitting = True
        self.close()
        QApplication.instance().quit()

    def closeEvent(self, e):
        sc = self.mapscreen
        if not self._quitting and sc.session is not None and sc.runner is not None:
            # closing the window must not stop the route: it keeps going from
            # the menu-bar item, which shows its progress
            e.ignore()
            self.hide()
            QApplication.instance().setQuitOnLastWindowClosed(False)
            return
        self._quitting = True
        sc._closing = True
        sc.pause_for_quit()               # a route resumes from here next time
        sc.hold_awake(False)
        self.portable.stop()
        if self._macui:
            try:
                self._macui.teardown()
            except Exception:
                pass
        try:
            self.bridge.close()
        finally:
            super().closeEvent(e)
            QApplication.instance().quit()


def app_icon() -> QIcon:
    """A generated mark: an accent ring + dot on a rounded square."""
    s = 256
    pm = QPixmap(s, s)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#121826"))
    p.drawRoundedRect(0, 0, s, s, 58, 58)
    pen = QPen(QColor("#3b82f6")); pen.setWidth(22)
    p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(80, 80, 96, 96)
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#3b82f6"))
    p.drawEllipse(108, 108, 40, 40)
    p.end()
    return QIcon(pm)


def main():
    import applog
    applog.install_hooks()      # a dying worker thread must not vanish silently
    QApplication.setApplicationName("Spoofr")
    QApplication.setOrganizationName("Spoofr")
    QApplication.setApplicationDisplayName("Spoofr")
    app = QApplication(sys.argv)
    theme.apply(app)
    icon = app_icon()
    app.setWindowIcon(icon)
    win = MainWindow()
    win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
