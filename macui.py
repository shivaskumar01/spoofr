"""macui — native macOS extras for Spoofr: a menu-bar item and a panic hotkey.

Both are best-effort and macOS-only (pyobjc, already a pymobiledevice3 dep). Every
action is marshalled back to the Tk app through its main-thread queue (``app._post``)
so nothing here ever touches Tk directly. If pyobjc is missing or anything fails,
``install()`` returns ``None`` and the app runs exactly as before.

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

    def rebuildMenu(self):
        if self._status_item is None:
            return
        menu = NSMenu.alloc().init()
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Spoofr", None, "")
        header.setEnabled_(False)
        menu.addItem_(header)
        menu.addItem_(NSMenuItem.separatorItem())
        try:
            saved = list(self._app.saved or [])
        except Exception:
            saved = []
        if saved:
            for p in saved[:12]:
                try:
                    it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                        f"Go to  {p['name']}", "goTo:", "")
                    it.setTarget_(self)
                    it.setRepresentedObject_(f"{float(p['lat'])},{float(p['lon'])}")
                    menu.addItem_(it)
                except Exception:
                    continue
        else:
            empty = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "No saved places yet", None, "")
            empty.setEnabled_(False)
            menu.addItem_(empty)
        menu.addItem_(NSMenuItem.separatorItem())
        restore = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Restore real GPS", "restore:", "")
        restore.setTarget_(self)
        menu.addItem_(restore)
        opn = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Open Spoofr", "openApp:", "")
        opn.setTarget_(self)
        menu.addItem_(opn)
        self._status_item.setMenu_(menu)

    def goTo_(self, sender):
        try:
            lat, lon = (float(x) for x in str(sender.representedObject()).split(","))
        except Exception:
            return
        self._app._post(lambda: self._app._use_place(lat, lon))

    def restore_(self, sender):
        self._app._post(self._app.restore_real_gps)

    def openApp_(self, sender):
        self._app._post(self._app._raise_window)

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
