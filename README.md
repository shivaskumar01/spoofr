# Spoofr

Set your iPhone's GPS to any point on a map, and every iOS app sees the fake
location. No jailbreak. You drive it from your Mac, from your phone's browser, or
from a headless always-on host.

It works on iOS 17 and later, including iOS 27. Nothing is pinned to a version: it
rides Apple's personalized developer disk image, which pymobiledevice3 fetches for
whatever the phone is running.

## API keys

None. Everything runs locally against your own iPhone over USB or Wi-Fi. The pairing
token (`SPOOFER_TOKEN`) is generated for you, so there is nothing to input.

## How it works

Spoofr is a native Mac app (PySide6, in `qtui/`). The map fills the window and
everything else floats on it: the status pill top-left (which phone, and whether
it's on the cable or Wi-Fi), search top-centre, the control panel on the right, and
recenter / zoom / map style (Standard, Satellite, Hybrid) bottom-right. It follows
the system's light or dark appearance, map included.

Apple's location override is a host-to-device developer command, so a host is always
in the loop. There is no jailbreak-free way to run this on the phone alone.

## One-time setup

1. Turn on Developer Mode (Settings, then Privacy & Security, then Developer Mode, then
   On, and the phone reboots). Any no-jailbreak location tool needs it, and you can't
   get around that. The app notices when it's off, walks you through it, and reconnects
   on its own afterward.
2. Plug into your Mac once, tap Trust, and enter your PIN.
3. Build a Python 3.11 venv with micromamba (prebuilt, no admin):

   ```bash
   cd spoofr
   curl -Ls https://micro.mamba.pm/api/micromamba/osx-arm64/latest | tar -xj -C /tmp bin/micromamba
   /tmp/bin/micromamba create -y -p .venv -c conda-forge python=3.11 pip
   .venv/bin/pip install -e ".[dev]"
   ```

That's the whole setup. Older versions of this app also needed the `ipsw` CLI to build
the developer disk image; pymobiledevice3 11 downloads a personalized image instead, so
it is no longer required.

## Going wireless

You only need the cable for the first minute.

1. With the cable in, open ☰ then Settings then ⚡ Go wireless (one-time).
2. Unplug. From then on, discovery, the tunnel, and spoofing all run over Wi-Fi. The
   phone only needs to be on the same network.

The status pill always shows how the phone is linked (`· USB`, `· Wi-Fi`, or
`· USB + Wi-Fi`), and it reads Ready, iPhone on Wi-Fi before you even click Connect. If
the link drops, say you unplug, Wi-Fi naps, or the phone roams, Spoofr reconnects on its
own for up to two minutes and re-asserts your spoofed location. The button reads
Reconnecting while it tries, and you click it to stop.

### Things to try with the phone in hand

- Zero cable, ever: `.venv/bin/python -m pymobiledevice3 remote pair` runs iOS 17
  wireless pairing (a prompt shows up on the phone). If it works on your iOS version,
  even the first cable is unnecessary.
- No Wi-Fi around, like a car or the outdoors: turn on the phone's Personal Hotspot,
  join it from the Mac, and same-subnet discovery should work over it.

## Running it

Double-click **`dist/Spoofr.app`**. That is the real app: a self-contained bundle with
its own Python, so it can ask macOS for a precise CoreLocation fix. No sudo, no Terminal.
It starts the Wi-Fi tunnel itself and asks for your macOS password once, and only if the
tunnel isn't already up.

The `Spoofr.app` at the top of the repo is a one-line launcher for *this checkout* —
handy while developing, but it runs as a plain script, so macOS denies it CoreLocation
and the map centres on your IP (city-level) instead.

Rebuilding the bundle changes its code signature (it is ad-hoc signed, not
Developer-ID), so macOS treats it as a new app and asks for Location permission again.
That's expected, not a bug.

- **Connect.** Plug the phone in. It shows up as a card (name, model, iOS) within a
  second or two; press Connect. A phone that hasn't trusted this Mac says so and
  carries on by itself once you tap Trust, and Developer Mode (one time) is walked
  through step by step. After the first connect the phone is remembered and connects
  on its own (Settings ▸ Connect automatically).
- **Teleport.** Click the map to drop a pin (drag it to fine-tune). Its card shows the
  address, the coordinates (click to copy), how far it is from the phone, and a star to
  keep it. Teleport here (T) moves the phone. Places lists your last 20 destinations and
  starred places; one click goes there again.
- **Routes.** Route here (R) plans a route from wherever the phone is. Click the map to
  add stops, drag to reorder. Follow roads (walking, cycling, driving), a straight line,
  or draw your own. Speed presets are Walk 5, Jog 9, Cycle 18 and Drive 50 km/h, or any
  custom speed, in km/h or mph. More options: stop, loop or ping-pong at the end, laps
  (or forever), a pause at each stop, ±10% speed variation, GPS jitter. The distance and
  time update as you change anything, and Start plays exactly the line on screen. While
  it runs you can pause (Space), change speed without the phone jumping, follow it on the
  map, or stop (stay there, or go back to the real location). Routes can be saved,
  duplicated, and imported or exported as GPX.
- **Unplugging.** A route runs on the clock, not on how many fixes got through. With
  Wi-Fi control on, unplug and it keeps moving the phone over Wi-Fi. Without it, or if
  the phone goes out of reach, the phone holds its last spot, the pill turns amber, and
  when it's back the route either catches up to where it would be now or continues from
  where it stopped (your choice, asked once). The route is saved continuously, so a quit,
  a crash or a Mac restart shows a Resume banner next time, and a phone that restarted
  meanwhile is noticed and offered "Re-apply location" or "Resume route".
- **While a route runs** the Mac stays awake (with a warning on low battery), closing the
  window keeps it going from the menu-bar item (◉ with its progress, plus Pause, Stop
  and Open), and quitting asks first.
- **Stop spoofing** (⌘., or ⌃⌥⌘R from anywhere) puts the real location back. As a last
  resort, restarting the iPhone always does too.
- **iPhone control:** status pill ▸ Control from your iPhone shows a QR code; scan it and
  drive everything from the phone's browser on the same Wi-Fi.

What iOS doesn't allow, so Spoofr doesn't pretend to: the phone's own GPS can't be read
over the developer connection, so "where you really are" is this Mac's location, marked
*approximate*; and the location API takes latitude and longitude only, so heading is
shown on the Mac's map and iOS derives the phone's own from the movement.

For a dev run: `.venv/bin/python -m qtui`. The basemap is `DEFAULT_SOURCE` in
`qtui/tilemap.py` — Esri's street map, recoloured on arrival into the navy canvas in
dark mode and softened in light mode. It needs no API key, which is the
whole point: CARTO's dark basemap now stamps "API KEY REQUIRED" across every tile
while still answering HTTP 200, so nothing in the fetch path can tell it failed.
The phone's map (`web/app.js`) uses the same source, inverted in MapLibre's raster
paint. To change basemaps, set a new `TileSource`; the tile cache is keyed to the
URL, so switching never serves stale tiles from the old provider.

## If Connect fails

The status pill reading `Ready, iPhone on USB` only means macOS can see the phone. The
tunnel is a separate thing, and it is where connecting actually fails.

- **"The tunnel daemon is running, but it never opened a tunnel to this iPhone."** Not a
  trust problem — Spoofr had already read the phone's name and iOS version by the time
  that appears. The dialog quotes the daemon's own last words; `/tmp/spoofr-tunneld.log`
  has the rest.
- iOS 18.2 removed QUIC, and pymobiledevice3 only picks TCP by default on Python 3.13+,
  so Spoofr starts the daemon with `--protocol tcp` explicitly. A daemon left running
  from an older build keeps holding the port while failing every handshake; Connect now
  notices one without that flag and retires it, which costs one extra admin prompt.

## Headless host (always-on, no desktop app)

This turns the Mac into a permanent spoofing box where the phone's browser is the only
UI, and it survives reboots:

```bash
sudo .venv/bin/python host.py install   # launchd daemon: tunnel + server at boot
.venv/bin/python host.py url            # stable URL + QR, scan once and bookmark it
.venv/bin/python host.py status         # daemon / tunnel / phone health
sudo .venv/bin/python host.py uninstall
```

The token is fixed at install time, so the bookmarked URL keeps working. It is
token-gated and LAN-only by design, so don't port-forward it.

## Layout

```
qtui/          # the app: app, mapview (the controller), panel, session (the route
               # engine), tilemap, bridge, welcome, sidebar, markers, and more
core.py        # pymobiledevice3 engine: tunnel, mount, set/clear/route, wireless
portable.py    # iPhone mode: tunnel elevation, runs the phone server, makes the QR
server.py      # stdlib HTTP control server for the phone
web/           # the phone's web UI (MapLibre, served locally)
host.py        # headless always-on host mode (launchd daemon)
spoofr_app.py  # one entry point: the app, and the --tunneld/--server helpers
macui.py       # menu-bar item + panic hotkey (pyobjc, best-effort)
Spoofr.app     # dev launcher for this checkout
dist/          # the packaged app (exact CoreLocation needs the bundle)
tests/, SpoofrQt.spec, pyproject.toml
```

Every device write goes through one path — `qtui/bridge.py`'s `push()` — and every call
into the phone is time-bounded in `core.py`. If a fix doesn't land, the app says so, stops
whatever was moving, and rebuilds the session in the background rather than animating a
map that no longer matches the phone.

## Tests

```bash
.venv/bin/pytest
```

No device required. Covers the route math and coordinate parsing, the Web-Mercator
projection, and — the ones that matter — the failure paths: that a wedged device call is
bounded and frees its lock, that a Restore can overtake a fix already in flight, and that
a dead session stops the walk/route workers and starts exactly one reconnect.

Rebuild and check the bundle with:

```bash
.venv/bin/pyinstaller SpoofrQt.spec --noconfirm
dist/Spoofr.app/Contents/MacOS/Spoofr --selftest      # expect: SELFTEST PASS
```

## Legal

Location spoofing is broadly legal for development, privacy, and games. Using it to
defraud insurers, defeat court-ordered monitoring, or break verified-location terms of
service is illegal in many places. How you use it is on you.

[pymobiledevice3]: https://github.com/doronz88/pymobiledevice3
