"""File-based emergency stop: create a KILL_SWITCH file to halt the bot instantly."""
from __future__ import annotations

import os


def is_active(path: str) -> bool:
    return os.path.exists(path)


def read_reason(path: str) -> str:
    """First line of the kill switch file, if present."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return ""


def activate(path: str, reason: str = "") -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(reason or "manual kill switch\n")
