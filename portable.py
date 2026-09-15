"""Portable mode, hand iPhone control to the phone over Wi-Fi.

The desktop app uses this to flip into "iPhone" mode: make sure the Wi-Fi tunnel
is up (elevating once with the macOS password dialog if needed), run the
phone-control server, and produce a QR the phone scans. The Mac stays the host;
the phone becomes the control surface.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE / ".venv" / "bin" / "python"
TUNNELD_PORT = 49151
TUNNELD_LOG = "/tmp/spoofr-tunneld.log"


def _helper_cmd(*args: str) -> list[str]:
    """Command that re-invokes this app in a helper mode (--tunneld / --server).

    Frozen bundle: the app executable dispatches on argv. Dev: venv python +
    spoofr_app.py, which is the same dispatch with none of the UI attached, so the
    root tunnel helper never imports a GUI toolkit."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [str(PY), str(HERE / "spoofr_app.py"), *args]


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def port_open(host: str, port: int) -> bool:
    s = socket.socket(); s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def free_port(start: int = 8765) -> int:
    for p in range(start, start + 25):
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", p)); return p
        except OSError:
            continue
        finally:
            s.close()
    return start


def _kill_my_servers() -> None:
    """Stop any non-root server.py we previously started (keep to one)."""
    try:
        import psutil
        me = os.getuid()
        for p in psutil.process_iter(["pid", "cmdline", "uids"]):
            try:
                cl = " ".join(p.info.get("cmdline") or [])
                if ("server.py" in cl or "--server" in cl) \
                        and p.uids().real == me and p.pid != os.getpid():
                    p.kill()
            except Exception:
                pass
    except Exception:
        pass


def detached_cmd(argv: list[str], log: str) -> str:
    """A shell command that starts `argv` in the background and lets go of it.

    Deliberately not nohup. Under `do shell script ... with administrator
    privileges` nohup dies with "can't detach from console: Inappropriate ioctl
    for device" and never execs the command at all — so the user typed their
    password, osascript reported success, and the tunnel still never came up
    (see the root-owned /tmp/spoofr-tunneld.log). It works fine unprivileged,
    which is what made it easy to miss.

    Redirecting all three streams inside a backgrounded subshell is enough: the
    subshell exits at once and the child is reparented to launchd.
    """
    cmd = " ".join(shlex.quote(c) for c in argv)
    return f"( {cmd} > {log} 2>&1 < /dev/null & )"


def ensure_tunnel() -> None:
    """Make sure tunneld is on :49151. If it's down and we're not root, start it
    as a root daemon via ONE macOS admin prompt; then the non-root app attaches."""
    if port_open("127.0.0.1", TUNNELD_PORT):
        return
    if os.geteuid() == 0:
        return  # running as root → core._Tunneld.ensure() will spawn it
    sh = detached_cmd(_helper_cmd("--tunneld"), TUNNELD_LOG)
    ascmd = sh.replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(["osascript", "-e",
                        f'do shell script "{ascmd}" with administrator privileges'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-128" in err or "User canceled" in err:
            raise PermissionError("Admin password cancelled, the Wi-Fi tunnel needs it once.")
        raise RuntimeError(err.splitlines()[-1] if err else "couldn't start the Wi-Fi tunnel")
    for _ in range(48):
        if port_open("127.0.0.1", TUNNELD_PORT):
            return
        time.sleep(0.25)
    raise RuntimeError("the Wi-Fi tunnel didn't come up")


class Portable:
    """Runs server.py and exposes its URL + live status for the QR view."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.port: int | None = None
        self.token: str | None = None

    def start(self) -> str:
        ensure_tunnel()                          # may prompt for the admin password once
        _kill_my_servers()
        self.token = secrets.token_urlsafe(16)
        self.port = free_port(8765)
        env = dict(os.environ, SPOOFER_TOKEN=self.token)
        self.proc = subprocess.Popen(
            _helper_cmd("--server", str(self.port)),
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(48):
            if port_open("127.0.0.1", self.port):
                break
            time.sleep(0.25)
        else:
            self.stop()
            raise RuntimeError("the phone server didn't come up")
        self.url = f"http://{lan_ip()}:{self.port}/?t={self.token}"
        return self.url

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict | None:
        if not (self.port and self.token):
            return None
        try:
            u = f"http://127.0.0.1:{self.port}/status?t={self.token}"
            with urllib.request.urlopen(u, timeout=1.2) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None

    def qr_png(self) -> str:
        import segno
        tmp = Path(tempfile.gettempdir()) / "spoofer-qr.png"
        segno.make(self.url, error="m").save(str(tmp), scale=8, border=3)
        return str(tmp)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None
        self.url = self.port = self.token = None
