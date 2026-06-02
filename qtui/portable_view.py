"""iPhone (portable) mode — hand control to the phone over Wi-Fi.

PortableController owns the shared portable.Portable server on a worker thread and
reports its URL + live phone status via signals. PortableView is the centred card
(QR + status) the user sees when they pick the “iPhone” tab.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout

from . import theme


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
        import portable
        self._portable = portable.Portable()
        try:
            url = self._portable.start()        # may prompt for the admin password once
        except Exception as e:
            self._poll = False
            self.failed.emit(str(e))
            return
        self.qrReady.emit(url)
        while self._poll:
            if not self._portable.alive():
                self.failed.emit("Phone server stopped — switch to This Mac and back to retry.")
                return
            self.statusUpdate.emit(self._portable.status())
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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QFrame()
        card.setObjectName("Card")
        card.setFixedWidth(440)
        c = QVBoxLayout(card)
        c.setContentsMargins(46, 30, 46, 30)
        c.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("Control from your iPhone")
        title.setFont(theme.ui_font(20, weight=700))
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        c.addWidget(title)

        blurb = QLabel("Open the Camera on your iPhone and point it at this code.\n"
                       "Your phone just needs to be on the same Wi-Fi.")
        blurb.setFont(theme.ui_font(13))
        blurb.setStyleSheet(f"color: {theme.MUTED};")
        blurb.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        c.addWidget(blurb)
        c.addSpacing(16)

        self.qr = QLabel("Starting…")
        self.qr.setFixedSize(248, 248)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setStyleSheet(f"color: {theme.MUTED}; background: #ffffff; border-radius: 12px;")
        c.addWidget(self.qr, alignment=Qt.AlignmentFlag.AlignHCenter)
        c.addSpacing(12)

        self.url = QLabel("")
        self.url.setFont(theme.ui_font(12))
        self.url.setStyleSheet(f"color: {theme.TEXT};")
        self.url.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.url.setWordWrap(True)
        c.addWidget(self.url)

        self.copy_btn = QPushButton("Copy link")
        self.copy_btn.setProperty("variant", "soft")
        self.copy_btn.setFixedHeight(32)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        c.addWidget(self.copy_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        c.addSpacing(10)

        self.status = QLabel("●  Starting the phone server…")
        self.status.setFont(theme.ui_font(13, weight=600))
        self.status.setStyleSheet(f"color: {theme.AMBER};")
        self.status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        c.addWidget(self.status)

        outer.addWidget(card)

    def reset(self):
        self.qr.setText("Starting…"); self.qr.setPixmap(QPixmap())
        self.url.setText("")
        self.set_status("●  Starting the phone server…", theme.AMBER)

    def show_qr(self, png_path: str, url: str):
        if png_path:
            pix = QPixmap(png_path)
            if not pix.isNull():
                self.qr.setPixmap(pix.scaled(
                    232, 232, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                self.qr.setText("")
        self.url.setText(url or "")

    def set_status(self, text: str, color: str):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
