"""The slide-out side menu: Places (saved + recent), Route, Settings, About.

A child overlay of the main window that animates in from the left over a dim
scrim (click the scrim to close). It owns the shared settings dict (persisted via
store) and emits high-level signals for the actions the window performs.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPropertyAnimation, QSize, Qt, Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QInputDialog, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import store, theme
from .widgets import Segmented, Switch, hairline, icon_close

WIDTH = 328
RECENT_MAX = 10


def _label(text, size=13, color=theme.TEXT, weight=400, wrap=True):
    lb = QLabel(text)
    lb.setWordWrap(wrap)
    lb.setFont(theme.ui_font(size, weight=weight))
    lb.setStyleSheet(f"color: {color};")
    if wrap:
        # word-wrapped labels need a height-for-width size policy or a layout
        # clips their last line
        lb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return lb


def _caption(text):
    lb = QLabel(text.upper())
    f = theme.ui_font(11, weight=600)
    f.setLetterSpacing(f.SpacingType.PercentageSpacing, 106)
    lb.setFont(f)
    lb.setStyleSheet(f"color: {theme.FAINT};")
    return lb


def _row_button(text, height=38):
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(height)
    b.setStyleSheet(
        f"QPushButton {{ text-align: left; padding-left: 12px; border: none; border-radius: 9px;"
        f" background: transparent; color: {theme.TEXT}; font-weight: 500; }}"
        f"QPushButton:hover {{ background: {theme.GHOST_HI}; }}")
    return b


class _Scrim(QWidget):
    """Dims the window behind the open menu; a click on it closes the menu."""
    clicked = Signal()

    def paintEvent(self, e):
        QPainter(self).fillRect(self.rect(), QColor(4, 7, 12, 120))

    def mousePressEvent(self, e):
        self.clicked.emit()


class _PlaceRow(QFrame):
    """A clickable saved/recent place; its ☆/✕ buttons take their own clicks."""
    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
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
            line = hairline()
            self._lay.addWidget(line)
        self._lay.addWidget(w)
        return w


class Sidebar(QFrame):
    usePlace = Signal(float, float, str)
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
        self.setObjectName("Side")
        self.setStyleSheet(f"#Side {{ background: {theme.PANEL}; border-right: 1px solid {theme.BORDER}; }}")
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
        top.setContentsMargins(20, 16, 14, 10)
        title = QLabel(f"<span style='color:{theme.BLUE}'>◉</span>&nbsp;&nbsp;Spoofr")
        title.setFont(theme.ui_font(17, weight=700))
        close = QPushButton()
        close.setIcon(QIcon(icon_close()))
        close.setIconSize(QSize(14, 14))
        close.setProperty("variant", "icon")
        close.setFixedSize(30, 30)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close_menu)
        top.addWidget(title); top.addStretch(1); top.addWidget(close)
        root.addLayout(top)

        tabs = QHBoxLayout()
        tabs.setContentsMargins(16, 0, 16, 12)
        self._tabs = Segmented(["Places", "Route", "Settings", "About"], height=32, font_pt=12)
        self._tabs.changed.connect(lambda v: self._show(v.lower()))
        tabs.addWidget(self._tabs)
        root.addLayout(tabs)
        root.addWidget(hairline())

        self._stack = QStackedWidget()
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
        area.viewport().setAutoFillBackground(False)   # the panel colour shows through
        inner = QWidget()
        inner.setObjectName("SideInner")
        inner.setStyleSheet("#SideInner { background: transparent; }")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(16, 14, 16, 18); lay.setSpacing(8)
        area.setWidget(inner)
        # stretch lives at the end (added by the caller via _finish): aligning the
        # layout to the top instead is what clipped the wrapped labels
        return area, lay

    @staticmethod
    def _finish(lay: QVBoxLayout):
        lay.addStretch(1)

    def _build_places(self):
        area, lay = self._scroll()
        save = QPushButton("★   Save current spot")
        save.setProperty("variant", "soft"); save.setFixedHeight(36)
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(lambda: self.saveCurrent.emit())
        lay.addWidget(save)
        lay.addSpacing(8)
        lay.addWidget(_caption("Saved"))
        self._saved_group = _Group()
        lay.addWidget(self._saved_group)
        lay.addSpacing(8)
        lay.addWidget(_caption("Recent"))
        self._recent_group = _Group()
        lay.addWidget(self._recent_group)
        self._finish(lay)
        self.refresh_places()
        return area

    def _place_row(self, title, sub, lat, lon, label, on_delete=None, on_save=None):
        row = _PlaceRow()
        row.setObjectName("Prow")
        row.setStyleSheet(f"#Prow {{ background: transparent; border-radius: 12px; }}"
                          f"#Prow:hover {{ background: {theme.GHOST_HI}; }}")
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        rl = QHBoxLayout(row); rl.setContentsMargins(14, 7, 6, 7); rl.setSpacing(4)
        text = QVBoxLayout(); text.setSpacing(0)
        t = QLabel(title); t.setFont(theme.ui_font(13, weight=500))
        text.addWidget(t)
        if sub:
            s = QLabel(sub); s.setFont(theme.ui_font(11)); s.setStyleSheet(f"color: {theme.MUTED};")
            text.addWidget(s)
        rl.addLayout(text, 1)
        for glyph, cb, tip in (("☆", on_save, "Save this place"), ("✕", on_delete, "Remove")):
            if cb:
                x = QPushButton(glyph); x.setFixedSize(28, 28)
                x.setToolTip(tip)
                x.setCursor(Qt.CursorShape.PointingHandCursor)
                x.setStyleSheet(
                    f"QPushButton {{ border: none; border-radius: 8px; background: transparent;"
                    f" color: {theme.FAINT}; font-size: 13px; }}"
                    f"QPushButton:hover {{ background: {theme.ELEV}; color: {theme.TEXT}; }}")
                x.clicked.connect(cb)
                rl.addWidget(x)
        row.clicked.connect(lambda: self.usePlace.emit(lat, lon, label))
        return row

    def _clear_group(self, group: _Group):
        lay = group._lay
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def refresh_places(self):
        self._clear_group(self._saved_group)
        self._clear_group(self._recent_group)
        saved = self.settings.get("saved", [])
        recent = self.settings.get("recent", [])
        if not saved:
            self._saved_group.add(self._empty("Save a spot to jump back to it in one click."))
        for i, p in enumerate(saved):
            self._saved_group.add(self._place_row(
                p["name"], f"{p['lat']:.4f}, {p['lon']:.4f}", p["lat"], p["lon"], p["name"],
                on_delete=lambda _=False, i=i: self._delete_saved(i)))
        if not recent:
            self._recent_group.add(self._empty("Places you set show up here."))
        for p in recent:
            coords = f"{p['lat']:.4f}, {p['lon']:.4f}"
            name = p.get("name") or ""
            self._recent_group.add(self._place_row(
                name or coords, coords if name else "", p["lat"], p["lon"], name,
                on_save=lambda _=False, la=p["lat"], lo=p["lon"], nm=name: self._save_named(la, lo, nm)))
        self.placesChanged.emit()

    @staticmethod
    def _empty(text):
        lb = _label(text, 12, theme.MUTED)
        lb.setContentsMargins(14, 10, 14, 10)
        return lb

    def _delete_saved(self, idx: int):
        saved = self.settings.get("saved", [])
        if 0 <= idx < len(saved):
            saved.pop(idx)
            self.settings["saved"] = saved
            store.save(self.settings)
            self.refresh_places()

    def save_place(self, lat: float, lon: float, suggestion: str = ""):
        self._save_named(lat, lon, suggestion)

    def _save_named(self, lat: float, lon: float, suggestion: str = ""):
        name, ok = QInputDialog.getText(self, "Save place",
                                        f"Name this spot  ({lat:.4f}, {lon:.4f})",
                                        text=suggestion)
        name = (name or "").strip()
        if not ok or not name:
            return
        self.settings.setdefault("saved", []).insert(0, {"name": name, "lat": lat, "lon": lon})
        store.save(self.settings)
        self.refresh_places()

    def add_recent(self, lat: float, lon: float, name: str = ""):
        recent = [r for r in self.settings.get("recent", [])
                  if abs(r["lat"] - lat) > 1e-4 or abs(r["lon"] - lon) > 1e-4]
        entry = {"lat": lat, "lon": lon}
        if name:
            entry["name"] = name
        recent.insert(0, entry)
        self.settings["recent"] = recent[:RECENT_MAX]
        store.save(self.settings)
        self.refresh_places()

    def name_recent(self, lat: float, lon: float, name: str):
        """A recent's name arrived after it was logged (reverse geocode)."""
        for r in self.settings.get("recent", []):
            if abs(r["lat"] - lat) <= 1e-4 and abs(r["lon"] - lon) <= 1e-4 and not r.get("name"):
                r["name"] = name
                store.save(self.settings)
                self.refresh_places()
                return

    def recent_name(self, lat: float, lon: float) -> str:
        for r in self.settings.get("recent", []):
            if abs(r["lat"] - lat) <= 1e-4 and abs(r["lon"] - lon) <= 1e-4:
                return r.get("name") or ""
        return ""

    def _build_route(self):
        area, lay = self._scroll()
        lay.addWidget(_label("Switch to Route on the map, click where you want to end up, "
                             "and press Start. Your iPhone sets off from wherever it is.",
                             12, theme.MUTED))
        lay.addSpacing(4)
        g = _Group()
        g.add(self._switch_row("Follow real roads", self.settings.get("snap_roads", False),
                               self._on_snap,
                               "Walks take footpaths, drives take roads, using the pace you pick."))
        g.add(self._switch_row("Loop", False, lambda on: self.loopToggled.emit(bool(on)),
                               "Start over from the beginning at the end."))
        g.add(self._switch_row("Bounce", False, lambda on: self.bounceToggled.emit(bool(on)),
                               "Go there and come back, on repeat."))
        lay.addWidget(g)
        lay.addSpacing(8)
        lay.addWidget(_caption("GPX"))
        g2 = _Group()
        imp = _row_button("Import a GPX track…"); imp.clicked.connect(lambda: self.importGpx.emit())
        exp = _row_button("Export this route…"); exp.clicked.connect(lambda: self.exportGpx.emit())
        g2.add(imp); g2.add(exp)
        lay.addWidget(g2)
        lay.addWidget(_label("Import a recorded track to replay it; export saves what Start "
                             "would walk.", 12, theme.MUTED))
        self._finish(lay)
        return area

    def _on_snap(self, on):
        self.settings["snap_roads"] = bool(on); store.save(self.settings)
        self.snapToggled.emit(bool(on))

    def _build_settings(self):
        area, lay = self._scroll()
        lay.addWidget(_caption("Map"))
        g = _Group()
        bright = QFrame()
        bl = QHBoxLayout(bright); bl.setContentsMargins(14, 8, 10, 8)
        bl.addWidget(_label("Brightness", 13, wrap=False)); bl.addStretch(1)
        self._bright = Segmented(["Dim", "Normal", "Bright"], height=28, font_pt=11)
        self._bright.set_value(self.settings.get("brightness", "Normal"))
        self._bright.changed.connect(self._on_bright)
        bl.addWidget(self._bright)
        g.add(bright)
        g.add(self._switch_row("Pulsing location dot", self.settings.get("pulse", True),
                               self._on_pulse))
        lay.addWidget(g)
        lay.addSpacing(8)
        lay.addWidget(_caption("Realism"))
        g2 = _Group()
        g2.add(self._switch_row("GPS jitter", self.settings.get("jitter", False), self._on_jitter,
                                "Wobbles a held location a few metres, like a real GPS fix."))
        lay.addWidget(g2)
        lay.addSpacing(8)
        lay.addWidget(_caption("Wireless"))
        g3 = _Group()
        self._wifi_btn = _row_button("⚡  Go wireless (one-time)")
        self._wifi_btn.clicked.connect(lambda: self.goWireless.emit())
        g3.add(self._wifi_btn)
        lay.addWidget(g3)
        self._wifi_status = _label("", 12, theme.MUTED)
        self._wifi_status.setContentsMargins(4, 0, 4, 0)
        lay.addWidget(self._wifi_status)
        if self.settings.get("wireless_on"):
            self.set_wireless_status("✓  Wireless is on, the cable is optional.", theme.GREEN)
        else:
            self.set_wireless_status("With the cable in, click once. After that you can connect "
                                     "and control the iPhone over Wi-Fi.", theme.MUTED)
        self._finish(lay)
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
            self.set_wireless_status("Wireless is off. With the cable in, click above "
                                     "to enable it.", theme.MUTED)

    def _switch_row(self, text, on, cb, detail: str = ""):
        row = QFrame()
        rl = QHBoxLayout(row); rl.setContentsMargins(14, 9, 12, 9); rl.setSpacing(10)
        col = QVBoxLayout(); col.setSpacing(2)
        col.addWidget(_label(text, 13))
        if detail:
            col.addWidget(_label(detail, 11, theme.MUTED))
        rl.addLayout(col, 1)
        sw = Switch(); sw.set_on(on); sw.toggled.connect(cb)
        rl.addWidget(sw, 0, Qt.AlignmentFlag.AlignVCenter)
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
        lay.addWidget(_label("Spoofr", 17, weight=700))
        lay.addWidget(_label(
            "Sets your iPhone’s GPS to anywhere on the map. Every app on the phone "
            "sees that location. No jailbreak.", 13, theme.MUTED))
        lay.addSpacing(6)
        g = _Group()
        for k, v in (("Teleport", "Click the map, then Set location here"),
                     ("Walk", "Arrow keys, or hold a direction on the pad"),
                     ("Route", "Click a destination, then Start"),
                     ("Panic", "⌃⌥⌘R restores real GPS from anywhere")):
            row = QFrame()
            rl = QVBoxLayout(row); rl.setContentsMargins(14, 8, 14, 8); rl.setSpacing(1)
            rl.addWidget(_label(k, 13, weight=600))
            rl.addWidget(_label(v, 12, theme.MUTED))
            g.add(row)
        lay.addWidget(g)
        lay.addSpacing(6)
        lay.addWidget(_label(
            "Spoofing is fine for development, privacy and games. Using it to defraud, or to "
            "defeat court-ordered monitoring, can be illegal. How you use it is on you.",
            12, theme.FAINT))
        # deliberately open-ended: nothing here is pinned to an iOS version, and
        # the old "17–26" went stale the week iOS 27 shipped
        lay.addWidget(_label("Version 1.1  ·  iOS 17 and later", 12, theme.FAINT))
        self._finish(lay)
        return area

    # ---- nav + slide ----------------------------------------------------

    def _show(self, key: str):
        self._tabs.set_value(key.capitalize())
        self._stack.setCurrentWidget(self._sections[key])

    def show_section(self, key: str):
        self._show(key)
        self.open_menu()

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
            self.hide()      # fully closed → stop compositing the off-screen panel
