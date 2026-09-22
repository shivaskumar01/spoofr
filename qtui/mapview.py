"""MapPanel, the map and everything that floats on it.

Owns the full-bleed TileMap plus the floating chrome (Teleport/Route switch,
search + autocomplete, 'Set location here', the route card, the walk pad, the
locate and zoom buttons) and all the teleport / walk / route behaviour. Talks to
the device only through the DeviceBridge.
"""

from __future__ import annotations

import math
import os
import random
import subprocess
import threading
import time

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout,
)

from . import geo, route, store, theme
from .controlbar import RouteCard
from .markers import PulseMarker, make_pin, make_waypoint
from .tilemap import TileMap
from .widgets import (
    Segmented, hairline, icon_arrow, icon_locate, icon_plus, icon_search,
)

_M_PER_DEG = 111_320.0
ROUTE_DT = 1.0            # seconds between route fixes (real GPS is about 1 Hz)
ORIGIN_MAX_M = 100_000.0  # past this, "start from where you are" stops meaning anything
EDGE = 16                 # gap between floating chrome and the map's edge
SNAP_CACHE_MAX = 32       # road-snapped plans kept, keyed by (points, profile)
M_PER_MILE = 1609.344


def _clock(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _duration(seconds: float) -> str:
    """'<1 min', '23 min', '1 h 05 min'."""
    m = int(round(seconds / 60.0))
    if m < 1:
        return "<1 min"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _distance(m: float) -> str:
    """Miles, or feet under a tenth of a mile."""
    mi = m / M_PER_MILE
    if mi < 0.1:
        return f"{m * 3.28084:,.0f} ft"
    return f"{mi:.2f} mi" if mi < 1 else f"{mi:,.1f} mi"


def _path_length(pts) -> float:
    return sum(route.meters(a, b) for a, b in zip(pts, pts[1:]))


def _jitter(lat: float, lon: float, radius_m: float = 4.0) -> tuple[float, float]:
    """Nudge a point by a random offset within `radius_m` metres."""
    r = radius_m * math.sqrt(random.random())
    th = random.uniform(0.0, 2.0 * math.pi)
    dlat = (r * math.cos(th)) / _M_PER_DEG
    dlon = (r * math.sin(th)) / (_M_PER_DEG * max(0.15, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def _float_frame(parent) -> QFrame:
    f = QFrame(parent)
    f.setObjectName("Float")
    return f


def _icon_btn(pixmap, w: int = 36, h: int = 36, tip: str = "") -> QPushButton:
    b = QPushButton()
    b.setProperty("variant", "icon")
    b.setIcon(QIcon(pixmap))
    dpr = pixmap.devicePixelRatio() or 1.0
    b.setIconSize(QSize(round(pixmap.width() / dpr), round(pixmap.height() / dpr)))
    b.setFixedSize(w, h)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
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
            f"#Srow {{ border-radius: 8px; }} #Srow:hover {{ background: {theme.GHOST_HI}; }}")

    def mousePressEvent(self, e):
        self._on_pick(self._item)


def _place_label(item: dict) -> str:
    """'Rockefeller Center, New York' from a search suggestion."""
    label, sec = item.get("label") or "", item.get("secondary") or ""
    first = sec.split(", ")[0] if sec else ""
    return f"{label}, {first}" if first and first != label else label


class MapPanel(QFrame):
    hint = Signal(str)
    readout = Signal(str)                # "lat, lon" of the iPhone, "" when unknown
    committed = Signal(float, float, str)   # a teleport landed (-> recents), label may be ""
    placeNamed = Signal(float, float, str)  # a recent's name arrived later (reverse geocode)
    modeChanged = Signal(str)            # "teleport" | "route"
    playingChanged = Signal(bool)        # a route started / stopped
    routeOffline = Signal(bool)          # a running route can't reach the iPhone
    estimateChanged = Signal(str)        # route card: "1.2 mi · 23 min"
    # worker-thread results, marshalled back to the GUI thread
    _suggestReady = Signal(str, object)
    _geocodeReady = Signal(float, float, str)
    _geocodeFailed = Signal(str)
    _walkStep = Signal(float, float)
    _jitterStep = Signal(float, float)
    _planReady = Signal(object, object)               # key, route.Snapped
    _routeProgress = Signal(int, int, float, float)   # i, total, lat, lon
    _routeDone = Signal(str)
    _named = Signal(float, float, str)

    def __init__(self, bridge, settings=None, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.settings = settings if settings is not None else {}
        self.setObjectName("MapPanel")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.map = TileMap(self)
        lay.addWidget(self.map)

        self.pending: tuple[float, float] | None = None
        self._pending_label = ""
        self._commit_label = ""
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
        self._pad_wanted = False        # walk pad: shown while connected, in Teleport

        # route state
        self.mode = "teleport"          # "teleport" | "route"
        self.profile = "walking"        # OSRM profile from the speed preset
        self._transport = "Walking"     # display verb for the route status line
        self.loop = False
        self.bounce = False
        self.snap = False
        self.points: list[tuple[float, float]] = []   # waypoints
        self._wp_ovs: list = []         # waypoint marker overlays
        self._path_ov = None            # planned route polyline
        self._plan_key = None           # (points, profile) the router is being asked about
        self._plan_m = 0.0              # length of the plan on screen
        self._snap_cache: dict = {}     # (points, profile) -> route.Snapped
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
        self._sync_chrome()
        threading.Thread(target=self._jitter_worker, daemon=True).start()

    # ---- floating chrome ------------------------------------------------

    def _build_floating(self):
        # Teleport / Route (top-left)
        self.mode_box = _float_frame(self.map)
        ml = QHBoxLayout(self.mode_box)
        ml.setContentsMargins(4, 4, 4, 4)
        self.mode_seg = Segmented(["Teleport", "Route"], height=32, font_pt=12)
        self.mode_seg.setStyleSheet("#Seg { background: transparent; }")
        ml.addWidget(self.mode_seg)

        # search (top-centre)
        self.search_bar = _float_frame(self.map)
        sl = QHBoxLayout(self.search_bar)
        sl.setContentsMargins(14, 0, 14, 0); sl.setSpacing(9)
        glass = QLabel(); glass.setPixmap(icon_search())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search a place, or paste lat, lon")
        self.search.setFixedHeight(40)
        self.search.setFont(theme.ui_font(14))
        sl.addWidget(glass); sl.addWidget(self.search, 1)
        self.search_bar.setFixedHeight(42)

        # autocomplete dropdown (under search)
        self.suggest_box = _float_frame(self.map)
        self._suggest_lay = QVBoxLayout(self.suggest_box)
        self._suggest_lay.setContentsMargins(6, 6, 6, 6); self._suggest_lay.setSpacing(2)
        self.suggest_box.hide()

        # locate + zoom (bottom-right)
        self.locate_box = _float_frame(self.map)
        ll = QVBoxLayout(self.locate_box); ll.setContentsMargins(3, 3, 3, 3)
        self.locate_btn = _icon_btn(icon_locate(), 36, 36, "Show my iPhone")
        ll.addWidget(self.locate_btn)
        self.zoom_box = _float_frame(self.map)
        zl = QVBoxLayout(self.zoom_box)
        zl.setContentsMargins(3, 3, 3, 3); zl.setSpacing(2)
        zin = _icon_btn(icon_plus(), 36, 34, "Zoom in")
        zout = _icon_btn(icon_plus(minus=True), 36, 34, "Zoom out")
        zl.addWidget(zin); zl.addWidget(hairline()); zl.addWidget(zout)
        zin.clicked.connect(lambda: self.map.zoom_at(1.0))
        zout.clicked.connect(lambda: self.map.zoom_at(-1.0))

        # 'Set location here' (bottom-centre, only while a pin is staged)
        self.set_btn = QPushButton("Set location here", self.map)
        self.set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_btn.setFixedSize(212, 46)
        self.set_btn.setFont(theme.ui_font(15, weight=600))
        self.set_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BLUE}; color: #fff; border: none; border-radius: 23px; }}"
            f"QPushButton:hover {{ background: {theme.BLUE_HI}; }}")

        # route card (bottom-centre, Route mode)
        self.route_card = RouteCard(self, parent=self.map)

        self._suggest_timer = QTimer(self)
        self._suggest_timer.setSingleShot(True)
        self._suggest_timer.timeout.connect(self._fire_suggest)
        # ask the router once the user stops adding waypoints for a beat
        self._plan_timer = QTimer(self)
        self._plan_timer.setSingleShot(True)
        self._plan_timer.setInterval(350)
        self._plan_timer.timeout.connect(self._fetch_plan)

    def _wire(self):
        self.search.installEventFilter(self)   # Esc dismisses the suggestions
        self.map.clicked.connect(self._on_click)
        self.mode_seg.changed.connect(lambda v: self.set_mode(v.lower()))
        self.set_btn.clicked.connect(self._commit)
        self.locate_btn.clicked.connect(self.show_me)
        self.bridge.located.connect(self._on_located)
        self.bridge.restored.connect(self.on_restored)
        self.search.textEdited.connect(self._on_type)
        self.search.returnPressed.connect(self._on_enter)
        self._suggestReady.connect(self._show_suggestions)
        self._geocodeReady.connect(lambda la, lo, lb: self.goto(la, lo, label=lb))
        self._geocodeFailed.connect(lambda m: self.hint.emit(m))
        self._walkStep.connect(self._on_walk_step)
        self._jitterStep.connect(self._on_jitter_step)
        self._planReady.connect(self._on_plan_ready)
        self._routeProgress.connect(self._on_route_progress)
        self.routeOffline.connect(self._on_route_offline)
        self.playingChanged.connect(self.hold_awake)
        self.playingChanged.connect(lambda _p: self._sync_chrome())
        self._routeDone.connect(self._on_route_done)
        self._named.connect(self.placeNamed)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_floating()

    def _layout_floating(self):
        m = self.map
        W, H = m.width(), m.height()
        for w in (self.mode_box, self.locate_box, self.zoom_box, self.route_card):
            w.adjustSize()
        self.mode_box.move(EDGE, EDGE)
        # the search bar takes what is left between the mode switch and the far
        # edge, centred, and never wider than is comfortable to read
        room = W - 2 * (EDGE + self.mode_box.width() + 12)
        sw = max(240, min(400, room))
        sx = (W - sw) // 2
        self.search_bar.setFixedWidth(sw)
        self.search_bar.move(sx, EDGE)
        self.suggest_box.setFixedWidth(sw)
        self.suggest_box.adjustSize()
        self.suggest_box.move(sx, EDGE + self.search_bar.height() + 8)
        zx = W - self.zoom_box.width() - EDGE
        self.zoom_box.move(zx, H - self.zoom_box.height() - EDGE)
        self.locate_box.move(W - self.locate_box.width() - EDGE,
                             self.zoom_box.y() - self.locate_box.height() - 10)
        bottom = H - EDGE
        if self.route_card.isVisible():
            self.route_card.move((W - self.route_card.width()) // 2, bottom - self.route_card.height())
            bottom = self.route_card.y() - 10
        self.set_btn.move((W - self.set_btn.width()) // 2, bottom - self.set_btn.height())
        self.walk_pad.adjustSize()
        self.walk_pad.move(EDGE, H - self.walk_pad.height() - EDGE)

    def _sync_chrome(self):
        """Show exactly the floating controls that make sense right now."""
        route_mode = self.mode == "route"
        self.route_card.setVisible(route_mode or self._playing)
        self.set_btn.setVisible(bool(self.pending) and not route_mode)
        # the pad would fight a route for the phone, so it steps aside for one
        self.walk_pad.setVisible(self._pad_wanted and not route_mode and not self._playing)
        self._layout_floating()
        for w in (self.set_btn, self.route_card, self.walk_pad):
            if w.isVisible():
                w.raise_()

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
        self.search.clearFocus()
        if self.mode == "route":
            if self._playing:
                self.hint.emit("A route is running. Stop it to change the route.")
                return
            self._add_waypoint(lat, lon)
        else:
            self._stage(lat, lon)

    def _stage(self, lat: float, lon: float, label: str = ""):
        """Drop/move the staged teleport pin (a candidate, not yet sent)."""
        self.pending = (lat, lon)
        self._pending_label = label
        if self._pin_ov is None:
            self._pin_ov = self.map.add_marker(lat, lon, self._pin, anchor="s")
        else:
            self.map.move_marker(self._pin_ov, lat, lon)
        self._sync_chrome()
        where = label or f"{lat:.5f}, {lon:.5f}"
        self.hint.emit(f"Pinned {where}. Click “Set location here” to move your iPhone.")

    def _commit(self):
        if not self.pending:
            return
        if self.bridge.device is None:
            self.hint.emit("Connect to your iPhone first.")
            return
        # an explicit teleport wins: a running route or walk would otherwise
        # drag the phone straight back a second later
        if self._playing or self._walking:
            self.stop_motion()
        self._commit_label = self._pending_label
        self.bridge.set_location(*self.pending)

    def teleport(self, lat: float, lon: float, label: str = ""):
        """Go there now (saved places, the menu bar): stage it and send it."""
        self.goto(lat, lon, label=label)
        self._commit()

    def _set_live(self, lat: float, lon: float):
        """Show/move the live 'You' marker + readout (teleport set or walk step)."""
        self._live_pos = (lat, lon)
        if self._live is None:
            self._live = PulseMarker()
            self._live.set_pulsing(self._pulse)
            self._live_ov = self.map.add_item(self._live, lat, lon)
        else:
            self.map.move_marker(self._live_ov, lat, lon)
        self.readout.emit(f"{lat:.5f}, {lon:.5f}")

    def _on_located(self, lat: float, lon: float):
        # a teleport set landed: live marker, drop the staged pin, log a recent
        self._set_live(lat, lon)
        self._anchor = (lat, lon)
        self._active_spoof = (lat, lon)
        self.persist_spoof()
        if self._pin_ov is not None:
            self.map.remove_overlay(self._pin_ov); self._pin_ov = None
        self.pending = None
        label, self._commit_label = self._commit_label, ""
        self._sync_chrome()
        self.committed.emit(lat, lon, label)
        if not label:
            threading.Thread(target=self._name_worker, args=(lat, lon), daemon=True).start()
        if self.points and not self._playing:
            self._refresh_plan()         # the route now starts from here

    def _name_worker(self, lat: float, lon: float):
        name = geo.reverse(lat, lon)
        if name:
            self._named.emit(lat, lon, name)

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
        self._commit_label = ""
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
        self._sync_chrome()
        self.readout.emit("")

    def on_restored(self):
        """Spoof cleared → drop the staged pin and stop wobbling, but KEEP the live
        'You' marker on the map (re-located to the user's real/current location).
        The marker should always be visible while connected."""
        self.stop_motion()
        if self._pin_ov is not None:
            self.map.remove_overlay(self._pin_ov)
            self._pin_ov = None
        self.pending = None
        self._sync_chrome()
        self._anchor = None              # nothing to jitter around once the spoof is cleared
        self._active_spoof = None        # phone is back on real GPS, forget the saved spoof
        self.persist_spoof()
        self.bridge.locate()             # re-show the live marker at the current location

    def goto(self, lat: float, lon: float, zoom: float = 15, label: str = ""):
        if self.mode == "route":
            self.set_mode("teleport")    # search/place implies teleport, not a waypoint
        self.map.set_view(lat, lon, max(zoom, self.map.zoom))
        self._stage(lat, lon, label)     # stage it too, so one tap sets it

    def locate_me(self, lat: float, lon: float):
        """Fly to the user's current location and show the 'You' marker,
        informational only (no spoof, no jitter anchor). Street zoom: the packaged
        app gets an exact CoreLocation fix; a plain dev run gets IP (city), either
        way the user clicks/searches to set the iPhone."""
        self.map.set_view(lat, lon, max(15, self.map.zoom))
        self._set_live(lat, lon)
        self.hint.emit("Centred on your location. Click anywhere or search, then “Set location here”.")

    def show_me(self):
        """The locate button: bring the iPhone's marker back into view."""
        if self._live_pos is None:
            self.hint.emit("No location yet. Connect, then set one.")
            return
        self.map.set_view(self._live_pos[0], self._live_pos[1], max(15, self.map.zoom))

    # ---- walk pad (joystick) + arrow keys -------------------------------

    def _build_walk_pad(self):
        """A four-way pad (hold to walk). Diagonals come from two arrow keys."""
        self.walk_pad = _float_frame(self.map)
        grid = QGridLayout(self.walk_pad)
        grid.setContentsMargins(6, 6, 6, 6); grid.setSpacing(2)
        for (row, col), angle, vec, tip in (((0, 1), 0, (1, 0), "Walk north"),
                                            ((1, 0), -90, (0, -1), "Walk west"),
                                            ((1, 2), 90, (0, 1), "Walk east"),
                                            ((2, 1), 180, (-1, 0), "Walk south")):
            b = _icon_btn(icon_arrow(angle), 32, 32, tip)
            grid.addWidget(b, row, col)
            b.pressed.connect(lambda v=vec: self._walk_press(v))
            b.released.connect(self._walk_release)
        dot = QLabel("●")
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dot.setFixedSize(32, 32)
        dot.setStyleSheet(f"color: {theme.LIVE}; font-size: 9px;")
        dot.setToolTip("Hold a direction, or use the arrow keys")
        grid.addWidget(dot, 1, 1)
        self.walk_pad.hide()

    def show_walk_pad(self, show: bool):
        self._pad_wanted = bool(show)
        if not show:
            self._walk_release()
        self._sync_chrome()

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
        if self._playing:
            # both would write the phone every second and fight over it
            self._walk_vec = (0.0, 0.0)
            self.hint.emit("A route is running. Stop it to walk by hand.")
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
        mode = "route" if mode == "route" else "teleport"
        changed = mode != self.mode
        self.mode = mode
        self.mode_seg.set_value(mode.capitalize())
        if mode == "route":
            self._walk_release()
            self.hint.emit("Route: click where you want to end up. Your iPhone starts from "
                           "where it is now; extra clicks add stops on the way.")
            self._refresh_plan()
        else:
            self.hint.emit("Teleport: click the map or search, then “Set location here”.")
        self._sync_chrome()
        if changed:
            self.modeChanged.emit(mode)

    def set_speed(self, mps: float):
        self.speed = float(mps)
        self._emit_estimate()

    def set_preset(self, profile: str, gerund: str = "Walking"):
        changed = profile != self.profile
        self.profile = profile
        self._transport = gerund        # "Walking"/"Running"/"Cycling"/"Driving"
        if changed and self.snap:
            self._refresh_plan()        # a walk and a drive take different streets

    def set_loop(self, on: bool):
        self.loop = bool(on)
        self._emit_estimate()

    def set_bounce(self, on: bool):
        self.bounce = bool(on)
        self._emit_estimate()

    def set_snap(self, on: bool):
        self.snap = bool(on)
        self._refresh_plan()

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
        self._refresh_plan()
        n = len(self.points)
        if n == 1:
            if self.route_origin() is not None:
                self.hint.emit("Destination set. Press Start to head there from where your "
                               "iPhone is now, or click again to add a stop on the way.")
            else:
                self.hint.emit("Destination set. Set your iPhone’s location first so the "
                               "route has somewhere to start, or drop a second waypoint.")
        else:
            self.hint.emit(f"{n} stops, green is the destination. Press Start, or keep adding.")

    def undo_waypoint(self):
        if self._playing or not self.points:
            return
        self.points.pop()
        if not self.points:
            self._route_is_track = False
        self._redraw_waypoints()
        self._refresh_plan()
        self.hint.emit(f"{len(self.points)} stop(s) left." if self.points else "Waypoints cleared.")

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

    # -- the planned path: what Start would walk, drawn before you press it --

    def _refresh_plan(self):
        """Redraw the route Start would walk, from where the phone is now.

        With snap-to-roads on, a dotted straight preview shows at once and the
        router is asked after a short pause; its answer is cached, so pressing
        Start plays exactly the path on screen without asking again.
        """
        self._plan_timer.stop()
        pts, _ = self.route_plan()
        if len(pts) < 2:
            self._plan_key = None
            self._show_plan(None)
            return
        if self.snap and not self._route_is_track:
            key = (tuple(pts), self.profile)
            self._plan_key = key
            cached = self._snap_cache.get(key)
            if cached is not None:
                self._show_plan(cached.points)
                return
            self._show_plan(pts, pending=True)
            self._plan_timer.start()
        else:
            self._plan_key = None
            self._show_plan(pts)

    def _fetch_plan(self):
        key = self._plan_key
        if key is not None:
            threading.Thread(target=self._plan_worker, args=(key,), daemon=True).start()

    def _plan_worker(self, key):
        pts, profile = key
        self._planReady.emit(key, route.snap_to_roads(list(pts), profile))

    def _on_plan_ready(self, key, snapped):
        if snapped.ok:
            if len(self._snap_cache) >= SNAP_CACHE_MAX:
                self._snap_cache.pop(next(iter(self._snap_cache)))
            self._snap_cache[key] = snapped
        if key != self._plan_key or self._playing:
            return                       # the waypoints moved on since we asked
        if snapped.ok:
            self._show_plan(snapped.points)
        else:
            self._show_plan(list(key[0]))
            self.hint.emit("Couldn’t reach the road router, the route will go in straight lines.")

    def _show_plan(self, pts, pending: bool = False):
        if not pts or len(pts) < 2:
            if self._path_ov is not None:
                self.map.remove_overlay(self._path_ov)
                self._path_ov = None
            self._plan_m = 0.0
        else:
            color = theme.MUTED if pending else theme.BLUE
            width = 4 if pending else 5
            if self._path_ov is None:
                self._path_ov = self.map.add_path(pts, color=color, width=width, dashed=pending)
            else:
                self.map.update_path(self._path_ov, pts)
                self.map.style_path(self._path_ov, color, width, dashed=pending)
            self._plan_m = _path_length(pts)
        self._emit_estimate()

    def _emit_estimate(self):
        if self._playing:
            return                       # the card shows progress instead
        if self._plan_m <= 0:
            self.estimateChanged.emit("")
            return
        m = self._plan_m * (2 if self.bounce else 1)
        text = f"{_distance(m)}  ·  {_duration(m / max(self.speed, 0.3))}"
        self.estimateChanged.emit(text + ("  ·  loops" if self.loop else ""))

    def clear_route(self):
        self.stop_route()
        self._clear_travelled()
        self._route_is_track = False
        for ov in self._wp_ovs:
            self.map.remove_overlay(ov)
        self._wp_ovs.clear()
        self.points.clear()
        self._plan_key = None
        self._plan_timer.stop()
        self._show_plan(None)
        self.hint.emit("Waypoints cleared.")

    def toggle_route(self):
        self.stop_route() if self._playing else self.start_route()

    def start_route(self):
        if self.bridge.device is None:
            self.hint.emit("Connect to your iPhone first.")
            return
        if self._playing:
            return
        pts, from_here = self.route_plan()
        if len(pts) < 2:
            self.hint.emit(self._why_no_route())
            return
        self._walk_release()             # the route owns the phone now
        self._plan_timer.stop()
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
            "Starting at waypoint 1, your iPhone jumps there first, then follows the route.")
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
            return (f"Your iPhone is {km:,.0f} km from that pin, too far to route from "
                    f"where it is. Set its location nearer first, or drop a second waypoint.")
        return ("That pin is where your iPhone already is. Pick a destination a little "
                "further away.")

    def stop_route(self):
        was = self._playing
        self._route_gen += 1          # invalidate any running route worker
        self._playing = False
        self._following = False
        self.playingChanged.emit(False)
        if was:
            self._emit_estimate()

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
        halfway through and nothing says why. `-w` ties caffeinate to this
        process, so a crash can't leave the Mac unable to sleep forever.
        Best-effort: without caffeinate the route still runs, unprotected.
        """
        if on and self._caffeinate is None:
            try:
                self._caffeinate = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self._caffeinate = None
        elif not on and self._caffeinate is not None:
            proc, self._caffeinate = self._caffeinate, None
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                pass

    def _on_route_offline(self, offline: bool):
        if offline:
            i, total = self._route_at
            self.hint.emit(f"Out of touch with your iPhone at {i}/{total}. The route keeps "
                           "running; reconnect and it will be wherever the route has got to.")
        else:
            self.hint.emit("iPhone is back, catching it up to the route.")

    def _route_sleep(self, gen: int, dt: float) -> bool:
        """Sleep up to dt; return True early if this route was stopped/superseded."""
        end = time.monotonic() + dt
        while time.monotonic() < end:
            if self._route_stale(gen):
                return True
            time.sleep(0.05)
        return self._route_stale(gen)

    def _route_worker(self, pts, gen: int):
        try:
            if self.snap and not self._route_is_track:
                key = (tuple(pts), self.profile)
                snapped = self._snap_cache.get(key)
                if snapped is None:
                    self.hint.emit(f"Finding a {self._transport.lower()} route along real roads…")
                    snapped = route.snap_to_roads(pts, self.profile)
                    self._planReady.emit(key, snapped)     # cache + draw on the GUI thread
                if self._route_stale(gen):
                    self._routeDone.emit("Route stopped.")
                    return
                if snapped.ok:
                    pts = snapped.points
                else:
                    self.hint.emit("Couldn’t reach the road router, going straight between waypoints.")
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
        if i < self._route_at[0]:
            self._clear_travelled()      # a loop came round again: start a fresh trail
        self._travelled.append((lat, lon))
        if self._travel_ov is None:
            self._travel_ov = self.map.add_path(self._travelled, color=theme.LIVE, width=6, z=6)
        else:
            self.map.update_path(self._travel_ov, self._travelled)
        self._route_at = (i, total)
        pct = i * 100 // max(total, 1)
        left = _clock(max(0, total - i) * ROUTE_DT)
        self.estimateChanged.emit(f"{pct}%  ·  {left} left")
        self.hint.emit(f"{self._transport}…  {pct}%  ·  {left} to go")

    def _on_route_done(self, msg: str):
        self._playing = False
        self._following = False
        self.playingChanged.emit(False)
        self.hint.emit(msg)
        if self._live_pos:
            self._anchor = self._live_pos
        self.persist_spoof()         # remember where the route left the phone
        if msg == "Route complete.":
            self._show_plan(None)    # the cyan trail now shows the way it went
        else:
            self._refresh_plan()     # Start again would go from here

    def load_route(self, pts):
        self.clear_route()
        self.points = [tuple(p) for p in pts]
        self._route_is_track = True      # a recorded track already has its own start
        self._redraw_waypoints()
        self._refresh_plan()
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
        if self.snap and not self._route_is_track:
            cached = self._snap_cache.get((tuple(pts), self.profile))
            if cached is not None:
                pts = cached.points      # the road-following path that is on screen
        path, _ = QFileDialog.getSaveFileName(
            self, "Export route as GPX", "spoofr-route.gpx", "GPX track (*.gpx)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(route.build_gpx(pts, "Spoofr route"))
            self.hint.emit(f"Exported {len(pts)} points to {path}")
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
        self.suggest_box.adjustSize()
        self.suggest_box.move(self.search_bar.x(), self.search_bar.y() + self.search_bar.height() + 8)
        self.suggest_box.show(); self.suggest_box.raise_()

    def _hide_suggestions(self):
        self.suggest_box.hide()
        self._suggest_items = []

    def _pick(self, item: dict):
        self._hide_suggestions()
        self.search.setText(item["label"])
        self.search.clearFocus()
        self.goto(item["lat"], item["lon"], label=_place_label(item))

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
            self._geocodeReady.emit(lat, lon, q)
        except Exception as e:
            self._geocodeFailed.emit(str(e))
