"""iPhone (portable) mode, hand control to the phone over Wi-Fi.

PortableController owns the shared portable.Portable server on a worker thread and
reports its URL + live phone status via signals. PortableView is the centred card
(QR + status) the user sees when they pick the “iPhone” tab.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from . import theme
from .widgets import Dot, button


class PortableController(QObject):
    qrReady = Signal(str)            # url
    statusUpdate = Signal(object)    # status dict | None
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._portable = None
        self._poll = False

    def start(self):
        if self._poll:
            return
        self._poll = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        # bind the server to a local (srv, not `server`: that is a module here):
        # stop() nulls self._portable underneath us, and reaching through the
        # attribute would blow up mid-poll
        import portable
        srv = portable.Portable()
        self._portable = srv
        try:
            url = srv.start()                   # may prompt for the admin password once
        except Exception as e:
            self._poll = False
            self.failed.emit(str(e))
            return
        if not self._poll:                      # stopped while the server was starting
            threading.Thread(target=srv.stop, daemon=True).start()
            return
        self.qrReady.emit(url)
        while self._poll:
            if not srv.alive():
                if self._poll:                  # a stop() of our own isn't a failure
                    self.failed.emit("Phone server stopped, switch to This Mac and back to retry.")
                return
            self.statusUpdate.emit(srv.status())
            time.sleep(2.0)

    def qr_png(self):
        return self._portable.qr_png() if self._portable else None

    def url(self):
        return self._portable.url if self._portable else None

    def stop(self):
        self._poll = False
        p, self._portable = self._portable, None
        if p:
            threading.Thread(target=p.stop, daemon=True).start()


class PortableView(QFrame):
    """The QR card, shown over the map while the phone is in control."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QFrame()
        card.setObjectName("Card")
        card.setFixedWidth(420)
        c = QVBoxLayout(card)
        c.setContentsMargins(40, 34, 40, 30)
        c.setSpacing(0)
        c.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("Control from your iPhone")
        title.setFont(theme.ui_font(20, weight=700))
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        c.addWidget(title)
        c.addSpacing(6)

        blurb = QLabel("Point your iPhone’s Camera at this code.\n"
                       "The phone just needs to be on the same Wi-Fi.")
        blurb.setFont(theme.ui_font(13))
        blurb.setStyleSheet(f"color: {theme.MUTED};")
        blurb.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        c.addWidget(blurb)
        c.addSpacing(22)

        self.qr = QLabel("Starting…")
        self.qr.setFixedSize(232, 232)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setStyleSheet("color: #5b6780; background: #ffffff; border-radius: 16px;")
        self.qr.setAccessibleName("QR code to open Spoofr on your iPhone")
        c.addWidget(self.qr, alignment=Qt.AlignmentFlag.AlignHCenter)
        c.addSpacing(18)

        st = QHBoxLayout(); st.setSpacing(8)
        st.addStretch(1)
        self._dot = Dot(theme.AMBER, 8)
        self.status = QLabel("Starting the phone server…")
        self.status.setFont(theme.ui_font(13, weight=600))
        st.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        st.addWidget(self.status)
        st.addStretch(1)
        c.addLayout(st)
        c.addSpacing(16)

        link = QHBoxLayout(); link.setSpacing(8)
        self.url = QLabel("")
        self.url.setFont(theme.mono_font(11))
        self.url.setStyleSheet(f"color: {theme.MUTED};")
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_btn = button("Copy link", "soft", height=30)
        link.addWidget(self.url, 1)
        link.addWidget(self.copy_btn)
        c.addLayout(link)
        c.addSpacing(14)
        self.back_btn = button("Back to this Mac", "primary", height=36)
        c.addWidget(self.back_btn)

        outer.addWidget(card)

    def reset(self):
        self.qr.setPixmap(QPixmap()); self.qr.setText("Starting…")
        self.url.setText("")
        self.set_status("Starting the phone server…", theme.AMBER)

    def show_qr(self, png_path: str, url: str):
        if png_path:
            pix = QPixmap(png_path)
            if not pix.isNull():
                dpr = self.devicePixelRatioF() or 1.0
                side = round(216 * dpr)
                pix = pix.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.FastTransformation)   # crisp modules
                pix.setDevicePixelRatio(dpr)
                self.qr.setPixmap(pix)
        # the token makes the full URL long; show host:port, copy the whole thing
        short = (url or "").split("/?")[0].replace("http://", "")
        self.url.setText(short)
        self.url.setToolTip(url or "")

    def set_status(self, text: str, color: str):
        self.status.setText(text)
        self._dot.set_color(color)

    def paintEvent(self, e):
        p = QPainter(self)
        c = QColor(theme.BG)
        c.setAlpha(225)
        p.fillRect(self.rect(), c)
