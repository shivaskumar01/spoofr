"""Window-level behaviour around a route that is waiting for its phone."""

from __future__ import annotations

import pytest

from qtui import store, theme
from qtui.bridge import DeviceBridge


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


def _suspend(win):
    """Put the window in the state of a route waiting for the phone."""
    win.panel._playing = True
    win.panel._route_offline = True
    win.panel.points = [(37.788, -122.407)]
    return win


class TestASuspendedRouteIsNotThrownAway:
    def test_device_lost_keeps_the_route_and_its_waypoints(self, win):
        _suspend(win)
        win.panel._redraw_waypoints()
        win._on_device_lost()
        assert win.panel.route_is_playing() is True
        assert win.panel.points, "waypoints were cleared out from under the route"
        assert win.panel._wp_ovs, "waypoint markers were removed"
        assert "paused" in win.hint.text().lower()
        assert win.status.text() == "Waiting for your iPhone"

    def test_device_lost_still_clears_up_when_no_route_is_waiting(self, win):
        win.panel._playing = False
        win.panel._route_offline = False
        win.panel._on_click(37.788, -122.407)
        win._on_device_lost()
        assert win.status.text() == "Disconnected"

    def test_the_phone_reappearing_reconnects_on_its_own(self, win, monkeypatch):
        """The user should not have to click Connect to finish a walk."""
        _suspend(win)
        started = []
        monkeypatch.setattr(win, "_start_connect", lambda: started.append(True))
        win._on_visible("USB")
        assert started == [True]
        assert "reconnecting" in win.status.text().lower()

    def test_no_auto_connect_when_nothing_is_waiting(self, win, monkeypatch):
        started = []
        monkeypatch.setattr(win, "_start_connect", lambda: started.append(True))
        win._on_visible("USB")
        assert started == []
        assert "Ready" in win.status.text()

    def test_reconnecting_does_not_yank_the_phone_backwards(self, win):
        """The route's next fix is the right position; re-applying an older
        saved spoof would drag the phone back to where it started."""
        _suspend(win)
        win.settings["active_spoof"] = {"lat": 1.0, "lon": 2.0}
        restored = []
        win.panel.restore_active_spoof = lambda la, lo: restored.append((la, lo))

        class Dev:
            name, ios, link, wireless_on = "iPhone", "26.6", "USB", True

        win._on_connected(Dev())
        assert restored == [], "re-asserted a stale position over a live route"
        assert "picks up where it left off" in win.hint.text()
