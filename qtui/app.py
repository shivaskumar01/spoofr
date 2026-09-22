"""Spoofr, native PySide6 application shell.

The window chrome (header with the status pill, This Mac / iPhone switch and
Connect / Restore; a footer with the hint line and the live coordinates) around
a full-bleed MapPanel. Connect/Restore drive the device through a DeviceBridge
on worker threads; everything on the map lives in the MapPanel.
"""

from __future__ import annotations

import random
import sys

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import store, theme
from .bridge import DeviceBridge
from .mapview import MapPanel
from .portable_view import PortableController, PortableView
from .sidebar import Sidebar
from .widgets import Dot, ElidedLabel, Segmented, button, hairline, icon_menu, repolish
from .wizard import DevModeWizard

_ARROWS = {Qt.Key.Key_Up: "Up", Qt.Key.Key_Down: "Down",
           Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right"}

START_CITIES = [
    (47.6062, -122.3321), (37.7749, -122.4194), (40.7128, -74.0060),
    (25.7617, -80.1918), (48.8566, 2.3522),
]


class StatusPill(QFrame):
    """Dot + one line of status; long device names elide instead of pushing
    the header's buttons off the edge."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Status")
        self.setStyleSheet(f"#Status {{ background: {theme.ELEV}; border-radius: 15px; }}")
        self.setFixedHeight(30)
        self.setMaximumWidth(380)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 14, 0); lay.setSpacing(8)
        self.dot = Dot(theme.GREY, 8)
        self.label = ElidedLabel("Not connected")
        self.label.setFont(theme.ui_font(13, weight=500))
        lay.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.label, 1)

    def set(self, text: str, color: str):
        self.label.setText(text)
        self.dot.set_color(color)
        self.label.update()

    def text(self) -> str:
        return self.label.text()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle("Spoofr")
        self.resize(1180, 800)
        self.setMinimumSize(900, 620)
        self._wizard = None

        # device bridge: blocking core.* calls run off the GUI thread
        self.bridge = DeviceBridge()
        self.settings = store.load()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addWidget(hairline())

        # body: the map (This Mac) ↔ the QR card (iPhone)
        self.panel = MapPanel(self.bridge, self.settings)
        self.portable = PortableController()
        self.portable_view = PortableView()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.panel)
        self.stack.addWidget(self.portable_view)
        root.addWidget(self.stack, 1)

        root.addWidget(hairline())
        root.addWidget(self._build_footer())

        # open over a familiar city until the phone connects
        lat, lon = random.choice(START_CITIES)
        self.panel.map.set_view(lat, lon, 12)

        # slide-out side menu (overlay child of the window)
        self.sidebar = Sidebar(self.settings, self)
        self.sidebar.move(-self.sidebar.width(), 0)
        self.sidebar.hide()

        # ---- wiring ----
        self.bridge.status.connect(self.set_status)
        self.bridge.hint.connect(self.set_hint)
        self.bridge.connected.connect(self._on_connected)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.devModeRequired.connect(self._on_dev_mode_required)
        self.bridge.deviceLost.connect(self._on_device_lost)
        self.bridge.reconnecting.connect(self._on_reconnecting)
        self.bridge.visible.connect(self._on_visible)
        self.bridge.wirelessResult.connect(self._on_wireless_result)
        self.bridge.currentLocation.connect(self.panel.locate_me)
        # the liveness monitor re-asserts this fix to prove the channel still works
        self.bridge.heartbeat_source = self.panel.heartbeat_point
        self.panel.hint.connect(self.set_hint)
        self.panel.readout.connect(self._set_readout)
        self.panel.committed.connect(self.sidebar.add_recent)
        self.panel.placeNamed.connect(self.sidebar.name_recent)
        self.connect_btn.clicked.connect(self._on_connect_btn)
        self.restore_btn.clicked.connect(self._on_restore)
        self.menu_btn.clicked.connect(self.sidebar.toggle)
        self.app_mode.changed.connect(self._on_app_mode)
        self.sidebar.goWireless.connect(self._go_wireless)
        self.sidebar.usePlace.connect(self._use_place)
        self.sidebar.saveCurrent.connect(self._save_current)
        self.sidebar.brightnessChanged.connect(self.panel.set_brightness)
        self.sidebar.pulseToggled.connect(self.panel.set_pulsing)
        self.sidebar.jitterToggled.connect(self.panel.set_jitter)
        self.sidebar.snapToggled.connect(self.panel.set_snap)
        self.sidebar.loopToggled.connect(self.panel.set_loop)
        self.sidebar.bounceToggled.connect(self.panel.set_bounce)
        self.sidebar.importGpx.connect(self._import_gpx)
        self.sidebar.exportGpx.connect(self.panel.export_gpx)
        self.portable.qrReady.connect(self._on_qr_ready)
        self.portable.statusUpdate.connect(self._on_portable_status)
        self.portable.failed.connect(self._on_portable_failed)
        self.portable_view.copy_btn.clicked.connect(self._copy_portable_link)

        # apply saved preferences
        self.panel.set_brightness(self.settings.get("brightness", "Normal"))
        self.panel.set_pulsing(self.settings.get("pulse", True))
        self.panel.set_jitter(self.settings.get("jitter", False))
        self.panel.set_snap(self.settings.get("snap_roads", False))

        # menu-bar item + panic hotkey (native, best-effort)
        self._macui = None
        self.sidebar.placesChanged.connect(self._refresh_macui)
        QTimer.singleShot(300, self._install_macui)

        # idle pre-flight: light up the pill when an iPhone is visible
        self.bridge.start_visibility()

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Bar")
        bar.setFixedHeight(56)
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 0, 16, 0)
        h.setSpacing(0)

        self.menu_btn = QPushButton()
        self.menu_btn.setProperty("variant", "icon")
        self.menu_btn.setIcon(QIcon(icon_menu()))
        self.menu_btn.setIconSize(QSize(18, 18))
        self.menu_btn.setFixedSize(36, 36)
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.setToolTip("Places, route options and settings")
        h.addWidget(self.menu_btn)
        h.addSpacing(8)

        mark = QLabel(f"<span style='color:{theme.BLUE}'>◉</span>&nbsp;&nbsp;Spoofr")
        mark.setFont(theme.ui_font(15, weight=700))
        h.addWidget(mark)
        h.addSpacing(16)

        self.pill = StatusPill()
        h.addWidget(self.pill, 1, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        h.addSpacing(16)

        self.app_mode = Segmented(["This Mac", "iPhone"], height=32, font_pt=12)
        self.app_mode.setToolTip("Drive the iPhone from this Mac, or hand control to the "
                                 "phone itself with a QR code")
        h.addWidget(self.app_mode)
        h.addSpacing(12)
        self.restore_btn = button("Restore GPS", "ghost", height=32)
        self.restore_btn.setMinimumWidth(110)
        self.restore_btn.setToolTip("Put the iPhone back on its real GPS (⌃⌥⌘R from anywhere)")
        self.connect_btn = button("Connect", "primary", height=32)
        self.connect_btn.setMinimumWidth(118)
        h.addWidget(self.restore_btn)
        h.addSpacing(8)
        h.addWidget(self.connect_btn)
        return bar

    def _build_footer(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Bar")
        bar.setFixedHeight(30)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 0, 16, 0)
        h.setSpacing(16)
        self.hint = ElidedLabel("Click Connect to drive your iPhone from this Mac.")
        self.hint.setFont(theme.ui_font(12))
        self.hint.setStyleSheet(f"color: {theme.MUTED};")
        h.addWidget(self.hint, 1)
        self.readout = QLabel("")
        self.readout.setFont(theme.mono_font(11))
        self.readout.setStyleSheet(f"color: {theme.LIVE_HI};")
        self.readout.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.readout.setToolTip("Where your iPhone is (select to copy)")
        h.addWidget(self.readout)
        return bar

    # the pill is the one status surface; `status` stays for callers/tests that
    # read it back
    @property
    def status(self) -> StatusPill:
        return self.pill

    def set_status(self, text: str, color: str):
        self.pill.set(text, color)

    def set_hint(self, text: str):
        self.hint.setText(text)
        self.hint.update()

    def _set_readout(self, text: str):
        self.readout.setText(f"◉  {text}" if text else "")

    # ---- connect / restore ---------------------------------------------

    def _set_connect_state(self, state: str):
        """state: 'connect' (blue) | 'connecting' (disabled) | 'disconnect' (red)
        | 'reconnecting' (ghost; click = stop trying)."""
        b = self.connect_btn
        if state == "connecting":
            b.setText("Connecting…"); b.setProperty("variant", "primary"); b.setEnabled(False)
        elif state == "disconnect":
            b.setText("Disconnect"); b.setProperty("variant", "ghost"); b.setEnabled(True)
        elif state == "reconnecting":
            b.setText("Reconnecting…"); b.setProperty("variant", "ghost"); b.setEnabled(True)
        else:
            b.setText("Connect"); b.setProperty("variant", "primary"); b.setEnabled(True)
        repolish(b)

    def _on_connect_btn(self):
        if self.bridge.is_reconnecting():
            self.bridge.cancel_reconnect()       # the button reads “Reconnecting…”
        elif self.bridge.is_connected():
            self._disconnect()
        elif not self.bridge._connecting:
            self._start_connect()

    def _start_connect(self):
        self._set_connect_state("connecting")
        self.bridge.connect()

    def _disconnect(self):
        self.panel.persist_spoof()       # remember the set location for next time
        self.bridge.disconnect()         # close session WITHOUT clearing, spoof stays on the phone
        self.panel.clear_all()
        self.panel.show_walk_pad(False)
        self._set_connect_state("connect")
        self.set_status("Not connected", theme.GREY)
        self.set_hint("Disconnected. Your set location stays on the iPhone; "
                      "reconnect and Restore GPS to reset it.")

    def _on_device_lost(self):
        # gone for good (reconnect gave up or was stopped), spoof persists on the phone
        self.panel.persist_spoof()
        if self.panel.route_is_playing():
            # a route is mid-walk and holding. Keep it, and its waypoints, and let
            # the idle visibility poll reconnect as soon as the phone is back —
            # unplugging should not cost you the walk you were halfway through.
            self._set_connect_state("connect")
            self.set_status("Waiting for your iPhone", theme.AMBER)
            self.set_hint("Route paused. Plug the iPhone back in, or bring it back onto "
                          "this Wi-Fi, and it picks up where it left off.")
            return
        self.panel.clear_all()
        self.panel.show_walk_pad(False)
        self._set_connect_state("connect")
        self.set_status("Disconnected", theme.GREY)
        self.set_hint("Lost the iPhone. Your set location stays on the phone; "
                      "reconnect and Restore GPS to reset it.")

    def _on_reconnecting(self, attempt: int):
        self._set_connect_state("reconnecting")
        if attempt == 1:
            self.set_hint("Connection dropped, reconnecting… your set location stays on "
                          "the iPhone. Click “Reconnecting…” to stop trying.")

    def _on_visible(self, kinds: str):
        b = self.bridge
        if b.is_connected() or b._connecting or b.is_reconnecting():
            return
        if self.app_mode.value() == "iPhone":
            return
        if kinds and self.panel.route_is_playing():
            # a paused route wants its phone back; don't make the user click
            self.set_status(f"iPhone back on {kinds}, reconnecting…", theme.AMBER)
            self._start_connect()
            return
        if kinds:
            self.set_status(f"Ready  ·  iPhone on {kinds}", theme.LIVE)
        else:
            self.set_status("Not connected", theme.GREY)

    def _on_connected(self, device):
        if self.app_mode.value() == "iPhone":
            # a connect that was already running when the user switched to iPhone
            # mode: the phone's server owns the device now, so let go at once
            self.bridge.drop_device()
            return
        self._set_connect_state("disconnect")
        self.panel.show_walk_pad(True)
        spoof = self.settings.get("active_spoof")
        if self.panel.route_is_playing():
            # the route's next fix is the right position; re-asserting an older
            # one here would yank the phone backwards
            self.set_hint("Reconnected. The route picks up where it left off.")
        elif spoof:
            self.panel.restore_active_spoof(spoof["lat"], spoof["lon"])
            self.set_hint("Your iPhone is still set to your last location. Restore GPS to "
                          "reset it, or pick a new spot.")
        else:
            self.bridge.locate()         # fly the map to the user's current location
            self.set_hint("Connected. Finding your location… drop a pin or search to move your iPhone.")
        # remember + surface whether the cable is still required
        wireless_on = bool(getattr(device, "wireless_on", False))
        self.settings["wireless_on"] = wireless_on
        store.save(self.settings)
        self.sidebar.refresh_wireless(wireless_on)

    def _on_restore(self):
        self.panel.stop_motion()         # stop any route/walk before clearing the spoof
        self.bridge.restore()

    # ---- places + walking ----------------------------------------------

    def _use_place(self, lat: float, lon: float, label: str = ""):
        self.sidebar.close_menu()
        if self.app_mode.value() == "iPhone":
            self.set_hint("Switch to This Mac to set a location from here.")
            return
        if self.bridge.is_connected():
            self.panel.teleport(lat, lon, label)
        else:
            self.panel.goto(lat, lon, label=label)   # stage it; Set once connected

    def _save_current(self):
        loc = self.panel.pending or self.panel._live_pos
        if not loc:
            self.set_hint("Pick or set a location first, then save it.")
            return
        name = (self.panel._pending_label if self.panel.pending
                else self.sidebar.recent_name(*loc))
        self.sidebar.save_place(loc[0], loc[1], name)

    def _import_gpx(self):
        self.sidebar.close_menu()
        if self.panel.import_gpx():
            self.panel.set_mode("route")

    # ---- one-time wireless enable ---------------------------------------

    def _go_wireless(self):
        self.sidebar.set_wireless_busy(True)
        self.bridge.go_wireless()

    def _on_wireless_result(self, ok: bool, msg: str):
        self.sidebar.set_wireless_busy(False)
        self.sidebar.set_wireless_status(("✓  " if ok else "⚠  ") + msg,
                                         theme.GREEN if ok else theme.RED)
        if ok:
            self.settings["wireless_on"] = True
            store.save(self.settings)

    # ---- iPhone (portable QR) mode -------------------------------------

    def _on_app_mode(self, mode: str):
        if mode == "iPhone":
            self.panel.stop_motion()
            self.panel.persist_spoof()         # remember it before we let go of the device
            self.panel.show_walk_pad(False)
            self.bridge.drop_device()          # hand the device to the phone; keep the tunnel
            self.panel.clear_all()             # the Mac no longer owns this fix
            self._set_connect_state("connect")
            self.connect_btn.setEnabled(False)
            self.restore_btn.setEnabled(False)
            self.portable_view.reset()
            self.stack.setCurrentWidget(self.portable_view)
            self.set_status("Starting portable…", theme.AMBER)
            self.set_hint("Starting the phone server… you may be asked for your password once.")
            self.portable.start()
        else:
            self.portable.stop()
            self.stack.setCurrentWidget(self.panel)
            self._set_connect_state("connect")
            self.restore_btn.setEnabled(True)
            self.set_status("Not connected", theme.GREY)
            self.set_hint("Click Connect to drive your iPhone from this Mac.")

    def _on_qr_ready(self, url: str):
        self.portable_view.show_qr(self.portable.qr_png(), url)
        self.set_status("Waiting for your phone…", theme.AMBER)
        self.set_hint("Scan the QR with your iPhone to take control. Switch back to “This Mac” anytime.")

    def _on_portable_status(self, st):
        if self.app_mode.value() != "iPhone":
            return
        if st and st.get("connected"):
            nm = st.get("name") or "iPhone"
            self.set_status(f"iPhone in control  ·  {nm}", theme.GREEN)
            self.portable_view.set_status(f"{nm} connected, controlling from your phone", theme.GREEN)
        else:
            self.set_status("Waiting for your phone…", theme.AMBER)
            self.portable_view.set_status("Waiting for your phone, scan the QR", theme.AMBER)

    def _on_portable_failed(self, msg: str):
        self.portable_view.set_status(msg, theme.RED)
        self.set_status("Portable mode failed", theme.RED)
        self.set_hint("Couldn’t start portable mode. Switch back to This Mac and try again.")

    def _copy_portable_link(self):
        url = self.portable.url()
        if not url:
            return
        QApplication.clipboard().setText(url)
        self.portable_view.copy_btn.setText("Copied!")
        QTimer.singleShot(1200, lambda: self.portable_view.copy_btn.setText("Copy link"))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and self.sidebar.is_open():
            self.sidebar.close_menu()
            e.accept(); return
        if not e.isAutoRepeat():
            k = _ARROWS.get(e.key())
            if k and not isinstance(self.focusWidget(), QLineEdit):
                self.panel.key_walk(k, True)
                e.accept(); return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if not e.isAutoRepeat():
            k = _ARROWS.get(e.key())
            if k:
                self.panel.key_walk(k, False)
                e.accept(); return
        super().keyReleaseEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "sidebar"):
            self.sidebar.fit_height(self.height())
            self.sidebar.move(0 if self.sidebar.is_open() else -self.sidebar.width(), 0)

    def _on_failed(self, msg: str):
        self._set_connect_state("connect")
        QMessageBox.critical(self, "Couldn’t connect", msg)

    def _on_dev_mode_required(self):
        self._set_connect_state("connect")
        if self._wizard is not None and self._wizard.isVisible():
            self._wizard.raise_()
            return
        self._wizard = DevModeWizard(self)
        self._wizard.accepted.connect(self._start_connect)   # dev mode on → reconnect
        self._wizard.show()

    # ---- menu-bar item + panic hotkey (native, best-effort) ------------

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

    def _post(self, fn):
        """macui (Cocoa callbacks) marshals actions here; run on the Qt loop."""
        QTimer.singleShot(0, fn)

    @property
    def saved(self):
        return self.settings.get("saved", [])

    def restore_real_gps(self):
        self.panel.stop_motion()
        self.bridge.restore(panic=True)

    def _raise_window(self):
        try:
            self.showNormal(); self.raise_(); self.activateWindow()
        except Exception:
            pass

    def closeEvent(self, e):
        self.panel._closing = True       # stop the jitter worker
        self.panel.stop_route()          # releases caffeinate via playingChanged
        self.panel.hold_awake(False)     # don't orphan caffeinate
        self.panel.persist_spoof()       # keep + remember the set location across runs
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


def app_icon() -> QIcon:
    """A generated mark: a blue ring + dot on the dark panel, rounded square."""
    s = 256
    pm = QPixmap(s, s)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme.PANEL))
    p.drawRoundedRect(0, 0, s, s, 58, 58)
    pen = QPen(QColor(theme.BLUE)); pen.setWidth(22)
    p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(80, 80, 96, 96)
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(theme.BLUE))
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
    theme.apply_theme(app)
    icon = app_icon()
    app.setWindowIcon(icon)
    win = MainWindow()
    win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
