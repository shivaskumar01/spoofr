"""Settings & Help: a sheet that slides in from the left over a dim scrim.

Everything here is a preference that sticks (saved to ~/.spoofr/settings.json).
Places, routes and the route options live in the control panel instead; this
is for how the app behaves, plus help.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import store, theme
from .widgets import IconButton, Segmented, Switch, button, hairline, icon_close, label, set_role

WIDTH = 340


def _caption(text):
    lb = label(text.upper(), 10, 700, "faint")
    f = lb.font(); f.setLetterSpacing(f.SpacingType.PercentageSpacing, 108); lb.setFont(f)
    return lb


def _wrap(text, size=12, role="muted"):
    lb = label(text, size, role=role, wrap=True)
    # word-wrapped labels need a height-for-width size policy, or a layout
    # clips their last line
    lb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return lb


class _Scrim(QWidget):
    """Dims the window behind the open sheet; a click on it closes the sheet."""
    clicked = Signal()

    def paintEvent(self, e):
        c = QColor(0, 0, 0, 110 if theme.is_dark() else 60)
        QPainter(self).fillRect(self.rect(), c)

    def mousePressEvent(self, e):
        self.clicked.emit()


class _Group(QFrame):
    """A rounded card of rows separated by hairlines (System Settings style)."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Group")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)

    def add(self, w):
        if self._lay.count():
            self._lay.addWidget(hairline())
        self._lay.addWidget(w)
        return w


def _row(text, control, detail: str = "", stacked: bool = False) -> QWidget:
    """A settings row: label (and detail) with its control at the right, or
    underneath when the control is too wide to share the line."""
    row = QFrame()
    if stacked:
        col = QVBoxLayout(row); col.setContentsMargins(14, 9, 12, 10); col.setSpacing(6)
        col.addWidget(_wrap(text, 13, ""))
        if detail:
            col.addWidget(_wrap(detail, 11))
        col.addWidget(control)
        return row
    rl = QHBoxLayout(row); rl.setContentsMargins(14, 9, 12, 9); rl.setSpacing(10)
    col = QVBoxLayout(); col.setSpacing(2)
    col.addWidget(_wrap(text, 13, ""))
    if detail:
        col.addWidget(_wrap(detail, 11))
    rl.addLayout(col, 1)
    rl.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


