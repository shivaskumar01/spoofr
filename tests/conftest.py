"""Shared test setup: headless Qt and an importable project root."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Qt widgets must never try to open a window during a test run.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session (Qt allows only one)."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
