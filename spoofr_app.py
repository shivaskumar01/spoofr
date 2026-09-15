"""Entry point for the packaged Spoofr.app (PyInstaller bundle).

The frozen executable re-invokes itself for the privileged helpers (portable.py's
_helper_cmd), so dispatch those here; otherwise launch the native Qt UI. Kept as a
single top-level module so PyInstaller has one clean entry.
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--tunneld":
        # privileged tunnel daemon (launched as root via osascript)
        from pymobiledevice3.__main__ import main as pmd_main
        sys.argv = ["pymobiledevice3", "remote", "tunneld"]
        pmd_main()
    elif args and args[0] == "--server":
        # phone-control web server for iPhone (QR) mode
        sys.argv = ["server", args[1] if len(args) > 1 else "8765"]
        import server
        server.main()
    elif args and args[0] == "--loctest":
        # diagnostic: resolve current location (CoreLocation in the bundle) → file
        from qtui import geo
        open("/tmp/spoofr_loc.txt", "w").write(str(geo.current_location()))
        sys.exit(0)
    elif args and args[0] == "--selftest":
        import importlib
        sys.argv = sys.argv[:1]          # server.py reads a port from argv at import
        ok = True
        for m in ("applog", "core", "macui", "portable", "server",
                  "qtui.app", "qtui.geo", "qtui.route", "qtui.tilemap",
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
