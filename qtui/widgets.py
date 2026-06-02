"""Small reusable native controls Qt doesn't ship: a segmented control and a
painted toggle switch. Both match the app's dark theme.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QPropertyAnimation, Property, QRectF
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QPushButton,
)

from . import theme


def _blend(a: str, b: str, t: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(
        int(ca.red() + (cb.red() - ca.red()) * t),
        int(ca.green() + (cb.green() - ca.green()) * t),
        int(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


class Segmented(QFrame):
    """A pill of mutually-exclusive options (Teleport/Route, Walk/Run/…)."""

    changed = Signal(str)

    def __init__(self, values, height: int = 34, font_pt: int = 13, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setStyleSheet(f"QFrame {{ background: {theme.ELEV}; border-radius: {height // 2}px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns: dict[str, QPushButton] = {}
        for v in values:
            b = QPushButton(v)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(height - 6)
            f = b.font(); f.setPointSize(font_pt); f.setBold(True); b.setFont(f)
            b.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {theme.MUTED}; border: none;"
                f" border-radius: {(height - 6) // 2}px; padding: 0 14px; }}"
                f"QPushButton:checked {{ background: {theme.BLUE}; color: #ffffff; }}"
                f"QPushButton:hover:!checked {{ color: {theme.TEXT}; }}")
            self._group.addButton(b)
            lay.addWidget(b)
            self._btns[v] = b
            b.clicked.connect(lambda _=False, val=v: self.changed.emit(val))
        if values:
            self._btns[values[0]].setChecked(True)

    def set_value(self, v: str):
        if v in self._btns:
            self._btns[v].setChecked(True)

    def value(self) -> str:
        b = self._group.checkedButton()
        return b.text() if b else ""


class Switch(QAbstractButton):
    """A compact animated on/off switch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._t = 0.0
        self._anim = QPropertyAnimation(self, b"t", self)
        self._anim.setDuration(130)
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
        m = 3
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
        self.setChecked(on)
        self._t = 1.0 if on else 0.0
        self.update()
