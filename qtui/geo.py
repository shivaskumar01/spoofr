"""Address search / geocoding — pure HTTP, no device stack.

Kept separate from core.py so the search box never pulls in pymobiledevice3.
Mirrors core's behaviour: Photon autocomplete, ArcGIS→OSM geocoding, and a
lat,lon parser. Call from a worker thread (blocking network).
"""

from __future__ import annotations

import re


def parse_coords(s: str | None):
    """'37.77, -122.41' -> (lat, lon), validated; else None."""
    m = re.fullmatch(r"\s*(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*", s or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def suggest(query: str, limit: int = 6) -> list[dict]:
    """Autocomplete -> up to `limit` {label, secondary, lat, lon}. [] on error."""
    import requests
    try:
        r = requests.get("https://photon.komoot.io/api/",
                         params={"q": query, "limit": limit, "lang": "en"},
                         headers={"User-Agent": "Spoofr/1.0 (macOS location utility)"}, timeout=4)
        feats = r.json().get("features", [])
    except Exception:
        return []
    out: list[dict] = []
    for f in feats:
        p = f.get("properties", {})
        coords = (f.get("geometry") or {}).get("coordinates")
        if not coords or len(coords) < 2:
            continue
        name = p.get("name") or p.get("street") or p.get("city") or ""
        parts = [p.get(k) for k in ("street", "city", "state", "country")
                 if p.get(k) and p.get(k) != name]
        secondary = ", ".join(dict.fromkeys(parts))
        if not name:
            name = secondary or "—"
        out.append({"label": name, "secondary": secondary,
                    "lat": float(coords[1]), "lon": float(coords[0])})
    return out


def geocode(query: str) -> tuple[float, float]:
    """Address/city -> (lat, lon). Raises if not found."""
    import geocoder
    for provider in (geocoder.arcgis, geocoder.osm):
        try:
            result = provider(query)
        except Exception:
            continue
        if result.ok and result.latlng:
            return result.latlng[0], result.latlng[1]
    raise RuntimeError(f"Couldn’t find “{query}”. Try a more specific address or city.")


def current_location(timeout: float = 8.0):
    """Where is this Mac (the phone is right next to it)?

    Precise via macOS CoreLocation when Spoofr runs as the signed .app bundle — one
    native 'Allow' prompt, then ~Wi-Fi accuracy. In a plain `python -m qtui` run,
    CoreLocation is silently denied by macOS, so that path is skipped instantly and
    IP geolocation (ip-api → ipinfo, city-level) carries it with zero setup. iOS
    exposes no way to read the phone's own GPS over the dev tunnel.

    Returns (lat, lon) or None. Blocking — call from a worker thread.
    """
    return _mac_location(timeout) or _ip_location()


def _mac_location(timeout: float):
    """Precise CoreLocation fix — only attempted in the bundle (where it can work)."""
    import sys
    if not getattr(sys, "frozen", False):   # script run: macOS denies it → don't stall
        return None
    try:
        import time as _t
        from CoreLocation import CLLocationManager
        from Foundation import NSDate, NSRunLoop
    except Exception:
        return None
    try:
        if not CLLocationManager.locationServicesEnabled():
            return None
        if CLLocationManager.authorizationStatus() == 2:    # the user chose Deny
            return None
        mgr = CLLocationManager.alloc().init()
        mgr.requestWhenInUseAuthorization()                 # shows the prompt once
        mgr.startUpdatingLocation()
        rl = NSRunLoop.currentRunLoop()
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            loc = mgr.location()
            if loc is not None:
                c = loc.coordinate()
                mgr.stopUpdatingLocation()
                return float(c.latitude), float(c.longitude)
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        mgr.stopUpdatingLocation()
    except Exception:
        pass
    return None


def _ip_location():
    import requests
    headers = {"User-Agent": "Spoofr/1.0 (macOS location utility)"}
    for url, pick in (
        ("http://ip-api.com/json",
         lambda d: (d["lat"], d["lon"]) if d.get("status") == "success" else None),
        ("https://ipinfo.io/json",
         lambda d: tuple(d["loc"].split(",")) if d.get("loc") else None),
    ):
        try:
            d = requests.get(url, timeout=4, headers=headers).json()
            xy = pick(d)
            if xy and xy[0] is not None:
                return float(xy[0]), float(xy[1])
        except Exception:
            continue
    return None
