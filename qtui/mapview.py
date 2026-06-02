"""MapPanel — the map card and everything that lives on it.

Owns the TileMap plus the floating chrome (search + autocomplete, the
'Set location here' button, the live coordinate readout, the zoom pill) and the
teleport + search interaction. Talks to the device only through the DeviceBridge.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from . import geo, theme
from .markers import PulseMarker, make_pin
from .tilemap import TileMap


def _icon_btn(text: str, w: int, h: int, pt: int = 18) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("variant", "icon")
    b.setFixedSize(w, h)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    f = b.font(); f.setPointSize(pt); f.setBold(True); b.setFont(f)
    return b


class _SuggestRow(QFrame):
    """A two-line clickable autocomplete result."""

    def __init__(self, item: dict, on_pick):
        super().__init__()
        self._item, self._on_pick = item, on_pick
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("Srow")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 7, 12, 7)
        lay.setSpacing(1)
        name = QLabel(item["label"])
        name.setFont(theme.ui_font(13, weight=600))
        lay.addWidget(name)
        if item.get("secondary"):
            sec = QLabel(item["secondary"])
            sec.setFont(theme.ui_font(12))
            sec.setStyleSheet(f"color: {theme.MUTED};")
            lay.addWidget(sec)
        self.setStyleSheet(
            f"#Srow {{ border-radius: 8px; }} #Srow:hover {{ background: {theme.GHOST}; }}")

    def mousePressEvent(self, e):
        self._on_pick(self._item)


class MapPanel(QFrame):
    hint = Signal(str)
    # worker-thread results, marshalled back to the GUI thread
    _suggestReady = Signal(str, object)
    _geocodeReady = Signal(float, float)
    _geocodeFailed = Signal(str)

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.setObjectName("MapFrame")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        self.map = TileMap(self)
        lay.addWidget(self.map)

        self.pending: tuple[float, float] | None = None
        self._pin = make_pin(theme.PIN)
        self._pin_ov = None             # staged red pin overlay
        self._live = None               # PulseMarker
        self._live_ov = None
        self._suggest_items: list[dict] = []

        self._build_floating()
        self._wire()

    # ---- floating chrome ------------------------------------------------

    def _build_floating(self):
        # zoom pill (bottom-right)
        self.zoom_pill = QFrame(self.map)
        self.zoom_pill.setObjectName("Pill")
        zl = QVBoxLayout(self.zoom_pill)
        zl.setContentsMargins(3, 3, 3, 3); zl.setSpacing(2)
        zin, zout = _icon_btn("＋", 42, 40), _icon_btn("－", 42, 40)
        sep = QFrame(); sep.setObjectName("Hairline"); sep.setFixedHeight(1)
        zl.addWidget(zin); zl.addWidget(sep); zl.addWidget(zout)
        zin.clicked.connect(lambda: self.map.zoom_at(1.0))
        zout.clicked.connect(lambda: self.map.zoom_at(-1.0))

        # search bar (top-center)
        self.search_bar = QFrame(self.map)
        self.search_bar.setStyleSheet(
            f"QFrame {{ background: {theme.PANEL}; border: 1px solid {theme.BORDER}; border-radius: 14px; }}")
        sl = QHBoxLayout(self.search_bar)
        sl.setContentsMargins(14, 4, 6, 4); sl.setSpacing(4)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search address, city, or place")
        self.search.setFixedWidth(300); self.search.setFixedHeight(34)
        self.search.setFont(theme.ui_font(14))
        go = QPushButton("Go"); go.setProperty("variant", "primary")
        go.setFixedSize(52, 30); go.setCursor(Qt.CursorShape.PointingHandCursor)
        sl.addWidget(self.search); sl.addWidget(go)
        go.clicked.connect(self._do_search)

        # autocomplete dropdown (under search)
        self.suggest_box = QFrame(self.map)
        self.suggest_box.setObjectName("Suggest")
        self.suggest_box.setStyleSheet(
            f"#Suggest {{ background: {theme.ELEV}; border: 1px solid {theme.BORDER}; border-radius: 12px; }}")
        self._suggest_lay = QVBoxLayout(self.suggest_box)
        self._suggest_lay.setContentsMargins(6, 6, 6, 6); self._suggest_lay.setSpacing(2)
        self.suggest_box.hide()

        # 'Set location here' (bottom-center, hidden until a pin is staged)
        self.set_btn = QPushButton("Set location here", self.map)
        self.set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_btn.setFixedSize(214, 46)
        self.set_btn.setFont(theme.ui_font(15, weight=600))
        self.set_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BLUE}; color: #fff; border: none; border-radius: 23px; }}"
            f"QPushButton:hover {{ background: {theme.BLUE_HI}; }}")
        self.set_btn.clicked.connect(self._commit)
        self.set_btn.hide()

        # live coordinate readout (top-left, hidden until a location is set)
        self.readout = QLabel("", self.map)
        self.readout.setFont(theme.mono_font(11))
        self.readout.setStyleSheet(
            f"background: {theme.PANEL}; color: {theme.LIVE_HI}; border-radius: 8px; padding: 4px 9px;")
        self.readout.hide()

        self._suggest_timer = QTimer(self)
        self._suggest_timer.setSingleShot(True)
        self._suggest_timer.timeout.connect(self._fire_suggest)

    def _wire(self):
        self.map.clicked.connect(self._on_click)
        self.bridge.located.connect(self._on_located)
        self.bridge.restored.connect(self.clear_markers)
        self.search.textEdited.connect(self._on_type)
        self.search.returnPressed.connect(self._on_enter)
        self._suggestReady.connect(self._show_suggestions)
        self._geocodeReady.connect(lambda la, lo: self.goto(la, lo))
        self._geocodeFailed.connect(lambda m: self.hint.emit(m))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        m = self.map
        W, H = m.width(), m.height()
        self.zoom_pill.adjustSize()
        self.zoom_pill.move(W - self.zoom_pill.width() - 16, H - self.zoom_pill.height() - 16)
        self.search_bar.adjustSize()
        sx = (W - self.search_bar.width()) // 2
        self.search_bar.move(sx, 16)
        self.suggest_box.setFixedWidth(self.search_bar.width())
        self.suggest_box.adjustSize()
        self.suggest_box.move(sx, 16 + self.search_bar.height() + 8)
        self.set_btn.move((W - self.set_btn.width()) // 2, H - self.set_btn.height() - 18)
        self.readout.adjustSize()
        self.readout.move(16, 14)

    # ---- teleport: stage a pin, then commit -----------------------------

    def _on_click(self, lat: float, lon: float):
        self.pending = (lat, lon)
        if self._pin_ov is None:
            self._pin_ov = self.map.add_marker(lat, lon, self._pin, anchor="s")
        else:
            self.map.move_marker(self._pin_ov, lat, lon)
        self.set_btn.show(); self.set_btn.raise_()
        self.hint.emit(f"Pinned {lat:.5f}, {lon:.5f} — tap “Set location here” to move your iPhone.")

    def _commit(self):
        if self.pending:
            self.bridge.set_location(*self.pending)

    def _on_located(self, lat: float, lon: float):
        # the set landed: show the live 'You' marker, drop the staged pin
        if self._live is None:
            self._live = PulseMarker()
            self._live_ov = self.map.add_item(self._live, lat, lon)
        else:
            self.map.move_marker(self._live_ov, lat, lon)
        self.readout.setText(f"◉  {lat:.5f},  {lon:.5f}")
        self.readout.adjustSize(); self.readout.show(); self.readout.raise_()
        if self._pin_ov is not None:
            self.map.remove_overlay(self._pin_ov); self._pin_ov = None
        self.pending = None
        self.set_btn.hide()

    def clear_markers(self):
        for ov in (self._pin_ov, self._live_ov):
            if ov is not None:
                self.map.remove_overlay(ov)
        self._pin_ov = self._live_ov = self._live = None
        self.pending = None
        self.set_btn.hide()
        self.readout.hide()

    def goto(self, lat: float, lon: float, zoom: float = 15):
        self.map.set_view(lat, lon, zoom)
        self._on_click(lat, lon)        # stage it too, so one tap sets it

    # ---- search ---------------------------------------------------------

    def _on_type(self, text: str):
        self._suggest_timer.stop()
        q = text.strip()
        if len(q) < 2 or geo.parse_coords(q):
            self._hide_suggestions()
            return
        self._suggest_timer.start(260)

    def _fire_suggest(self):
        q = self.search.text().strip()
        if len(q) >= 2:
            threading.Thread(target=self._suggest_worker, args=(q,), daemon=True).start()

    def _suggest_worker(self, q: str):
        self._suggestReady.emit(q, geo.suggest(q))

    def _show_suggestions(self, q: str, items: list):
        if self.search.text().strip() != q:
            return                       # stale: user kept typing
        while self._suggest_lay.count():
            w = self._suggest_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._suggest_items = items or []
        if not items:
            self._hide_suggestions()
            return
        for it in items:
            self._suggest_lay.addWidget(_SuggestRow(it, self._pick))
        self.suggest_box.setFixedWidth(self.search_bar.width())
        self.suggest_box.adjustSize()
        self.suggest_box.move(self.search_bar.x(), 16 + self.search_bar.height() + 8)
        self.suggest_box.show(); self.suggest_box.raise_()

    def _hide_suggestions(self):
        self.suggest_box.hide()
        self._suggest_items = []

    def _pick(self, item: dict):
        self._hide_suggestions()
        self.search.setText(item["label"])
        self.goto(item["lat"], item["lon"])

    def _on_enter(self):
        q = self.search.text().strip()
        c = geo.parse_coords(q)
        if c:
            self.goto(*c)
        elif self._suggest_items:
            self._pick(self._suggest_items[0])
        else:
            self._do_search()

    def _do_search(self):
        q = self.search.text().strip()
        if not q:
            return
        c = geo.parse_coords(q)
        if c:
            self.goto(*c)
            return
        self._hide_suggestions()
        self.hint.emit(f"Searching for “{q}”…")
        threading.Thread(target=self._geocode_worker, args=(q,), daemon=True).start()

    def _geocode_worker(self, q: str):
        try:
            lat, lon = geo.geocode(q)
            self._geocodeReady.emit(lat, lon)
        except Exception as e:
            self._geocodeFailed.emit(str(e))
