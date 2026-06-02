"""Spoofr — native PySide6 application shell.

The window chrome (header, status pill, Connect/Restore, hint bar) wrapped around
a MapPanel. Connect/Restore drive the device through a DeviceBridge on worker
threads; teleport + search live in the MapPanel. Route/places/QR land in later
phases. The device core (core.py) is shared with the legacy Tk app unchanged.
"""

from __future__ import annotations

import random
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from . import theme
from .bridge import DeviceBridge
from .mapview import MapPanel
from .wizard import DevModeWizard

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

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        hair = QFrame(); hair.setObjectName("Hairline"); hair.setFixedHeight(1)
        root.addWidget(hair)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 8, 14, 6)
        bl.setSpacing(0)
        self.panel = MapPanel(self.bridge)
        bl.addWidget(self.panel, 1)
        self.hint = QLabel("Click Connect to drive your iPhone from this Mac.")
        self.hint.setStyleSheet(f"color: {theme.MUTED};")
        self.hint.setFont(theme.ui_font(12))
        self.hint.setContentsMargins(8, 6, 8, 6)
        bl.addWidget(self.hint)
        root.addWidget(body, 1)

        # open over a familiar city until the phone connects
        lat, lon = random.choice(START_CITIES)
        self.panel.map.set_view(lat, lon, 11)

        # ---- wiring ----
        self.bridge.status.connect(self.set_status)
        self.bridge.hint.connect(self.set_hint)
        self.bridge.connected.connect(self._on_connected)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.devModeRequired.connect(self._on_dev_mode_required)
        self.panel.hint.connect(self.set_hint)
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        self.restore_btn.clicked.connect(lambda: self.bridge.restore())

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Bar")
        bar.setFixedHeight(60)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 20, 0)
        h.setSpacing(0)

        menu = _btn("☰", "icon", width=40, height=36)
        f = menu.font(); f.setPointSize(18); menu.setFont(f)
        h.addWidget(menu)
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

    def _on_connect_clicked(self):
        self.connect_btn.setEnabled(False)
        self.bridge.connect()

    def _on_connected(self, device):
        self.connect_btn.setEnabled(True)
        self.set_hint("Connected — drop a pin or search a place, then Set location here.")

    def _on_failed(self, msg: str):
        self.connect_btn.setEnabled(True)
        QMessageBox.critical(self, "Couldn’t connect", msg)

    def _on_dev_mode_required(self):
        self.connect_btn.setEnabled(True)
        if self._wizard is not None and self._wizard.isVisible():
            self._wizard.raise_()
            return
        self._wizard = DevModeWizard(self)
        self._wizard.accepted.connect(self._on_connect_clicked)   # dev mode on → reconnect
        self._wizard.show()

    def closeEvent(self, e):
        try:
            self.bridge.close()
        finally:
            super().closeEvent(e)


def main():
    QApplication.setApplicationName("Spoofr")
    QApplication.setOrganizationName("Spoofr")
    QApplication.setApplicationDisplayName("Spoofr")
    app = QApplication(sys.argv)
    theme.apply_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
