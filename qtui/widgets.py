"""Small reusable native controls Qt doesn't ship: a segmented control, a
painted toggle switch, a label that elides instead of clipping, a status dot,
and a few crisp painted icons. All match the app's dark theme.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, QPropertyAnimation, Property
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QWidget,
)

from . import theme


def _blend(a: str, b: str, t: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(
        int(ca.red() + (cb.red() - ca.red()) * t),
        int(ca.green() + (cb.green() - ca.green()) * t),
        int(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


def repolish(w: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property (variant) changed."""
    w.style().unpolish(w)
    w.style().polish(w)


def button(text: str, variant: str = "primary", width: int | None = None,
           height: int = 32) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("variant", variant)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(height)
    if width:
        b.setFixedWidth(width)
    return b


def hairline(vertical: bool = False) -> QFrame:
    f = QFrame()
    f.setObjectName("VHairline" if vertical else "Hairline")
    return f


class Segmented(QFrame):
    """A pill of mutually-exclusive options (Teleport/Route, Walk/Run/…).

    `changed` fires only when the selection actually changes. Re-clicking the
    option that is already selected is not a change: it used to re-run the whole
    mode switch, which reset the Connect button while still connected and wiped
    the iPhone-mode QR code.
    """

    changed = Signal(str)

    def __init__(self, values, height: int = 32, font_pt: int = 12, parent=None):
        super().__init__(parent)
        self.setObjectName("Seg")
        self.setFixedHeight(height)
        self.setStyleSheet(f"#Seg {{ background: {theme.ELEV}; border-radius: {height // 2}px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns: dict[str, QPushButton] = {}
        self._value = values[0] if values else ""
        inner = height - 6
        for v in values:
            b = QPushButton(v)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(inner)
            b.setFont(theme.ui_font(font_pt, weight=600))
            b.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {theme.MUTED}; border: none;"
                f" border-radius: {inner // 2}px; padding: 0 12px; }}"
                f"QPushButton:checked {{ background: {theme.BLUE}; color: #ffffff; }}"
                f"QPushButton:hover:!checked {{ color: {theme.TEXT}; }}")
            self._group.addButton(b)
            lay.addWidget(b)
            self._btns[v] = b
            b.clicked.connect(lambda _=False, val=v: self._clicked(val))
        if values:
            self._btns[values[0]].setChecked(True)

    def _clicked(self, v: str):
        if v == self._value:
            return
        self._value = v
        self.changed.emit(v)

    def set_value(self, v: str):
        """Select `v` without emitting (external sync)."""
        if v in self._btns:
            self._btns[v].setChecked(True)
            self._value = v

    def value(self) -> str:
        return self._value


class Switch(QAbstractButton):
    """A compact animated on/off switch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 23)
        self._t = 0.0
        self._anim = QPropertyAnimation(self, b"t", self)
        self._anim.setDuration(140)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool):
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        h = self.height()
        p.setBrush(_blend(theme.GHOST_HI, theme.BLUE, self._t))
        p.drawRoundedRect(QRectF(0, 0, self.width(), h), h / 2, h / 2)
        m = 2.5
        d = h - 2 * m
        x = m + (self.width() - 2 * m - d) * self._t
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, m, d, d))

    def _get_t(self) -> float:
        return self._t

    def _set_t(self, v: float):
        self._t = v
        self.update()

    t = Property(float, _get_t, _set_t)

    def set_on(self, on: bool):
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)
        self._t = 1.0 if on else 0.0
        self.update()


class ElidedLabel(QLabel):
    """A one-line label that ends in … rather than being cut off mid-word.

    text() still returns the full string (and it is the tooltip when elided), so
    nothing that reads the label back sees a truncated message.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setMinimumWidth(10)

    def paintEvent(self, e):
        p = QPainter(self)
        fm = QFontMetrics(self.font())
        r = self.contentsRect()
        full = self.text()
        shown = fm.elidedText(full, Qt.TextElideMode.ElideRight, r.width())
        self.setToolTip(full if shown != full else "")
        p.setPen(self.palette().color(self.foregroundRole()))
        p.drawText(r, int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), shown)

    def minimumSizeHint(self):
        s = super().minimumSizeHint()
        s.setWidth(10)
        return s


