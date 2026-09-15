"""The bridge's single write path: failures stop the caller and heal the session.

Before this, _walk_worker / _route_worker / _jitter_worker each did
`try: dev.set(...) except Exception: pass`, so the map kept animating against an
iPhone that had stopped moving minutes earlier.
"""

from __future__ import annotations

import pytest

import core
from qtui.bridge import DeviceBridge


class FakeDevice:
    def __init__(self, error=None):
        self.error, self.sets, self.suspends, self.cleared = error, [], 0, 0
        self.serial, self.link, self.name, self.ios = "abc123", "USB", "iPhone", "18.0"

    def set(self, lat, lon):
        if self.error:
            raise self.error
        self.sets.append((lat, lon))

    def clear(self):
        if self.error:
            raise self.error
        self.cleared += 1

    def suspend(self):
        self.suspends += 1

    closed_with_clear = None

    def close(self, clear=True):
        self.closed_with_clear = clear


@pytest.fixture
def bridge(qapp, monkeypatch):
    # never let a test actually dial the phone
    monkeypatch.setattr(DeviceBridge, "_reconnect_worker", lambda self, gen: None)
    return DeviceBridge()


def test_push_without_a_device_is_false():
    assert DeviceBridge().push(1.0, 2.0) is False


def test_push_delivers_the_fix(bridge):
    bridge.device = FakeDevice()
    assert bridge.push(37.0, -122.0) is True
    assert bridge.device.sets == [(37.0, -122.0)]


def test_push_failure_reports_and_starts_a_reconnect(bridge):
    hints = []
    bridge.hint.connect(hints.append)
    bridge.device = FakeDevice(error=RuntimeError("Connection closed"))
    assert bridge.push(1.0, 2.0) is False
    assert any("Lost the iPhone" in h for h in hints)
    assert bridge.is_reconnecting()
    assert bridge.device is None, "the dead session must be dropped"


def test_a_burst_of_failures_starts_exactly_one_reconnect(bridge):
    """Walk, route and jitter can all fail in the same tick."""
    dev = FakeDevice(error=RuntimeError("Channel is closed"))
    bridge.device = dev
    gen_before = bridge._reconnect_gen
    for _ in range(5):
        bridge.push(1.0, 2.0)
        bridge.device = bridge.device or dev      # simulate racing workers
    assert bridge._reconnect_gen - gen_before == 1


def test_quiet_push_stays_silent_but_still_heals(bridge):
    """The jitter worker and the heartbeat must not spam the hint bar."""
    hints = []
    bridge.hint.connect(hints.append)
    bridge.device = FakeDevice(error=RuntimeError("Connection closed"))
    assert bridge.push(1.0, 2.0, quiet=True) is False
    assert hints == []
    assert bridge.is_reconnecting()


def test_cancelled_push_is_not_a_session_failure(bridge):
    """A Restore/stop overtaking a fix must not trigger a reconnect."""
    bridge.device = FakeDevice(error=core.Cancelled("stopped"))
    assert bridge.push(1.0, 2.0) is False
    assert not bridge.is_reconnecting()
    assert bridge.device is not None


def test_suspend_reaches_the_device(bridge):
    bridge.device = FakeDevice()
    bridge.suspend()
    assert bridge.device.suspends == 1
    bridge.device = None
    bridge.suspend()          # no device: must not raise


def test_heartbeat_point_uses_the_registered_source(bridge):
    assert bridge._heartbeat_point() is None
    bridge.heartbeat_source = lambda: (1.0, 2.0)
    assert bridge._heartbeat_point() == (1.0, 2.0)
    bridge.heartbeat_source = lambda: 1 / 0     # a broken source must not kill the monitor
    assert bridge._heartbeat_point() is None
