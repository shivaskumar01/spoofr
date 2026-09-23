"""Entry point for the packaged Spoofr.app (PyInstaller bundle).

The frozen executable re-invokes itself for the privileged helpers (portable.py's
_helper_cmd), so dispatch those here; otherwise launch the native Qt UI. Kept as a
single top-level module so PyInstaller has one clean entry.
"""

from __future__ import annotations

import sys


def _act_for(user: str) -> None:
    """Set SUDO_USER/UID/GID for `user`, as sudo would for a root process."""
    import os
    import pwd
    try:
        pw = pwd.getpwnam(user)
    except (KeyError, TypeError):
        return
    os.environ.update(SUDO_USER=pw.pw_name, SUDO_UID=str(pw.pw_uid), SUDO_GID=str(pw.pw_gid))


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--tunneld":
        # privileged tunnel daemon (launched as root via osascript). Remaining
        # args are passed straight through -- portable.py sends --protocol tcp,
        # without which a modern iPhone can never be tunnelled -- except
        # --as-user, which is ours: act for that user the way sudo would, so
        # pymobiledevice3 reads (and writes) pairing records in *their* home.
        # Without it the daemon looked in root's and never found the phone on Wi-Fi.
        rest = list(args[1:])
        if "--as-user" in rest:
            i = rest.index("--as-user")
            user = rest[i + 1] if i + 1 < len(rest) else ""
            del rest[i:i + 2]
            _act_for(user)
        from pymobiledevice3.__main__ import main as pmd_main
        sys.argv = ["pymobiledevice3", "remote", "tunneld", *rest]
        pmd_main()
    elif args and args[0] == "--server":
        # phone-control web server for iPhone (QR) mode
        sys.argv = ["server", args[1] if len(args) > 1 else "8765"]
        import server
        server.main()
    elif args and args[0] == "--loctest":
        # diagnostic: the same main-thread CoreLocation path the app uses → file
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from qtui.locator import Locator
        app = QApplication(sys.argv[:1])
        got = []

        def found(lat, lon, precise):
            got.append((lat, lon, precise))
            if precise:
                app.quit()
        loc = Locator()
        loc.found.connect(found)
        loc.request()
        QTimer.singleShot(15000, app.quit)
        app.exec()
        open("/tmp/spoofr_loc.txt", "w").write(str(got[-1] if got else None))
        sys.exit(0)
    elif args and args[0] == "--selftest":
        import importlib
        sys.argv = sys.argv[:1]          # server.py reads a port from argv at import
        ok = True
        for m in ("applog", "core", "macui", "portable", "server",
                  "qtui.app", "qtui.geo", "qtui.route", "qtui.tilemap", "qtui.session",
                  "qtui.panel", "qtui.welcome", "qtui.mapview", "qtui.sidebar", "qtui.locator",
                  "pymobiledevice3", "pymobiledevice3.__main__",
                  "PySide6.QtWidgets", "PySide6.QtNetwork",
                  "CoreLocation", "geocoder", "requests", "gpxpy", "segno", "psutil"):
            try:
                importlib.import_module(m)
                print("OK  ", m)
            except Exception as e:
                ok = False
                print("FAIL", m, e)
        print("SELFTEST", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    else:
        from qtui.app import main as gui_main
        gui_main()


if __name__ == "__main__":
    main()
