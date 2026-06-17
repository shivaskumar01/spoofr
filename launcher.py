"""Spoofr, standalone QR launcher (superseded by the app's built-in iPhone tab).

Run `.venv/bin/python launcher.py` for a standalone QR window. Click
Start: it brings up the phone-control server and shows a QR code + link. Scan the
QR with your iPhone's Camera (same Wi-Fi) to open the controller in Safari.

Starting the tunnel needs admin rights. If one is already running, no password is
needed; otherwise macOS shows its standard password dialog (no Terminal).

While running, the launcher polls the server and shows live health (iPhone
connected / waiting / tunnel down), auto-restarts the server if it dies, and
surfaces clear errors.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import customtkinter as ctk
import segno
from PIL import Image

HERE = Path(__file__).resolve().parent
PY = HERE / ".venv" / "bin" / "python"
PMD = [str(PY), "-m", "pymobiledevice3"]   # the pymobiledevice3 CLI in our venv
TUNNELD_PORT = 49151

# palette (matches the app)
BG = "#0a0e17"; PANEL = "#111a2c"; GHOST = "#1c2740"; GHOST_HI = "#283655"
BLUE = "#3b82f6"; BLUE_HI = "#5c9bff"; TEXT = "#eaf0fb"; MUTED = "#8a97b4"
GREEN = "#2bd07a"; AMBER = "#f4b740"; RED = "#ff5c5c"

ctk.set_appearance_mode("dark")


class _AdminCancelled(Exception):
    """User dismissed the macOS admin-password dialog."""


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _port_open(host: str, port: int) -> bool:
    s = socket.socket(); s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _free_port(start: int = 8765) -> int:
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
    """Stop any non-root server.py we previously started (keeps things to one)."""
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline", "uids"]):
            try:
                cl = " ".join(p.info.get("cmdline") or [])
                if "server.py" in cl and "python" in (p.info.get("name") or "") \
                        and p.uids().real == os.getuid() and p.pid != os.getpid():
                    p.kill()
            except Exception:
                pass
    except Exception:
        pass


class Launcher(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Spoofr")
        self.geometry("440x690")
        self.minsize(440, 690)
        self.configure(fg_color=BG)
        self.proc = None        # the non-root server subprocess (if we own it)
        self.url = None
        self.port = None
        self.token = None
        self.qr_img = None
        self._running = False    # a server session is meant to be up
        self._monitor = None     # health-poll thread
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        ctk.CTkLabel(self, text="◉  Spoofr", text_color=TEXT,
                     font=ctk.CTkFont(size=21, weight="bold")).pack(pady=(28, 2))
        ctk.CTkLabel(self, text="Control your iPhone's location from your phone.",
                     text_color=MUTED, font=ctk.CTkFont(size=13)).pack(pady=(0, 18))

        self.status = ctk.CTkLabel(self, text="Not running", text_color=MUTED,
                                   font=ctk.CTkFont(size=14, weight="bold"))
        self.status.pack(pady=(0, 14))

        self.start_btn = ctk.CTkButton(self, text="Start", command=self.on_start,
                                       width=190, height=46, corner_radius=12, fg_color=BLUE,
                                       hover_color=BLUE_HI, text_color="#ffffff",
                                       font=ctk.CTkFont(size=15, weight="bold"))
        self.start_btn.pack(pady=(0, 20))

        self.card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=16)  # holds QR (shown when running)
        self.qr_label = ctk.CTkLabel(self.card, text="")
        self.qr_label.pack(padx=16, pady=16)
        self.url_label = ctk.CTkLabel(self, text="", text_color=TEXT, wraplength=380,
                                      justify="center", font=ctk.CTkFont(size=12))
        self.copy_btn = ctk.CTkButton(self, text="Copy link", command=self.on_copy, width=140, height=34,
                                      corner_radius=10, fg_color=GHOST, hover_color=GHOST_HI, text_color=TEXT)
        self.tip = ctk.CTkLabel(self, text="", text_color=MUTED, wraplength=380, justify="center",
                                font=ctk.CTkFont(size=12))

        # one-time setup (anchored to the bottom): enable wireless so the cable is never needed again
        self.wifi_status = ctk.CTkLabel(self, text="", text_color=MUTED, wraplength=380,
                                        justify="center", font=ctk.CTkFont(size=12))
        self.wifi_status.pack(side="bottom", pady=(0, 14))
        self.wifi_btn = ctk.CTkButton(self, text="⚡  Enable wireless (one-time)", command=self.on_enable_wifi,
                                      width=240, height=32, corner_radius=10, fg_color="transparent",
                                      border_width=1, border_color=GHOST_HI, hover_color=GHOST,
                                      text_color=MUTED, font=ctk.CTkFont(size=12))
        self.wifi_btn.pack(side="bottom", pady=(0, 4))

    def _status(self, text, color):
        """Thread-safe status update."""
        self.after(0, lambda: self.status.configure(text=text, text_color=color))

    def _api_status(self) -> dict | None:
        """Ask our own server how it's doing. Returns the /status dict or None."""
        if not (self.port and self.token):
            return None
        try:
            url = f"http://127.0.0.1:{self.port}/status?t={self.token}"
            with urllib.request.urlopen(url, timeout=1.2) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None

    # ---- start / stop ----

    def on_start(self):
        if self._running:
            self.on_stop(); return
        self.start_btn.configure(state="disabled", text="Starting…")
        self._status("Starting…", AMBER)
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        try:
            _kill_my_servers()
            self.token = secrets.token_urlsafe(16)
            self.port = _free_port(8765)
            if _port_open("127.0.0.1", TUNNELD_PORT):
                self._start_nonroot()          # tunnel already up → no password
            else:
                self._status("Waiting for the macOS password…", AMBER)
                self._start_root()             # native admin dialog, then root server
            for _ in range(48):
                if _port_open("127.0.0.1", self.port):
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("the server didn't come up (another app may be using the port)")
            self.url = f"http://{_lan_ip()}:{self.port}/?t={self.token}"
            self._running = True
            self.after(0, self._show_running)
            self._start_monitor()
        except _AdminCancelled:
            self._reset_to_start("Admin password cancelled, the tunnel needs it once to start.")
        except Exception as e:
            self._reset_to_start(f"Couldn't start: {e}")

    def _reset_to_start(self, message: str):
        self._status(message, RED)
        self.after(0, lambda: self.start_btn.configure(state="normal", text="Start",
                                                       fg_color=BLUE, hover_color=BLUE_HI))

    def _start_nonroot(self):
        env = dict(os.environ, SPOOFER_TOKEN=self.token)
        self.proc = subprocess.Popen([str(PY), str(HERE / "server.py"), str(self.port)],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _start_root(self):
        sh = (f"cd {HERE} && SPOOFER_TOKEN={self.token} nohup {PY} server.py {self.port} "
              f"> /tmp/spoofer-server.log 2>&1 &")
        ascmd = sh.replace("\\", "\\\\").replace('"', '\\"')
        r = subprocess.run(["osascript", "-e",
                            f'do shell script "{ascmd}" with administrator privileges'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            if "-128" in err or "User canceled" in err:
                raise _AdminCancelled()
            raise RuntimeError(err.splitlines()[-1] if err else "couldn't start the tunnel")
        self.proc = None  # root server isn't a child we can poll/terminate directly

    def _show_running(self):
        self._status("●  Waiting for your phone, scan the QR", AMBER)
        self.start_btn.configure(state="normal", text="Stop", fg_color=GHOST, hover_color=GHOST_HI)
        tmp = Path(tempfile.gettempdir()) / "spoofer-qr.png"
        segno.make(self.url, error="m").save(str(tmp), scale=8, border=3)  # dark-on-white = reliable
        pil = Image.open(tmp)
        self.qr_img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(240, 240))
        self.qr_label.configure(image=self.qr_img)
        self.card.pack(pady=(0, 12))
        self.url_label.configure(text=self.url); self.url_label.pack(pady=(0, 10))
        self.copy_btn.pack(pady=(0, 8))
        self.tip.configure(text="On your iPhone (same Wi-Fi): open Camera, point at the QR, tap the link.")
        self.tip.pack(pady=(0, 12))

    # ---- live health monitor ----

    def _start_monitor(self):
        if self._monitor and self._monitor.is_alive():
            return
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def _monitor_loop(self):
        while self._running:
            time.sleep(2.0)
            if not self._running:
                return
            if self.proc is not None and self.proc.poll() is not None:  # our server died
                self.after(0, self._recover_server)
                return
            tunnel_up = _port_open("127.0.0.1", TUNNELD_PORT)
            port_up = _port_open("127.0.0.1", self.port) if self.port else False
            st = self._api_status() if port_up else None
            self.after(0, lambda st=st, tu=tunnel_up, pu=port_up: self._render_health(st, tu, pu))

    def _render_health(self, st, tunnel_up, port_up):
        if not self._running:
            return
        if not tunnel_up:
            self.status.configure(text="⚠  Tunnel stopped, click Stop, then Start to restore.",
                                  text_color=RED)
            return
        if not port_up or st is None:
            self.status.configure(text="…  Server starting / not responding", text_color=AMBER)
            return
        if st.get("connected"):
            nm = st.get("name") or "iPhone"
            ios = st.get("ios") or ""
            extra = f" · iOS {ios}" if ios else ""
            self.status.configure(text=f"●  {nm} connected{extra}", text_color=GREEN)
        else:
            self.status.configure(text="●  Waiting for your phone, scan the QR", text_color=AMBER)

    def _recover_server(self):
        if not self._running:
            return
        if _port_open("127.0.0.1", TUNNELD_PORT):
            self.status.configure(text="●  Server hiccup, auto-restarting…", text_color=AMBER)
            try:
                self._start_nonroot()       # same port + token → the phone's session survives
                self._start_monitor()
            except Exception as e:
                self.status.configure(text=f"⚠  Couldn't auto-restart: {e}", text_color=RED)
        else:
            self.status.configure(text="⚠  Server & tunnel stopped, click Stop, then Start.",
                                  text_color=RED)

    # ---- one-time wireless enable (so the cable is never needed again) ----

    def on_enable_wifi(self):
        self.wifi_btn.configure(state="disabled", text="Enabling…")
        self._wifi_msg("Looking for your iPhone over USB…", AMBER)
        threading.Thread(target=self._enable_wifi_worker, daemon=True).start()

    def _wifi_msg(self, text, color):
        self.after(0, lambda: self.wifi_status.configure(text=text, text_color=color))

    def _enable_wifi_worker(self):
        try:
            if not self._usb_connected():
                self._wifi_msg("Plug your iPhone in with the cable, unlock it, then click again.", RED)
                return
            self._wifi_msg("Enabling wireless on the iPhone…", AMBER)
            subprocess.run(PMD + ["lockdown", "wifi-connections", "--state", "on"],
                           capture_output=True, text=True, timeout=30)
            chk = subprocess.run(PMD + ["lockdown", "wifi-connections"],
                                 capture_output=True, text=True, timeout=20)
            if '"EnableWifiConnections": true' in chk.stdout:
                self._wifi_msg("✓  Wireless enabled, unplug the cable; it stays on from now on.", GREEN)
            else:
                tail = (chk.stderr or "").strip().splitlines()
                self._wifi_msg("Couldn't confirm it turned on" + (f": {tail[-1]}" if tail else "."), RED)
        except subprocess.TimeoutExpired:
            self._wifi_msg("The iPhone didn't respond, unlock it and try again.", RED)
        except Exception as e:
            self._wifi_msg(f"Failed: {e}", RED)
        finally:
            self.after(0, lambda: self.wifi_btn.configure(state="normal",
                                                          text="⚡  Enable wireless (one-time)"))

    def _usb_connected(self) -> bool:
        try:
            out = subprocess.run(PMD + ["usbmux", "list"], capture_output=True, text=True, timeout=15)
        except Exception:
            return False
        try:
            data = json.loads(out.stdout)
            if isinstance(data, list):
                return any((d or {}).get("ConnectionType") == "USB" for d in data)
        except Exception:
            pass
        return '"ConnectionType": "USB"' in out.stdout

    def on_copy(self):
        if not self.url:
            return
        self.clipboard_clear(); self.clipboard_append(self.url)
        self.copy_btn.configure(text="Copied!")
        self.after(1200, lambda: self.copy_btn.configure(text="Copy link"))

    def on_stop(self):
        self._running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        else:  # root server we started via osascript
            try:
                subprocess.run(["osascript", "-e",
                                'do shell script "pkill -f \\"server.py\\"" with administrator privileges'],
                               check=False)
            except Exception:
                pass
        self.proc = None; self.url = None; self.port = None; self.token = None
        self.card.pack_forget(); self.url_label.pack_forget()
        self.copy_btn.pack_forget(); self.tip.pack_forget()
        self.start_btn.configure(text="Start", fg_color=BLUE, hover_color=BLUE_HI)
        self._status("Stopped", MUTED)

    def _on_close(self):
        self._running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.destroy()


if __name__ == "__main__":
    Launcher().mainloop()
