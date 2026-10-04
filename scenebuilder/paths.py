"""Where the app's bundled files live: next to the code when run from source, inside the
PyInstaller bundle (sys._MEIPASS) when run as the double-click desktop app."""
from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).resolve().parent)
    return Path(__file__).resolve().parent.parent
