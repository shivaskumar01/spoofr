# Spoofr

Set your iPhone's GPS to any point on a map, and every iOS app sees the fake
location. No jailbreak. You drive it from your Mac, from your phone's browser, or
from a headless always-on host.

It works on every iOS from 17 through 26.x. Nothing is pinned to a version, because
it rides Apple's universal personalized developer image.

## API keys

None. Everything runs locally against your own iPhone over USB or Wi-Fi. The pairing
token (`SPOOFER_TOKEN`) is generated for you, so there is nothing to input.

## How it works

Spoofr is a native Mac app (PySide6, in `qtui/`) with a This Mac / iPhone switch.

- This Mac: a dark, GPU-smooth map. Click to drop a pin and set your location, walk
  with the joystick or the arrow keys, or drop waypoints and walk a route (optionally
  snapped to real roads).
- iPhone: it shows a QR code. Scan it and control everything from your phone's browser
  over the same Wi-Fi. The Mac stays the host. The phone is the remote.

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

4. Install the `ipsw` CLI. pymobiledevice3 shells out to it to build the developer disk
   image (iOS 17+). Without it, mounting hangs. Use the cask and clear quarantine:

   ```bash
   brew install --cask blacktop/tap/ipsw
   xattr -dr com.apple.quarantine /opt/homebrew/Caskroom/ipsw
   ```

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

- This Mac: Connect, click the map, then Set location here. Route mode drops numbered
  waypoints and walks them at a pace you pick (Walk, Run, Cycle, or Drive, with loop or
  bounce, and GPX import and export). Restore GPS clears the spoof. The panic hotkey
  ⌃⌥⌘R restores real GPS from anywhere.
- iPhone: click the iPhone tab, scan the QR with your Camera, and control it from Safari.

For a dev run: `.venv/bin/python -m qtui`. The basemap is `DEFAULT_SOURCE` in
`qtui/tilemap.py` — Esri's street map, desaturated and inverted on arrival into the
dark canvas the rest of the UI is built around. It needs no API key, which is the
whole point: CARTO's dark basemap now stamps "API KEY REQUIRED" across every tile
while still answering HTTP 200, so nothing in the fetch path can tell it failed.
The phone's map (`web/app.js`) uses the same source, inverted in MapLibre's raster
paint. To change basemaps, set a new `TileSource`; the tile cache is keyed to the
URL, so switching never serves stale tiles from the old provider.

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
qtui/          # the app: app, mapview, tilemap, bridge, sidebar, markers, and more
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
