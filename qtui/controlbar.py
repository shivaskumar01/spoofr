"""The control strip under the header: the Teleport/Route mode toggle and, in
Route mode, the playback controls (pace presets, speed, loop/bounce, start/stop/
clear). Drives the MapPanel.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QWidget,
)

from . import theme
from .widgets import Segmented

PRESETS = {"Walk": (1.4, "walking"), "Run": (3.3, "walking"),
           "Cycle": (6.0, "cycling"), "Drive": (13.5, "driving")}
GERUNDS = {"Walk": "Walking", "Run": "Running", "Cycle": "Cycling", "Drive": "Driving"}


def _btn(text, variant, w=None, h=32):
    b = QPushButton(text)
    b.setProperty("variant", variant)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(h)
    if w:
        b.setFixedWidth(w)
    return b


class ControlBar(QFrame):
    appModeChanged = Signal(str)        # "mac" | "iphone"

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.setObjectName("Ctl")
        self.setStyleSheet(f"#Ctl {{ background: {theme.BG}; }}")
        self.setFixedHeight(52)

        h = QHBoxLayout(self)
        h.setContentsMargins(16, 9, 16, 9); h.setSpacing(10)

        self.app_mode = Segmented(["This Mac", "iPhone"], height=32, font_pt=12)
        self.app_mode.changed.connect(self._on_app_mode)
        h.addWidget(self.app_mode)

        self.mode = Segmented(["Teleport", "Route"], height=32, font_pt=12)
        self.mode.changed.connect(self._on_mode)
        h.addWidget(self.mode)

        self.route_ctl = QWidget()
        rl = QHBoxLayout(self.route_ctl)
        rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(8)
        self.preset = Segmented(["Walk", "Run", "Cycle", "Drive"], height=28, font_pt=11)
        self.preset.changed.connect(self._on_preset)
        rl.addWidget(self.preset)
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(5, 350); self.speed.setValue(14); self.speed.setFixedWidth(94)
        self.speed.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 4px; background: {theme.ELEV}; border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {theme.BLUE}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {theme.BLUE}; width: 15px; height: 15px;"
            f" margin: -6px 0; border-radius: 7px; }}"
            f"QSlider::handle:horizontal:hover {{ background: {theme.BLUE_HI}; }}")
        self.speed.valueChanged.connect(self._on_speed)
        rl.addWidget(self.speed)
        self.speed_lbl = QLabel("1.4 m/s")
        # "26.0 m/s" needs more than 50px; it was rendering as "26.0 m"
        self.speed_lbl.setFixedWidth(70); self.speed_lbl.setFont(theme.ui_font(12))
        rl.addWidget(self.speed_lbl)
        self.start_btn = _btn("Start", "primary")
        self.start_btn.clicked.connect(panel.start_route); rl.addWidget(self.start_btn)
        self.stop_btn = _btn("Stop", "soft")
        self.stop_btn.clicked.connect(panel.stop_route); rl.addWidget(self.stop_btn)
        clear = _btn("Clear", "soft"); clear.clicked.connect(panel.clear_route); rl.addWidget(clear)
        h.addWidget(self.route_ctl)
        h.addStretch(1)
        self.route_ctl.hide()
        panel.playingChanged.connect(self.set_playing)

    def set_playing(self, playing: bool):
        """Make a running route unmistakable: Start goes quiet, Stop lights up."""
        self.start_btn.setEnabled(not playing)
        self.start_btn.setText("Playing…" if playing else "Start")
        self.stop_btn.setProperty("variant", "danger" if playing else "soft")
        for b in (self.start_btn, self.stop_btn):
            b.style().unpolish(b); b.style().polish(b)

    def _on_app_mode(self, val: str):
        iphone = (val == "iPhone")
        self.mode.setVisible(not iphone)
        if iphone:
            self.route_ctl.hide()
        else:
            self.route_ctl.setVisible(self.mode.value() == "Route")
        self.appModeChanged.emit("iphone" if iphone else "mac")

    def _on_mode(self, val: str):
        self.panel.set_mode(val.lower())
        self.route_ctl.setVisible(val == "Route")

    def set_mode(self, val: str):
        """External sync (e.g. after a GPX import switches to Route)."""
        self.mode.set_value(val)
        self._on_mode(val)

    def _on_preset(self, name: str):
        spd, prof = PRESETS.get(name, (1.4, "walking"))
        self.panel.set_preset(prof, GERUNDS.get(name, "Walking"))
        self._mph = name in ("Cycle", "Drive")   # vehicle speeds read in mph
        self.speed.setValue(int(spd * 10))
        self._on_speed(self.speed.value())        # setValue doesn't fire if unchanged;
                                                  # re-render so the unit always matches

    def _on_speed(self, v: int):
        spd = v / 10.0
        if getattr(self, "_mph", False):
            self.speed_lbl.setText(f"{spd * 2.2369363:.0f} mph")
        else:
            self.speed_lbl.setText(f"{spd:.1f} m/s")
        self.panel.set_speed(spd)
