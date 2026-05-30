"""Tests for the route math in core.py. No device required."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import _meters, route_points


def test_meters_known_distance():
    # 0.01° of latitude at the equator is ~1111 m.
    assert 1108 < _meters((0.0, 0.0), (0.01, 0.0)) < 1115


def test_meters_zero():
    assert _meters((37.7749, -122.4194), (37.7749, -122.4194)) == 0.0


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
        assert _meters(p, q) <= 20.0 + 1e-6


def test_route_lands_on_every_waypoint():
    pts = [(0.0, 0.0), (0.0, 0.005), (0.005, 0.005)]
    path = route_points(pts, speed_mps=50.0, dt=1.0)
    for wp in pts:
        assert wp in path
