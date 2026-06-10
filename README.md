# Spoofr

Set your iPhone's GPS to any point on a map and every iOS app sees the fake location — no jailbreak. Drive it from your Mac, from your phone's browser, or from a headless always-on host.

Works on **all iOS 17–26.x** — nothing is hardcoded to a version; it uses Apple's universal personalized developer image.

## How it works

Spoofr is a native Mac app (PySide6, `qtui/`) with a **This Mac / iPhone** switch:

- **This Mac** — a dark, GPU-smooth map: click to drop a pin and set your location, walk with the joystick/arrow keys, or drop waypoints and "walk" a route (optionally snapped to real roads).
- **iPhone** — shows a QR code; scan it and control everything from your phone's browser (same Wi‑Fi). The Mac stays the host; the phone is the remote.

Apple's location override is a host→device developer command, so a host is always in the loop — there's no jailbreak‑free way to run this standalone on the phone.

## One-time setup

1. **Developer Mode** (Settings → Privacy & Security → Developer Mode → On — the phone reboots). Required for *any* no‑jailbreak location tool; can't be bypassed. The app detects when it's off and walks you through it, then auto‑connects.
2. Plug into your Mac once, tap **Trust** and enter your PIN.
3. **Python 3.11 venv** — build with micromamba (prebuilt, no admin):

   ```bash
   cd spoofr
   curl -Ls https://micro.mamba.pm/api/micromamba/osx-arm64/latest | tar -xj -C /tmp bin/micromamba
   /tmp/bin/micromamba create -y -p .venv -c conda-forge python=3.11 'tk=8.6.*' pip
   .venv/bin/pip install -e ".[dev]"
   ```

   (Tk 8.6 is only needed by the legacy Tk UI, `gui.py`; the main app is Qt.)

4. **`ipsw` CLI** — pymobiledevice3 shells out to it to build the developer disk image (iOS 17+); without it, mounting hangs. Use the cask (prebuilt) and clear quarantine:

   ```bash
   brew install --cask blacktop/tap/ipsw
   xattr -dr com.apple.quarantine /opt/homebrew/Caskroom/ipsw
   ```

## Going wireless

The cable is only needed for the first minute of the app's life:

1. With the cable in, open ☰ → **Settings** → **⚡ Go wireless (one-time)**.
2. Unplug. Done — discovery, the tunnel, and spoofing all run over Wi‑Fi from then on (the phone just has to be on the same network).

The status pill always shows how the phone is linked (`· USB`, `· Wi‑Fi`, or `· USB + Wi‑Fi`), and shows **Ready · iPhone on Wi‑Fi** before you even click Connect. If the connection drops — unplug, Wi‑Fi nap, roaming — Spoofr auto‑reconnects for up to two minutes and re‑asserts your spoofed location; the button reads **Reconnecting…** (click it to stop trying).

### Experiments to try with the phone in hand

- **Zero cable, ever:** `.venv/bin/python -m pymobiledevice3 remote pair` — iOS 17 wireless pairing (a prompt appears on the phone). If it works on your iOS version, even the first-time cable is unnecessary.
- **No Wi‑Fi around (car, outdoors):** turn on the phone's Personal Hotspot, join it from the Mac — same-subnet discovery should work over it.

## Running it

Open **Spoofr** (the app icon in Applications, or double‑click `Spoofr.app`). No sudo, no Terminal: it starts the Wi‑Fi tunnel itself, asking for your macOS password **once** — and only if the tunnel isn't already up.

- **This Mac:** **Connect** → click the map → **Set location here**. Route mode drops numbered waypoints and walks them at a chosen pace (Walk/Run/Cycle/Drive, loop/bounce, GPX import/export). **Restore GPS** clears the spoof. Panic hotkey: ⌃⌥⌘R restores real GPS from anywhere.
- **iPhone:** click the **iPhone** tab → scan the QR with your Camera → control from Safari.

Dev run: `.venv/bin/python -m qtui`. The basemap URL is `DEFAULT_TILES` in `qtui/tilemap.py`.

## Headless host (always-on, no desktop app)

Make the Mac a permanent spoofing box — the phone's browser is the only UI, across reboots:

```bash
sudo .venv/bin/python host.py install   # launchd daemon: tunnel + server at boot
.venv/bin/python host.py url            # stable URL + QR — scan once, bookmark it
.venv/bin/python host.py status         # daemon / tunnel / phone health
sudo .venv/bin/python host.py uninstall
```

The token is fixed at install time, so the bookmarked URL keeps working. Token-gated, LAN-only by design — don't port-forward it.

## Layout

```
qtui/          # the native Qt app: app, mapview, tilemap, bridge, sidebar, …
core.py        # pymobiledevice3 engine: tunnel, mount, set/clear/route, wireless
portable.py    # iPhone mode: tunnel elevation, runs the phone server, makes the QR
server.py      # stdlib HTTP control server for the phone
web/           # the phone's web UI (MapLibre)
host.py        # headless always-on host mode (launchd daemon)
spoofr_app.py  # entry point for the packaged .app (PyInstaller)
gui.py         # the legacy Tk app (kept; also hosts the --tunneld/--server helpers)
launcher.py    # superseded standalone QR launcher
Spoofr.app     # double-click bundle → runs the Qt app from this checkout
dist/          # packaged, signed Spoofr.app (exact CoreLocation needs the bundle)
tests/ · pyproject.toml
```

## Tests

```bash
.venv/bin/pytest
```

Covers the route math (distance + interpolation); no device required.

## Legal

Location spoofing is broadly legal for development, privacy, and games. Using it to defraud insurers, defeat court‑ordered monitoring, or violate verified‑location terms of service can be illegal. How you use it is on you.

[pymobiledevice3]: https://github.com/doronz88/pymobiledevice3
