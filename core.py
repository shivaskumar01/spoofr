"""Spoofr — device control, the privileged tunnel, route math.

pymobiledevice3 9.x is fully async; Tkinter is sync. Every coroutine runs on one
persistent background loop and the caller blocks until it finishes, so the GUI
never touches asyncio.

The RSD tunnel to an iOS 17+ device can only be created by a root process, so
the app is launched with sudo. connect() then starts and supervises
`pymobiledevice3 remote tunneld` itself (or attaches to one already running);
cleanup() stops it again on exit.

connect() pipeline:
    start/attach tunneld  →  find the USB device  →  mount the Developer Disk
    Image  →  grab the device's RSD tunnel  →  open a LocationSimulation.
"""

from __future__ import annotations

import os
import sys

# pymobiledevice3 shells out to the `ipsw` CLI (installed via Homebrew) to build
# the personalized developer image. Launching with sudo replaces PATH with a
# minimal secure_path that omits Homebrew, so `ipsw` wouldn't be found and the
# mount would stall. Restore the Homebrew dirs *before* importing pymobiledevice3,
# which transitively imports plumbum and snapshots PATH for command lookup.
if getattr(sys, "frozen", False):  # bundled app: find the ipsw binary we ship inside it
    _bundle_bin = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "bin")
    if os.path.isdir(_bundle_bin):
        os.environ["PATH"] = _bundle_bin + os.pathsep + os.environ.get("PATH", "")
for _brew in ("/opt/homebrew/bin", "/usr/local/bin"):
    if os.path.isdir(_brew) and _brew not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _brew

import asyncio
import math
import random
import shutil
import socket
import subprocess
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from pymobiledevice3.exceptions import (
    AlreadyMountedError,
    DeveloperModeIsNotEnabledError,
    TunneldConnectionError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.amfi import AmfiService
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.mobile_image_mounter import auto_mount_personalized
from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS, get_tunneld_devices
from pymobiledevice3.usbmux import list_devices

EARTH_RADIUS_M = 6_371_000.0
TUNNELD_LOG = Path("/tmp/spoofer-tunneld.log")

StatusFn = Callable[[str], None]


class SpooferError(RuntimeError):
    """A problem the user can act on; the message is shown verbatim in the GUI."""


class DeveloperModeRequired(SpooferError):
    """Developer Mode is off on the iPhone — the GUI should run the enable wizard
    rather than show a plain error."""


# --- one background event loop for all device I/O ------------------------

class _Loop:
    """A daemon-thread asyncio loop so sync code can await coroutines."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="spoofer-loop",
                         daemon=True).start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


_loop = _Loop()


# --- the privileged tunnel daemon ---------------------------------------

class _Tunneld:
    """Owns `pymobiledevice3 remote tunneld` — the root process that builds the
    network tunnel to the iPhone.

    If a tunneld is already listening we attach to it and leave it be. Otherwise
    we start one (which needs this app to be root, i.e. launched with sudo) and
    stop it again on cleanup().
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None

    def ensure(self) -> None:
        if _port_open(*TUNNELD_DEFAULT_ADDRESS):
            return  # already up — reuse it, don't take over its lifecycle
        if os.geteuid() != 0:
            raise SpooferError(
                "The tunnel to your iPhone needs root. Relaunch the app with sudo:\n"
                f"    sudo {sys.executable} {Path(sys.argv[0]).resolve()}"
            )
        with TUNNELD_LOG.open("w") as log:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "pymobiledevice3", "remote", "tunneld"],
                stdout=log, stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise SpooferError("The tunnel daemon exited at once:\n" + _tail(TUNNELD_LOG))
            if _port_open(*TUNNELD_DEFAULT_ADDRESS):
                return
            time.sleep(0.3)
        raise SpooferError("The tunnel daemon didn’t come up within 15 seconds.")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


_tunneld = _Tunneld()


def cleanup() -> None:
    """Stop the tunnel daemon if we started it. Safe to call on exit."""
    _tunneld.stop()


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _tail(path: Path, lines: int = 6) -> str:
    try:
        return "\n".join(path.read_text().splitlines()[-lines:]) or "(no output)"
    except OSError:
        return "(no output)"


def _log(msg: str, exc: bool = False) -> None:
    """Append a diagnostic line (and optional traceback) to ~/.spoofr/spoofr.log."""
    try:
        import datetime
        import traceback
        p = Path.home() / ".spoofr" / "spoofr.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}  {msg}\n")
            if exc:
                f.write(traceback.format_exc())
    except Exception:
        pass


