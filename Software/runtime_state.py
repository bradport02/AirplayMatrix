"""
Where the kiosk app leaves state for the web UI to read.

The two processes can't talk directly: shairport-sync's metadata FIFO has a
single reader, and that's the kiosk app. So the web UI can't answer "what's
playing?" by looking at the source -- it has to be told. This module is the
one place the file locations are agreed, so neither side can drift.

Everything here is best-effort and disposable. A missing or stale file means
"the app isn't running or hasn't got that far", which is exactly what the
web UI should show in that case, so nothing needs to guard against absence
beyond handling it as "unknown".

Under ~/.local/state rather than ~/.config on purpose: this is generated
runtime state, not configuration, and nothing here is worth backing up.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

STATE_DIR = Path.home() / ".local" / "state" / "airplaymatrix"

# What the kiosk app is currently showing -- see app_qt5/status_writer.py.
NOW_PLAYING_PATH = STATE_DIR / "now-playing.json"

# The kiosk app's own stdout/stderr, redirected by airplaymatrix-run.sh so
# the Diagnostics page has something to show. Without this the autostart
# launches the app with nowhere for its output to go.
KIOSK_LOG_PATH = STATE_DIR / "kiosk.log"


def ensure_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict) -> None:
    """Write-then-rename, so a reader never sees a half-written file."""
    ensure_dir()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def tail(path: Path, lines: int) -> str:
    """Last `lines` lines of a file, without reading all of it.

    The kiosk log can reach several megabytes over a long session with DEBUG
    on, and the web UI runs on a Pi Zero -- reading the whole thing to show
    the end of it would be a needless spike. Walks backwards in blocks
    instead.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            block = 8192
            data = b""
            while end > 0 and data.count(b"\n") <= lines:
                step = min(block, end)
                end -= step
                handle.seek(end)
                data = handle.read(step) + data
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError:
        return ""
