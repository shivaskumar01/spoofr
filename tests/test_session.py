"""The route engine: where the phone should be, as a function of the clock.

These pin the brief's Section 6/7 guarantees without a phone: progress is tied
to the clock, survives being out of reach, sleep, quits and reboots, and speed
changes never make the phone jump.
"""

from __future__ import annotations

import pytest

from qtui.session import (
    KMH, Clock, Options, Plan, RouteSession, Runner, meters,
)


class FakeTime:
    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_700_000_000.0
        self.boot = "boot-A"

    def tick(self, s):
        self.mono += s
        self.wall += s

    def kw(self):
        return {"mono": lambda: self.mono, "boot": lambda: self.boot, "wall": lambda: self.wall}


@pytest.fixture
def ft():
    return FakeTime()


def line_east(km: float, lat: float = 37.0, lon: float = -122.0, n: int = 1):
    """A straight west→east line `km` long, split into n segments."""
    dlon = (km * 1000.0) / (111_320.0 * __import__("math").cos(__import__("math").radians(lat)))
    return [(lat, lon + dlon * i / n) for i in range(n + 1)]


def session(ft, km=2.0, kmh=5.0, n=4, **opts):
    plan = Plan(line_east(km, n=n))
    return RouteSession(plan, kmh * KMH, Options(**opts), clock=Clock(**ft.kw()), seed=7)


class TestTiming:
    def test_two_km_at_five_kmh_takes_twenty_four_minutes(self, ft):
        """Acceptance test: a 2 km walk at 5 km/h arrives in ~24 minutes."""
        s = session(ft)
        ft.tick(23.5 * 60)
        s.advance(s.clock.t())
        assert not s.prog.finished
        ft.tick(0.6 * 60)
        s.advance(s.clock.t())
        assert s.prog.finished
        assert s.summary()["duration_s"] == pytest.approx(24 * 60, abs=60)

    def test_variation_keeps_the_eta_honest(self, ft):
        """±10% wobble must average out: still ~24 min for the same walk."""
        s = session(ft, variation=True)
        ft.tick(25 * 60)
        s.advance(s.clock.t())
        assert s.prog.finished
        assert s.summary()["duration_s"] == pytest.approx(24 * 60, abs=60)

    def test_variation_really_varies(self, ft):
        s = session(ft, variation=True)
        speeds = {round(s.speed_at(t), 3) for t in range(0, 60, 5)}
        assert len(speeds) > 5
        assert all(0.89 * 5 * KMH <= v <= 1.11 * 5 * KMH for v in speeds)

    def test_position_follows_the_line(self, ft):
        s = session(ft, km=1.0, kmh=36.0)          # 10 m/s
        ft.tick(30)
        lat, lon, heading = s.position_now()
        assert meters(s.plan.points[0], (lat, lon)) == pytest.approx(300, abs=1)
        assert heading == pytest.approx(90, abs=1)   # due east


class TestSpeedChanges:
    def test_a_speed_change_does_not_move_the_phone(self, ft):
        s = session(ft, km=5.0, kmh=5.0)
        ft.tick(600)
        before = s.position_now()
        s.set_speed(50 * KMH)
        assert s.position() == before, "changing speed made the phone jump"

    def test_the_new_speed_applies_from_then_on(self, ft):
        s = session(ft, km=10.0, kmh=5.0)
        ft.tick(360)                                # 500 m at 5 km/h
        s.set_speed(36 * KMH)                       # then 10 m/s
        ft.tick(100)                                # +1000 m
        s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(1500, abs=1)

    def test_eta_updates_immediately(self, ft):
        s = session(ft, km=2.0, kmh=5.0)
        slow = s.remaining_s()
        s.set_speed(10 * KMH)
        assert s.remaining_s() == pytest.approx(slow / 2, rel=0.01)


