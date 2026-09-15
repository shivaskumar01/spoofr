"""Regression tests for the failure in ~/.spoofr/spoofr.log (2026-06-03).

    location set failed (ConnectionTerminatedError('Connection closed')); reopening…
    reopen+retry failed: TimeoutError()

Six of those over half an hour. The set() held Device._lock across an unbounded
`_loop.run`, so a wedged channel froze Restore GPS, the panic hotkey and app quit
with it. These tests pin the properties that stop that happening: every call is
time-bounded, the lock is always released, and a stop/Restore can overtake a fix
that is already in flight.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import AsyncExitStack

import pytest

import core


class ConnectionTerminatedError(Exception):
    """Stand-in for the pymobiledevice3 exception that starts the cascade."""


class FakeLocation:
    """A LocationSimulation whose behaviour the test dictates."""

    def __init__(self, set_error=None, hang_set=False):
        self.set_error, self.hang_set = set_error, hang_set
        self.sets: list[tuple[float, float]] = []
        self.clears = 0

    async def set(self, lat, lon):
        if self.hang_set:
            await asyncio.sleep(60)
        if self.set_error:
            raise self.set_error
        self.sets.append((lat, lon))

    async def clear(self):
        self.clears += 1


class Device(core.Device):
    """core.Device with _reopen replaced, so no real DVT channel is needed."""

    reopen_mode = "ok"       # "ok" | "hang" | "fail"

    async def _reopen(self):
        if self.reopen_mode == "hang":
            await asyncio.sleep(60)
        if self.reopen_mode == "fail":
            raise ConnectionTerminatedError("Channel is closed")
        self._location = FakeLocation()


def make_device(location=None, reopen_mode="ok") -> Device:
    dev = Device(name="iPhone", ios="18.0", _location=location or FakeLocation(),
                 _stack=AsyncExitStack(), _rsd=object(), _loc_stack=AsyncExitStack())
    dev.reopen_mode = reopen_mode
    return dev


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    """Same code paths, budgets small enough to assert on."""
    monkeypatch.setattr(core, "SET_TIMEOUT", 0.3)
    monkeypatch.setattr(core, "CLEAR_TIMEOUT", 0.3)
    monkeypatch.setattr(core, "REOPEN_TIMEOUT", 0.3)
    monkeypatch.setattr(core, "CLOSE_TIMEOUT", 0.3)
    monkeypatch.setattr(core, "LOCK_TIMEOUT", 3.0)


def test_set_lands_on_the_device():
    dev = make_device()
    dev.set(37.5, -122.5)
    assert dev._location.sets == [(37.5, -122.5)]


def test_wedged_set_is_bounded_and_frees_the_lock():
    """A set that never answers must not park the caller forever."""
    dev = make_device(FakeLocation(hang_set=True), reopen_mode="hang")
    t0 = time.monotonic()
    with pytest.raises(core.SpooferError):
        dev.set(1.0, 2.0)
    assert time.monotonic() - t0 < 2.0, "set() exceeded its timeout budget"
    assert dev._lock.acquire(timeout=0.5), "set() leaked the lock"
    dev._lock.release()


def test_the_logged_cascade_raises_instead_of_hanging():
    """set fails -> reopen hangs: exactly the logged sequence."""
    dev = make_device(FakeLocation(set_error=ConnectionTerminatedError("Connection closed")),
                      reopen_mode="hang")
    t0 = time.monotonic()
    with pytest.raises(Exception):
        dev.set(1.0, 2.0)
    assert time.monotonic() - t0 < 2.0
    assert dev._lock.acquire(timeout=0.5)
    dev._lock.release()


def test_restore_still_works_after_a_wedged_set():
    """The panic hotkey has to get through even right after a dead set()."""
    dev = make_device(FakeLocation(hang_set=True), reopen_mode="hang")
    with pytest.raises(core.SpooferError):
        dev.set(1.0, 2.0)
    dev.reopen_mode = "ok"
    dev.clear()                       # must not raise, must not hang
    assert dev._location.clears == 1


def test_set_retries_once_through_a_reopen():
    dev = make_device(FakeLocation(set_error=ConnectionTerminatedError("Channel is closed")))
    dev.set(4.0, 5.0)                 # first attempt fails, reopen swaps in a live channel
    assert dev._location.sets == [(4.0, 5.0)]


def test_suspend_drops_a_queued_fix():
    """A route fix waiting on the lock must not land after a Restore/stop."""
    loc = FakeLocation()
    dev = make_device(loc)
    dev._lock.acquire()               # stand in for an op already in progress
    started = threading.Event()

    def queued():
        started.set()
        dev.set(9.0, 9.0)

    t = threading.Thread(target=queued, daemon=True)
    t.start()
    started.wait(1.0)
    time.sleep(0.1)                   # let it block on acquire()
    dev.suspend()                     # Restore GPS / stop motion happens here
    dev._lock.release()
    t.join(2.0)
    assert not t.is_alive()
    assert loc.sets == [], "a superseded fix reached the device"


def test_clear_suspends_so_nothing_re_spoofs_behind_it():
    loc = FakeLocation()
    dev = make_device(loc)
    epoch = dev._epoch
    dev.clear()
    assert dev._epoch > epoch, "clear() must invalidate in-flight fixes"
    assert loc.clears == 1


def test_close_cannot_hang_on_quit():
    dev = make_device(FakeLocation(hang_set=True))

    class Stuck(AsyncExitStack):
        async def aclose(self):
            await asyncio.sleep(60)

    dev._loc_stack = Stuck()
    t0 = time.monotonic()
    dev.close(clear=True)             # must return, never raise
    assert time.monotonic() - t0 < 3.0, "close() exceeded its budget"


def test_close_with_clear_false_leaves_the_spoof_on_the_phone():
    loc = FakeLocation()
    dev = make_device(loc)
    dev.close(clear=False)
    assert loc.clears == 0
