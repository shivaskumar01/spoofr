"""Route math, snap-to-roads, and GPX, pure, no device stack.

Interpolation between waypoints, OSRM road-snapping, and GPX read/write. Kept
out of core.py so route editing / GPX import never pulls in pymobiledevice3.
Call the network/file functions from a worker thread.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0


def meters(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def route_points(points, speed_mps: float, dt: float = 1.0):
    """Fixes to emit so the polyline is walked at `speed_mps`, one per `dt`s."""
    if speed_mps <= 0 or dt <= 0:
        raise ValueError("speed_mps and dt must be positive")
    if len(points) < 2:
        return list(points)
    step = speed_mps * dt
    path = [tuple(points[0])]
    for a, b in zip(points, points[1:]):
        steps = max(1, math.ceil(meters(a, b) / step))
        for i in range(1, steps + 1):
            if i == steps:
                path.append(tuple(b))
            else:
                f = i / steps
                path.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return path


def snap_to_roads(points, profile: str = "walking", timeout: float = 8.0):
    """Replace straight segments with real road geometry (OSRM). Returns the
    densified [(lat,lon),…]; on any failure returns `points` unchanged."""
    pts = [tuple(p) for p in points]
    if len(pts) < 2:
        return pts
    import requests
    coords = ";".join(f"{lon},{lat}" for lat, lon in pts)
    url = f"https://router.project-osrm.org/route/v1/{profile}/{coords}"
    try:
        r = requests.get(url, params={"overview": "full", "geometries": "geojson"},
                         headers={"User-Agent": "Spoofr/1.0 (macOS location utility)"},
                         timeout=timeout)
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            geo = data["routes"][0]["geometry"]["coordinates"]   # [lon, lat]
            snapped = [(c[1], c[0]) for c in geo if len(c) >= 2]
            if len(snapped) >= 2:
                return snapped
    except Exception:
        pass
    return pts


def parse_gpx(path: str):
    import gpxpy
    with open(path, "r", encoding="utf-8") as fh:
        gpx = gpxpy.parse(fh)
    pts: list[tuple[float, float]] = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            pts += [(p.latitude, p.longitude) for p in seg.points]
    if not pts:
        for rte in gpx.routes:
            pts += [(p.latitude, p.longitude) for p in rte.points]
    if not pts:
        pts += [(w.latitude, w.longitude) for w in gpx.waypoints]
    if len(pts) < 2:
        raise RuntimeError("That GPX file has no usable track (need at least two points).")
    return pts


def build_gpx(points, name: str = "Spoofr route") -> str:
    import gpxpy
    import gpxpy.gpx
    gpx = gpxpy.gpx.GPX()
    gpx.creator = "Spoofr"
    trk = gpxpy.gpx.GPXTrack(name=name)
    gpx.tracks.append(trk)
    seg = gpxpy.gpx.GPXTrackSegment()
    trk.segments.append(seg)
    for lat, lon in points:
        seg.points.append(gpxpy.gpx.GPXTrackPoint(latitude=lat, longitude=lon))
    return gpx.to_xml()
