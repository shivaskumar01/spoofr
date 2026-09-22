"""Spoofr, device control and the privileged tunnel.

Purely the device engine: nothing here knows about maps, routes or geocoding
(qtui/route.py and qtui/geo.py own those, so editing a route never drags in
pymobiledevice3).

pymobiledevice3 9.x is fully async; the UI is not. Every coroutine runs on one
persistent background loop and the caller blocks until it finishes, so no
front-end ever touches asyncio. Every one of those waits is bounded: an
unbounded device call parks its caller while holding Device._lock, which used to
freeze Restore GPS, the panic hotkey and app quit along with it.

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
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import CancelledError
from concurrent.futures import TimeoutError as _FutureTimeout
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from applog import log as _log

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

# Same file portable.py points the privileged daemon at — these were spelled
# differently ("spoofer" vs "spoofr"), so every diagnostic here read a file
# that did not exist while the real errors piled up in the other one.
TUNNELD_LOG = Path("/tmp/spoofr-tunneld.log")

# Every device round-trip is bounded. Without these a single wedged call blocks
# its caller forever *while holding Device._lock*, which freezes Restore GPS, the
# panic hotkey and app quit along with it (see ~/.spoofr/spoofr.log, 2026-06-03).
SET_TIMEOUT = 6.0        # one LocationSimulation.set round-trip
CLEAR_TIMEOUT = 8.0      # clearing the spoof: worth waiting a little longer
REOPEN_TIMEOUT = 12.0    # rebuilding the DVT + location channels
CLOSE_TIMEOUT = 5.0      # tearing the session down on disconnect/quit
LOCK_TIMEOUT = 30.0      # ceiling on waiting for another caller's device op
MOUNT_TIMEOUT = 300.0    # developer disk image, including a download on a new iOS
RSD_TIMEOUT = 45.0       # how long to let the daemon find and tunnel this phone
USBMUX_TIMEOUT = 5.0     # listing devices: a local socket, should be instant
LOCKDOWN_TIMEOUT = 15.0  # a lockdown round-trip (dev-mode status, reveal, wireless)
# the whole connect pipeline, as a backstop over the per-step bounds above
CONNECT_TIMEOUT = MOUNT_TIMEOUT + RSD_TIMEOUT + 120.0

StatusFn = Callable[[str], None]


class SpooferError(RuntimeError):
    """A problem the user can act on; the message is shown verbatim in the GUI."""


class DeveloperModeRequired(SpooferError):
    """Developer Mode is off on the iPhone, the GUI should run the enable wizard
    rather than show a plain error."""


class NoDeviceFound(SpooferError):
    """usbmux can't see an iPhone right now. Often transient — a phone that has
    only just been plugged in takes a moment to enumerate."""


class TunnelNotReady(SpooferError):
    """The daemon is running but hasn't opened a tunnel to this iPhone yet.
    Worth retrying, and worth restarting the daemon over; never a trust problem."""


class Cancelled(SpooferError):
    """A device call was cut short by ``Device.suspend()`` — a Restore GPS or a
    stop overtook it. Not a session failure: the caller should simply stop, not
    reconnect."""


# --- one background event loop for all device I/O ------------------------

class _Loop:
    """A daemon-thread asyncio loop so sync code can await coroutines."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="spoofer-loop",
                         daemon=True).start()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def wait(self, fut, timeout: Optional[float] = None):
        """Block on `fut`; on expiry cancel the coroutine and raise, so no caller
        can be stuck behind a device that stopped answering."""
        try:
            return fut.result(timeout)
        except _FutureTimeout:
            if fut.done():          # the coroutine itself raised a TimeoutError
                raise
            fut.cancel()
            raise SpooferError("The iPhone stopped responding.") from None
        except CancelledError:
            raise Cancelled("The device call was cancelled.") from None

    def run(self, coro, timeout: Optional[float] = None):
        return self.wait(self.submit(coro), timeout)


