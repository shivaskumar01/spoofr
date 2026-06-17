"""One-time Developer Mode onboarding dialog.

Apple requires Developer Mode to set a device's location. When core.connect()
raises DeveloperModeRequired, the window opens this dialog: it surfaces the
(hidden) toggle on the phone, shows the steps, and polls until the user turns it
on, then auto-accepts so the caller reconnects.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QFrame, QLabel, QPushButton, QVBoxLayout,
)

from . import theme  # `core` is imported lazily in the worker threads (heavy: pmd3)

STEPS = (
    "1.   On your iPhone:  Settings  ▸  Privacy & Security",
    "2.   Scroll to  Developer Mode  and turn it On",
    "3.   Tap  Restart  when prompted",
    "4.   After reboot, unlock and tap  Turn On  (enter passcode)",
)


class DevModeWizard(QDialog):
    _ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Enable Developer Mode")
        self.setFixedSize(560, 470)
        self.setModal(True)
        self._polling = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 24, 26, 22)
        lay.setSpacing(0)

        title = QLabel("One-time setup: Developer Mode")
        title.setFont(theme.ui_font(20, weight=700))
        lay.addWidget(title)
        lay.addSpacing(6)

        blurb = QLabel("Apple requires Developer Mode to set your iPhone’s location. "
                       "It’s free, reversible anytime, and takes about 30 seconds.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {theme.MUTED};")
        lay.addWidget(blurb)
        lay.addSpacing(16)

        box = QFrame()
        box.setObjectName("Card")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(18, 12, 18, 12)
        bl.setSpacing(10)
        for s in STEPS:
            row = QLabel(s)
            row.setWordWrap(True)
            row.setFont(theme.ui_font(14))
            bl.addWidget(row)
        lay.addWidget(box)
        lay.addSpacing(16)

        self._status = QLabel("●  Waiting for Developer Mode…")
        self._status.setFont(theme.ui_font(13, weight=600))
        self._status.setStyleSheet(f"color: {theme.AMBER};")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._status)
        lay.addStretch(1)

        cancel = QPushButton("Cancel")
        cancel.setProperty("variant", "soft")
        cancel.setFixedSize(110, 34)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        lay.addWidget(cancel, alignment=Qt.AlignmentFlag.AlignCenter)

        self._ready.connect(self._on_ready)
        # surface the hidden toggle on the phone, then poll for it
        threading.Thread(target=self._reveal, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()

    def _reveal(self):
        try:
            import core
            core.reveal_developer_mode()
        except Exception:
            pass

    def _poll(self):
        import core
        while self._polling:
            try:
                on = core.developer_mode_status()
            except Exception:
                on = None
            if on:
                self._ready.emit()
                return
            time.sleep(2.0)

    def _on_ready(self):
        self._polling = False
        self._status.setText("✓  Developer Mode on, connecting…")
        self._status.setStyleSheet(f"color: {theme.GREEN};")
        QTimer.singleShot(900, self.accept)

    def reject(self):
        self._polling = False
        super().reject()

    def closeEvent(self, e):
        self._polling = False
        super().closeEvent(e)
