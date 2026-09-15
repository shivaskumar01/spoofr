"""Coordinate parsing — the one piece of the search box that never hits a network."""

from __future__ import annotations

import pytest

from qtui.geo import parse_coords


@pytest.mark.parametrize("text, expected", [
    ("37.7749, -122.4194", (37.7749, -122.4194)),
    ("37.7749 -122.4194", (37.7749, -122.4194)),
    ("  47.6062,-122.3321  ", (47.6062, -122.3321)),
    ("0,0", (0.0, 0.0)),
    ("-90,180", (-90.0, 180.0)),
])
def test_parses_valid_pairs(text, expected):
    assert parse_coords(text) == expected


@pytest.mark.parametrize("text", [
    None, "", "Seattle", "37.7749", "91,0", "-91,0", "0,181", "0,-181",
    "abc,def", "37.7749, -122.4194, 12",
])
def test_rejects_everything_else(text):
    assert parse_coords(text) is None


def test_a_place_name_is_not_mistaken_for_coordinates():
    """The search box treats a parsed pair as a teleport target, so a false
    positive here would silently move the phone to the wrong place."""
    for name in ("Golden Gate Bridge", "10 Downing Street", "SW1A 1AA"):
        assert parse_coords(name) is None
