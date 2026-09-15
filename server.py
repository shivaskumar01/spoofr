"""Spoofr, Wi-Fi control server (drive it from your phone).

Run on the Mac (the host) with sudo so it can build the tunnel:
    sudo .venv/bin/python server.py

It prints a URL like http://192.168.x.x:8765/?t=<token>. Open that in Safari on
your iPhone (same Wi-Fi) and the phone becomes the full control surface. The Mac
talks to the iPhone over Wi-Fi via pymobiledevice3 (see core.py); the phone talks
to the Mac over HTTP. Stdlib only, no web framework, live updates via polling.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core
from qtui import geo, route      # pure helpers: no Qt, no pymobiledevice3

WEB = (Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent) / "web"
# Port override: `spoofr_app.py --server 8766`. Tolerant of a non-numeric argv
# (a bare int() here made the module unimportable from anything with flags).
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8765
TOKEN = os.environ.get("SPOOFER_TOKEN") or secrets.token_urlsafe(16)  # launcher can pin it

# Served without a token (they are just the UI). MapLibre is vendored rather than
# pulled from a CDN: the phone is often on a Wi-Fi that can reach this Mac and
# nothing else, and a blank page with no controls is not a usable fallback.
STATIC = ("/app.js", "/style.css", "/maplibre-gl.js", "/maplibre-gl.css")


class State:
    """One spoofing session, shared across request threads.

    Mirrors the desktop app's DeviceBridge: a single write path, a liveness
    monitor that actually exercises the developer channel, and a bounded
    auto-reconnect. Without those, status() reported `connected` forever after one
    successful connect, so the phone happily drove a session that had been dead
    for half an hour.
    """

    RECONNECT_WINDOW = 120.0     # keep trying this long before giving up
    HEARTBEAT_EVERY = 15.0       # re-assert the spoof this often to prove the channel

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.device: core.Device | None = None
        self.name = ""
        self.ios = ""
        self.link = ""                                 # "USB" / "Wi-Fi" / "USB + Wi-Fi"
        self.live: tuple[float, float] | None = None   # what the phone's map shows
        self.spoof: tuple[float, float] | None = None  # what the iPhone is actually set to
        self.home: tuple[float, float] | None = None   # approx real location
        self.route_thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.dev_mode_needed = False
        self.lost = False                              # session died since last connect
        self._connect_lock = threading.Lock()
        self._connecting = False
        self._reconnecting = False
        self._monitor_gen = 0
        self._reconnect_gen = 0

    # ---- connect ----------------------------------------------------------

    def connect(self) -> dict:
        """Open a session, or report one that is already open or in progress.

        The phone retries /connect on a timer, and the Developer Mode wizard
        retries on another. Without this guard each retry became a full
        core.connect() queued on self.lock, piling up threads behind a connect
        that can take a minute.
        """
        with self._connect_lock:
            if self.device is not None:
                return self._session()
            if self._connecting:
                return {"connecting": True}
            self._connecting = True
        try:
            return self._connect()
        finally:
            self._connecting = False

    def _connect(self) -> dict:
        with self.lock:
            try:
                dev = core.connect()
            except core.DeveloperModeRequired:
                self.dev_mode_needed = True
                core.reveal_developer_mode()
                raise
            self.device = dev
            self.name, self.ios, self.link = dev.name, dev.ios, dev.link
            self.dev_mode_needed = False
            self.lost = False
            if self.home is None:
                self.home = geo.current_location()
            if self.live is None:
                self.live = self.home
            core._log(f"phone server connected: {dev.name} iOS {dev.ios} via {dev.link}")
        self._start_monitor()
        return self._session()

    def _session(self) -> dict:
        return {"name": self.name, "ios": self.ios, "link": self.link}

    # ---- the single write path --------------------------------------------

    def push(self, lat: float, lon: float) -> bool:
        """Move the iPhone. False means the fix didn't land and the session is gone."""
        dev = self.device
        if dev is None:
            return False
        try:
            dev.set(lat, lon)
        except core.Cancelled:
            return False                 # a restore/stop overtook it
        except Exception as e:
            self._session_lost(dev, e)
            return False
        self.live = self.spoof = (lat, lon)
        return True

    def set_location(self, lat: float, lon: float) -> None:
        if not self.device:
            raise RuntimeError("Not connected")
        self._halt_route()
        if not self.push(lat, lon):
            raise RuntimeError("Lost the iPhone, reconnecting… try again in a moment.")

    # ---- route ------------------------------------------------------------

    def _halt_route(self) -> None:
        self.stop.set()
        t = self.route_thread
        if t and t.is_alive():
            t.join(timeout=3)

    def start_route(self, points: list[tuple[float, float]], speed: float) -> None:
        if not self.device:
            raise RuntimeError("Not connected")
        self._halt_route()
        self.stop.clear()
        path = route.route_points(points, max(speed, 0.3), dt=1.0)

        def run():
            for lat, lon in path:
                if self.stop.is_set():
                    return
                if not self.push(lat, lon):
                    return               # push() already started the reconnect
                if self.stop.wait(1.0):
                    return

        self.route_thread = threading.Thread(target=run, daemon=True)
        self.route_thread.start()

    def stop_route(self) -> None:
        self.stop.set()

    # ---- restore ----------------------------------------------------------

    def restore(self) -> None:
        self._halt_route()
        dev = self.device
        if dev:
            try:
                dev.clear()              # suspends in-flight fixes itself
            except Exception as e:
                self._session_lost(dev, e)
                raise
        self.spoof = None
        self.live = self.home

    # ---- liveness + auto-reconnect ----------------------------------------

    def _start_monitor(self) -> None:
        self._monitor_gen += 1
        threading.Thread(target=self._monitor, args=(self._monitor_gen,),
                         daemon=True).start()

    def _monitor(self, gen: int) -> None:
        misses = 0
        last_beat = time.monotonic()
        while gen == self._monitor_gen:
            time.sleep(3.0)
            dev = self.device
            if dev is None or gen != self._monitor_gen:
                return
            st = core.link_status(dev.serial)
            if st is None:
                pass                     # transient usbmux hiccup: no information
            elif st:
                misses = 0
                if st != self.link:
                    self.link = dev.link = st
            else:
                misses += 1
                if misses >= 2:          # ~6s gone: really unplugged, not a blip
                    self._session_lost(dev, RuntimeError("the iPhone went away"))
                    return
            # usbmux visibility says nothing about whether the developer tunnel
            # under it still works, so re-assert the spoof as a real round-trip
            if time.monotonic() - last_beat >= self.HEARTBEAT_EVERY:
                last_beat = time.monotonic()
                spot = self.spoof
                busy = bool(self.route_thread and self.route_thread.is_alive())
                if spot and not busy and not self.push(spot[0], spot[1]):
                    return

    def _session_lost(self, dead, err: Exception) -> None:
        if self.device is not dead:
            return                       # someone else already handled it
        core._log(f"phone server lost the session: {err!r}")
        self.device = None
        self.lost = True
        self.link = ""
        self._monitor_gen += 1
        self.stop.set()
        threading.Thread(target=lambda: dead.close(clear=False), daemon=True).start()
        if self._reconnecting:
            return
        self._reconnecting = True
        self._reconnect_gen += 1
        threading.Thread(target=self._reconnect, args=(self._reconnect_gen,),
                         daemon=True).start()

    def _reconnect(self, gen: int) -> None:
        deadline = time.monotonic() + self.RECONNECT_WINDOW
        delay = 3.0
        try:
            while time.monotonic() < deadline and gen == self._reconnect_gen:
                time.sleep(delay)
                if gen != self._reconnect_gen:
                    return
                try:
                    if self.connect().get("name"):
                        core._log("phone server reconnected")
                        spot = self.spoof
                        if spot:
                            self.push(spot[0], spot[1])   # put the spoof back
                        return
                except Exception:
                    pass
                delay = min(delay * 1.6, 20.0)
            core._log("phone server gave up reconnecting")
        finally:
            if gen == self._reconnect_gen:
                self._reconnecting = False

    # ---- status -----------------------------------------------------------

    def status(self) -> dict:
        live = self.live
        return {
            "connected": self.device is not None,
            "connecting": self._connecting,
            "reconnecting": self._reconnecting,
            "lost": self.lost and self.device is None,
            "name": self.name,
            "ios": self.ios,
            "link": self.link,
            "dev_mode_needed": self.dev_mode_needed,
            "route_active": bool(self.route_thread and self.route_thread.is_alive()),
            "live": {"lat": live[0], "lon": live[1]} if live else None,
        }

    def shutdown(self) -> None:
        """Hand the phone back cleanly on SIGTERM (the desktop app terminates us
        when you switch back to This Mac). clear=False, so the spoof survives the
        hand-off exactly like every other teardown path."""
        self._monitor_gen += 1
        self._reconnect_gen += 1
        self.stop.set()
        dev, self.device = self.device, None
        if dev:
            try:
                dev.close(clear=False)
            except Exception:
                pass


