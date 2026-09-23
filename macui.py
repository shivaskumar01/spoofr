"""macui, native macOS extras for Spoofr: a menu-bar item and a panic hotkey.

The menu-bar item shows a running route's progress and can pause, stop, reopen
the window or quit, so a route keeps going with the window closed.

Both are best-effort and macOS-only (pyobjc, already a pymobiledevice3 dep). Every
action is marshalled back onto the Qt event loop through ``app._post`` so nothing
here ever touches a widget from a Cocoa callback. If pyobjc is missing or anything
fails, ``install()`` returns ``None`` and the app runs exactly as before.

Panic hotkey: Control-Option-Command-R restores the real GPS from anywhere. The
system-wide part needs a one-time Accessibility grant (System Settings → Privacy &
Security → Accessibility); the in-app part works whenever Spoofr is focused.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSStatusBar, NSMenu, NSMenuItem, NSVariableStatusItemLength, NSEvent,
    NSEventMaskKeyDown, NSEventModifierFlagCommand, NSEventModifierFlagControl,
    NSEventModifierFlagOption,
)
from Foundation import NSObject

# Control-Option-Command-R
_HOTKEY_MASK = NSEventModifierFlagControl | NSEventModifierFlagOption | NSEventModifierFlagCommand
_HOTKEY_KEY = "r"


def _hotkey_match(event) -> bool:
    try:
        if (event.modifierFlags() & _HOTKEY_MASK) != _HOTKEY_MASK:
            return False
        return (event.charactersIgnoringModifiers() or "").lower() == _HOTKEY_KEY
    except Exception:
        return False


class _Controller(NSObject):
    """Owns the status item + event monitors and forwards clicks to the app."""

    def initWithApp_(self, app):
        self = objc.super(_Controller, self).init()
        if self is None:
            return None
        self._app = app
        self._status_item = None
        self._global_mon = None
        self._local_mon = None
        return self

    # ---- menu bar -------------------------------------------------------
    def installMenuBar(self):
        item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        try:
            item.button().setTitle_("◉")
        except Exception:
            try:
                item.setTitle_("◉")
            except Exception:
                pass
        self._status_item = item
        self.rebuildMenu()

    @objc.python_method
    def setProgress(self, text):
        """Title next to the ◉ while a route runs: '◉ 62%', '◉ ❚❚' (paused)."""
        if self._status_item is None:
            return
        title = f"◉ {text}" if text else "◉"
        try:
            self._status_item.button().setTitle_(title)
        except Exception:
            try:
                self._status_item.setTitle_(title)
            except Exception:
                pass
        if text != getattr(self, "_last_progress", None):
            self._last_progress = text
            self.rebuildMenu()

    @objc.python_method
    def _item(self, menu, title, action=None, enabled=True, rep=None):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        if action:
            it.setTarget_(self)
        if rep is not None:
            it.setRepresentedObject_(rep)
        it.setEnabled_(bool(enabled and action))
        menu.addItem_(it)
        return it

    def rebuildMenu(self):
        if self._status_item is None:
            return
        menu = NSMenu.alloc().initWithTitle_("Spoofr")
        menu.setAutoenablesItems_(False)
        try:
            st = self._app.menu_state()
        except Exception:
            st = {"route": None, "connected": False, "spoofing": False, "name": ""}
        r = st.get("route")
        if r:
            left = r.get("left")
            mins = f" · {int(round(left / 60))} min left" if left is not None else ""
            state = "Paused" if r.get("paused") else f"{r.get('pct', 0)}% done{mins}"
            self._item(menu, f"Route to {r.get('name') or 'destination'}", None)
            self._item(menu, f"   {state}", None)
            if r.get("running"):
                self._item(menu, "Resume Route" if r.get("paused") else "Pause Route", "pauseRoute:")
            self._item(menu, "Stop Route…", "stopRoute:")
        else:
            who = st.get("name") or "iPhone"
            self._item(menu, f"{who} connected" if st.get("connected") else "Not connected", None)
        menu.addItem_(NSMenuItem.separatorItem())
        try:
            saved = list(self._app.saved or [])
        except Exception:
            saved = []
        for p in saved[:12]:
            try:
                self._item(menu, f"Go to  {p['name']}", "goTo:",
                           rep=f"{float(p['lat'])},{float(p['lon'])},{p['name']}")
            except Exception:
                continue
        if saved:
            menu.addItem_(NSMenuItem.separatorItem())
        self._item(menu, "Stop Spoofing", "restore:", enabled=True)
        menu.addItem_(NSMenuItem.separatorItem())
        self._item(menu, "Open Spoofr", "openApp:")
        self._item(menu, "Quit Spoofr", "quitApp:")
        self._status_item.setMenu_(menu)

    def goTo_(self, sender):
        try:
            la, lo, name = (str(sender.representedObject()).split(",", 2) + [""])[:3]
            lat, lon = float(la), float(lo)
        except Exception:
            return
        self._app._post(lambda: self._app._use_place(lat, lon, name))

    def pauseRoute_(self, sender):
        self._app._post(self._app.route_pause_toggle)

    def stopRoute_(self, sender):
        self._app._post(self._app.route_stop)

    def restore_(self, sender):
        self._app._post(self._app.restore_real_gps)

    def openApp_(self, sender):
        self._app._post(self._app._raise_window)

    def quitApp_(self, sender):
        self._app._post(self._app.quit_app)

    # ---- panic hotkey ---------------------------------------------------
    def installHotkey(self):
        def global_handler(event):
            if _hotkey_match(event):
                self._app._post(self._app.restore_real_gps)

        def local_handler(event):
            if _hotkey_match(event):
                self._app._post(self._app.restore_real_gps)
                return None      # swallow it
            return event

        try:
            self._global_mon = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, global_handler)
        except Exception:
            self._global_mon = None
        try:
            self._local_mon = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, local_handler)
        except Exception:
            self._local_mon = None

    # ---- teardown -------------------------------------------------------
    def teardown(self):
        try:
            if self._status_item is not None:
                NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
        except Exception:
            pass
        self._status_item = None
        for mon in (self._global_mon, self._local_mon):
            try:
                if mon is not None:
                    NSEvent.removeMonitor_(mon)
            except Exception:
                pass
        self._global_mon = self._local_mon = None


def install(app):
    """Add the menu-bar item + panic hotkey. Returns a controller exposing
    ``rebuildMenu()`` and ``teardown()``, or ``None`` if unavailable."""
    ctrl = _Controller.alloc().initWithApp_(app)
    if ctrl is None:
        return None
    ctrl.installMenuBar()
    ctrl.installHotkey()
    return ctrl
