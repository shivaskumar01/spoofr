"""Address search / geocoding, pure HTTP, no device stack.

Kept separate from core.py so the search box never pulls in pymobiledevice3.
Mirrors core's behaviour: Photon autocomplete, ArcGIS→OSM geocoding, and a
lat,lon parser. Call from a worker thread (blocking network).
"""

from __future__ import annotations

import re


class Offline(RuntimeError):
    """No internet: say so, and what still works."""


OFFLINE_SEARCH = ("You’re offline. Address search needs the internet, but you can still "
                  "paste coordinates like 33.4242, -111.9281.")

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_QUOTES = str.maketrans({"′": "'", "’": "'", "‘": "'", "″": '"', "”": '"', "“": '"',
                         "º": "°", "˚": "°"})


def _coord(part: str):
    """One coordinate: decimal, degrees-minutes or DMS, signed or N/S/E/W."""
    hemis = re.findall(r"[NSEW]", part.upper())
    if len(hemis) > 1:
        return None
    nums = _NUM.findall(part)
    if not 1 <= len(nums) <= 3:
        return None
    d = float(nums[0])
    neg = d < 0 or part.strip().startswith("-")
    m = float(nums[1]) if len(nums) > 1 else 0.0
    sec = float(nums[2]) if len(nums) > 2 else 0.0
    if m >= 60 or sec >= 60 or m < 0 or sec < 0:
        return None
    v = abs(d) + m / 60.0 + sec / 3600.0
    h = hemis[0] if hemis else None
    if h in ("S", "W") or (h is None and neg):
        v = -v
    return v, h


def parse_coords(s: str | None):
    """'33.4242, -111.9281', '33.4242° N, 111.9281° W', '33°25\'27"N 111°55\'41"W'
    -> (lat, lon), range-checked; anything else (an address) -> None."""
    s = (s or "").translate(_QUOTES).strip()
    if not s or re.search(r"[A-DF-MO-RT-VX-Za-df-mo-rt-vx-z]", s):
        return None                     # letters other than N/S/E/W: an address
    letters = [(m.start(), m.group().upper()) for m in re.finditer(r"[NSEWnsew]", s)]
    if s.count(",") == 1:
        parts = s.split(",")
    elif len(letters) == 2:
        (i0, _), (i1, _) = letters
        cut = i0 + 1 if s[:i0].strip() else i1      # trailing "33N 111W" / leading "N33 W111"
        parts = [s[:cut], s[cut:]]
    elif not letters and s.count(",") == 0:
        nums = _NUM.findall(s)
        if len(nums) not in (2, 4, 6):
            return None
        half = len(nums) // 2
        parts = [" ".join(nums[:half]), " ".join(nums[half:])]
    else:
        return None
    a, b = _coord(parts[0]), _coord(parts[1])
    if not a or not b:
        return None
    (va, ha), (vb, hb) = a, b
    if ha in ("E", "W") or hb in ("N", "S"):
        va, vb, ha, hb = vb, va, hb, ha              # "111°W, 33°N": longitude first
    if (ha and ha not in ("N", "S")) or (hb and hb not in ("E", "W")):
        return None
    lat, lon = va, vb
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


def reverse(lat: float, lon: float) -> str:
    """(lat, lon) -> a short human name ('Rockefeller Center, New York'), or ""."""
    import requests
    try:
        r = requests.get("https://photon.komoot.io/reverse",
                         params={"lat": lat, "lon": lon, "lang": "en", "limit": 1},
                         headers={"User-Agent": "Spoofr/1.0 (macOS location utility)"}, timeout=4)
        feats = r.json().get("features", [])
    except Exception:
        return ""
    if not feats:
        return ""
    p = feats[0].get("properties", {})
    street = " ".join(x for x in (p.get("housenumber"), p.get("street")) if x)
    name = p.get("name") or street
    place = p.get("city") or p.get("district") or p.get("county") or p.get("state") or ""
    if name and place and place != name:
        return f"{name}, {place}"
    return name or place or ""


def geocode(query: str) -> tuple[float, float]:
    """Address/place -> (lat, lon). Raises Offline with no internet, or
    RuntimeError if the place can't be found."""
    import requests
    try:
        r = requests.get("https://photon.komoot.io/api/",
                         params={"q": query, "limit": 1, "lang": "en"},
                         headers={"User-Agent": "Spoofr/1.1 (macOS location utility)"}, timeout=6)
        feats = r.json().get("features", [])
        if feats:
            c = feats[0]["geometry"]["coordinates"]
            return float(c[1]), float(c[0])
    except (requests.ConnectionError, requests.Timeout):
        raise Offline(OFFLINE_SEARCH) from None
    except Exception:
        pass
    try:
        import geocoder
        result = geocoder.arcgis(query)
        if result.ok and result.latlng:
            return result.latlng[0], result.latlng[1]
    except Exception:
        pass
    raise RuntimeError(f"Couldn’t find “{query}”. Try a more specific address or city.")


def current_location(timeout: float = 8.0):
    """Where is this Mac (the phone is right next to it)?

    Precise via macOS CoreLocation when Spoofr runs as the signed .app bundle, one
    native 'Allow' prompt, then ~Wi-Fi accuracy. In a plain `python -m qtui` run,
    CoreLocation is silently denied by macOS, so that path is skipped instantly and
    IP geolocation (ip-api → ipinfo, city-level) carries it with zero setup. iOS
    exposes no way to read the phone's own GPS over the dev tunnel.

    Returns (lat, lon) or None. Blocking, call from a worker thread.
    """
    return _mac_location(timeout) or _ip_location()


def _mac_location(timeout: float):
    """Precise CoreLocation fix, only attempted in the bundle (where it can work)."""
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
