"""MapScreen: the map fills the window; everything else floats on top.

    top-left      status pill (device + link; click for details / disconnect)
    top-centre    search (addresses, places, pasted coordinates)
    right         the control panel (Places · pin · route builder · running)
    bottom-right  recenter, zoom, map style
    bottom-left   walk pad (hold a direction, or use the arrow keys)

This is also the controller for everything you do on the map: dropping pins,
teleporting, building and running routes, and the route session that keeps
going when the phone is out of reach. It talks to the phone only through the
DeviceBridge's single write path.
"""

from __future__ import annotations

import copy
import math
import os
import random
import subprocess
import threading
import time

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPropertyAnimation, Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QMessageBox, QVBoxLayout, QWidget,
)

from . import geo, route, store, theme
from .markers import SpoofMarker, make_end, make_pin, make_real_marker, make_start, make_stop
from .panel import ControlPanel, WIDTH as PANEL_W, fmt_distance
from .session import KMH, Options, Plan, RouteSession, Runner, meters
from .tilemap import TileMap
from .welcome import WelcomeOverlay, link_word
from .widgets import (
    Dot, ElidedLabel, IconButton, Toast, button, hairline, icon_arrow, icon_layers,
    icon_locate, icon_menu, icon_minus, icon_panel, icon_plus, icon_search, label,
)

EDGE = 16                 # gap between floating chrome and the map's edge
NARROW = 980              # below this map width the panel becomes a toggleable sheet
ORIGIN_MAX_M = 100_000.0  # past this, "start from where you are" stops meaning anything
RECENT_MAX = 20
SNAP_CACHE_MAX = 32
_M_PER_DEG = 111_320.0


