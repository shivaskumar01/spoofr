"""Connecting should survive the hiccups that are normal when a phone is plugged in.

Most connect failures are transient and self-healing: a phone that has just been
plugged in has not finished enumerating, or the tunnel daemon has not discovered
it yet. Every one of those used to become a modal error the user dismissed and
retried by hand, which is most of what made connecting feel unreliable.
"""

from __future__ import annotations

import pytest

import core
import portable
from qtui.bridge import DeviceBridge


class FakePhone:
    name, ios, link, serial, wireless_on = "iPhone", "27.0", "USB", "abc", True

    def set(self, lat, lon): pass
    def clear(self): pass
    def suspend(self): pass
    def close(self, clear=True): pass


@pytest.fixture
def bridge(qapp, monkeypatch):
    monkeypatch.setattr(DeviceBridge, "CONNECT_BACKOFF", 0.01)
    monkeypatch.setattr(DeviceBridge, "_start_monitor", lambda self: None)
    monkeypatch.setattr(portable, "ensure_tunnel", lambda: None)
    monkeypatch.setattr(portable, "restart_tunnel", lambda: None)
    b = DeviceBridge()
    b.connected.connect(lambda d: b.__dict__.setdefault("_seen", []).append(d))
    return b


def _run(bridge):
    """Drive the worker synchronously so assertions are deterministic."""
    from PySide6.QtWidgets import QApplication
    bridge._connecting = True
    bridge._connect_worker()
    QApplication.processEvents()


def test_a_clean_connect_reports_once(bridge, monkeypatch):
    phone = FakePhone()
    monkeypatch.setattr(core, "connect", lambda **k: phone)
    failures = []
    bridge.failed.connect(failures.append)
    _run(bridge)
    assert bridge.device is phone
    assert failures == []


def test_a_phone_still_enumerating_is_retried_not_reported(bridge, monkeypatch):
    """Plugging in and hitting Connect immediately is the common case."""
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) < 3:
            raise core.NoDeviceFound("No iPhone reachable.")
        return FakePhone()

    monkeypatch.setattr(core, "connect", flaky)
    failures = []
    bridge.failed.connect(failures.append)
    _run(bridge)
    assert len(calls) == 3
    assert bridge.device is not None
    assert failures == [], "reported a hiccup it recovered from"


def test_a_daemon_that_will_not_tunnel_gets_replaced(bridge, monkeypatch):
    """Waiting longer never fixes this one; a fresh daemon does."""
    restarts = []
    monkeypatch.setattr(portable, "restart_tunnel", lambda: restarts.append(1))
    calls = []

    def not_ready(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise core.TunnelNotReady("no tunnel for this iPhone")
        return FakePhone()

    monkeypatch.setattr(core, "connect", not_ready)
    _run(bridge)
    assert restarts == [1], "did not restart the tunnel daemon"
    assert bridge.device is not None


def test_the_daemon_is_only_replaced_once(bridge, monkeypatch):
    """Three admin prompts in a row would be worse than the failure."""
    restarts = []
    monkeypatch.setattr(portable, "restart_tunnel", lambda: restarts.append(1))
    monkeypatch.setattr(core, "connect",
                        lambda **k: (_ for _ in ()).throw(core.TunnelNotReady("nope")))
    failures = []
    bridge.failed.connect(failures.append)
    _run(bridge)
    assert restarts == [1]
    assert len(failures) == 1


def test_developer_mode_asks_the_user_instead_of_retrying(bridge, monkeypatch):
    """Retrying cannot help; the phone needs a setting changed."""
    calls = []

    def needs_dev_mode(**kw):
        calls.append(1)
        raise core.DeveloperModeRequired("off")

    monkeypatch.setattr(core, "connect", needs_dev_mode)
    asked, failures = [], []
    bridge.devModeRequired.connect(lambda: asked.append(1))
    bridge.failed.connect(failures.append)
    _run(bridge)
    assert calls == [1], "retried something only the user can fix"
    assert asked == [1] and failures == []


def test_a_cancelled_password_prompt_is_taken_at_face_value(bridge, monkeypatch):
    calls = []

    def cancelled():
        calls.append(1)
        raise PermissionError("Admin password cancelled, the Wi-Fi tunnel needs it once.")

    monkeypatch.setattr(portable, "ensure_tunnel", cancelled)
    failures = []
    bridge.failed.connect(failures.append)
    _run(bridge)
    assert calls == [1], "re-prompted after the user said no"
    assert len(failures) == 1


def test_persistent_failure_is_reported_once_with_the_real_reason(bridge, monkeypatch):
    monkeypatch.setattr(core, "connect",
                        lambda **k: (_ for _ in ()).throw(core.SpooferError("mount timed out")))
    failures = []
    bridge.failed.connect(failures.append)
    _run(bridge)
    assert len(failures) == 1
    assert "mount timed out" in failures[0]
    assert bridge.device is None
