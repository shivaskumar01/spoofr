"""Spoofr — native PySide6 application shell.

Phase 0/1: the window chrome, the GPU map, and the connection status surface.
Teleport/route/places/QR land in later phases. The device core (core.py) is
shared with the legacy Tk app unchanged.
"""

from __future__ import annotations

import random
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from . import theme
from .tilemap import TileMap

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


class MapCard(QFrame):
    """The rounded map card: the GPU map plus controls that float over it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MapFrame")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        self.map = TileMap(self)
        lay.addWidget(self.map)

        # floating zoom pill (bottom-right)
        self.zoom_pill = QFrame(self)
        self.zoom_pill.setObjectName("Pill")
        zl = QVBoxLayout(self.zoom_pill)
        zl.setContentsMargins(3, 3, 3, 3)
        zl.setSpacing(2)
        zin = _btn("＋", "icon", width=42, height=40)
        zout = _btn("－", "icon", width=42, height=40)
        for b in (zin, zout):
            f = b.font(); f.setPointSize(18); f.setBold(True); b.setFont(f)
        sep = QFrame(); sep.setObjectName("Hairline"); sep.setFixedHeight(1)
        zl.addWidget(zin); zl.addWidget(sep); zl.addWidget(zout)
        zin.clicked.connect(lambda: self.map.zoom_at(1.0))
        zout.clicked.connect(lambda: self.map.zoom_at(-1.0))
        self.zoom_pill.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        p = self.zoom_pill
        p.adjustSize()
        p.move(self.width() - p.width() - 16, self.height() - p.height() - 16)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle("Spoofr")
        self.resize(1060, 780)
        self.setMinimumSize(860, 600)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        hair = QFrame(); hair.setObjectName("Hairline"); hair.setFixedHeight(1)
        root.addWidget(hair)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 8, 14, 10)
        self.card = MapCard()
        bl.addWidget(self.card)
        root.addWidget(body, 1)

        # open over a familiar city until the phone connects
        lat, lon = random.choice(START_CITIES)
        self.card.map.set_view(lat, lon, 11)

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