state = State()


class Handler(BaseHTTPRequestHandler):
    def _ok(self, q) -> bool:
        return hmac.compare_digest(q.get("t", [""])[0] or "", TOKEN)

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        ctype = {".html": "text/html", ".js": "text/javascript",
                 ".css": "text/css", ".map": "application/json"}.get(
                     path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # always serve the latest UI on refresh
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._file(WEB / "index.html")
            return
        if u.path in STATIC:
            self._file(WEB / u.path.lstrip("/"))
            return
        if not self._ok(q):
            self._json({"error": "bad token"}, 403)
            return
        if u.path == "/status":
            self._json(state.status())
        elif u.path == "/geocode":
            try:
                lat, lon = geo.geocode(q.get("q", [""])[0])
                self._json({"lat": lat, "lon": lon})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif u.path == "/locate":
            loc = state.home or geo.current_location()
            self._json({"lat": loc[0], "lon": loc[1]} if loc else {"error": "unknown"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._ok(q):
            self._json({"error": "bad token"}, 403)
            return
        body = self._body()
        try:
            if u.path == "/connect":
                self._json(state.connect())
            elif u.path == "/set":
                state.set_location(float(body["lat"]), float(body["lon"]))
                self._json({"ok": True})
            elif u.path == "/route":
                pts = [(float(a), float(b)) for a, b in body["points"]]
                state.start_route(pts, float(body.get("speed", 1.4)))
                self._json({"ok": True})
            elif u.path == "/stop":
                state.stop_route()
                self._json({"ok": True})
            elif u.path == "/restore":
                state.restore()
                self._json({"ok": True})
            elif u.path == "/devmode/reveal":
                core.reveal_developer_mode()
                self._json({"ok": True})
            else:
                self.send_error(404)
        except core.DeveloperModeRequired:
            self._json({"dev_mode_needed": True})
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def log_message(self, *a) -> None:  # quiet
        pass


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    import applog
    applog.install_hooks()

    # The desktop app terminates us when you switch back to This Mac; hand the
    # phone back cleanly instead of dying mid-session.
    def _bye(signum, frame):
        state.shutdown()
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            pass

    ip = _lan_ip()
    url = f"http://{ip}:{PORT}/?t={TOKEN}"
    print("\n  Spoofr, open this in Safari on your iPhone")
    print("  (same Wi-Fi as this Mac):\n", flush=True)
    print(f"      {url}\n", flush=True)
    print(f"  (also reachable locally at http://127.0.0.1:{PORT}/?t={TOKEN} )\n", flush=True)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except OSError as e:
        print(f"  Could not bind port {PORT}: {e}", flush=True)
        print(f"  Something else is using it, try another port:  server.py {PORT + 1}\n", flush=True)


if __name__ == "__main__":
    main()
