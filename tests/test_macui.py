"""The menu-bar item: it must actually install (a PyObjC selector mistake once
made it fail silently), and it must show a running route's progress."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS menu bar")


class App:
    saved = [{"name": "Home", "lat": 1.0, "lon": 2.0}]
    state = {"route": {"name": "Park", "paused": False, "pct": 62, "left": 600, "running": True},
             "connected": True, "spoofing": True, "name": "iPhone"}

    def menu_state(self):
        return self.state

    def _post(self, fn):
        fn()


def test_it_installs_and_shows_route_progress():
    macui = pytest.importorskip("macui")
    from AppKit import NSApplication
    NSApplication.sharedApplication()
    c = macui.install(App())
    try:
        c.setProgress("62%")
        assert str(c._status_item.button().title()) == "◉ 62%"
        m = c._status_item.menu()
        titles = [str(m.itemAtIndex_(i).title()) for i in range(m.numberOfItems())]
        for want in ("Pause Route", "Stop Route…", "Stop Spoofing", "Open Spoofr", "Quit Spoofr"):
            assert want in titles, titles
    finally:
        c.teardown()
