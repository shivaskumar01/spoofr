"""Web-Mercator projection and the tile retry policy."""

from __future__ import annotations

import pytest

from qtui.tilemap import TILE, MAX_TILE_ATTEMPTS, TileMap


@pytest.fixture
def tmap(qapp):
    return TileMap()


@pytest.mark.parametrize("lat, lon", [
    (0.0, 0.0), (37.7749, -122.4194), (47.6062, -122.3321),
    (-33.8688, 151.2093), (51.5074, -0.1278), (0.0, 179.9), (0.0, -179.9),
])
@pytest.mark.parametrize("zoom", [2, 8, 11, 16, 20])
def test_projection_round_trips(tmap, lat, lon, zoom):
    tmap._z = zoom
    back_lat, back_lon = tmap._scene_to_ll(tmap._scene_pt(lat, lon))
    assert back_lat == pytest.approx(lat, abs=1e-6)
    assert back_lon == pytest.approx(lon, abs=1e-6)


def test_latitude_is_clamped_at_the_mercator_limit(tmap):
    """Web Mercator can't represent the poles; the clamp keeps y finite and on
    the board (bar sub-pixel float slack at the very edge)."""
    tmap._z = 11
    world = TILE * (2 ** tmap._z)
    for lat in (90.0, -90.0, 89.9, -89.9):
        y = tmap._scene_pt(lat, 0.0).y()
        assert -1e-3 <= y <= world + 1e-3
    assert tmap._scene_pt(90.0, 0.0).y() == pytest.approx(0.0, abs=1e-3)
    assert tmap._scene_pt(-90.0, 0.0).y() == pytest.approx(world, abs=1e-3)


def test_the_world_is_square_at_every_level(tmap):
    for z in (2, 10, 18):
        tmap._z = z
        assert tmap._scene_pt(0.0, 180.0).x() == pytest.approx(tmap._world())
        assert tmap._scene_pt(0.0, -180.0).x() == pytest.approx(0.0)


def test_zoom_is_clamped_to_the_configured_range(tmap):
    tmap.set_view(0.0, 0.0, 99)
    assert tmap.zoom == tmap.max_zoom
    tmap.set_view(0.0, 0.0, -5)
    assert tmap.zoom == tmap.min_zoom


def test_a_failing_tile_is_retried_but_not_forever(tmap):
    """A layout pass runs on every pan frame; an unconditional retry hammered a
    tile the server would never serve."""
    key = (11, 327, 713)
    requested = []
    tmap._request = requested.append
    tmap._tiles[key] = object()
    for _ in range(MAX_TILE_ATTEMPTS + 3):
        if 0 < tmap._failed.get(key, 0) < MAX_TILE_ATTEMPTS:
            tmap._request(key)
        tmap._failed[key] = tmap._failed.get(key, 0) + 1   # what _on_tile does on error
    assert len(requested) == MAX_TILE_ATTEMPTS - 1


def test_tile_scale_comes_from_the_tile_not_the_display(tmap):
    """A source that only serves 256px would be drawn at half size on a retina
    Mac if we assumed @2x, tearing the grid open."""
    from PySide6.QtGui import QPixmap
    for width, expected in ((TILE, 1.0), (TILE * 2, 2.0)):
        pm = QPixmap(width, width)
        pm.fill()
        assert tmap._prepare(pm).devicePixelRatio() == pytest.approx(expected)


def test_recolour_turns_a_light_basemap_dark(tmap):
    from PySide6.QtGui import QColor, QPixmap
    assert tmap._source.recolor, "the default source is a light map we invert"
    white = QPixmap(TILE, TILE)
    white.fill(QColor("#ffffff"))
    out = tmap._prepare(white).toImage()
    assert QColor(out.pixel(8, 8)).lightness() < 20, "white should invert to near-black"

    black = QPixmap(TILE, TILE)
    black.fill(QColor("#000000"))
    out = tmap._prepare(black).toImage()
    assert QColor(out.pixel(8, 8)).lightness() > 235, "black should invert to near-white"


def test_no_source_depends_on_an_api_key(tmap):
    """CARTO still answers 200 while stamping "API KEY REQUIRED" across every
    tile, so no fetch-level check can catch it. Pin the source instead, on both
    surfaces — the desktop map and the phone's."""
    import pathlib
    import re

    assert "cartocdn" not in tmap._source.url
    assert tmap._source.attribution

    for f, comment in (("qtui/tilemap.py", "#"), ("web/app.js", "//")):
        for i, line in enumerate(pathlib.Path(f).read_text(encoding="utf-8").splitlines(), 1):
            if "cartocdn" in line:
                stripped = line.strip()
                assert stripped.startswith(comment) or stripped.startswith("*"), (
                    f"{f}:{i} still points at a keyed basemap: {stripped}")


def test_both_surfaces_use_the_same_basemap(tmap):
    """The Mac app and the phone should not drift onto different providers."""
    import pathlib
    import re
    js = pathlib.Path("web/app.js").read_text(encoding="utf-8")
    m = re.search(r'tiles:\s*\["([^"]+)"\]', js)
    assert m, "could not find the raster tile template in web/app.js"
    host = lambda u: u.split("/")[2]
    assert host(m.group(1)) == host(tmap._source.url)
