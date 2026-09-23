"""Map marker graphics, drawn natively with QPainter.

- SpoofMarker: where the iPhone is set to. The accent colour, gently pulsing,
  with a heading arrow while it moves. It glides between positions.
- make_real_marker: the phone's real location as far as we can know it — a
  subtle hollow grey dot, labelled "approximate" (it is really this Mac's).
- make_pin: a dropped pin, a candidate, not a location.
- make_start / make_end / make_stop: the route's ends and numbered stops.
"""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QEasingCurve, QPointF, QRectF, Qt, QPropertyAnimation, QVariantAnimation, Property,
)
from PySide6.QtGui import (
    QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import QGraphicsObject

from . import theme

_DPR = 2.0  # render markers at retina density


def _canvas(w: float, h: float) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(int(w * _DPR), int(h * _DPR))
    pm.setDevicePixelRatio(_DPR)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return pm, p


def make_pin(color: str | None = None, w: int = 34, h: int = 46) -> QPixmap:
    """A teardrop pin: gradient head + tip, white core, tip at the bottom."""
    pm, p = _canvas(w, h)
    c = QColor(color or theme.PIN)
    cx, r = w / 2, w * 0.37
    head = r + w * 0.05
    ang = math.radians(58)
    dx, dy = r * math.cos(ang), r * math.sin(ang)
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    path.addEllipse(QPointF(cx, head), r, r)
    path.moveTo(cx - dx, head + dy)
    path.lineTo(cx + dx, head + dy)
    path.lineTo(cx, h - 1)
    path.closeSubpath()
    grad = QLinearGradient(0, head - r, 0, h)
    grad.setColorAt(0.0, c.lighter(130))
    grad.setColorAt(1.0, c)
    p.setPen(QPen(QColor(0, 0, 0, 60), 1))
    p.setBrush(QBrush(grad))
    p.drawPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(cx, head), r * 0.38, r * 0.38)
    p.end()
    return pm


def make_real_marker(label: str = "approximate") -> tuple[QPixmap, tuple[float, float]]:
    """A hollow grey ring with a small caption. Returns (pixmap, anchor offset)."""
    w, h = 84.0, 34.0
    pm, p = _canvas(w, h)
    ring = QColor(theme.REAL)
    p.setPen(QPen(ring, 2.2))
    p.setBrush(QColor(255, 255, 255, 40 if theme.is_dark() else 120))
    p.drawEllipse(QPointF(w / 2, 9), 6.5, 6.5)
    f = theme.ui_font(9, weight=600)
    p.setFont(f)
    p.setPen(QColor(theme.MAP_BG))            # a halo so the caption reads on any map
    for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        p.drawText(QRectF(ox, 19 + oy, w, 14), Qt.AlignmentFlag.AlignHCenter, label)
    p.setPen(ring)
    p.drawText(QRectF(0, 19, w, 14), Qt.AlignmentFlag.AlignHCenter, label)
    p.end()
    return pm, (w / 2, 9)


def _disc(size: float, fill: str, ring: str = "#ffffff", ring_w: float = 2.5) -> tuple[QPixmap, QPainter]:
    pm, p = _canvas(size, size)
    c = QPointF(size / 2, size / 2)
    p.setPen(QPen(QColor(0, 0, 0, 70), 1))
    p.setBrush(QColor(ring))
    p.drawEllipse(c, size / 2 - 0.5, size / 2 - 0.5)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(fill))
    p.drawEllipse(c, size / 2 - ring_w, size / 2 - ring_w)
    return pm, p


def make_start(size: int = 18) -> QPixmap:
    """Start: a white disc with an accent ring."""
    pm, p = _disc(size, "#ffffff", theme.ACCENT, 4.0)
    p.end()
    return pm


