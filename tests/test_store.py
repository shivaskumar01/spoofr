"""Settings persistence: saved places must survive a crash mid-write."""

from __future__ import annotations

import pytest

from qtui import store


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(store, "_path", lambda: p)
    return p


def test_round_trip(settings_file):
    store.save({"saved": [{"name": "home", "lat": 1.0, "lon": 2.0}]})
    assert store.load()["saved"][0]["name"] == "home"


def test_missing_file_loads_as_empty(settings_file):
    assert store.load() == {}


def test_corrupt_file_loads_as_empty_instead_of_raising(settings_file):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{not json")
    assert store.load() == {}


def test_save_is_atomic(settings_file, monkeypatch):
    """A failed write must leave the previous settings intact, not truncated."""
    store.save({"saved": [{"name": "home", "lat": 1.0, "lon": 2.0}]})
    original = settings_file.read_text()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    with monkeypatch.context() as m:       # scoped: settings_file stays patched
        m.setattr(store.json, "dumps", boom)
        store.save({"saved": []})

    assert settings_file.read_text() == original
    assert store.load()["saved"][0]["name"] == "home"


def test_no_temp_file_is_left_behind(settings_file):
    store.save({"a": 1})
    leftovers = list(settings_file.parent.glob("*.tmp"))
    assert leftovers == []
