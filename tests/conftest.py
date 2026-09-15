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


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    """Keep the suite out of the user's real ~/.spoofr/spoofr.log.

    core._log is applog.log, and several tests drive failure paths that log, so
    without this a test run scribbles fake-device errors into the file you read
    when something actually goes wrong.
    """
    import applog
    monkeypatch.setattr(applog, "PATH", tmp_path / "spoofr.log")
