"""The bridge between the Qt GUI thread and the blocking device core.

Every `core.*` call blocks (it round-trips to the iPhone over the tunnel), so it
must run off the GUI thread. This QObject owns the live `core.Device`, runs each
operation on a daemon thread, and reports back with Qt signals, which, emitted
from a worker thread to this GUI-thread object, auto-queue onto the GUI thread.
So nothing here ever touches widgets directly; the window just connects slots.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from . import theme

# `core` / `portable` pull in all of pymobiledevice3 (heavy). They are imported
# lazily inside the worker threads so launching the app stays instant, the first
# Connect pays the import cost, and it happens off the GUI thread.


class DeviceBridge(QObject):
    # text, hex-color, drive the status pill
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
    deviceLost = Signal()                    # gone for good (reconnect gave up / cancelled)
    reconnecting = Signal(int)               # session dropped; auto-rebuild attempt #n
    visible = Signal(str)                    # idle pre-flight: "", "USB", "Wi-Fi", "USB + Wi-Fi"
    wirelessResult = Signal(bool, str)       # one-time wireless enable: ok, message

    RECONNECT_WINDOW = 120.0                 # seconds to keep trying before giving up

    def __init__(self):
        super().__init__()
        self.device: core.Device | None = None
        self._connecting = False
        self._monitor_on = False
        self._monitor_gen = 0    # invalidates old monitor threads across reconnects
        self._reconnecting = False
        self._reconnect_gen = 0
        self._vis_gen = 0

    # ---- connect --------------------------------------------------------

    def connect(self):
        if self._connecting:
            return
        self._reconnecting = False      # a manual connect takes over from any auto-retry
        self._reconnect_gen += 1
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
            self._emit_connected_status(device)
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

    def _emit_connected_status(self, dev):
        self.status.emit(
            f"Connected  ·  {dev.name}  ·  iOS {dev.ios}  ·  {dev.link}", theme.GREEN)

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
                # core already rebuilt the DVT channel and retried, so the whole
                # session/tunnel is dead (e.g. the cable was pulled and the old
                # tunnel went with it), rebuild the session in the background
                if self.device is dev:
                    self._begin_reconnect(dev)
        threading.Thread(target=work, daemon=True).start()

    def restore(self, panic: bool = False):
        dev = self.device
        if not dev:
            self.hint.emit("Not connected, nothing to restore.")
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
        # a new generation orphans any older monitor thread that is still inside
        # its 3 s sleep (e.g. disconnect → reconnect within that window)
        self._monitor_gen += 1
        self._monitor_on = True
        threading.Thread(target=self._monitor_worker, args=(self._monitor_gen,),
                         daemon=True).start()

    def _monitor_worker(self, gen: int):
        import time
        import core
        misses = 0
        while self._monitor_on and gen == self._monitor_gen:
            time.sleep(3.0)
            dev = self.device
            if dev is None or not self._monitor_on or gen != self._monitor_gen:
                return
            st = core.link_status(dev.serial)
            if st is None:
                continue                    # transient usbmux hiccup, no information
            if st:
                misses = 0
                if st != dev.link:          # e.g. cable pulled with wireless on
                    dev.link = st
                    self._emit_connected_status(dev)
            else:
                misses += 1
                if misses >= 2:             # ~6s gone → really unplugged (not a blip)
                    if gen != self._monitor_gen:
                        return
                    self._begin_reconnect(dev)   # spoof stays on; try to get it back
                    return

    # ---- auto-reconnect (wireless drops, unplug→Wi-Fi handoff, replug) ----

    def is_reconnecting(self) -> bool:
        return self._reconnecting

    def _begin_reconnect(self, dead):
        """The session died (device vanished, or the tunnel under it went stale).
        Close it WITHOUT clearing, the spoof stays on the phone, and quietly
        rebuild for up to RECONNECT_WINDOW seconds."""
        if self._reconnecting:
            return
        self._reconnecting = True
        self._monitor_on = False
        self._monitor_gen += 1
        if self.device is dead:
            self.device = None
        threading.Thread(target=lambda: dead.close(clear=False), daemon=True).start()
        self._reconnect_gen += 1
        threading.Thread(target=self._reconnect_worker, args=(self._reconnect_gen,),
                         daemon=True).start()

    def cancel_reconnect(self):
        """User gave up waiting, settle the UI through the normal lost path."""
        if not self._reconnecting:
            return
        self._reconnecting = False
        self._reconnect_gen += 1
        self.deviceLost.emit()

    def _reconnect_worker(self, gen: int):
        import os
        import time
        import core
        import portable

        def cancelled() -> bool:
            return gen != self._reconnect_gen or not self._reconnecting

        deadline = time.monotonic() + self.RECONNECT_WINDOW
        delay = 3.0
        attempt = 0
        while time.monotonic() < deadline and not cancelled():
            # if the root tunnel itself died we can't rebuild silently (that
            # would pop a password prompt out of nowhere), give up cleanly
            if os.geteuid() != 0 and not portable.port_open("127.0.0.1", portable.TUNNELD_PORT):
                break
            attempt += 1
            self.reconnecting.emit(attempt)
            try:
                device = core.connect(on_status=lambda m: self.status.emit(m, theme.AMBER))
                if cancelled():          # user clicked Stop mid-attempt
                    threading.Thread(target=lambda: device.close(clear=False),
                                     daemon=True).start()
                    return
                self._reconnecting = False
                self.device = device
                self._emit_connected_status(device)
                self.connected.emit(device)
                self._start_monitor()
                return
            except core.DeveloperModeRequired:
                break                    # needs the user, fall through to lost
            except Exception:
                pass
            self.status.emit("Waiting for the iPhone…", theme.AMBER)
            end = time.monotonic() + delay
            while time.monotonic() < end:
                if cancelled():
                    return
                time.sleep(0.5)
            delay = min(delay * 1.6, 20.0)
        if cancelled():
            return
        self._reconnecting = False
        self.deviceLost.emit()

    # ---- idle pre-flight: is an iPhone visible before Connect? -----------

    def start_visibility(self):
        self._vis_gen += 1
        threading.Thread(target=self._vis_worker, args=(self._vis_gen,),
                         daemon=True).start()

    def stop_visibility(self):
        self._vis_gen += 1

    def _vis_worker(self, gen: int):
        import time
        import core      # first poll also warms the heavy import off-thread
        while gen == self._vis_gen:
            if self.device is None and not self._connecting and not self._reconnecting:
                kinds = core.visible_kinds()
                if gen != self._vis_gen:
                    return
                self.visible.emit(kinds)
            time.sleep(4.0)

    # ---- one-time wireless enable ----------------------------------------

    def go_wireless(self):
        threading.Thread(target=self._wireless_worker, daemon=True).start()

    def _wireless_worker(self):
        import core
        try:
            core.enable_wireless()
            self.wirelessResult.emit(True, "Wireless is on, unplug whenever. Your iPhone "
                                           "stays controllable on this Wi-Fi.")
        except Exception as e:
            self.wirelessResult.emit(False, str(e))

    # ---- disconnect / teardown ------------------------------------------

    def _stop_auto(self):
        """Stop the monitor + any reconnect loop (user is taking over)."""
        self._monitor_on = False
        self._monitor_gen += 1
        self._reconnecting = False
        self._reconnect_gen += 1

    def disconnect(self):
        """User-initiated disconnect: close the session but LEAVE the spoofed
        location active on the iPhone (it persists until reset/reboot)."""
        self._stop_auto()
        dev, self.device = self.device, None
        if dev:
            threading.Thread(target=lambda: dev.close(clear=False), daemon=True).start()

    def drop_device(self):
        """Release the device session (hand off to the phone) but leave the
        Wi-Fi tunnel up, so iPhone mode can reuse it."""
        self._stop_auto()
        dev, self.device = self.device, None
        if dev:
            threading.Thread(target=dev.close, daemon=True).start()

    def close(self):
        """Tear the session + tunnel down on app exit. Leaves the spoof active on
        the iPhone (clear=False) so it persists between runs."""
        self._stop_auto()
        self.stop_visibility()
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