class TestPause:
    def test_pause_holds_position_and_resume_carries_on(self, ft):
        s = session(ft, km=2.0, kmh=36.0)
        ft.tick(50)
        s.pause()
        held = s.position_now()
        ft.tick(600)
        assert s.position_now() == held, "moved while paused"
        s.resume()
        ft.tick(10)
        s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(600, abs=1)

    def test_paused_time_is_not_route_time(self, ft):
        s = session(ft, km=1.0, kmh=36.0)          # 100 s of route
        ft.tick(40)
        s.pause()
        ft.tick(1000)
        s.resume()
        ft.tick(60)
        s.advance(s.clock.t())
        assert s.prog.finished
        assert s.summary()["duration_s"] == pytest.approx(100, abs=1)


class TestLapsAndEnds:
    def test_three_laps_runs_exactly_three_laps_and_stops(self, ft):
        """Acceptance test: a loop set to 3 laps runs exactly 3 and stops."""
        s = session(ft, km=1.0, kmh=36.0, end="loop", laps=3)
        L = s.lap.total
        assert L > 1000, "a loop includes the leg back to the start"
        ft.tick(2.9 * L / 10)
        s.advance(s.clock.t())
        assert s.lap_number() == 3 and not s.prog.finished
        ft.tick(0.2 * L / 10)
        s.advance(s.clock.t())
        assert s.prog.finished
        assert s.prog.D == pytest.approx(3 * L)
        lat, lon, _ = s.position()
        assert meters((lat, lon), s.plan.points[0]) < 1, "a finished loop ends back at the start"

    def test_ping_pong_comes_back(self, ft):
        s = session(ft, km=1.0, kmh=36.0, end="pingpong", laps=2)
        L = s.lap.total
        ft.tick(150)                                # out L, then 1500 - L back
        lat, lon, heading = s.position_now()
        assert meters(s.plan.points[0], (lat, lon)) == pytest.approx(2 * L - 1500, abs=1)
        assert heading == pytest.approx(270, abs=1), "heading on the way back is reversed"
        ft.tick(100)
        s.advance(s.clock.t())
        assert s.prog.finished
        assert meters(s.position()[:2], s.plan.points[0]) < 1

    def test_forever_never_finishes(self, ft):
        s = session(ft, km=1.0, kmh=36.0, end="loop", laps=None)
        ft.tick(100_000)
        s.advance(s.clock.t())
        assert not s.prog.finished and s.remaining_m() is None

    def test_dwell_at_each_stop(self, ft):
        plan = Plan(line_east(2.0, n=2), stops_d=[1000.0])
        s = RouteSession(plan, 10.0, Options(dwell_s=30), clock=Clock(**ft.kw()))
        ft.tick(100)                                 # reaches the stop
        s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(1000)
        ft.tick(25)                                  # still dwelling
        s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(1000)
        ft.tick(15)                                  # 10 s past the dwell
        s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(1100, abs=1)
        assert s.remaining_s() == pytest.approx(90, abs=1)


class TestOutOfReach:
    def test_catch_up_is_where_it_would_have_been(self, ft):
        s = session(ft, km=5.0, kmh=36.0)
        ft.tick(10); s.advance(s.clock.t()); s.mark_delivered()
        s.mark_offline()
        ft.tick(120)
        s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(1300, abs=1)
        assert s.skipped() == pytest.approx((100, 1300), abs=1)

    def test_continue_resumes_from_the_last_fix_received(self, ft):
        s = session(ft, km=5.0, kmh=36.0)
        ft.tick(10); s.advance(s.clock.t()); s.mark_delivered()
        s.mark_offline()
        ft.tick(120)
        s.advance(s.clock.t())
        s.rewind_to_delivered()
        assert s.prog.D == pytest.approx(100, abs=1)
        ft.tick(10); s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(200, abs=1)

    def test_finished_while_away_lands_on_the_end(self, ft):
        s = session(ft, km=1.0, kmh=36.0)
        ft.tick(10); s.advance(s.clock.t()); s.mark_delivered()
        ft.tick(10_000)
        lat, lon, _ = s.position_now()
        assert s.prog.finished
        assert meters((lat, lon), s.plan.points[-1]) < 1


