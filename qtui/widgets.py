"""Small reusable native controls Qt doesn't ship: a segmented control, a
painted switch, a label that elides instead of clipping, a status dot, a toast,
a collapsible section, and crisp painted icons.

Colours come from the global stylesheet (object names / properties) or are read
from `theme` at paint time, so everything here follows light/dark switches.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import (
    QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer, Signal, Property,
)
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
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
    """Re-apply the stylesheet after a dynamic property changed."""
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def button(text: str, variant: str = "primary", width: int | None = None,
           height: int = 32, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setProperty("variant", variant)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(height)
    if width:
        b.setFixedWidth(width)
    if tip:
        b.setToolTip(tip)
    b.setAccessibleName(text or tip)
    return b


def label(text: str = "", size: int = 13, weight: int = 400, role: str = "",
          wrap: bool = False, num: bool = False) -> QLabel:
    lb = QLabel(text)
    lb.setFont(theme.num_font(size, weight) if num else theme.ui_font(size, weight))
    if role:
        lb.setProperty("role", role)
    if wrap:
        lb.setWordWrap(True)
    return lb


def set_role(lb: QLabel, role: str) -> None:
    if lb.property("role") != role:
        lb.setProperty("role", role)
        repolish(lb)


def hairline(vertical: bool = False) -> QFrame:
    f = QFrame()
    f.setObjectName("VHairline" if vertical else "Hairline")
    return f


class Segmented(QFrame):
    """A pill of mutually-exclusive options (Teleport/Route, Walk/Run/…).

    `changed` fires only when the selection actually changes: re-clicking the
    option already selected used to re-run the whole mode switch.
    """

    changed = Signal(str)

    def __init__(self, values, height: int = 30, font_pt: int = 12, parent=None,
                 labels: dict | None = None):
        super().__init__(parent)
        self.setObjectName("Seg")
        self.setFixedHeight(height)
        inner = height - 6
        # only the radii are per-instance; colours come from the global sheet
        self.setStyleSheet(f"QFrame#Seg {{ border-radius: {height // 2}px; }}"
                           f"QPushButton#SegBtn {{ border-radius: {inner // 2}px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns: dict[str, QPushButton] = {}
        self._value = values[0] if values else ""
        for v in values:
            b = QPushButton((labels or {}).get(v, v))
            b.setObjectName("SegBtn")
            # share the width and shrink to fit: a fixed natural width made the
            # widest control push a whole panel wider than its frame
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(10)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(inner)
            b.setFont(theme.ui_font(font_pt, weight=600))
            b.setAccessibleName(v)
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

    def button(self, v: str) -> QPushButton | None:
        return self._btns.get(v)


class Switch(QAbstractButton):
    """A compact animated on/off switch."""

    def __init__(self, parent=None, name: str = ""):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(38, 22)
        if name:
            self.setAccessibleName(name)
        self._t = 0.0
        self._anim = QPropertyAnimation(self, b"t", self)
        self._anim.setDuration(150)
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
        p.setBrush(_blend(theme.ELEV_HI, theme.ACCENT, self._t))
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
    """A one-line label that ends in … rather than being cut off.

    text() still returns the full string (it is also the tooltip when elided),
    so nothing that reads the label back sees a truncated message.
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

    def __init__(self, color: str | None = None, size: int = 8, parent=None):
        super().__init__(parent)
        self._color = QColor(color or theme.GREY)
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


class IconButton(QPushButton):
    """A square icon button whose painted icon follows the theme.

    `factory(color) -> QPixmap` is called again whenever the palette changes, so
    a light/dark switch re-renders the glyph in the right ink.
    """

    def __init__(self, factory: Callable[[str], QPixmap], tip: str, size: int = 34,
                 icon_px: int = 18, color: Callable[[], str] | None = None, parent=None):
        super().__init__(parent)
        self._factory, self._icon_px = factory, icon_px
        self._color = color or (lambda: theme.TEXT)
        self.setProperty("variant", "icon")
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.refresh_icon()

    def refresh_icon(self):
        self.setIcon(QIcon(self._factory(self._color())))
        self.setIconSize(QSize(self._icon_px, self._icon_px))

    def changeEvent(self, e):
        if e.type() in (e.Type.PaletteChange, e.Type.StyleChange):
            self.refresh_icon()
        super().changeEvent(e)


