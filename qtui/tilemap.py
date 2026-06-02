"""A GPU-accelerated slippy-map widget — the native replacement for the Tk
canvas map.

It is a QGraphicsView (OpenGL viewport) over a Web-Mercator tile pyramid:

  * tiles are QGraphicsPixmapItems fetched asynchronously with QNetworkAccessManager
    and a QNetworkDiskCache, so revisits are instant and there is no white flash;
  * the scene lives in "world pixels" at one integer zoom level; fractional zoom is
    a GPU view-scale, so pinch is buttery and tiles only re-grid when the level flips;
  * markers keep a constant on-screen size (ItemIgnoresTransformations); routes are
    cosmetic-pen paths that stay a constant width at any zoom;
  * macOS trackpad pinch/scroll arrive as real Qt gesture/wheel events — no pyobjc.

Public surface mirrors what the app needs: set_center/center, set_zoom/zoom,
zoom_at, add_marker/move_marker/remove_item, set_path, plus `clicked(lat, lon)`
and `viewChanged` signals.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, QUrl, QStandardPaths
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest, QNetworkReply,
)
from PySide6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsPixmapItem, QGraphicsPathItem,
)

from . import theme

TILE = 256
DEFAULT_TILES = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
SUBDOMAINS = ("a", "b", "c")


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class _PointOverlay:
    """A constant-screen-size item pinned to a geographic coordinate."""
    __slots__ = ("item", "lat", "lon")

    def __init__(self, item, lat, lon):
        self.item, self.lat, self.lon = item, lat, lon


class _PathOverlay:
    __slots__ = ("item", "pts")

    def __init__(self, item, pts):
        self.item, self.pts = item, list(pts)


class TileMap(QGraphicsView):
    clicked = Signal(float, float)       # left-click (not a drag) at (lat, lon)
    viewChanged = Signal()               # center/zoom changed (after the view settled)

    def __init__(self, parent=None, tile_url: str = DEFAULT_TILES,
                 min_zoom: int = 2, max_zoom: int = 20):
        super().__init__(parent)
        self._tile_url = tile_url
        self.min_zoom, self.max_zoom = min_zoom, max_zoom
        self._zoom = 11.0                # fractional zoom
        self._z = 11                     # integer tile level the scene is built at
        self._clat, self._clon = 0.0, 0.0
        self._retina = self.devicePixelRatioF() >= 1.5

        # --- scene + raster viewport ---
        # (The QOpenGLWidget viewport fails to composite QGraphicsScene items on
        # macOS/Qt6; the default raster engine is CoreGraphics-backed and smooth
        # for a pixmap tile grid, and it actually renders.)
        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QColor(theme.MAP_BG))
        self.setScene(self._scene)
        # Smart updates: the animated live marker repaints only its own small
        # rect each frame instead of the whole tile grid (the Tk pulse's sin).
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

        # --- async tile loading w/ disk cache ---
        self._nam = QNetworkAccessManager(self)
        cache = QNetworkDiskCache(self)
        cache_dir = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation)) / "tiles"
        cache.setCacheDirectory(str(cache_dir))
        cache.setMaximumCacheSize(256 * 1024 * 1024)   # 256 MB on disk
        self._nam.setCache(cache)
        self._nam.finished.connect(self._on_tile)
        self._tiles: dict[tuple[int, int, int], QGraphicsPixmapItem] = {}
        self._pending: set[tuple[int, int, int]] = set()

        self._points: list[_PointOverlay] = []
        self._paths: list[_PathOverlay] = []
        self._press_pos = None
        self._dragged = False
        self._tint: QColor | None = None   # brightness overlay

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

    def pan_to(self, lat: float, lon: float):
        """Lightweight recenter (no transform rebuild) — for follow-during-walk."""
        self._clat, self._clon = lat, lon
        self.centerOn(self._scene_pt(lat, lon))
        self._layout_tiles()
        self.viewChanged.emit()

    def set_brightness(self, name: str):
        self._tint = {"Dim": QColor(0, 0, 0, 54),
                      "Bright": QColor(255, 255, 255, 18)}.get(name)
        self.viewport().update()

    def drawForeground(self, painter: QPainter, rect: QRectF):
        if self._tint is not None:
            painter.fillRect(rect, self._tint)

    def set_zoom(self, z: float, anchor=None):
        self._set_zoom(z, anchor)

    def set_view(self, lat: float, lon: float, z: float):
        self._clat, self._clon = lat, lon
        self._zoom = _clamp(float(z), self.min_zoom, self.max_zoom)
        self._z = int(round(self._zoom))
        self._apply_view()

    def zoom_at(self, step: float, view_pos=None):
        self._set_zoom(self._zoom + step, view_pos)

    # markers: pixmap pinned to a coordinate, constant screen size
    def add_marker(self, lat: float, lon: float, pixmap: QPixmap, anchor: str = "s", z: int = 10):
        item = QGraphicsPixmapItem(pixmap)
        item.setFlag(QGraphicsPixmapItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setZValue(z)
        dpr = pixmap.devicePixelRatio() or 1.0
        w, h = pixmap.width() / dpr, pixmap.height() / dpr
        item.setOffset(-w / 2, -h if anchor == "s" else -h / 2)
        self._scene.addItem(item)
        ov = _PointOverlay(item, lat, lon)
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

    def remove_overlay(self, ov):
        try:
            self._scene.removeItem(ov.item)
        except Exception:
            pass
        if ov in self._points:
            self._points.remove(ov)
        if ov in self._paths:
            self._paths.remove(ov)

    def add_path(self, pts, color: str = theme.BLUE, width: int = 5, z: int = 5):
        item = QGraphicsPathItem()
        pen = QPen(QColor(color), width)
        pen.setCosmetic(True)                 # constant width at any zoom
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        item.setPen(pen)
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

    def _apply_view(self):
        """Re-base the scene at the current integer zoom, set the GPU scale, recenter,
        then lay out tiles + overlays. Cheap enough to call every gesture frame."""
        w = self._world()
        self._scene.setSceneRect(0, 0, w, w)
        self.resetTransform()
        self.scale(self._scale, self._scale)
        self.centerOn(self._scene_pt(self._clat, self._clon))
        self._reproject()
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

    def _visible_scene_rect(self) -> QRectF:
        return self.mapToScene(self.viewport().rect()).boundingRect()

    def _layout_tiles(self):
        z, n = self._z, 2 ** self._z
        vis = self._visible_scene_rect()
        margin = TILE
        x0 = max(0, int((vis.left() - margin) // TILE))
        x1 = min(n - 1, int((vis.right() + margin) // TILE))
        y0 = max(0, int((vis.top() - margin) // TILE))
        y1 = min(n - 1, int((vis.bottom() + margin) // TILE))
        needed = set()
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                key = (z, tx, ty)
                needed.add(key)
                if key not in self._tiles:
                    self._make_tile(key)
        # prune anything off-screen or from a stale zoom level
        for key in list(self._tiles):
            if key not in needed:
                self._scene.removeItem(self._tiles.pop(key))
                self._pending.discard(key)

    def _make_tile(self, key):
        z, x, y = key
        item = QGraphicsPixmapItem()
        item.setPos(x * TILE, y * TILE)
        item.setZValue(-10)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(item)
        self._tiles[key] = item
        self._request(key)

    def _request(self, key):
        if key in self._pending:
            return
        z, x, y = key
        sub = SUBDOMAINS[(x + y) % len(SUBDOMAINS)]
        r = "@2x" if self._retina else ""
        url = (self._tile_url.replace("{s}", sub).replace("{z}", str(z))
               .replace("{x}", str(x)).replace("{y}", str(y)).replace("{r}", r))
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.PreferCache)
        req.setAttribute(QNetworkRequest.Attribute.HttpPipeliningAllowedAttribute, True)
        req.setRawHeader(b"User-Agent", b"Spoofr/1.0 (macOS location utility)")
        req.setAttribute(QNetworkRequest.Attribute.User, key)
        self._pending.add(key)
        self._nam.get(req)

    def _on_tile(self, reply: QNetworkReply):
        key = reply.request().attribute(QNetworkRequest.Attribute.User)
        self._pending.discard(tuple(key) if key else key)
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                pix = QPixmap()
                pix.loadFromData(reply.readAll())
                if not pix.isNull():
                    pix.setDevicePixelRatio(2.0 if self._retina else 1.0)
                    item = self._tiles.get(tuple(key) if key else key)
                    if item is not None:
                        item.setPixmap(pix)
        finally:
            reply.deleteLater()

    # ---- input: pan (drag / two-finger scroll), zoom (pinch / double-click) ----

    def wheelEvent(self, e):
        d = e.pixelDelta()
        if d.isNull():
            d = e.angleDelta() / 8
        self._pan_pixels(d.x(), d.y())
        e.accept()

    def _pan_pixels(self, dx: float, dy: float):
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
                    pos = self.mapFromGlobal(self.cursor().pos())
                    self._set_zoom(self._zoom + math.log2(sf), pos)
                e.accept()
                return True
        return super().event(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position()
            self._dragged = False
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press_pos is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            delta = e.position() - self._press_pos
            if self._dragged or delta.manhattanLength() > 3:
                self._dragged = True
                self._press_pos = e.position()
                self._pan_pixels(delta.x(), delta.y())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            if not self._dragged:
                lat, lon = self._scene_to_ll(self.mapToScene(e.position().toPoint()))
                self.clicked.emit(lat, lon)
            self._press_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.zoom_at(1.0, e.position().toPoint())
        e.accept()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_view()
        # round the corners so the map sits inside the card's rounded frame
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 11, 11)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