_loop = _Loop()


# --- the privileged tunnel daemon ---------------------------------------

class _Tunneld:
    """Owns `pymobiledevice3 remote tunneld`, the root process that builds the
    network tunnel to the iPhone.

    If a tunneld is already listening we attach to it and leave it be. Otherwise
    we start one (which needs this app to be root, i.e. launched with sudo) and
    stop it again on cleanup().
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None

    def ensure(self) -> None:
        if _port_open(*TUNNELD_DEFAULT_ADDRESS):
            return  # already up, reuse it, don't take over its lifecycle
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


def ensure_tunneld() -> None:
    """Start (or attach to) the tunnel daemon now, used by the headless host to
    pre-warm it at boot so the phone's first Connect doesn't pay the wait."""
    _tunneld.ensure()


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _tunneld_tail(lines: int = 4) -> str:
    """The last few *interesting* lines of the tunnel daemon's log.

    It writes an access line for every poll, and the app polls twice a second, so
    a plain tail is all routine traffic: the error dialog showed three "GET /"
    lines and nothing about why the tunnel failed, while the real message sat a
    few hundred lines above it.
    """
    try:
        raw = TUNNELD_LOG.read_text().splitlines()
    except OSError:
        return "(the tunnel daemon has not written a log)"
    noise = re.compile(r'"(?:GET|POST|PUT|DELETE) \S+ HTTP/[\d.]+"')
    kept = [ln for ln in raw if ln.strip() and not noise.search(ln)]
    return "\n".join(kept[-lines:]) or "(nothing but routine polling)"


def _tail(path: Path, lines: int = 6) -> str:
    try:
        return "\n".join(path.read_text().splitlines()[-lines:]) or "(no output)"
    except OSError:
        return "(no output)"


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
    wireless_on: bool = False      # EnableWifiConnections, cable-free control available
    _lock: "threading.Lock" = field(default_factory=threading.Lock)
    _epoch: int = 0                # bumped by suspend(); invalidates in-flight set()s
    _inflight: object = None       # the device call running right now, so suspend() can cut it

    # --- call plumbing ---------------------------------------------------

    def suspend(self) -> None:
        """Invalidate every queued and in-flight set(), used by Restore GPS and by
        stopping a route or walk.

        Without this a fix that was already in flight can land *after* clear() and
        silently re-spoof the phone the user just restored. Bumping the epoch drops
        the queued callers; cancelling the running future means a wedged call
        doesn't make the user wait out its whole timeout budget."""
        self._epoch += 1
        fut = self._inflight
        if fut is not None:
            try:
                fut.cancel()
            except Exception:
                pass

    def _run(self, coro, timeout: float):
        """Run one device coroutine, bounded, and recorded so suspend() can cut it."""
        fut = _loop.submit(coro)
        self._inflight = fut
        try:
            return _loop.wait(fut, timeout)
        finally:
            self._inflight = None

    def _acquire(self, what: str):
        if not self._lock.acquire(timeout=LOCK_TIMEOUT):
            raise SpooferError(f"The iPhone is busy and didn’t free up (while trying to {what}).")

    def set(self, lat: float, lon: float) -> None:
        """Place the iPhone at (lat, lon).

        The instruments (DVT) channel that backs LocationSimulation gets torn down
        if it sits idle (e.g. between connecting and the first 'Set location'),
        surfacing as 'channel is closed'. So on any failure we rebuild the DVT +
        location channels on the existing tunnel and retry once. The lock serializes
        concurrent callers (route + jitter + walk) so a reopen never races a set.

        Every step is time-bounded, so a dead tunnel surfaces as an exception the
        caller can act on instead of a thread parked forever holding the lock.
        """
        epoch = self._epoch
        self._acquire("set the location")
        try:
            if epoch != self._epoch:
                return                      # a stop/Restore overtook this fix
            try:
                self._run(self._location.set(lat, lon), SET_TIMEOUT)
            except Cancelled:
                return
            except Exception as first:
                _log(f"location set failed ({first!r}); reopening DVT/location channel", exc=True)
                try:
                    self._run(self._reopen(), REOPEN_TIMEOUT)
                    self._run(self._location.set(lat, lon), SET_TIMEOUT)
                except Cancelled:
                    return
                except Exception as second:
                    _log(f"reopen+retry failed: {second!r}", exc=True)
                    raise
        finally:
            self._lock.release()

    async def _reopen(self) -> None:
        """Rebuild the DVT + LocationSimulation channels on the existing RSD tunnel."""
        try:
            await self._loc_stack.aclose()
        except Exception:
            pass
        stack = AsyncExitStack()
        dvt = DvtProvider(self._rsd)
        await asyncio.wait_for(stack.enter_async_context(dvt), 8)
        location = LocationSimulation(dvt)
        await asyncio.wait_for(stack.enter_async_context(location), 8)
        self._loc_stack = stack
        self._location = location

    def clear(self) -> None:
        """Drop the spoof; iOS reacquires the real GPS fix in a few seconds.

        suspend() runs first so no in-flight or queued fix can re-spoof the phone
        behind us. Same idle-channel recovery as set(): the DVT channel may have
        been torn down while sitting connected, so on failure rebuild and retry once.

        The clear itself is deliberately not registered with suspend(): suspend
        exists to cut *fixes* short, and a stop or a second panic press landing
        mid-restore used to cancel the restore instead, which then read as a
        dead session and kicked off a reconnect.
        """
        self.suspend()
        self._acquire("restore real GPS")
        try:
            try:
                _loop.run(self._location.clear(), CLEAR_TIMEOUT)
            except Exception as first:
                _log(f"location clear failed ({first!r}); reopening DVT/location channel", exc=True)
                try:
                    _loop.run(self._reopen(), REOPEN_TIMEOUT)
                    _loop.run(self._location.clear(), CLEAR_TIMEOUT)
                except Exception as second:
                    _log(f"reopen+retry failed: {second!r}", exc=True)
                    raise
        finally:
            self._lock.release()

    def close(self, clear: bool = True) -> None:
        """Tear down the DVT/location session + tunnel. With clear=False the
        simulated location is LEFT ACTIVE on the iPhone (it persists until the user
        resets it or the device reboots), used when disconnecting on purpose.

        Bounded and suspend-first: quitting the app must never block on a device
        that stopped answering."""
        self.suspend()
        got = self._lock.acquire(timeout=CLOSE_TIMEOUT)
        try:
            async def shutdown():
                if clear:
                    try:
                        await asyncio.wait_for(self._location.clear(), CLEAR_TIMEOUT)
                    except Exception:
                        pass
                try:
                    await self._loc_stack.aclose()
                finally:
                    await self._stack.aclose()
            try:
                self._run(shutdown(), CLOSE_TIMEOUT + CLEAR_TIMEOUT)
            except Exception as e:
                _log(f"close failed ({e!r}); the session is dropped anyway")
        finally:
            if got:
                self._lock.release()


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
    return _loop.run(_open(say), CONNECT_TIMEOUT)


