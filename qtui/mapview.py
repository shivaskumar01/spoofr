"""MapPanel — the map card and everything that lives on it.

Owns the TileMap plus the floating chrome (search + autocomplete, the
'Set location here' button, the live coordinate readout, the zoom pill) and the
teleport + search interaction. Talks to the device only through the DeviceBridge.
"""

from __future__ import annotations

import math
import random
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from . import geo, route, theme
from .markers import PulseMarker, make_pin, make_waypoint
from .tilemap import TileMap

_M_PER_DEG = 111_320.0


def _jitter(lat: float, lon: float, radius_m: float = 4.0) -> tuple[float, float]:
    """Nudge a point by a random offset within `radius_m` metres."""
    r = radius_m * math.sqrt(random.random())
    th = random.uniform(0.0, 2.0 * math.pi)
    dlat = (r * math.cos(th)) / _M_PER_DEG
    dlon = (r * math.sin(th)) / (_M_PER_DEG * max(0.15, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


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
    committed = Signal(float, float)     # a teleport set landed (-> recents)
    # worker-thread results, marshalled back to the GUI thread
    _suggestReady = Signal(str, object)
    _geocodeReady = Signal(float, float)
    _geocodeFailed = Signal(str)
    _walkStep = Signal(float, float)
    _jitterStep = Signal(float, float)
    _routeSnapped = Signal(object)
    _routeDone = Signal(str)

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
        self._live_pos = None           # where the You dot is now
        self._anchor = None             # idle point the jitter wobbles around
        self._suggest_items: list[dict] = []

        # movement state (shared by walk pad + route)
        self.speed = 1.4                # m/s
        self._walk_vec = (0.0, 0.0)     # (north, east) unit vector
        self._walk_pos = None
        self._walking = False
        self._following = False
        self._keys_down: set[str] = set()
        self._jitter_on = False
        self._jitter_m = 4.0
        self._pulse = True
        self._closing = False

        # route state
        self.mode = "teleport"          # "teleport" | "route"
        self.profile = "walking"        # OSRM profile from the speed preset
        self.loop = False
        self.bounce = False
        self.snap = False
        self.points: list[tuple[float, float]] = []   # waypoints
        self._wp_ovs: list = []         # waypoint marker overlays
        self._path_ov = None            # route polyline overlay
        self._playing = False
        self._route_stop = threading.Event()

        self._build_floating()
        self._build_walk_pad()
        self._wire()
        threading.Thread(target=self._jitter_worker, daemon=True).start()

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
        self._walkStep.connect(self._on_walk_step)
        self._jitterStep.connect(self._on_jitter_step)
        self._routeSnapped.connect(self._redraw_path)
        self._routeDone.connect(self._on_route_done)

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
        if getattr(self, "walk_pad", None) and self.walk_pad.isVisible():
            self._position_walk_pad()

    # ---- teleport: stage a pin, then commit -----------------------------

    def _on_click(self, lat: float, lon: float):
        if self.mode == "route":
            self._add_waypoint(lat, lon)
            return
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

    def _set_live(self, lat: float, lon: float):
        """Show/move the live 'You' marker + readout (teleport set or walk step)."""
        self._live_pos = (lat, lon)
        if self._live is None:
            self._live = PulseMarker()
            self._live.set_pulsing(self._pulse)
            self._live_ov = self.map.add_item(self._live, lat, lon)
        else:
            self.map.move_marker(self._live_ov, lat, lon)
        self.readout.setText(f"◉  {lat:.5f},  {lon:.5f}")
        self.readout.adjustSize(); self.readout.show(); self.readout.raise_()

    def _on_located(self, lat: float, lon: float):
        # a teleport set landed: live marker, drop the staged pin, log a recent
        self._set_live(lat, lon)
        self._anchor = (lat, lon)
        if self._pin_ov is not None:
            self.map.remove_overlay(self._pin_ov); self._pin_ov = None
        self.pending = None
        self.set_btn.hide()
        self.committed.emit(lat, lon)

    def clear_markers(self):
        for ov in (self._pin_ov, self._live_ov):
            if ov is not None:
                self.map.remove_overlay(ov)
        self._pin_ov = self._live_ov = self._live = None
        self._live_pos = self._anchor = None
        self.pending = None
        self.set_btn.hide()
        self.readout.hide()

    def goto(self, lat: float, lon: float, zoom: float = 15):
        self.map.set_view(lat, lon, zoom)
        self._on_click(lat, lon)        # stage it too, so one tap sets it

    # ---- walk pad (joystick) + arrow keys -------------------------------

    def _build_walk_pad(self):
        self.walk_pad = QFrame(self.map)
        self.walk_pad.setObjectName("Pill")
        grid = QGridLayout(self.walk_pad)
        grid.setContentsMargins(8, 8, 8, 8); grid.setSpacing(3)
        dirs = [("↖", (1, -1)), ("↑", (1, 0)), ("↗", (1, 1)),
                ("←", (0, -1)), ("•", (0, 0)), ("→", (0, 1)),
                ("↙", (-1, -1)), ("↓", (-1, 0)), ("↘", (-1, 1))]
        for i, (glyph, vec) in enumerate(dirs):
            stop = vec == (0, 0)
            b = QPushButton(glyph); b.setFixedSize(34, 34)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            f = b.font(); f.setPointSize(15); b.setFont(f)
            b.setStyleSheet(
                f"QPushButton {{ border: none; border-radius: 8px;"
                f" background: {'transparent' if stop else theme.ELEV};"
                f" color: {theme.MUTED if stop else theme.TEXT}; }}"
                f"QPushButton:hover {{ background: {theme.GHOST_HI}; }}")
            grid.addWidget(b, i // 3, i % 3)
            if stop:
                b.clicked.connect(self._walk_release)
            else:
                b.pressed.connect(lambda v=vec: self._walk_press(v))
                b.released.connect(self._walk_release)
        self.walk_pad.hide()

    def show_walk_pad(self, show: bool):
        self.walk_pad.setVisible(show)
        if show:
            self._position_walk_pad(); self.walk_pad.raise_()
        else:
            self._walk_release()

    def _position_walk_pad(self):
        self.walk_pad.adjustSize()
        self.walk_pad.move(16, self.map.height() - self.walk_pad.height() - 16)

    def _walk_press(self, vec):
        n, e = vec
        mag = math.hypot(n, e) or 1.0
        self._walk_vec = (n / mag, e / mag)
        self._start_walk()

    def _walk_release(self):
        self._walk_vec = (0.0, 0.0)
        self._walking = False
        self._following = False

    def _start_walk(self):
        if self._walking:
            return
        if self.bridge.device is None:
            self._walk_vec = (0.0, 0.0)
            self.hint.emit("Connect to your iPhone first.")
            return
        self._walk_pos = self._live_pos or self.map.center()
        self._walking = True
        self._following = True
        threading.Thread(target=self._walk_worker, daemon=True).start()

    def _walk_worker(self):
        dt = 0.18
        while self._walking and self.bridge.device is not None:
            vx, vy = self._walk_vec
            if (vx or vy) and self._walk_pos:
                lat, lon = self._walk_pos
                dist = max(self.speed, 0.3) * dt
                lat += (vx * dist) / _M_PER_DEG
                lon += (vy * dist) / (_M_PER_DEG * max(0.15, math.cos(math.radians(lat))))
                self._walk_pos = (lat, lon)
                try:
                    self.bridge.device.set(lat, lon)
                except Exception:
                    pass
                self._walkStep.emit(lat, lon)
            time.sleep(dt)
        if self._walk_pos:
            self._anchor = self._walk_pos

    def _on_walk_step(self, lat: float, lon: float):
        self._set_live(lat, lon)
        if self._following:
            self.map.pan_to(lat, lon)

    def key_walk(self, key: str, pressed: bool):
        if pressed:
            self._keys_down.add(key)
        else:
            self._keys_down.discard(key)
        n = ("Up" in self._keys_down) - ("Down" in self._keys_down)
        e = ("Right" in self._keys_down) - ("Left" in self._keys_down)
        if not n and not e:
            self._walk_release()
        else:
            mag = math.hypot(n, e)
            self._walk_vec = (n / mag, e / mag)
            self._start_walk()

    # ---- jitter / pulse / brightness ------------------------------------

    def _jitter_worker(self):
        while not self._closing:
            try:
                if (self._jitter_on and self.bridge.device is not None
                        and self._anchor and not self._walking):
                    jlat, jlon = _jitter(self._anchor[0], self._anchor[1], self._jitter_m)
                    try:
                        self.bridge.device.set(jlat, jlon)
                    except Exception:
                        pass
                    self._jitterStep.emit(jlat, jlon)
            except Exception:
                pass
            time.sleep(1.5)

    def _on_jitter_step(self, lat: float, lon: float):
        # wobble only the dot; the readout stays on the anchor
        if self._live_ov is not None:
            self.map.move_marker(self._live_ov, lat, lon)

    def set_pulsing(self, on: bool):
        self._pulse = bool(on)
        if self._live is not None:
            self._live.set_pulsing(self._pulse)

    def set_jitter(self, on: bool):
        self._jitter_on = bool(on)

    def set_brightness(self, name: str):
        self.map.set_brightness(name)

    # ---- route mode -----------------------------------------------------

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "route":
            self.set_btn.hide()
            self.hint.emit("Route mode — click the map to drop waypoints, then press Start.")
        else:
            if self.pending:
                self.set_btn.show(); self.set_btn.raise_()
            self.hint.emit("Teleport — click the map or search, then “Set location here”.")

    def set_speed(self, mps: float):
        self.speed = float(mps)

    def set_preset(self, profile: str):
        self.profile = profile

    def set_loop(self, on: bool):
        self.loop = bool(on)

    def set_bounce(self, on: bool):
        self.bounce = bool(on)

    def set_snap(self, on: bool):
        self.snap = bool(on)

    def _add_waypoint(self, lat: float, lon: float):
        self.points.append((lat, lon))
        ov = self.map.add_marker(lat, lon, make_waypoint(len(self.points)), anchor="center", z=8)
        self._wp_ovs.append(ov)
        self._redraw_path(self.points)
        self.hint.emit(f"{len(self.points)} waypoint(s). Press Start to walk the route.")

    def _redraw_path(self, pts):
        if self._path_ov is not None:
            self.map.remove_overlay(self._path_ov)
            self._path_ov = None
        if len(pts) >= 2:
            self._path_ov = self.map.add_path(pts, color=theme.BLUE, width=5)

    def clear_route(self):
        self.stop_route()
        for ov in self._wp_ovs:
            self.map.remove_overlay(ov)
        self._wp_ovs.clear()
        self.points.clear()
        if self._path_ov is not None:
            self.map.remove_overlay(self._path_ov)
            self._path_ov = None
        self.hint.emit("Waypoints cleared.")

    def start_route(self):
        if self.bridge.device is None:
            self.hint.emit("Connect to your iPhone first.")
            return
        if len(self.points) < 2:
            self.hint.emit("Drop at least two waypoints first.")
            return
        if self._playing:
            return
        self._route_stop.clear()
        self._playing = True
        self._following = True
        threading.Thread(target=self._route_worker, args=(list(self.points),), daemon=True).start()

    def stop_route(self):
        self._route_stop.set()
        self._playing = False
        self._following = False

    def _route_worker(self, pts):
        try:
            if self.snap:
                self.hint.emit("Snapping the route to roads…")
                snapped = route.snap_to_roads(pts, self.profile)
                if len(snapped) >= 2:
                    pts = snapped
                    self._routeSnapped.emit(list(snapped))
            path = route.route_points(pts, max(self.speed, 0.3), dt=1.0)
            if not path:
                self._routeDone.emit("Nothing to walk.")
                return
            seq = path + path[-2::-1] if self.bounce else path   # forward, then back
            repeat = self.loop or self.bounce
            total = len(seq)
            dev = self.bridge.device
            while True:
                for i, (lat, lon) in enumerate(seq, 1):
                    if self._route_stop.is_set() or self.bridge.device is None:
                        self._routeDone.emit("Route stopped.")
                        return
                    try:
                        dev.set(lat, lon)
                    except Exception:
                        pass
                    self._walkStep.emit(lat, lon)
                    self.hint.emit(f"Walking…  {i}/{total}   ({lat:.5f}, {lon:.5f})")
                    if self._route_stop.wait(1.0):
                        self._routeDone.emit("Route stopped.")
                        return
                if not repeat:
                    self._routeDone.emit("Route complete.")
                    return
        finally:
            self._playing = False

    def _on_route_done(self, msg: str):
        self._playing = False
        self._following = False
        self.hint.emit(msg)
        if self._live_pos:
            self._anchor = self._live_pos

    def load_route(self, pts):
        self.clear_route()
        self.points = [tuple(p) for p in pts]
        n = len(self.points)
        for i in (1, n):                 # mark only start/end (GPX can be dense)
            la, lo = self.points[i - 1]
            self._wp_ovs.append(self.map.add_marker(
                la, lo, make_waypoint(i), anchor="center", z=8))
        self._redraw_path(self.points)
        self.map.set_view(self.points[0][0], self.points[0][1], 14)

    def import_gpx(self) -> bool:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import GPX track", "", "GPX track (*.gpx);;All files (*)")
        if not path:
            return False
        try:
            pts = route.parse_gpx(path)
        except Exception as e:
            self.hint.emit(f"Couldn’t read that GPX: {e}")
            return False
        self.load_route(pts)
        self.hint.emit(f"Loaded {len(pts)} points from GPX. Press Start to walk it.")
        return True

    def export_gpx(self):
        if len(self.points) < 2:
            self.hint.emit("Drop at least two waypoints (or import a track) to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export route as GPX", "spoofr-route.gpx", "GPX track (*.gpx)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(route.build_gpx(self.points, "Spoofr route"))
            self.hint.emit(f"Exported {len(self.points)} points → {path}")
        except Exception as e:
            self.hint.emit(f"Export failed: {e}")

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