class Toast(QFrame):
    """A short confirmation that slides up, waits, and fades away."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        self._text = QLabel()
        self._text.setObjectName("ToastText")
        self._text.setFont(theme.ui_font(13, weight=600))
        lay.addWidget(self._text)
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._fade = QPropertyAnimation(self._fx, b"opacity", self)
        self._fade.setDuration(200)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._out)
        self.hide()

    def show_text(self, text: str, ms: int = 2600):
        self._text.setText(text)
        self.adjustSize()
        self.show(); self.raise_()
        self._fade.stop()
        self._fade.setStartValue(self._fx.opacity())
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.start()
        self._timer.start(ms)

    def text(self) -> str:
        return self._text.text()

    def _out(self):
        self._fade.stop()
        self._fade.setStartValue(self._fx.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        QTimer.singleShot(220, lambda: self.hide() if self._fx.opacity() < 0.05 else None)


class Collapsible(QWidget):
    """'More options ▸' — a header that shows/hides a body, collapsed by default."""

    toggled = Signal(bool)

    def __init__(self, title: str, body: QWidget, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._title = title
        self.head = QPushButton()
        self.head.setProperty("variant", "row")
        self.head.setCursor(Qt.CursorShape.PointingHandCursor)
        self.head.setFixedHeight(30)
        self.head.clicked.connect(lambda: self.set_open(not self.body.isVisible()))
        self.body = body
        lay.addWidget(self.head)
        lay.addWidget(body)
        self.set_open(False)

    def set_open(self, on: bool):
        self.body.setVisible(on)
        self.head.setText(("▾  " if on else "▸  ") + self._title)
        self.head.setAccessibleName(self._title)
        self.toggled.emit(on)


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


def icon_search(color: str | None = None, size: int = 16) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color or theme.MUTED, 1.7))
    p.drawEllipse(QPointF(size * 0.43, size * 0.43), size * 0.30, size * 0.30)
    p.drawLine(QPointF(size * 0.65, size * 0.65), QPointF(size * 0.88, size * 0.88))
    p.end()
    return pm


def icon_locate(color: str | None = None, size: int = 18) -> QPixmap:
    """A navigation arrow: 'show me where the iPhone is'."""
    pm, p = _canvas(size)
    s = size
    path = QPainterPath()
    path.moveTo(s * 0.86, s * 0.14)
    path.lineTo(s * 0.52, s * 0.88)
    path.lineTo(s * 0.44, s * 0.56)
    path.lineTo(s * 0.12, s * 0.48)
    path.closeSubpath()
    p.setPen(_pen(color or theme.TEXT, 1.6))
    p.drawPath(path)
    p.end()
    return pm


def icon_menu(color: str | None = None, size: int = 18) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color or theme.TEXT, 1.7))
    for y in (0.3, 0.5, 0.7):
        p.drawLine(QPointF(size * 0.2, size * y), QPointF(size * 0.8, size * y))
    p.end()
    return pm


def icon_plus(color: str | None = None, size: int = 16, minus: bool = False) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color or theme.TEXT, 1.8))
    p.drawLine(QPointF(size * 0.2, size * 0.5), QPointF(size * 0.8, size * 0.5))
    if not minus:
        p.drawLine(QPointF(size * 0.5, size * 0.2), QPointF(size * 0.5, size * 0.8))
    p.end()
    return pm


def icon_minus(color: str | None = None, size: int = 16) -> QPixmap:
    return icon_plus(color, size, minus=True)


def icon_layers(color: str | None = None, size: int = 18) -> QPixmap:
    """Stacked diamonds: map style."""
    pm, p = _canvas(size)
    p.setPen(_pen(color or theme.TEXT, 1.5))
    s = size
    for dy in (0.0, 0.18):
        path = QPainterPath()
        path.moveTo(s * 0.5, s * (0.2 + dy))
        path.lineTo(s * 0.86, s * (0.4 + dy))
        path.lineTo(s * 0.5, s * (0.6 + dy))
        path.lineTo(s * 0.14, s * (0.4 + dy))
        path.closeSubpath()
        p.drawPath(path)
    p.end()
    return pm


def icon_panel(color: str | None = None, size: int = 18) -> QPixmap:
    """A window with a right-hand sidebar: show/hide the control panel."""
    pm, p = _canvas(size)
    p.setPen(_pen(color or theme.TEXT, 1.5))
    r = QRectF(size * 0.14, size * 0.2, size * 0.72, size * 0.6)
    p.drawRoundedRect(r, 2.5, 2.5)
    p.drawLine(QPointF(size * 0.6, size * 0.2), QPointF(size * 0.6, size * 0.8))
    p.end()
    return pm


def icon_undo(color: str | None = None, size: int = 16) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color or theme.TEXT, 1.6))
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


def icon_close(color: str | None = None, size: int = 14) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color or theme.MUTED, 1.6))
    p.drawLine(QPointF(size * 0.25, size * 0.25), QPointF(size * 0.75, size * 0.75))
    p.drawLine(QPointF(size * 0.75, size * 0.25), QPointF(size * 0.25, size * 0.75))
    p.end()
    return pm


def icon_star(color: str | None = None, size: int = 16, filled: bool = False) -> QPixmap:
    import math
    pm, p = _canvas(size)
    c = QColor(color or theme.TEXT)
    p.setPen(_pen(c.name(), 1.4))
    p.setBrush(c if filled else Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    cx, cy, R, r = size / 2, size / 2 + 0.5, size * 0.42, size * 0.18
    for i in range(10):
        a = math.radians(-90 + i * 36)
        rad = R if i % 2 == 0 else r
        pt = QPointF(cx + rad * math.cos(a), cy + rad * math.sin(a))
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    path.closeSubpath()
    p.drawPath(path)
    p.end()
    return pm


def icon_drag(color: str | None = None, size: int = 14) -> QPixmap:
    """Six dots: a drag handle."""
    pm, p = _canvas(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color or theme.FAINT))
    for x in (0.36, 0.64):
        for y in (0.25, 0.5, 0.75):
            p.drawEllipse(QPointF(size * x, size * y), 1.3, 1.3)
    p.end()
    return pm


def icon_arrow(angle_deg: float, color: str | None = None, size: int = 16) -> QPixmap:
    """A chevron pointing at `angle_deg` (0 = up, clockwise), for the walk pad."""
    pm, p = _canvas(size)
    p.translate(size / 2, size / 2)
    p.rotate(angle_deg)
    p.setPen(_pen(color or theme.TEXT, 1.9))
    p.drawLine(QPointF(-size * 0.24, size * 0.10), QPointF(0, -size * 0.16))
    p.drawLine(QPointF(0, -size * 0.16), QPointF(size * 0.24, size * 0.10))
    p.end()
    return pm


def icon_phone(color: str | None = None, size: int = 22) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(_pen(color or theme.TEXT, 1.6))
    p.drawRoundedRect(QRectF(size * 0.28, size * 0.08, size * 0.44, size * 0.84), 3.5, 3.5)
    p.drawLine(QPointF(size * 0.44, size * 0.8), QPointF(size * 0.56, size * 0.8))
    p.end()
    return pm
