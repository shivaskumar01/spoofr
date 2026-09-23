"""Where the iPhone really is.

iOS offers no way to read the phone's own GPS over the developer connection
(location simulation is set/clear only). But the phone is on this Mac's cable,
or on its Wi-Fi, so this Mac's location *is* the phone's real location, to
within a cable's length (or a Wi-Fi network's range).

So: macOS CoreLocation, driven properly — created on the main thread, where the
Qt event loop is also the Cocoa run loop, with a delegate for the answer. Apple
delivers CoreLocation callbacks on the run loop of the thread that created the
manager; the old code polled it from a worker thread instead, which is not a
supported way to use it. A permission granted later (the first-run prompt) is
picked up by the delegate and upgrades an approximate fix to a precise one.

IP geolocation (city-level) fills in if CoreLocation can't answer in time.
"""

from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QObject, QTimer, Signal

PRECISE_M = 1000.0        # a fix better than this is "the iPhone", not "approximately"
TIMEOUT_MS = 8000

try:                                        # macOS only; the app runs without it
    import objc
    from CoreLocation import CLLocationManager, kCLLocationAccuracyBest
    from Foundation import NSObject

    class _Delegate(NSObject):
        def initWithOwner_(self, owner):
            self = objc.super(_Delegate, self).init()
            if self is None:
                return None
            self._owner = owner
            return self

        def locationManager_didUpdateLocations_(self, mgr, locations):
            if locations:
                self._owner._take(locations[-1])

        def locationManager_didFailWithError_(self, mgr, error):
            self._owner._cl_failed()

        def locationManagerDidChangeAuthorization_(self, mgr):
            self._owner._auth_changed()

    _HAVE_CL = True
except Exception:                           # pragma: no cover - non-macOS / no pyobjc
    _HAVE_CL = False


def _use_core_location() -> bool:
    # A plain `python -m qtui` run has no bundle identity: macOS silently denies
    # it and never even prompts, so don't wait on an answer that can't come.
    return _HAVE_CL and bool(getattr(sys, "frozen", False))


class Locator(QObject):
    """found(lat, lon, precise) — possibly twice: approximate first, then exact."""

    found = Signal(float, float, bool)
    _ip = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mgr = None
        self._delegate = None
        self._precise = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fallback)
        self._ip.connect(self._on_ip)

    def request(self):
        """Ask again (on connect, after Stop spoofing). Answers arrive via `found`."""
        self._precise = False
        self._timer.start(TIMEOUT_MS)
        if not self._start_core_location():
            self._fallback()

    # ---- CoreLocation ----

    def _start_core_location(self) -> bool:
        if not _use_core_location():
            return False
        try:
            if not CLLocationManager.locationServicesEnabled():
                return False
            if self._mgr is None:
                self._mgr = CLLocationManager.alloc().init()
                self._delegate = _Delegate.alloc().initWithOwner_(self)
                self._mgr.setDelegate_(self._delegate)
                self._mgr.setDesiredAccuracy_(kCLLocationAccuracyBest)
            status = self._mgr.authorizationStatus()
            if status in (1, 2):                 # restricted / denied
                return False
            if status == 0:                      # not determined: the one-time prompt
                self._mgr.requestWhenInUseAuthorization()
            self._mgr.startUpdatingLocation()
            cached = self._mgr.location()
            if cached is not None:
                self._take(cached)
            return True
        except Exception:
            return False

    def _take(self, loc):
        try:
            c = loc.coordinate()
            acc = float(loc.horizontalAccuracy())
        except Exception:
            return
        precise = 0 <= acc <= PRECISE_M
        if precise:
            self._precise = True
            self._timer.stop()
            try:
                self._mgr.stopUpdatingLocation()
            except Exception:
                pass
        self.found.emit(float(c.latitude), float(c.longitude), precise)

    def _cl_failed(self):
        if not self._precise:
            self._fallback()

    def _auth_changed(self):
        try:
            status = self._mgr.authorizationStatus()
        except Exception:
            return
        if status in (3, 4) and not self._precise:   # just allowed: go get the real fix
            self._mgr.startUpdatingLocation()

    # ---- the city-level fallback ----

    def _fallback(self):
        if self._precise:
            return
        threading.Thread(target=self._ip_worker, daemon=True).start()

    def _ip_worker(self):
        from . import geo
        self._ip.emit(geo._ip_location())

    def _on_ip(self, loc):
        if loc and not self._precise:
            self.found.emit(float(loc[0]), float(loc[1]), False)