class TestTheClock:
    def test_sleep_counts(self, ft):
        """CLOCK_MONOTONIC keeps counting while the lid is shut."""
        s = session(ft, km=5.0, kmh=36.0)
        ft.tick(300)                                 # 'asleep' for 5 minutes
        s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(3000, abs=1)

    def test_a_wall_clock_change_changes_nothing(self, ft):
        s = session(ft, km=5.0, kmh=36.0)
        ft.wall -= 3600                             # time zone / clock set back
        ft.tick(60)
        s.advance(s.clock.t())
        assert s.prog.D == pytest.approx(600, abs=1)

    def test_it_survives_a_save_and_reload(self, ft):
        s = session(ft, km=5.0, kmh=36.0, end="pingpong", laps=3)
        ft.tick(100); s.set_speed(20.0); s.pause(); ft.tick(5); s.resume()
        ft.tick(20); s.advance(s.clock.t())
        d = s.to_dict()
        ft.tick(30)
        back = RouteSession.from_dict(d, clock_kw=ft.kw())
        back.advance(back.clock.t())
        s.advance(s.clock.t())
        assert back.prog.D == pytest.approx(s.prog.D, abs=0.01)
        assert back.opts == s.opts and back.segments == s.segments

    def test_it_survives_a_mac_restart(self, ft):
        s = session(ft, km=50.0, kmh=36.0)
        ft.tick(100); s.advance(s.clock.t())
        d = s.to_dict()
        # reboot: the monotonic clock starts over; 10 minutes pass while off
        ft.boot, ft.mono, ft.wall = "boot-B", 5.0, ft.wall + 600
        back = RouteSession.from_dict(d, clock_kw=ft.kw())
        back.advance(back.clock.t())
        assert back.prog.D == pytest.approx(7000, abs=5)


class TestRunner:
    def _runner(self, ft, **opts):
        s = session(ft, km=1.0, kmh=36.0, **opts)
        sent, state = [], {"ok": True}

        def push(lat, lon):
            if state["ok"]:
                sent.append((lat, lon))
            return state["ok"]
        return s, Runner(s, push), sent, state

    def test_it_pushes_the_current_position(self, ft):
        s, r, sent, _ = self._runner(ft)
        ft.tick(10)
        t = r.step()
        assert t.delivered and len(sent) == 1
        assert meters(s.plan.points[0], sent[0]) == pytest.approx(100, abs=1)

    def test_out_of_reach_then_back_reports_the_skip(self, ft):
        s, r, sent, state = self._runner(ft)
        ft.tick(5); r.step()
        state["ok"] = False
        ft.tick(30)
        assert r.step().offline
        state["ok"] = True
        ft.tick(5)
        t = r.step()
        assert t.came_back and t.skipped == pytest.approx((50, 400), abs=1)

    def test_hold_sends_nothing(self, ft):
        s, r, sent, _ = self._runner(ft)
        s.hold = True
        ft.tick(5)
        r.step()
        assert sent == []

    def test_a_paused_route_sends_its_spot_once(self, ft):
        s, r, sent, _ = self._runner(ft)
        ft.tick(5); r.step()
        ft.tick(3)
        s.pause()                                   # 30 m past the last fix
        r.step(); r.step(); r.step()
        assert len(sent) == 2, "the held spot is sent once, not every tick"

    def test_done_only_once_the_phone_has_the_end(self, ft):
        s, r, sent, state = self._runner(ft)
        state["ok"] = False
        ft.tick(1000)
        assert not r.step().done
        state["ok"] = True
        t = r.step()
        assert t.done and meters(sent[-1], s.plan.points[-1]) < 1

    def test_jitter_stays_close(self, ft):
        s, r, sent, _ = self._runner(ft, jitter=True)
        for _ in range(20):
            ft.tick(1); r.step()
        for (lat, lon), i in zip(sent, range(1, 21)):
            exact = s.plan.at(i * 10.0)
            assert meters((lat, lon), exact[:2]) <= 3.0