def _jitter(lat: float, lon: float, radius_m: float = 4.0) -> tuple[float, float]:
    r = radius_m * math.sqrt(random.random())
    th = random.uniform(0.0, 2.0 * math.pi)
    dlat = (r * math.cos(th)) / _M_PER_DEG
    dlon = (r * math.sin(th)) / (_M_PER_DEG * max(0.15, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def default_units() -> str:
    from PySide6.QtCore import QLocale
    return "mph" if QLocale.system().measurementSystem() in (
        QLocale.MeasurementSystem.ImperialUSSystem,
        QLocale.MeasurementSystem.ImperialUKSystem) else "kmh"


# ---- the route being planned ---------------------------------------------------

class Draft:
    """A route before it runs: where it goes, how, and how fast."""

    def __init__(self, stops=None, style: str = "roads", profile: str = "walking",
                 speed_kmh: float = 5.0, opts: dict | None = None, name: str = "",
                 start: dict | None = None):
        self.stops: list[dict] = [dict(s) for s in (stops or [])]   # last one = the end
        self.style = style
        self.profile = profile
        self.speed_kmh = speed_kmh
        self.opts = {"end": "stop", "laps": 1, "dwell_s": 0, "variation": False, "jitter": False}
        self.opts.update(opts or {})
        self.name = name
        self.start = start                   # None = wherever the iPhone is

    def to_dict(self) -> dict:
        return {"stops": self.stops, "style": self.style, "profile": self.profile,
                "speed_kmh": self.speed_kmh, "opts": self.opts, "name": self.name,
                "start": self.start}

    @classmethod
    def from_dict(cls, d: dict) -> "Draft":
        return cls(d.get("stops"), d.get("style", "roads"), d.get("profile", "walking"),
                   d.get("speed_kmh", 5.0), d.get("opts"), d.get("name", ""), d.get("start"))


# ---- floating pieces ---------------------------------------------------------

class StatusPill(QFrame):
    clicked = Signal()

    def __init__(self, on_menu, parent=None):
        super().__init__(parent)
        self.setObjectName("Float")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 14, 4)
        lay.setSpacing(8)
        self.menu = IconButton(icon_menu, "Settings and help", size=32, icon_px=17)
        self.menu.clicked.connect(on_menu)
        lay.addWidget(self.menu)
        lay.addWidget(hairline(vertical=True))
        self.dot = Dot(theme.GREY, 9)
        lay.addWidget(self.dot)
        self.text = ElidedLabel("Disconnected")
        self.text.setFont(theme.ui_font(13, 600))
        self.text.setMinimumWidth(80)
        self.text.setMaximumWidth(300)
        lay.addWidget(self.text, 1)
        self.setAccessibleName("Connection status")

    def set(self, text: str, color: str):
        self.text.setText(text)
        self.text.setToolTip(text)
        fm = self.text.fontMetrics()
        self.text.setFixedWidth(min(300, fm.horizontalAdvance(text) + 4))
        self.text.update()
        self.dot.set_color(color)
        self.adjustSize()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class DevicePopover(QFrame):
    """Device details, the one-time wireless switch, and Disconnect."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Float")
        self.setFixedWidth(280)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        self.name = label("", 15, 700)
        lay.addWidget(self.name)
        self.rows = label("", 12, role="muted", wrap=True, num=True)
        lay.addWidget(self.rows)
        self.wifi = label("", 11, role="muted", wrap=True)
        lay.addWidget(self.wifi)
        self.wifi_btn = button("Set up Wi-Fi control", "soft", height=30)
        lay.addWidget(self.wifi_btn)
        self.phone_btn = button("Control from your iPhone…", "soft", height=30,
                                tip="Hand control to the phone with a QR code")
        lay.addWidget(self.phone_btn)
        self.action = button("Disconnect", "ghost", height=32)
        lay.addWidget(self.action)
        self.hide()


class Banner(QFrame):
    """A one-line question with up to two answers (resume, re-apply…)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(8)
        self.text = label("", 13, 500, wrap=True)
        self.text.setMaximumWidth(340)
        lay.addWidget(self.text, 1)
        self.a = button("", "primary", height=30)
        self.b = button("", "soft", height=30)
        lay.addWidget(self.a)
        lay.addWidget(self.b)
        self._cbs = (None, None)
        self.a.clicked.connect(lambda: self._fire(0))
        self.b.clicked.connect(lambda: self._fire(1))
        self.hide()

    def ask(self, text: str, a: str, on_a, b: str = "", on_b=None):
        self.text.setText(text)
        self.a.setText(a)
        self.b.setText(b)
        self.b.setVisible(bool(b))
        self._cbs = (on_a, on_b)
        self.adjustSize()
        self.show(); self.raise_()

    def _fire(self, i: int):
        cb = self._cbs[i]
        self.hide()
        if cb:
            cb()


class _SuggestRow(QFrame):
    def __init__(self, item: dict, on_pick):
        super().__init__()
        self._item, self._on_pick = item, on_pick
        self.setObjectName("Row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 7, 12, 7)
        lay.setSpacing(1)
        lay.addWidget(label(item["label"], 13, 600))
        if item.get("secondary"):
            lay.addWidget(label(item["secondary"], 12, role="muted"))

    def mouseReleaseEvent(self, e):
        self._on_pick(self._item)


def _place_label(item: dict) -> str:
    """'Rockefeller Center, New York' from a search suggestion."""
    lb, sec = item.get("label") or "", item.get("secondary") or ""
    first = sec.split(", ")[0] if sec else ""
    return f"{lb}, {first}" if first and first != lb else lb


# ---- the screen ----------------------------------------------------------------

class MapScreen(QWidget):
    openSettings = Signal()
    openPortable = Signal()
    goWireless = Signal()
    disconnectAsked = Signal(bool)        # restore real location first?
    connectAsked = Signal(str)            # serial
    sessionChanged = Signal()             # for the menu bar
    progress = Signal(str)                # menu-bar title while a route runs
    readout = Signal(str)
    _suggestReady = Signal(str, object)
    _geocodeReady = Signal(float, float, str)
    _geocodeFailed = Signal(str)
    _named = Signal(object, float, float, str)   # (what, lat, lon, name)
    _planReady = Signal(object, object)
    _tick = Signal(object)
    _walkStep = Signal(float, float)
    _jitterStep = Signal(float, float)

    def __init__(self, bridge, settings: dict, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.settings = settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.map = TileMap(self)
        lay.addWidget(self.map)
        self.map.set_style(settings.get("map_style", "standard"))

        # device + location state
        self.conn_state = "disconnected"     # disconnected|ready|connecting|connected|reconnecting
        self.ready_link = ""
        self.device: dict | None = None      # {udid, name, model, ios, link, wireless_on}
        self.spoof: tuple[float, float] | None = None
        self.spoof_name = ""
        self.real: tuple[float, float] | None = None
        self._spoof_ov = self._spoof_item = None
        self._real_ov = None
        self._teleport_label: str | None = None
        self._quiet_teleport = False
        self._center_on_real = False
        self._glide = None

        # pin
        self.pin: dict | None = None
        self._pin_ov = None
        self._pin_px = make_pin()

        # route
        self.draft: Draft | None = None
        self._plan: dict = {}
        self._plan_key = None
        self._snap_cache: dict = {}
        self._route_ovs: list = []
        self._line_ov = None
        self._travel_ov = None
        self._skip_ov = None
        self.session: RouteSession | None = None
        self.session_udid = ""
        self.runner: Runner | None = None
        self.summary: dict | None = None
        self.follow = True
        self._follow_paused = False
        self._resume_pending = False
        self._last_tick = None
        self._caffeinate = None

        # walking / jitter
        self._walk_vec = (0.0, 0.0)
        self._walk_pos = None
        self._walking = False
        self._keys_down: set[str] = set()
        self._jitter_on = bool(settings.get("jitter", False))
        self._pulse = True
        self._closing = False
        self._pad_wanted = bool(settings.get("walk_pad", True))
        self._browsing = False
        self._force_welcome = False
        self._portable = False
        self._sheet_open = False
        self._suggest_items: list[dict] = []

        self._build()
        self._wire()
        v = settings.get("view")
        if v:
            self.map.set_view(v["lat"], v["lon"], v.get("zoom", 12))
        else:
            # never the middle of the ocean: a continent, until we know better
            us = default_units() == "mph"
            self.map.set_view(39.5 if us else 48.5, -98.35 if us else 9.5, 4)
        self._sync_chrome()
        threading.Thread(target=self._jitter_worker, daemon=True).start()

    # ------------------------------------------------------------------ build

    def _build(self):
        m = self.map
        self.pill = StatusPill(lambda: self.openSettings.emit(), m)
        self.pop = DevicePopover(m)

        self.search_bar = QFrame(m)
        self.search_bar.setObjectName("Float")
        sl = QHBoxLayout(self.search_bar)
        sl.setContentsMargins(14, 0, 12, 0); sl.setSpacing(9)
        self._glass = QLabel(); self._glass.setPixmap(icon_search())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search a place, or paste coordinates")
        self.search.setFont(theme.ui_font(14))
        self.search.setAccessibleName("Search")
        sl.addWidget(self._glass); sl.addWidget(self.search, 1)
        self.search_bar.setFixedHeight(42)
        self.suggest_box = QFrame(m)
        self.suggest_box.setObjectName("Float")
        self._suggest_lay = QVBoxLayout(self.suggest_box)
        self._suggest_lay.setContentsMargins(6, 6, 6, 6); self._suggest_lay.setSpacing(1)
        self.suggest_box.hide()

        self.panel = ControlPanel(self, m)
        self.sheet_box = QFrame(m); self.sheet_box.setObjectName("Float")
        sb = QHBoxLayout(self.sheet_box); sb.setContentsMargins(3, 3, 3, 3)
        self.sheet_btn = IconButton(icon_panel, "Show or hide the panel", size=36, icon_px=18)
        sb.addWidget(self.sheet_btn)
        self._sheet_anim = QPropertyAnimation(self.panel, b"pos", self)
        self._sheet_anim.setDuration(200)
        self._sheet_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._sheet_anim.finished.connect(lambda: self._layout())

        # map controls
        self.ctrl_box = QFrame(m); self.ctrl_box.setObjectName("Float")
        cl = QVBoxLayout(self.ctrl_box); cl.setContentsMargins(3, 3, 3, 3); cl.setSpacing(2)
        self.locate_btn = IconButton(icon_locate, "Recenter on your iPhone", size=36)
        self.zin = IconButton(icon_plus, "Zoom in", size=36, icon_px=16)
        self.zout = IconButton(icon_minus, "Zoom out", size=36, icon_px=16)
        self.style_btn = IconButton(icon_layers, "Map style: standard, satellite, hybrid", size=36)
        for w in (self.locate_btn, hairline(), self.zin, self.zout, hairline(), self.style_btn):
            cl.addWidget(w)
        self.style_menu = QMenu(self)
        self._style_actions = {}
        for key, text in (("standard", "Standard"), ("satellite", "Satellite"), ("hybrid", "Hybrid")):
            a = self.style_menu.addAction(text)
            a.setCheckable(True)
            a.triggered.connect(lambda _=False, k=key: self.set_map_style(k))
            self._style_actions[key] = a
        self._style_actions[self.map.style_name].setChecked(True)

        # walk pad
        self.walk_pad = QFrame(m); self.walk_pad.setObjectName("Float")
        grid = QGridLayout(self.walk_pad)
        grid.setContentsMargins(5, 5, 5, 5); grid.setSpacing(1)
        for (r, c), ang, vec, tip in (((0, 1), 0, (1, 0), "Walk north"),
                                      ((1, 0), -90, (0, -1), "Walk west"),
                                      ((1, 2), 90, (0, 1), "Walk east"),
                                      ((2, 1), 180, (-1, 0), "Walk south")):
            b = IconButton(lambda col, a=ang: icon_arrow(a, col), tip, size=30, icon_px=15)
            b.pressed.connect(lambda v=vec: self._walk_press(v))
            b.released.connect(self._walk_release)
            grid.addWidget(b, r, c)
        mid = QLabel("walk"); mid.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mid.setFont(theme.ui_font(9, 600)); mid.setProperty("role", "faint")
        mid.setFixedSize(30, 30); mid.setToolTip("Hold a direction, or use the arrow keys")
        grid.addWidget(mid, 1, 1)

        self.banner = Banner(m)
        self.toast = Toast(m)
        self.welcome = WelcomeOverlay(m)
        self.welcome.connectClicked.connect(self.connectAsked)
        self.welcome.browse.connect(self._browse)

        for w in (self.pill, self.pop, self.search_bar, self.suggest_box, self.panel,
                  self.sheet_box, self.ctrl_box, self.walk_pad, self.banner, self.toast):
            self.map.cast_shadow(w)

        self._suggest_timer = QTimer(self); self._suggest_timer.setSingleShot(True)
        self._suggest_timer.timeout.connect(self._fire_suggest)
        self._plan_timer = QTimer(self); self._plan_timer.setSingleShot(True)
        self._plan_timer.setInterval(300)
        self._plan_timer.timeout.connect(self._fetch_plan)
        self._name_timer = QTimer(self); self._name_timer.setSingleShot(True)
        self._name_timer.setInterval(450)
        self._name_timer.timeout.connect(self._lookup_pin_name)
        self._view_timer = QTimer(self); self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(1500)
        self._view_timer.timeout.connect(self._save_view)

    def _wire(self):
        m = self.map
        m.clicked.connect(self._on_map_click)
        m.userPanned.connect(self._on_user_panned)
        m.markerMoved.connect(self._on_marker_moved)
        m.markerDropped.connect(self._on_marker_dropped)
        m.viewChanged.connect(self._view_timer.start)
        self.pill.clicked.connect(self._toggle_popover)
        self.pop.action.clicked.connect(self._popover_action)
        self.pop.wifi_btn.clicked.connect(lambda: (self.pop.hide(), self.goWireless.emit()))
        self.pop.phone_btn.clicked.connect(lambda: (self.pop.hide(), self.openPortable.emit()))
        self.sheet_btn.clicked.connect(lambda: self._set_sheet(not self._sheet_open))
        self.locate_btn.clicked.connect(self.recenter)
        self.zin.clicked.connect(lambda: m.zoom_at(1.0))
        self.zout.clicked.connect(lambda: m.zoom_at(-1.0))
        self.style_btn.clicked.connect(self._show_style_menu)
        self.search.installEventFilter(self)
        self.search.textEdited.connect(self._on_type)
        self.search.returnPressed.connect(self._on_enter)
        self.bridge.located.connect(self._on_located)
        self.bridge.restored.connect(self._on_restored)
        self._suggestReady.connect(self._show_suggestions)
        self._geocodeReady.connect(lambda la, lo, lb: self.drop_pin(la, lo, name=lb, center=True))
        self._geocodeFailed.connect(self.show_toast)
        self._named.connect(self._on_named)
        self._planReady.connect(self._on_plan_ready)
        self._tick.connect(self._on_tick)
        self._walkStep.connect(self._on_walk_step)
        self._jitterStep.connect(self._on_jitter_step)

    # ------------------------------------------------------------------ layout

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout()

    def _layout(self):
        m = self.map
        W, H = m.width(), m.height()
        self.welcome.setGeometry(0, 0, W, H)
        narrow = W < NARROW
        self.pill.adjustSize()
        self.pill.move(EDGE, EDGE)
        self.ctrl_box.adjustSize()
        self.ctrl_box.move(W - self.ctrl_box.width() - EDGE, H - self.ctrl_box.height() - EDGE)
        self.sheet_box.setVisible(narrow)
        self.sheet_box.adjustSize()
        self.sheet_box.move(W - self.sheet_box.width() - EDGE, EDGE)
        # the panel: a right-hand column, or a sheet that slides in when narrow.
        # The map controls sit under it; when that would squeeze the panel, they
        # step to its left instead and the panel takes the full height.
        top = EDGE + (self.sheet_box.height() + 8 if narrow else 0)
        shown_x = W - PANEL_W - EDGE
        want = self.panel.preferred_height()
        under = self.ctrl_box.y() - 12 - top
        panel_up = self.panel.isVisible() or not narrow
        if want > under and panel_up:
            self.ctrl_box.move(shown_x - self.ctrl_box.width() - 10,
                               H - self.ctrl_box.height() - EDGE)
            max_h = H - top - EDGE
        else:
            max_h = under
        self.panel.setFixedHeight(max(160, min(max_h, want)))
        if self._sheet_anim.state() != QPropertyAnimation.State.Running:
            self.panel.setVisible(self._sheet_open if narrow else True)
            self.panel.move(shown_x, top)
        right = (shown_x - 12) if not narrow else (self.sheet_box.x() - 12)
        left = self.pill.x() + self.pill.width() + 12
        room = right - left
        sw = max(180, min(420, room))
        sx = left + max(0, (room - sw) // 2)
        self.search_bar.setFixedWidth(sw)
        self.search_bar.move(sx, EDGE)
        self.suggest_box.setFixedWidth(sw)
        self.suggest_box.adjustSize()
        self.suggest_box.move(sx, EDGE + self.search_bar.height() + 6)
        self.pop.adjustSize()
        self.pop.move(EDGE, EDGE + self.pill.height() + 8)
        self.banner.adjustSize()
        self.banner.move(max(EDGE, min(sx + (sw - self.banner.width()) // 2,
                                       (shown_x if not narrow else W) - self.banner.width() - 12)),
                         EDGE + self.search_bar.height() + 10)
        self.walk_pad.adjustSize()
        self.walk_pad.move(EDGE, H - self.walk_pad.height() - EDGE)
        self.toast.adjustSize()
        self.toast.move((W - self.toast.width()) // 2, H - self.toast.height() - 28)
        for w in (self.pill, self.search_bar, self.panel, self.sheet_box, self.ctrl_box,
                  self.walk_pad, self.banner, self.suggest_box, self.pop, self.toast):
            w.raise_()
        self.welcome.raise_()

    def _set_sheet(self, open_: bool):
        self._sheet_open = open_
        W = self.map.width()
        if W >= NARROW:
            return
        top = EDGE + self.sheet_box.height() + 8
        start = QPoint(W + 4, top) if open_ else self.panel.pos()
        end = QPoint(W - PANEL_W - EDGE, top) if open_ else QPoint(W + 4, top)
        self.panel.show(); self.panel.raise_()
        self._sheet_anim.stop()
        self._sheet_anim.setStartValue(start)
        self._sheet_anim.setEndValue(end)
        self._sheet_anim.start()

    def _sync_chrome(self):
        """Show the floating pieces that make sense right now."""
        page = ("pin" if self.pin else "running" if self.session else
                "summary" if self.summary else "builder" if self.draft else "home")
        if page != self.panel.page:
            self.panel.show_page(page)
            if self.map.width() < NARROW and page != "home" and not self._sheet_open:
                QTimer.singleShot(0, lambda: self._set_sheet(True))
        self._refresh_page()
        self.panel.set_spoofing(self.spoof is not None and self.conn_state == "connected")
        busy = self.session is not None
        self.walk_pad.setVisible(self._pad_wanted and self.conn_state == "connected"
                                 and not busy and self.draft is None)
        show_welcome = (self.conn_state in ("disconnected", "ready", "connecting")
                        and not self._browsing and not self._portable
                        and (self.session is None or self._force_welcome))
        self.welcome.setVisible(show_welcome)
        self._update_pill()
        self._layout()

    def _refresh_page(self):
        p = self.panel.page
        if p == "home":
            self.panel.home.refresh()
        elif p == "pin":
            self._render_pin()
        elif p == "builder":
            self._render_builder()
        elif p == "summary" and self.summary:
            self.panel.summary.show_summary(self.summary, self.units)
        elif p == "running":
            if self._last_tick is not None:
                self._render_running(self._last_tick)
            elif self.session is not None:
                self._render_running(self._idle_tick())

    def _idle_tick(self):
        """A tick-shaped snapshot for a session that isn't running yet (resume offered)."""
        from .session import Tick
        s = self.session
        lat, lon, h = s.position()
        return Tick(lat, lon, h, s.fraction(), s.remaining_m(), s.remaining_s(), s.base_speed(),
                    s.lap_number(), delivered=False, offline=True, paused=True,
                    finished=s.prog.finished, D=s.prog.D)

    def _browse(self):
        self._browsing = True
        self._force_welcome = False
        self._sync_chrome()

    def show_welcome(self):
        """The device cards (from the pill, when not connected)."""
        self._browsing = False
        self._force_welcome = True
        self._sync_chrome()

    def set_portable(self, on: bool):
        self._portable = on
        self._sync_chrome()

    # ------------------------------------------------------------------ status

    @property
    def units(self) -> str:
        return self.settings.get("units") or default_units()

    def _update_pill(self):
        name = (self.device or {}).get("name") or "iPhone"
        s = self.conn_state
        t = self._last_tick
        route_away = (self.session is not None and self.runner is not None
                      and (s != "connected" or (t is not None and t.offline)))
        if route_away:
            text, color = "Out of reach · route clock running", theme.AMBER
        elif s == "connected":
            text, color = f"{name} · {link_word((self.device or {}).get('link', ''))}", theme.GREEN
        elif s == "connecting":
            text, color = "Connecting…", theme.AMBER
        elif s == "reconnecting":
            text, color = "Reconnecting…", theme.AMBER
        elif s == "ready":
            text, color = f"iPhone ready · {link_word(self.ready_link)}", theme.LIVE
        else:
            text, color = "Disconnected", theme.GREY
        self.pill.set(text, color)

    def set_conn_state(self, state: str, link: str = ""):
        if self.conn_state == "connected" and state in ("ready", "disconnected") and self.device:
            return                        # a stale discovery tick after we connected
        if state == self.conn_state and (state != "ready" or link == self.ready_link):
            return                        # discovery ticks every 1.5 s; don't redraw for nothing
        self.conn_state = state
        if state == "ready":
            self.ready_link = link
        self._sync_chrome()

    def _toggle_popover(self):
        if self.pop.isVisible():
            self.pop.hide()
            return
        d = self.device
        if self.conn_state != "connected" or not d:
            self.show_welcome()
            return
        self.pop.name.setText(d.get("name") or "iPhone")
        self.pop.rows.setText(f"{d.get('model') or 'iPhone'}\niOS {d.get('ios', '')}\n"
                              f"Connected by {link_word(d.get('link', '')).lower()}")
        if d.get("wireless_on"):
            self.pop.wifi.setText("Wi-Fi control is on: unplug any time, and it keeps "
                                  "working on this Wi-Fi.")
            self.pop.wifi_btn.hide()
        else:
            self.pop.wifi.setText("Without Wi-Fi control, unplugging holds your iPhone at its "
                                  "last spot until you plug back in.")
            self.pop.wifi_btn.setVisible("USB" in d.get("link", ""))
        self.pop.show(); self.pop.raise_()
        self._layout()

    def _popover_action(self):
        self.pop.hide()
        name = (self.device or {}).get("name") or "your iPhone"
        box = QMessageBox(self)
        box.setWindowTitle("Disconnect")
        box.setText(f"Disconnect from {name}?")
        real = None
        if self.spoof is not None:
            box.setInformativeText("Your iPhone can stay at the spoofed location, or go back "
                                   "to its real location first.")
            leave = box.addButton("Leave it there", QMessageBox.ButtonRole.AcceptRole)
            real = box.addButton("Restore real location first", QMessageBox.ButtonRole.AcceptRole)
        else:
            leave = box.addButton("Disconnect", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is leave:
            self.disconnectAsked.emit(False)
        elif real is not None and box.clickedButton() is real:
            self.disconnectAsked.emit(True)

    def show_toast(self, text: str):
        self.toast.show_text(text)
        self._layout()

    # ------------------------------------------------------------------ device hooks

    def on_connected(self, device):
        """A session to the phone opened (first time, or coming back)."""
        udid = getattr(device, "udid", "") or getattr(device, "serial", "")
        self.device = {"udid": udid, "name": device.name, "model": getattr(device, "model", ""),
                       "ios": device.ios, "link": device.link,
                       "wireless_on": bool(getattr(device, "wireless_on", False))}
        self.conn_state = "connected"
        self._browsing = False
        self._force_welcome = False
        fresh = bool(getattr(device, "fresh_mount", False))
        if self.session is not None:
            if self.session_udid and udid and self.session_udid != udid:
                self.show_toast("A different iPhone is connected; the other one’s route is "
                                "kept for later.")
                self._stash_session()
            else:
                self._session_back(fresh)
        if self.session is None:
            rec = self._spoof_record(udid)
            if rec and fresh:
                self._set_spoof(None)
                self.banner.ask("Your iPhone restarted, which put it back on its real location.",
                                "Re-apply location",
                                lambda: self.teleport_to(rec["lat"], rec["lon"], rec.get("name", "")),
                                "Keep real location", self._forget_spoof)
            elif rec:
                # the phone still holds it: show it, and quietly re-assert it so
                # the app and the phone agree
                self._set_spoof((rec["lat"], rec["lon"]), rec.get("name", ""))
                self.map.set_view(rec["lat"], rec["lon"], max(15, self.map.zoom))
                self._teleport_label = rec.get("name", "")
                self._quiet_teleport = True
                self.bridge.set_location(rec["lat"], rec["lon"])
            else:
                self._center_on_real = True
                if self.real is not None:
                    self.set_real(*self.real)
                self.bridge.locate()
            self._maybe_offer_resume(udid)
        self._sync_chrome()

    def on_device_lost(self):
        """Gone for good (reconnect gave up). A running route keeps its clock."""
        self.conn_state = "disconnected"
        self._walk_release()
        self._sync_chrome()

    def on_reconnecting(self):
        self.conn_state = "reconnecting"
        self._walk_release()
        self._sync_chrome()

    def on_disconnected(self):
        """The user disconnected on purpose."""
        if self.session is not None:
            self._stash_session(pause=True)
        self.conn_state = "disconnected"
        self.device = None
        self._walk_release()
        self._set_spoof(None)
        self._sync_chrome()

    # ------------------------------------------------------------------ spoof marker

    def _spoof_record(self, udid: str | None = None) -> dict | None:
        udid = udid or (self.device or {}).get("udid", "")
        return (self.settings.get("spoofs") or {}).get(udid)

    def _persist_spoof(self):
        udid = (self.device or {}).get("udid") or self.session_udid
        if not udid:
            return
        spoofs = self.settings.setdefault("spoofs", {})
        if self.spoof:
            spoofs[udid] = {"lat": self.spoof[0], "lon": self.spoof[1], "name": self.spoof_name}
        else:
            spoofs.pop(udid, None)
        store.save(self.settings)

    def _forget_spoof(self):
        self._set_spoof(None)
        self._persist_spoof()
        self._sync_chrome()

    def _set_spoof(self, pos, name: str = "", animate=False, heading=None):
        self.spoof = pos
        if pos is not None:
            if name:
                self.spoof_name = name
            if self._spoof_ov is None:
                self._spoof_item = SpoofMarker()
                self._spoof_item.set_pulsing(self._pulse)
                self._spoof_ov = self.map.add_item(self._spoof_item, pos[0], pos[1])
            elif animate:
                ms = animate if isinstance(animate, int) and not isinstance(animate, bool) else 220
                self._glide_to(pos, ms)
            else:
                self.map.move_marker(self._spoof_ov, pos[0], pos[1])
            self._spoof_item.set_heading(heading)
            self.readout.emit(f"{pos[0]:.5f}, {pos[1]:.5f}")
        else:
            self.spoof_name = ""
            if self._glide is not None:
                self._glide.stop()
            if self._spoof_ov is not None:
                self.map.remove_overlay(self._spoof_ov)
            self._spoof_ov = self._spoof_item = None
            self.readout.emit("")
        self.panel.set_spoofing(pos is not None and self.conn_state == "connected")

    def _glide_to(self, pos, ms: int):
        """Move the spoofed marker smoothly (purely visual; the phone jumps)."""
        g = self._glide
        if g is None:
            g = self._glide = QVariantAnimation(self)
            g.setEasingCurve(QEasingCurve.Type.OutCubic)
            g.setStartValue(0.0)
            g.setEndValue(1.0)
            g.valueChanged.connect(self._glide_step)
        g.stop()
        ov = self._spoof_ov
        self._glide_from = (ov.lat, ov.lon)
        self._glide_dest = pos
        g.setDuration(max(1, ms))
        g.start()

    def _glide_step(self, f):
        if self._spoof_ov is None:
            return
        (a0, o0), (a1, o1) = self._glide_from, self._glide_dest
        self.map.move_marker(self._spoof_ov, a0 + (a1 - a0) * f, o0 + (o1 - o0) * f)

    def set_real(self, lat: float, lon: float):
        """The phone's real location as best we know it: this Mac's, approximate."""
        self.real = (lat, lon)
        pm, off = make_real_marker()
        if self._real_ov is None:
            self._real_ov = self.map.add_marker(lat, lon, pm, z=9, offset=off)
        else:
            self.map.move_marker(self._real_ov, lat, lon)
        if (self._center_on_real or not self.settings.get("view")) and self.spoof is None:
            self._center_on_real = False
            self.map.set_view(lat, lon, max(14, self.map.zoom))

    def _on_located(self, lat: float, lon: float):
        """A fix landed on the phone (teleport)."""
        quiet, self._quiet_teleport = self._quiet_teleport, False
        label_ = self._teleport_label or ""
        self._teleport_label = None
        self._set_spoof((lat, lon), label_, animate=not quiet)
        self._persist_spoof()
        if quiet:
            return
        self.show_toast(f"Teleported to {label_ or f'{lat:.5f}, {lon:.5f}'}")
        self._add_recent(lat, lon, label_)
        if not label_:
            threading.Thread(target=self._name_worker, args=("recent", lat, lon), daemon=True).start()
        self._sync_chrome()

    def _on_restored(self):
        self._set_spoof(None)
        self._persist_spoof()
        self.show_toast("Your iPhone is using its real location again.")
        self._sync_chrome()

    # ------------------------------------------------------------------ pins

    def _on_map_click(self, lat: float, lon: float):
        self._hide_suggestions()
        self.search.clearFocus()
        self.pop.hide()
        if self.draft is not None and self.session is None and self.pin is None:
            self.add_stop(lat, lon)
            return
        self.drop_pin(lat, lon)

    def drop_pin(self, lat: float, lon: float, name: str = "", center: bool = False):
        """One pin at a time: a new one replaces the old."""
        self.pin = {"lat": lat, "lon": lon, "name": name, "address": ""}
        if self._pin_ov is None:
            self._pin_ov = self.map.add_marker(lat, lon, self._pin_px, anchor="s", z=15,
                                               draggable=True)
        else:
            self.map.move_marker(self._pin_ov, lat, lon)
        if center:
            self.map.set_view(lat, lon, max(15, self.map.zoom))
        self._name_timer.start(0)
        self._sync_chrome()

    def close_pin(self):
        if self._pin_ov is not None:
            self.map.remove_overlay(self._pin_ov)
        self._pin_ov = None
        self.pin = None
        self._sync_chrome()

    def _on_marker_moved(self, ov, lat, lon):
        if ov is self._pin_ov and self.pin is not None:
            self.pin.update(lat=lat, lon=lon, name="", address="")
            self._render_pin()

    def _on_marker_dropped(self, ov, lat, lon):
        if ov is self._pin_ov and self.pin is not None:
            self._name_timer.start()

    def _lookup_pin_name(self):
        if self.pin is not None:
            p = self.pin
            threading.Thread(target=self._name_worker, args=("pin", p["lat"], p["lon"]),
                             daemon=True).start()
            self._render_pin()

    def _name_worker(self, what: str, lat: float, lon: float):
        self._named.emit(what, lat, lon, geo.reverse(lat, lon))

    def _on_named(self, what, lat, lon, name):
        if what == "pin":
            p = self.pin
            if p and abs(p["lat"] - lat) < 1e-9 and abs(p["lon"] - lon) < 1e-9:
                p["address"] = name or "No address here"
                self._render_pin()
        elif what == "recent" and name:
            for r in self.settings.get("recent", []):
                if abs(r["lat"] - lat) < 1e-5 and abs(r["lon"] - lon) < 1e-5 and not r.get("name"):
                    r["name"] = name
            if self.spoof and abs(self.spoof[0] - lat) < 1e-5 and not self.spoof_name:
                self.spoof_name = name
                self._persist_spoof()
            store.save(self.settings)
            self.sessionChanged.emit()
            if self.panel.page == "home":
                self.panel.home.refresh()
        elif what == "stop" and self.draft is not None and name:
            for s in self.draft.stops:
                if abs(s["lat"] - lat) < 1e-9 and abs(s["lon"] - lon) < 1e-9:
                    s["name"] = name
            self._render_builder()

    def _render_pin(self):
        p = self.pin
        if p is None or self.panel.page != "pin":
            return
        addr = p["address"]
        title = p["name"] or (addr.split(",")[0] if addr and addr != "No address here"
                              else "Dropped pin")
        sub = addr if addr and addr != title else ("" if (p["name"] or addr) else "Looking up the address…")
        ref = self.spoof or self.real
        dist = ""
        if ref is not None:
            whose = "your iPhone" if self.spoof else "you (approximately)"
            dist = f"{fmt_distance(meters(ref, (p['lat'], p['lon'])), self.units)} from {whose}"
        self.panel.pin.show_info(title, sub, p["lat"], p["lon"], dist, self._is_starred(p))
        self._layout()

    def _pin_label(self) -> str:
        p = self.pin or {}
        addr = p.get("address") if p.get("address") != "No address here" else ""
        return p.get("name") or addr or ""

    def copy_pin_coords(self):
        if self.pin:
            QGuiApplication.clipboard().setText(f"{self.pin['lat']:.6f}, {self.pin['lon']:.6f}")
            self.show_toast("Coordinates copied")

    def _is_starred(self, p) -> bool:
        return any(abs(s["lat"] - p["lat"]) < 1e-5 and abs(s["lon"] - p["lon"]) < 1e-5
                   for s in self.settings.get("saved", []))

    def toggle_star_pin(self):
        p = self.pin
        if not p:
            return
        saved = self.settings.setdefault("saved", [])
        if self._is_starred(p):
            self.settings["saved"] = [s for s in saved if not (
                abs(s["lat"] - p["lat"]) < 1e-5 and abs(s["lon"] - p["lon"]) < 1e-5)]
            self.show_toast("Removed from starred places")
        else:
            saved.insert(0, {"name": self._pin_label() or f"{p['lat']:.4f}, {p['lon']:.4f}",
                             "lat": p["lat"], "lon": p["lon"]})
            self.show_toast("Starred")
        store.save(self.settings)
        self.sessionChanged.emit()
        self._render_pin()

    def teleport_pin(self):
        if self.pin:
            p = self.pin
            if self.teleport_to(p["lat"], p["lon"], self._pin_label()):
                self.close_pin()

    def route_pin(self):
        if not self.pin:
            return
        if self.session is not None:
            if not self._confirm_stop_route("plan a new one"):
                return
            self._end_session()
        p = self.pin
        dest = {"lat": p["lat"], "lon": p["lon"], "name": self._pin_label()}
        s = self.settings
        self.draft = Draft([dest], style=s.get("path_style", "roads"),
                           profile=s.get("profile", "walking"),
                           speed_kmh=s.get("speed_kmh", 5.0), name=dest["name"])
        self.close_pin()
        self._refresh_plan()
        self._sync_chrome()

    # ------------------------------------------------------------------ teleport

    def teleport_to(self, lat: float, lon: float, name: str = "") -> bool:
        if self.conn_state != "connected":
            self.show_toast("Connect your iPhone first.")
            return False
        if self.session is not None:
            if not self._confirm_stop_route("teleport"):
                return False
            self._end_session()
        self._walk_release()
        self.bridge.suspend()
        self._teleport_label = name
        self.bridge.set_location(lat, lon)
        return True

    def _confirm_stop_route(self, what: str) -> bool:
        r = QMessageBox.question(self, "Stop the route?",
                                 f"Stop the current route and {what}?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                 QMessageBox.StandardButton.Yes)
        return r == QMessageBox.StandardButton.Yes

    def stop_spoofing(self):
        """Always available, and it must always work."""
        if self.session is not None:
            self._end_session()
        self._walk_release()
        self.bridge.suspend()
        if self.conn_state != "connected":
            self.show_toast("Connect your iPhone to restore it. Restarting the iPhone also "
                            "always restores its real location.")
            return
        self.bridge.restore()

    def recenter(self):
        self._follow_paused = False
        pos = self.spoof or self.real
        if pos is None:
            self.show_toast("No location yet. Search for a place to get started.")
            return
        self.map.pan_to(pos[0], pos[1], animate_ms=250)

    def set_map_style(self, key: str):
        self.map.set_style(key)
        for k, a in self._style_actions.items():
            a.setChecked(k == key)
        self.settings["map_style"] = key
        store.save(self.settings)

    def _show_style_menu(self):
        self.style_menu.exec(self.style_btn.mapToGlobal(
            QPoint(-self.style_menu.sizeHint().width() - 6, 0)))

    # ------------------------------------------------------------------ places

    def _add_recent(self, lat, lon, name=""):
        rec = [r for r in self.settings.get("recent", [])
               if abs(r["lat"] - lat) > 1e-4 or abs(r["lon"] - lon) > 1e-4]
        entry = {"lat": lat, "lon": lon}
        if name:
            entry["name"] = name
        rec.insert(0, entry)
        self.settings["recent"] = rec[:RECENT_MAX]
        store.save(self.settings)
        self.sessionChanged.emit()

    def places(self):
        return self.settings.get("saved", []), self.settings.get("recent", [])

    def use_place(self, p: dict):
        if self.conn_state == "connected":
            self.map.pan_to(p["lat"], p["lon"], animate_ms=220)
            self.teleport_to(p["lat"], p["lon"], p.get("name", ""))
        else:
            self.drop_pin(p["lat"], p["lon"], name=p.get("name", ""), center=True)

    def star_place(self, p: dict):
        saved = self.settings.setdefault("saved", [])
        if not any(abs(s["lat"] - p["lat"]) < 1e-5 and abs(s["lon"] - p["lon"]) < 1e-5 for s in saved):
            saved.insert(0, {"name": p.get("name") or f"{p['lat']:.4f}, {p['lon']:.4f}",
                             "lat": p["lat"], "lon": p["lon"]})
            store.save(self.settings)
            self.show_toast("Starred")
            self.sessionChanged.emit()
        self._sync_chrome()

    def rename_place(self, i: int):
        saved = self.settings.get("saved", [])
        if 0 <= i < len(saved):
            name, ok = QInputDialog.getText(self, "Rename place", "Name", text=saved[i]["name"])
            if ok and name.strip():
                saved[i]["name"] = name.strip()
                store.save(self.settings)
                self.sessionChanged.emit()
                self._sync_chrome()

    def delete_place(self, i: int):
        saved = self.settings.get("saved", [])
        if 0 <= i < len(saved):
            saved.pop(i)
            store.save(self.settings)
            self.sessionChanged.emit()
            self._sync_chrome()

    # ------------------------------------------------------------------ route: building

    def start_point(self):
        if self.draft is not None and self.draft.start:
            s = self.draft.start
            return (s["lat"], s["lon"]), s.get("name") or "Start of the track"
        if self.spoof is not None:
            return self.spoof, "Your iPhone’s location"
        if self.real is not None:
            return self.real, "Your location (approximate)"
        return None, "Set a location first"

    def add_stop(self, lat: float, lon: float):
        d = self.draft
        stop = {"lat": lat, "lon": lon, "name": ""}
        if d.style == "draw" or not d.stops:
            d.stops.append(stop)
        else:
            d.stops.insert(len(d.stops) - 1, stop)       # a stop on the way, before the end
        if d.style != "draw":
            threading.Thread(target=self._name_worker, args=("stop", lat, lon), daemon=True).start()
        self._refresh_plan()
        self._sync_chrome()

    def add_stop_hint(self):
        self.show_toast("Click the map where you want to stop")

    def remove_stop(self, i: int):
        d = self.draft
        if d and 0 <= i < len(d.stops):
            d.stops.pop(i)
            if not d.stops:
                self.close_builder()
                return
            self._refresh_plan()
            self._sync_chrome()

    def undo_trace(self):
        if self.draft and self.draft.stops:
            self.remove_stop(len(self.draft.stops) - 1)

    def reorder_stops(self, order: list):
        d = self.draft
        if d and sorted(order) == list(range(len(d.stops))):
            d.stops = [d.stops[i] for i in order]
            self._refresh_plan()
            QTimer.singleShot(0, self._sync_chrome)   # rebuild rows after the drop settles

    def set_path_style(self, style: str):
        if self.draft:
            self.draft.style = style
            self.settings["path_style"] = style
            store.save(self.settings)
            self._refresh_plan()
            self._sync_chrome()

    def set_profile(self, profile: str):
        if self.draft:
            self.draft.profile = profile
            self.settings["profile"] = profile
            store.save(self.settings)
            self._refresh_plan()
            self._sync_chrome()

    def use_straight_line(self):
        self.set_path_style("straight")

    def set_speed_kmh(self, kmh: float):
        self.settings["speed_kmh"] = kmh
        store.save(self.settings)
        if self.draft:
            self.draft.speed_kmh = kmh
        if self.session is not None and self.runner is not None:
            with self.runner.lock:
                self.session.set_speed(kmh * KMH)
                self._save_session()
            self.runner.poke()
            self._render_running(self.runner.step())
        self._plan_estimate()
        if self.panel.page == "builder":
            self._render_builder()

    def set_units(self, u: str):
        self.settings["units"] = u
        store.save(self.settings)
        self._refresh_page()

    def set_options(self, **kw):
        if not self.draft:
            return
        o = self.draft.opts
        if "forever" in kw:
            o["laps"] = None if kw.pop("forever") else max(1, int(o.get("laps") or 1))
        if "laps" in kw and o.get("laps") is None:
            kw.pop("laps")
        o.update(kw)
        self._plan_estimate()
        self._render_builder()

    def close_builder(self):
        self.draft = None
        self._plan = {}
        self._plan_key = None
        self._plan_timer.stop()
        self._clear_route_overlays()
        self._sync_chrome()

    def _draft_points(self):
        d = self.draft
        if d is None or not d.stops:
            return []
        pts = [(s["lat"], s["lon"]) for s in d.stops]
        if d.start is not None:
            return [(d.start["lat"], d.start["lon"])] + pts
        start, _ = self.start_point()
        if start is None or not (1.0 <= meters(start, pts[0]) <= ORIGIN_MAX_M):
            return pts if len(pts) >= 2 else []
        return [start] + pts

    def _refresh_plan(self):
        """Work out what Start would walk; road routing is asked for in the
        background and cached, so Start plays exactly the line on screen."""
        self._plan_timer.stop()
        d = self.draft
        pts = self._draft_points()
        if d is None or len(pts) < 2:
            self._plan = {"ready": False, "busy": False,
                          "message": self._why_no_route() if d else ""}
            self._plan_key = None
            self._draw_plan()
            return
        key = (d.style, d.profile, tuple(pts))
        self._plan_key = key
        if d.style == "roads":
            cached = self._snap_cache.get(key)
            if cached is not None:
                self._set_plan(key, cached)
                return
            self._plan = {"ready": False, "busy": True, "points": pts, "pending": True,
                          "message": f"Finding a {d.profile} route…"}
            self._draw_plan()
            self._plan_timer.start()
        else:
            self._set_plan(key, route.Snapped(list(pts), True))

    def _why_no_route(self) -> str:
        start, _ = self.start_point()
        if start is None:
            return "Teleport somewhere first, so the route has a start."
        d = self.draft
        if d and d.stops and d.start is None:
            gap = meters(start, (d.stops[0]["lat"], d.stops[0]["lon"]))
            if gap > ORIGIN_MAX_M:
                return (f"Your iPhone is {gap / 1000:,.0f} km away. Teleport closer first, "
                        "or add a second stop to start from.")
            if gap < 1.0:
                return "That’s where your iPhone already is. Pick somewhere a little further."
        return ""

    def _fetch_plan(self):
        key = self._plan_key
        if key is not None:
            threading.Thread(target=self._plan_worker, args=(key,), daemon=True).start()

    def _plan_worker(self, key):
        _style, profile, pts = key
        self._planReady.emit(key, route.snap_to_roads(list(pts), profile))

    def _on_plan_ready(self, key, snapped):
        if snapped.ok:
            if len(self._snap_cache) >= SNAP_CACHE_MAX:
                self._snap_cache.pop(next(iter(self._snap_cache)))
            self._snap_cache[key] = snapped
        if key == self._plan_key:
            self._set_plan(key, snapped)

    def _set_plan(self, key, snapped):
        style, _profile, pts = key
        if not snapped.ok:
            msg = {"offline": "You’re offline, so roads can’t be followed.",
                   "noroute": "No road goes there."}.get(snapped.reason,
                                                         "Couldn’t reach the road router.")
            self._plan = {"ready": False, "busy": False, "points": list(pts), "pending": True,
                          "message": msg, "offer_straight": True}
            self._draw_plan()
            self._render_builder()
            return
        plan = Plan(list(snapped.points))
        inner = list(pts[1:-1]) if style != "draw" else []
        if style == "roads":
            stops_d = plan.locate(inner) if inner else []
        else:
            stops_d = [plan.cum[min(i, len(plan.cum) - 1)] for i in range(1, len(pts) - 1)] if inner else []
        # an explicit start (a track) is where it begins; from "here" the first
        # point is the phone, which is not a stop to dwell at
        self._plan = {"ready": True, "busy": False, "points": plan.points, "stops_d": stops_d,
                      "distance": plan.total, "message": ""}
        self._plan_estimate()
        self._draw_plan()
        self._render_builder()

    def _plan_estimate(self):
        d, p = self.draft, self._plan
        if not d or not p.get("distance"):
            return
        laps = 1 if d.opts["end"] == "stop" else d.opts.get("laps")
        per = p["distance"]
        if d.opts["end"] == "loop" and p.get("points"):
            per += meters(p["points"][-1], p["points"][0])
        dwell = len(p.get("stops_d", [])) * float(d.opts.get("dwell_s") or 0)
        per_s = per / max(0.1, d.speed_kmh * KMH) + dwell
        p["duration"] = None if laps is None else per_s * laps
        p["distance_total"] = None if laps is None else per * laps

    def _draw_plan(self):
        self._clear_route_overlays()
        d = self.draft
        p = self._plan
        pts = p.get("points") or []
        if len(pts) >= 2:
            if p.get("pending"):
                self._line_ov = self.map.add_path(pts, color=theme.MUTED, width=4, dashed=True, z=5)
            else:
                self._line_ov = self.map.add_path(pts, color=theme.ACCENT, width=6, z=5, arrows=True)
        if d is None:
            return
        start, _ = self.start_point()
        if start is not None and (d.start is not None or self.spoof is None):
            self._route_ovs.append(self.map.add_marker(start[0], start[1], make_start(),
                                                       anchor="center", z=8))
        if d.style != "draw":
            n = len(d.stops)
            for i, s in enumerate(d.stops):
                px = make_end() if i == n - 1 else make_stop(i + 1)
                self._route_ovs.append(self.map.add_marker(s["lat"], s["lon"], px,
                                                           anchor="center", z=9))
        elif d.stops:
            e = d.stops[-1]
            self._route_ovs.append(self.map.add_marker(e["lat"], e["lon"], make_end(),
                                                       anchor="center", z=9))

    def _clear_route_overlays(self):
        for ov in self._route_ovs:
            self.map.remove_overlay(ov)
        self._route_ovs = []
        for attr in ("_line_ov", "_travel_ov", "_skip_ov"):
            ov = getattr(self, attr)
            if ov is not None:
                self.map.remove_overlay(ov)
                setattr(self, attr, None)

    def _link_note(self) -> str:
        d = self.device or {}
        if d.get("wireless_on"):
            return ("You can unplug once it starts: the route keeps going as long as your "
                    "iPhone stays on the same Wi-Fi as this Mac.")
        return ("To keep moving unplugged, set up Wi-Fi control (click the status pill) and keep "
                "your iPhone on the same Wi-Fi as this Mac. Without it, unplugging holds the "
                "phone at its spot and it catches up when you plug back in.")

    def _render_builder(self):
        if self.draft is None or self.panel.page != "builder":
            return
        _, start_name = self.start_point()
        st = dict(self._plan)
        st["distance"] = st.get("distance_total", st.get("distance"))
        self.panel.builder.refresh(self.draft, start_name, st, self.units, self._link_note())
        self._layout()

    # ------------------------------------------------------------------ route: saved

    def saved_routes(self) -> list:
        return self.settings.get("routes", [])

    def save_route(self):
        d = self.draft
        if not d:
            return
        default = f"To {d.name}" if d.name else "My route"
        name, ok = QInputDialog.getText(self, "Save route", "Name", text=default)
        if not ok or not name.strip():
            return
        entry = {"name": name.strip(), "draft": d.to_dict(),
                 "summary": self.panel.builder.summary.text()}
        if self._plan.get("ready") and self._plan_key is not None:
            style, profile, pts = self._plan_key
            entry["plan"] = {"key": [style, profile, [list(x) for x in pts]],
                             "points": [list(x) for x in self._plan["points"]]}
        self.settings.setdefault("routes", []).insert(0, entry)
        store.save(self.settings)
        self.show_toast(f"Saved “{name.strip()}”")

    def load_route(self, i: int):
        routes = self.saved_routes()
        if not (0 <= i < len(routes)):
            return
        if self.session is not None:
            self.show_toast("Stop the running route first.")
            return
        r = routes[i]
        self.draft = Draft.from_dict(r["draft"])
        plan = r.get("plan")
        if plan:
            style, profile, pts = plan["key"]
            key = (style, profile, tuple(tuple(p) for p in pts))
            self._snap_cache[key] = route.Snapped([tuple(p) for p in plan["points"]], True)
        self.close_pin()
        self._refresh_plan()
        self._sync_chrome()
        pts = [(s["lat"], s["lon"]) for s in self.draft.stops]
        if self.draft.start:
            pts.insert(0, (self.draft.start["lat"], self.draft.start["lon"]))
        self.map.fit(pts)

    def duplicate_route(self, i: int):
        routes = self.saved_routes()
        if 0 <= i < len(routes):
            dup = copy.deepcopy(routes[i])
            dup["name"] = routes[i]["name"] + " copy"
            routes.insert(i + 1, dup)
            store.save(self.settings)
            self._sync_chrome()

    def rename_route(self, i: int):
        routes = self.saved_routes()
        if 0 <= i < len(routes):
            name, ok = QInputDialog.getText(self, "Rename route", "Name", text=routes[i]["name"])
            if ok and name.strip():
                routes[i]["name"] = name.strip()
                store.save(self.settings)
                self._sync_chrome()

    def delete_route(self, i: int):
        routes = self.saved_routes()
        if 0 <= i < len(routes):
            routes.pop(i)
            store.save(self.settings)
            self._sync_chrome()

    def import_gpx(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import a GPX track", "",
                                              "GPX track (*.gpx);;All files (*)")
        if not path:
            return
        try:
            pts = route.parse_gpx(path)
        except Exception as e:
            self.show_toast(f"Couldn’t read that GPX: {e}")
            return
        self.load_track(pts, os.path.splitext(os.path.basename(path))[0])

    def load_track(self, pts, name: str = "Track"):
        if self.session is not None:
            self.show_toast("Stop the running route first.")
            return
        s = self.settings
        self.draft = Draft([{"lat": a, "lon": b, "name": ""} for a, b in pts[1:]],
                           style="draw", speed_kmh=s.get("speed_kmh", 5.0), name=name,
                           start={"lat": pts[0][0], "lon": pts[0][1], "name": "Start of the track"})
        self.close_pin()
        self._refresh_plan()
        self._sync_chrome()
        self.map.fit(pts)
        self.show_toast(f"Loaded {len(pts)} points. Press Start to walk the track.")

    def export_route(self):
        pts = self._plan.get("points") if self._plan.get("ready") else None
        if not pts:
            self.show_toast("Nothing to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export route as GPX",
                                              f"{(self.draft.name or 'route')}.gpx",
                                              "GPX track (*.gpx)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(route.build_gpx(pts, self.draft.name or "Spoofr route"))
            self.show_toast(f"Exported {len(pts)} points")
        except Exception as e:
            self.show_toast(f"Export failed: {e}")

    # ------------------------------------------------------------------ route: running

    def start_route(self):
        if self.conn_state != "connected":
            self.show_toast("Connect your iPhone first.")
            return
        p = self._plan
        if not p.get("ready") or self.draft is None:
            return
        d = self.draft
        o = d.opts
        opts = Options(end=o["end"], laps=(None if o.get("laps") is None else int(o.get("laps") or 1)),
                       dwell_s=float(o.get("dwell_s") or 0), variation=bool(o.get("variation")),
                       jitter=bool(o.get("jitter")))
        self._walk_release()
        self.session = RouteSession(Plan(p["points"], p.get("stops_d", [])), d.speed_kmh * KMH,
                                    opts, name=d.name)
        self.session.draft = d.to_dict()
        self.session_udid = (self.device or {}).get("udid", "")
        self.summary = None
        self.follow = True
        self._follow_paused = False
        self._last_tick = None
        self.draft = None
        self._save_session()
        self._start_runner()
        self._sync_chrome()
        self.sessionChanged.emit()

    def _start_runner(self):
        self.runner = Runner(self.session, push=self._push, on_tick=self._tick.emit,
                             save=self._save_session)
        self.runner.start()
        self._redraw_session_overlays()
        self.hold_awake(True)

    def _push(self, lat: float, lon: float) -> bool:
        s = self.session
        if s is None or s.hold:
            return False
        return self.bridge.push(lat, lon, quiet=True)

    def _save_session(self, s: RouteSession | None = None):
        s = s or self.session
        if s is None or not self.session_udid:
            return
        d = s.to_dict()
        d["udid"] = self.session_udid
        d["draft"] = getattr(s, "draft", None)
        store.save_session(self.session_udid, d)

    def _redraw_session_overlays(self):
        self._clear_route_overlays()
        s = self.session
        if s is None:
            return
        pts = s.plan.points
        self._line_ov = self.map.add_path(pts, color=theme.ACCENT, width=6, z=5, arrows=True)
        self._route_ovs.append(self.map.add_marker(pts[0][0], pts[0][1], make_start(),
                                                   anchor="center", z=8))
        self._route_ovs.append(self.map.add_marker(pts[-1][0], pts[-1][1], make_end(),
                                                   anchor="center", z=9))
        for i, dd in enumerate(s.plan.stops_d):
            la, lo, _ = s.plan.at(dd)
            self._route_ovs.append(self.map.add_marker(la, lo, make_stop(i + 1), anchor="center", z=9))

    def session_laps(self):
        return self.session.laps if self.session is not None else 1

    def _on_tick(self, t):
        s = self.session
        if s is None:
            return
        self._last_tick = t
        heading = None if (t.paused or t.finished) else t.heading
        period = self.runner._period() if self.runner else 1.0
        self._set_spoof((t.lat, t.lon), s.name, animate=int(min(900, period * 950)), heading=heading)
        # the part already travelled (this lap), and any stretch a catch-up skipped
        L = s.lap.total
        if L > 0:
            lap_i, d_in = s._lap_of(t.D)
            done = s.lap.between(0, d_in) if not s._reversed(lap_i) else s.lap.between(L - d_in, L)
            if len(done) >= 2:
                if self._travel_ov is None:
                    self._travel_ov = self.map.add_path(done, color=theme.TRAVELLED, width=6, z=6)
                else:
                    self.map.update_path(self._travel_ov, done)
            elif self._travel_ov is not None:
                self.map.update_path(self._travel_ov, [])
            if t.skipped:
                a, b = t.skipped
                la, a_in = s._lap_of(a)
                lb, b_in = s._lap_of(b)
                gap = s.lap.between(a_in if la == lb else 0.0, b_in)
                if self._skip_ov is not None:
                    self.map.remove_overlay(self._skip_ov)
                    self._skip_ov = None
                if len(gap) >= 2:
                    self._skip_ov = self.map.add_path(gap, color=theme.ACCENT, width=4,
                                                      dashed=True, z=7)
        if t.came_back:
            self.show_toast(f"Welcome back. Route resumed, {int(t.fraction * 100)}% complete.")
        if t.offline and not s.hold and self.settings.get("catch_up") != "catchup":
            s.hold = True        # decide catch-up vs continue before anything lands
        if self.follow and not self._follow_paused and not t.paused:
            if self.map.is_near_edge(t.lat, t.lon, margin=0.3):
                self.map.pan_to(t.lat, t.lon, animate_ms=400)
        self._render_running(t)
        self._update_pill()
        pct = int(t.fraction * 100)
        self.progress.emit("❚❚" if t.paused else ("…" if t.offline else f"{pct}%"))
        if t.done:
            self._finish_session()

    def _render_running(self, t):
        if self.panel.page != "running" or self.session is None:
            return
        msg = ""
        if self.runner is None:
            msg = "Paused when Spoofr last closed. Resume to carry on."
        elif t.offline or self.conn_state != "connected":
            msg = ("Your iPhone is out of reach. The route clock keeps running, and the phone "
                   "catches up when it’s back.")
        self.panel.running.update_tick(t, self.session.name, self.units, msg)
        self.panel.running.speed.set_state(t.speed * 3.6, self.units)
        self.panel.running.follow.set_on(self.follow and not self._follow_paused)
        self.panel.running.awake.setVisible(self._caffeinate is not None)

    def set_follow(self, on: bool):
        self.follow = on
        self._follow_paused = False
        if on and self.spoof:
            self.map.pan_to(self.spoof[0], self.spoof[1], animate_ms=250)

    def _on_user_panned(self):
        if self.session is not None and self.follow:
            self._follow_paused = True       # recenter snaps back

    def toggle_pause(self):
        s, r = self.session, self.runner
        if s is None:
            return
        if r is None:
            self.resume_session()
            return
        with r.lock:
            s.resume() if s.paused else s.pause()
            self._save_session()
        r.poke()
        self._render_running(r.step())
        self.sessionChanged.emit()

    def stop_route(self):
        if self.session is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Stop the route")
        box.setText("Stop the route?")
        box.setInformativeText("Your iPhone can stay where it is now, or go back to its real "
                               "location.")
        stay = box.addButton("Stay here", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("Return to real location", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(stay)
        box.exec()
        if box.clickedButton() is stay:
            self._end_session()
            self._persist_spoof()
            self.show_toast("Route stopped. Your iPhone stays here.")
        elif box.clickedButton() is back:
            self.stop_spoofing()

    def _end_session(self):
        if self.runner is not None:
            self.runner.stop()
        self.runner = None
        if self.session_udid:
            store.drop_session(self.session_udid)
        self.session = None
        self._last_tick = None
        self.hold_awake(False)
        self.banner.hide()
        self._resume_pending = False
        self._clear_route_overlays()
        if self._spoof_item is not None:
            self._spoof_item.set_heading(None)
        self.progress.emit("")
        self._sync_chrome()
        self.sessionChanged.emit()

    def _finish_session(self):
        s = self.session
        self.summary = dict(s.summary(), name=s.name)
        self._persist_spoof()
        self._end_session()
        self.show_toast("Route complete")

    def dismiss_summary(self):
        self.summary = None
        self._sync_chrome()

    def _stash_session(self, pause: bool = False):
        """Keep the session on disk but stop driving it (another phone, a disconnect)."""
        if self.session is None:
            return
        lock = self.runner.lock if self.runner is not None else threading.RLock()
        with lock:
            if pause and not self.session.paused:
                self.session.pause()
            self._save_session()
        if self.runner is not None:
            self.runner.stop()
        self.runner = None
        self.session = None
        self._last_tick = None
        self.hold_awake(False)
        self._clear_route_overlays()
        self.progress.emit("")
        self.sessionChanged.emit()

    def pause_for_quit(self):
        """Quitting stops the route: pause it, so Resume picks up from here."""
        if self.session is not None:
            lock = self.runner.lock if self.runner is not None else threading.RLock()
            with lock:
                if not self.session.paused:
                    self.session.pause()
                self._save_session()
            if self.runner is not None:
                self.runner.stop(join=1.0)

    # -- coming back ---------------------------------------------------------

    def restore_session(self, udid: str, d: dict):
        """A route was left behind (quit, crash, restart). Offer to resume it."""
        try:
            s = RouteSession.from_dict(d)
        except Exception:
            store.drop_session(udid)
            return
        s.draft = d.get("draft")
        if s.prog.finished:
            store.drop_session(udid)
            return
        if not s.paused:
            s.mark_offline()          # the app wasn't running: a gap like any other
        self.session, self.session_udid = s, udid
        self._redraw_session_overlays()
        lat, lon, _ = s.position()
        self._set_spoof((lat, lon), s.name)
        self.map.set_view(lat, lon, max(14, self.map.zoom))
        self._last_tick = None
        pct = int(s.fraction() * 100)
        self.banner.ask(f"Your route to {s.name or 'your destination'} was {pct}% done.",
                        "Resume", self.resume_session, "Discard", self._end_session)
        self._sync_chrome()

    def resume_session(self):
        s = self.session
        if s is None:
            return
        self.banner.hide()
        if self.conn_state != "connected":
            self._resume_pending = True
            self.show_toast("Connect your iPhone and the route picks up from there.")
            self._sync_chrome()
            return
        self._resume_pending = False
        if s.paused:
            s.resume()
        elif s.offline_since is not None:
            self._apply_catch_up()
        if self.runner is None:
            self._start_runner()
        self._sync_chrome()
        self.sessionChanged.emit()

    def _session_back(self, fresh_mount: bool):
        """The phone is reachable again while a session exists."""
        s = self.session
        if fresh_mount:
            s.hold = True
            if self.runner is None and not self._resume_pending:
                self._start_runner()
            self.banner.ask("Your iPhone restarted and lost its spoofed location.",
                            "Resume route", self._release_after_restart,
                            "Re-apply location", self._reapply_and_pause)
            return
        if self._resume_pending:
            self.resume_session()
            return
        if self.runner is None:
            return                     # the Resume banner is waiting on the user
        if s.hold or s.offline_since is not None:
            self._apply_catch_up()
        self.runner.poke()

    def _release_after_restart(self):
        if self.session is None:
            return
        self._resume_pending = False
        if self.session.paused:
            self.session.resume()
        self._apply_catch_up()
        if self.runner is None:
            self._start_runner()
        self.runner.poke()

    def _reapply_and_pause(self):
        s, r = self.session, self.runner
        if s is None:
            return
        lock = r.lock if r is not None else threading.RLock()
        with lock:
            s.pause()
            s.hold = False
        if r is None:
            self._start_runner()
        else:
            r.poke()
        self.show_toast("Location re-applied. The route is paused; Resume when you’re ready.")

    def _apply_catch_up(self):
        """Catch up (default) or continue from where it stopped. Asked the first time."""
        s = self.session
        pref = self.settings.get("catch_up")
        if pref not in ("catchup", "continue"):
            box = QMessageBox(self)
            box.setWindowTitle("Your iPhone is back")
            box.setText("The route kept going while your iPhone was away.")
            box.setInformativeText("Catch up puts it where it would be now. Continue picks up "
                                   "from the last spot it actually reached. You can change "
                                   "this later in Settings.")
            cu = box.addButton("Catch up", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Continue from where it stopped", QMessageBox.ButtonRole.AcceptRole)
            box.setDefaultButton(cu)
            box.exec()
            pref = "catchup" if box.clickedButton() is cu else "continue"
            self.settings["catch_up"] = pref
            store.save(self.settings)
        lock = self.runner.lock if self.runner is not None else threading.RLock()
        with lock:
            if pref == "continue":
                s.rewind_to_delivered()
            s.hold = False
        if self.runner is not None:
            self.runner.poke()

    def _maybe_offer_resume(self, udid: str):
        """A connected phone with a route left on disk (and nothing running)."""
        if self.session is not None or not udid:
            return
        d = store.load_session(udid)
        if d:
            self.restore_session(udid, d)

    # ------------------------------------------------------------------ keep the Mac awake

    def hold_awake(self, on: bool):
        """Keep the Mac out of idle sleep while a route runs. `-w` ties caffeinate to
        this process, so a crash can't leave the Mac unable to sleep."""
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

    def set_battery_warning(self, text: str):
        self.panel.running.set_battery(text)

    # ------------------------------------------------------------------ walking

    def _walk_press(self, vec):
        n, e = vec
        mag = math.hypot(n, e) or 1.0
        self._walk_vec = (n / mag, e / mag)
        self._start_walk()

    def _walk_release(self):
        self._walk_vec = (0.0, 0.0)
        self._walking = False

    def _start_walk(self):
        if self._walking:
            return
        if self.conn_state != "connected":
            self._walk_vec = (0.0, 0.0)
            return
        if self.session is not None:
            self._walk_vec = (0.0, 0.0)
            self.show_toast("A route is running. Stop it to walk by hand.")
            return
        self._walk_pos = self.spoof or self.real or self.map.center()
        self._walking = True
        threading.Thread(target=self._walk_worker, daemon=True).start()

    def _walk_worker(self):
        dt = 0.18
        last = time.monotonic()
        speed = max(0.5, float(self.settings.get("speed_kmh", 5.0)) * KMH)
        while self._walking and self.bridge.device is not None:
            vx, vy = self._walk_vec
            now = time.monotonic()
            step_t = min(now - last, 1.0)
            last = now
            if (vx or vy) and self._walk_pos and step_t > 0.01:
                lat, lon = self._walk_pos
                dist = speed * step_t
                lat += (vx * dist) / _M_PER_DEG
                lon += (vy * dist) / (_M_PER_DEG * max(0.15, math.cos(math.radians(lat))))
                self._walk_pos = (lat, lon)
                if not self.bridge.push(lat, lon):
                    self._walk_release()
                    break
                self._walkStep.emit(lat, lon)
            time.sleep(dt)

    def _on_walk_step(self, lat: float, lon: float):
        self._set_spoof((lat, lon), "")
        self.spoof_name = ""
        if self.map.is_near_edge(lat, lon):
            self.map.pan_to(lat, lon, animate_ms=300)
        now = time.monotonic()
        if now - getattr(self, "_last_persist", 0.0) > 3.0:
            self._last_persist = now
            self._persist_spoof()

    def key_walk(self, key: str, pressed: bool):
        if pressed:
            self._keys_down.add(key)
        else:
            self._keys_down.discard(key)
        n = ("Up" in self._keys_down) - ("Down" in self._keys_down)
        e = ("Right" in self._keys_down) - ("Left" in self._keys_down)
        if not n and not e:
            if self._walking:
                self._walk_release()
                self._persist_spoof()
        else:
            mag = math.hypot(n, e)
            self._walk_vec = (n / mag, e / mag)
            self._start_walk()

    # ------------------------------------------------------------------ jitter / heartbeat

    def _jitter_worker(self):
        while not self._closing:
            try:
                if (self._jitter_on and self.conn_state == "connected" and self.spoof
                        and not self._walking and self.session is None):
                    a = self.spoof
                    jl = _jitter(a[0], a[1], 4.0)
                    if self.bridge.push(jl[0], jl[1], quiet=True):
                        self._jitterStep.emit(jl[0], jl[1])
            except Exception:
                pass
            time.sleep(1.5)

    def _on_jitter_step(self, lat, lon):
        if self._spoof_ov is not None:
            self.map.move_marker(self._spoof_ov, lat, lon)   # the dot wobbles, the record doesn't

    def set_jitter(self, on: bool):
        self._jitter_on = bool(on)

    def set_pulsing(self, on: bool):
        self._pulse = bool(on)
        if self._spoof_item is not None:
            self._spoof_item.set_pulsing(self._pulse)

    def set_walk_pad(self, on: bool):
        self._pad_wanted = bool(on)
        self._sync_chrome()

    def heartbeat_point(self):
        """The fix the bridge's monitor may re-assert to prove the channel is
        alive, or None when something else is already writing (or nothing is set)."""
        if self._walking or self._jitter_on:
            return None
        if self.session is not None and self.runner is not None and not self.session.paused:
            return None
        return self.spoof

    # ------------------------------------------------------------------ keyboard

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def escape(self) -> bool:
        if self.suggest_box.isVisible():
            self._hide_suggestions()
        elif self.pop.isVisible():
            self.pop.hide()
        elif self.search.hasFocus():
            self.search.clearFocus()
        elif self.pin is not None:
            self.close_pin()
        elif self.map.width() < NARROW and self._sheet_open:
            self._set_sheet(False)
        else:
            return False
        return True

    # ------------------------------------------------------------------ theme

    def retheme(self):
        self.map.set_dark(theme.is_dark())
        self._pin_px = make_pin()
        if self._pin_ov is not None:
            self.map.set_marker_pixmap(self._pin_ov, self._pin_px)
        if self._real_ov is not None:
            self.map.set_marker_pixmap(self._real_ov, make_real_marker()[0])
        self._glass.setPixmap(icon_search())
        if self.session is not None:
            self._redraw_session_overlays()
        elif self.draft is not None:
            self._draw_plan()
        self.welcome.update()
        self._sync_chrome()

    def _save_view(self):
        la, lo = self.map.center()
        self.settings["view"] = {"lat": la, "lon": lo, "zoom": self.map.zoom}
        store.save(self.settings)

    # ------------------------------------------------------------------ search

    def eventFilter(self, obj, e):
        if obj is self.search and e.type() == QEvent.Type.KeyPress and e.key() == Qt.Key.Key_Escape:
            self._hide_suggestions()
            self.search.clearFocus()
            return True
        return super().eventFilter(obj, e)

    def _on_type(self, text: str):
        self._suggest_timer.stop()
        q = text.strip()
        if len(q) < 2 or geo.parse_coords(q):
            self._hide_suggestions()
            return
        self._suggest_timer.start(250)

    def _fire_suggest(self):
        q = self.search.text().strip()
        if len(q) >= 2:
            threading.Thread(target=lambda: self._suggestReady.emit(q, geo.suggest(q)),
                             daemon=True).start()

    def _show_suggestions(self, q: str, items: list):
        if self.search.text().strip() != q:
            return
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
        self.suggest_box.show(); self.suggest_box.raise_()
        self._layout()

    def _hide_suggestions(self):
        self.suggest_box.hide()
        self._suggest_items = []

    def _pick(self, item: dict):
        self._hide_suggestions()
        self.search.setText(item["label"])
        self.search.clearFocus()
        self.drop_pin(item["lat"], item["lon"], name=_place_label(item), center=True)

    def _on_enter(self):
        q = self.search.text().strip()
        c = geo.parse_coords(q)
        if c:
            self.search.clearFocus()
            self.drop_pin(*c, center=True)
        elif self._suggest_items:
            self._pick(self._suggest_items[0])
        elif q:
            self._hide_suggestions()
            self.show_toast(f"Searching for “{q}”…")
            threading.Thread(target=self._geocode_worker, args=(q,), daemon=True).start()

    def _geocode_worker(self, q: str):
        try:
            lat, lon = geo.geocode(q)
            self._geocodeReady.emit(lat, lon, q)
        except Exception as e:
            self._geocodeFailed.emit(str(e))
