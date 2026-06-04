"""The bridge between the Qt GUI thread and the blocking device core.

Every `core.*` call blocks (it round-trips to the iPhone over the tunnel), so it
must run off the GUI thread. This QObject owns the live `core.Device`, runs each
operation on a daemon thread, and reports back with Qt signals — which, emitted
from a worker thread to this GUI-thread object, auto-queue onto the GUI thread.
So nothing here ever touches widgets directly; the window just connects slots.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from . import theme

# `core` / `portable` pull in all of pymobiledevice3 (heavy). They are imported
# lazily inside the worker threads so launching the app stays instant — the first
# Connect pays the import cost, and it happens off the GUI thread.


class DeviceBridge(QObject):
    # text, hex-color  — drive the status pill
    status = Signal(str, str)
    # one-line feedback for the hint bar
    hint = Signal(str)
    connected = Signal(object)        # core.Device
    devModeRequired = Signal()
    failed = Signal(str)              # human-readable error
    restored = Signal()
    # (lat, lon) just pushed to the phone, for the live marker
    located = Signal(float, float)
    currentLocation = Signal(float, float)   # the Mac's location, found on connect
    deviceLost = Signal()                    # the iPhone was unplugged / vanished

    def __init__(self):
        super().__init__()
        self.device: core.Device | None = None
        self._connecting = False
        self._monitor_on = False

    # ---- connect --------------------------------------------------------

    def connect(self):
        if self._connecting:
            return
        self._connecting = True
        old, self.device = self.device, None
        if old:
            threading.Thread(target=old.close, daemon=True).start()
        self.status.emit("Connecting…", theme.AMBER)
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        import core
        import portable
        try:
            portable.ensure_tunnel()   # brings up the Wi-Fi tunnel (one admin prompt)
            device = core.connect(on_status=lambda m: self.status.emit(m, theme.AMBER))
            self.device = device
            self.status.emit(f"Connected  ·  {device.name}  ·  iOS {device.ios}", theme.GREEN)
            self.connected.emit(device)
            self._start_monitor()
        except core.DeveloperModeRequired:
            self.status.emit("Developer Mode needed", theme.AMBER)
            self.devModeRequired.emit()
        except Exception as e:
            self.status.emit("Not connected", theme.RED)
            self.failed.emit(str(e))
        finally:
            self._connecting = False

    # ---- set / restore --------------------------------------------------

    def set_location(self, lat: float, lon: float):
        dev = self.device
        if not dev:
            self.hint.emit("Connect to your iPhone first.")
            return
        self.hint.emit(f"Setting location to {lat:.5f}, {lon:.5f}…")

        def work():
            try:
                dev.set(lat, lon)
                self.located.emit(lat, lon)
                self.hint.emit(f"Location set to {lat:.5f}, {lon:.5f}")
            except Exception as e:
                self.hint.emit(f"Failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def restore(self, panic: bool = False):
        dev = self.device
        if not dev:
            self.hint.emit("Not connected — nothing to restore.")
            return

        def work():
            try:
                dev.clear()
                self.restored.emit()
                tag = " (panic)" if panic else ""
                self.hint.emit(f"Real GPS restored{tag}. iOS reacquires in a few seconds.")
            except Exception as e:
                self.hint.emit(f"Restore failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def locate(self):
        """Find the Mac's current location (off-thread) and emit it."""
        threading.Thread(target=self._locate_worker, daemon=True).start()

    def _locate_worker(self):
        from . import geo
        loc = geo.current_location()
        if loc:
            self.currentLocation.emit(loc[0], loc[1])

    def is_connected(self) -> bool:
        return self.device is not None

    # ---- liveness monitor (notice an unplug) ----------------------------

    def _start_monitor(self):
        if self._monitor_on:
            return
        self._monitor_on = True
        threading.Thread(target=self._monitor_worker, daemon=True).start()

    def _monitor_worker(self):
        import time
        import core
        misses = 0
        while self._monitor_on:
            time.sleep(3.0)
            dev = self.device
            if dev is None or not self._monitor_on:
                return
            if core.device_present(dev.serial):
                misses = 0
            else:
                misses += 1
                if misses >= 2:             # ~6s gone → really unplugged (not a blip)
                    self._monitor_on = False
                    self.device = None      # session is dead; the spoof stays on the phone
                    self.deviceLost.emit()
                    return

    # ---- disconnect / teardown ------------------------------------------

    def disconnect(self):
        """User-initiated disconnect: close the session but LEAVE the spoofed
        location active on the iPhone (it persists until reset/reboot)."""
        self._monitor_on = False
        dev, self.device = self.device, None
        if dev:
            threading.Thread(target=lambda: dev.close(clear=False), daemon=True).start()

    def drop_device(self):
        """Release the device session (hand off to the phone) but leave the
        Wi-Fi tunnel up, so iPhone mode can reuse it."""
        self._monitor_on = False
        dev, self.device = self.device, None
        if dev:
            threading.Thread(target=dev.close, daemon=True).start()

    def close(self):
        """Tear the session + tunnel down on app exit. Leaves the spoof active on
        the iPhone (clear=False) so it persists between runs."""
        self._monitor_on = False
        dev, self.device = self.device, None
        if dev:
            try:
                dev.close(clear=False)
            except Exception:
                pass
        import sys
        if "core" in sys.modules:        # only if we actually connected this run
            try:
                sys.modules["core"].cleanup()
            except Exception:
                pass
