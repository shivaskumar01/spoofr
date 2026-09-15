"""Pure helpers in core.py: how the phone is linked, and the tunnel's port check."""

from __future__ import annotations

import pytest

from core import _kinds_to_link


@pytest.mark.parametrize("kinds, expected", [
    ({"USB"}, "USB"),
    ({"Network"}, "Wi-Fi"),
    ({"USB", "Network"}, "USB + Wi-Fi"),
    (set(), ""),
    ({"Bluetooth"}, ""),
])
def test_kinds_to_link(kinds, expected):
    assert _kinds_to_link(kinds) == expected


def test_empty_link_means_gone_not_unknown():
    """The liveness monitor treats "" as a disconnect and None as no-information,
    so these must stay distinct."""
    assert _kinds_to_link(set()) == ""
    assert _kinds_to_link(set()) is not None
