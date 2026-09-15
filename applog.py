"""Spoofr's diagnostic log: ~/.spoofr/spoofr.log.

Deliberately free of heavy imports (no pymobiledevice3, no Qt) so the app can
log from anywhere — including before core.py is loaded — without paying for it.

Spoofr normally runs as a .app bundle, where stderr goes nowhere anyone will
ever look. install_hooks() is what makes a dying worker thread visible at all.
"""

from __future__ import annotations

import datetime
import sys
import threading
import traceback
from pathlib import Path

PATH = Path.home() / ".spoofr" / "spoofr.log"
MAX_BYTES = 1_000_000


def _write(text: str) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            if PATH.stat().st_size > MAX_BYTES:
                PATH.replace(PATH.with_name(PATH.name + ".1"))
        except OSError:
            pass
        with PATH.open("a") as f:
            f.write(text)
    except Exception:
        pass          # logging must never be the thing that breaks the app


def _stamp() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def log(msg: str, exc: bool = False) -> None:
    """Append one line, optionally with the traceback of the exception being handled."""
    text = f"{_stamp()}  {msg}\n"
    if exc:
        text += traceback.format_exc()
    _write(text)


def log_exc(msg: str, exc_info) -> None:
    """Append a line plus an explicit (type, value, traceback) triple."""
    _write(f"{_stamp()}  {msg}\n" + "".join(traceback.format_exception(*exc_info)))


def install_hooks() -> None:
    """Route unhandled exceptions — main thread and worker threads — to the log."""
    previous = sys.excepthook

    def main_hook(exc_type, exc, tb):
        log_exc("unhandled exception", (exc_type, exc, tb))
        previous(exc_type, exc, tb)

    def thread_hook(args):
        name = getattr(args.thread, "name", "?")
        log_exc(f"unhandled exception in thread {name}",
                (args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = main_hook
    threading.excepthook = thread_hook
