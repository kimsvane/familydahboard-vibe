from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

#: Semantisk version – holdes synkron med pyproject.toml.
VERSION = "0.3.0"

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

_started_at = ""


def _file_build() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


#: Unik build-id. Sættes normalt via docker --build-arg FAMILY_DASHBOARD_BUILD=$(git rev-parse --short HEAD),
#: ellers bruges indholdet af VERSION-filen, så buildet altid kan identificeres.
BUILD_ID = (os.getenv("FAMILY_DASHBOARD_BUILD") or _file_build() or "dev").strip()


def mark_started() -> None:
    global _started_at
    _started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_info() -> dict[str, str]:
    return {
        "version": VERSION,
        "build": BUILD_ID,
        "started_at": _started_at,
    }


def banner() -> str:
    return (
        f"Family Dashboard version {VERSION} (build {BUILD_ID}) "
        f"startet {_started_at or 'ukendt tid'} – Python {os.sys.version.split()[0]}"
    )
