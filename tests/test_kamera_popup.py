"""Kameraer skal kunne vælges fra eller til for popup."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database
from app.reolink import CameraMonitor, ReolinkCamera
from app.schemas import CameraCreate, CameraUpdate


def lav_kamera(database: Database, navn: str = "Entré", **kwargs) -> dict:
    return database.create_camera(
        navn,
        "http://10.0.0.5/",
        "admin",
        "pw",
        0,
        True,
        True,
        True,
        "rtsp://admin:pw@10.0.0.5:554/h264Preview_01_main",
        **kwargs,
    )


def test_popup_er_slaet_til_ved_default(tmp_path: Path) -> None:
    database = Database(tmp_path / "kamera.db")
    assert lav_kamera(database)["popup_enabled"] is True


def test_popup_kan_slaas_fra(tmp_path: Path) -> None:
    database = Database(tmp_path / "kamera.db")
    kamera = database.create_camera(
        "Have", "http://10.0.0.5/", "admin", "pw", 0, True, True, True, "", False
    )
    assert database.get_camera(kamera["id"])["popup_enabled"] is False


def test_opdatering_skifter_popup(tmp_path: Path) -> None:
    database = Database(tmp_path / "kamera.db")
    kamera = lav_kamera(database)
    database.update_camera(kamera["id"], {"popup_enabled": 0})
    assert database.get_camera(kamera["id"])["popup_enabled"] is False
    database.update_camera(kamera["id"], {"popup_enabled": 1})
    assert database.get_camera(kamera["id"])["popup_enabled"] is True


def test_gamle_databaser_far_spalten(tmp_path: Path) -> None:
    # En database fra før denne spalte blev til. Den skal opgradere
    # uden at miste noget, og gamle kameraer skal fortsat give popup,
    # ellers ville en opgradering slå alle alarmer fra uden varsel.
    sti = tmp_path / "gammel.db"
    database = Database(sti)
    with database.connection() as connection:
        connection.execute("DROP TABLE cameras")
        connection.execute(
            "CREATE TABLE cameras (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
            "host TEXT NOT NULL, username TEXT NOT NULL DEFAULT '', "
            "password TEXT NOT NULL DEFAULT '', channel INTEGER NOT NULL DEFAULT 0, "
            "person_enabled INTEGER NOT NULL DEFAULT 1, vehicle_enabled INTEGER NOT NULL DEFAULT 1, "
            "snapshots_enabled INTEGER NOT NULL DEFAULT 1, live_stream_url TEXT NOT NULL DEFAULT '', "
            "sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO cameras(name, host, created_at, updated_at) VALUES ('Entré', 'http://x/', 'a', 'a')"
        )
    opgraderet = Database(sti)
    kamera = opgraderet.get_camera(1)
    assert kamera is not None
    assert kamera["popup_enabled"] is True, "et gammelt kamera skal give popup efter opgradering"


def test_schema_afviser_ukendte_felter() -> None:
    # Frontenden sendte tidligere has_password med, hvilket gav
    # "[object Object]" i stedet for en brugbar fejl.
    with pytest.raises(Exception):
        CameraUpdate(name="Entré", popup_enabled=True, has_password=False)
    # popup_enabled skal derimod være gyldigt.
    assert CameraUpdate(popup_enabled=False).popup_enabled is False
    assert CameraCreate(
        name="Entré", host="http://10.0.0.5/", popup_enabled=False
    ).popup_enabled is False


def test_aktivitet_registrerer_popup_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Serveren skal sende flaget med, så forsiden kan skelne mellem
    # "aktivt i loggen" og "skal give popup".
    import asyncio

    database = Database(tmp_path / "kamera.db")
    kamera = database.create_camera(
        "Have", "http://10.0.0.5/", "admin", "pw", 0, True, True, True, "", False
    )
    monitor = CameraMonitor(database)

    async def aktiv(_self, _client):
        return [{"type": "people", "label": "Person"}]

    monkeypatch.setattr(ReolinkCamera, "get_ai_state", aktiv)
    asyncio.run(monitor.poll())
    aktiv = monitor.activity()["active"]
    assert len(aktiv) == 1
    assert aktiv[0]["popup_enabled"] is False, "et fra-slået kamera skal stadig logge, men melde fra til popup"


def test_overvaagning_faar_kameraets_adgangskode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    database = Database(tmp_path / "kamera.db")
    lav_kamera(database)
    monitor = CameraMonitor(database)
    fanget: dict = {}

    async def laes_adgangskode(self, _client):
        fanget["password"] = self.password
        return []

    monkeypatch.setattr(ReolinkCamera, "get_ai_state", laes_adgangskode)
    asyncio.run(monitor.poll())

    assert fanget.get("password") == "pw", (
        "pollingen skal bruge den gemte adgangskode – ellers afviser Reolink login, "
        "bliver aktivitetsloggen tom, og popup'en dukker aldrig op"
    )
    assert database.list_cameras()[0]["password"] == "", "API-listen skal fortsat være hemmelighedsfri"
