"""The route card: the floating strip at the bottom of the map in Route mode.

Pace presets, a speed slider, the live distance/ETA (progress while a route
plays), undo/clear, and one Start ⇄ Stop button. Drives the MapPanel.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSlider

from . import theme
from .widgets import Segmented, button, hairline, icon_undo, repolish

PRESETS = {"Walk": (1.4, "walking"), "Run": (3.3, "walking"),
           "Cycle": (6.0, "cycling"), "Drive": (13.5, "driving")}
GERUNDS = {"Walk": "Walking", "Run": "Running", "Cycle": "Cycling", "Drive": "Driving"}
MPH_PER_MPS = 2.2369363


def speed_text(mps: float) -> str:
    """One unit everywhere: '3.1 mph', '30 mph'."""
    mph = mps * MPH_PER_MPS
    return f"{mph:.1f} mph" if mph < 10 else f"{mph:.0f} mph"


class RouteCard(QFrame):
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.setObjectName("Float")

        h = QHBoxLayout(self)
        h.setContentsMargins(8, 7, 8, 7); h.setSpacing(8)

        self.preset = Segmented(list(PRESETS), height=32, font_pt=12)
        self.preset.changed.connect(self._on_preset)
        h.addWidget(self.preset)

        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(5, 350)          # tenths of a m/s: 1.1 – 78 mph
        self.speed.setValue(14)
        self.speed.setFixedWidth(84)
        self.speed.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed.valueChanged.connect(self._on_speed)
        h.addWidget(self.speed)
        self.speed_lbl = QLabel(speed_text(1.4))
        self.speed_lbl.setFixedWidth(66)
        self.speed_lbl.setFont(theme.ui_font(12, weight=600))
        h.addWidget(self.speed_lbl)

        h.addWidget(hairline(vertical=True))

        self.eta = QLabel("")
        self.eta.setFixedWidth(148)
        self.eta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.eta.setFont(theme.ui_font(12))
        h.addWidget(self.eta)
        self.set_estimate("")

        self.undo_btn = QPushButton()
        self.undo_btn.setProperty("variant", "icon")
        self.undo_btn.setIcon(QIcon(icon_undo()))
        self.undo_btn.setIconSize(QSize(16, 16))
        self.undo_btn.setFixedSize(32, 32)
        self.undo_btn.setToolTip("Remove the last waypoint")
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.clicked.connect(panel.undo_waypoint)
        h.addWidget(self.undo_btn)

        self.clear_btn = button("Clear", "soft", height=32)
        self.clear_btn.clicked.connect(panel.clear_route)
        h.addWidget(self.clear_btn)

        self.start_btn = button("Start", "primary", width=76, height=32)
        self.start_btn.clicked.connect(panel.toggle_route)
        h.addWidget(self.start_btn)

        panel.playingChanged.connect(self.set_playing)
        panel.estimateChanged.connect(self.set_estimate)

    def set_playing(self, playing: bool):
        """Make a running route unmistakable: the one button becomes a red Stop."""
        self.start_btn.setText("Stop" if playing else "Start")
        self.start_btn.setProperty("variant", "danger" if playing else "primary")
        repolish(self.start_btn)
        self.undo_btn.setEnabled(not playing)
        self.preset.setEnabled(not playing)

    def set_estimate(self, text: str):
        if text:
            self.eta.setText(text)
            self.eta.setStyleSheet(f"color: {theme.TEXT};")
        else:
            self.eta.setText("Click to set a destination")
            self.eta.setStyleSheet(f"color: {theme.FAINT};")

    def _on_preset(self, name: str):
        spd, prof = PRESETS.get(name, (1.4, "walking"))
        self.panel.set_preset(prof, GERUNDS.get(name, "Walking"))
        self.speed.setValue(int(round(spd * 10)))
        self._on_speed(self.speed.value())    # setValue doesn't fire if unchanged

    def _on_speed(self, v: int):
        spd = v / 10.0
        self.speed_lbl.setText(speed_text(spd))
        self.panel.set_speed(spd)
