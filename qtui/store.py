"""Tiny settings store — the same ~/.spoofr/settings.json the Tk app uses, so
saved places, recents, and preferences carry over between the two front-ends.
"""

from __future__ import annotations

import json
from pathlib import Path


def _path() -> Path:
    return Path.home() / ".spoofr" / "settings.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {}


def save(d: dict) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2))
    except Exception:
        pass
