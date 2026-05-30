"""Spoofr — an iPhone location spoofer (dark customtkinter desktop app).

Open it from the Spoofr icon (or `.venv/bin/python gui.py`). It drives
your iPhone over a Wi-Fi tunnel; the first time the tunnel is needed it asks for
your macOS password once to start it, then attaches with no password after.

The top-left switch picks the mode: "This Mac" drives the phone from this map;
"iPhone" hands control to your phone's browser via a QR code (see portable.py).
"""

from __future__ import annotations

import json
import math
import queue
import random
import sys
import threading
import time
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
import tkintermapview
import tkintermapview.map_widget as _tkmw
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

import core
import portable

# Native macOS pinch-to-zoom. Tk emits no pinch event, so we attach an
# NSMagnificationGestureRecognizer to the window's NSView via pyobjc.
try:
    import objc
    from Foundation import NSObject
    from AppKit import NSApp, NSMagnificationGestureRecognizer

    class _PinchHandler(NSObject):
        def handleMagnify_(self, rec):
            try:
                if rec.state() == 2:  # NSGestureRecognizerStateChanged
                    self._app._on_pinch(rec.magnification() - self._last)
                self._last = rec.magnification()
            except Exception:
                pass

    _PINCH_OK = True
except Exception:
    _PINCH_OK = False

# Dark, labelled basemap (CARTO dark_all) — matches the app and never flashes white.
TILE_SERVER = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"

# --- palette: near-black canvas · deep-blue surfaces · crisp blue accent -
BG       = "#0a0e17"   # app background
PANEL    = "#111a2c"   # bars / map frame
ELEV     = "#1a2440"   # raised pills, inputs, controls
BORDER   = "#27344f"   # hairline dividers & outlines
BLUE     = "#3b82f6"   # primary accent
BLUE_HI  = "#5c9bff"   # primary hover
GHOST    = "#1c2740"   # secondary fill
GHOST_HI = "#283655"   # secondary hover
TEXT     = "#eaf0fb"   # primary text
MUTED    = "#8a97b4"   # secondary text
PIN      = "#ff5a5f"   # staged target pin
PIN_HI   = "#ff8488"
LIVE     = "#22d3ee"   # live (current) location marker
LIVE_HI  = "#67e8f9"
MAP_BG   = "#0b0f19"   # the duotone "black": tile placeholders, canvas bg, and corner-fill behind floating panels
# connection dot
GREY, GREEN, AMBER, RED = "#6b7687", "#2bd07a", "#f4b740", "#ff5c5c"

PAN_SPEED = 12   # map pixels moved per two-finger-scroll delta unit

ctk.set_appearance_mode("dark")

# Recolor the basemap to the project's black+blue by duotone-ing each tile. Contained
# to tkintermapview's tile loader — we shim the PhotoImage *name* in its module only;
# our markers create PIL.ImageTk images directly and are untouched.
_DUO_PRESETS = {
    "Dim":    dict(black=(9, 12, 20),  mid=(28, 56, 112),  white=(96, 142, 214)),
    "Normal": dict(black=(11, 15, 25), mid=(40, 80, 150),  white=(140, 185, 255)),
    "Bright": dict(black=(14, 19, 32), mid=(54, 104, 184), white=(176, 206, 255)),
}
_DUO = dict(_DUO_PRESETS["Normal"])


def _settings_path() -> Path:
    return Path.home() / ".spoofr" / "settings.json"


def _load_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text())
    except Exception:
        return {}


def _save_settings_dict(d: dict) -> None:
    try:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


def _duotone_tile(img):
    return ImageOps.colorize(img.convert("L"), **_DUO).convert("RGB")


class _TileImageShim:
    @staticmethod
    def PhotoImage(image=None, **kw):
        if image is not None and getattr(image, "size", None) == (256, 256):
            image = _duotone_tile(image)
        return ImageTk.PhotoImage(image, **kw)


_tkmw.ImageTk = _TileImageShim


def _draw_pin(fill, size=(40, 52)):
    """A polished teardrop pin — soft vertical gradient + white core, tip at bottom."""
    ss = 4
    w, h = size[0] * ss, size[1] * ss
    cx, r = w / 2, w * 0.38
    head = r + w * 0.07
    ang = math.radians(57)
    dx, dy = r * math.cos(ang), r * math.sin(ang)
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([cx - r, head - r, cx + r, head + r], fill=255)
    md.polygon([(cx - dx, head + dy), (cx + dx, head + dy), (cx, h - ss)], fill=255)
    light = tuple(min(c + 48, 255) for c in fill[:3])
    grad = Image.new("RGB", (w, h))
    gd = ImageDraw.Draw(grad)
    for yy in range(h):
        t = yy / h
        gd.line([(0, yy), (w, yy)], fill=tuple(int(light[i] * (1 - t) + fill[i] * t) for i in range(3)))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    hr = r * 0.42
    d.ellipse([cx - hr, head - hr, cx + hr, head + hr], fill=(255, 255, 255, 255))
    return img.resize(size, Image.LANCZOS)


def _draw_pulse_frame(phase, core=(34, 211, 238), size=46):
    """One frame of a pulsing location dot — expanding/fading ring behind a steady
    white-ringed core. phase in [0,1)."""
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    c = s / 2
    rmin, rmax = s * 0.13, s * 0.46
    r = rmin + (rmax - rmin) * phase
    a = int(140 * (1 - phase))
    if a > 0:
        ring = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse([c - r, c - r, c + r, c + r], fill=core[:3] + (a,))
        img = Image.alpha_composite(img, ring.filter(ImageFilter.GaussianBlur(s * 0.015)))
    d = ImageDraw.Draw(img)
    wr, cr = s * 0.20, s * 0.14
    d.ellipse([c - wr, c - wr, c + wr, c + wr], fill=(255, 255, 255, 255))
    d.ellipse([c - cr, c - cr, c + cr, c + cr], fill=core[:3] + (255,))
    return img.resize((size, size), Image.LANCZOS)


