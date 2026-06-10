"""Spoofr — headless host mode: the Mac as an always-on spoofing box.

Runs the tunnel + the phone-control server (server.py) as a boot service, so the
desktop app never needs to be open: scan the QR once, bookmark the URL on your
iPhone, and the phone is the whole UI from then on — across reboots.

    sudo .venv/bin/python host.py install     # one-time: launchd daemon at boot
    .venv/bin/python host.py url              # the stable URL + QR to scan
    .venv/bin/python host.py status           # daemon / tunnel / phone health
    sudo .venv/bin/python host.py uninstall   # remove the daemon
    .venv/bin/python host.py run [port]       # foreground (what the daemon runs)

The token is generated once at install and stored in the LaunchDaemon plist, so
the URL — http://<this-mac>.local:8765/?t=<token> — never changes. The server is
token-gated and LAN-only by design; don't port-forward it to the internet.
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE / ".venv" / "bin" / "python"
LABEL = "com.spoofr.host"
PLIST = Path("/Library/LaunchDaemons") / f"{LABEL}.plist"
LOG = "/tmp/spoofr-host.log"
DEFAULT_PORT = 8765
TUNNELD_PORT = 49151


def _hostname_local() -> str:
    name = socket.gethostname()
    return name if name.endswith(".local") else name + ".local"


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _urls(port: int, token: str) -> list[str]:
    return [f"http://{_hostname_local()}:{port}/?t={token}",
            f"http://{_lan_ip()}:{port}/?t={token}"]


def _print_access(port: int, token: str) -> None:
    stable, ip = _urls(port, token)
    print("\n  Scan with your iPhone's Camera (same Wi-Fi), then bookmark it:\n")
    try:
        import segno
        qr = segno.make(stable, error="m")
        try:
            qr.terminal(compact=True, border=2)
        except TypeError:           # older segno: no compact/border kwargs
            qr.terminal()
    except Exception:
        pass
    print(f"\n      {stable}")
    print(f"      {ip}   (if .local doesn’t resolve)\n")


def _build_plist(token: str, port: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(PY), str(HERE / "host.py"), "run", str(port)],
        "WorkingDirectory": str(HERE),
        "EnvironmentVariables": {"SPOOFER_TOKEN": token},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": LOG,
        "StandardErrorPath": LOG,
    }


def _read_plist() -> tuple[str, int] | None:
    """(token, port) from the installed daemon, or None if not installed."""
    try:
        with PLIST.open("rb") as fh:
            d = plistlib.load(fh)
        token = d["EnvironmentVariables"]["SPOOFER_TOKEN"]
        args = d.get("ProgramArguments", [])
        port = int(args[-1]) if args and str(args[-1]).isdigit() else DEFAULT_PORT
        return token, port
    except Exception:
        return None


def _require_root(what: str) -> None:
    if os.geteuid() != 0:
        sys.exit(f"{what} needs root (it manages a LaunchDaemon):\n"
                 f"    sudo {PY} {HERE / 'host.py'} {what}")


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


# ---- commands -------------------------------------------------------------

def cmd_run(port: int) -> None:
    """Foreground host: pre-warm the tunnel (root), then serve the phone UI."""
    token = os.environ.get("SPOOFER_TOKEN")
    if not token:
        # ad-hoc run outside launchd: keep a stable token per user anyway
        import secrets
        tpath = Path.home() / ".spoofr" / "host-token"
        try:
            token = tpath.read_text().strip()
        except OSError:
            token = secrets.token_urlsafe(16)
            tpath.parent.mkdir(parents=True, exist_ok=True)
            tpath.write_text(token)
        os.environ["SPOOFER_TOKEN"] = token

    if os.geteuid() == 0:
        import core
        try:
            print("Starting the iPhone tunnel…", flush=True)
            core.ensure_tunneld()
            print("Tunnel up.", flush=True)
        except Exception as e:
            # KeepAlive will relaunch us; meanwhile the server can still answer
            print(f"Tunnel didn’t start ({e}); serving anyway — first Connect will retry.",
                  flush=True)
    elif not _port_open(TUNNELD_PORT):
        print("Note: not root and no tunnel on :49151 — the phone’s Connect will fail\n"
              "until the tunnel is up (install the daemon, or start the desktop app once).",
              flush=True)

    _print_access(port, token)
    sys.argv = ["server", str(port)]      # server.py reads its port from argv at import
    import server
    server.main()


def cmd_install(port: int) -> None:
    _require_root("install")
    existing = _read_plist()
    if existing:
        token = existing[0]               # keep the URL (and phone bookmarks) stable
    else:
        import secrets
        token = secrets.token_urlsafe(16)
    _launchctl("bootout", f"system/{LABEL}")          # replace any older copy
    with PLIST.open("wb") as fh:
        plistlib.dump(_build_plist(token, port), fh)
    PLIST.chmod(0o644)
    r = _launchctl("bootstrap", "system", str(PLIST))
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {(r.stderr or r.stdout).strip()}")
    print(f"Installed — Spoofr now hosts at boot (log: {LOG}).")
    _print_access(port, token)


def cmd_uninstall() -> None:
    _require_root("uninstall")
    _launchctl("bootout", f"system/{LABEL}")
    try:
        PLIST.unlink()
    except FileNotFoundError:
        pass
    print("Uninstalled — Spoofr no longer runs at boot.")


def cmd_status() -> None:
    info = _read_plist()
    if not info:
        print("Daemon: not installed   (sudo .venv/bin/python host.py install)")
    else:
        token, port = info
        loaded = _launchctl("print", f"system/{LABEL}").returncode == 0
        print(f"Daemon: {'running' if loaded else 'installed but not loaded'}   port {port}")
        print(f"Tunnel: {'up' if _port_open(TUNNELD_PORT) else 'DOWN'}   (:{TUNNELD_PORT})")
        if _port_open(port):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/status?t={token}", timeout=2) as r:
                    st = json.loads(r.read().decode())
                if st.get("connected"):
                    print(f"Phone:  connected · {st.get('name')} · iOS {st.get('ios')}")
                else:
                    print("Phone:  waiting (open the bookmarked URL on the iPhone)")
            except Exception as e:
                print(f"Server: unreachable ({e})")
        else:
            print(f"Server: not listening on :{port}")


def cmd_url() -> None:
    info = _read_plist()
    if not info:
        sys.exit("Not installed — run:  sudo .venv/bin/python host.py install")
    token, port = info
    _print_access(port, token)


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "status"
    port = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else DEFAULT_PORT
    if cmd == "run":
        cmd_run(port)
    elif cmd == "install":
        cmd_install(port)
    elif cmd == "uninstall":
        cmd_uninstall()
    elif cmd == "status":
        cmd_status()
    elif cmd == "url":
        cmd_url()
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv[1:])
