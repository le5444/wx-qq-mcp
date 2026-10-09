"""Writable per-user runtime locations, separate from the installed package.

Importing this module never creates directories or inspects chat data.
"""
import os
from pathlib import Path


def data_directory() -> Path:
    configured = os.environ.get("WXQQ_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
    return (local / "wx-qq-mcp").resolve()


RUNTIME = data_directory()