def geocode(query: str) -> tuple[float, float]:
    """Look up an address or city → (lat, lon). Raises SpooferError if not found.

    ArcGIS first (works without a key); OpenStreetMap as a fallback. Blocking —
    call from a worker thread.
    """
    import geocoder
    for provider in (geocoder.arcgis, geocoder.osm):
        try:
            result = provider(query)
        except Exception:
            continue
        if result.ok and result.latlng:
            return result.latlng[0], result.latlng[1]
    raise SpooferError(f"Couldn’t find “{query}”. Try a more specific address or city.")


def suggest(query: str, limit: int = 6) -> list[dict]:
    """Autocomplete a place/address/city → up to ``limit`` candidates, each a dict
    {label, secondary, lat, lon}. Uses Photon (free, no key). Blocking — call from a
    worker thread. Returns [] on any error."""
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
        secondary = ", ".join(dict.fromkeys(parts))   # de-dupe, keep order
        if not name:
            name = secondary or "—"
        out.append({"label": name, "secondary": secondary,
                    "lat": float(coords[1]), "lon": float(coords[0])})
    return out


def my_location() -> Optional[tuple[float, float]]:
    """Approximate current location from this Mac's public IP (city-level).

    Returns None if it can't be determined. Blocking — call from a worker thread.
    """
    import geocoder
    try:
        g = geocoder.ip("me")
        if g.ok and g.latlng:
            return g.latlng[0], g.latlng[1]
    except Exception:
        pass
    return None


# --- a live spoofing session --------------------------------------------

@dataclass
class Device:
    name: str
    ios: str
    _location: LocationSimulation
    _stack: AsyncExitStack         # owns the RSD tunnel (closed last)
    _rsd: object                   # RSD connection, to rebuild the DVT/location session
    _loc_stack: AsyncExitStack     # owns the current DVT + LocationSimulation (rebuildable)
    serial: str = ""               # usbmux serial, for liveness checks
    link: str = "USB"              # how the phone is visible: "USB" / "Wi-Fi" / "USB + Wi-Fi"
    wireless_on: bool = False      # EnableWifiConnections — cable-free control available
    _lock: "threading.Lock" = field(default_factory=threading.Lock)

    def set(self, lat: float, lon: float) -> None:
        """Place the iPhone at (lat, lon).

        The instruments (DVT) channel that backs LocationSimulation gets torn down
        if it sits idle (e.g. between connecting and the first 'Set location'),
        surfacing as 'channel is closed'. So on any failure we rebuild the DVT +
        location channels on the existing tunnel and retry once. The lock serializes
        concurrent callers (route + jitter + walk) so a reopen never races a set.
        """
        with self._lock:
            try:
                _loop.run(self._location.set(lat, lon))
            except Exception as first:
                _log(f"location set failed ({first!r}); reopening DVT/location channel", exc=True)
                try:
                    _loop.run(self._reopen())
                    _loop.run(self._location.set(lat, lon))
                except Exception as second:
                    _log(f"reopen+retry failed: {second!r}", exc=True)
                    raise

    async def _reopen(self) -> None:
        """Rebuild the DVT + LocationSimulation channels on the existing RSD tunnel."""
        try:
            await self._loc_stack.aclose()
        except Exception:
            pass
        stack = AsyncExitStack()
        dvt = DvtProvider(self._rsd)
        await asyncio.wait_for(stack.enter_async_context(dvt), 20)
        location = LocationSimulation(dvt)
        await asyncio.wait_for(stack.enter_async_context(location), 20)
        self._loc_stack = stack
        self._location = location

    def clear(self) -> None:
        """Drop the spoof; iOS reacquires the real GPS fix in a few seconds.

        Same idle-channel recovery as set(): the DVT channel may have been torn
        down while sitting connected, so on failure rebuild it and retry once.
        """
        with self._lock:
            try:
                _loop.run(self._location.clear())
            except Exception as first:
                _log(f"location clear failed ({first!r}); reopening DVT/location channel", exc=True)
                try:
                    _loop.run(self._reopen())
                    _loop.run(self._location.clear())
                except Exception as second:
                    _log(f"reopen+retry failed: {second!r}", exc=True)
                    raise

    def play_route(self, points, speed_mps, stop: threading.Event,
                   dt: float = 1.0, on_step: Optional[Callable] = None,
                   loop: bool = False, bounce: bool = False) -> None:
        """Walk `points` at speed_mps, one fix every `dt` seconds.

        With `bounce` the path is walked forward then back; with `loop` (or
        `bounce`) it repeats until `stop` is set, otherwise it returns at the
        end. Call from a worker thread — it sleeps between fixes.
        """
        path = route_points(points, speed_mps, dt)
        if not path:
            return
        seq = path + path[-2::-1] if bounce else path   # forward, then back
        repeat = loop or bounce
        total = len(seq)
        while True:
            for i, (lat, lon) in enumerate(seq, 1):
                if stop.is_set():
                    return
                self.set(lat, lon)
                if on_step:
                    on_step(i, total, lat, lon)
                if stop.wait(dt):
                    return
            if not repeat:
                return

    def close(self, clear: bool = True) -> None:
        """Tear down the DVT/location session + tunnel. With clear=False the
        simulated location is LEFT ACTIVE on the iPhone (it persists until the user
        resets it or the device reboots) — used when disconnecting on purpose."""
        async def shutdown():
            if clear:
                try:
                    await self._location.clear()
                except Exception:
                    pass
            try:
                await self._loc_stack.aclose()
            finally:
                await self._stack.aclose()
        try:
            _loop.run(shutdown())
        except Exception:
            pass


