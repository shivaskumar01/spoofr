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

# pymobiledevice3 11 downloads the personalized developer image itself; older
# releases shelled out to the `ipsw` CLI for it, which is why the app once bundled
# that binary. Keeping Homebrew on PATH is still cheap insurance: launching with
# sudo replaces PATH with a secure_path that omits it, and plumbum (imported by
# pymobiledevice3) snapshots PATH at import for any command it looks up.
for _brew in ("/opt/homebrew/bin", "/usr/local/bin"):
    if os.path.isdir(_brew) and _brew not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _brew

import asyncio
import json
import plistlib
import re
import urllib.request
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
    NotPairedError,
    PairingDialogResponsePendingError,
    PasswordRequiredError,
    TunneldConnectionError,
    UserDeniedPairingError,
)
from pymobiledevice3.common import get_home_folder
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.amfi import AmfiService
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
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
# After the cable comes out, the tunnel daemon needs a few seconds to find the
# phone on Wi-Fi (it browses every 5 s) and build a tunnel over it.
WIFI_REATTACH_WAIT = 25.0
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


class PhoneLocked(SpooferError):
    """The iPhone has to be unlocked for this step. Worth retrying on its own."""


class NeedsTrust(SpooferError):
    """The iPhone hasn't trusted this Mac yet (or said Don't Trust)."""


LOCKED_MSG = "Your iPhone is locked. Unlock it and it will carry on."
TRUST_MSG = "Unlock your iPhone and tap Trust."
DENIED_MSG = ("Your iPhone said Don’t Trust. Unplug it, plug it back in, and tap Trust "
              "this time.")


def human_error(e: BaseException) -> str:
    """A sentence a person can act on, never a raw exception name or code."""
    if isinstance(e, SpooferError):
        return str(e)
    if isinstance(e, PasswordRequiredError):
        return LOCKED_MSG
    if isinstance(e, UserDeniedPairingError):
        return DENIED_MSG
    if isinstance(e, (NotPairedError, PairingDialogResponsePendingError)):
        return TRUST_MSG
    if isinstance(e, PermissionError):
        return str(e) or "The administrator password was cancelled."
    text = str(e).strip()
    looks_raw = (
        not text or len(text) > 240 or " " not in text          # a bare token or code
        or text.startswith(("<", "{", "[", "("))
        or re.search(r"0x[0-9A-Fa-f]{4,}|\[Errno \d+\]|errno|Traceback|Error\(", text)
    )
    if looks_raw:
        return ("Couldn’t talk to your iPhone. Unplug it, plug it back in, unlock it, "
                "and try again.")
    return text


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
    udid: str = ""                 # stable identity: sessions and settings are per phone
    model: str = ""                # "iPhone 17 Pro"
    # the developer image had to be mounted on this connect (diagnostic only: iOS
    # can unmount it without a restart, so this is not a restart signal)
    fresh_mount: bool = False
    # seconds the phone has been up (mach time). It only goes backwards when the
    # phone restarts, which is how a restart is told apart from a replug.
    uptime: Optional[float] = None
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


def connect(on_status: Optional[StatusFn] = None, serial: Optional[str] = None,
            meta: Optional[dict] = None) -> Device:
    """Start/attach the tunnel and open a spoofing session against the iPhone
    (`serial`, or the first one visible, cable preferred).

    `on_status`, if given, receives short progress strings and may be called from
    a background thread.
    """
    def say(msg: str) -> None:
        if on_status:
            on_status(msg)

    say("Starting the tunnel…")
    _tunneld.ensure()
    return _loop.run(_open(say, serial, meta), CONNECT_TIMEOUT)


# --- discovery: what is plugged in, and is it ready? --------------------------

def probe(serials: Optional[set] = None) -> list[dict]:
    """Every visible iPhone as a card: name, model, iOS, link, and whether it
    trusts this Mac yet. `serials` limits the (slower) lockdown query to those;
    the rest come back with just their serial and link. Blocking, bounded."""
    try:
        return _loop.run(_probe(serials), USBMUX_TIMEOUT + 2 * LOCKDOWN_TIMEOUT)
    except Exception:
        return []


async def _probe(serials: Optional[set]) -> list[dict]:
    try:
        devices = await list_devices()
    except Exception:
        return []
    kinds: dict[str, set] = {}
    for d in devices:
        kinds.setdefault(d.serial, set()).add(d.connection_type)
    out = []
    for serial, k in kinds.items():
        card = {"serial": serial, "link": _kinds_to_link(k), "state": "unknown"}
        if serials is None or serial in serials:
            card.update(await _describe(serial))
        out.append(card)
    return out


