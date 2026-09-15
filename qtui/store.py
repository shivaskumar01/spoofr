"""Tiny settings store, the same ~/.spoofr/settings.json the Tk app uses, so
saved places, recents, and preferences carry over between the two front-ends.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _path() -> Path:
    return Path.home() / ".spoofr" / "settings.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {}


def save(d: dict) -> None:
    """Write the settings atomically.

    A bare write_text truncates first, so a crash (or a quit) landing in the
    middle of it left an empty file and took every saved place with it.
    """
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        os.replace(tmp, p)          # atomic on the same filesystem
    except Exception:
        pass