def _draw_dot(core, size=26, glow=True):
    """A location dot — filled core + white ring, optional soft glow. Centered."""
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    c = s / 2
    if glow:
        g = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(g).ellipse([c - s * 0.40, c - s * 0.40, c + s * 0.40, c + s * 0.40],
                                  fill=core[:3] + (90,))
        img = Image.alpha_composite(img, g.filter(ImageFilter.GaussianBlur(s * 0.07)))
    d = ImageDraw.Draw(img)
    rr, cr = s * 0.30, s * 0.21
    d.ellipse([c - rr, c - rr, c + rr, c + rr], fill=(255, 255, 255, 255))
    d.ellipse([c - cr, c - cr, c + cr, c + cr], fill=core)
    return img.resize((size, size), Image.LANCZOS)


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Spoofr")
        self.geometry("1060x780")
        self.minsize(860, 600)
        self.configure(fg_color=BG)

        self.device: core.Device | None = None
        self.mode = "teleport"
        self.speed = 1.4
        self._connecting = False

        self.pending: tuple[float, float] | None = None   # staged, not yet sent
        self.points: list[tuple[float, float]] = []
        self.markers: list = []
        self.path = None
        self.loc_marker = None
        self.live_marker = None          # live (current) location dot
        self._home = None                # approximate real location (IP-based)
        self.stop = threading.Event()
        self.player: threading.Thread | None = None
        self._ui_q: queue.Queue = queue.Queue()
        self._pinch_accum = 0.0          # magnification accrued by the native pinch callback
        self._pan_dx = self._pan_dy = 0.0  # scroll-pan pixels, applied once per _drain frame
        self._precache_after = None      # debounce id for neighbouring-zoom pre-cache
        self._precaching = False
        self._wizard = None              # Developer Mode onboarding dialog
        self._wizard_polling = False
        self._resize_after = None        # throttle id for map resize redraws
        self.app_mode = "mac"            # "mac" (desktop map) | "iphone" (portable QR)
        self.portable = portable.Portable()
        self._portable_poll = False
        self.portable_qr_img = None
        self._suggest_after = None       # search autocomplete debounce id
        self._suggest_items = []         # current suggestion list
        self.settings = _load_settings()
        self._guide_picked = self._guide_set = self._guide_portable = False
        self.menu_open = False
        self._sections = {}
        self.saved = self.settings.get("saved", [])      # [{name,lat,lon}] favorites
        self.recent = self.settings.get("recent", [])    # [{lat,lon}] most-recent first
        self._live_pos = None         # current spoofed (lat,lon) — where the You dot is
        self._walk_vec = (0.0, 0.0)   # (north, east) unit vector; non-zero while walking
        self._walk_pos = None         # position the joystick walks from
        self._walking = False         # walk worker running
        self._keys_down = set()       # arrow keys currently held
        self._key_release_after = {}  # per-key auto-repeat debounce ids

        self._build()
        if self.settings.get("show_guide", True) and not self.settings.get("seen_guide", False):
            self.after(700, self._first_run_guide)
        self._drain()
        self.after(300, self._install_pinch)  # native pinch-to-zoom
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ---------------------------------------------------------

    def _btn(self, parent, text, command, kind="primary", width=118, height=36):
        """Consistent button styling. kind = primary | soft | ghost."""
        common = dict(text=text, command=command, width=width, height=height,
                      corner_radius=10, font=self.f_btn)
        if kind == "primary":
            return ctk.CTkButton(parent, fg_color=BLUE, hover_color=BLUE_HI,
                                 text_color="#ffffff", **common)
        if kind == "ghost":
            return ctk.CTkButton(parent, fg_color="transparent", border_width=1,
                                 border_color=BORDER, hover_color=GHOST, text_color=TEXT, **common)
        return ctk.CTkButton(parent, fg_color=GHOST, hover_color=GHOST_HI, text_color=TEXT, **common)

    def _build(self) -> None:
        self.f_title = ctk.CTkFont(size=15, weight="bold")
        self.f_btn = ctk.CTkFont(size=13, weight="bold")
        self.f_body = ctk.CTkFont(size=13)
        self.f_hint = ctk.CTkFont(size=12)

        # ---- header: wordmark + status pill (left) · actions (right) ----
        header = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=60)
        header.pack(fill="x")
        header.pack_propagate(False)

        self.menu_btn = ctk.CTkButton(header, text="☰", width=40, height=36, corner_radius=10,
                                      fg_color="transparent", hover_color=GHOST, text_color=TEXT,
                                      font=ctk.CTkFont(size=18), command=self._toggle_menu)
        self.menu_btn.pack(side="left", padx=(14, 0), pady=12)
        mark = ctk.CTkFrame(header, fg_color="transparent")
        mark.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(mark, text="◉", font=ctk.CTkFont(size=17), text_color=BLUE).pack(side="left")
        ctk.CTkLabel(mark, text="Spoofr", font=self.f_title, text_color=TEXT).pack(side="left", padx=(8, 0))

        pill = ctk.CTkFrame(header, fg_color=ELEV, corner_radius=15)
        pill.pack(side="left", padx=16, pady=14)
        self.dot = ctk.CTkLabel(pill, text="●", font=ctk.CTkFont(size=12), text_color=GREY, width=10)
        self.dot.pack(side="left", padx=(13, 0), pady=6)
        self.status = ctk.CTkLabel(pill, text="Not connected", font=self.f_body, text_color=TEXT)
        self.status.pack(side="left", padx=(7, 15))

        self.connect_btn = self._btn(header, "Connect", self.on_connect, "primary", width=118)
        self.connect_btn.pack(side="right", padx=(0, 20), pady=12)
        self.restore_btn = self._btn(header, "Restore GPS", self.on_restore, "ghost", width=118)
        self.restore_btn.pack(side="right", padx=(0, 10), pady=12)

        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # ---- controls row: mode switch + (route mode) speed & playback ----
        ctl = ctk.CTkFrame(self, fg_color=BG, corner_radius=0, height=56)
        ctl.pack(fill="x")
        ctl.pack_propagate(False)

        self.app_seg = ctk.CTkSegmentedButton(
            ctl, values=["This Mac", "iPhone"], command=self._on_app_mode,
            fg_color=ELEV, selected_color=BLUE, selected_hover_color=BLUE_HI,
            unselected_color=ELEV, unselected_hover_color=GHOST_HI,
            text_color=TEXT, corner_radius=9, height=34, font=self.f_btn)
        self.app_seg.set("This Mac")
        self.app_seg.pack(side="left", padx=(20, 14), pady=11)

        self.mode_seg = ctk.CTkSegmentedButton(
            ctl, values=["Teleport", "Route"], command=self._on_mode,
            fg_color=ELEV, selected_color=BLUE, selected_hover_color=BLUE_HI,
            unselected_color=ELEV, unselected_hover_color=GHOST_HI,
            text_color=TEXT, corner_radius=9, height=34, font=self.f_btn)
        self.mode_seg.set("Teleport")
        self.mode_seg.pack(side="left", pady=11)

        self.route_ctl = ctk.CTkFrame(ctl, fg_color="transparent")
        ctk.CTkLabel(self.route_ctl, text="Speed", text_color=MUTED, font=self.f_body).pack(side="left", padx=(8, 10))
        self.speed_slider = ctk.CTkSlider(self.route_ctl, from_=0.5, to=35, command=self._on_speed,
                                          width=160, height=18, fg_color=ELEV,
                                          button_color=BLUE, button_hover_color=BLUE_HI, progress_color=BLUE)
        self.speed_slider.set(1.4)
        self.speed_slider.pack(side="left")
        self.speed_lbl = ctk.CTkLabel(self.route_ctl, text="1.4 m/s", text_color=TEXT, width=62, font=self.f_body)
        self.speed_lbl.pack(side="left", padx=(10, 0))
        self._btn(self.route_ctl, "Start", self.on_start, "primary", width=72, height=32).pack(side="left", padx=(14, 4))
        self._btn(self.route_ctl, "Stop", self.on_stop, "soft", width=64, height=32).pack(side="left", padx=4)
        self._btn(self.route_ctl, "Clear", self.on_clear_route, "soft", width=64, height=32).pack(side="left", padx=4)

        # Map, in a rounded frame for a card-like edge.
        self.wrap = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14)
        self.wrap.pack(fill="both", expand=True, padx=14, pady=(2, 8))
        self.map = tkintermapview.TkinterMapView(self.wrap, corner_radius=12)
        self.map.pack(fill="both", expand=True, padx=3, pady=3)
        self.map.set_tile_server(TILE_SERVER, max_zoom=20)
        # Start over a random familiar city until the phone connects — never mid-ocean.
        _start = random.choice([
            (47.6062, -122.3321),   # Seattle
            (37.7749, -122.4194),   # San Francisco
            (40.7128,  -74.0060),   # New York City
            (25.7617,  -80.1918),   # Miami
            (48.8566,    2.3522),   # Paris
        ])
        self._home = _start
        self.map.set_position(*_start)
        self.map.set_zoom(11)
        # Dark placeholders + canvas bg so un-loaded tiles blend with the dark
        # basemap while panning/zooming instead of flashing white.
        self._blank_tile = ImageTk.PhotoImage(
            Image.new("RGB", (self.map.tile_size, self.map.tile_size), (11, 15, 25)))
        self.map.empty_tile_image = self._blank_tile
        self.map.not_loaded_tile_image = self._blank_tile
        self.map.canvas.configure(bg=MAP_BG)
        self._make_marker_icons()
        # Throttle resize: tkintermapview redraws the whole tile grid on every
        # <Configure>, which flickers during a window drag. Redraw at ~14fps instead.
        self.map.unbind("<Configure>")
        self.map.bind("<Configure>", self._on_map_resize)
        self.map.add_left_click_map_command(self._on_map_click)
        self.map.add_right_click_menu_command(
            label="Drop pin here", command=self._on_map_click, pass_coords=True)
        self.map.canvas.configure(cursor="crosshair")  # precise location-picking cursor
        # Two-finger scroll pans; pinch zooms (native, see _install_pinch).
        self.map.canvas.bind("<MouseWheel>", self._on_pan)
        self.map.canvas.bind("<Shift-MouseWheel>", self._on_pan_h)
        self.map.canvas.bind("<Double-Button-1>", self._on_double_click)  # double-tap = zoom in
        # Hide tkintermapview's tiny top-left zoom buttons; we add nicer ones below.
        for _b in (self.map.button_zoom_in, self.map.button_zoom_out):
            _b.draw = lambda *a, **k: None
        self.map.canvas.delete("button")

        # Search — a floating pill, top-center (clear of the zoom control).
        self.search_bar = ctk.CTkFrame(self.map, fg_color=PANEL, corner_radius=14,
                                       border_width=1, border_color=BORDER, bg_color=MAP_BG)
        self.search_bar.place(relx=0.5, y=16, anchor="n")
        self.search_entry = ctk.CTkEntry(
            self.search_bar, placeholder_text="Search address, city, or place", width=300, height=38,
            border_width=0, fg_color="transparent", text_color=TEXT, font=self.f_body)
        self.search_entry.pack(side="left", padx=(16, 4), pady=6)
        self.search_entry.bind("<Return>", self._on_search_enter)
        self.search_entry.bind("<KeyRelease>", self._on_search_type)
        self.search_entry.bind("<Escape>", lambda e: self._hide_suggestions())
        self.search_entry.bind("<FocusOut>", lambda e: self.after(180, self._hide_suggestions))
        self._btn(self.search_bar, "Go", self._on_search, "primary", width=52, height=30).pack(side="left", padx=(0, 6))
        # autocomplete dropdown — placed just below the bar when there are matches
        self.search_box = ctk.CTkFrame(self.map, fg_color=ELEV, corner_radius=12,
                                       border_width=1, border_color=BORDER, bg_color=MAP_BG)

        # Commit button — floated bottom-center, shown only once a pin is staged.
        self.set_btn = ctk.CTkButton(self.map, text="Set location here", command=self._commit,
                                     width=224, height=48, corner_radius=24, text_color="#ffffff",
                                     fg_color=BLUE, hover_color=BLUE_HI, bg_color=MAP_BG,
                                     font=ctk.CTkFont(size=15, weight="bold"))

        # Zoom control — one rounded pill (＋ / －), bottom-right.
        zoom = ctk.CTkFrame(self.map, fg_color=PANEL, corner_radius=12,
                            border_width=1, border_color=BORDER, bg_color=MAP_BG)
        zoom.place(relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")
        zfont = ctk.CTkFont(size=20, weight="bold")
        ctk.CTkButton(zoom, text="＋", width=42, height=40, corner_radius=10, fg_color="transparent",
                      hover_color=GHOST, text_color=TEXT, font=zfont,
                      command=lambda: self._zoom_at(1)).pack(padx=3, pady=(3, 0))
        ctk.CTkFrame(zoom, fg_color=BORDER, height=1, width=34).pack()
        ctk.CTkButton(zoom, text="－", width=42, height=40, corner_radius=10, fg_color="transparent",
                      hover_color=GHOST, text_color=TEXT, font=zfont,
                      command=lambda: self._zoom_at(-1)).pack(padx=3, pady=(0, 3))

        # Live coordinate readout — top-left, mono.
        self.coord_readout = ctk.CTkLabel(self.map, text="", text_color=LIVE_HI,
                                          font=ctk.CTkFont(family="Menlo", size=11),
                                          fg_color=PANEL, corner_radius=8, bg_color=MAP_BG)
        # Joystick / walk pad — bottom-left, shown only while connected.
        self._build_walk_pad()
        for _k in ("Up", "Down", "Left", "Right"):   # arrow keys walk too (guarded vs. typing)
            self.bind(f"<KeyPress-{_k}>", self._key_walk_press)
            self.bind(f"<KeyRelease-{_k}>", self._key_walk_release)

        # Hint / action-feedback line.
        self.hint = ctk.CTkLabel(self, text="", text_color=MUTED, anchor="w", font=self.f_hint)
        self.hint.pack(fill="x", padx=22, pady=(2, 12))

        self._build_portable_view()   # the "iPhone" mode QR view (hidden until selected)
        self._build_sidebar()         # the slide-out menu (Getting Started / Settings / About)
        self.after(500, self._pulse_tick)   # animate the live "You" dot

        self._on_speed(1.4)
        self._on_mode("Teleport")
        self._set_hint("Click the map or search a place — then Connect your iPhone.")

    def _on_mode(self, value: str) -> None:
        self.mode = value.lower()
        if self.mode == "route":
            self.route_ctl.pack(side="left", padx=(6, 0))
            self._set_hint("Click the map to drop waypoints, then press Start.")
        else:
            self.route_ctl.pack_forget()
            self._set_hint("Click the map or search to drop a pin, then tap “Set location here”.")
        self._update_set_btn()

    def _on_speed(self, value) -> None:
        self.speed = float(value)
        self.speed_lbl.configure(text=f"{self.speed:.1f} m/s")

    # ---- app mode: This Mac (desktop map) ↔ iPhone (portable QR) --------

    def _build_portable_view(self) -> None:
        """The 'iPhone' mode view — a centered card with the QR + live status.
        Built once, hidden until the user switches to iPhone mode."""
        self.portable_view = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        card = ctk.CTkFrame(self.portable_view, fg_color=PANEL, corner_radius=18,
                            border_width=1, border_color=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(card, text="Control from your iPhone",
                     font=ctk.CTkFont(size=20, weight="bold"), text_color=TEXT).pack(padx=46, pady=(30, 4))
        ctk.CTkLabel(card, text="Open the Camera on your iPhone and point it at this code.\n"
                                "Your phone just needs to be on the same Wi-Fi.",
                     font=self.f_body, text_color=MUTED, justify="center").pack(padx=46, pady=(0, 18))
        self.portable_qr_label = ctk.CTkLabel(card, text="Starting…", text_color=MUTED,
                                              width=248, height=248, font=self.f_body)
        self.portable_qr_label.pack(padx=46)
        self.portable_url_label = ctk.CTkLabel(card, text="", text_color=TEXT,
                                               font=self.f_hint, wraplength=320, justify="center")
        self.portable_url_label.pack(padx=46, pady=(14, 6))
        self.portable_copy_btn = self._btn(card, "Copy link", self._copy_portable_link, "soft",
                                           width=120, height=32)
        self.portable_copy_btn.pack(pady=(0, 8))
        self.portable_status_label = ctk.CTkLabel(card, text="●  Starting the phone server…",
                                                  font=ctk.CTkFont(size=13, weight="bold"), text_color=AMBER)
        self.portable_status_label.pack(padx=46, pady=(8, 30))

    def _on_app_mode(self, value: str) -> None:
        new = "iphone" if "iPhone" in value else "mac"
        if new == self.app_mode:
            return
        self.app_mode = new
        if new == "iphone":
            self._enter_iphone_mode()
        else:
            self._enter_mac_mode()

    def _enter_iphone_mode(self) -> None:
        self._guide_portable = True
        self._update_guide()
        # hand the device off: stop any route and drop the desktop's connection
        self.stop.set()
        old, self.device = self.device, None
        if old:
            self._bg(old.close)
        # hide the Mac-only controls + map; disable Mac actions
        self.mode_seg.pack_forget()
        self.route_ctl.pack_forget()
        self.set_btn.place_forget()
        self._show_walk_pad(False)
        self.coord_readout.place_forget()
        self.connect_btn.configure(state="disabled")
        self.restore_btn.configure(state="disabled")
        self.wrap.pack_forget()
        self.portable_view.pack(fill="both", expand=True, padx=14, pady=(2, 8), before=self.hint)
        # reset the card, then start the phone server off-thread
        self.portable_qr_label.configure(image=None, text="Starting…")
        self.portable_url_label.configure(text="")
        self.portable_status_label.configure(text="●  Starting the phone server…", text_color=AMBER)
        self._set_status("Starting portable…", AMBER)
        self._set_hint("Starting the phone server… you may be asked for your password once.")
        self._portable_poll = True
        self._bg(self._start_portable_worker)

    def _enter_mac_mode(self) -> None:
        self._portable_poll = False
        self._bg(self.portable.stop)              # stop the server (releases the device)
        self.portable_view.pack_forget()
        self.wrap.pack(fill="both", expand=True, padx=14, pady=(2, 8), before=self.hint)
        self.mode_seg.pack(side="left", pady=11)
        self.connect_btn.configure(state="normal")
        self.restore_btn.configure(state="normal")
        self._on_mode(self.mode.capitalize())     # restore route controls if needed
        self._set_status("Not connected", GREY)
        self._set_hint("Click Connect to drive your iPhone from this Mac.")

    def _start_portable_worker(self) -> None:
        try:
            url = self.portable.start()
        except Exception as e:
            self._post(lambda e=e: self._portable_failed(str(e)))
            return
        self._post(lambda: self._show_qr(url))
        while self._portable_poll:                # poll the server for live phone status
            if not self.portable.alive():
                self._post(lambda: self.portable_status_label.configure(
                    text="⚠  Phone server stopped — switch to This Mac and back to retry.",
                    text_color=RED))
                return
            st = self.portable.status()
            self._post(lambda st=st: self._render_portable_status(st))
            time.sleep(2.0)

    def _show_qr(self, url: str) -> None:
        try:
            pil = Image.open(self.portable.qr_png())
            self.portable_qr_img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(248, 248))
            self.portable_qr_label.configure(image=self.portable_qr_img, text="")
        except Exception as e:
            self.portable_qr_label.configure(text=f"QR error: {e}")
        self.portable_url_label.configure(text=url)
        self._set_status("Waiting for your phone…", AMBER)
        self._set_hint("Scan the QR with your iPhone to take control. Switch back to “This Mac” anytime.")

    def _render_portable_status(self, st) -> None:
        if self.app_mode != "iphone":
            return
        if st and st.get("connected"):
            nm = st.get("name") or "iPhone"
            self.status.configure(text=f"iPhone in control · {nm}")
            self.dot.configure(text_color=GREEN)
            self.portable_status_label.configure(
                text=f"●  {nm} connected — controlling from your phone", text_color=GREEN)
        else:
            self.status.configure(text="Waiting for your phone…")
            self.dot.configure(text_color=AMBER)
            self.portable_status_label.configure(
                text="●  Waiting for your phone — scan the QR", text_color=AMBER)

    def _portable_failed(self, msg: str) -> None:
        self.portable_status_label.configure(text=f"⚠  {msg}", text_color=RED)
        self._set_status("Portable mode failed", RED)
        self._set_hint("Couldn’t start portable mode — switch back to This Mac and try again.")

    def _copy_portable_link(self) -> None:
        if not self.portable.url:
            return
        self.clipboard_clear(); self.clipboard_append(self.portable.url)
        self.portable_copy_btn.configure(text="Copied!")
        self.after(1200, lambda: self.portable_copy_btn.configure(text="Copy link"))

    # ---- side menu: Getting Started / Settings / About ------------------

    def _build_sidebar(self) -> None:
        self.sidebar = ctk.CTkFrame(self, width=320, corner_radius=0, fg_color=PANEL)
        top = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(22, 6))
        ctk.CTkLabel(top, text="Spoofr", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkButton(top, text="✕", width=32, height=32, corner_radius=8, fg_color="transparent",
                      hover_color=GHOST, text_color=MUTED, font=ctk.CTkFont(size=15),
                      command=self._close_menu).pack(side="right")
        ctk.CTkFrame(self.sidebar, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=(6, 10))
        nav = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        nav.pack(fill="x", padx=14)
        self.nav_btns = {}
        for key, label in (("places", "Places"), ("guide", "Getting Started"), ("settings", "Settings"), ("about", "About")):
            b = ctk.CTkButton(nav, text=label, anchor="w", height=38, corner_radius=9,
                              fg_color="transparent", hover_color=GHOST, text_color=TEXT,
                              font=self.f_btn, command=lambda k=key: self._show_menu_section(k))
            b.pack(fill="x", pady=2)
            self.nav_btns[key] = b
        ctk.CTkFrame(self.sidebar, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=10)
        self.menu_content = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.menu_content.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        self._build_places_section()
        self._build_guide_section()
        self._build_settings_section()
        self._build_about_section()
        self._show_menu_section("places")

    def _show_menu_section(self, key: str) -> None:
        for k, frame in self._sections.items():
            frame.pack_forget()
            self.nav_btns[k].configure(fg_color="transparent")
        self._sections[key].pack(fill="both", expand=True)
        self.nav_btns[key].configure(fg_color=GHOST)

    def _toggle_menu(self) -> None:
        self._close_menu() if self.menu_open else self._open_menu()

    def _open_menu(self) -> None:
        self.menu_open = True
        self.sidebar.place(x=-320, y=0, relheight=1.0)
        self.sidebar.lift()
        self._slide_menu(-320, 0, 12)

    def _close_menu(self) -> None:
        if not self.menu_open:
            return
        self.menu_open = False
        self._slide_menu(0, -320, 11, hide=True)

    def _slide_menu(self, frm: int, to: int, steps: int, hide: bool = False, i: int = 0) -> None:
        try:
            self.sidebar.place_configure(x=int(frm + (to - frm) * (i / steps)))
        except Exception:
            return
        if i < steps:
            self.after(11, lambda: self._slide_menu(frm, to, steps, hide, i + 1))
        elif hide:
            self.sidebar.place_forget()

    def _first_run_guide(self) -> None:
        self.settings["seen_guide"] = True
        self._save_settings()
        self._show_menu_section("guide")
        self._open_menu()

    def _save_settings(self) -> None:
        _save_settings_dict(self.settings)

    # -- Getting Started (interactive checklist) --

    def _build_guide_section(self) -> None:
        f = ctk.CTkFrame(self.menu_content, fg_color="transparent")
        self._sections["guide"] = f
        ctk.CTkLabel(f, text="Getting started", font=self.f_title, text_color=TEXT,
                     anchor="w").pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(f, text="Each step ticks off as you do it.", font=self.f_hint,
                     text_color=MUTED, anchor="w", justify="left", wraplength=272).pack(fill="x", pady=(0, 12))
        self._guide_rows = []
        for txt in ("Connect your iPhone — tap Connect (top right).",
                    "Pick a spot — search above, or click the map.",
                    "Press “Set location here” to move your iPhone.",
                    "Optional — tap the “iPhone” tab to control from your phone."):
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", pady=5)
            icon = ctk.CTkLabel(row, text="○", font=ctk.CTkFont(size=16), text_color=MUTED, width=22)
            icon.pack(side="left", anchor="n")
            lbl = ctk.CTkLabel(row, text=txt, font=self.f_body, text_color=TEXT, anchor="w",
                               justify="left", wraplength=230)
            lbl.pack(side="left", fill="x", expand=True)
            self._guide_rows.append((icon, lbl))
        self._guide_done = ctk.CTkLabel(f, text="", font=self.f_btn, text_color=GREEN, anchor="w",
                                        justify="left", wraplength=272)
        self._guide_done.pack(fill="x", pady=(14, 0))
        self._update_guide()

    def _update_guide(self) -> None:
        if not getattr(self, "_guide_rows", None):
            return
        done = [self.device is not None,
                self._guide_picked or self.pending is not None,
                self._guide_set,
                self._guide_portable]
        for (icon, _lbl), ok in zip(self._guide_rows, done):
            icon.configure(text="✓" if ok else "○", text_color=GREEN if ok else MUTED)
        self._guide_done.configure(text="✓  Nice — you’ve got the hang of it!" if all(done[:3]) else "")

    # -- Settings --

    def _build_settings_section(self) -> None:
        f = ctk.CTkFrame(self.menu_content, fg_color="transparent")
        self._sections["settings"] = f
        ctk.CTkLabel(f, text="Settings", font=self.f_title, text_color=TEXT,
                     anchor="w").pack(fill="x", pady=(2, 12))
        ctk.CTkLabel(f, text="Map brightness", font=self.f_body, text_color=MUTED,
                     anchor="w").pack(fill="x")
        self.bright_seg = ctk.CTkSegmentedButton(
            f, values=["Dim", "Normal", "Bright"], command=self._set_map_brightness,
            fg_color=ELEV, selected_color=BLUE, selected_hover_color=BLUE_HI,
            unselected_color=ELEV, unselected_hover_color=GHOST_HI, text_color=TEXT,
            height=32, font=self.f_hint)
        self.bright_seg.set(self.settings.get("brightness", "Normal"))
        self.bright_seg.pack(fill="x", pady=(5, 16))
        self.pulse_switch = ctk.CTkSwitch(f, text="Pulsing live marker", command=self._toggle_pulse,
                                          font=self.f_body, text_color=TEXT, progress_color=BLUE)
        self.pulse_switch.pack(fill="x", pady=7)
        (self.pulse_switch.select if self.settings.get("pulse", True) else self.pulse_switch.deselect)()
        self.guide_switch = ctk.CTkSwitch(f, text="Show guide on startup", command=self._toggle_show_guide,
                                          font=self.f_body, text_color=TEXT, progress_color=BLUE)
        self.guide_switch.pack(fill="x", pady=7)
        (self.guide_switch.select if self.settings.get("show_guide", True) else self.guide_switch.deselect)()

    def _set_map_brightness(self, name: str) -> None:
        self.settings["brightness"] = name
        self._save_settings()
        _DUO.clear(); _DUO.update(_DUO_PRESETS.get(name, _DUO_PRESETS["Normal"]))
        try:
            self.map.tile_image_cache.clear()
            self._apply_map_resize()
        except Exception:
            pass

    def _toggle_pulse(self) -> None:
        self.settings["pulse"] = bool(self.pulse_switch.get())
        self._save_settings()
        if not self.settings["pulse"] and self.live_marker is not None:
            try:
                self.live_marker.change_icon(self._pulse_frames[0])
            except Exception:
                pass

    def _toggle_show_guide(self) -> None:
        self.settings["show_guide"] = bool(self.guide_switch.get())
        self._save_settings()

    # -- About --

    def _build_about_section(self) -> None:
        f = ctk.CTkFrame(self.menu_content, fg_color="transparent")
        self._sections["about"] = f
        ctk.CTkLabel(f, text="About Spoofr", font=self.f_title, text_color=TEXT,
                     anchor="w").pack(fill="x", pady=(2, 8))
        text = ("Spoofr sets your iPhone’s GPS to anywhere on the map — every app on your "
                "phone then sees that location. No jailbreak.\n\n"
                "•  This Mac — drive it from this map.\n"
                "•  iPhone — scan a QR and control it from your phone over Wi-Fi.\n\n"
                "Spoofing is fine for development, privacy and games. Using it to defraud "
                "or to defeat court-ordered monitoring can be illegal — how you use it is "
                "on you.")
        ctk.CTkLabel(f, text=text, font=self.f_body, text_color=MUTED, anchor="w",
                     justify="left", wraplength=276).pack(fill="x")
        ctk.CTkLabel(f, text="Version 1.0  ·  iOS 17–26", font=self.f_hint, text_color=MUTED,
                     anchor="w").pack(fill="x", pady=(16, 0))

    # ---- saved / recent places + coordinates ----------------------------

    def _build_places_section(self) -> None:
        f = ctk.CTkScrollableFrame(self.menu_content, fg_color="transparent",
                                   scrollbar_button_color=GHOST, scrollbar_button_hover_color=GHOST_HI)
        self._sections["places"] = f
        ctk.CTkLabel(f, text="Places", font=self.f_title, text_color=TEXT, anchor="w").pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(f, text="Save the spots you use; recents are tracked automatically.",
                     font=self.f_hint, text_color=MUTED, anchor="w", justify="left", wraplength=248).pack(fill="x", pady=(0, 12))
        self._btn(f, "★  Save current spot", self._save_current_place, "soft", width=248, height=34).pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(f, text="SAVED", font=ctk.CTkFont(family="Menlo", size=10), text_color=MUTED, anchor="w").pack(fill="x", pady=(0, 4))
        self.saved_list = ctk.CTkFrame(f, fg_color="transparent")
        self.saved_list.pack(fill="x")
        ctk.CTkLabel(f, text="RECENT", font=ctk.CTkFont(family="Menlo", size=10), text_color=MUTED, anchor="w").pack(fill="x", pady=(14, 4))
        self.recent_list = ctk.CTkFrame(f, fg_color="transparent")
        self.recent_list.pack(fill="x")
        self._refresh_places()

    def _refresh_places(self) -> None:
        if not getattr(self, "saved_list", None):
            return
        for w in self.saved_list.winfo_children():
            w.destroy()
        for w in self.recent_list.winfo_children():
            w.destroy()
        if not self.saved:
            ctk.CTkLabel(self.saved_list, text="No saved spots yet.", font=self.f_hint, text_color=MUTED, anchor="w").pack(fill="x", pady=2)
        for i, p in enumerate(self.saved):
            self._place_row(self.saved_list, p["name"], p["lat"], p["lon"], on_delete=lambda idx=i: self._delete_saved(idx))
        if not self.recent:
            ctk.CTkLabel(self.recent_list, text="Nothing recent.", font=self.f_hint, text_color=MUTED, anchor="w").pack(fill="x", pady=2)
        for p in self.recent:
            self._place_row(self.recent_list, f"{p['lat']:.4f}, {p['lon']:.4f}", p["lat"], p["lon"],
                            on_save=lambda la=p["lat"], lo=p["lon"]: self._save_place_named(la, lo))

    def _place_row(self, parent, label, lat, lon, on_delete=None, on_save=None) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=1)
        ctk.CTkButton(row, text=label, anchor="w", height=32, corner_radius=8, fg_color="transparent",
                      hover_color=GHOST, text_color=TEXT, font=self.f_body,
                      command=lambda: self._use_place(lat, lon)).pack(side="left", fill="x", expand=True)
        if on_save:
            ctk.CTkButton(row, text="★", width=30, height=30, corner_radius=8, fg_color="transparent",
                          hover_color=GHOST, text_color=MUTED, command=on_save).pack(side="right")
        if on_delete:
            ctk.CTkButton(row, text="✕", width=30, height=30, corner_radius=8, fg_color="transparent",
                          hover_color=GHOST, text_color=MUTED, command=on_delete).pack(side="right")

    def _use_place(self, lat: float, lon: float) -> None:
        self._close_menu()
        self._goto(lat, lon)               # fly + stage the pin
        if self.device:
            self._commit()                 # one-click: also set it

    def _save_current_place(self) -> None:
        loc = self.pending or self._live_pos
        if not loc:
            self._set_hint("Pick or set a location first, then save it.")
            return
        self._save_place_named(loc[0], loc[1])

    def _save_place_named(self, lat: float, lon: float) -> None:
        dlg = ctk.CTkInputDialog(text=f"Name this spot  ({lat:.4f}, {lon:.4f}):", title="Save place")
        name = (dlg.get_input() or "").strip()
        if not name:
            return
        self.saved.insert(0, {"name": name, "lat": lat, "lon": lon})
        self.settings["saved"] = self.saved
        self._save_settings()
        self._refresh_places()
        self._set_hint(f"Saved “{name}”.")

    def _delete_saved(self, idx: int) -> None:
        if 0 <= idx < len(self.saved):
            self.saved.pop(idx)
            self.settings["saved"] = self.saved
            self._save_settings()
            self._refresh_places()

    def _add_recent(self, lat: float, lon: float) -> None:
        self.recent = [r for r in self.recent if abs(r["lat"] - lat) > 1e-4 or abs(r["lon"] - lon) > 1e-4]
        self.recent.insert(0, {"lat": lat, "lon": lon})
        self.recent = self.recent[:10]
        self.settings["recent"] = self.recent
        self._save_settings()
        self._refresh_places()

    @staticmethod
    def _parse_coords(s: str):
        import re
        m = re.fullmatch(r"\s*(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*", s)
        if not m:
            return None
        lat, lon = float(m.group(1)), float(m.group(2))
        return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None

    def _set_readout(self, lat: float, lon: float) -> None:
        self.coord_readout.configure(text=f"  ◉  {lat:.5f},  {lon:.5f}  ")
        if self.app_mode == "mac":
            self.coord_readout.place(relx=0.0, x=16, y=14, anchor="nw")

    # ---- joystick / walk mode --------------------------------------------

    def _build_walk_pad(self) -> None:
        self.walk_pad = ctk.CTkFrame(self.map, fg_color=PANEL, corner_radius=14,
                                     border_width=1, border_color=BORDER, bg_color=MAP_BG)
        grid = ctk.CTkFrame(self.walk_pad, fg_color="transparent")
        grid.pack(padx=8, pady=8)
        dirs = [("↖", (1, -1)), ("↑", (1, 0)), ("↗", (1, 1)),
                ("←", (0, -1)), ("•", (0, 0)), ("→", (0, 1)),
                ("↙", (-1, -1)), ("↓", (-1, 0)), ("↘", (-1, 1))]
        for i, (glyph, vec) in enumerate(dirs):
            stop = vec == (0, 0)
            cell = ctk.CTkLabel(grid, text=glyph, width=34, height=34, corner_radius=8,
                                fg_color=("transparent" if stop else ELEV),
                                text_color=(MUTED if stop else TEXT), font=ctk.CTkFont(size=15))
            cell.grid(row=i // 3, column=i % 3, padx=2, pady=2)
            if stop:
                cell.bind("<Button-1>", lambda e: self._walk_release())
            else:
                cell.bind("<ButtonPress-1>", lambda e, v=vec: self._walk_press(v))
                cell.bind("<ButtonRelease-1>", lambda e: self._walk_release())
                cell.bind("<Enter>", lambda e, c=cell: c.configure(fg_color=GHOST_HI))
                cell.bind("<Leave>", lambda e, c=cell: c.configure(fg_color=ELEV))

    def _show_walk_pad(self, show: bool) -> None:
        if show and self.app_mode == "mac":
            self.walk_pad.place(relx=0.0, rely=1.0, x=16, y=-16, anchor="sw")
        else:
            self.walk_pad.place_forget()
            self._walk_release()

    def _walk_press(self, vec) -> None:
        n, e = vec
        mag = math.hypot(n, e) or 1.0
        self._walk_vec = (n / mag, e / mag)
        self._start_walk()

    def _walk_release(self) -> None:
        self._walk_vec = (0.0, 0.0)

    def _start_walk(self) -> None:
        if self._walking:
            return
        if not self._need_device():
            self._walk_vec = (0.0, 0.0)
            return
        self._walk_pos = self._live_pos or self.map.get_position()
        self._walking = True
        self._bg(self._walk_worker)

    def _walk_worker(self) -> None:
        dt, n = 0.18, 0
        try:
            while self._walking and self.device is not None:
                vx, vy = self._walk_vec
                if (vx or vy) and self._walk_pos:
                    lat, lon = self._walk_pos
                    dist = max(self.speed, 0.3) * dt                      # metres this tick
                    lat += (vx * dist) / 111320.0
                    lon += (vy * dist) / (111320.0 * max(0.15, math.cos(math.radians(lat))))
                    self._walk_pos = (lat, lon)
                    try:
                        self.device.set(lat, lon)
                    except Exception:
                        pass
                    self._post(lambda la=lat, lo=lon: self._set_live(la, lo))
                    n += 1
                    if n % 3 == 0:                                        # follow the view ~3×/sec
                        self._post(lambda la=lat, lo=lon: self._safe_center(la, lo))
                time.sleep(dt)
        finally:
            self._walking = False

    def _safe_center(self, lat: float, lon: float) -> None:
        try:
            self.map.set_position(lat, lon)
        except Exception:
            pass

    def _key_walk_press(self, e) -> None:
        foc = self.focus_get()
        if foc is not None and foc.winfo_class() in ("Entry", "TEntry"):
            return                                                       # typing — let arrows edit text
        k = e.keysym
        aid = self._key_release_after.pop(k, None)
        if aid:
            self.after_cancel(aid)
        self._keys_down.add(k)
        self._update_walk_from_keys()

    def _key_walk_release(self, e) -> None:
        k = e.keysym
        self._key_release_after[k] = self.after(60, lambda: self._key_really_release(k))

    def _key_really_release(self, k: str) -> None:
        self._key_release_after.pop(k, None)
        self._keys_down.discard(k)
        self._update_walk_from_keys()

    def _update_walk_from_keys(self) -> None:
        n = ("Up" in self._keys_down) - ("Down" in self._keys_down)
        e = ("Right" in self._keys_down) - ("Left" in self._keys_down)
        if not n and not e:
            self._walk_vec = (0.0, 0.0)
        else:
            mag = math.hypot(n, e)
            self._walk_vec = (n / mag, e / mag)
            self._start_walk()

    # ---- main-thread UI pump (Tk is not thread-safe) --------------------

    def _bg(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _post(self, fn) -> None:
        """Schedule fn on the main thread. Safe to call from any thread."""
        self._ui_q.put(fn)

    def _drain(self) -> None:
        while True:
            try:
                fn = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass
        # Apply coalesced map gestures once per frame, on the Tk main loop.
        moved = False
        if self._pan_dx or self._pan_dy:
            dx, dy = self._pan_dx, self._pan_dy
            self._pan_dx = self._pan_dy = 0.0
            try:
                self._pan(dx, dy)
            except Exception:
                pass
            moved = True
        if self._pinch_accum:
            d, self._pinch_accum = self._pinch_accum, 0.0
            try:
                self.map.set_zoom(self.map.zoom + d * 3.0)
            except Exception:
                pass
            moved = True
        if moved:
            self._schedule_precache()
        self.after(33, self._drain)

    def _set_status(self, text: str, color: str) -> None:
        def apply():
            self.status.configure(text=text)
            self.dot.configure(text_color=color)
        self._post(apply)

    def _set_hint(self, text: str) -> None:
        self._post(lambda: self.hint.configure(text=text))

    def _need_device(self) -> bool:
        if self.device:
            return True
        self._set_hint("Connect to your iPhone first.")
        return False

    # ---- map centering --------------------------------------------------

    def _center(self, lat: float, lon: float, zoom: int) -> None:
        self.map.set_position(lat, lon)
        self.map.set_zoom(zoom)
        self._schedule_precache()

    def _schedule_precache(self) -> None:
        """Debounce: once the view stops moving, pre-load the next/prev zoom levels."""
        if self._precache_after is not None:
            try:
                self.after_cancel(self._precache_after)
            except Exception:
                pass
        self._precache_after = self.after(350, lambda: self._bg(self._precache_neighbors))

    def _precache_neighbors(self) -> None:
        """Download the ±1 zoom levels for the current view into the in-memory tile
        cache, so the next zoom step lands on ready tiles (no gap). Runs off-thread."""
        if self._precaching:
            return
        self._precaching = True
        try:
            m = self.map
            z = round(m.zoom)
            ulx, uly = m.upper_left_tile_pos
            lrx, lry = m.lower_right_tile_pos
            count = 0
            for dz in (1, -1):  # next zoom-in level first (most common), then zoom-out
                nz = z + dz
                if not (m.min_zoom <= nz <= m.max_zoom):
                    continue
                s = 2.0 ** dz
                n = 2 ** nz
                for x in range(int(ulx * s), int(lrx * s) + 1):
                    for y in range(int(uly * s), int(lry * s) + 1):
                        if not m.running or count >= 90:
                            return
                        if 0 <= x < n and 0 <= y < n and f"{nz}{x}{y}" not in m.tile_image_cache:
                            m.request_image(nz, x, y)
                            count += 1
        except Exception:
            pass
        finally:
            self._precaching = False

    def _on_pan(self, event) -> str:
        """Two-finger scroll → pan. Coalesced: the trackpad fires hundreds of
        events per swipe, so just accumulate here and redraw once per _drain frame."""
        self._pan_dy += event.delta * PAN_SPEED
        return "break"

    def _on_pan_h(self, event) -> str:
        """Horizontal two-finger scroll → pan left/right (coalesced)."""
        self._pan_dx += event.delta * PAN_SPEED
        return "break"

    def _install_pinch(self) -> None:
        """Attach a native macOS pinch (magnify) gesture recognizer to our window."""
        if not _PINCH_OK:
            return
        try:
            nswin = next((w for w in NSApp.windows() if str(w.title()) == self.title()), None)
            if nswin is None:
                return
            handler = _PinchHandler.alloc().init()
            handler._app = self
            handler._last = 0.0
            rec = NSMagnificationGestureRecognizer.alloc().initWithTarget_action_(handler, "handleMagnify:")
            nswin.contentView().addGestureRecognizer_(rec)
            self._pinch_handler, self._pinch_rec = handler, rec  # keep refs alive
        except Exception:
            pass

    def _on_pinch(self, delta: float) -> None:
        """Native pinch callback — fires in a Cocoa gesture context with no valid
        Python/Tk thread state, so it must NOT touch Tk (that aborts the process).
        Only accumulate; _drain applies the zoom on the Tk main loop."""
        self._pinch_accum += delta

    def _on_double_click(self, event) -> str:
        """Double-tap to zoom in toward the cursor."""
        self._zoom_at(1, event.x, event.y)
        return "break"

    def _zoom_at(self, step: float, x=None, y=None) -> None:
        if x is None:
            self.map.set_zoom(self.map.zoom + step)
        else:
            w = max(getattr(self.map, "width", 0) or self.map.canvas.winfo_width(), 1)
            h = max(getattr(self.map, "height", 0) or self.map.canvas.winfo_height(), 1)
            self.map.set_zoom(self.map.zoom + step, relative_pointer_x=x / w, relative_pointer_y=y / h)

    def _pan(self, dx: float, dy: float) -> None:
        """Shift the visible map by (dx, dy) pixels — same math as drag-panning."""
        m = self.map
        tx = m.lower_right_tile_pos[0] - m.upper_left_tile_pos[0]
        ty = m.lower_right_tile_pos[1] - m.upper_left_tile_pos[1]
        mx = (dx / max(m.width, 1)) * tx
        my = (dy / max(m.height, 1)) * ty
        m.upper_left_tile_pos = (m.upper_left_tile_pos[0] + mx, m.upper_left_tile_pos[1] + my)
        m.lower_right_tile_pos = (m.lower_right_tile_pos[0] + mx, m.lower_right_tile_pos[1] + my)
        m.check_map_border_crossing()
        m.draw_move()

    def _on_map_resize(self, event) -> None:
        """Debounced resize. tkintermapview redraws the whole tile grid on every
        <Configure> (flicker); a fire-once-early throttle instead left large windows
        with un-covered, cut-out tiles. So: record the new size now, then do ONE clean
        full redraw ~130ms after the resize settles (cancel+reschedule each event)."""
        m = self.map
        if event.width == m.width and event.height == m.height:
            return
        m.width, m.height = event.width, event.height
        if self._resize_after is not None:
            try:
                self.after_cancel(self._resize_after)
            except Exception:
                pass
        self._resize_after = self.after(130, self._apply_map_resize)

    def _apply_map_resize(self) -> None:
        self._resize_after = None
        m = self.map
        try:
            m.min_zoom = math.ceil(math.log2(math.ceil(max(m.width, 1) / m.tile_size)))
            if m.zoom < m.min_zoom:
                m.zoom = m.min_zoom
            m.set_zoom(m.zoom)      # fix the position vertices for the new size
            m.draw_move()           # load + draw tiles to cover the whole enlarged canvas
            m.draw_rounded_corners()
            self._schedule_precache()
        except Exception:
            pass

    # ---- connect / restore ----------------------------------------------

    def on_connect(self) -> None:
        if self._connecting:
            return
        self._connecting = True
        self.stop.set()
        old, self.device = self.device, None
        if old:
            self._bg(old.close)
        self.connect_btn.configure(state="disabled")
        self._set_status("Connecting…", AMBER)
        self._bg(self._connect_worker)

    def _connect_worker(self) -> None:
        try:
            portable.ensure_tunnel()   # bring up the Wi-Fi tunnel (asks for the password once if needed)
            device = core.connect(on_status=lambda m: self._set_status(m, AMBER))
            self.device = device
            self._set_status(f"Connected  ·  {device.name}  ·  iOS {device.ios}", GREEN)
            self._set_hint("Connected — drop a pin, paste coords, or use the ◉ walk pad (bottom-left).")
            self._post(self._update_guide)
            self._post(lambda: self._show_walk_pad(True))
        except core.DeveloperModeRequired:
            self._set_status("Developer Mode needed", AMBER)
            self._post(self._start_dev_mode_wizard)
        except Exception as e:
            msg = str(e)
            self._set_status("Not connected", RED)
            self._post(lambda: messagebox.showerror("Couldn’t connect", msg))
        finally:
            self._connecting = False
            self._post(lambda: self.connect_btn.configure(state="normal"))

    def on_restore(self) -> None:
        if not self._need_device():
            return
        self.stop.set()

        def work():
            if self.player and self.player.is_alive():
                self.player.join(timeout=5)
            try:
                self.device.clear()
                self._post(self._on_restored)
                self._set_hint("Real GPS restored. iOS reacquires in a few seconds.")
            except Exception as e:
                self._set_hint(f"Restore failed: {e}")
        self._bg(work)

    # ---- Developer Mode onboarding wizard -------------------------------

    def _start_dev_mode_wizard(self) -> None:
        if self._wizard is not None and self._wizard.winfo_exists():
            self._wizard.lift()
            return
        self._bg(core.reveal_developer_mode)  # surface the toggle on the phone

        w = ctk.CTkToplevel(self)
        self._wizard = w
        w.title("Enable Developer Mode")
        w.geometry("560x450")
        w.resizable(False, False)
        w.configure(fg_color=BG)
        w.transient(self)
        w.protocol("WM_DELETE_WINDOW", self._close_wizard)
        w.after(50, w.lift)

        ctk.CTkLabel(w, text="One-time setup: Developer Mode",
                     font=ctk.CTkFont(size=20, weight="bold"), text_color=TEXT).pack(pady=(26, 4), padx=26)
        ctk.CTkLabel(w, text="Apple requires Developer Mode to set your iPhone’s location. "
                             "It’s free, reversible anytime, and takes about 30 seconds.",
                     font=ctk.CTkFont(size=13), text_color=MUTED, wraplength=500,
                     justify="left").pack(pady=(0, 18), padx=26)

        box = ctk.CTkFrame(w, fg_color=PANEL, corner_radius=12)
        box.pack(fill="x", padx=26)
        for s in ("1.   On your iPhone:  Settings  ▸  Privacy & Security",
                  "2.   Scroll to  Developer Mode  and turn it On",
                  "3.   Tap  Restart  when prompted",
                  "4.   After reboot, unlock and tap  Turn On  (enter passcode)"):
            ctk.CTkLabel(box, text=s, font=ctk.CTkFont(size=14), text_color=TEXT,
                         anchor="w", justify="left", wraplength=480).pack(fill="x", padx=18, pady=7)

        self._wizard_status = ctk.CTkLabel(w, text="●  Waiting for Developer Mode…",
                                           font=ctk.CTkFont(size=13, weight="bold"), text_color=AMBER)
        self._wizard_status.pack(pady=(18, 6))
        ctk.CTkButton(w, text="Cancel", width=110, height=34, corner_radius=10,
                      fg_color=GHOST, hover_color=GHOST_HI, text_color=TEXT,
                      command=self._close_wizard).pack(pady=(2, 22))

        self._wizard_polling = True
        self._bg(self._poll_dev_mode)

    def _poll_dev_mode(self) -> None:
        while self._wizard_polling:
            try:
                on = core.developer_mode_status()
            except Exception:
                on = None
            if on:
                self._post(self._dev_mode_ready)
                return
            time.sleep(2.0)

    def _dev_mode_ready(self) -> None:
        self._wizard_polling = False
        if self._wizard is not None and self._wizard.winfo_exists():
            self._wizard_status.configure(text="✓  Developer Mode on — connecting…", text_color=GREEN)
        self.after(900, self._finish_wizard_and_connect)

    def _finish_wizard_and_connect(self) -> None:
        self._close_wizard()
        self.on_connect()

    def _close_wizard(self) -> None:
        self._wizard_polling = False
        w, self._wizard = self._wizard, None
        if w is not None and w.winfo_exists():
            try:
                w.grab_release()
            except Exception:
                pass
            w.destroy()

    # ---- teleport: stage a pin, then commit -----------------------------

    def _on_map_click(self, coords) -> None:
        lat, lon = coords
        if self.mode == "teleport":
            self._stage(lat, lon)
        else:
            self._add_point(lat, lon)

    def _stage(self, lat: float, lon: float) -> None:
        """Place/move the pin without touching the phone — just a candidate."""
        self.pending = (lat, lon)
        self._guide_picked = True
        self._mark_location(lat, lon)
        self._update_set_btn()
        self._update_guide()
        self._set_hint(f"Pinned {lat:.5f}, {lon:.5f} — tap “Set location here” to move your iPhone.")

    def _commit(self) -> None:
        if not self.pending or not self._need_device():
            return
        lat, lon = self.pending

        def work():
            try:
                self.device.set(lat, lon)
                self._post(lambda: self._on_committed(lat, lon))
                self._set_hint(f"Location set to {lat:.5f}, {lon:.5f}")
            except Exception as e:
                self._set_hint(f"Failed: {e}")
        self._set_hint(f"Setting location to {lat:.5f}, {lon:.5f}…")
        self._bg(work)

    def _update_set_btn(self) -> None:
        if self.mode == "teleport" and self.pending:
            self.set_btn.place(relx=0.5, rely=1.0, y=-18, anchor="s")
        else:
            self.set_btn.place_forget()

    # ---- address search --------------------------------------------------

    def _on_search(self, event=None) -> None:
        query = self.search_entry.get().strip()
        if not query:
            return
        c = self._parse_coords(query)
        if c:
            self._goto(*c)
            self._set_hint(f"Jumped to {c[0]:.5f}, {c[1]:.5f}")
            return
        self._set_hint(f"Searching for “{query}”…")
        self._bg(self._search_worker, query)

    def _search_worker(self, query: str) -> None:
        try:
            lat, lon = core.geocode(query)
        except Exception as e:
            self._set_hint(str(e))
            return
        self._post(lambda: self._goto(lat, lon))

    def _goto(self, lat: float, lon: float) -> None:
        self._center(lat, lon, 15)
        if self.mode == "route":
            self.mode_seg.set("Teleport")
            self._on_mode("Teleport")
        self._stage(lat, lon)

    # ---- search autocomplete --------------------------------------------

    def _on_search_enter(self, event=None) -> None:
        if self._parse_coords(self.search_entry.get().strip()):
            self._on_search()
        elif self._suggest_items:
            self._pick_suggestion(self._suggest_items[0])
        else:
            self._on_search()

    def _on_search_type(self, event=None) -> None:
        if event is not None and event.keysym in ("Return", "Escape", "Up", "Down", "Left", "Right", "Tab"):
            return
        q = self.search_entry.get().strip()
        if self._suggest_after is not None:
            try:
                self.after_cancel(self._suggest_after)
            except Exception:
                pass
            self._suggest_after = None
        if len(q) < 2 or self._parse_coords(q):
            self._hide_suggestions()
            return
        self._suggest_after = self.after(260, lambda: self._bg(self._suggest_worker, q))

    def _suggest_worker(self, q: str) -> None:
        items = core.suggest(q)
        self._post(lambda: self._show_suggestions(q, items))

    def _show_suggestions(self, q: str, items: list) -> None:
        if self.app_mode != "mac" or self.search_entry.get().strip() != q:
            return                      # stale: user kept typing or switched mode
        for w in self.search_box.winfo_children():
            w.destroy()
        self._suggest_items = items or []
        if not items:
            self._hide_suggestions()
            return
        n = len(items)
        for i, it in enumerate(items):
            self._make_suggest_row(it, first=(i == 0), last=(i == n - 1))
        self.search_box.place(relx=0.5, y=70, anchor="n")
        self.search_box.lift()

    def _make_suggest_row(self, it: dict, first: bool, last: bool) -> None:
        row = ctk.CTkFrame(self.search_box, fg_color="transparent", corner_radius=8)
        row.pack(fill="x", padx=6, pady=(6 if first else 2, 6 if last else 0))
        name = ctk.CTkLabel(row, text=it["label"], font=self.f_btn, text_color=TEXT,
                            anchor="w", justify="left", wraplength=320)
        widgets = [row, name]
        if it["secondary"]:
            name.pack(fill="x", padx=12, pady=(7, 0))
            sec = ctk.CTkLabel(row, text=it["secondary"], font=self.f_hint, text_color=MUTED,
                               anchor="w", justify="left", wraplength=320)
            sec.pack(fill="x", padx=12, pady=(0, 8))
            widgets.append(sec)
        else:
            name.pack(fill="x", padx=12, pady=8)
        for w in widgets:
            w.bind("<Enter>", lambda e, r=row: r.configure(fg_color=GHOST))
            w.bind("<Leave>", lambda e, r=row: r.configure(fg_color="transparent"))
            w.bind("<Button-1>", lambda e, i=it: self._pick_suggestion(i))

    def _pick_suggestion(self, it: dict) -> None:
        self._hide_suggestions()
        self.search_entry.delete(0, "end")
        self.search_entry.insert(0, it["label"])
        self._goto(it["lat"], it["lon"])

    def _hide_suggestions(self) -> None:
        self.search_box.place_forget()
        self._suggest_items = []

    # ---- current-location pin -------------------------------------------

    def _make_marker_icons(self) -> None:
        """Custom map markers (PIL → PhotoImage). Kept as attrs so Tk doesn't GC them.
        The live 'You' marker is a cycle of pulse frames animated by _pulse_tick."""
        self.icon_pin = ImageTk.PhotoImage(_draw_pin((255, 92, 96, 255)))
        self.icon_wp = ImageTk.PhotoImage(_draw_dot((96, 165, 255, 255), 16, glow=False))
        self._pulse_frames = [ImageTk.PhotoImage(_draw_pulse_frame(i / 18.0)) for i in range(18)]
        self._pulse_i = 0

    def _mark_location(self, lat: float, lon: float) -> None:
        if self.loc_marker is None:
            self.loc_marker = self.map.set_marker(lat, lon, icon=self.icon_pin, icon_anchor="s")
        else:
            self.loc_marker.set_position(lat, lon)

    def _clear_location_marker(self) -> None:
        if self.loc_marker:
            self.loc_marker.delete()
            self.loc_marker = None
        self.pending = None
        self._update_set_btn()

    def _clear_live(self) -> None:
        """Hide the live 'You' dot + readout — the phone's position is only known after a set."""
        if self.live_marker is not None:
            self.live_marker.delete()
            self.live_marker = None
        self._live_pos = None
        self.coord_readout.place_forget()

    def _set_live(self, lat: float, lon: float) -> None:
        """Move the live (current) location marker — where the iPhone is now."""
        self._live_pos = (lat, lon)
        self._set_readout(lat, lon)
        if self.live_marker is None:
            self.live_marker = self.map.set_marker(
                lat, lon, text="You", icon=self._pulse_frames[self._pulse_i],
                icon_anchor="center", text_color=LIVE_HI)
        else:
            self.live_marker.set_position(lat, lon)

    def _pulse_tick(self) -> None:
        """Animate the live 'You' marker by cycling its pre-rendered pulse frames."""
        if (self.live_marker is not None and self.settings.get("pulse", True)
                and getattr(self, "_pulse_frames", None)):
            self._pulse_i = (self._pulse_i + 1) % len(self._pulse_frames)
            try:
                self.live_marker.change_icon(self._pulse_frames[self._pulse_i])
            except Exception:
                pass
        self.after(55, self._pulse_tick)

    def _on_committed(self, lat: float, lon: float) -> None:
        """A set succeeded → the staged point is now the live location."""
        self._set_live(lat, lon)
        self._clear_location_marker()  # drop the red staging pin; live marker stands in
        self._add_recent(lat, lon)
        self._guide_set = True
        self._update_guide()

    def _on_restored(self) -> None:
        """Spoof cleared → the phone is back on its real GPS, which we can't read — hide the dot."""
        self._clear_location_marker()
        self._clear_live()

    # ---- route -----------------------------------------------------------

    def _add_point(self, lat: float, lon: float) -> None:
        self.points.append((lat, lon))
        self.markers.append(self.map.set_marker(
            lat, lon, text=str(len(self.points)), icon=self.icon_wp, icon_anchor="center", text_color=TEXT))
        if len(self.points) >= 2:
            if self.path:
                self.path.delete()
            self.path = self.map.set_path(self.points, color=BLUE, width=5)
        self._set_hint(f"{len(self.points)} waypoint(s). Press Start to walk the route.")

    def on_start(self) -> None:
        if not self._need_device():
            return
        if len(self.points) < 2:
            self._set_hint("Drop at least two waypoints first.")
            return
        if self.player and self.player.is_alive():
            return
        self.stop.clear()
        self.player = threading.Thread(target=self._play_worker, daemon=True)
        self.player.start()

    def _play_worker(self) -> None:
        def on_step(i, total, lat, lon):
            self._set_hint(f"Walking…  {i}/{total}   ({lat:.5f}, {lon:.5f})")
            self._post(lambda la=lat, lo=lon: self._set_live(la, lo))
        try:
            self.device.play_route(self.points, self.speed, self.stop, on_step=on_step)
            self._set_hint("Route stopped." if self.stop.is_set() else "Route complete.")
        except Exception as e:
            self._set_hint(f"Route failed: {e}")

    def on_stop(self) -> None:
        self.stop.set()

    def on_clear_route(self) -> None:
        self.stop.set()
        for m in self.markers:
            m.delete()
        self.markers.clear()
        if self.path:
            self.path.delete()
            self.path = None
        self.points.clear()
        self._set_hint("Waypoints cleared.")

    # ---- shutdown --------------------------------------------------------

    def _on_close(self) -> None:
        self.stop.set()
        self._walking = False
        self._wizard_polling = False
        self._portable_poll = False
        self.portable.stop()
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
        core.cleanup()
        self.destroy()


if __name__ == "__main__":
    _args = sys.argv[1:]
    if _args and _args[0] == "--tunneld":
        # helper mode (launched as root via osascript): build the Wi-Fi tunnel
        from pymobiledevice3.__main__ import main as _pmd_main
        sys.argv = ["pymobiledevice3", "remote", "tunneld"]
        _pmd_main()
    elif _args and _args[0] == "--server":
        # helper mode: run the phone-control web server on the given port
        sys.argv = ["server", _args[1] if len(_args) > 1 else "8765"]
        import server
        server.main()
    else:
        App().mainloop()
