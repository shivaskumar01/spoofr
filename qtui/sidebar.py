"""The slide-out side menu: Places (saved + recent), Settings, About.

A child overlay of the main window that animates in from the left. It owns the
shared settings dict (persisted via store) and emits high-level signals for the
actions the window performs (use/save a place, brightness/pulse/jitter changes).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve, QPoint
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QInputDialog, QLabel, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import store, theme
from .widgets import Segmented, Switch

WIDTH = 320


def _label(text, size=13, color=theme.TEXT, weight=400, mono=False):
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setFont(theme.mono_font(10) if mono else theme.ui_font(size, weight=weight))
    lb.setStyleSheet(f"color: {color};")
    return lb


def _navbtn(text):
    b = QPushButton(text)
    b.setCheckable(True)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(38)
    b.setFont(theme.ui_font(13, weight=600))
    b.setStyleSheet(
        f"QPushButton {{ text-align: left; padding-left: 12px; border: none; border-radius: 9px;"
        f" background: transparent; color: {theme.TEXT}; }}"
        f"QPushButton:hover {{ background: {theme.GHOST}; }}"
        f"QPushButton:checked {{ background: {theme.GHOST}; }}")
    return b


class Sidebar(QFrame):
    usePlace = Signal(float, float)
    saveCurrent = Signal()
    brightnessChanged = Signal(str)
    pulseToggled = Signal(bool)
    jitterToggled = Signal(bool)
    snapToggled = Signal(bool)
    loopToggled = Signal(bool)
    bounceToggled = Signal(bool)
    importGpx = Signal()
    exportGpx = Signal()
    placesChanged = Signal()
    goWireless = Signal()

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setObjectName("Bar")
        self.setStyleSheet(f"#Bar {{ background: {theme.PANEL}; border-right: 1px solid {theme.BORDER}; }}")
        self.setFixedWidth(WIDTH)
        self._open = False
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(190)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._after_slide)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(20, 20, 16, 8)
        title = _label("Spoofr", 18, weight=700)
        close = QPushButton("✕")
        close.setObjectName("x"); close.setFixedSize(32, 32)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(
            f"QPushButton {{ border: none; border-radius: 8px; background: transparent; color: {theme.MUTED}; }}"
            f"QPushButton:hover {{ background: {theme.GHOST}; }}")
        close.clicked.connect(self.close_menu)
        top.addWidget(title); top.addStretch(1); top.addWidget(close)
        root.addLayout(top)

        nav = QVBoxLayout()
        nav.setContentsMargins(14, 6, 14, 6); nav.setSpacing(2)
        self._nav = {}
        self._stack = QStackedWidget()
        for key, lbl in (("places", "Places"), ("route", "Route"), ("settings", "Settings"), ("about", "About")):
            b = _navbtn(lbl)
            b.clicked.connect(lambda _=False, k=key: self._show(k))
            nav.addWidget(b)
            self._nav[key] = b
        root.addLayout(nav)
        hair = QFrame(); hair.setObjectName("Hairline"); hair.setFixedHeight(1)
        root.addWidget(hair)
        root.addWidget(self._stack, 1)

        self._sections = {
            "places": self._build_places(),
            "route": self._build_route(),
            "settings": self._build_settings(),
            "about": self._build_about(),
        }
        for s in self._sections.values():
            self._stack.addWidget(s)
        self._show("places")

    # ---- sections -------------------------------------------------------

    def _scroll(self) -> tuple[QScrollArea, QVBoxLayout]:
        area = QScrollArea(); area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget(); lay = QVBoxLayout(inner)
        lay.setContentsMargins(18, 10, 18, 16); lay.setSpacing(6)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        area.setWidget(inner)
        return area, lay

    def _build_places(self):
        area, lay = self._scroll()
        lay.addWidget(_label("Places", 15, weight=700))
        lay.addWidget(_label("Save the spots you use; recents are tracked automatically.",
                             12, theme.MUTED))
        save = QPushButton("★  Save current spot")
        save.setProperty("variant", "soft"); save.setFixedHeight(34)
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(lambda: self.saveCurrent.emit())
        lay.addWidget(save)
        lay.addSpacing(6)
        lay.addWidget(_label("SAVED", mono=True, color=theme.MUTED))
        self._saved_box = QVBoxLayout(); self._saved_box.setSpacing(1)
        lay.addLayout(self._saved_box)
        lay.addSpacing(10)
        lay.addWidget(_label("RECENT", mono=True, color=theme.MUTED))
        self._recent_box = QVBoxLayout(); self._recent_box.setSpacing(1)
        lay.addLayout(self._recent_box)
        self.refresh_places()
        return area

    def _place_row(self, label, lat, lon, on_delete=None, on_save=None):
        row = QFrame()
        rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(2)
        b = QPushButton(label)
        b.setCursor(Qt.CursorShape.PointingHandCursor); b.setFixedHeight(32)
        b.setStyleSheet(
            f"QPushButton {{ text-align: left; padding-left: 10px; border: none; border-radius: 8px;"
            f" background: transparent; color: {theme.TEXT}; }}"
            f"QPushButton:hover {{ background: {theme.GHOST}; }}")
        b.clicked.connect(lambda: self.usePlace.emit(lat, lon))
        rl.addWidget(b, 1)
        for glyph, cb in (("★", on_save), ("✕", on_delete)):
            if cb:
                x = QPushButton(glyph); x.setFixedSize(30, 30)
                x.setCursor(Qt.CursorShape.PointingHandCursor)
                x.setStyleSheet(
                    f"QPushButton {{ border: none; border-radius: 8px; background: transparent; color: {theme.MUTED}; }}"
                    f"QPushButton:hover {{ background: {theme.GHOST}; color: {theme.TEXT}; }}")
                x.clicked.connect(cb)
                rl.addWidget(x)
        return row

    def refresh_places(self):
        for box in (self._saved_box, self._recent_box):
            while box.count():
                w = box.takeAt(0).widget()
                if w:
                    w.deleteLater()
        saved = self.settings.get("saved", [])
        recent = self.settings.get("recent", [])
        if not saved:
            self._saved_box.addWidget(_label("No saved spots yet.", 12, theme.MUTED))
        for i, p in enumerate(saved):
            self._saved_box.addWidget(self._place_row(
                p["name"], p["lat"], p["lon"], on_delete=lambda i=i: self._delete_saved(i)))
        if not recent:
            self._recent_box.addWidget(_label("Nothing recent.", 12, theme.MUTED))
        for p in recent:
            self._recent_box.addWidget(self._place_row(
                f"{p['lat']:.4f}, {p['lon']:.4f}", p["lat"], p["lon"],
                on_save=lambda la=p["lat"], lo=p["lon"]: self._save_named(la, lo)))
        self.placesChanged.emit()

    def _delete_saved(self, idx: int):
        saved = self.settings.get("saved", [])
        if 0 <= idx < len(saved):
            saved.pop(idx)
            self.settings["saved"] = saved
            store.save(self.settings)
            self.refresh_places()

    def save_place(self, lat: float, lon: float):
        self._save_named(lat, lon)

    def _save_named(self, lat: float, lon: float):
        name, ok = QInputDialog.getText(self, "Save place", f"Name this spot  ({lat:.4f}, {lon:.4f}):")
        name = (name or "").strip()
        if not ok or not name:
            return
        self.settings.setdefault("saved", []).insert(0, {"name": name, "lat": lat, "lon": lon})
        store.save(self.settings)
        self.refresh_places()

    def add_recent(self, lat: float, lon: float):
        recent = [r for r in self.settings.get("recent", [])
                  if abs(r["lat"] - lat) > 1e-4 or abs(r["lon"] - lon) > 1e-4]
        recent.insert(0, {"lat": lat, "lon": lon})
        self.settings["recent"] = recent[:10]
        store.save(self.settings)
        self.refresh_places()

    def _build_route(self):
        area, lay = self._scroll()
        lay.addWidget(_label("Route", 15, weight=700))
        lay.addWidget(_label("In Route mode, click the map to drop waypoints, then press Start. "
                             "Set the pace with the presets.", 12, theme.MUTED))
        lay.addSpacing(4)
        lay.addWidget(self._switch_row("Snap route to roads",
                                       self.settings.get("snap_roads", False), self._on_snap))
        lay.addWidget(_label("Follows real streets between waypoints, using the preset’s profile "
                             "(walk/cycle/drive).", 12, theme.MUTED))
        lay.addSpacing(4)
        lay.addWidget(self._switch_row("Loop the route", False,
                                       lambda on: self.loopToggled.emit(bool(on))))
        lay.addWidget(self._switch_row("Bounce (there and back)", False,
                                       lambda on: self.bounceToggled.emit(bool(on))))
        lay.addSpacing(8)
        lay.addWidget(_label("GPX", mono=True, color=theme.MUTED))
        imp = QPushButton("Import GPX…"); imp.setProperty("variant", "soft"); imp.setFixedHeight(34)
        imp.setCursor(Qt.CursorShape.PointingHandCursor)
        imp.clicked.connect(lambda: self.importGpx.emit())
        exp = QPushButton("Export route…"); exp.setProperty("variant", "soft"); exp.setFixedHeight(34)
        exp.setCursor(Qt.CursorShape.PointingHandCursor)
        exp.clicked.connect(lambda: self.exportGpx.emit())
        lay.addWidget(imp); lay.addWidget(exp)
        lay.addWidget(_label("Import a recorded track to replay it; export the waypoints you’ve dropped.",
                             12, theme.MUTED))
        return area

    def _on_snap(self, on):
        self.settings["snap_roads"] = bool(on); store.save(self.settings)
        self.snapToggled.emit(bool(on))

    def _build_settings(self):
        area, lay = self._scroll()
        lay.addWidget(_label("Settings", 15, weight=700))
        lay.addSpacing(4)
        lay.addWidget(_label("Map brightness", 13, theme.MUTED))
        self._bright = Segmented(["Dim", "Normal", "Bright"], height=32, font_pt=12)
        self._bright.set_value(self.settings.get("brightness", "Normal"))
        self._bright.changed.connect(self._on_bright)
        lay.addWidget(self._bright)
        lay.addSpacing(10)
        lay.addWidget(self._switch_row("Pulsing live marker",
                                       self.settings.get("pulse", True), self._on_pulse))
        lay.addWidget(self._switch_row("GPS jitter (look human)",
                                       self.settings.get("jitter", False), self._on_jitter))
        lay.addWidget(_label("Jitter wobbles a held location a few metres so apps see a natural, "
                             "noisy GPS fix.", 12, theme.MUTED))
        lay.addSpacing(12)
        lay.addWidget(_label("WIRELESS", mono=True, color=theme.MUTED))
        self._wifi_btn = QPushButton("⚡  Go wireless (one-time)")
        self._wifi_btn.setProperty("variant", "soft")
        self._wifi_btn.setFixedHeight(34)
        self._wifi_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wifi_btn.clicked.connect(lambda: self.goWireless.emit())
        lay.addWidget(self._wifi_btn)
        initial = ("✓  Wireless is on, the cable is optional."
                   if self.settings.get("wireless_on")
                   else "With the cable in, click once. After that you can connect and "
                        "control the iPhone over Wi-Fi, no cable.")
        self._wifi_status = _label(initial, 12,
                                   theme.GREEN if self.settings.get("wireless_on") else theme.MUTED)
        lay.addWidget(self._wifi_status)
        return area

    def set_wireless_busy(self, busy: bool):
        self._wifi_btn.setEnabled(not busy)
        self._wifi_btn.setText("Enabling…" if busy else "⚡  Go wireless (one-time)")
        if busy:
            self.set_wireless_status("Talking to the iPhone over the cable…", theme.AMBER)

    def set_wireless_status(self, text: str, color: str = theme.MUTED):
        self._wifi_status.setText(text)
        self._wifi_status.setStyleSheet(f"color: {color};")

    def refresh_wireless(self, on: bool):
        """Called after each connect with the device's actual switch state."""
        if on:
            self.set_wireless_status("✓  Wireless is on, the cable is optional.", theme.GREEN)
        else:
            self.set_wireless_status("Wireless is off, with the cable in, click above "
                                     "to enable it.", theme.MUTED)

    def _switch_row(self, text, on, cb):
        row = QFrame()
        rl = QHBoxLayout(row); rl.setContentsMargins(0, 4, 0, 4)
        rl.addWidget(_label(text, 13))
        rl.addStretch(1)
        sw = Switch(); sw.set_on(on); sw.toggled.connect(cb)
        rl.addWidget(sw)
        return row

    def _on_bright(self, name):
        self.settings["brightness"] = name; store.save(self.settings)
        self.brightnessChanged.emit(name)

    def _on_pulse(self, on):
        self.settings["pulse"] = bool(on); store.save(self.settings)
        self.pulseToggled.emit(bool(on))

    def _on_jitter(self, on):
        self.settings["jitter"] = bool(on); store.save(self.settings)
        self.jitterToggled.emit(bool(on))

    def _build_about(self):
        area, lay = self._scroll()
        lay.addWidget(_label("About Spoofr", 15, weight=700))
        lay.addWidget(_label(
            "Spoofr sets your iPhone’s GPS to anywhere on the map, every app on your phone "
            "then sees that location. No jailbreak.\n\n"
            "Spoofing is fine for development, privacy and games. Using it to defraud or to "
            "defeat court-ordered monitoring can be illegal, how you use it is on you.",
            13, theme.MUTED))
        lay.addSpacing(8)
        # deliberately open-ended: nothing here is pinned to an iOS version, and
        # the old "17–26" went stale the week iOS 27 shipped
        lay.addWidget(_label("Version 1.0  ·  iOS 17 and later", 12, theme.MUTED))
        return area

    # ---- nav + slide ----------------------------------------------------

    def _show(self, key: str):
        for k, b in self._nav.items():
            b.setChecked(k == key)
        self._stack.setCurrentWidget(self._sections[key])

    def fit_height(self, h: int):
        self.resize(WIDTH, h)

    def is_open(self) -> bool:
        return self._open

    def toggle(self):
        self.close_menu() if self._open else self.open_menu()

    def open_menu(self):
        self._open = True
        self.show(); self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(0, self.y()))
        self._anim.start()

    def close_menu(self):
        if not self._open:
            return
        self._open = False
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(-WIDTH, self.y()))
        self._anim.start()

    def _after_slide(self):
        if not self._open:
            self.hide()      # fully closed → stop compositing the off-screen panel
