"""Route math — the copy the app actually runs.

These assertions used to live against core.route_points, which the Qt app never
called: it goes through qtui/route.py. The two implementations had already
drifted (snap_to_roads defaulted to "driving" in one and "walking" in the other),
so core's copy is gone and this is the only one left.
"""

from __future__ import annotations

import pytest

from qtui.route import meters, route_points


def test_meters_known_distance():
    # 0.01° of latitude at the equator is ~1111 m.
    assert 1108 < meters((0.0, 0.0), (0.01, 0.0)) < 1115


def test_meters_zero():
    assert meters((37.7749, -122.4194), (37.7749, -122.4194)) == 0.0


def test_meters_is_symmetric():
    a, b = (47.6062, -122.3321), (37.7749, -122.4194)
    assert meters(a, b) == pytest.approx(meters(b, a))


def test_route_short_inputs_pass_through():
    assert route_points([], 1.0) == []
    assert route_points([(1.0, 2.0)], 1.0) == [(1.0, 2.0)]


def test_route_rejects_nonpositive_speed_or_dt():
    pts = [(0.0, 0.0), (0.0, 0.01)]
    for speed, dt in [(0.0, 1.0), (-1.0, 1.0), (1.0, 0.0), (1.0, -1.0)]:
        with pytest.raises(ValueError):
            route_points(pts, speed, dt)


def test_route_hits_both_endpoints_exactly():
    a, b = (0.0, 0.0), (0.0, 0.01)  # ~1113 m east
    path = route_points([a, b], speed_mps=100.0, dt=1.0)
    assert path[0] == a
    assert path[-1] == b
    assert 10 <= len(path) <= 14  # ~12 one-second steps + the start


def test_route_steps_respect_speed():
    # No hop between consecutive fixes should exceed speed * dt (+ float slack).
    path = route_points([(0.0, 0.0), (0.001, 0.02), (0.01, 0.02)], speed_mps=20.0, dt=1.0)
    for p, q in zip(path, path[1:]):
        assert meters(p, q) <= 20.0 + 1e-6


def test_route_lands_on_every_waypoint():
    pts = [(0.0, 0.0), (0.0, 0.005), (0.005, 0.005)]
    path = route_points(pts, speed_mps=50.0, dt=1.0)
    for wp in pts:
        assert wp in path


def test_route_density_scales_with_dt():
    pts = [(0.0, 0.0), (0.0, 0.02)]
    fine = route_points(pts, speed_mps=10.0, dt=0.5)
    coarse = route_points(pts, speed_mps=10.0, dt=2.0)
    assert len(fine) > len(coarse)


class TestProfileRouting:
    """Picking Walk / Cycle / Drive has to change the road route.

    router.project-osrm.org accepts /route/v1/walking/ and /route/v1/cycling/ and
    answers "Ok" while returning the identical car geometry every time, so the
    transport selector did nothing and a walk was routed down whatever a car
    would take. The profile lives in the host now, not the path.
    """

    def test_each_profile_gets_its_own_router(self):
        from qtui.route import OSRM_INSTANCES, osrm_url
        hosts = {p: osrm_url(p, "0,0;1,1") for p in ("walking", "cycling", "driving")}
        assert len(set(hosts.values())) == 3, f"profiles share a router: {hosts}"
        assert "routed-foot" in hosts["walking"]
        assert "routed-bike" in hosts["cycling"]
        assert "routed-car" in hosts["driving"]
        assert len(OSRM_INSTANCES) == 3

    def test_car_only_public_server_is_not_used(self):
        from qtui.route import osrm_url
        for p in ("walking", "cycling", "driving"):
            assert "router.project-osrm.org" not in osrm_url(p, "0,0;1,1")

    def test_unknown_profile_falls_back_rather_than_crashing(self):
        from qtui.route import DEFAULT_PROFILE, osrm_url
        assert osrm_url("teleportation", "0,0;1,1") == osrm_url(DEFAULT_PROFILE, "0,0;1,1")

    def test_snap_returns_geometry_and_the_router_estimate(self, monkeypatch):
        import qtui.route as route

        class Resp:
            @staticmethod
            def json():
                return {"code": "Ok", "routes": [{
                    "distance": 1960.0, "duration": 1572.0,
                    "geometry": {"coordinates": [[-122.42, 37.77], [-122.41, 37.78],
                                                 [-122.40, 37.79]]}}]}

        seen = {}

        def fake_get(url, **kw):
            seen["url"] = url
            return Resp()

        monkeypatch.setattr("requests.get", fake_get)
        out = route.snap_to_roads([(37.77, -122.42), (37.79, -122.40)], "cycling")
        assert out.ok is True
        assert out.points[0] == (37.77, -122.42) and len(out.points) == 3
        assert out.distance_m == 1960.0 and out.duration_s == 1572.0
        assert "routed-bike" in seen["url"]

    def test_a_dead_router_still_lets_the_route_play(self, monkeypatch):
        """Straight lines beat refusing to move."""
        import qtui.route as route

        def boom(*a, **k):
            raise OSError("no network")

        monkeypatch.setattr("requests.get", boom)
        pts = [(37.77, -122.42), (37.79, -122.40)]
        out = route.snap_to_roads(pts, "walking")
        assert out.ok is False
        assert out.points == pts
        assert out.duration_s == 0.0