def make_end(size: int = 26) -> QPixmap:
    """End: an accent disc carrying a small white flag."""
    pm, p = _disc(size, theme.ACCENT)
    s = size
    p.setPen(QPen(QColor("#ffffff"), 1.6))
    p.drawLine(QPointF(s * 0.38, s * 0.28), QPointF(s * 0.38, s * 0.74))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    p.drawPolygon(QPolygonF([QPointF(s * 0.40, s * 0.28), QPointF(s * 0.70, s * 0.38),
                             QPointF(s * 0.40, s * 0.50)]))
    p.end()
    return pm


def make_stop(n: int, size: int = 22) -> QPixmap:
    """A numbered stop: white disc, accent ring and number."""
    pm, p = _disc(size, "#ffffff", theme.ACCENT, 2.5)
    p.setPen(QColor(theme.ACCENT))
    f = p.font(); f.setPixelSize(int(size * 0.52)); f.setBold(True); p.setFont(f)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, str(n))
    p.end()
    return pm


def make_waypoint(n: int, color: str | None = None, size: int = 22) -> QPixmap:
    """Backwards-compatible numbered waypoint (filled)."""
    pm, p = _disc(size, color or theme.ACCENT)
    p.setPen(QColor("#ffffff"))
    f = p.font(); f.setPixelSize(int(size * 0.56)); f.setBold(True); p.setFont(f)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, str(n))
    p.end()
    return pm


class SpoofMarker(QGraphicsObject):
    """Where the iPhone is set to: an accent dot with a white ring, a soft pulse,
    and a heading arrow while it's moving. Constant on-screen size."""

    def __init__(self, size: int = 48):
        super().__init__()
        self._size = size
        self._phase = 0.0
        self._heading: float | None = None
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(20)
        self._anim = QPropertyAnimation(self, b"phase", self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1800)
        self._anim.setLoopCount(-1)
        self._anim.start()
        self._glide: QVariantAnimation | None = None

    def boundingRect(self) -> QRectF:
        s = self._size
        return QRectF(-s / 2, -s / 2, s, s)

    def set_heading(self, deg: float | None):
        if deg != self._heading:
            self._heading = deg
            self.update()

    def paint(self, p: QPainter, opt, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        s = self._size
        accent = QColor(theme.ACCENT)
        ph = self._phase
        a = int(110 * (1.0 - ph))
        if a > 0:
            halo = QColor(accent)
            halo.setAlpha(a)
            p.setBrush(halo)
            r = s * (0.14 + 0.30 * ph)
            p.drawEllipse(QPointF(0, 0), r, r)
        if self._heading is not None:
            p.save()
            p.rotate(self._heading)
            cone = QColor(accent)
            cone.setAlpha(210)
            p.setBrush(cone)
            p.drawPolygon(QPolygonF([QPointF(0, -s * 0.42), QPointF(s * 0.13, -s * 0.20),
                                     QPointF(-s * 0.13, -s * 0.20)]))
            p.restore()
        p.setBrush(QColor(0, 0, 0, 55))
        p.drawEllipse(QPointF(0, 1), s * 0.21, s * 0.21)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(0, 0), s * 0.20, s * 0.20)
        p.setBrush(accent)
        p.drawEllipse(QPointF(0, 0), s * 0.14, s * 0.14)

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, v: float):
        self._phase = v
        self.update()

    phase = Property(float, _get_phase, _set_phase)

    def set_pulsing(self, on: bool):
        self._anim.start() if on else self._anim.stop()
        if not on:
            self._phase = 0.0
            self.update()


# the old name, for anything that still imports it
PulseMarker = SpoofMarker


def glide(tmap, ov, lat: float, lon: float, ms: int = 220) -> QVariantAnimation | None:
    """Move a marker overlay to (lat, lon) smoothly (purely visual)."""
    if ms <= 0:
        tmap.move_marker(ov, lat, lon)
        return None
    a0, o0 = ov.lat, ov.lon
    anim = QVariantAnimation(tmap)
    anim.setDuration(ms)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.valueChanged.connect(
        lambda f: tmap.move_marker(ov, a0 + (lat - a0) * f, o0 + (lon - o0) * f))
    anim.start(QVariantAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim
