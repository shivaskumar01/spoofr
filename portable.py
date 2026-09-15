"""Portable mode, hand iPhone control to the phone over Wi-Fi.

The desktop app uses this to flip into "iPhone" mode: make sure the Wi-Fi tunnel
is up (elevating once with the macOS password dialog if needed), run the
phone-control server, and produce a QR the phone scans. The Mac stays the host;
the phone becomes the control surface.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
PY = HERE / ".venv" / "bin" / "python"
TUNNELD_PORT = 49151
TUNNELD_LOG = "/tmp/spoofr-tunneld.log"

# iOS 18.2 removed QUIC, and pymobiledevice3 only picks TCP by default on Python
# 3.13+ (remote/common.py: DEFAULT = TCP if sys.version_info >= (3, 13) else
# QUIC). On 3.11 the daemon therefore tries QUIC, fails every handshake with
# QuicProtocolNotSupportedError, and publishes no tunnel at all -- while still
# listening on its port, so everything downstream looks like the phone simply
# isn't trusted. Ask for TCP explicitly; it is the default on 3.13+ anyway.
TUNNEL_PROTOCOL = "tcp"


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


def is_tunneld_cmd(cmd: str) -> bool:
    """Is this command line really one of our tunnel daemons?

    These pids get SIGKILLed as root, so the test has to be exact. A substring
    match is not: any process whose arguments merely *mention* --tunneld matches
    one, and while this was being written that included both the shell evaluating
    a command containing the word and a `python -c` whose inline source quoted an
    example. Either would have been killed.

    So --tunneld must directly follow the program it belongs to, in one of the two
    shapes _helper_cmd() produces — the frozen bundle, or a Python running
    spoofr_app.py — and inline-code invocations are never a daemon.
    """
    if re.search(r"(?:^|\s)-c(?:\s|$)", cmd):        # python -c / sh -c, not a daemon
        return False
    head = cmd.split(None, 1)[0] if cmd.strip() else ""
    if head.endswith(("/sh", "/zsh", "/bash", "/env")):
        return False
    if re.search(r"/Spoofr\s+--tunneld(?:\s|$)", cmd):            # packaged app
        return True
    return "python" in head and bool(                              # dev run
        re.search(r"spoofr_app\.py\s+--tunneld(?:\s|$)", cmd))


def _ps_lines() -> list[str]:
    """Every process on the machine as "pid command" lines (seam for tests)."""
    try:
        return subprocess.run(["ps", "-Ao", "pid=,command="],
                              capture_output=True, text=True, timeout=5).stdout.splitlines()
    except Exception:
        return []


def running_tunnelds() -> list[tuple[int, str]]:
    """Every running Spoofr tunnel daemon, as (pid, command line).

    Deliberately ps and not psutil: the daemon runs as root, and psutil cannot
    read another user’s command line without privileges — it would report no
    daemon at all, and we’d try to start another on an occupied port.

    All of them, not the first: a daemon that refuses to die keeps the port while
    later ones pile up behind it, idle and useless, and the one still answering is
    the oldest rather than the newest.
    """
    found = []
    for line in _ps_lines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit() and is_tunneld_cmd(cmd):
            found.append((int(pid), cmd))
    return found


def tunneld_pids() -> list[int]:
    return [pid for pid, _ in running_tunnelds()]


def _speaks_tcp(cmd: str) -> bool:
    return f"--protocol {TUNNEL_PROTOCOL}" in cmd or f"--protocol={TUNNEL_PROTOCOL}" in cmd


def tunnel_is_healthy() -> bool:
    """Is there exactly one daemon, and can it actually open a tunnel?

    Two or more means an older one is still holding the port and the newer ones
    never bound it, so whoever is answering is not the one we asked for — start
    over rather than trust it.
    """
    daemons = running_tunnelds()
    return len(daemons) == 1 and _speaks_tcp(daemons[0][1])


def tunnel_start_cmd(stale_pids: Optional[list[int]] = None) -> str:
    """The shell command that (re)starts the tunnel daemon under one admin prompt.

    SIGKILL, not SIGTERM. tunneld is uvicorn, and on TERM it tries to shut down
    gracefully — which never finishes while its tunnel tasks are stuck retrying a
    handshake that cannot succeed. A TERMed daemon was still alive and still
    holding the port half an hour later, with two replacements idling behind it
    because they could never bind. There is no state to lose: the daemon is a
    stateless supervisor, and dropping its tunnels is the point.

    Killing by pid rather than `pkill -f -- --tunneld`, whose pattern would match
    the very shell running this command.
    """
    start = detached_cmd(_helper_cmd("--tunneld", "--protocol", TUNNEL_PROTOCOL), TUNNELD_LOG)
    if not stale_pids:
        return start
    pids = " ".join(str(p) for p in stale_pids)
    return (f"kill {pids} 2>/dev/null ; sleep 1 ; kill -9 {pids} 2>/dev/null ; "
            f"sleep 1 ; {start}")


def ensure_tunnel() -> None:
    """Make sure a *working* tunneld is on :49151. If it's down (or up but unable
    to tunnel), start a fresh one as root via ONE macOS admin prompt; then the
    non-root app attaches."""
    if port_open("127.0.0.1", TUNNELD_PORT) and tunnel_is_healthy():
        return
    if os.geteuid() == 0:
        return  # running as root → core._Tunneld.ensure() will spawn it
    sh = tunnel_start_cmd(tunneld_pids())
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