def _kinds_to_link(kinds: set[str]) -> str:
    has_usb = "USB" in kinds
    has_net = "Network" in kinds
    if has_usb and has_net:
        return "USB + Wi-Fi"
    return "USB" if has_usb else ("Wi-Fi" if has_net else "")


def link_status(serial: str) -> Optional[str]:
    """How this iPhone is visible right now: "USB" / "Wi-Fi" / "USB + Wi-Fi",
    "" if it's definitively gone, or None on a transient usbmux hiccup (treat as
    no-information, not a disconnect). Blocking, call from a worker thread."""
    if not serial:
        return None
    try:
        return _loop.run(_link_status(serial), USBMUX_TIMEOUT)
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
    """How *any* iPhone is visible right now ("" if none), the idle pre-flight
    that lights up Connect before the user clicks. Blocking, worker thread."""
    try:
        return _loop.run(_visible_kinds(), USBMUX_TIMEOUT)
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
    _loop.run(_enable_wireless(), 3 * LOCKDOWN_TIMEOUT + USBMUX_TIMEOUT)


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
            raise SpooferError("The iPhone didn’t confirm the switch, unlock it and try again.")
    except asyncio.TimeoutError:
        raise SpooferError("The iPhone didn’t respond. Unlock it and try again.") from None
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass


