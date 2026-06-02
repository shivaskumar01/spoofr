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
    """Best available 'where is this Mac' (the phone is right next to it).

    CoreLocationCLI first — precise Wi-Fi positioning, if it's installed and the
    user granted Location Services (`brew install corelocationcli`). Otherwise IP
    geolocation (ip-api, then ipinfo) — region-level, but far better than ipinfo
    alone for many ISPs. Returns (lat, lon) or None. Blocking — worker thread only.

    iOS exposes no way to read the phone's real GPS over the developer tunnel, so
    the Mac's own location is the closest available proxy.
    """
    return _corelocationcli(timeout) or _ip_location()


def _corelocationcli(timeout: float):
    """Precise fix via the CoreLocationCLI helper, if installed + permitted."""
    import os
    import shutil
    import subprocess
    exe = shutil.which("CoreLocationCLI")
    if not exe:                          # GUI-launched apps often lack /opt/homebrew/bin on PATH
        for cand in ("/opt/homebrew/bin/CoreLocationCLI", "/usr/local/bin/CoreLocationCLI"):
            if os.path.exists(cand):
                exe = cand
                break
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--format", "%latitude %longitude"],
                             capture_output=True, text=True, timeout=timeout)
        nums = []
        for tok in out.stdout.replace(",", " ").split():
            try:
                nums.append(float(tok))
            except ValueError:
                pass
        # must look like a real coordinate (guards against error text / partial output)
        if len(nums) >= 2 and -90 <= nums[0] <= 90 and -180 <= nums[1] <= 180 \
                and (nums[0] or nums[1]):
            return nums[0], nums[1]
    except Exception:
        pass
    return None


def _ip_location():
    """IP geolocation. ip-api.com first (more accurate for many consumer ISPs
    than ipinfo), ipinfo.io as a fallback. Region-level at best."""
    import requests
    headers = {"User-Agent": "Spoofr/1.0 (macOS location utility)"}
    try:
        d = requests.get("http://ip-api.com/json", timeout=4, headers=headers).json()
        if d.get("status") == "success" and d.get("lat") is not None:
            return float(d["lat"]), float(d["lon"])
    except Exception:
        pass
    try:
        d = requests.get("https://ipinfo.io/json", timeout=4, headers=headers).json()
        if d.get("loc"):
            lat, lon = d["loc"].split(",")
            return float(lat), float(lon)
    except Exception:
        pass
    return None
