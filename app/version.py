from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

#: Semantisk version – holdes synkron med pyproject.toml.
VERSION = "0.4.0"

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web"

#: Filerne i frontenden. De bestemmer build-id'en, fordi de leveres til
#: browseren med ?v=<build-id> som cache-buster.
_ASSET_FILES = ("index.html", "app.js", "styles.css")

_started_at = ""


def _file_build() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def asset_fingerprint() -> str:
    """Kort fingeraftryk af frontenden.

    Uden dette var build-id'en bare VERSION, som ikke ændrer sig ved et
    frontend-build. Så fik browseren samme URL til app.js hver gang og
    genbrugte den gamle fil, selvom serveren kørte ny kode.
    """
    import hashlib

    digest = hashlib.sha256()
    for name in _ASSET_FILES:
        try:
            digest.update((_STATIC_DIR / name).read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:8]


#: Unik build-id. Sættes normalt via docker --build-arg FAMILY_DASHBOARD_BUILD=$(git rev-parse --short HEAD).
#: Ellers bygges den af VERSION plus et fingeraftryk af frontenden, så enhver
#: ændring i index.html, app.js eller styles.css giver en ny værdi. Det er
#: nødvendigt, fordi denne id bruges som ?v= på assets: uden den ville
#: browseren genbruge en gammel app.js efter en opdatering.
BUILD_ID = (os.getenv("FAMILY_DASHBOARD_BUILD") or "").strip()
if not BUILD_ID:
    _base = _file_build() or VERSION
    _fingerprint = asset_fingerprint()
    BUILD_ID = f"{_base}-{_fingerprint}" if _fingerprint else _base


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
