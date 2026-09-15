"""The log is the only window into a .app that has no visible stderr."""

from __future__ import annotations

import threading

import pytest

import applog


@pytest.fixture
def logfile(tmp_path, monkeypatch):
    p = tmp_path / "spoofr.log"
    monkeypatch.setattr(applog, "PATH", p)
    return p


def test_log_appends_timestamped_lines(logfile):
    applog.log("first")
    applog.log("second")
    lines = logfile.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("first") and lines[1].endswith("second")


def test_log_rotates_instead_of_growing_forever(logfile, monkeypatch):
    monkeypatch.setattr(applog, "MAX_BYTES", 200)
    for i in range(40):
        applog.log(f"line {i} " + "x" * 40)
    assert logfile.with_name(logfile.name + ".1").exists()
    assert logfile.stat().st_size <= applog.MAX_BYTES + 200


def test_logging_never_raises_even_with_an_unwritable_path(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "PATH", tmp_path / "nope" / "\0bad" / "x.log")
    applog.log("this must not blow up")      # no exception == pass


def test_a_dying_worker_thread_reaches_the_log(logfile):
    applog.install_hooks()
    try:
        t = threading.Thread(target=lambda: 1 / 0, name="doomed-worker")
        t.start()
        t.join(2)
        text = logfile.read_text()
        assert "doomed-worker" in text and "ZeroDivisionError" in text
    finally:
        threading.excepthook = threading.__excepthook__