async def _describe(serial: str) -> dict:
    """Identity + trust state, without ever triggering the Trust dialog."""
    try:
        lockdown = await asyncio.wait_for(
            create_using_usbmux(serial, autopair=False), LOCKDOWN_TIMEOUT)
    except PasswordRequiredError:
        return {"state": "locked"}
    except Exception as e:
        return {"state": "error", "error": human_error(e)}
    try:
        v = lockdown.all_values or {}
        model = lockdown.display_name or v.get("ProductType") or "iPhone"
        return {
            "udid": lockdown.udid or serial,
            "name": v.get("DeviceName") or model,
            "model": model,
            "ios": lockdown.product_version or "",
            "state": "ready" if lockdown.paired else "trust",
        }
    finally:
        try:
            await asyncio.wait_for(lockdown.close(), 5)
        except Exception:
            pass


def request_trust(serial: str, wait: float = 60.0) -> str:
    """Ask the iPhone to trust this Mac (it shows the Trust dialog) and wait for
    the answer: "trusted", "locked" (unlock it first), "denied", or "waiting"."""
    try:
        _loop.run(_request_trust(serial, wait), wait + LOCKDOWN_TIMEOUT)
        return "trusted"
    except PasswordRequiredError:
        return "locked"
    except UserDeniedPairingError:
        return "denied"
    except Exception:
        return "waiting"


async def _request_trust(serial: str, wait: float) -> None:
    lockdown = await create_using_usbmux(serial, autopair=True, pair_timeout=wait)
    try:
        await lockdown.close()
    except Exception:
        pass


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
    link = _kinds_to_link(kinds)
    if not link and serial in await asyncio.get_running_loop().run_in_executor(None, tunneld_udids):
        # usbmux doesn't list it, but the daemon holds a tunnel to it: on Wi-Fi
        return "Wi-Fi"
    return link


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


async def _open(say: StatusFn, want: Optional[str] = None,
                meta: Optional[dict] = None) -> Device:
    say("Looking for your iPhone…")
    muxed = await _bounded(list_devices(), 10,
                           "Couldn’t reach the Mac’s iPhone service. Unplug the phone, "
                           "plug it back in, and try again.")
    if want:
        # only the phone asked for: falling back to "any phone" used to connect a
        # different iPhone than the one whose session was waiting
        muxed = [d for d in muxed if d.serial == want]
        if not muxed:
            # not on the cable (or usbmux's Wi-Fi listing): the tunnel daemon may
            # still reach it over Wi-Fi, which needs no usbmux at all
            return await _reattach(want, say, meta or {})
    if not muxed:
        raise NoDeviceFound("Your iPhone isn’t showing up. Plug it in with a cable, "
                            "unlock it, and tap Trust if it asks.")
    # prefer the cable when both links exist, faster and steadier for the
    # lockdown/mount phase; Wi-Fi-only devices still work
    muxed.sort(key=lambda d: d.connection_type != "USB")
    serial = muxed[0].serial
    link = _kinds_to_link({d.connection_type for d in muxed
                           if getattr(d, "serial", None) == serial}) or "USB"

    try:
        lockdown = await _bounded(create_using_usbmux(serial, autopair=False), 15,
                                  "Your iPhone didn’t answer. Unplug it, plug it back in, "
                                  "and unlock it.")
    except PasswordRequiredError:
        raise PhoneLocked(LOCKED_MSG) from None
    if not lockdown.paired:
        try:
            await lockdown.close()
        except Exception:
            pass
        raise NeedsTrust(TRUST_MSG)
    fresh_mount = False
    try:
        name = lockdown.all_values.get("DeviceName", "iPhone")
        ios = lockdown.product_version
        udid = lockdown.udid or serial
        model = lockdown.display_name or lockdown.all_values.get("ProductType") or "iPhone"
        # so the tunnel daemon can find this phone on Wi-Fi after the cable is out
        _save_pair_record(udid, lockdown)
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
            fresh_mount = True
        except asyncio.TimeoutError:
            raise SpooferError(_mount_timeout_message(ios)) from None
        except AlreadyMountedError:
            pass
        except DeveloperModeIsNotEnabledError:
            raise DeveloperModeRequired("Developer Mode is off on the iPhone.") from None
        except PasswordRequiredError:
            raise PhoneLocked(LOCKED_MSG) from None
    finally:
        try:
            await asyncio.wait_for(lockdown.close(), 5)
        except Exception:
            pass

    rsd = await _wait_for_rsd(udid, say)
    return await _session_on(rsd, say, name=name, ios=ios, serial=serial, link=link,
                             wireless_on=wireless_on, udid=udid, model=model,
                             fresh_mount=fresh_mount)


