from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    database_path: Path
    static_dir: Path
    host: str
    port: int
    password: str
    auth_required: bool
    secret_key: str
    api_token: str
    timezone: str
    sync_interval: int
    background_sync: bool
    latitude: float
    longitude: float
    location_name: str
    allow_private_calendars: bool
    secure_cookies: bool
    allowed_origins: tuple[str, ...]

    @property
    def session_secret(self) -> bytes:
        return self.secret_key.encode("utf-8")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    data_dir = Path(os.getenv("FAMILY_DASHBOARD_DATA_DIR", str(root / "data"))).expanduser()
    if not data_dir.is_absolute():
        data_dir = (Path.cwd() / data_dir).resolve()
    else:
        data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    database_value = os.getenv("FAMILY_DASHBOARD_DATABASE")
    database_path = (
        Path(database_value).expanduser() if database_value else data_dir / "family_dashboard.db"
    )
    if not database_path.is_absolute():
        database_path = (Path.cwd() / database_path).resolve()
    static_value = os.getenv("FAMILY_DASHBOARD_STATIC_DIR")
    static_dir = Path(static_value).expanduser() if static_value else root / "web"
    if not static_dir.is_absolute():
        static_dir = (Path.cwd() / static_dir).resolve()
    origins = tuple(
        item.strip()
        for item in os.getenv(
            "FAMILY_DASHBOARD_ALLOWED_ORIGINS",
            "http://localhost:8080,http://127.0.0.1:8080,http://localhost:8000,http://127.0.0.1:8000",
        ).split(",")
        if item.strip()
    )
    return Settings(
        root_dir=root,
        data_dir=data_dir,
        database_path=database_path,
        static_dir=static_dir,
        host=os.getenv("FAMILY_DASHBOARD_HOST", "0.0.0.0"),
        port=_as_int(os.getenv("FAMILY_DASHBOARD_PORT"), 8080),
        password=os.getenv("FAMILY_DASHBOARD_PASSWORD", "family"),
        auth_required=_as_bool(os.getenv("FAMILY_DASHBOARD_AUTH_REQUIRED"), True),
        secret_key=os.getenv("FAMILY_DASHBOARD_SECRET_KEY", "change-this-secret-before-production"),
        api_token=os.getenv("FAMILY_DASHBOARD_API_TOKEN", "").strip(),
        timezone=os.getenv("FAMILY_DASHBOARD_TIMEZONE", "Europe/Copenhagen"),
        sync_interval=max(60, _as_int(os.getenv("FAMILY_DASHBOARD_SYNC_INTERVAL"), 900)),
        background_sync=_as_bool(os.getenv("FAMILY_DASHBOARD_BACKGROUND_SYNC"), True),
        latitude=float(os.getenv("FAMILY_DASHBOARD_LATITUDE", "55.6761")),
        longitude=float(os.getenv("FAMILY_DASHBOARD_LONGITUDE", "12.5683")),
        location_name=os.getenv("FAMILY_DASHBOARD_LOCATION_NAME", "København"),
        allow_private_calendars=_as_bool(
            os.getenv("FAMILY_DASHBOARD_ALLOW_PRIVATE_CALENDARS"), False
        ),
        secure_cookies=_as_bool(os.getenv("FAMILY_DASHBOARD_SECURE_COOKIES"), False),
        allowed_origins=origins,
    )
