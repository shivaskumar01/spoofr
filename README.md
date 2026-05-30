# Decoy

Set your iPhone's GPS to any point on a map and every iOS app sees the fake location — no jailbreak. Drive it from your Mac, or hand control to your phone's browser over Wi-Fi.

Works on **all iOS 17–26.x** — nothing is hardcoded to a version; it uses Apple's universal personalized developer image.

## How it works

Decoy is a Mac app (`gui.py`) with a **This Mac / iPhone** switch:

- **This Mac** — a dark map: click to drop a pin and set your location, or drop waypoints and "walk" a route.
- **iPhone** — shows a QR code; scan it and control everything from your phone's browser (same Wi‑Fi), cable‑free. The Mac stays the host; the phone is the remote.

Apple's location override is a host→device developer command, so a Mac is always in the loop — there's no jailbreak‑free way to run this standalone on the phone.

## One-time setup

1. **Developer Mode** (Settings → Privacy & Security → Developer Mode → On — the phone reboots). Required for *any* no‑jailbreak location tool; can't be bypassed. The app detects when it's off and walks you through it, then auto‑connects.
2. Plug into your Mac once, tap **Trust** and enter your PIN.
3. **Tk 8.6 environment** — `tkintermapview` breaks on the Tcl/Tk 9.0 that uv's Python and current Homebrew ship (blank map + crash). Build with micromamba (prebuilt, no admin):

   ```bash
   cd decoy
   curl -Ls https://micro.mamba.pm/api/micromamba/osx-arm64/latest | tar -xj -C /tmp bin/micromamba
   /tmp/bin/micromamba create -y -p .venv -c conda-forge python=3.11 'tk=8.6.*' pip
   .venv/bin/pip install -e ".[dev]"
   ```

   Verify: `.venv/bin/python -c "import tkinter; print(tkinter.TkVersion)"` prints `8.6`.

4. **`ipsw` CLI** — pymobiledevice3 shells out to it to build the developer disk image (iOS 17+); without it, mounting hangs. Use the cask (prebuilt) and clear quarantine:

   ```bash
   brew install --cask blacktop/tap/ipsw
   xattr -dr com.apple.quarantine /opt/homebrew/Caskroom/ipsw
   ```

5. **Cable‑free (optional, one‑time)** — to use iPhone mode without the cable, open Decoy → **⚡ Enable wireless** (or run `.venv/bin/pymobiledevice3 lockdown wifi-connections --state on`) once while plugged in. After that the phone is reachable over Wi‑Fi; unplug for good.

## Running it

Open **Decoy** (the app icon in Applications, or double‑click `Decoy.app`). No sudo, no Terminal: it starts the Wi‑Fi tunnel itself, asking for your macOS password **once** — and only if the tunnel isn't already up.

- **This Mac:** **Connect** → click the map → **Set location here**. Route mode drops numbered waypoints and walks them at a chosen speed. **Restore GPS** clears the spoof.
- **iPhone:** click the **iPhone** tab → scan the QR with your Camera → control from Safari.

The desktop basemap is set near the top of `gui.py` (`TILE_SERVER`); swap `lyrs=m` for `lyrs=s` for satellite.

## Layout

```
core.py        # pymobiledevice3 engine: tunnel, mount, set/clear/route
gui.py         # the Decoy desktop app (map + This Mac / iPhone switch)
portable.py    # iPhone mode: tunnel elevation, runs the phone server, makes the QR
server.py      # stdlib HTTP control server for the phone
web/           # the phone's web UI (MapLibre)
launcher.py    # superseded standalone QR launcher (kept; the iPhone tab replaces it)
Decoy.app      # double-click bundle → runs gui.py
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
