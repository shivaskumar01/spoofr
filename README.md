# Spoofr

Set your iPhone's GPS to any point on a map, and every iOS app sees the fake
location. No jailbreak. You drive it from your Mac, from your phone's browser, or
from a headless always-on host.

It works on every iOS from 17 through 26.x. Nothing is pinned to a version, because
it rides Apple's universal personalized developer image.

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
   /tmp/bin/micromamba create -y -p .venv -c conda-forge python=3.11 'tk=8.6.*' pip
   .venv/bin/pip install -e ".[dev]"
   ```

   (Tk 8.6 is only there for the legacy Tk UI, `gui.py`. The main app is Qt.)

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

Open Spoofr (the icon in Applications, or double-click `Spoofr.app`). No sudo, no
Terminal. It starts the Wi-Fi tunnel itself and asks for your macOS password once, and
only if the tunnel isn't already up.

- This Mac: Connect, click the map, then Set location here. Route mode drops numbered
  waypoints and walks them at a pace you pick (Walk, Run, Cycle, or Drive, with loop or
  bounce, and GPX import and export). Restore GPS clears the spoof. The panic hotkey
  ⌃⌥⌘R restores real GPS from anywhere.
- iPhone: click the iPhone tab, scan the QR with your Camera, and control it from Safari.

For a dev run: `.venv/bin/python -m qtui`. The basemap URL is `DEFAULT_TILES` in
`qtui/tilemap.py`.

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
qtui/          # the native Qt app: app, mapview, tilemap, bridge, sidebar, and more
core.py        # pymobiledevice3 engine: tunnel, mount, set/clear/route, wireless
portable.py    # iPhone mode: tunnel elevation, runs the phone server, makes the QR
server.py      # stdlib HTTP control server for the phone
web/           # the phone's web UI (MapLibre)
host.py        # headless always-on host mode (launchd daemon)
spoofr_app.py  # entry point for the packaged .app (PyInstaller)
gui.py         # the legacy Tk app (kept; also hosts the --tunneld/--server helpers)
launcher.py    # superseded standalone QR launcher
Spoofr.app     # double-click bundle that runs the Qt app from this checkout
dist/          # packaged, signed Spoofr.app (exact CoreLocation needs the bundle)
tests/, pyproject.toml
```

## Tests

```bash
.venv/bin/pytest
```

Covers the route math (distance and interpolation). No device required.

## Legal

Location spoofing is broadly legal for development, privacy, and games. Using it to
defraud insurers, defeat court-ordered monitoring, or break verified-location terms of
service is illegal in many places. How you use it is on you.

[pymobiledevice3]: https://github.com/doronz88/pymobiledevice3