class Dot(QWidget):
    """A small filled status dot."""

    def __init__(self, color: str = theme.GREY, size: int = 8, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(size, size)

    def set_color(self, color: str):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QRectF(0, 0, self.width(), self.height()))


# ---- painted icons (crisp at any DPR, no image files to bundle) -----------

def _canvas(size: int) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return pm, p


def _pen(color: str, width: float) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def icon_search(color: str = theme.MUTED, size: int = 16) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color, 1.7))
    r = size * 0.30
    c = QPointF(size * 0.43, size * 0.43)
    p.drawEllipse(c, r, r)
    p.drawLine(QPointF(size * 0.65, size * 0.65), QPointF(size * 0.88, size * 0.88))
    p.end()
    return pm


def icon_locate(color: str = theme.TEXT, size: int = 18) -> QPixmap:
    """A navigation arrow, the universal 'show me where I am'."""
    pm, p = _canvas(size)
    path = QPainterPath()
    s = size
    path.moveTo(s * 0.86, s * 0.14)
    path.lineTo(s * 0.52, s * 0.88)
    path.lineTo(s * 0.44, s * 0.56)
    path.lineTo(s * 0.12, s * 0.48)
    path.closeSubpath()
    p.setPen(_pen(color, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.end()
    return pm


def icon_menu(color: str = theme.TEXT, size: int = 18) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color, 1.7))
    for y in (0.3, 0.5, 0.7):
        p.drawLine(QPointF(size * 0.2, size * y), QPointF(size * 0.8, size * y))
    p.end()
    return pm


def icon_plus(color: str = theme.TEXT, size: int = 16, minus: bool = False) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color, 1.8))
    p.drawLine(QPointF(size * 0.2, size * 0.5), QPointF(size * 0.8, size * 0.5))
    if not minus:
        p.drawLine(QPointF(size * 0.5, size * 0.2), QPointF(size * 0.5, size * 0.8))
    p.end()
    return pm


def icon_undo(color: str = theme.TEXT, size: int = 16) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color, 1.6))
    path = QPainterPath()
    path.moveTo(size * 0.30, size * 0.40)
    path.lineTo(size * 0.62, size * 0.40)
    path.cubicTo(size * 0.92, size * 0.40, size * 0.92, size * 0.84, size * 0.62, size * 0.84)
    path.lineTo(size * 0.40, size * 0.84)
    p.drawPath(path)
    p.drawLine(QPointF(size * 0.30, size * 0.40), QPointF(size * 0.46, size * 0.24))
    p.drawLine(QPointF(size * 0.30, size * 0.40), QPointF(size * 0.46, size * 0.56))
    p.end()
    return pm


def icon_close(color: str = theme.MUTED, size: int = 14) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color, 1.6))
    p.drawLine(QPointF(size * 0.25, size * 0.25), QPointF(size * 0.75, size * 0.75))
    p.drawLine(QPointF(size * 0.75, size * 0.25), QPointF(size * 0.25, size * 0.75))
    p.end()
    return pm


def icon_arrow(angle_deg: float, color: str = theme.TEXT, size: int = 16) -> QPixmap:
    """A chevron pointing at `angle_deg` (0 = up, clockwise), for the walk pad."""
    pm, p = _canvas(size)
    p.translate(size / 2, size / 2)
    p.rotate(angle_deg)
    p.setPen(_pen(color, 1.9))
    p.drawLine(QPointF(-size * 0.24, size * 0.10), QPointF(0, -size * 0.16))
    p.drawLine(QPointF(0, -size * 0.16), QPointF(size * 0.24, size * 0.10))
    p.end()
    return pm