def developer_mode_status() -> Optional[bool]:
    """True/False if an iPhone is connected, else None. Over USB, no root/tunnel.
    None too if the phone doesn't answer in time: the wizard just polls again."""
    try:
        return _loop.run(_dev_mode_status(), LOCKDOWN_TIMEOUT)
    except SpooferError:
        return None


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
        _loop.run(_reveal_dev_mode(), LOCKDOWN_TIMEOUT)
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
        raise NoDeviceFound("No iPhone reachable. Plug it in and tap “Trust”, or go "
                            "cable-free: menu ▸ Settings ▸ “Go wireless” (one-time, with "
                            "the cable in), then stay on the same Wi-Fi.")
    # prefer the cable when both links exist, faster and steadier for the
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
            dev_mode_on = bool(await asyncio.wait_for(
                lockdown.get_developer_mode_status(), LOCKDOWN_TIMEOUT))
        except Exception:
            dev_mode_on = True  # query unsupported (or slow) → let the mount decide
        if not dev_mode_on:
            raise DeveloperModeRequired("Developer Mode is off on the iPhone.")
        say("Preparing the developer disk image (first time can take a few minutes)…")
        try:
            # generous: on a new iOS release this downloads a fresh image, and a
            # 60s cap turned a slow connection into a hard failure
            await asyncio.wait_for(auto_mount_personalized(lockdown), MOUNT_TIMEOUT)
        except asyncio.TimeoutError:
            raise SpooferError(_mount_timeout_message(ios)) from None
        except AlreadyMountedError:
            pass
        except DeveloperModeIsNotEnabledError:
            raise DeveloperModeRequired("Developer Mode is off on the iPhone.") from None
    finally:
        try:
            await asyncio.wait_for(lockdown.close(), 5)
        except Exception:
            pass

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
    """Why the developer disk image didn't mount.

    pymobiledevice3 11 downloads a personalized image rather than building one
    with the `ipsw` CLI, so this is now almost always the network — the first
    mount on a new iOS release has to fetch the image before anything can start.
    """
    return (
        f"Preparing the developer disk image timed out (iOS {ios}).\n\n"
        "The first connect on a new iOS release downloads that image, so this is "
        "usually a slow or interrupted network rather than anything about the "
        "phone. Try again on a stable connection.\n\n"
        "If it keeps failing on an iOS version that has only just come out, "
        "pymobiledevice3 may need an update:\n"
        "    .venv/bin/pip install -U pymobiledevice3"
    )


async def _wait_for_rsd(udid: str, say: StatusFn, timeout: float = RSD_TIMEOUT):
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
            raise TunnelNotReady(
                "The tunnel daemon is running, but it never opened a tunnel to this "
                "iPhone.\n\n"
                "This is almost certainly not a trust problem: Spoofr just read this "
                "phone’s name and iOS version over the same connection, which it "
                "could not have done if the phone weren’t paired and unlocked.\n\n"
                "What the tunnel daemon last reported:\n"
                f"{_tunneld_tail(4)}\n\n"
                "Quit Spoofr and open it again — the next Connect retires the old "
                "daemon and starts a fresh one."
            )
        await asyncio.sleep(0.5)


async def _close_quietly(rsd) -> None:
    try:
        await rsd.close()
    except Exception:
        pass