def connect(on_status: Optional[StatusFn] = None) -> Device:
    """Start/attach the tunnel and open a spoofing session against the iPhone.

    Launch the app with sudo so the tunnel can be created. `on_status`, if given,
    receives short progress strings and may be called from a background thread.
    """
    def say(msg: str) -> None:
        if on_status:
            on_status(msg)

    say("Starting the tunnel…")
    _tunneld.ensure()
    return _loop.run(_open(say))


def device_present(serial: str) -> bool:
    """True if the iPhone with this usbmux serial is still connected (USB/Wi-Fi).
    Used to notice an unplug. Blocking — call from a worker thread."""
    if not serial:
        return True
    try:
        return _loop.run(_device_present(serial))
    except Exception:
        return False


async def _device_present(serial: str) -> bool:
    try:
        devices = await list_devices()
    except Exception:
        return True      # transient usbmux hiccup — don't declare a disconnect
    return any(getattr(d, "serial", None) == serial for d in devices)


def _kinds_to_link(kinds: set[str]) -> str:
    has_usb = "USB" in kinds
    has_net = "Network" in kinds
    if has_usb and has_net:
        return "USB + Wi-Fi"
    return "USB" if has_usb else ("Wi-Fi" if has_net else "")


def link_status(serial: str) -> Optional[str]:
    """How this iPhone is visible right now: "USB" / "Wi-Fi" / "USB + Wi-Fi",
    "" if it's definitively gone, or None on a transient usbmux hiccup (treat as
    no-information, not a disconnect). Blocking — call from a worker thread."""
    if not serial:
        return None
    try:
        return _loop.run(_link_status(serial))
    except Exception:
        return None


async def _link_status(serial: str) -> Optional[str]:
    try:
        devices = await list_devices()
    except Exception:
        return None
    kinds = {d.connection_type for d in devices if getattr(d, "serial", None) == serial}
    return _kinds_to_link(kinds)


def visible_kinds() -> str:
    """How *any* iPhone is visible right now ("" if none) — the idle pre-flight
    that lights up Connect before the user clicks. Blocking — worker thread."""
    try:
        return _loop.run(_visible_kinds())
    except Exception:
        return ""


async def _visible_kinds() -> str:
    try:
        devices = await list_devices()
    except Exception:
        return ""
    return _kinds_to_link({d.connection_type for d in devices})


def enable_wireless() -> None:
    """One-time switch: tell the iPhone to stay reachable over Wi-Fi (the classic
    "Show this iPhone when on Wi-Fi"). Needs the cable for this one call; after
    it, discovery/lockdown/tunnel all work cable-free on the same network.
    Raises SpooferError with a user-facing message on failure."""
    _loop.run(_enable_wireless())


