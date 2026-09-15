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


class TestRouteFeedback:
    """A running route has to be visibly running.

    At walking pace the marker moves ~1.4 m per fix, and the map used to recentre
    on every one, pinning it to the middle of the screen — so a working route was
    indistinguishable from Start doing nothing at all.
    """

    def _armed(self, panel):
        from tests.test_bridge import FakeDevice
        panel.bridge.device = FakeDevice()
        panel.set_snap(False)                       # no network in tests
        panel.set_mode("route")                     # clicks drop waypoints, not pins
        panel._on_click(37.7749, -122.4194)
        panel._on_click(37.7760, -122.4180)
        assert len(panel.points) == 2
        return panel

    def test_start_announces_itself(self, panel, qapp):
        from PySide6.QtWidgets import QApplication
        self._armed(panel)
        states = []
        panel.playingChanged.connect(states.append)
        panel.start_route()
        QApplication.processEvents()
        assert states == [True]
        panel.stop_route()
        assert states == [True, False]

    def test_progress_draws_the_travelled_track_and_an_eta(self, panel, qapp):
        from PySide6.QtWidgets import QApplication
        self._armed(panel)
        hints = []
        panel.hint.connect(hints.append)
        panel._on_route_progress(25, 100, 37.775, -122.418)
        QApplication.processEvents()
        assert panel._travel_ov is not None, "no travelled track drawn"
        assert panel._travelled == [(37.775, -122.418)]
        assert any("25%" in h and "to go" in h for h in hints), hints

    def test_the_travelled_track_is_cleared_between_runs(self, panel, qapp):
        self._armed(panel)
        panel._on_route_progress(1, 10, 37.775, -122.418)
        assert panel._travelled
        panel.start_route()
        assert panel._travelled == [], "last run's track leaked into the new one"
        panel.stop_route()
        panel.clear_route()
        assert panel._travel_ov is None

    def test_following_does_not_pin_the_marker_to_the_centre(self, panel, qapp):
        panel.map.set_view(37.7749, -122.4194, 15)
        panel.map.resize(800, 600)
        centre = panel.map.center()
        panel._following = True
        # a fix a few metres away must not move the view
        panel._on_walk_step(37.77495, -122.41935)
        assert panel.map.center() == centre, "recentred on a tiny step"
        # one far outside the view must
        panel._on_walk_step(37.9, -122.2)
        assert panel.map.center() != centre


class TestRouteStartsFromThePhone:
    """A destination should be one click.

    The phone is already somewhere, so the start is not something you should have
    to place. Requiring two waypoints also meant the first fix jumped the phone to
    waypoint 1 — which is what read as "it just teleports me there".
    """

    SF = (37.7749, -122.4194)
    DEST = (37.7880, -122.4074)

    def _route_mode(self, panel, origin=SF):
        from tests.test_bridge import FakeDevice
        panel.bridge.device = FakeDevice()
        panel.set_snap(False)
        panel.set_mode("route")
        panel._active_spoof = origin
        return panel

    def test_one_waypoint_is_enough(self, panel):
        self._route_mode(panel)
        panel._on_click(*self.DEST)
        pts, from_here = panel.route_plan()
        assert len(panel.points) == 1
        assert len(pts) == 2 and from_here is True

    def test_the_route_begins_where_the_phone_is(self, panel):
        """No jump to waypoint 1."""
        self._route_mode(panel)
        panel._on_click(*self.DEST)
        pts, _ = panel.route_plan()
        assert pts[0] == self.SF
        assert pts[-1] == self.DEST

    def test_a_live_marker_counts_as_the_start_when_nothing_is_spoofed(self, panel):
        self._route_mode(panel, origin=None)
        panel._set_live(*self.SF)
        panel._on_click(*self.DEST)
        pts, from_here = panel.route_plan()
        assert from_here is True and pts[0] == self.SF

    def test_extra_waypoints_are_stops_not_a_start(self, panel):
        self._route_mode(panel)
        for p in [(37.780, -122.415), (37.785, -122.410), self.DEST]:
            panel._on_click(*p)
        pts, from_here = panel.route_plan()
        assert from_here is True
        assert len(pts) == 4 and pts[0] == self.SF and pts[-1] == self.DEST

    def test_a_far_away_phone_is_not_dragged_across_the_world(self, panel):
        """Routing Paris -> a pin in San Francisco is never what the click meant."""
        self._route_mode(panel, origin=(48.8566, 2.3522))
        panel._on_click(*self.DEST)
        pts, from_here = panel.route_plan()
        assert from_here is False and pts == [self.DEST]

    def test_an_imported_track_keeps_its_own_start(self, panel):
        self._route_mode(panel)
        track = [(37.80, -122.40), (37.81, -122.39), (37.82, -122.38)]
        panel.load_route(track)
        pts, from_here = panel.route_plan()
        assert from_here is False and pts == track

    def test_dropping_a_waypoint_makes_it_a_route_again(self, panel):
        self._route_mode(panel)
        panel.load_route([(37.80, -122.40), (37.81, -122.39)])
        assert panel._route_is_track is True
        panel._on_click(*self.DEST)
        assert panel._route_is_track is False, "hand-placed waypoints are not a track"

    def test_the_last_waypoint_is_the_destination(self, panel):
        self._route_mode(panel)
        panel._on_click(*self.DEST)
        assert len(panel._wp_ovs) == 1
        panel._on_click(37.79, -122.40)
        assert len(panel._wp_ovs) == 2, "markers renumbered as stops are added"

    def test_a_dense_track_only_marks_its_ends(self, panel):
        self._route_mode(panel)
        panel.load_route([(37.7 + i * 0.001, -122.4) for i in range(200)])
        assert len(panel._wp_ovs) == 2, "200 markers would bury the map"

    @pytest.mark.parametrize("origin, points, expected", [
        ((37.7749, -122.4194), [], "drop a destination"),
        (None, [(37.788, -122.407)], "starting point"),
        ((48.8566, 2.3522), [(37.788, -122.407)], "km from that pin"),
        ((37.7749, -122.4194), [(37.7749, -122.4194)], "already is"),
    ])
    def test_each_refusal_explains_itself(self, panel, origin, points, expected):
        self._route_mode(panel, origin=origin)
        panel.points = list(points)
        assert expected in panel._why_no_route()

    def test_start_refuses_without_a_destination(self, panel, qapp):
        self._route_mode(panel)
        panel.start_route()
        assert panel._playing is False
