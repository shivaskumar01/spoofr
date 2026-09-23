"""The app against the product brief, without a phone.

Each class is a section of the brief: connecting, the main screen, teleport,
routes, unplugging mid-route, reconnecting and resuming, stopping. A fake
device stands in for the iPhone; the route engine's clock maths is covered in
test_session.py, so these check the behaviour a person would see.
"""

from __future__ import annotations

import time

import pytest

import core
from qtui import route, store
from qtui.bridge import DeviceBridge
from qtui.session import Clock, Options, Plan, RouteSession, Tick
from tests.test_bridge import FakeDevice

SF = (37.7749, -122.4194)
DEST = (37.7880, -122.4074)


class Phone(FakeDevice):
    def __init__(self, udid="UDID-1", fresh_mount=False, wireless_on=True):
        super().__init__()
        self.udid, self.model, self.fresh_mount, self.wireless_on = \
            udid, "iPhone 17 Pro", fresh_mount, wireless_on
        self.name = "Test iPhone"


def _spin(seconds=0.05):
    from PySide6.QtWidgets import QApplication
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)


@pytest.fixture
def win(qapp, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(store, "_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(DeviceBridge, "_reconnect_worker", lambda self, gen: None)
    monkeypatch.setattr(DeviceBridge, "start_visibility", lambda self: None)
    monkeypatch.setattr(DeviceBridge, "locate", lambda self: None)
    monkeypatch.setattr("qtui.app.MainWindow._install_macui", lambda self: None)
    # the confirmations answer Yes (tests that care override this)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    from qtui.app import MainWindow
    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    _spin()
    yield w
    sc = w.mapscreen
    sc._closing = True
    if sc.runner is not None:
        sc.runner.stop()
    sc.hold_awake(False)


def connect(w, phone=None):
    phone = phone or Phone()
    w.bridge.device = phone
    w._on_connected(phone)
    return phone


def straight_route(w, dest=DEST, origin=SF):
    """Connected, spoofed at `origin`, a straight-line route to `dest` planned."""
    sc = w.mapscreen
    sc._set_spoof(origin)
    sc.drop_pin(*dest)
    sc.route_pin()
    sc.set_path_style("straight")
    return sc


# ---- §2 Connecting ---------------------------------------------------------

class TestConnecting:
    CARD = {"serial": "s1", "udid": "UDID-1", "name": "Test iPhone", "model": "iPhone 17 Pro",
            "ios": "26.3", "link": "USB", "state": "ready"}

    def test_first_launch_is_the_welcome_screen(self, win):
        sc = win.mapscreen
        assert sc.welcome.isVisibleTo(win)
        assert "Connect your iPhone with a cable" in sc.welcome.title.text()

    def test_a_plugged_in_phone_becomes_a_card(self, win):
        win._on_phones([self.CARD])
        cards = win.mapscreen.welcome._cards
        assert list(cards) == ["s1"]
        c = cards["s1"]
        assert c.name.text() == "Test iPhone"
        assert "iPhone 17 Pro" in c.detail.text() and "iOS 26.3" in c.detail.text()
        assert "Cable" in c.detail.text()
        assert c.btn.isEnabled() and c.btn.text() == "Connect"

    def test_an_untrusted_phone_says_what_to_do(self, win):
        win._on_phones([dict(self.CARD, state="trust")])
        c = win.mapscreen.welcome._cards["s1"]
        assert c.state.text() == "Unlock your iPhone and tap Trust."
        assert not c.btn.isEnabled()

    def test_one_card_per_phone(self, win):
        win._on_phones([self.CARD, dict(self.CARD, serial="s2", udid="UDID-2", name="Other")])
        assert set(win.mapscreen.welcome._cards) == {"s1", "s2"}
        assert "Choose" in win.mapscreen.welcome.title.text()

    def test_a_remembered_phone_connects_by_itself(self, win, monkeypatch):
        calls = []
        monkeypatch.setattr(win.bridge, "connect", lambda serial=None: calls.append(serial))
        win.settings["devices"] = {"UDID-1": {"name": "Test iPhone"}}
        win._on_phones([self.CARD])
        assert calls == ["s1"]

    def test_a_new_phone_waits_for_connect(self, win, monkeypatch):
        calls = []
        monkeypatch.setattr(win.bridge, "connect", lambda serial=None: calls.append(serial))
        win._on_phones([self.CARD])
        assert calls == []

    def test_auto_connect_can_be_turned_off(self, win, monkeypatch):
        calls = []
        monkeypatch.setattr(win.bridge, "connect", lambda serial=None: calls.append(serial))
        win.settings["devices"] = {"UDID-1": {}}
        win.settings["auto_connect"] = False
        win._on_phones([self.CARD])
        assert calls == []

    def test_connecting_remembers_the_phone(self, win):
        connect(win)
        assert "UDID-1" in win.settings["devices"]
        assert win.settings["last_device"] == "UDID-1"
        assert not win.mapscreen.welcome.isVisibleTo(win)

    def test_a_manual_disconnect_does_not_bounce_back(self, win, monkeypatch):
        connect(win)
        calls = []
        monkeypatch.setattr(win.bridge, "connect", lambda serial=None: calls.append(serial))
        win._disconnect(restore_first=False)
        win._on_phones([dict(self.CARD, serial="abc123")])
        assert calls == [], "reconnected straight after the user disconnected"

    def test_errors_are_sentences(self):
        from pymobiledevice3.exceptions import PasswordRequiredError, UserDeniedPairingError
        assert core.human_error(PasswordRequiredError()) == core.LOCKED_MSG
        assert "Don’t Trust" in core.human_error(UserDeniedPairingError())
        for raw in ("0xE8000015", "[Errno 61] Connection refused", "ConnectionTerminatedError()",
                    "", "kAMDMobileImageMounterDeviceLocked"):
            msg = core.human_error(RuntimeError(raw))
            assert "0x" not in msg and "Errno" not in msg and "Error(" not in msg, msg
            assert msg.endswith("."), msg
        assert core.human_error(RuntimeError("Your iPhone isn’t showing up.")) == \
            "Your iPhone isn’t showing up."

    def test_a_failed_connect_shows_on_the_card(self, win):
        win._on_phones([self.CARD])
        win._want_serial = "s1"
        win._on_failed("Your iPhone is locked. Unlock it and it will carry on.")
        c = win.mapscreen.welcome._cards["s1"]
        assert "locked" in c.state.text()
        assert c.btn.isEnabled()


class TestDiscovery:
    def test_trust_is_asked_once_per_plug_in(self, qapp, monkeypatch):
        b = DeviceBridge()
        asked = []
        monkeypatch.setattr(core, "probe", lambda serials=None: (
            [{"serial": "s1", "link": "USB", "state": "unknown"}] if serials == set()
            else [{"serial": "s1", "link": "USB", "state": "trust", "udid": "U"}]))
        monkeypatch.setattr(DeviceBridge, "_trust_worker",
                            lambda self, s: asked.append(s))
        b.discover()
        b.discover()
        assert asked == ["s1"], "asked for Trust more than once"

    def test_a_ready_phone_is_not_re_queried(self, qapp, monkeypatch):
        b = DeviceBridge()
        described = []

        def probe(serials=None):
            if serials == set():
                return [{"serial": "s1", "link": "USB", "state": "unknown"}]
            described.append(set(serials))
            return [{"serial": "s1", "link": "USB", "state": "ready", "udid": "U"}]
        monkeypatch.setattr(core, "probe", probe)
        b.discover(); b.discover(); b.discover()
        assert described == [{"s1"}]


# ---- §3 Main screen --------------------------------------------------------

class TestMainScreen:
    def test_the_pill_says_which_link(self, win):
        connect(win)
        assert win.mapscreen.pill.text.text() == "Test iPhone · Cable"
        win.mapscreen.device["link"] = "Wi-Fi"
        win.mapscreen._update_pill()
        assert win.mapscreen.pill.text.text().endswith("Wi-Fi")

    def test_a_pin_opens_its_card(self, win):
        connect(win)
        sc = win.mapscreen
        sc._set_spoof(SF)
        sc.drop_pin(*DEST, name="Somewhere")
        assert sc.panel.page == "pin"
        assert sc.panel.pin.title.text() == "Somewhere"
        assert "from your iPhone" in sc.panel.pin.distance.text()

    def test_one_pin_at_a_time(self, win):
        sc = win.mapscreen
        sc.drop_pin(*SF)
        sc.drop_pin(*DEST)
        assert sc.pin["lat"] == DEST[0]
        assert sum(1 for ov in sc.map._points if ov.draggable) == 1

    def test_dragging_the_pin_updates_the_card_live(self, win):
        sc = win.mapscreen
        sc.drop_pin(*SF)
        sc._on_marker_moved(sc._pin_ov, 37.8, -122.5)
        assert sc.panel.pin.coords.text() == "37.800000, -122.500000"

    def test_escape_dismisses_the_pin(self, win):
        sc = win.mapscreen
        sc.drop_pin(*SF)
        assert sc.escape() is True
        assert sc.pin is None and sc.panel.page == "home"

    def test_copy_coordinates(self, win, qapp):
        sc = win.mapscreen
        sc.drop_pin(*DEST)
        sc.copy_pin_coords()
        assert qapp.clipboard().text() == "37.788000, -122.407400"

    def test_the_real_location_is_labelled_approximate(self, win):
        sc = win.mapscreen
        sc.set_real(*SF)
        assert sc._real_ov is not None
        sc.drop_pin(*DEST)
        assert "approximately" in sc.panel.pin.distance.text()

    def test_never_opens_on_the_ocean(self, win):
        lat, lon = win.mapscreen.map.center()
        assert (lat, lon) != (0.0, 0.0)

    def test_map_styles(self, win):
        sc = win.mapscreen
        for k in ("satellite", "hybrid", "standard"):
            sc.set_map_style(k)
            assert sc.map.style_name == k
        assert win.settings["map_style"] == "standard"

    def test_narrow_windows_turn_the_panel_into_a_sheet(self, win):
        win.resize(800, 600)
        _spin()
        sc = win.mapscreen
        sc._layout()
        assert sc.sheet_box.isVisibleTo(win)
        assert not sc.panel.isVisibleTo(win), "the panel should start tucked away"
        sc.drop_pin(*SF)
        _spin(0.4)
        assert sc._sheet_open, "a pin should bring the sheet out"


# ---- §4 Teleport -----------------------------------------------------------

class TestTeleport:
    def test_teleport_pin_sends_it_and_names_it(self, win):
        phone = connect(win)
        sc = win.mapscreen
        sc.drop_pin(*DEST, name="Union Square, San Francisco")
        sc.teleport_pin()
        _spin(0.3)
        assert phone.sets[-1] == DEST
        assert sc.spoof == DEST
        assert sc.toast.text() == "Teleported to Union Square, San Francisco"
        assert win.settings["recent"][0]["name"] == "Union Square, San Francisco"

    def test_recents_keep_twenty(self, win):
        sc = win.mapscreen
        for i in range(25):
            sc._add_recent(10 + i, 10)
        assert len(win.settings["recent"]) == 20

    def test_teleporting_mid_route_asks_then_stops_the_route(self, win, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        asked = []
        monkeypatch.setattr(QMessageBox, "question", staticmethod(
            lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes))
        sc.teleport_to(40.0, -74.0)
        assert asked == ["Stop the current route and teleport?"]
        assert sc.session is None

    def test_saying_no_keeps_the_route(self, win, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        monkeypatch.setattr(QMessageBox, "question", staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Cancel))
        assert sc.teleport_to(40.0, -74.0) is False
        assert sc.session is not None

    def test_stars_rename_and_delete(self, win, monkeypatch):
        from PySide6.QtWidgets import QInputDialog
        sc = win.mapscreen
        sc.drop_pin(*DEST, name="Café")
        sc.toggle_star_pin()
        assert win.settings["saved"][0]["name"] == "Café"
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Coffee", True)))
        sc.rename_place(0)
        assert win.settings["saved"][0]["name"] == "Coffee"
        sc.delete_place(0)
        assert win.settings["saved"] == []


# ---- §5 Routes -------------------------------------------------------------

class TestRoutes:
    def test_route_here_starts_from_the_phone(self, win):
        connect(win)
        sc = straight_route(win)
        assert sc.panel.page == "builder"
        assert sc._plan["ready"]
        assert sc._plan["points"][0] == SF and sc._plan["points"][-1] == DEST

    def test_clicks_add_stops_before_the_end(self, win):
        connect(win)
        sc = straight_route(win)
        sc._on_map_click(37.78, -122.415)
        assert [(s["lat"], s["lon"]) for s in sc.draft.stops] == [(37.78, -122.415), DEST]
        assert len(sc._plan["points"]) == 3
        assert len(sc._plan["stops_d"]) == 1, "the stop is where it dwells"

    def test_reorder_and_remove(self, win):
        connect(win)
        sc = straight_route(win)
        sc._on_map_click(37.78, -122.415)
        sc.reorder_stops([1, 0])
        assert (sc.draft.stops[-1]["lat"], sc.draft.stops[-1]["lon"]) == (37.78, -122.415)
        sc.remove_stop(0)
        assert len(sc.draft.stops) == 1

    def test_the_summary_is_live(self, win):
        connect(win)
        sc = straight_route(win)
        before = sc.panel.builder.summary.text()
        sc.set_speed_kmh(50.0)
        after = sc.panel.builder.summary.text()
        assert before != after and "min" in after

    def test_draw_traces_points(self, win):
        connect(win)
        sc = straight_route(win)
        sc.set_path_style("draw")
        for p in [(37.776, -122.418), (37.778, -122.416)]:
            sc._on_map_click(*p)
        assert sc._plan["stops_d"] == [], "drawn points are a path, not stops"
        assert len(sc._plan["points"]) == 4

    def test_no_road_offers_a_straight_line(self, win, monkeypatch):
        connect(win)
        sc = straight_route(win)
        sc.set_path_style("roads")
        sc._on_plan_ready(sc._plan_key, route.Snapped([SF, DEST], reason="noroute"))
        assert sc._plan["offer_straight"] and "No road" in sc._plan["message"]
        assert not sc._plan["ready"]
        sc.use_straight_line()
        assert sc._plan["ready"]

    def test_offline_roads_say_so(self, win):
        connect(win)
        sc = straight_route(win)
        sc.set_path_style("roads")
        sc._on_plan_ready(sc._plan_key, route.Snapped([SF, DEST], reason="offline"))
        assert "offline" in sc._plan["message"]

    def test_start_uses_the_cached_road_path(self, win, monkeypatch):
        connect(win)
        sc = straight_route(win)
        sc.set_path_style("roads")
        snapped = route.Snapped([SF, (37.78, -122.41), DEST], True)
        sc._on_plan_ready(sc._plan_key, snapped)
        sc.start_route()
        assert sc.session.plan.points == [SF, (37.78, -122.41), DEST]

    def test_speed_presets_and_units(self, win):
        connect(win)
        sc = straight_route(win)
        ctl = sc.panel.builder.speed
        ctl.chips._btns["Drive"].click()
        assert sc.draft.speed_kmh == 50.0
        for unit, shown in (("kmh", "50"), ("mph", "31")):
            other = "mph" if unit == "kmh" else "kmh"
            if ctl.unit.value() == unit:          # already selected: go via the other
                ctl.unit._btns[other].click()
            ctl.unit._btns[unit].click()
            assert win.settings["units"] == unit
            assert ctl.value.text() == shown

    def test_a_very_fast_speed_warns_but_is_allowed(self, win):
        connect(win)
        sc = straight_route(win)
        ctl = sc.panel.builder.speed
        ctl.unit.set_value("kmh"); ctl._units = "kmh"
        ctl.value.setText("250"); ctl._on_typed()
        assert sc.draft.speed_kmh == 250.0
        assert ctl.warn.isVisibleTo(win)

    def test_save_load_duplicate(self, win, monkeypatch):
        from PySide6.QtWidgets import QInputDialog
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Commute", True)))
        connect(win)
        sc = straight_route(win)
        sc.save_route()
        sc.close_builder()
        sc.duplicate_route(0)
        assert [r["name"] for r in win.settings["routes"]] == ["Commute", "Commute copy"]
        sc.load_route(0)
        assert sc.draft is not None and sc._plan["ready"]

    def test_gpx_round_trips_identically(self, win, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QFileDialog
        connect(win)
        sc = straight_route(win)
        sc._on_map_click(37.78123456789, -122.41098765432)
        out = tmp_path / "r.gpx"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
        sc.export_route()
        exported = list(sc._plan["points"])
        sc.close_builder()
        sc.load_track(route.parse_gpx(str(out)))
        assert sc._plan["points"] == exported


# ---- §5 Running ------------------------------------------------------------

class TestRunning:
    def test_start_runs_and_shows_progress(self, win):
        phone = connect(win)
        sc = straight_route(win)
        sc.start_route()
        assert sc.panel.page == "running"
        assert sc._caffeinate is not None, "the Mac must stay awake"
        _spin(1.3)
        assert phone.sets, "nothing reached the phone"
        assert sc.panel.running.stats["remaining"].text() not in ("", "—")

    def test_pause_holds_and_resume_carries_on(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        sc.toggle_pause()
        assert sc.session.paused
        assert sc.panel.running.pause.text() == "Resume"
        sc.toggle_pause()
        assert not sc.session.paused

    def test_speed_changes_mid_route_without_a_jump(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        _spin(0.3)
        with sc.runner.lock:
            before = sc.session.position_now()
        sc.set_speed_kmh(40.0)
        with sc.runner.lock:
            after = sc.session.position()
        assert abs(after[0] - before[0]) < 1e-5 and abs(after[1] - before[1]) < 1e-5
        assert sc.session.base_speed() == pytest.approx(40 / 3.6)

    def test_walking_is_refused_while_a_route_runs(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        sc.key_walk("Up", True)
        assert sc._walking is False
        sc.key_walk("Up", False)

    def test_following_pans_only_near_the_edge(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        sc.map.resize(800, 600)
        sc.map.set_view(*SF, 15)
        c = sc.map.center()
        t = Tick(SF[0] + 0.00001, SF[1], 0, 0.1, 100, 60, 1.4, 1, True, False, False, False)
        sc._on_tick(t)
        assert sc.map.center() == c
        sc._on_user_panned()
        assert sc._follow_paused, "panning by hand should not fight the follow"

    def test_a_loop_trail_starts_fresh_each_lap(self, win):
        connect(win)
        sc = straight_route(win)
        sc.draft.opts.update(end="loop", laps=3)
        sc.start_route()
        L = sc.session.lap.total
        mk = lambda D: Tick(SF[0], SF[1], 0, 0.5, 1, 1, 1.4, 1, True, False, False, False, D=D)
        sc._on_tick(mk(0.9 * L))
        n1 = len(sc._travel_ov.pts)
        sc._on_tick(mk(1.05 * L))
        assert len(sc._travel_ov.pts) < n1


# ---- §6 Unplugging mid-route -----------------------------------------------

class TestOutOfReach:
    def test_the_pill_turns_amber(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        t = Tick(SF[0], SF[1], 0, 0.3, 10, 10, 1.4, 1, False, True, False, False)
        sc._on_tick(t)
        assert "route clock running" in sc.pill.text.text()

    def test_the_route_note_says_what_the_phone_needs(self, win):
        connect(win, Phone(wireless_on=False))
        sc = straight_route(win)
        assert "Wi-Fi" in sc.panel.builder.link_note.text()

    def test_losing_the_phone_keeps_the_route(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        win._on_device_lost()
        assert sc.session is not None and sc.runner is not None

    def test_closing_the_window_keeps_it_running(self, win):
        from PySide6.QtGui import QCloseEvent
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        e = QCloseEvent()
        win.closeEvent(e)
        assert not e.isAccepted() and sc.runner is not None

    def test_menu_bar_state(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        st = win.menu_state()
        assert st["route"]["running"] and st["route"]["pct"] >= 0


# ---- §7 Reconnecting and resuming ------------------------------------------

class TestResuming:
    def test_the_session_is_saved_as_it_runs(self, win, tmp_path):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        assert store.load_session("UDID-1") is not None

    def test_quitting_pauses_it_for_next_time(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        sc.pause_for_quit()
        d = store.load_session("UDID-1")
        assert d["paused"] is True

    def test_relaunch_offers_to_resume(self, win):
        s = RouteSession(Plan([SF, DEST]), 1.4, Options(), name="Home")
        s.pause()
        sc = win.mapscreen
        sc.restore_session("UDID-1", dict(s.to_dict(), udid="UDID-1"))
        assert sc.banner.isVisibleTo(win) and "Home" in sc.banner.text.text()
        assert sc.runner is None, "nothing moves until you say so"
        connect(win)
        assert sc.runner is None, "reconnecting alone should not start it"
        sc.resume_session()
        assert sc.runner is not None and not sc.session.paused

    def test_catch_up_and_continue(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        _spin(1.2)
        win.settings["catch_up"] = "continue"
        with sc.runner.lock:
            sc.session.mark_delivered()
            delivered = sc.session.prog.D
            sc.session.mark_offline()
            sc.session.prog.D += 50.0          # the clock ran on while away
        sc._apply_catch_up()
        assert sc.session.prog.D == pytest.approx(delivered, abs=2.0)
        assert sc.session.hold is False

    def test_a_restarted_phone_is_noticed(self, win):
        win.settings["spoofs"] = {"UDID-1": {"lat": SF[0], "lon": SF[1], "name": "Work"}}
        connect(win, Phone(fresh_mount=True))
        sc = win.mapscreen
        assert sc.banner.isVisibleTo(win)
        assert sc.banner.a.text() == "Re-apply location"
        assert sc.spoof is None

    def test_a_restarted_phone_mid_route_offers_both(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        sc.on_connected(Phone(fresh_mount=True))
        assert {sc.banner.a.text(), sc.banner.b.text()} == {"Resume route", "Re-apply location"}
        assert sc.session.hold

    def test_sessions_are_per_phone(self, win):
        connect(win)
        sc = straight_route(win)
        sc.start_route()
        sc.on_connected(Phone(udid="OTHER"))
        assert sc.session is None
        assert store.load_session("UDID-1") is not None, "the first phone's route was lost"


# ---- §8 Stopping -------------------------------------------------------------

class TestStopping:
    def test_stop_spoofing_restores(self, win):
        phone = connect(win)
        sc = win.mapscreen
        sc._set_spoof(SF)
        sc.stop_spoofing()
        _spin(0.3)
        assert phone.cleared == 1
        assert sc.spoof is None
        assert sc.toast.text() == "Your iPhone is using its real location again."

    def test_stop_spoofing_ends_a_route_too(self, win):
        phone = connect(win)
        sc = straight_route(win)
        sc.start_route()
        sc.stop_spoofing()
        _spin(0.3)
        assert sc.session is None and phone.cleared == 1

    def test_offline_it_explains_the_way_out(self, win):
        sc = win.mapscreen
        sc.stop_spoofing()
        assert "restarting the iPhone" in sc.toast.text().lower() or \
               "Restarting the iPhone" in sc.toast.text()


# ---- the Mac -----------------------------------------------------------------

class TestTheMac:
    def test_caffeinate_dies_with_the_app(self, win):
        sc = win.mapscreen
        sc.hold_awake(True)
        try:
            assert "-w" in sc._caffeinate.args
        finally:
            sc.hold_awake(False)
        assert sc._caffeinate is None

    def test_battery_parsing(self, monkeypatch):
        from qtui import app
        import subprocess

        class R:
            stdout = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t17%; discharging;"
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
        assert app.battery() == (17, True)

    def test_heartbeat_steps_aside_while_a_route_pushes(self, win):
        connect(win)
        sc = straight_route(win)
        assert sc.heartbeat_point() == SF
        sc.start_route()
        assert sc.heartbeat_point() is None
        sc.toggle_pause()
        assert sc.heartbeat_point() is not None, "a paused route should keep the channel checked"


class TestLookAndFeel:
    def test_both_appearances_render(self, win, qapp):
        from qtui import theme
        for scheme in ("light", "dark"):
            theme.apply(qapp, scheme)
            win.mapscreen.retheme()
            assert win.grab().width() > 0
        assert theme.is_dark()

    def test_segmented_only_reports_real_changes(self, qapp):
        from qtui.widgets import Segmented
        seg = Segmented(["A", "B"])
        got = []
        seg.changed.connect(got.append)
        seg._btns["A"].click(); seg._btns["B"].click(); seg._btns["B"].click()
        assert got == ["B"]

    def test_icon_buttons_are_labelled_for_screen_readers(self, win):
        from qtui.widgets import IconButton
        unnamed = [b for b in win.findChildren(IconButton) if not b.accessibleName()]
        assert unnamed == []
