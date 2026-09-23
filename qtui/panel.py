"""The control panel: the floating card on the right of the map.

It changes with what you are doing:
    home      Places (recent + starred) and saved Routes
    pin       the dropped pin: address, coordinates, distance, Teleport / Route
    builder   a route being planned: stops, path style, speed, options, summary
    running   a route in progress: progress, remaining, speed, arrival, pause/stop
    summary   a route that finished

"Stop spoofing" sits at the bottom whenever the iPhone is at a fake location.
The panel draws; the controller (`ctl`, the MapScreen) decides.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget, QLineEdit,
)

from . import theme
from .widgets import (
    Collapsible, ElidedLabel, IconButton, Segmented, Switch, button, hairline, icon_close,
    icon_drag, icon_star, icon_undo, label, set_role,
)

WIDTH = 328
PRESETS_KMH = {"Walk": 5.0, "Jog": 9.0, "Cycle": 18.0, "Drive": 50.0}
KMH_PER_MPH = 1.609344
FAST_KMH = 200.0


# ---- formatting ------------------------------------------------------------

def fmt_distance(m: float | None, units: str) -> str:
    if m is None:
        return "—"
    if units == "mph":
        mi = m / 1609.344
        if mi < 0.1:
            return f"{m * 3.28084:,.0f} ft"
        return f"{mi:.2f} mi" if mi < 10 else f"{mi:,.1f} mi"
    if m < 1000:
        return f"{m:,.0f} m"
    return f"{m / 1000:.2f} km" if m < 10_000 else f"{m / 1000:,.1f} km"


def fmt_duration(s: float | None) -> str:
    if s is None:
        return "—"
    m = int(round(s / 60.0))
    if s < 60:
        return f"{int(s)} s" if s >= 1 else "<1 s"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def fmt_speed(kmh: float, units: str) -> str:
    v = kmh / KMH_PER_MPH if units == "mph" else kmh
    return f"{v:.1f}" if v < 10 else f"{v:.0f}"


def fmt_clock(epoch: float) -> str:
    return time.strftime("%-I:%M %p", time.localtime(epoch))


# ---- building blocks ---------------------------------------------------------

def _row_btn(factory, tip, cb, size=26, px=14, color=None):
    b = IconButton(factory, tip, size=size, icon_px=px, color=color or (lambda: theme.FAINT))
    b.clicked.connect(cb)
    return b


class _Row(QFrame):
    """A clickable list row with a title, an optional subtitle and small actions."""
    clicked = Signal()

    def __init__(self, title: str, sub: str = "", actions=()):
        super().__init__()
        self.setObjectName("Row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 4, 6)
        lay.setSpacing(2)
        col = QVBoxLayout(); col.setSpacing(0)
        t = ElidedLabel(title); t.setFont(theme.ui_font(13, 500))
        col.addWidget(t)
        if sub:
            s = ElidedLabel(sub); s.setFont(theme.ui_font(11)); s.setProperty("role", "muted")
            col.addWidget(s)
        lay.addLayout(col, 1)
        for a in actions:
            lay.addWidget(a)
        self.setAccessibleName(title)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


def _caption(text: str) -> QLabel:
    lb = label(text.upper(), 10, 700, "faint")
    f = lb.font(); f.setLetterSpacing(f.SpacingType.PercentageSpacing, 108); lb.setFont(f)
    return lb


def _empty(text: str) -> QLabel:
    lb = label(text, 12, role="muted", wrap=True)
    lb.setContentsMargins(4, 4, 4, 8)
    return lb


def _clear(lay):
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.deleteLater()
        elif it.layout() is not None:
            _clear(it.layout())


class _Page(QWidget):
    """A page with a header (title + close) over scrollable content."""

    def __init__(self, title: str = "", closable: bool = False, on_close=None):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(16, 12, 8, 6)
        self.title = ElidedLabel(title)
        self.title.setFont(theme.ui_font(15, 700))
        head.addWidget(self.title, 1)
        if closable:
            x = IconButton(icon_close, "Close (Esc)", size=28, icon_px=13,
                           color=lambda: theme.MUTED)
            x.clicked.connect(on_close)
            head.addWidget(x)
        outer.addLayout(head)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.area.viewport().setAutoFillBackground(False)
        self.inner = QWidget()
        self.inner.setObjectName("PageInner")
        self.inner.setStyleSheet("#PageInner { background: transparent; }")
        self.body = QVBoxLayout(self.inner)
        self.body.setContentsMargins(16, 2, 16, 14)
        self.body.setSpacing(8)
        self.area.setWidget(self.inner)
        outer.addWidget(self.area, 1)
        # primary actions live below the scroll area, so they never scroll away
        self.foot = QVBoxLayout()
        self.foot.setContentsMargins(16, 6, 16, 12)
        self.foot.setSpacing(6)
        outer.addLayout(self.foot)

    def content_height(self) -> int:
        foot = self.foot.sizeHint().height() if self.foot.count() else 0
        return self.inner.sizeHint().height() + 46 + foot


# ---- speed -------------------------------------------------------------------

class SpeedControl(QWidget):
    """Walk / Jog / Cycle / Drive chips, or Custom with a slider and a typed value.
    Units toggle between km/h and mph; the speed itself is always km/h inside."""

    speedChanged = Signal(float)      # km/h
    unitsChanged = Signal(str)        # "kmh" | "mph"

    def __init__(self):
        super().__init__()
        self._kmh = 5.0
        self._units = "kmh"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.chips = Segmented([*PRESETS_KMH, "Custom"], height=30, font_pt=11)
        self.chips.changed.connect(self._on_chip)
        lay.addWidget(self.chips)
        row = QHBoxLayout(); row.setSpacing(8)
        self.value = QLineEdit()
        self.value.setObjectName("Field")
        self.value.setFixedWidth(68)
        self.value.setFont(theme.num_font(13, 600))
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.value.setValidator(QDoubleValidator(0.1, 2000.0, 1, self))
        self.value.setAccessibleName("Speed")
        self.value.editingFinished.connect(self._on_typed)
        self.unit = Segmented(["kmh", "mph"], height=26, font_pt=10,
                              labels={"kmh": "km/h", "mph": "mph"})
        self.unit.changed.connect(self._on_unit)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(1, 300)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.setAccessibleName("Custom speed")
        self.slider.valueChanged.connect(self._on_slide)
        row.addWidget(self.value)
        row.addWidget(self.unit)
        row.addWidget(self.slider, 1)
        lay.addLayout(row)
        self.warn = label("Faster than any car on the road. Allowed, just so you know.",
                          11, role="warn", wrap=True)
        self.warn.hide()
        lay.addWidget(self.warn)
        self._sync()

    def set_state(self, kmh: float, units: str):
        self._kmh, self._units = float(kmh), units
        self.unit.set_value(units)
        preset = next((k for k, v in PRESETS_KMH.items() if abs(v - kmh) < 0.05), "Custom")
        self.chips.set_value(preset)
        self._sync()

    def kmh(self) -> float:
        return self._kmh

    def _display(self, kmh: float) -> float:
        return kmh / KMH_PER_MPH if self._units == "mph" else kmh

    def _sync(self):
        self.value.setText(fmt_speed(self._kmh, self._units))
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(min(300, max(1, self._display(self._kmh))))))
        self.slider.blockSignals(False)
        self.warn.setVisible(self._kmh > FAST_KMH)

    def _set(self, kmh: float):
        kmh = max(0.5, min(2000.0, kmh))
        if abs(kmh - self._kmh) < 1e-6:
            return
        self._kmh = kmh
        self._sync()
        self.speedChanged.emit(kmh)

    def _on_chip(self, name: str):
        if name in PRESETS_KMH:
            self._set(PRESETS_KMH[name])

    def _on_typed(self):
        try:
            v = float(self.value.text().replace(",", "."))
        except ValueError:
            self._sync()
            return
        self.chips.set_value("Custom")
        self._set(v * KMH_PER_MPH if self._units == "mph" else v)

    def _on_slide(self, v: int):
        self.chips.set_value("Custom")
        self._set(v * KMH_PER_MPH if self._units == "mph" else float(v))

    def _on_unit(self, u: str):
        self._units = u
        self._sync()
        self.unitsChanged.emit(u)


# ---- pages -------------------------------------------------------------------

class HomePage(_Page):
    def __init__(self, ctl):
        super().__init__("Spoofr")
        self.ctl = ctl
        self.tabs = Segmented(["Places", "Routes"], height=30, font_pt=12)
        self.tabs.changed.connect(lambda _v: self.refresh())
        self.body.addWidget(self.tabs)
        self.hint = label("Click the map to drop a pin.", 12, role="muted", wrap=True)
        self.body.addWidget(self.hint)
        self.list = QVBoxLayout(); self.list.setSpacing(2)
        self.body.addLayout(self.list)
        self.body.addStretch(1)

    def refresh(self):
        _clear(self.list)
        if self.tabs.value() == "Places":
            self._places()
        else:
            self._routes()

    def _places(self):
        starred, recent = self.ctl.places()
        self.list.addWidget(_caption("Starred"))
        if not starred:
            self.list.addWidget(_empty("Star a place on its pin card to keep it here."))
        for i, p in enumerate(starred):
            r = _Row(p["name"], f"{p['lat']:.5f}, {p['lon']:.5f}", actions=(
                _row_btn(lambda c: _pencil(c), "Rename", lambda _=False, i=i: self.ctl.rename_place(i)),
                _row_btn(icon_close, "Delete", lambda _=False, i=i: self.ctl.delete_place(i)),
            ))
            r.clicked.connect(lambda p=p: self.ctl.use_place(p))
            self.list.addWidget(r)
        self.list.addSpacing(8)
        self.list.addWidget(_caption("Recent"))
        if not recent:
            self.list.addWidget(_empty("Places you teleport to show up here."))
        for p in recent:
            coords = f"{p['lat']:.5f}, {p['lon']:.5f}"
            r = _Row(p.get("name") or coords, coords if p.get("name") else "", actions=(
                _row_btn(lambda c: icon_star(c), "Star", lambda _=False, p=p: self.ctl.star_place(p)),
            ))
            r.clicked.connect(lambda p=p: self.ctl.use_place(p))
            self.list.addWidget(r)

    def _routes(self):
        routes = self.ctl.saved_routes()
        if not routes:
            self.list.addWidget(_empty("Saved routes show up here. Build one with Route here on "
                                       "a pin, then Save."))
        for i, r in enumerate(routes):
            sub = r.get("summary", "")
            row = _Row(r["name"], sub, actions=(
                _row_btn(lambda c: _pencil(c), "Rename", lambda _=False, i=i: self.ctl.rename_route(i)),
                _row_btn(lambda c: _copy_icon(c), "Duplicate", lambda _=False, i=i: self.ctl.duplicate_route(i)),
                _row_btn(icon_close, "Delete", lambda _=False, i=i: self.ctl.delete_route(i)),
            ))
            row.clicked.connect(lambda i=i: self.ctl.load_route(i))
            self.list.addWidget(row)
        self.list.addSpacing(6)
        imp = button("Import GPX…", "soft", height=30)
        imp.clicked.connect(self.ctl.import_gpx)
        self.list.addWidget(imp)


def _pencil(color):
    from PySide6.QtCore import QPointF
    from .widgets import _canvas, _pen
    pm, p = _canvas(14)
    p.setPen(_pen(color, 1.5))
    p.drawLine(QPointF(3.5, 10.5), QPointF(10.5, 3.5))
    p.drawLine(QPointF(2.8, 11.2), QPointF(3.6, 8.8))
    p.end()
    return pm


def _copy_icon(color):
    from PySide6.QtCore import QRectF
    from .widgets import _canvas, _pen
    pm, p = _canvas(14)
    p.setPen(_pen(color, 1.3))
    p.drawRoundedRect(QRectF(2.5, 4.5, 7, 7), 1.5, 1.5)
    p.drawRoundedRect(QRectF(5, 2, 7, 7), 1.5, 1.5)
    p.end()
    return pm


class PinPage(_Page):
    def __init__(self, ctl):
        super().__init__("Dropped pin", closable=True, on_close=ctl.close_pin)
        self.ctl = ctl
        self.address = label("", 12, role="muted", wrap=True)
        self.body.addWidget(self.address)
        row = QHBoxLayout(); row.setSpacing(6)
        self.coords = QPushButton("")
        self.coords.setProperty("variant", "row")
        self.coords.setFont(theme.mono_font(11))
        self.coords.setFixedHeight(26)
        self.coords.setCursor(Qt.CursorShape.PointingHandCursor)
        self.coords.setToolTip("Copy coordinates")
        self.coords.setAccessibleName("Copy coordinates")
        self.coords.clicked.connect(ctl.copy_pin_coords)
        row.addWidget(self.coords, 1)
        self._starred = False
        self.star = IconButton(lambda c: icon_star(c, filled=self._starred), "Star this place",
                               size=30, icon_px=16, color=self._star_color)
        self.star.clicked.connect(ctl.toggle_star_pin)
        row.addWidget(self.star)
        self.body.addLayout(row)
        self.distance = label("", 12, role="muted", num=True)
        self.body.addWidget(self.distance)
        self.body.addSpacing(4)
        self.teleport = button("Teleport here", "primary", height=36, tip="Teleport here (T)")
        self.teleport.clicked.connect(ctl.teleport_pin)
        self.route = button("Route here", "soft", height=36, tip="Route here (R)")
        self.route.clicked.connect(ctl.route_pin)
        pair = QHBoxLayout(); pair.setSpacing(8)
        pair.addWidget(self.teleport, 1); pair.addWidget(self.route, 1)
        self.body.addLayout(pair)
        self.drag_hint = label("Drag the pin to fine-tune it. Esc dismisses.", 11, role="faint", wrap=True)
        self.body.addWidget(self.drag_hint)
        self.body.addStretch(1)

    def _star_color(self):
        return theme.AMBER if self._starred else theme.MUTED

    def show_info(self, title: str, address: str, lat: float, lon: float,
                  distance: str, starred: bool):
        self.title.setText(title)
        self.title.update()
        self.address.setText(address)
        self.address.setVisible(bool(address))
        self.coords.setText(f"{lat:.6f}, {lon:.6f}")
        self.distance.setText(distance)
        self.distance.setVisible(bool(distance))
        self.set_starred(starred)

    def set_starred(self, on: bool):
        self._starred = on
        self.star.setToolTip("Unstar" if on else "Star this place")
        self.star.refresh_icon()


class _StopList(QListWidget):
    """Stops you can drag into a new order."""
    reordered = Signal(list)           # new order, as indexes into the old list

    def __init__(self):
        super().__init__()
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setSpacing(1)
        self.model().rowsMoved.connect(self._moved)

    def _moved(self, *_):
        order = [self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())]
        self.reordered.emit(order)

    def fit(self):
        h = sum(self.sizeHintForRow(i) + 2 for i in range(self.count()))
        self.setFixedHeight(max(0, h + 2))


def _stop_row(badge: str, name: str, on_remove=None, handle=True) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(2, 3, 2, 3)
    lay.setSpacing(6)
    if handle:
        h = QLabel(); h.setPixmap(icon_drag(theme.FAINT)); h.setToolTip("Drag to reorder")
        lay.addWidget(h)
    else:
        lay.addSpacing(14)
    b = QLabel(badge)
    b.setFixedSize(20, 20)
    b.setAlignment(Qt.AlignmentFlag.AlignCenter)
    b.setFont(theme.ui_font(10, 700))
    b.setStyleSheet(f"background: {theme.ACCENT_SOFT}; color: {theme.ACCENT}; border-radius: 10px;")
    lay.addWidget(b)
    t = ElidedLabel(name)
    t.setFont(theme.ui_font(12, 500))
    lay.addWidget(t, 1)
    if on_remove:
        x = _row_btn(icon_close, "Remove this stop", on_remove, size=24, px=12)
        lay.addWidget(x)
    return w


class BuilderPage(_Page):
    def __init__(self, ctl):
        super().__init__("Route", closable=True, on_close=ctl.close_builder)
        self.ctl = ctl
        b = self.body
        self.start_row = QVBoxLayout()
        b.addLayout(self.start_row)
        self.stops = _StopList()
        self.stops.reordered.connect(ctl.reorder_stops)
        b.addWidget(self.stops)
        self.trace = QHBoxLayout()
        b.addLayout(self.trace)
        add = QHBoxLayout()
        self.add_btn = button("＋  Add stop", "link", height=26, tip="Then click the map")
        self.add_btn.clicked.connect(ctl.add_stop_hint)
        self.click_hint = label("Click the map to add a stop.", 11, role="faint")
        add.addWidget(self.add_btn); add.addWidget(self.click_hint, 1)
        b.addLayout(add)

        b.addWidget(_caption("Path"))
        self.style = Segmented(["roads", "straight", "draw"], height=30, font_pt=11,
                               labels={"roads": "Follow roads", "straight": "Straight", "draw": "Draw"})
        self.style.changed.connect(ctl.set_path_style)
        b.addWidget(self.style)
        self.profile = Segmented(["walking", "cycling", "driving"], height=28, font_pt=11,
                                 labels={"walking": "Walking", "cycling": "Cycling", "driving": "Driving"})
        self.profile.changed.connect(ctl.set_profile)
        b.addWidget(self.profile)
        self.router = QHBoxLayout()
        self.router_msg = label("", 11, role="muted", wrap=True)
        self.straight_btn = button("Use a straight line", "link", height=24)
        self.straight_btn.clicked.connect(ctl.use_straight_line)
        self.router.addWidget(self.router_msg, 1)
        self.router.addWidget(self.straight_btn)
        b.addLayout(self.router)

        b.addWidget(_caption("Speed"))
        self.speed = SpeedControl()
        self.speed.speedChanged.connect(ctl.set_speed_kmh)
        self.speed.unitsChanged.connect(ctl.set_units)
        b.addWidget(self.speed)

        opts = QWidget()
        ol = QGridLayout(opts)
        ol.setContentsMargins(4, 0, 0, 0)
        ol.setHorizontalSpacing(8); ol.setVerticalSpacing(8)
        ol.addWidget(label("At the end", 12), 0, 0)
        self.end = Segmented(["stop", "loop", "pingpong"], height=26, font_pt=10,
                             labels={"stop": "Stop", "loop": "Loop", "pingpong": "Ping-pong"})
        self.end.changed.connect(lambda v: ctl.set_options(end=v))
        ol.addWidget(self.end, 0, 1)
        ol.addWidget(label("Laps", 12), 1, 0)
        laps = QHBoxLayout()
        self.laps = QSpinBox(); self.laps.setRange(1, 999); self.laps.setFixedWidth(64)
        self.laps.setAccessibleName("Laps")
        self.laps.valueChanged.connect(lambda v: ctl.set_options(laps=v))
        self.forever = Switch(name="Repeat forever")
        self.forever.toggled.connect(lambda on: ctl.set_options(forever=on))
        laps.addWidget(self.laps); laps.addSpacing(6)
        laps.addWidget(label("Forever", 12, role="muted")); laps.addWidget(self.forever); laps.addStretch(1)
        ol.addLayout(laps, 1, 1)
        ol.addWidget(label("Pause at stops", 12), 2, 0)
        dw = QHBoxLayout()
        self.dwell = QSpinBox(); self.dwell.setRange(0, 3600); self.dwell.setSuffix(" s")
        self.dwell.setFixedWidth(76); self.dwell.setAccessibleName("Pause at each stop, seconds")
        self.dwell.valueChanged.connect(lambda v: ctl.set_options(dwell_s=v))
        dw.addWidget(self.dwell); dw.addStretch(1)
        ol.addLayout(dw, 2, 1)
        ol.addWidget(label("Vary speed ±10%", 12), 3, 0)
        self.variation = Switch(name="Vary speed")
        self.variation.toggled.connect(lambda on: ctl.set_options(variation=on))
        ol.addWidget(self.variation, 3, 1, Qt.AlignmentFlag.AlignLeft)
        ol.addWidget(label("GPS jitter", 12), 4, 0)
        self.jitter = Switch(name="GPS jitter")
        self.jitter.toggled.connect(lambda on: ctl.set_options(jitter=on))
        ol.addWidget(self.jitter, 4, 1, Qt.AlignmentFlag.AlignLeft)
        note = label("iOS works out the heading from the movement itself; there is no "
                     "separate heading to send.", 11, role="faint", wrap=True)
        ol.addWidget(note, 5, 0, 1, 2)
        self.more = Collapsible("More options", opts)
        b.addWidget(self.more)

        b.addStretch(1)
        f = self.foot
        f.addWidget(hairline())
        self.summary = label("", 13, 600, num=True)
        self.summary.setAccessibleName("Route summary")
        f.addWidget(self.summary)
        # what the phone needs to keep moving: always in view before Start
        self.link_note = label("", 11, role="muted", wrap=True)
        f.addWidget(self.link_note)
        self.start = button("Start route", "primary", height=36)
        self.start.clicked.connect(ctl.start_route)
        f.addWidget(self.start)
        io = QHBoxLayout()
        self.save = button("Save route", "soft", height=30)
        self.save.clicked.connect(ctl.save_route)
        self.export = button("Export GPX…", "link", height=30)
        self.export.clicked.connect(ctl.export_route)
        io.addWidget(self.save, 1); io.addWidget(self.export)
        f.addLayout(io)

    def refresh(self, d, start_name: str, plan_state: dict, units: str, link_note: str):
        """Redraw from the draft (`d`) and the plan state (distance, status…)."""
        _clear(self.start_row)
        self.start_row.addWidget(_stop_row("S", "Start · " + start_name, handle=False))
        self.stops.clear()
        draw = d.style == "draw"
        if not draw:
            n = len(d.stops)
            for i, st in enumerate(d.stops):
                badge = "E" if i == n - 1 else str(i + 1)
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, i)
                w = _stop_row(badge, st.get("name") or f"{st['lat']:.5f}, {st['lon']:.5f}",
                              on_remove=lambda _=False, i=i: self.ctl.remove_stop(i))
                item.setSizeHint(QSize(10, 30))
                self.stops.addItem(item)
                self.stops.setItemWidget(item, w)
        self.stops.fit()
        self.stops.setVisible(not draw)
        _clear(self.trace)
        if draw:
            pts = len(d.stops)
            self.trace.addWidget(label(f"{pts} point{'s' if pts != 1 else ''} traced. Click the "
                                       "map to keep drawing.", 12, role="muted", wrap=True), 1)
            u = _row_btn(icon_undo, "Remove the last point", self.ctl.undo_trace, size=28, px=15,
                         color=lambda: theme.TEXT)
            self.trace.addWidget(u)
        self.add_btn.setVisible(not draw)
        self.click_hint.setText("Click the map to add a stop." if not draw else "")
        self.style.set_value(d.style)
        self.profile.set_value(d.profile)
        self.profile.setVisible(d.style == "roads")
        msg, offer = plan_state.get("message", ""), plan_state.get("offer_straight", False)
        self.router_msg.setText(msg)
        set_role(self.router_msg, "warn" if offer else "muted")
        self.router_msg.setVisible(bool(msg))
        self.straight_btn.setVisible(offer)
        self.speed.set_state(d.speed_kmh, units)
        o = d.opts
        self.end.set_value(o["end"])
        self.laps.blockSignals(True); self.laps.setValue(max(1, o.get("laps") or 1)); self.laps.blockSignals(False)
        self.forever.set_on(o.get("laps") is None)
        loopish = o["end"] != "stop"
        self.laps.setEnabled(loopish and o.get("laps") is not None)
        self.forever.setEnabled(loopish)
        self.dwell.blockSignals(True); self.dwell.setValue(int(o.get("dwell_s", 0))); self.dwell.blockSignals(False)
        self.variation.set_on(bool(o.get("variation")))
        self.jitter.set_on(bool(o.get("jitter")))
        dist, dur = plan_state.get("distance"), plan_state.get("duration")
        n_stops = max(0, len(d.stops) - 1) if not draw else 0
        bits = [fmt_distance(dist, units) if dist else "—", fmt_duration(dur) if dur else "—"]
        if not draw:
            bits.append(f"{n_stops} stop{'s' if n_stops != 1 else ''}")
        if o["end"] != "stop":
            bits.append("forever" if o.get("laps") is None else f"× {o.get('laps') or 1} laps")
        self.summary.setText("  ·  ".join(bits))
        self.link_note.setText(link_note)
        self.link_note.setVisible(bool(link_note))
        ready = plan_state.get("ready", False)
        self.start.setEnabled(ready)
        self.start.setText("Start route" if ready or not plan_state.get("busy")
                           else "Finding the way…")
        self.save.setEnabled(bool(d.stops))
        self.export.setEnabled(ready)


class RunningPage(_Page):
    def __init__(self, ctl):
        super().__init__("On the way")
        self.ctl = ctl
        b = self.body
        top = QHBoxLayout()
        self.pct = label("0%", 26, 700, num=True)
        self.lap = label("", 12, role="muted", num=True)
        top.addWidget(self.pct); top.addStretch(1); top.addWidget(self.lap, 0, Qt.AlignmentFlag.AlignBottom)
        b.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000); self.bar.setTextVisible(False); self.bar.setFixedHeight(6)
        self.bar.setAccessibleName("Route progress")
        b.addWidget(self.bar)
        grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(2)
        self.stats = {}
        for i, (key, cap) in enumerate((("remaining", "Remaining"), ("left", "Time left"),
                                        ("speed", "Speed"), ("arrive", "Arrives"))):
            r, c = divmod(i, 2)
            grid.addWidget(label(cap, 11, role="muted"), r * 2, c)
            v = label("—", 15, 600, num=True)
            self.stats[key] = v
            grid.addWidget(v, r * 2 + 1, c)
        b.addLayout(grid)
        self.state = label("", 12, role="warn", wrap=True)
        b.addWidget(self.state)
        b.addWidget(_caption("Speed"))
        self.speed = SpeedControl()
        self.speed.speedChanged.connect(ctl.set_speed_kmh)
        self.speed.unitsChanged.connect(ctl.set_units)
        b.addWidget(self.speed)
        fr = QHBoxLayout()
        fr.addWidget(label("Follow on the map", 12), 1)
        self.follow = Switch(name="Follow the route on the map")
        self.follow.toggled.connect(ctl.set_follow)
        fr.addWidget(self.follow)
        b.addLayout(fr)
        self.awake = label("☕  Keeping this Mac awake while the route runs.", 11, role="faint", wrap=True)
        b.addWidget(self.awake)
        self.battery = label("", 11, role="warn", wrap=True)
        self.battery.hide()
        b.addWidget(self.battery)
        b.addStretch(1)
        btns = QHBoxLayout()
        self.pause = button("Pause", "soft", height=36, tip="Pause / resume (Space)")
        self.pause.clicked.connect(ctl.toggle_pause)
        self.stop = button("Stop", "danger", height=36)
        self.stop.clicked.connect(ctl.stop_route)
        btns.addWidget(self.pause, 1); btns.addWidget(self.stop, 1)
        self.foot.addLayout(btns)

    def update_tick(self, t, name: str, units: str, offline_msg: str):
        self.title.setText("Paused" if t.paused else f"To {name or 'your destination'}")
        self.title.update()
        self.pct.setText(f"{int(t.fraction * 100)}%")
        self.bar.setValue(int(t.fraction * 1000))
        laps = self.ctl.session_laps()
        self.lap.setText("" if laps == 1 else
                         (f"Lap {t.lap}" if laps is None else f"Lap {min(t.lap, laps)} of {laps}"))
        self.stats["remaining"].setText(fmt_distance(t.remaining_m, units)
                                        if t.remaining_m is not None else "∞")
        self.stats["left"].setText(fmt_duration(t.remaining_s) if t.remaining_s is not None else "∞")
        self.stats["speed"].setText(f"{fmt_speed(t.speed * 3.6, units)} {'mph' if units == 'mph' else 'km/h'}")
        self.stats["arrive"].setText(fmt_clock(time.time() + t.remaining_s)
                                     if t.remaining_s is not None else "—")
        self.state.setText(offline_msg)
        self.state.setVisible(bool(offline_msg))
        self.pause.setText("Resume" if t.paused else "Pause")

    def set_battery(self, text: str):
        self.battery.setText(text)
        self.battery.setVisible(bool(text))


class SummaryPage(_Page):
    def __init__(self, ctl):
        super().__init__("Route complete")
        self.ctl = ctl
        self.lines = label("", 13, num=True, wrap=True)
        self.body.addWidget(self.lines)
        done = button("Done", "primary", height=34)
        done.clicked.connect(ctl.dismiss_summary)
        self.body.addWidget(done)
        self.body.addStretch(1)

    def show_summary(self, s: dict, units: str):
        self.lines.setText(
            f"Distance   {fmt_distance(s['distance_m'], units)}\n"
            f"Duration   {fmt_duration(s['duration_s'])}\n"
            f"Finished   {fmt_clock(s['finished_wall'])}")


# ---- the panel -------------------------------------------------------------

class ControlPanel(QFrame):
    """The floating card. `show(page)` switches what it shows."""

    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.setObjectName("Float")
        self.setFixedWidth(WIDTH)
        self.ctl = ctl
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.home = HomePage(ctl)
        self.pin = PinPage(ctl)
        self.builder = BuilderPage(ctl)
        self.running = RunningPage(ctl)
        self.summary = SummaryPage(ctl)
        for p in (self.home, self.pin, self.builder, self.running, self.summary):
            self.stack.addWidget(p)
        foot = QHBoxLayout()
        foot.setContentsMargins(10, 4, 10, 8)
        self.stop_spoof = button("Stop spoofing", "dangerlink", height=28,
                                 tip="Put the iPhone back on its real location (⌘.)")
        self.stop_spoof.clicked.connect(ctl.stop_spoofing)
        foot.addWidget(self.stop_spoof)
        foot.addStretch(1)
        self.foot = QWidget(); self.foot.setLayout(foot)
        lay.addWidget(self.foot)
        self.page = "home"

    def show_page(self, name: str):
        self.page = name
        self.stack.setCurrentWidget(getattr(self, name))
        if name == "home":
            self.home.refresh()

    def set_spoofing(self, on: bool):
        self.stop_spoof.setVisible(on)
        self.foot.setVisible(on)

    def preferred_height(self) -> int:
        page = self.stack.currentWidget()
        h = page.content_height() + (self.foot.sizeHint().height() if self.foot.isVisible() else 0)
        return h
