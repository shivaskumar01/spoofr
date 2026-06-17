"""Map marker graphics, drawn natively with QPainter.

- make_pin / make_waypoint return crisp @2x QPixmaps for the staged pin + waypoints.
- PulseMarker is a live QGraphicsObject whose ring expands/fades via a
  QPropertyAnimation, a genuinely smooth 60fps pulse, not a frame cycle.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QPropertyAnimation, Property
from PySide6.QtGui import (
    QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPixmap,
)
from PySide6.QtWidgets import QGraphicsObject

from . import theme

_DPR = 2.0  # render markers at retina density


def make_pin(color: str, w: int = 38, h: int = 50) -> QPixmap:
    """A teardrop pin: gradient head + tip, white core, tip at the bottom."""
    pm = QPixmap(int(w * _DPR), int(h * _DPR))
    pm.setDevicePixelRatio(_DPR)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    c = QColor(color)
    cx, r = w / 2, w * 0.37
    head = r + w * 0.05
    ang = math.radians(58)
    dx, dy = r * math.cos(ang), r * math.sin(ang)
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)          # merge head + tip cleanly
    path.addEllipse(QPointF(cx, head), r, r)
    path.moveTo(cx - dx, head + dy)
    path.lineTo(cx + dx, head + dy)
    path.lineTo(cx, h - 1)
    path.closeSubpath()
    grad = QLinearGradient(0, head - r, 0, h)
    grad.setColorAt(0.0, c.lighter(138))
    grad.setColorAt(1.0, c)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawPath(path)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(cx, head), r * 0.40, r * 0.40)
    p.end()
    return pm


def make_waypoint(n: int, color: str = theme.BLUE, size: int = 22) -> QPixmap:
    """A small numbered route waypoint: filled circle, white ring + number."""
    pm = QPixmap(int(size * _DPR), int(size * _DPR))
    pm.setDevicePixelRatio(_DPR)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    c = QPointF(size / 2, size / 2)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(c, size * 0.48, size * 0.48)
    p.setBrush(QColor(color))
    p.drawEllipse(c, size * 0.40, size * 0.40)
    p.setPen(QColor("#ffffff"))
    f = p.font(); f.setPixelSize(int(size * 0.56)); f.setBold(True); p.setFont(f)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, str(n))
    p.end()
    return pm


class PulseMarker(QGraphicsObject):
    """The live 'You' marker: a steady white-ringed core with a ring that
    expands and fades on a loop. Constant on-screen size at any zoom."""

    def __init__(self, color: str = theme.LIVE, size: int = 46):
        super().__init__()
        self._color = QColor(color)
        self._size = size
        self._phase = 0.0
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(20)
        self._anim = QPropertyAnimation(self, b"phase", self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1600)
        self._anim.setLoopCount(-1)
        self._anim.start()

    def boundingRect(self) -> QRectF:
        s = self._size
        return QRectF(-s / 2, -s / 2, s, s)

    def paint(self, p: QPainter, opt, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        s = self._size
        ph = self._phase
        r = s * (0.13 + 0.33 * ph)
        a = int(150 * (1.0 - ph))
        if a > 0:
            col = QColor(self._color)
            col.setAlpha(a)
            p.setBrush(col)
            p.drawEllipse(QPointF(0, 0), r, r)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(0, 0), s * 0.20, s * 0.20)
        p.setBrush(self._color)
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
