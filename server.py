"""Spoofr — Wi-Fi control server (drive it from your phone).

Run on the Mac (the host) with sudo so it can build the tunnel:
    sudo .venv/bin/python server.py

It prints a URL like http://192.168.x.x:8765/?t=<token>. Open that in Safari on
your iPhone (same Wi-Fi) and the phone becomes the full control surface. The Mac
talks to the iPhone over Wi-Fi via pymobiledevice3 (see core.py); the phone talks
to the Mac over HTTP. Stdlib only — no web framework, live updates via polling.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core

WEB = Path(__file__).resolve().parent / "web"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765  # override: server.py 8766
TOKEN = os.environ.get("SPOOFER_TOKEN") or secrets.token_urlsafe(16)  # launcher can pin it


class State:
    """One spoofing session, shared across request threads (device ops serialized)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.device: core.Device | None = None
        self.name = ""
        self.ios = ""
        self.live: tuple[float, float] | None = None   # current (live) location
        self.home: tuple[float, float] | None = None   # approx real location (IP)
        self.route_thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.dev_mode_needed = False

    def _halt_route(self) -> None:
        self.stop.set()
        t = self.route_thread
        if t and t.is_alive():
            t.join(timeout=3)

    def connect(self) -> dict:
        with self.lock:
            try:
                dev = core.connect()
            except core.DeveloperModeRequired:
                self.dev_mode_needed = True
                core.reveal_developer_mode()
                raise
            self.device = dev
            self.name, self.ios = dev.name, dev.ios
            self.dev_mode_needed = False
            if self.home is None:
                self.home = core.my_location()
            self.live = self.home
            return {"name": dev.name, "ios": dev.ios}

    def set_location(self, lat: float, lon: float) -> None:
        with self.lock:
            if not self.device:
                raise RuntimeError("Not connected")
            self._halt_route()
            self.device.set(lat, lon)
            self.live = (lat, lon)

    def start_route(self, points: list[tuple[float, float]], speed: float) -> None:
        with self.lock:
            if not self.device:
                raise RuntimeError("Not connected")
            self._halt_route()
            self.stop.clear()

            def run():
                def on_step(i, total, lat, lon):
                    self.live = (lat, lon)
                try:
                    self.device.play_route(points, speed, self.stop, on_step=on_step)
                except Exception:
                    pass

            self.route_thread = threading.Thread(target=run, daemon=True)
            self.route_thread.start()

    def stop_route(self) -> None:
        self.stop.set()

    def restore(self) -> None:
        with self.lock:
            self._halt_route()
            if self.device:
                self.device.clear()
            self.live = self.home

    def status(self) -> dict:
        live = self.live
        return {
            "connected": self.device is not None,
            "name": self.name,
            "ios": self.ios,
            "dev_mode_needed": self.dev_mode_needed,
            "route_active": bool(self.route_thread and self.route_thread.is_alive()),
            "live": {"lat": live[0], "lon": live[1]} if live else None,
        }


state = State()


class Handler(BaseHTTPRequestHandler):
    def _ok(self, q) -> bool:
        return q.get("t", [None])[0] == TOKEN

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
                 ".css": "text/css"}.get(path.suffix, "application/octet-stream")
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
        if u.path in ("/app.js", "/style.css"):
            self._file(WEB / u.path.lstrip("/"))
            return
        if not self._ok(q):
            self._json({"error": "bad token"}, 403)
            return
        if u.path == "/status":
            self._json(state.status())
        elif u.path == "/geocode":
            try:
                lat, lon = core.geocode(q.get("q", [""])[0])
                self._json({"lat": lat, "lon": lon})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif u.path == "/locate":
            loc = core.my_location()
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
    ip = _lan_ip()
    url = f"http://{ip}:{PORT}/?t={TOKEN}"
    print("\n  Spoofr — open this in Safari on your iPhone")
    print("  (same Wi-Fi as this Mac):\n", flush=True)
    print(f"      {url}\n", flush=True)
    print(f"  (also reachable locally at http://127.0.0.1:{PORT}/?t={TOKEN} )\n", flush=True)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except OSError as e:
        print(f"  Could not bind port {PORT}: {e}", flush=True)
        print(f"  Something else is using it — try another port:  server.py {PORT + 1}\n", flush=True)


if __name__ == "__main__":
    main()