async def _enable_wireless():
    try:
        devices = await list_devices()
    except Exception:
        devices = []
    usb = [d for d in devices if d.connection_type == "USB"]
    if not usb:
        raise SpooferError("Plug the iPhone in with the cable for this one-time switch "
                           "(unlock it first), then click again.")
    lockdown = await _bounded(create_using_usbmux(usb[0].serial), 15,
                              "The iPhone didn’t respond. Unlock it and try again.")
    try:
        await asyncio.wait_for(lockdown.set_enable_wifi_connections(True), 15)
        on = bool(await asyncio.wait_for(lockdown.get_enable_wifi_connections(), 15))
        if not on:
            raise SpooferError("The iPhone didn’t confirm the switch — unlock it and try again.")
    except asyncio.TimeoutError:
        raise SpooferError("The iPhone didn’t respond. Unlock it and try again.") from None
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass


def developer_mode_status() -> Optional[bool]:
    """True/False if an iPhone is connected, else None. Over USB — no root/tunnel."""
    return _loop.run(_dev_mode_status())


async def _dev_mode_status() -> Optional[bool]:
    devices = await list_devices()
    if not devices:
        return None
    try:
        lockdown = await create_using_usbmux(devices[0].serial)
    except Exception:
        return None
    try:
        return bool(await lockdown.get_developer_mode_status())
    except Exception:
        return None
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass


def reveal_developer_mode() -> None:
    """Surface the (hidden) Developer Mode toggle in the iPhone's Settings."""
    try:
        _loop.run(_reveal_dev_mode())
    except Exception:
        pass


async def _reveal_dev_mode() -> None:
    devices = await list_devices()
    if not devices:
        return
    lockdown = await create_using_usbmux(devices[0].serial)
    try:
        await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass


async def _open(say: StatusFn) -> Device:
    say("Looking for your iPhone…")
    muxed = await _bounded(list_devices(), 10,
                           "Couldn’t reach usbmuxd (the USB device service).")
    if not muxed:
        raise SpooferError("No iPhone reachable. Plug it in and tap “Trust”, or go "
                           "cable-free: menu ▸ Settings ▸ “Go wireless” (one-time, with "
                           "the cable in), then stay on the same Wi-Fi.")
    # prefer the cable when both links exist — faster and steadier for the
    # lockdown/mount phase; Wi-Fi-only devices still work
    muxed.sort(key=lambda d: d.connection_type != "USB")
    serial = muxed[0].serial
    link = _kinds_to_link({d.connection_type for d in muxed
                           if getattr(d, "serial", None) == serial}) or "USB"

    lockdown = await _bounded(create_using_usbmux(serial), 15,
                             "The iPhone didn’t respond. Re-plug it, unlock it, and trust this Mac.")
    try:
        name = lockdown.all_values.get("DeviceName", "iPhone")
        ios = lockdown.product_version
        udid = lockdown.udid or serial
        try:
            wireless_on = bool(await asyncio.wait_for(lockdown.get_enable_wifi_connections(), 5))
        except Exception:
            wireless_on = False
        say("Checking Developer Mode…")
        try:
            dev_mode_on = bool(await lockdown.get_developer_mode_status())
        except Exception:
            dev_mode_on = True  # query unsupported → let the mount decide
        if not dev_mode_on:
            raise DeveloperModeRequired("Developer Mode is off on the iPhone.")
        say("Preparing the developer disk image (first time can take a minute)…")
        try:
            await asyncio.wait_for(auto_mount_personalized(lockdown), 60)
        except asyncio.TimeoutError:
            raise SpooferError(_mount_timeout_message(ios)) from None
        except AlreadyMountedError:
            pass
        except DeveloperModeIsNotEnabledError:
            raise DeveloperModeRequired("Developer Mode is off on the iPhone.") from None
    finally:
        await lockdown.close()

    rsd = await _wait_for_rsd(udid, say)

    say("Opening the location service…")
    stack = AsyncExitStack()          # owns the RSD tunnel (closes last)
    loc_stack = AsyncExitStack()      # owns DVT + location (rebuildable if the channel idles out)
    try:
        stack.push_async_callback(rsd.close)
        dvt = DvtProvider(rsd)
        await _bounded(loc_stack.enter_async_context(dvt), 20,
                       "Timed out opening the developer-services session.")
        location = LocationSimulation(dvt)
        await _bounded(loc_stack.enter_async_context(location), 20,
                       "Timed out opening the location service.")
    except Exception:
        await loc_stack.aclose()
        await stack.aclose()
        raise

    return Device(name=name, ios=ios, _location=location, _stack=stack,
                  _rsd=rsd, _loc_stack=loc_stack, serial=serial,
                  link=link, wireless_on=wireless_on)