async def _session_on(rsd, say: StatusFn, **fields) -> Device:
    """Open the location service over a tunnel's RSD and wrap it as a Device."""
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
        uptime = await _uptime(dvt)
    except Exception:
        await loc_stack.aclose()
        await stack.aclose()
        raise
    return Device(_location=location, _stack=stack, _rsd=rsd, _loc_stack=loc_stack,
                  uptime=uptime, **fields)


def describe(dev: "Device") -> str:
    """One log line's worth: which path, how long the phone has been up. Never
    raises: a log line must not be able to break a connect."""
    try:
        base = f"{dev.name} iOS {dev.ios} via {dev.link}"
        uptime = getattr(dev, "uptime", None)
        up = f"{uptime / 3600:.1f} h" if uptime is not None else "?"
        via = ", ".join(tunneld_udids().get(getattr(dev, "udid", ""), [])) or "?"
        mount = "mounted now" if getattr(dev, "fresh_mount", False) else "already mounted"
        return f"{base} (tunnel {via}; up {up}; image {mount})"
    except Exception:
        return str(getattr(dev, "name", "iPhone"))


async def _uptime(dvt) -> Optional[float]:
    """Seconds since the phone booted (mach absolute time), or None."""
    try:
        async def read():
            async with DeviceInfo(dvt) as info:
                t = await info.mach_time_info()
            return float(t[0]) * float(t[1]) / float(t[2]) / 1e9
        return await asyncio.wait_for(read(), 5)
    except Exception:
        return None


async def _reattach(udid: str, say: StatusFn, meta: dict) -> Device:
    """Reach a known iPhone through the tunnel daemon alone (Wi-Fi, no cable).

    No usbmux, no lockdown, no mount: the developer image stays mounted until the
    phone restarts, and the tunnel is all the location service needs. Name and iOS
    come from what was remembered on the last cable connect.
    """
    say("Looking for your iPhone on Wi-Fi…")
    # ask the daemon for a Wi-Fi tunnel now rather than waiting for its next
    # 5-second scan; whichever arrives first, this or the scan, is used below
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, request_wifi_tunnel, udid)
    rsd = await _wait_for_rsd(udid, say, timeout=WIFI_REATTACH_WAIT)
    _log(f"reattached over Wi-Fi: {udid} via {tunneld_udids().get(udid)}")
    ios = getattr(rsd, "product_version", None) or meta.get("ios", "")
    return await _session_on(
        rsd, say, name=meta.get("name") or "iPhone", ios=ios, serial=udid, link="Wi-Fi",
        wireless_on=True, udid=udid, model=meta.get("model") or "iPhone")


def _save_pair_record(udid: str, lockdown) -> None:
    """Keep this phone's lockdown pairing record where pymobiledevice3 looks for it.

    The tunnel daemon finds a phone on Wi-Fi by matching the Wi-Fi MAC address it
    advertises (_apple-mobdev2) against `<udid>.plist` records in its home folder.
    Pairing records normally live only in usbmuxd's store, so without this copy
    the daemon had nothing to match and never built a Wi-Fi tunnel.
    """
    try:
        record = dict(lockdown.pair_record or {})
        mac = record.get("WiFiMACAddress") or lockdown.all_values.get("WiFiAddress")
        if not record or not mac:
            return
        record["WiFiMACAddress"] = mac
        path = get_home_folder() / f"{udid}.plist"
        if path.exists():
            try:
                old = plistlib.loads(path.read_bytes())
                if old.get("HostID") == record.get("HostID") and old.get("WiFiMACAddress") == mac:
                    return
            except Exception:
                pass
        tmp = path.with_suffix(".plist.tmp")
        tmp.write_bytes(plistlib.dumps(record))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception as e:
        _log(f"couldn't save the pairing record for Wi-Fi: {e!r}")


def request_wifi_tunnel(udid: str, timeout: float = WIFI_REATTACH_WAIT) -> bool:
    """Ask the tunnel daemon to build a Wi-Fi (RemotePairing) tunnel to `udid`."""
    try:
        url = (f"http://{TUNNELD_DEFAULT_ADDRESS[0]}:{TUNNELD_DEFAULT_ADDRESS[1]}"
               f"/start-tunnel?udid={udid}&connection_type=wifi")
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def tunneld_udids() -> dict:
    """{udid: [tunnel interfaces]} straight from the tunnel daemon, {} if it's down."""
    try:
        with urllib.request.urlopen(
                f"http://{TUNNELD_DEFAULT_ADDRESS[0]}:{TUNNELD_DEFAULT_ADDRESS[1]}/", timeout=2) as r:
            data = json.loads(r.read().decode() or "{}")
        return {u: [t.get("interface", "") for t in ts] for u, ts in data.items()
                if isinstance(ts, list)}
    except Exception:
        return {}


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
