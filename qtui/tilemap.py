"""A smooth native slippy-map widget.

It is a QGraphicsView (raster viewport) over a Web-Mercator tile pyramid:

  * tiles are QGraphicsPixmapItems fetched asynchronously with QNetworkAccessManager
    and a QNetworkDiskCache, so revisits are instant and there is no white flash;
  * the scene lives in "world pixels" at one integer zoom level; fractional zoom is
    a view scale, so pinch is smooth and tiles only re-grid when the level flips;
  * three styles: Standard (a street map, recoloured into the app's navy ramp in
    dark mode), Satellite, and Hybrid (imagery with road and label layers on top);
  * markers keep a constant on-screen size (ItemIgnoresTransformations) and can be
    draggable; routes are cosmetic-pen paths with direction arrows;
  * the floating panels' soft shadows are painted here, under them, from cached
    pixmaps: a per-widget blur effect would re-blur every panel on every pan frame;
  * macOS trackpad pinch/scroll arrive as real Qt gesture/wheel events.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPointF, QRect, QRectF, Qt, Signal, QStandardPaths, QTimer,
    QUrl, QVariantAnimation,
)
from PySide6.QtGui import (
    QColor, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest, QNetworkReply,
)
from PySide6.QtWidgets import (
    QApplication, QGraphicsScene, QGraphicsView, QGraphicsPixmapItem, QGraphicsPathItem,
)

from . import theme

TILE = 256
MAX_TILE_ATTEMPTS = 3     # give up re-requesting a tile that keeps erroring
SUBDOMAINS = ("a", "b", "c")
_ESRI = "https://services.arcgisonline.com/ArcGIS/rest/services/"


class TileSource(NamedTuple):
    """Where basemap tiles come from, and what to do with them on arrival.

    `url` is a slippy template; {s} picks a subdomain and {r} becomes "@2x" when
    `retina` is set and the display warrants it. Note the segment order is the
    source's own — Esri serves {z}/{y}/{x}. `overlays` are drawn on top (labels,
    roads) and never recoloured.
    """
    url: str
    attribution: str
    recolor: bool = False     # recolour a light basemap into the app's dark ramp
    retina: bool = False      # does this source serve @2x tiles?
    overlays: tuple = ()


# Esri's street map. CARTO's dark_all was the obvious dark choice, but it now
# stamps "API KEY REQUIRED" across every tile while still answering 200, so
# nothing in the fetch path can even tell it failed. Esri needs no key and
# carries street labels at every zoom; in dark mode it is recoloured on arrival.
ESRI_DARK = TileSource(
    url=_ESRI + "World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    attribution="Esri",
    recolor=True,
)
SATELLITE = TileSource(
    url=_ESRI + "World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution="Esri, Maxar, Earthstar Geographics",
)
HYBRID = SATELLITE._replace(overlays=(
    _ESRI + "Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}",
    _ESRI + "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
))
STYLES = {"standard": ESRI_DARK, "satellite": SATELLITE, "hybrid": HYBRID}
DEFAULT_SOURCE = ESRI_DARK

# Light-map luminance -> navy. Index = the grey level of the original tile; the
# ramp runs inverted (paper-white land becomes the dark canvas, black label ink
# becomes the light end) and the gamma pushes the mid-greys of road casings and
# building outlines down towards the canvas, so streets read as quiet lines and
# labels stay legible instead of every edge shouting at the same contrast.
RAMP_GAMMA = 1.45


def _ramp_table(dark: str | None = None, light: str | None = None,
                gamma: float = RAMP_GAMMA) -> list[int]:
    lo = QColor(dark or theme.DARK["MAP_BG"])
    hi = QColor(light or theme.DARK["MAP_INK"])
    table = []
    for v in range(256):
        f = (1.0 - v / 255.0) ** gamma
        table.append(QColor(
            round(lo.red() + (hi.red() - lo.red()) * f),
            round(lo.green() + (hi.green() - lo.green()) * f),
            round(lo.blue() + (hi.blue() - lo.blue()) * f)).rgb())
    return table


_RAMP = _ramp_table()


def recolor(img: QImage) -> QImage:
    """Map a light basemap tile onto the navy ramp.

    One colour-table lookup per pixel: take the tile's grey levels and read them
    back as indices into the ramp. No per-pixel Python.
    """
    gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
    data = bytes(gray.constBits())      # keep alive until the copy below is made
    idx = QImage(data, gray.width(), gray.height(), gray.bytesPerLine(),
                 QImage.Format.Format_Indexed8)
    idx.setColorTable(_RAMP)
    return idx.convertToFormat(QImage.Format.Format_RGB32)


def soften(img: QImage) -> QImage:
    """Calm a light street map for light mode: most of the colour out, a little
    lighter. Two composited draws in C++, no per-pixel Python."""
    out = img.convertToFormat(QImage.Format.Format_RGB32)
    gray = img.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(
        QImage.Format.Format_RGB32)
    p = QPainter(out)
    p.setOpacity(0.62)
    p.drawImage(0, 0, gray)
    p.setOpacity(0.18)
    p.fillRect(out.rect(), QColor(255, 255, 255))
    p.end()
    return out


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class _PointOverlay:
    """A constant-screen-size item pinned to a geographic coordinate."""
    __slots__ = ("item", "lat", "lon", "draggable")

    def __init__(self, item, lat, lon, draggable=False):
        self.item, self.lat, self.lon, self.draggable = item, lat, lon, draggable


class _PathOverlay:
    __slots__ = ("item", "pts")

    def __init__(self, item, pts):
        self.item, self.pts = item, list(pts)


class RouteLine(QGraphicsPathItem):
    """A route: a cosmetic line with small chevrons pointing the way.

    Arrow anchors are laid out once per integer zoom level (in scene pixels), so
    painting a long route costs a transform per visible arrow, not per vertex.
    """

    SPACING = 120.0             # scene pixels between arrows (≈ screen pixels)

    def __init__(self):
        super().__init__()
        self._anchors: list[tuple[float, float, float]] = []

    def set_anchors(self, path: QPainterPath):
        out, carry = [], self.SPACING / 2
        n = path.elementCount()
        for i in range(1, n):
            a, b = path.elementAt(i - 1), path.elementAt(i)
            dx, dy = b.x - a.x, b.y - a.y
            seg = math.hypot(dx, dy)
            if seg <= 0:
                continue
            ang = math.degrees(math.atan2(dy, dx))
            d = carry
            while d < seg:
                out.append((a.x + dx * d / seg, a.y + dy * d / seg, ang))
                d += self.SPACING
            carry = d - seg
        self._anchors = out

    def paint(self, p: QPainter, opt, widget=None):
        super().paint(p, opt, widget)
        if not self._anchors:
            return
        t = p.worldTransform()
        exposed = opt.exposedRect.adjusted(-20, -20, 20, 20) if opt is not None else None
        p.save()
        p.resetTransform()
        pen = QPen(QColor(255, 255, 255, 235), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for x, y, ang in self._anchors:
            if exposed is not None and not exposed.contains(QPointF(x, y)):
                continue
            c = t.map(QPointF(x, y))
            r = math.radians(ang)
            ux, uy = math.cos(r), math.sin(r)
            tip = QPointF(c.x() + ux * 2.4, c.y() + uy * 2.4)
            back = QPointF(c.x() - ux * 2.4, c.y() - uy * 2.4)
            p.drawPolyline(QPolygonF([
                QPointF(back.x() - uy * 2.9, back.y() + ux * 2.9), tip,
                QPointF(back.x() + uy * 2.9, back.y() - ux * 2.9)]))
        p.restore()


def _shadow_pixmap(w: int, h: int, radius: float, spread: int, alpha: int) -> QPixmap:
    """A soft, blurred-looking rounded-rect shadow, drawn as stacked rings."""
    pm = QPixmap(w + 2 * spread, h + 2 * spread)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for i in range(spread, 0, -1):
        a = alpha * (1.0 - i / (spread + 1)) ** 2 / spread * 2.2
        p.setPen(QPen(QColor(0, 0, 0, max(1, min(255, int(a)))), 1.0))
        r = QRectF(spread - i, spread - i, w + 2 * i, h + 2 * i)
        p.drawRoundedRect(r, radius + i, radius + i)
    p.end()
    return pm


class TileMap(QGraphicsView):
    clicked = Signal(float, float)       # left-click (not a drag) at (lat, lon)
    viewChanged = Signal()               # center/zoom changed
    userPanned = Signal()                # the user moved the map (drag, scroll, pinch)
    markerMoved = Signal(object, float, float)     # a draggable marker, live
    markerDropped = Signal(object, float, float)   # ... and where it was let go

    def __init__(self, parent=None, source: TileSource = DEFAULT_SOURCE,
                 min_zoom: int = 2, max_zoom: int = 20):
        super().__init__(parent)
        self._source = source
        self._dark = theme.is_dark()
        self.min_zoom, self.max_zoom = min_zoom, max_zoom
        self._zoom = 11.0                # fractional zoom
        self._z = 11                     # integer tile level the scene is built at
        self._clat, self._clon = 0.0, 0.0
        self._retina = self.devicePixelRatioF() >= 1.5

        # --- scene + raster viewport ---
        # (A QOpenGLWidget viewport fails to composite QGraphicsScene items on
        # macOS/Qt6; the default raster engine is smooth for a pixmap grid.)
        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QColor(theme.MAP_BG))
        self.setScene(self._scene)
        # Smart updates: the animated live marker repaints only its own rect
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.grabGesture(Qt.GestureType.PinchGesture)
        # never take keyboard focus: arrow keys must reach the window's walk
        # handler, not QGraphicsView's built-in scrolling
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Map")

        # --- async tile loading w/ disk cache (keyed by URL, so styles never mix) ---
        self._nam = QNetworkAccessManager(self)
        cache = QNetworkDiskCache(self)
        cache_dir = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation)) / "tiles" / "v2"
        cache.setCacheDirectory(str(cache_dir))
        cache.setMaximumCacheSize(512 * 1024 * 1024)
        self._nam.setCache(cache)
        self._nam.setTransferTimeout(10_000)   # a stalled fetch errors out → retried
        self._nam.finished.connect(self._on_tile)
        # keys are (z, x, y) for the base layer and (layer, z, x, y) for overlays
        self._tiles: dict[tuple, QGraphicsPixmapItem] = {}
        self._inflight: dict[tuple, QNetworkReply] = {}
        # tile -> failed attempts. A layout pass runs on every pan/zoom frame, so
        # retrying unconditionally meant a tile the server won't serve was
        # re-requested forever; cap it and keep the rescaled placeholder instead.
        self._failed: dict[tuple, int] = {}

        self._points: list[_PointOverlay] = []
        self._paths: list[_PathOverlay] = []
        self._press_pos = None
        self._dragged = False
        self._drag_ov: _PointOverlay | None = None
        self._drag_off = QPointF()
        self._tint: QColor | None = None   # brightness overlay
        self._based_z: int | None = None   # integer level the scene is projected at
        self._shadowed: list = []          # floating widgets to cast shadows for
        self._shadow_cache: dict = {}

        # eased zoom for buttons / double-click / mouse wheel (pinch stays live)
        self._zoom_anim: QVariantAnimation | None = None
        self._zoom_target: float = self._zoom
        self._pan_anim: QVariantAnimation | None = None

        # single-click is emitted after a beat so a double-click (zoom) can
        # cancel it, otherwise zooming would also drop a pin / waypoint
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(min(QApplication.doubleClickInterval(), 250))
        self._click_timer.timeout.connect(self._emit_click)
        self._click_ll: tuple[float, float] | None = None

    # ---- web-mercator projection (scene coords are tile*TILE at level self._z) ----

    def _world(self) -> float:
        return TILE * (2 ** self._z)

    def _scene_pt(self, lat: float, lon: float) -> QPointF:
        n = 2 ** self._z
        x = (lon + 180.0) / 360.0 * n
        s = math.sin(math.radians(_clamp(lat, -85.05112878, 85.05112878)))
        y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
        return QPointF(x * TILE, y * TILE)

    def _scene_to_ll(self, p: QPointF) -> tuple[float, float]:
        n = 2 ** self._z
        lon = p.x() / TILE / n * 360.0 - 180.0
        yt = p.y() / TILE / n
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yt))))
        return lat, lon

    @property
    def _scale(self) -> float:
        return 2.0 ** (self._zoom - self._z)

    # ---- public API -----------------------------------------------------

    def center(self) -> tuple[float, float]:
        return self._clat, self._clon

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_center(self, lat: float, lon: float):
        self._clat, self._clon = lat, lon
        self._apply_view()

    def pan_to(self, lat: float, lon: float, animate_ms: int = 0):
        """Recenter (no transform rebuild). Animated pans glide instead of jumping."""
        if self._pan_anim is not None:
            self._pan_anim.stop()
            self._pan_anim = None
        if animate_ms <= 0:
            self._center_on(lat, lon)
            return
        a = QVariantAnimation(self)
        a.setDuration(animate_ms)
        a.setEasingCurve(QEasingCurve.Type.InOutQuad)
        a.setStartValue(QPointF(self._clon, self._clat))
        a.setEndValue(QPointF(lon, lat))
        a.valueChanged.connect(lambda v: self._center_on(v.y(), v.x()))
        a.start()
        self._pan_anim = a

    def _center_on(self, lat: float, lon: float):
        self._clat, self._clon = lat, lon
        self.centerOn(self._scene_pt(lat, lon))
        self._layout_tiles()
        self.viewChanged.emit()

    def lat_lon_at(self, view_pos) -> tuple[float, float]:
        return self._scene_to_ll(self.mapToScene(view_pos))

    def view_pos(self, lat: float, lon: float):
        return self.mapFromScene(self._scene_pt(lat, lon))

    def set_brightness(self, name: str):
        self._tint = {"Dim": QColor(0, 0, 0, 54),
                      "Bright": QColor(255, 255, 255, 18)}.get(name)
        self.viewport().update()

    # -- style + appearance --

    @property
    def style_name(self) -> str:
        return next((k for k, v in STYLES.items() if v == self._source), "standard")

    def set_style(self, name: str):
        src = STYLES.get(name, ESRI_DARK)
        if src == self._source:
            return
        self._source = src
        self._reload_tiles()

    def set_dark(self, dark: bool):
        """Follow the app appearance: the standard map is recoloured in dark mode."""
        self._scene.setBackgroundBrush(QColor(theme.MAP_BG))
        if dark == self._dark:
            self.viewport().update()
            return
        self._dark = dark
        self._shadow_cache.clear()
        self._reload_tiles()

    def _reload_tiles(self):
        for key in list(self._tiles):
            self._scene.removeItem(self._tiles.pop(key))
        for reply in list(self._inflight.values()):
            reply.abort()
        self._inflight.clear()
        self._failed.clear()
        self._layout_tiles()            # the disk cache makes this near-instant

    # -- shadows for the floating chrome --

    def cast_shadow(self, widget):
        """Paint a soft shadow under this floating child whenever it is visible."""
        if widget not in self._shadowed:
            self._shadowed.append(widget)
            widget.installEventFilter(self)

    def eventFilter(self, obj, e):
        if obj in self._shadowed and e.type() in (
                QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide):
            self.viewport().update()
        return super().eventFilter(obj, e)

    def drawForeground(self, painter: QPainter, rect: QRectF):
        if self._tint is not None:
            painter.fillRect(rect, self._tint)
        if not self._shadowed:
            return
        painter.save()
        painter.resetTransform()
        for w in self._shadowed:
            if not w.isVisible():
                continue
            g = w.geometry()
            radius = 14 if w.objectName() != "Toast" else 12
            spread = 14
            key = (g.width(), g.height(), radius, theme.SHADOW_ALPHA)
            pm = self._shadow_cache.get(key)
            if pm is None:
                if len(self._shadow_cache) > 64:
                    self._shadow_cache.clear()
                pm = _shadow_pixmap(g.width(), g.height(), radius, spread, theme.SHADOW_ALPHA)
                self._shadow_cache[key] = pm
            painter.drawPixmap(g.x() - spread, g.y() - spread + 3, pm)
        painter.restore()

    def set_zoom(self, z: float, anchor=None):
        self._set_zoom(z, anchor)

    def set_view(self, lat: float, lon: float, z: float):
        self._stop_zoom_anim()
        if self._pan_anim is not None:
            self._pan_anim.stop()
            self._pan_anim = None
        self._clat, self._clon = lat, lon
        self._zoom = _clamp(float(z), self.min_zoom, self.max_zoom)
        self._z = int(round(self._zoom))
        self._apply_view()

    def fit(self, pts, pad: int = 80, max_zoom: float = 17):
        """Show all of `pts` with some room around them."""
        if not pts:
            return
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        lat, lon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
        if len(pts) == 1:
            self.set_view(lat, lon, min(max_zoom, max(self._zoom, 15)))
            return
        W, H = max(100, self.width() - 2 * pad), max(100, self.height() - 2 * pad)
        z = max_zoom
        while z > self.min_zoom:
            n = TILE * 2 ** z
            x0 = (min(lons) + 180) / 360 * n
            x1 = (max(lons) + 180) / 360 * n
            def y(la):
                s = math.sin(math.radians(_clamp(la, -85.05, 85.05)))
                return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
            if x1 - x0 <= W and y(min(lats)) - y(max(lats)) <= H:
                break
            z -= 0.25
        self.set_view(lat, lon, z)

    def zoom_at(self, step: float, view_pos=None):
        self._animate_zoom_by(step, view_pos)

    # markers: pixmap pinned to a coordinate, constant screen size
    def add_marker(self, lat: float, lon: float, pixmap: QPixmap, anchor: str = "s",
                   z: int = 10, draggable: bool = False, offset=None):
        item = QGraphicsPixmapItem(pixmap)
        item.setFlag(QGraphicsPixmapItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setZValue(z)
        dpr = pixmap.devicePixelRatio() or 1.0
        w, h = pixmap.width() / dpr, pixmap.height() / dpr
        if offset is not None:
            item.setOffset(-offset[0], -offset[1])
        else:
            item.setOffset(-w / 2, -h if anchor == "s" else -h / 2)
        if draggable:
            item.setCursor(Qt.CursorShape.OpenHandCursor)
        self._scene.addItem(item)
        ov = _PointOverlay(item, lat, lon, draggable)
        self._points.append(ov)
        item.setPos(self._scene_pt(lat, lon))
        return ov

    def add_item(self, item, lat: float, lon: float):
        """Pin an already-configured QGraphicsItem (e.g. an animated marker with
        ItemIgnoresTransformations) to a coordinate."""
        self._scene.addItem(item)
        ov = _PointOverlay(item, lat, lon)
        self._points.append(ov)
        item.setPos(self._scene_pt(lat, lon))
        return ov

    def move_marker(self, ov: _PointOverlay, lat: float, lon: float):
        ov.lat, ov.lon = lat, lon
        ov.item.setPos(self._scene_pt(lat, lon))

    def set_marker_pixmap(self, ov: _PointOverlay, pixmap: QPixmap):
        ov.item.setPixmap(pixmap)

    def update_path(self, ov: "_PathOverlay", pts):
        """Repoint an existing polyline (used to grow the travelled track)."""
        ov.pts = list(pts)
        self._rebuild_path(ov)

    def style_path(self, ov: "_PathOverlay", color: str, width: int, dashed: bool = False):
        ov.item.setPen(self._path_pen(color, width, dashed))

    @staticmethod
    def _path_pen(color: str, width: int, dashed: bool) -> QPen:
        pen = QPen(QColor(color), width)
        pen.setCosmetic(True)                 # constant width at any zoom
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if dashed:
            pen.setDashPattern([0.1, 2.2])    # round dots
        return pen

    def is_near_edge(self, lat: float, lon: float, margin: float = 0.22) -> bool:
        """True when this coordinate has drifted out of the comfortable middle."""
        p = self.mapFromScene(self._scene_pt(lat, lon))
        r = self.viewport().rect()
        mx, my = r.width() * margin, r.height() * margin
        return not (mx <= p.x() <= r.width() - mx and my <= p.y() <= r.height() - my)

    def remove_overlay(self, ov):
        if ov is None:
            return
        try:
            self._scene.removeItem(ov.item)
        except Exception:
            pass
        if ov in self._points:
            self._points.remove(ov)
        if ov in self._paths:
            self._paths.remove(ov)

    def add_path(self, pts, color: str | None = None, width: int = 5, z: int = 5,
                 dashed: bool = False, arrows: bool = False):
        item = RouteLine() if arrows else QGraphicsPathItem()
        item.setPen(self._path_pen(color or theme.ACCENT, width, dashed))
        item.setZValue(z)
        self._scene.addItem(item)
        ov = _PathOverlay(item, pts)
        self._paths.append(ov)
        self._rebuild_path(ov)
        return ov

    # ---- internals ------------------------------------------------------

    def _set_zoom(self, new_zoom: float, view_pos=None):
        new_zoom = _clamp(float(new_zoom), self.min_zoom, self.max_zoom)
        before = None
        if view_pos is not None:
            before = self._scene_to_ll(self.mapToScene(view_pos))
        self._zoom = new_zoom
        self._z = int(round(new_zoom))
        self._apply_view()
        if before is not None:
            after = self._scene_to_ll(self.mapToScene(view_pos))
            self._clat += before[0] - after[0]
            self._clon += before[1] - after[1]
            self._apply_view()

    def _stop_zoom_anim(self):
        if self._zoom_anim is not None:
            self._zoom_anim.stop()
            self._zoom_anim = None

    def _animate_zoom_by(self, step: float, view_pos=None):
        # accumulate onto the in-flight target so rapid clicks/notches chain
        base = self._zoom_target if self._zoom_anim is not None else self._zoom
        self._animate_zoom_to(base + step, view_pos)

    def _animate_zoom_to(self, target: float, view_pos=None):
        target = _clamp(float(target), self.min_zoom, self.max_zoom)
        self._stop_zoom_anim()
        if abs(target - self._zoom) < 1e-6:
            return
        self._zoom_target = target
        a = QVariantAnimation(self)
        a.setStartValue(float(self._zoom))
        a.setEndValue(target)
        a.setDuration(180)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.valueChanged.connect(lambda v: self._set_zoom(float(v), view_pos))
        a.finished.connect(self._zoom_anim_done)
        a.start()
        self._zoom_anim = a

    def _zoom_anim_done(self):
        # only natural completion lands here (stop() doesn't emit finished)
        if self._zoom_anim is not None and self.sender() is self._zoom_anim:
            self._zoom_anim = None

    def _apply_view(self):
        """Set the scale + recenter, then lay out tiles. The scene is only
        re-based (rect + overlay reprojection, O(overlay points)) when the
        integer level actually flips, so pinch frames stay cheap."""
        if self._z != self._based_z:
            w = self._world()
            self._scene.setSceneRect(0, 0, w, w)
            self._reproject()
            self._based_z = self._z
        self.resetTransform()
        self.scale(self._scale, self._scale)
        self.centerOn(self._scene_pt(self._clat, self._clon))
        self._layout_tiles()
        self.viewChanged.emit()

    def _reproject(self):
        for ov in self._points:
            ov.item.setPos(self._scene_pt(ov.lat, ov.lon))
        for ov in self._paths:
            self._rebuild_path(ov)

    def _rebuild_path(self, ov: _PathOverlay):
        path = QPainterPath()
        for i, (la, lo) in enumerate(ov.pts):
            p = self._scene_pt(la, lo)
            path.moveTo(p) if i == 0 else path.lineTo(p)
        ov.item.setPath(path)
        if isinstance(ov.item, RouteLine):
            ov.item.set_anchors(path)

    def _visible_scene_rect(self) -> QRectF:
        return self.mapToScene(self.viewport().rect()).boundingRect()

    def _layers(self) -> list[str]:
        return [self._source.url, *self._source.overlays]

    @staticmethod
    def _key(layer: int, z: int, x: int, y: int) -> tuple:
        return (z, x, y) if layer == 0 else (layer, z, x, y)

    def _layout_tiles(self):
        z, n = self._z, 2 ** self._z
        vis = self._visible_scene_rect()
        margin = TILE
        x0 = max(0, int((vis.left() - margin) // TILE))
        x1 = min(n - 1, int((vis.right() + margin) // TILE))
        y0 = max(0, int((vis.top() - margin) // TILE))
        y1 = min(n - 1, int((vis.bottom() + margin) // TILE))
        needed = set()
        fresh = []
        layers = range(len(self._layers()))
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                for layer in layers:
                    key = self._key(layer, z, tx, ty)
                    needed.add(key)
                    if key not in self._tiles:
                        self._make_tile(key, layer)
                        if layer == 0:
                            fresh.append(key)
                    elif 0 < self._failed.get(key, 0) < MAX_TILE_ATTEMPTS:
                        self._request(key)             # earlier fetch errored, try again
        # seed brand-new tiles with imagery rescaled from the level we're leaving,
        # BEFORE that level is pruned: zooming never blanks to the background
        for key in fresh:
            ph = self._placeholder(key)
            if ph is not None:
                self._tiles[key].setPixmap(ph)
        # prune anything off-screen or from a stale zoom level
        for key in list(self._tiles):
            if key not in needed:
                self._scene.removeItem(self._tiles.pop(key))
                self._failed.pop(key, None)
                reply = self._inflight.pop(key, None)
                if reply is not None:
                    reply.abort()   # stop wasting the connection pool on it

    def _placeholder(self, key) -> QPixmap | None:
        """A stand-in for a not-yet-loaded base tile, rescaled from tiles we have
        at a nearby level: crop of an ancestor (zooming in) or a composite of the
        four children (zooming out)."""
        z, x, y = key
        for d in (1, 2, 3):
            src = self._tiles.get((z - d, x >> d, y >> d))
            if src is None or src.pixmap().isNull():
                continue
            pm = src.pixmap()                       # raw device pixels below
            f = 1 << d
            w, h = pm.width() // f, pm.height() // f
            if w < 1 or h < 1:
                break
            sub = pm.copy((x % f) * w, (y % f) * h, w, h)
            out = sub.scaled(pm.width(), pm.height(),
                             Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            out.setDevicePixelRatio(pm.devicePixelRatio())
            return out
        kids = [self._tiles.get((z + 1, 2 * x + dx, 2 * y + dy))
                for dy in (0, 1) for dx in (0, 1)]
        pms = [k.pixmap() if k is not None and not k.pixmap().isNull() else None
               for k in kids]
        ref = next((p for p in pms if p is not None), None)
        if ref is None:
            return None
        W, H = ref.width(), ref.height()
        out = QPixmap(W, H)
        out.fill(QColor(theme.MAP_BG))
        p = QPainter(out)
        for i, pm in enumerate(pms):
            if pm is not None:
                dx, dy = i % 2, i // 2
                p.drawPixmap(QRect(dx * W // 2, dy * H // 2, W // 2, H // 2), pm)
        p.end()
        out.setDevicePixelRatio(ref.devicePixelRatio())
        return out

    def _make_tile(self, key, layer: int = 0):
        z, x, y = key[-3:]
        item = QGraphicsPixmapItem()
        item.setPos(x * TILE, y * TILE)
        item.setZValue(-10 + layer)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(item)
        self._tiles[key] = item
        self._request(key)

    def _request(self, key):
        if key in self._inflight:
            return
        layer = key[0] if len(key) == 4 else 0
        z, x, y = key[-3:]
        layers = self._layers()
        if layer >= len(layers):
            return
        sub = SUBDOMAINS[(x + y) % len(SUBDOMAINS)]
        r = "@2x" if (self._source.retina and self._retina) else ""
        url = (layers[layer].replace("{s}", sub).replace("{z}", str(z))
               .replace("{x}", str(x)).replace("{y}", str(y)).replace("{r}", r))
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.PreferCache)
        req.setAttribute(QNetworkRequest.Attribute.HttpPipeliningAllowedAttribute, True)
        req.setRawHeader(b"User-Agent", b"Spoofr/1.1 (macOS location utility)")
        req.setAttribute(QNetworkRequest.Attribute.User, list(key))
        self._inflight[key] = self._nam.get(req)

    def _on_tile(self, reply: QNetworkReply):
        key = reply.request().attribute(QNetworkRequest.Attribute.User)
        key = tuple(key) if key else key
        if self._inflight.get(key) is reply:
            self._inflight.pop(key, None)
        try:
            err = reply.error()
            if err == QNetworkReply.NetworkError.NoError:
                pix = QPixmap()
                pix.loadFromData(reply.readAll())
                if not pix.isNull():
                    item = self._tiles.get(key)
                    if item is not None:
                        layer = key[0] if len(key) == 4 else 0
                        item.setPixmap(self._prepare(pix, layer))
                        self._failed.pop(key, None)
            elif err != QNetworkReply.NetworkError.OperationCanceledError:
                # failed (offline blip / HTTP error / timeout): count it so the
                # next layout pass retries, up to MAX_TILE_ATTEMPTS
                if key in self._tiles:
                    self._failed[key] = self._failed.get(key, 0) + 1
        finally:
            reply.deleteLater()

    def _prepare(self, pix: QPixmap, layer: int = 0) -> QPixmap:
        """Scale-correct the tile, and recolour it if the source needs it.

        The device pixel ratio comes from the tile we actually received rather
        than from a guess about the display: a source that only serves 256px
        would otherwise be drawn at half size on a retina Mac and tear the grid.
        """
        dpr = max(1.0, pix.width() / TILE)
        if layer == 0 and self._source.recolor:
            img = pix.toImage()
            pix = QPixmap.fromImage(recolor(img) if self._dark else soften(img))
        pix.setDevicePixelRatio(dpr)
        return pix

    # ---- input: pan (drag / two-finger scroll), zoom (pinch / double-click) ----

    def wheelEvent(self, e):
        d = e.pixelDelta()
        if not d.isNull():
            # trackpad two-finger scroll: pan (with the OS's own momentum)
            self._pan_pixels(d.x(), d.y())
            self.userPanned.emit()
        else:
            # an external mouse wheel: zoom at the cursor, like every map app
            notches = e.angleDelta().y() / 120.0
            if notches:
                self._animate_zoom_by(notches * 0.6, e.position().toPoint())
        e.accept()

    def _pan_pixels(self, dx: float, dy: float):
        if self._pan_anim is not None:
            self._pan_anim.stop()
            self._pan_anim = None
        sp = self._scene_pt(self._clat, self._clon)
        sp.setX(sp.x() - dx / self._scale)
        sp.setY(sp.y() - dy / self._scale)
        self._clat, self._clon = self._scene_to_ll(sp)
        self.centerOn(sp)
        self._layout_tiles()
        self.viewChanged.emit()

    def event(self, e):
        if e.type() == e.Type.Gesture:
            g = e.gesture(Qt.GestureType.PinchGesture)
            if g is not None:
                sf = g.scaleFactor()
                if sf and sf != 1.0:
                    self._stop_zoom_anim()      # live fingers beat a running ease
                    pos = self.mapFromGlobal(self.cursor().pos())
                    self._set_zoom(self._zoom + math.log2(sf), pos)
                e.accept()
                return True
        return super().event(e)

    def _draggable_at(self, pos) -> _PointOverlay | None:
        for item in self.items(pos):
            for ov in self._points:
                if ov.draggable and ov.item is item:
                    return ov
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            ov = self._draggable_at(e.position().toPoint())
            if ov is not None:
                self._drag_ov = ov
                self._drag_off = ov.item.pos() - self.mapToScene(e.position().toPoint())
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                e.accept()
                return
            self._press_pos = e.position()
            self._dragged = False
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_ov is not None:
            sp = self.mapToScene(e.position().toPoint()) + self._drag_off
            lat, lon = self._scene_to_ll(sp)
            self.move_marker(self._drag_ov, lat, lon)
            self.markerMoved.emit(self._drag_ov, lat, lon)
            e.accept()
            return
        if self._press_pos is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            delta = e.position() - self._press_pos
            if self._dragged or delta.manhattanLength() > 3:
                if not self._dragged:
                    self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                    self.userPanned.emit()
                self._dragged = True
                self._press_pos = e.position()
                self._pan_pixels(delta.x(), delta.y())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._drag_ov is not None:
            ov, self._drag_ov = self._drag_ov, None
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self.markerDropped.emit(ov, ov.lat, ov.lon)
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            if self._dragged:
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            else:
                # stash the click; it only fires if no double-click follows
                self._click_ll = self._scene_to_ll(self.mapToScene(e.position().toPoint()))
                self._click_timer.start()
            self._press_pos = None
        super().mouseReleaseEvent(e)

    def _emit_click(self):
        if self._click_ll is not None:
            lat, lon = self._click_ll
            self._click_ll = None
            self.clicked.emit(lat, lon)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._click_timer.stop()      # it was a zoom, not a pin/waypoint
            self._click_ll = None
            self._animate_zoom_by(1.0, e.position().toPoint())
        e.accept()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_view()
