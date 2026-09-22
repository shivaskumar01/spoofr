"""Behaviour pinned during the 2026-09 UI pass: controls that fought each other,
a mode switch that re-ran on a re-click, and the route preview/ETA."""

from __future__ import annotations

import threading
import time

import pytest

from qtui import route, store
from qtui.bridge import DeviceBridge
from qtui.mapview import MapPanel
from tests.test_bridge import FakeDevice

SF = (37.7749, -122.4194)
DEST = (37.7880, -122.4074)


@pytest.fixture
def panel(qapp, monkeypatch):
    monkeypatch.setattr(DeviceBridge, "_reconnect_worker", lambda self, gen: None)
    p = MapPanel(DeviceBridge(), {})
    p.set_snap(False)
    yield p
    p._closing = True
    p.stop_route()


@pytest.fixture
def win(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(DeviceBridge, "_reconnect_worker", lambda self, gen: None)
    monkeypatch.setattr(DeviceBridge, "start_visibility", lambda self: None)
    monkeypatch.setattr("qtui.app.MainWindow._install_macui", lambda self: None)
    from qtui.app import MainWindow
    w = MainWindow()
    yield w
    w.panel._closing = True
    w.panel.stop_route()


def _spin(seconds):
    from PySide6.QtWidgets import QApplication
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


class TestSegmented:
    def test_reclicking_the_selected_option_is_not_a_change(self, qapp):
        from qtui.widgets import Segmented
        seg = Segmented(["This Mac", "iPhone"])
        got = []
        seg.changed.connect(got.append)
        seg._btns["This Mac"].click()
        assert got == []
        seg._btns["iPhone"].click()
        seg._btns["iPhone"].click()
        assert got == ["iPhone"]

    def test_set_value_is_silent(self, qapp):
        from qtui.widgets import Segmented
        seg = Segmented(["A", "B"])
        got = []
        seg.changed.connect(got.append)
        seg.set_value("B")
        assert got == [] and seg.value() == "B"


class TestModeSwitch:
    def test_reclicking_this_mac_keeps_the_connected_button(self, win):
        """It used to reset Connect to 'Connect' while still connected."""
        win.bridge.device = FakeDevice()
        win._set_connect_state("disconnect")
        win.app_mode._btns["This Mac"].click()
        assert win.connect_btn.text() == "Disconnect"

    def test_a_connect_that_lands_in_iphone_mode_lets_go(self, win, monkeypatch):
        """The phone's server owns the device in iPhone mode."""
        dropped = []
        monkeypatch.setattr(win.portable, "start", lambda: None)
        monkeypatch.setattr(win.bridge, "drop_device", lambda: dropped.append(True))
        win.app_mode._btns["iPhone"].click()
        dropped.clear()
        win._on_connected(FakeDevice())
        assert dropped == [True]
        assert win.connect_btn.text() == "Connect"


class TestControlsDoNotFight:
    def _routing(self, panel):
        panel.bridge.device = FakeDevice()
        panel._active_spoof = SF
        panel.set_mode("route")
        panel._on_click(*DEST)
        panel.start_route()
        assert panel.route_is_playing()

    def test_teleporting_stops_a_running_route(self, panel):
        """Otherwise the route drags the phone straight back a second later."""
        self._routing(panel)
        panel.set_mode("teleport")
        panel._stage(40.0, -74.0)
        panel._commit()
        assert not panel.route_is_playing()

    def test_walking_is_refused_while_a_route_plays(self, panel):
        self._routing(panel)
        hints = []
        panel.hint.connect(hints.append)
        panel.key_walk("Up", True)
        assert panel._walking is False
        assert any("route is running" in h.lower() for h in hints)
        panel.key_walk("Up", False)

    def test_route_clicks_do_not_edit_a_running_route(self, panel):
        self._routing(panel)
        panel._on_click(37.79, -122.40)
        assert panel.points == [DEST]

    def test_the_walk_pad_steps_aside_in_route_mode(self, panel):
        panel.show_walk_pad(True)
        panel.set_mode("route")
        assert not panel.walk_pad.isVisibleTo(panel)
        panel.set_mode("teleport")
        assert panel.walk_pad.isVisibleTo(panel)


class TestRoutePreview:
    def test_the_plan_is_drawn_from_the_phone_with_an_estimate(self, panel):
        est = []
        panel.estimateChanged.connect(est.append)
        panel._active_spoof = SF
        panel.set_mode("route")
        panel._on_click(*DEST)
        assert panel._path_ov is not None
        assert panel._path_ov.pts[0] == SF, "the preview must start where the phone is"
        assert est and "mi" in est[-1] and "min" in est[-1]

    def test_undo_removes_the_last_stop(self, panel):
        panel._active_spoof = SF
        panel.set_mode("route")
        panel._on_click(37.78, -122.41)
        panel._on_click(*DEST)
        panel.undo_waypoint()
        assert panel.points == [(37.78, -122.41)]
        panel.undo_waypoint()
        assert panel.points == [] and panel._path_ov is None

    def test_start_plays_the_cached_snapped_path_without_asking_again(self, panel, monkeypatch):
        calls = []
        snapped = route.Snapped([SF, (37.78, -122.41), DEST], True, 2000.0, 1500.0)

        def fake_snap(pts, profile="walking", timeout=12.0):
            calls.append(profile)
            return snapped
        monkeypatch.setattr(route, "snap_to_roads", fake_snap)
        dev = FakeDevice()
        panel.bridge.device = dev
        panel._active_spoof = SF
        panel.set_mode("route")
        panel.snap = True
        panel._on_click(*DEST)
        panel._fetch_plan()                        # what the debounce timer does
        _spin(0.3)
        assert calls == ["walking"]
        assert panel._path_ov.pts == snapped.points
        panel.speed = 5000.0                       # finish in a couple of fixes
        panel.start_route()
        _spin(0.3)
        panel.stop_route()
        assert calls == ["walking"], "Start asked the router again"
        assert dev.sets and dev.sets[0] == SF

    def test_a_looping_route_does_not_grow_its_trail_forever(self, panel):
        panel._route_at = (0, 0)
        for i in range(1, 6):
            panel._on_route_progress(i, 5, 37.0 + i * 1e-4, -122.0)
        assert len(panel._travelled) == 5
        panel._on_route_progress(1, 5, 37.0, -122.0)    # came round again
        assert len(panel._travelled) == 1


def test_caffeinate_dies_with_the_app(panel):
    panel.hold_awake(True)
    try:
        args = panel._caffeinate.args
        assert "-w" in args, "a crash would leave the Mac unable to sleep"
    finally:
        panel.hold_awake(False)


def test_recents_get_a_name_later(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_path", lambda: tmp_path / "settings.json")
    from qtui.sidebar import Sidebar
    s = Sidebar({})
    s.add_recent(40.0, -74.0)
    s.name_recent(40.0, -74.0, "Somewhere, NYC")
    assert s.settings["recent"][0]["name"] == "Somewhere, NYC"
    assert s.recent_name(40.0, -74.0) == "Somewhere, NYC"