async def _bounded(coro, seconds: float, message: str):
    """Await `coro`, but turn a hang into a clear SpooferError instead of freezing."""
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError:
        raise SpooferError(message) from None


def _mount_timeout_message(ios: str) -> str:
    msg = [f"Mounting the developer disk image timed out (iOS {ios})."]
    if shutil.which("ipsw") is None:
        msg.append(
            "\nThe `ipsw` CLI that pymobiledevice3 needs to build the image isn’t "
            "installed. Install it, then click Connect again:\n"
            "    brew install blacktop/tap/ipsw"
        )
    else:
        msg.append(
            "\nYour iOS version may be newer than this pymobiledevice3 can prepare a "
            "developer image for, or Apple/GitHub was unreachable. Try again on a "
            "stable network; if it keeps failing, pymobiledevice3 needs an update."
        )
    return "\n".join(msg)


async def _wait_for_rsd(udid: str, say: StatusFn, timeout: float = 25.0):
    """Wait for tunneld to publish a tunnel for this iPhone and return its RSD.

    tunneld needs a few seconds to discover the device after it starts, so we
    poll. Connections to any other device are closed so nothing leaks.
    """
    say("Waiting for the tunnel to find your iPhone…")
    deadline = time.monotonic() + timeout
    while True:
        try:
            tunnels = await asyncio.wait_for(get_tunneld_devices(), 8)
        except (TunneldConnectionError, asyncio.TimeoutError):
            tunnels = []
        rsd = next((t for t in tunnels if getattr(t, "udid", None) == udid), None)
        for other in tunnels:
            if other is not rsd:
                await _close_quietly(other)
        if rsd is not None:
            return rsd
        if time.monotonic() >= deadline:
            raise SpooferError(
                "The tunnel is up but hasn’t found this iPhone. Make sure it’s "
                "unlocked and trusted, then click Connect again."
            )
        await asyncio.sleep(0.5)


async def _close_quietly(rsd) -> None:
    try:
        await rsd.close()
    except Exception:
        pass


# --- route math ----------------------------------------------------------

def _meters(a, b) -> float:
    """Great-circle distance between two (lat, lon) points, in metres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def route_points(points, speed_mps: float, dt: float = 1.0):
    """Fixes to emit so the polyline is walked at `speed_mps`, one per `dt` seconds.

    Straight-line interpolation between waypoints: over the distances anyone
    actually walks or drives, the gap to a true great-circle path is far below
    GPS noise, and the code stays trivially correct.
    """
    if speed_mps <= 0 or dt <= 0:
        raise ValueError("speed_mps and dt must be positive")
    if len(points) < 2:
        return list(points)

    step = speed_mps * dt
    path = [tuple(points[0])]
    for a, b in zip(points, points[1:]):
        steps = max(1, math.ceil(_meters(a, b) / step))
        for i in range(1, steps + 1):
            if i == steps:
                path.append(tuple(b))  # land exactly on the waypoint
            else:
                f = i / steps
                path.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return path


def jitter(lat: float, lon: float, radius_m: float = 4.0) -> tuple[float, float]:
    """Nudge a point by a random offset within `radius_m` metres, so a held
    position wobbles like a real GPS fix instead of sitting perfectly still."""
    r = radius_m * math.sqrt(random.random())          # uniform over the disc
    theta = random.uniform(0.0, 2.0 * math.pi)
    dlat = (r * math.cos(theta)) / 111_320.0
    dlon = (r * math.sin(theta)) / (111_320.0 * max(0.15, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def snap_to_roads(points, profile: str = "driving", timeout: float = 8.0):
    """Replace the straight segments between waypoints with the real road
    geometry (OSRM public server). Returns the densified [(lat,lon),…]; on any
    failure returns `points` unchanged so routing still works. Blocking — call
    from a worker thread."""
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
    """Read a .gpx file → [(lat,lon),…] from its first track (else route, else
    waypoints). Raises SpooferError if there's nothing usable."""
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
        raise SpooferError("That GPX file has no usable track (need at least two points).")
    return pts


def build_gpx(points, name: str = "Spoofr route") -> str:
    """Serialize [(lat,lon),…] to a GPX 1.1 XML string (one track)."""
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
