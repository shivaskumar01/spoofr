"""The welcome screen and the device cards.

Shown over a dimmed map while no iPhone is connected: an illustration and one
line when nothing is plugged in; a card per phone (name, model, iOS, link, one
big Connect button) the moment one shows up. A phone that hasn't trusted this
Mac says "Unlock your iPhone and tap Trust" and moves on by itself once it has.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme
from .widgets import ElidedLabel, button, icon_phone, label, set_role

LINK_WORDS = {"USB": "Cable", "Wi-Fi": "Wi-Fi", "USB + Wi-Fi": "Cable + Wi-Fi"}


def link_word(link: str) -> str:
    return LINK_WORDS.get(link, link or "")


class Illustration(QWidget):
    """A phone and its cable, drawn in the theme's ink."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(180, 150)
        self.setAccessibleName("An iPhone connected to a cable")

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ink = QColor(theme.MUTED)
        accent = QColor(theme.ACCENT)
        # phone
        body = QRectF(58, 6, 64, 116)
        p.setPen(QPen(ink, 2.4))
        p.setBrush(QColor(theme.ELEV))
        p.drawRoundedRect(body, 12, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.PANEL))
        p.drawRoundedRect(body.adjusted(5, 9, -5, -9), 6, 6)
        # a location dot on its screen
        p.setBrush(accent)
        p.drawEllipse(QPointF(90, 58), 7, 7)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(90, 58), 2.8, 2.8)
        # notch
        p.setBrush(ink)
        p.drawRoundedRect(QRectF(80, 10, 20, 4), 2, 2)
        # cable: plug, then a relaxed curve off to the Mac
        p.setBrush(ink)
        p.drawRoundedRect(QRectF(84, 122, 12, 10), 2, 2)
        path = QPainterPath(QPointF(90, 132))
        path.cubicTo(QPointF(90, 150), QPointF(150, 118), QPointF(174, 140))
        pen = QPen(ink, 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class DeviceCard(QFrame):
    connectClicked = Signal(str)          # serial

    def __init__(self, card: dict):
        super().__init__()
        self.setObjectName("Group")
        self.serial = card["serial"]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        top = QHBoxLayout(); top.setSpacing(10)
        icon = QLabel(); icon.setPixmap(icon_phone(theme.TEXT, 26))
        top.addWidget(icon)
        col = QVBoxLayout(); col.setSpacing(1)
        self.name = ElidedLabel(); self.name.setFont(theme.ui_font(15, 700))
        self.detail = ElidedLabel(); self.detail.setFont(theme.ui_font(12))
        self.detail.setProperty("role", "muted")
        col.addWidget(self.name); col.addWidget(self.detail)
        top.addLayout(col, 1)
        lay.addLayout(top)
        self.state = label("", 12, 600, wrap=True)
        lay.addWidget(self.state)
        self.btn = button("Connect", "primary", height=40)
        self.btn.clicked.connect(lambda: self.connectClicked.emit(self.serial))
        lay.addWidget(self.btn)
        self.update_card(card)

    def update_card(self, c: dict, connecting: bool = False, error: str = ""):
        self.name.setText(c.get("name") or "iPhone")
        bits = [c.get("model") or "", f"iOS {c['ios']}" if c.get("ios") else "", link_word(c.get("link", ""))]
        self.detail.setText("  ·  ".join(b for b in bits if b))
        st = c.get("state", "unknown")
        text, role, enabled, label_ = "", "muted", True, "Connect"
        if connecting:
            text, enabled, label_ = "", False, "Connecting…"
        elif st == "trust":
            text, role, enabled = "Unlock your iPhone and tap Trust.", "warn", False
            label_ = "Waiting for Trust…"
        elif st == "trust_locked":
            text, role, enabled = "Unlock your iPhone so it can ask to trust this Mac.", "warn", False
            label_ = "Waiting for Trust…"
        elif st == "locked":
            text, role = "Your iPhone is locked. Unlock it, then connect.", "warn"
        elif st == "denied":
            text, role, enabled = ("Your iPhone said Don’t Trust. Unplug it, plug it back in, "
                                   "and tap Trust."), "bad", False
        elif st == "error" and c.get("error"):
            text, role = c["error"], "bad"
        if error:
            text, role = error, "bad"
        self.state.setText(text)
        set_role(self.state, role)
        self.state.setVisible(bool(text))
        self.btn.setEnabled(enabled)
        self.btn.setText(label_)


class WelcomeOverlay(QWidget):
    """Covers the map until a phone is connected (or the user wants to look around)."""

    connectClicked = Signal(str)
    browse = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("Scrim")
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card = QFrame()
        self.card.setObjectName("Card")
        self.card.setFixedWidth(440)
        c = QVBoxLayout(self.card)
        c.setContentsMargins(32, 28, 32, 22)
        c.setSpacing(12)
        self.art = Illustration()
        c.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = label("Connect your iPhone with a cable to get started.", 17, 700, wrap=True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        c.addWidget(self.title)
        self.sub = label("Spoofr notices it the moment it’s plugged in.", 12, role="muted", wrap=True)
        self.sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        c.addWidget(self.sub)
        self.cards_box = QVBoxLayout()
        self.cards_box.setSpacing(10)
        c.addLayout(self.cards_box)
        self.browse_btn = QPushButton("Look around the map first")
        self.browse_btn.setProperty("variant", "link")
        self.browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_btn.setFixedHeight(28)
        self.browse_btn.clicked.connect(self.browse.emit)
        c.addWidget(self.browse_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(self.card)
        self._cards: dict[str, DeviceCard] = {}
        self._connecting: str | None = None
        self._errors: dict[str, str] = {}

    def paintEvent(self, e):
        p = QPainter(self)
        c = QColor(theme.BG)
        c.setAlpha(205 if theme.is_dark() else 190)
        p.fillRect(self.rect(), c)

    def set_phones(self, cards: list[dict]):
        seen = set()
        for card in cards:
            s = card["serial"]
            seen.add(s)
            if s not in self._cards:
                w = DeviceCard(card)
                w.connectClicked.connect(self._clicked)
                self._cards[s] = w
                self.cards_box.addWidget(w)
            self._cards[s].update_card(card, connecting=(s == self._connecting),
                                       error=self._errors.get(s, ""))
        for s in list(self._cards):
            if s not in seen:
                self._cards.pop(s).deleteLater()
                self._errors.pop(s, None)
        has = bool(cards)
        self.art.setVisible(not has)
        self.title.setText("Choose your iPhone" if len(cards) > 1 else
                           ("Your iPhone is here" if has else
                            "Connect your iPhone with a cable to get started."))
        self.sub.setText("" if has else "Spoofr notices it the moment it’s plugged in.")
        self.sub.setVisible(not has)
        self.card.adjustSize()

    def _clicked(self, serial: str):
        self._errors.pop(serial, None)
        self.connectClicked.emit(serial)

    def set_connecting(self, serial: str | None):
        self._connecting = serial
        for s, w in self._cards.items():
            w.btn.setEnabled(serial is None)
            if s == serial:
                w.btn.setText("Connecting…")

    def set_error(self, serial: str | None, message: str):
        self._connecting = None
        if serial is None and len(self._cards) == 1:
            serial = next(iter(self._cards))
        if serial:
            self._errors[serial] = message
        for s, w in self._cards.items():
            w.btn.setEnabled(True)
            w.btn.setText("Try again" if s == serial else "Connect")
            if s == serial:
                w.state.setText(message)
                set_role(w.state, "bad")
                w.state.setVisible(True)
