"""Tiny persistence layer under ~/.spoofr/.

settings.json holds preferences, places, saved routes and remembered phones.
Each phone's route session lives in its own file, sessions/<udid>.json, and is
saved continuously, so quitting, a crash or a Mac restart never loses a route.
Sessions are per device: plugging in another phone never touches this one's.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


def _root() -> Path:
    return Path.home() / ".spoofr"


def _path() -> Path:
    return _root() / "settings.json"


def _atomic_write(p: Path, text: str) -> None:
    """A bare write_text truncates first, so a crash (or a quit) landing in the
    middle of it left an empty file and took everything in it along."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)          # atomic on the same filesystem


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {}


def save(d: dict) -> None:
    try:
        _atomic_write(_path(), json.dumps(d, indent=2))
    except Exception:
        pass


# ---- per-device route sessions ---------------------------------------------

def _sessions() -> Path:
    return _path().parent / "sessions"


def _session_file(udid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", udid or "unknown")
    return _sessions() / f"{safe}.json"


def load_session(udid: str) -> dict | None:
    try:
        return json.loads(_session_file(udid).read_text())
    except Exception:
        return None


def save_session(udid: str, d: dict) -> None:
    try:
        _atomic_write(_session_file(udid), json.dumps(d))
    except Exception:
        pass


def drop_session(udid: str) -> None:
    try:
        _session_file(udid).unlink()
    except OSError:
        pass
