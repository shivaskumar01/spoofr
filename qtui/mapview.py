"""MapPanel, the map card and everything that lives on it.

Owns the TileMap plus the floating chrome (search + autocomplete, the
'Set location here' button, the live coordinate readout, the zoom pill) and the
teleport + search interaction. Talks to the device only through the DeviceBridge.
"""

from __future__ import annotations

import math
import random
import subprocess
import threading
import time

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from . import geo, route, store, theme
from .markers import PulseMarker, make_pin, make_waypoint
from .tilemap import TileMap

_M_PER_DEG = 111_320.0
ROUTE_DT = 1.0            # seconds between route fixes (real GPS is about 1 Hz)
ORIGIN_MAX_M = 100_000.0  # past this, "start from where you are" stops meaning anything


def _clock(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


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
    requestTeleport = Signal()           # search/place in route mode -> switch to teleport
    playingChanged = Signal(bool)        # a route started / stopped
    routeOffline = Signal(bool)          # a running route can't reach the iPhone
    # worker-thread results, marshalled back to the GUI thread
    _suggestReady = Signal(str, object)
    _geocodeReady = Signal(float, float)
    _geocodeFailed = Signal(str)
    _walkStep = Signal(float, float)
    _jitterStep = Signal(float, float)
    _routeSnapped = Signal(object)
    _routeProgress = Signal(int, int, float, float)   # i, total, lat, lon
    _routeDone = Signal(str)

    def __init__(self, bridge, settings=None, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.settings = settings if settings is not None else {}
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
        self._active_spoof = None       # last location pushed to the phone (persisted)
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
        self._transport = "Walking"     # display verb for the route status line
        self.loop = False
        self.bounce = False
        self.snap = False
        self.points: list[tuple[float, float]] = []   # waypoints
        self._wp_ovs: list = []         # waypoint marker overlays
        self._path_ov = None            # route polyline overlay
        self._playing = False
        self._route_gen = 0
        self._travelled: list[tuple[float, float]] = []   # the part already walked
        self._travel_ov = None
        self._route_is_track = False    # imported GPX: the file already includes a start
        self._route_offline = False     # running, but currently can't reach the phone
        self._route_at = (0, 0)         # (fix, total) — where a pause resumes from
        self._caffeinate = None         # holds off idle sleep while a route plays

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
        self.search.installEventFilter(self)   # Esc dismisses the suggestions
        self.map.clicked.connect(self._on_click)
        self.bridge.located.connect(self._on_located)
        self.bridge.restored.connect(self.on_restored)
        self.search.textEdited.connect(self._on_type)
        self.search.returnPressed.connect(self._on_enter)
        self._suggestReady.connect(self._show_suggestions)
        self._geocodeReady.connect(lambda la, lo: self.goto(la, lo))
        self._geocodeFailed.connect(lambda m: self.hint.emit(m))
        self._walkStep.connect(self._on_walk_step)
        self._jitterStep.connect(self._on_jitter_step)
        self._routeSnapped.connect(self._redraw_path)
        self._routeProgress.connect(self._on_route_progress)
        self.routeOffline.connect(self._on_route_offline)
        self.playingChanged.connect(self.hold_awake)
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

    def eventFilter(self, obj, e):
        if (obj is self.search and e.type() == QEvent.Type.KeyPress
                and e.key() == Qt.Key.Key_Escape):
            self._hide_suggestions()
            self.search.clearFocus()
            return True
        return super().eventFilter(obj, e)

    def _on_click(self, lat: float, lon: float):
        self._hide_suggestions()             # a map tap dismisses the dropdown
        if self.mode == "route":
            self._add_waypoint(lat, lon)
        else:
            self._stage(lat, lon)

    def _stage(self, lat: float, lon: float):
        """Drop/move the staged teleport pin (a candidate, not yet sent)."""
        self.pending = (lat, lon)
        if self._pin_ov is None:
            self._pin_ov = self.map.add_marker(lat, lon, self._pin, anchor="s")
        else:
            self.map.move_marker(self._pin_ov, lat, lon)
        self.set_btn.show(); self.set_btn.raise_()
        self.hint.emit(f"Pinned {lat:.5f}, {lon:.5f}, tap “Set location here” to move your iPhone.")

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
        self._active_spoof = (lat, lon)
        self.persist_spoof()
        if self._pin_ov is not None:
            self.map.remove_overlay(self._pin_ov); self._pin_ov = None
        self.pending = None
        self.set_btn.hide()
        self.committed.emit(lat, lon)

    def persist_spoof(self):
        """Remember (or forget) the location the iPhone is set to, so a later run
        can show it and offer Restore GPS."""
        if self._active_spoof:
            self.settings["active_spoof"] = {"lat": self._active_spoof[0],
                                             "lon": self._active_spoof[1]}
        else:
            self.settings.pop("active_spoof", None)
        store.save(self.settings)

    def restore_active_spoof(self, lat: float, lon: float):
        """On reconnect: the phone is still set to this spot, show it and re-apply
        so the app and device agree. Restore GPS then resets it."""
        self._active_spoof = (lat, lon)
        self._anchor = (lat, lon)
        self.map.set_view(lat, lon, 15)
        self.bridge.set_location(lat, lon)   # re-assert the spoof; located shows the marker

    def stop_motion(self):
        """Stop any active route or walk (e.g. before restoring real GPS).

        Also suspends the device so a fix that was already in flight can't land
        after the stop and silently move the phone again."""
        self.stop_route()
        self._walk_release()
        self.bridge.suspend()

    def clear_all(self):
        """Full clear on disconnect: stop motion and remove the live marker + pin."""
        self.stop_motion()
        self._clear_travelled()
        for ov in (self._pin_ov, self._live_ov):
            if ov is not None:
                self.map.remove_overlay(ov)
        self._pin_ov = self._live_ov = self._live = None
        self._live_pos = self._anchor = None
        self.pending = None
        self.set_btn.hide()
        self.readout.hide()

    def on_restored(self):
        """Spoof cleared → drop the staged pin and stop wobbling, but KEEP the live
        'You' marker on the map (re-located to the user's real/current location).
        The marker should always be visible while connected."""
        self.stop_motion()
        if self._pin_ov is not None:
            self.map.remove_overlay(self._pin_ov)
            self._pin_ov = None
        self.pending = None
        self.set_btn.hide()
        self._anchor = None              # nothing to jitter around once the spoof is cleared
        self._active_spoof = None        # phone is back on real GPS, forget the saved spoof
        self.persist_spoof()
        self.bridge.locate()             # re-show the live marker at the current location

    def goto(self, lat: float, lon: float, zoom: float = 15):
        if self.mode == "route":
            self.requestTeleport.emit()   # search/place implies teleport, not a waypoint
        self.map.set_view(lat, lon, zoom)
        self._stage(lat, lon)             # stage it too, so one tap sets it

    def locate_me(self, lat: float, lon: float):
        """Fly to the user's current location and show the 'You' marker,
        informational only (no spoof, no jitter anchor). Street zoom: the packaged
        app gets an exact CoreLocation fix; a plain dev run gets IP (city), either
        way the user clicks/searches to set the iPhone."""
        self.map.set_view(lat, lon, 15)
        self._set_live(lat, lon)
        self.hint.emit("Centered on your location, click anywhere or search, then “Set location here”.")

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
            b = QPushButton(glyph); b.setFixedSize(36, 36)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            f = b.font(); f.setPointSize(17); f.setBold(True); b.setFont(f)
            b.setStyleSheet(
                f"QPushButton {{ border: none; border-radius: 8px;"
                f" background: {'transparent' if stop else theme.ELEV_HI};"
                f" color: {theme.MUTED if stop else theme.LIVE_HI}; }}"
                f"QPushButton:hover {{ background: {theme.BLUE}; color: #ffffff; }}")
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
        # Step by *measured* elapsed time, not the nominal tick: device.set()
        # round-trips to the phone, so a fixed step per loop would walk slower
        # than the chosen speed. Clamp so a stall never teleports you.
        dt = 0.18
        last = time.monotonic()
        while self._walking and self.bridge.device is not None:
            vx, vy = self._walk_vec
            now = time.monotonic()
            step_t = min(now - last, 1.0)
            last = now
            if (vx or vy) and self._walk_pos and step_t > 0.01:
                lat, lon = self._walk_pos
                dist = max(self.speed, 0.3) * step_t
                lat += (vx * dist) / _M_PER_DEG
                lon += (vy * dist) / (_M_PER_DEG * max(0.15, math.cos(math.radians(lat))))
                self._walk_pos = (lat, lon)
                if not self.bridge.push(lat, lon):
                    self._walk_release()   # session gone (push started the reconnect)
                    break
                self._walkStep.emit(lat, lon)
            time.sleep(dt)
        if self._walk_pos:
            self._anchor = self._walk_pos

    def _on_walk_step(self, lat: float, lon: float):
        self._set_live(lat, lon)
        self._active_spoof = (lat, lon)
        # only recentre once the marker nears an edge: recentring every fix glues
        # it to the middle of the screen, and a 1.4 m/s walk then looks like
        # nothing is happening at all
        if self._following and self.map.is_near_edge(lat, lon):
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
                        and self._anchor and not self._walking and not self._playing):
                    jlat, jlon = _jitter(self._anchor[0], self._anchor[1], self._jitter_m)
                    if self.bridge.push(jlat, jlon, quiet=True):
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

    def heartbeat_point(self):
        """The fix the bridge's monitor may re-assert to prove the developer
        channel is still alive, or None when there is nothing to probe with.

        None when no spoof is active (a write would move a user who is on real
        GPS), and None while a walk, a route, or jitter is running: each of those
        already round-trips through bridge.push() far more often than the monitor
        would, and reports a dead session through the same path.
        """
        if self._walking or self._playing or self._jitter_on:
            return None
        return self._active_spoof

    def set_brightness(self, name: str):
        self.map.set_brightness(name)

    # ---- route mode -----------------------------------------------------

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "route":
            self.set_btn.hide()
            self.hint.emit("Route, click where you want to end up. Your iPhone starts from "
                           "where it is now; extra clicks add stops on the way.")
        else:
            if self.pending:
                self.set_btn.show(); self.set_btn.raise_()
            self.hint.emit("Teleport, click the map or search, then “Set location here”.")

    def set_speed(self, mps: float):
        self.speed = float(mps)

    def set_preset(self, profile: str, gerund: str = "Walking"):
        self.profile = profile
        self._transport = gerund        # "Walking"/"Running"/"Cycling"/"Driving"

    def set_loop(self, on: bool):
        self.loop = bool(on)

    def set_bounce(self, on: bool):
        self.bounce = bool(on)

    def set_snap(self, on: bool):
        self.snap = bool(on)

    def route_origin(self):
        """Where a route starts: wherever the iPhone is right now.

        The start is not something you should have to click — the phone is
        already somewhere. That makes a single waypoint a destination, and it is
        also why starting a route no longer jumps you to waypoint 1 first.
        """
        return self._active_spoof or self._live_pos

    def route_plan(self) -> tuple[list, bool]:
        """(points to walk, whether the current position was prepended).

        Not prepended for an imported track — the file is the route, start
        included — nor when the phone is a long way from the first waypoint:
        routing from Paris to a pin in San Francisco is never what the click
        meant, and no road router would honour it anyway.
        """
        pts = list(self.points)
        origin = self.route_origin()
        if self._route_is_track or origin is None or not pts:
            return pts, False
        gap = route.meters(origin, pts[0])
        if gap > ORIGIN_MAX_M or gap < 1.0:
            return pts, False
        return [origin] + pts, True

    def _add_waypoint(self, lat: float, lon: float):
        self.points.append((lat, lon))
        self._route_is_track = False
        self._redraw_waypoints()
        self._redraw_path(self.points)
        n = len(self.points)
        if n == 1:
            if self.route_origin() is not None:
                self.hint.emit("Destination set. Press Start to head there from where your "
                               "iPhone is now, or click again to add a stop on the way.")
            else:
                self.hint.emit("Destination set. Set your iPhone’s location first so the "
                               "route has somewhere to start, or drop a second waypoint.")
        else:
            self.hint.emit(f"{n} stops · green = destination. Press Start, or keep adding.")

    def _redraw_waypoints(self):
        """Renumber and recolour the markers; the last one is the destination.

        A dense imported track would mean hundreds of markers, so past a dozen
        only the ends are marked.
        """
        for ov in self._wp_ovs:
            self.map.remove_overlay(ov)
        self._wp_ovs.clear()
        n = len(self.points)
        if not n:
            return
        for i in (range(1, n + 1) if n <= 12 else (1, n)):
            la, lo = self.points[i - 1]
            color = theme.GREEN if i == n else theme.BLUE
            self._wp_ovs.append(self.map.add_marker(
                la, lo, make_waypoint(i, color=color), anchor="center", z=8))

    def _redraw_path(self, pts):
        if self._path_ov is not None:
            self.map.remove_overlay(self._path_ov)
            self._path_ov = None
        if len(pts) >= 2:
            self._path_ov = self.map.add_path(pts, color=theme.BLUE, width=5)

    def clear_route(self):
        self.stop_route()
        self._clear_travelled()
        self._route_is_track = False
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
        pts, from_here = self.route_plan()
        if len(pts) < 2:
            self.hint.emit(self._why_no_route())
            return
        if self._playing:
            return
        self._route_gen += 1
        gen = self._route_gen
        self._playing = True
        self._following = True
        self._clear_travelled()
        self._route_at = (0, 0)
        self._route_offline = False
        self.playingChanged.emit(True)
        self.hint.emit(
            "Routing from where your iPhone is now…" if from_here else
            "Starting at waypoint 1 — your iPhone jumps there first, then follows the route.")
        threading.Thread(target=self._route_worker, args=(pts, gen), daemon=True).start()

    def _why_no_route(self) -> str:
        """Say which of the ways to be un-routable this is, not just that it is."""
        if not self.points:
            return "Click the map to drop a destination first."
        origin = self.route_origin()
        if origin is None:
            return ("Set your iPhone’s location first so the route has a starting point, "
                    "or drop a second waypoint.")
        km = route.meters(origin, self.points[0]) / 1000.0
        if km * 1000.0 > ORIGIN_MAX_M:
            return (f"Your iPhone is {km:,.0f} km from that pin — too far to route from "
                    f"where it is. Set its location nearer first, or drop a second waypoint.")
        return ("That pin is where your iPhone already is — pick a destination a little "
                "further away.")

    def stop_route(self):
        self._route_gen += 1          # invalidate any running route worker
        self._playing = False
        self._following = False
        self.playingChanged.emit(False)

    def _clear_travelled(self):
        self._travelled = []
        if self._travel_ov is not None:
            self.map.remove_overlay(self._travel_ov)
            self._travel_ov = None

    def _route_stale(self, gen: int) -> bool:
        """Only a newer route (or Stop) ends this one.

        Deliberately not "is the device gone": unplugging mid-route used to make
        every remaining fix stale and throw the walk away. An unreachable phone
        does not stop the clock.
        """
        return gen != self._route_gen

    def route_is_playing(self) -> bool:
        """A route is running — reachable phone or not."""
        return self._playing

    def route_is_offline(self) -> bool:
        """A running route that currently cannot reach the phone."""
        return self._playing and self._route_offline

    def _set_offline(self, offline: bool):
        if offline != self._route_offline:
            self._route_offline = offline
            self.routeOffline.emit(offline)

    def hold_awake(self, on: bool):
        """Keep the Mac out of idle sleep while a route is playing.

        The Mac pushes every single fix, so if it dozes off the walk stops dead
        halfway through and nothing says why. Best-effort: if caffeinate isn't
        there, the route still runs, it just isn't protected from sleep.
        """
        if on and self._caffeinate is None:
            try:
                self._caffeinate = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-i"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self._caffeinate = None
        elif not on and self._caffeinate is not None:
            proc, self._caffeinate = self._caffeinate, None
            try:
                proc.terminate()
            except Exception:
                pass

    def _on_route_offline(self, offline: bool):
        if offline:
            i, total = self._route_at
            self.hint.emit(f"Out of touch with your iPhone at {i}/{total} — the route keeps "
                           "running. Reconnect and it will be wherever the route has got to.")
        else:
            self.hint.emit("iPhone is back — catching it up to the route.")

    def _route_sleep(self, gen: int, dt: float) -> bool:
        """Sleep up to dt; return True early if this route was stopped/superseded."""
        end = time.time() + dt
        while time.time() < end:
            if self._route_stale(gen):
                return True
            time.sleep(0.05)
        return self._route_stale(gen)

    def _route_worker(self, pts, gen: int):
        try:
            if self.snap:
                self.hint.emit(f"Finding a {self._transport.lower()} route along real roads…")
                snapped = route.snap_to_roads(pts, self.profile)
                if self._route_stale(gen):
                    self._routeDone.emit("Route stopped.")
                    return
                if snapped.ok:
                    pts = snapped.points
                    self._routeSnapped.emit(list(pts))
                else:
                    self.hint.emit("Couldn’t reach the router — going straight between waypoints.")
            path = route.route_points(pts, max(self.speed, 0.3), dt=ROUTE_DT)
            if not path:
                self._routeDone.emit("Nothing to walk.")
                return
            seq = path + path[-2::-1] if self.bounce else path   # forward, then back
            repeat = self.loop or self.bounce
            total = len(seq)
            # Where you are is a function of the clock, not of how many fixes we
            # managed to send. Start at 6:30 with twenty minutes of route ahead and
            # you are at the end at 6:50 — whether the phone was reachable for all
            # of it, some of it, or none of it. Unplugging costs only the fixes that
            # could not be delivered while it was away; reconnecting puts the phone
            # wherever the route has got to by then.
            started = time.monotonic()
            sent = -1
            while True:
                if self._route_stale(gen):
                    self._routeDone.emit("Route stopped.")
                    return
                i = int((time.monotonic() - started) / ROUTE_DT)
                over = i >= total
                if over and repeat:
                    i, over = i % total, False
                idx = min(i, total - 1)
                if idx != sent:
                    lat, lon = seq[idx]
                    # read the device through the bridge every fix, so a reconnect
                    # swaps it cleanly instead of us writing to a dead session
                    if self.bridge.push(lat, lon):
                        sent = idx
                        self._set_offline(False)
                        self._walkStep.emit(lat, lon)
                        self._routeProgress.emit(min(idx + 1, total), total, lat, lon)
                    else:
                        # out of touch: the clock runs on without us. Keep trying,
                        # including past the end, so a phone that comes back late
                        # still lands on the finished route.
                        self._set_offline(True)
                        if self._route_sleep(gen, 1.0):
                            self._routeDone.emit("Route stopped.")
                            return
                        continue
                if over:
                    self._routeDone.emit("Route complete.")
                    return
                if self._route_sleep(gen, 0.2):
                    self._routeDone.emit("Route stopped.")
                    return
        finally:
            if gen == self._route_gen:
                self._playing = False

    def _on_route_progress(self, i: int, total: int, lat: float, lon: float):
        """Draw the part already walked and say how much is left.

        Without this the only sign a route is running is a coordinate readout,
        which is indistinguishable from nothing happening when you are moving at
        walking pace.
        """
        self._travelled.append((lat, lon))
        if self._travel_ov is None:
            self._travel_ov = self.map.add_path(self._travelled, color=theme.LIVE, width=6, z=6)
        else:
            self.map.update_path(self._travel_ov, self._travelled)
        self._route_at = (i, total)
        left = _clock(max(0, total - i) * ROUTE_DT)
        self.hint.emit(f"{self._transport}…  {i * 100 // max(total, 1)}%  ·  {left} to go  "
                       f"·  {i}/{total} fixes")

    def _on_route_done(self, msg: str):
        self._playing = False
        self._following = False
        self.playingChanged.emit(False)
        self.hint.emit(msg)
        if self._live_pos:
            self._anchor = self._live_pos
        self.persist_spoof()         # remember where the route left the phone

    def load_route(self, pts):
        self.clear_route()
        self.points = [tuple(p) for p in pts]
        self._route_is_track = True      # a recorded track already has its own start
        self._redraw_waypoints()
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
        pts, _ = self.route_plan()       # export what Start would actually walk
        if len(pts) < 2:
            self.hint.emit("Drop a destination (or import a track) to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export route as GPX", "spoofr-route.gpx", "GPX track (*.gpx)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(route.build_gpx(pts, "Spoofr route"))
            self.hint.emit(f"Exported {len(pts)} points → {path}")
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
