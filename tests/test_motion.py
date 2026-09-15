"""Walk and route workers must stop when the phone stops answering.

The old workers swallowed every set() failure, so the live marker kept gliding
across the map while the iPhone sat still.
"""

from __future__ import annotations

import threading
import time

import pytest

from qtui.bridge import DeviceBridge
from qtui.mapview import MapPanel
from tests.test_bridge import FakeDevice


@pytest.fixture
def panel(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(DeviceBridge, "_reconnect_worker", lambda self, gen: None)
    monkeypatch.setattr("qtui.store._path", lambda: tmp_path / "settings.json")
    bridge = DeviceBridge()
    p = MapPanel(bridge, {})
    yield p
    p._closing = True


def test_route_stops_when_the_session_dies(panel):
    panel.bridge.device = FakeDevice(error=RuntimeError("Connection closed"))
    done = []
    panel._routeDone.connect(done.append)
    panel.speed = 100.0
    panel._route_gen += 1
    panel._playing = True
    panel._route_worker([(0.0, 0.0), (0.0, 0.01)], panel._route_gen)
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()          # deliver the queued signal
    assert done and "Lost the iPhone" in done[0]
    assert panel.bridge.is_reconnecting()


def test_route_walks_every_fix_while_the_device_is_healthy(panel):
    dev = FakeDevice()
    panel.bridge.device = dev
    panel.speed = 500.0                   # one hop: ~1113 m at 500 m/s over 1 s ticks
    panel._route_gen += 1
    gen = panel._route_gen

    t = threading.Thread(target=panel._route_worker,
                         args=([(0.0, 0.0), (0.0, 0.01)], gen), daemon=True)
    panel._playing = True
    t.start()
    time.sleep(0.4)
    panel.stop_route()                    # invalidates gen; worker returns promptly
    t.join(2.0)
    assert not t.is_alive()
    assert dev.sets, "no fixes reached the device"
    assert dev.sets[0] == (0.0, 0.0)


def test_walk_stops_when_the_session_dies(panel):
    panel.bridge.device = FakeDevice(error=RuntimeError("Channel is closed"))
    panel._walk_pos = (10.0, 10.0)
    panel._walk_vec = (1.0, 0.0)
    panel._walking = True
    t = threading.Thread(target=panel._walk_worker, daemon=True)
    t.start()
    t.join(2.0)
    assert not t.is_alive(), "the walk worker kept going against a dead session"
    assert panel._walking is False
    assert panel.bridge.is_reconnecting()


def test_stop_motion_suspends_the_device(panel):
    dev = FakeDevice()
    panel.bridge.device = dev
    panel.stop_motion()
    assert dev.suspends == 1


def test_heartbeat_point_is_none_while_something_else_writes(panel):
    panel._active_spoof = (1.0, 2.0)
    panel._jitter_on = False
    assert panel.heartbeat_point() == (1.0, 2.0)
    for attr in ("_walking", "_playing", "_jitter_on"):
        setattr(panel, attr, True)
        assert panel.heartbeat_point() is None
        setattr(panel, attr, False)
    panel._active_spoof = None
    assert panel.heartbeat_point() is None
