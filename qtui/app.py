"""Spoofr, native PySide6 application shell.

The window chrome (header, status pill, Connect/Restore, hint bar) wrapped around
a MapPanel. Connect/Restore drive the device through a DeviceBridge on worker
threads; teleport + search live in the MapPanel. Route/places/QR land in later
phases. The device core (core.py) is shared with the legacy Tk app unchanged.
"""

from __future__ import annotations

import random
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import store, theme
from .bridge import DeviceBridge
from .controlbar import ControlBar
from .mapview import MapPanel
from .portable_view import PortableController, PortableView
from .sidebar import Sidebar
from .wizard import DevModeWizard

_ARROWS = {Qt.Key.Key_Up: "Up", Qt.Key.Key_Down: "Down",
           Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right"}

START_CITIES = [
    (47.6062, -122.3321), (37.7749, -122.4194), (40.7128, -74.0060),
    (25.7617, -80.1918), (48.8566, 2.3522),
]


def _btn(text: str, variant: str = "primary", width: int | None = None, height: int = 36) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("variant", variant)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(height)
    if width:
        b.setFixedWidth(width)
    return b


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle("Spoofr")
        self.resize(1060, 780)
        self.setMinimumSize(860, 600)
        self._wizard = None

        # device bridge: blocking core.* calls run off the GUI thread
        self.bridge = DeviceBridge()
        self.settings = store.load()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        hair = QFrame(); hair.setObjectName("Hairline"); hair.setFixedHeight(1)
        root.addWidget(hair)

        # body (built first so the control bar can drive the panel)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(14, 6, 14, 6)
        bl.setSpacing(0)
        self.panel = MapPanel(self.bridge, self.settings)
        bl.addWidget(self.panel, 1)
        self.hint = QLabel("Click Connect to drive your iPhone from this Mac.")
        self.hint.setStyleSheet(f"color: {theme.MUTED};")
        self.hint.setFont(theme.ui_font(12))
        self.hint.setContentsMargins(8, 6, 8, 6)
        bl.addWidget(self.hint)

        self.controlbar = ControlBar(self.panel)
        root.addWidget(self.controlbar)

        # body stack: map (This Mac) ↔ QR card (iPhone)
        self.portable = PortableController()
        self.portable_view = PortableView()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.body)
        self.stack.addWidget(self.portable_view)
        root.addWidget(self.stack, 1)

        # open over a familiar city until the phone connects
        lat, lon = random.choice(START_CITIES)
        self.panel.map.set_view(lat, lon, 11)

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
        self.sidebar.goWireless.connect(self._go_wireless)
        self.panel.hint.connect(self.set_hint)
        self.panel.committed.connect(self.sidebar.add_recent)
        self.connect_btn.clicked.connect(self._on_connect_btn)
        self.restore_btn.clicked.connect(self._on_restore)
        self.menu_btn.clicked.connect(self.sidebar.toggle)
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
        self.panel.requestTeleport.connect(lambda: self.controlbar.set_mode("Teleport"))
        self.bridge.currentLocation.connect(self.panel.locate_me)
        self.controlbar.appModeChanged.connect(self._on_app_mode)
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
        bar.setFixedHeight(60)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 20, 0)
        h.setSpacing(0)

        self.menu_btn = _btn("☰", "icon", width=40, height=36)
        f = self.menu_btn.font(); f.setPointSize(18); self.menu_btn.setFont(f)
        h.addWidget(self.menu_btn)
        h.addSpacing(8)

        mark = QLabel("◉  Spoofr")
        mark.setFont(theme.ui_font(15, weight=700))
        mark.setStyleSheet(f"color: {theme.TEXT};")
        # tint the glyph by rich text
        mark.setText(f"<span style='color:{theme.BLUE}'>◉</span>&nbsp;&nbsp;Spoofr")
        h.addWidget(mark)
        h.addSpacing(16)

        pill = QFrame(); pill.setObjectName("Pill")
        pl = QHBoxLayout(pill); pl.setContentsMargins(13, 6, 15, 6); pl.setSpacing(7)
        self.dot = QLabel("●"); self.dot.setStyleSheet(f"color: {theme.GREY}; font-size: 12px;")
        self.status = QLabel("Not connected"); self.status.setFont(theme.ui_font(13))
        pl.addWidget(self.dot); pl.addWidget(self.status)
        h.addWidget(pill)

        h.addStretch(1)
        self.restore_btn = _btn("Restore GPS", "ghost", width=118)
        self.connect_btn = _btn("Connect", "primary", width=118)
        h.addWidget(self.restore_btn)
        h.addSpacing(10)
        h.addWidget(self.connect_btn)
        return bar

    def set_status(self, text: str, color: str):
        self.status.setText(text)
        self.dot.setStyleSheet(f"color: {color}; font-size: 12px;")

    def set_hint(self, text: str):
        self.hint.setText(text)

    # ---- connect / restore ---------------------------------------------

    def _set_connect_state(self, state: str):
        """state: 'connect' (blue) | 'connecting' (disabled) | 'disconnect' (red)
        | 'reconnecting' (ghost; click = stop trying)."""
        b = self.connect_btn
        if state == "connecting":
            b.setText("Connecting…"); b.setProperty("variant", "primary"); b.setEnabled(False)
        elif state == "disconnect":
            b.setText("Disconnect"); b.setProperty("variant", "danger"); b.setEnabled(True)
        elif state == "reconnecting":
            b.setText("Reconnecting…"); b.setProperty("variant", "ghost"); b.setEnabled(True)
        else:
            b.setText("Connect"); b.setProperty("variant", "primary"); b.setEnabled(True)
        b.style().unpolish(b); b.style().polish(b)

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
        self.set_hint("Disconnected, your set location stays on the iPhone. "
                      "Reconnect and Restore GPS to reset it.")

    def _on_device_lost(self):
        # gone for good (reconnect gave up or was stopped), spoof persists on the phone
        self.panel.persist_spoof()
        self.panel.clear_all()
        self.panel.show_walk_pad(False)
        self._set_connect_state("connect")
        self.set_status("Disconnected", theme.GREY)
        self.set_hint("Lost the iPhone, your set location stays on the phone. "
                      "Reconnect and Restore GPS to reset it.")

    def _on_reconnecting(self, attempt: int):
        self._set_connect_state("reconnecting")
        if attempt == 1:
            self.set_hint("Connection dropped, reconnecting… your set location stays on "
                          "the iPhone. Click “Reconnecting…” to stop trying.")

    def _on_visible(self, kinds: str):
        b = self.bridge
        if b.is_connected() or b._connecting or b.is_reconnecting():
            return
        if self.controlbar.app_mode.value() == "iPhone":
            return
        if kinds:
            self.set_status(f"Ready  ·  iPhone on {kinds}", theme.LIVE)
        else:
            self.set_status("Not connected", theme.GREY)

    def _on_connected(self, device):
        self._set_connect_state("disconnect")
        self.panel.show_walk_pad(True)
        spoof = self.settings.get("active_spoof")
        if spoof:
            self.panel.restore_active_spoof(spoof["lat"], spoof["lon"])
            self.set_hint("Your iPhone is still set to your last location, Restore GPS to reset, "
                          "or pick a new spot.")
        else:
            self.bridge.locate()         # fly the map to the user's current location
            self.set_hint("Connected, finding your location… drop a pin or search to move your iPhone.")
        # remember + surface whether the cable is still required
        wireless_on = bool(getattr(device, "wireless_on", False))
        self.settings["wireless_on"] = wireless_on
        store.save(self.settings)
        self.sidebar.refresh_wireless(wireless_on)

    def _on_restore(self):
        self.panel.stop_motion()         # stop any route/walk before clearing the spoof
        self.bridge.restore()

    # ---- places + walking ----------------------------------------------

    def _use_place(self, lat: float, lon: float):
        self.sidebar.close_menu()
        self.panel.goto(lat, lon)
        if self.bridge.is_connected():
            self.bridge.set_location(lat, lon)

    def _save_current(self):
        loc = self.panel.pending or self.panel._live_pos
        if not loc:
            self.set_hint("Pick or set a location first, then save it.")
            return
        self.sidebar.save_place(loc[0], loc[1])

    def _import_gpx(self):
        self.sidebar.close_menu()
        if self.panel.import_gpx():
            self.controlbar.set_mode("Route")

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
        if mode == "iphone":
            self.panel.stop_route()
            self.panel.show_walk_pad(False)
            self.bridge.drop_device()          # hand the device to the phone; keep the tunnel
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
            self.stack.setCurrentWidget(self.body)
            self._set_connect_state("connect")
            self.restore_btn.setEnabled(True)
            self.set_status("Not connected", theme.GREY)
            self.set_hint("Click Connect to drive your iPhone from this Mac.")

    def _on_qr_ready(self, url: str):
        self.portable_view.show_qr(self.portable.qr_png(), url)
        self.set_status("Waiting for your phone…", theme.AMBER)
        self.set_hint("Scan the QR with your iPhone to take control. Switch back to “This Mac” anytime.")

    def _on_portable_status(self, st):
        if self.controlbar.app_mode.value() != "iPhone":
            return
        if st and st.get("connected"):
            nm = st.get("name") or "iPhone"
            self.set_status(f"iPhone in control  ·  {nm}", theme.GREEN)
            self.portable_view.set_status(f"●  {nm} connected, controlling from your phone", theme.GREEN)
        else:
            self.set_status("Waiting for your phone…", theme.AMBER)
            self.portable_view.set_status("●  Waiting for your phone, scan the QR", theme.AMBER)

    def _on_portable_failed(self, msg: str):
        self.portable_view.set_status(f"⚠  {msg}", theme.RED)
        self.set_status("Portable mode failed", theme.RED)
        self.set_hint("Couldn’t start portable mode, switch back to This Mac and try again.")

    def _copy_portable_link(self):
        url = self.portable.url()
        if not url:
            return
        QApplication.clipboard().setText(url)
        self.portable_view.copy_btn.setText("Copied!")
        QTimer.singleShot(1200, lambda: self.portable_view.copy_btn.setText("Copy link"))

    def keyPressEvent(self, e):
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