class Sidebar(QFrame):
    autoConnectToggled = Signal(bool)
    unitsChanged = Signal(str)
    catchUpChanged = Signal(str)
    brightnessChanged = Signal(str)
    pulseToggled = Signal(bool)
    jitterToggled = Signal(bool)
    walkPadToggled = Signal(bool)
    goWireless = Signal()
    openPortable = Signal()

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setObjectName("Sheet")
        self.setFixedWidth(WIDTH)
        self._open = False
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._after_slide)
        self._scrim = _Scrim(parent)
        self._scrim.hide()
        self._scrim.clicked.connect(self.close_menu)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(20, 16, 12, 10)
        self._title = label("", 17, 700)
        self.retheme()
        close = IconButton(icon_close, "Close (Esc)", size=30, icon_px=13, color=lambda: theme.MUTED)
        close.clicked.connect(self.close_menu)
        top.addWidget(self._title); top.addStretch(1); top.addWidget(close)
        root.addLayout(top)
        root.addWidget(hairline())

        area = QScrollArea(); area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.viewport().setAutoFillBackground(False)
        inner = QWidget(); inner.setObjectName("SideInner")
        inner.setStyleSheet("#SideInner { background: transparent; }")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(16, 14, 16, 20); lay.setSpacing(8)
        area.setWidget(inner)
        root.addWidget(area, 1)
        self._build(lay)
        lay.addStretch(1)

    def _build(self, lay):
        from .mapview import default_units
        s = self.settings
        lay.addWidget(_caption("Connection"))
        g = _Group()
        self.auto = Switch(name="Connect automatically")
        self.auto.set_on(s.get("auto_connect", True))
        self.auto.toggled.connect(lambda on: self._save("auto_connect", bool(on), self.autoConnectToggled))
        g.add(_row("Connect automatically", self.auto,
                   "Reconnect to iPhones you’ve used before as soon as they’re plugged in "
                   "or back in reach."))
        self.wifi_btn = button("Set up", "soft", height=30)
        self.wifi_btn.clicked.connect(self.goWireless.emit)
        g.add(_row("Wi-Fi control", self.wifi_btn,
                   "One time, with the cable in. Then routes keep going when you unplug, "
                   "as long as the iPhone stays on this Mac’s Wi-Fi."))
        phone = button("Show QR", "soft", height=30)
        phone.clicked.connect(self.openPortable.emit)
        g.add(_row("Control from your iPhone", phone,
                   "Drive everything from the phone’s browser on the same Wi-Fi."))
        lay.addWidget(g)
        self.wifi_status = _wrap("")
        lay.addWidget(self.wifi_status)
        self.refresh_wireless(bool(s.get("wireless_on")))

        lay.addSpacing(6)
        lay.addWidget(_caption("Routes"))
        g = _Group()
        self.units = Segmented(["kmh", "mph"], height=28, font_pt=11,
                               labels={"kmh": "km/h", "mph": "mph"})
        self.units.set_value(s.get("units") or default_units())
        self.units.changed.connect(lambda u: self._save("units", u, self.unitsChanged))
        g.add(_row("Units", self.units))
        self.catch = Segmented(["ask", "catchup", "continue"], height=28, font_pt=11,
                               labels={"ask": "Ask", "catchup": "Catch up", "continue": "Continue"})
        self.catch.set_value(s.get("catch_up") or "ask")
        self.catch.changed.connect(self._on_catch)
        g.add(_row("When your iPhone comes back mid-route", self.catch, stacked=True))
        lay.addWidget(g)
        lay.addWidget(_wrap("Catch up jumps to where the route would be by now. Continue picks "
                            "up from the last spot the phone actually reached.", 11))

        lay.addSpacing(6)
        lay.addWidget(_caption("Map"))
        g = _Group()
        self.bright = Segmented(["Dim", "Normal", "Bright"], height=28, font_pt=11)
        self.bright.set_value(s.get("brightness", "Normal"))
        self.bright.changed.connect(lambda n: self._save("brightness", n, self.brightnessChanged))
        g.add(_row("Brightness", self.bright, "Dark appearance only.", stacked=True))
        sw = Switch(name="Pulsing location dot"); sw.set_on(s.get("pulse", True))
        sw.toggled.connect(lambda on: self._save("pulse", bool(on), self.pulseToggled))
        g.add(_row("Pulsing location dot", sw))
        sw = Switch(name="Walk pad"); sw.set_on(s.get("walk_pad", True))
        sw.toggled.connect(lambda on: self._save("walk_pad", bool(on), self.walkPadToggled))
        g.add(_row("Walk pad", sw, "Or walk with the arrow keys."))
        sw = Switch(name="GPS jitter"); sw.set_on(s.get("jitter", False))
        sw.toggled.connect(lambda on: self._save("jitter", bool(on), self.jitterToggled))
        g.add(_row("GPS jitter while holding a spot", sw,
                   "A few metres of wobble, like a real GPS fix."))
        lay.addWidget(g)

        lay.addSpacing(6)
        lay.addWidget(_caption("Help"))
        g = _Group()
        for k, v in (("Teleport", "Click the map, then Teleport here (T)."),
                     ("Route", "Click the map, then Route here (R). Click again to add stops."),
                     ("Pause / resume a route", "Space"),
                     ("Search", "⌘F: addresses, places, or coordinates like 33.42, -111.93."),
                     ("Stop spoofing", "⌘. in the app, or ⌃⌥⌘R from anywhere."),
                     ("Dismiss", "Esc")):
            row = QFrame()
            rl = QVBoxLayout(row); rl.setContentsMargins(14, 8, 14, 8); rl.setSpacing(1)
            rl.addWidget(label(k, 13, 600))
            rl.addWidget(_wrap(v))
            g.add(row)
        lay.addWidget(g)
        lay.addWidget(_wrap("Ever stuck at a fake location? Stop spoofing puts the real one "
                            "back. As a last resort, restarting the iPhone always does too."))

        lay.addSpacing(6)
        lay.addWidget(_caption("About"))
        lay.addWidget(_wrap("Spoofr sets your iPhone’s location to anywhere on the map, and "
                            "every app on the phone sees it. No jailbreak. Fine for development, "
                            "privacy and games; using it to defraud, or to defeat court-ordered "
                            "monitoring, can be illegal.", 12))
        lay.addWidget(_wrap("Version 1.1  ·  iOS 17 and later", 11, "faint"))

    # ---- preferences ----

    def _save(self, key, value, signal=None):
        self.settings[key] = value
        store.save(self.settings)
        if signal is not None:
            signal.emit(value)

    def _on_catch(self, v):
        self.settings["catch_up"] = None if v == "ask" else v
        store.save(self.settings)
        self.catchUpChanged.emit(v)

    def sync(self):
        """Re-read settings that change elsewhere (units in the route panel…)."""
        from .mapview import default_units
        self.units.set_value(self.settings.get("units") or default_units())
        self.catch.set_value(self.settings.get("catch_up") or "ask")

    def set_wireless_busy(self, busy: bool):
        self.wifi_btn.setEnabled(not busy)
        self.wifi_btn.setText("Setting up…" if busy else "Set up")
        if busy:
            self.set_wireless_status("Talking to the iPhone over the cable…", "warn")

    def set_wireless_status(self, text: str, role: str = "muted"):
        self.wifi_status.setText(text)
        set_role(self.wifi_status, role)
        self.wifi_status.setVisible(bool(text))

    def refresh_wireless(self, on: bool):
        if on:
            self.set_wireless_status("✓  Wi-Fi control is on. The cable is optional.", "good")
        else:
            self.set_wireless_status("")

    def retheme(self):
        self._title.setText(f"<span style='color:{theme.ACCENT}'>◉</span>&nbsp;&nbsp;Spoofr")

    # ---- slide ----

    def fit_height(self, h: int):
        self.resize(WIDTH, h)
        if self.parentWidget() is not None:
            self._scrim.setGeometry(self.parentWidget().rect())

    def is_open(self) -> bool:
        return self._open

    def toggle(self):
        self.close_menu() if self._open else self.open_menu()

    def open_menu(self):
        self._open = True
        self.sync()
        if self.parentWidget() is not None:
            self._scrim.setGeometry(self.parentWidget().rect())
        self._scrim.show(); self._scrim.raise_()
        self.show(); self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(0, self.y()))
        self._anim.start()

    def close_menu(self):
        if not self._open:
            return
        self._open = False
        self._scrim.hide()
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(-WIDTH, self.y()))
        self._anim.start()

    def _after_slide(self):
        if not self._open:
            self.hide()
