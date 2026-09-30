import sqlite3

from fastapi.testclient import TestClient

from app import main
from app.db import Database


def test_database_uses_configured_defaults(tmp_path):
    database = Database(
        tmp_path / "configured.db",
        {
            "timezone": "Asia/Tokyo",
            "location_name": "Testby",
            "latitude": "55.1",
            "longitude": "12.2",
        },
    )
    assert database.get_settings()["timezone"] == "Asia/Tokyo"
    assert database.get_settings()["location_name"] == "Testby"
    assert database.get_settings()["latitude"] == "55.1"
    assert database.get_settings()["longitude"] == "12.2"


def test_legacy_calendar_sources_are_migrated(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE calendar_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'ics',
                color TEXT NOT NULL DEFAULT '#5c7cfa',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_synced_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO calendar_sources(name, url, created_at, updated_at)
            VALUES ('Aula Emma', 'https://example.com/aula.ics', 'now', 'now');
            INSERT INTO calendar_sources(name, url, created_at, updated_at)
            VALUES ('Familie', 'https://example.com/family.ics', 'now', 'now');
            INSERT INTO calendar_sources(name, url, created_at, updated_at)
            VALUES ('Paula', 'https://example.com/paula.ics', 'now', 'now');
            """
        )
    sources = Database(path).list_sources()
    assert {source["name"]: source["kind"] for source in sources} == {
        "Aula Emma": "school",
        "Familie": "calendar",
        "Paula": "calendar",
    }


def test_authentication_crud_and_csrf(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    database = Database(tmp_path / "dashboard.db")
    database.update_settings({"weather_enabled": False})
    main.database = database
    main.synchronizer.database = database
    try:
        with TestClient(main.app) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/dashboard/summary").status_code == 401
            assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
            assert client.post("/api/auth/login", json={"password": "family"}).status_code == 200
            csrf = client.cookies.get("fd_csrf")
            assert csrf
            payload = {
                "name": "Familien",
                "birth_date": "2020-02-29",
                "color": "#f59e0b",
                "notes": "",
            }
            assert client.post("/api/birthdays", json=payload).status_code == 403
            created = client.post("/api/birthdays", json=payload, headers={"X-FD-CSRF": csrf})
            assert created.status_code == 201
            birthday_id = created.json()["birthday"]["id"]
            assert (
                client.patch(
                    f"/api/birthdays/{birthday_id}",
                    json={"name": "Familie"},
                    headers={"X-FD-CSRF": csrf},
                ).status_code
                == 200
            )
            assert (
                client.post(
                    "/api/members",
                    json={"name": "Alex", "color": "#5c7cfa"},
                    headers={"X-FD-CSRF": csrf},
                ).status_code
                == 201
            )
            assert (
                client.post(
                    "/api/frames",
                    json={
                        "name": "HA",
                        "url": "https://example.com",
                        "height": 400,
                        "accent": "#5c7cfa",
                        "visible": True,
                    },
                    headers={"X-FD-CSRF": csrf},
                ).status_code
                == 201
            )
            assert (
                client.post(
                    "/api/frames",
                    json={"name": "bad", "url": "file:///etc/passwd"},
                    headers={"X-FD-CSRF": csrf},
                ).status_code
                == 422
            )
            assert (
                client.post(
                    "/api/calendars",
                    json={
                        "name": "Skole",
                        "url": "https://example.com/calendar.ics",
                        "source_type": "ics",
                        "kind": "school",
                        "color": "#5c7cfa",
                        "enabled": True,
                    },
                    headers={"X-FD-CSRF": csrf},
                ).status_code
                == 201
            )
            summary = client.get("/api/dashboard/summary").json()
            assert len(summary["birthdays"]) == 1
            assert len(summary["members"]) == 1
            assert len(summary["frames"]) == 1
            assert len(summary["sources"]) == 1
            assert summary["sources"][0]["kind"] == "school"
            assert (
                client.delete(
                    f"/api/birthdays/{birthday_id}", headers={"X-FD-CSRF": csrf}
                ).status_code
                == 200
            )
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database


def test_runtime_settings_are_applied_to_summary(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    old_weather = main.weather
    database = Database(tmp_path / "settings.db")
    database.update_settings({"weather_enabled": True})
    weather_calls = {}

    class StubWeather:
        async def current(self, **values):
            weather_calls.update(values)
            return {"temperature": 20, "location": values["location_name"]}

    main.database = database
    main.synchronizer.database = database
    main.weather = StubWeather()
    try:
        with TestClient(main.app) as client:
            assert client.post("/api/auth/login", json={"password": "family"}).status_code == 200
            csrf = client.cookies.get("fd_csrf")
            headers = {"X-FD-CSRF": csrf}
            assert (
                client.patch(
                    "/api/settings",
                    json={"timezone": "Not/A-Timezone"},
                    headers=headers,
                ).status_code
                == 422
            )
            assert (
                client.patch(
                    "/api/settings",
                    json={
                        "timezone": "Asia/Tokyo",
                        "latitude": 55.1,
                        "longitude": 12.2,
                        "location_name": "Testby",
                    },
                    headers=headers,
                ).status_code
                == 200
            )
            summary = client.get("/api/dashboard/summary").json()
            assert summary["generated_at"].endswith("+09:00")
            assert weather_calls == {
                "latitude": 55.1,
                "longitude": 12.2,
                "timezone_name": "Asia/Tokyo",
                "location_name": "Testby",
            }
            assert summary["weather"]["location"] == "Testby"
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
        main.weather = old_weather


def test_theme_settings_round_trip_and_validation(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    database = Database(tmp_path / "theme.db")
    main.database = database
    main.synchronizer.database = database
    try:
        # En ny database skal starte med automatisk skift og fornuftige
        # klokkeslæt, ellers er vægskærmen hvid indtil nogen rører ved.
        start = database.get_settings()
        assert start["theme"] == "auto"
        assert start["theme_day_start"] == "07:00"
        assert start["theme_night_start"] == "20:00"
        with TestClient(main.app) as client:
            assert client.post("/api/auth/login", json={"password": "family"}).status_code == 200
            csrf = client.cookies.get("fd_csrf")
            headers = {"X-FD-CSRF": csrf}
            assert (
                client.patch(
                    "/api/settings",
                    json={
                        "theme": "light",
                        "theme_day_start": "08:45",
                        "theme_night_start": "21:00",
                    },
                    headers=headers,
                ).status_code
                == 200
            )
            summary = client.get("/api/dashboard/summary").json()
            assert summary["settings"]["theme"] == "light"
            assert summary["settings"]["theme_day_start"] == "08:45"
            assert summary["settings"]["theme_night_start"] == "21:00"
            # Kun de tre rigtige værdier slipper igennem, ellers ville en
            # skrivefejl gøre skærmen hvid uden at nogen kunne merke det.
            assert (
                client.patch(
                    "/api/settings", json={"theme": "neon"}, headers=headers
                ).status_code
                == 422
            )
            # Og klokkeslættene skal ligne klokkeslæt.
            assert (
                client.patch(
                    "/api/settings",
                    json={"theme_day_start": "halv otte"},
                    headers=headers,
                ).status_code
                == 422
            )
            assert (
                client.patch(
                    "/api/settings",
                    json={"theme_night_start": "25:00"},
                    headers=headers,
                ).status_code
                == 422
            )
            # Efter de afviste forsøg skal det stadig være det gemte.
            assert client.get("/api/dashboard/summary").json()["settings"]["theme"] == "light"
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database


def test_calendar_kinds_are_filtered(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    database = Database(tmp_path / "calendar-kinds.db")
    main.database = database
    main.synchronizer.database = database
    try:
        with TestClient(main.app) as client:
            assert client.post("/api/auth/login", json={"password": "family"}).status_code == 200
            csrf = client.cookies.get("fd_csrf")
            headers = {"X-FD-CSRF": csrf}
            school_response = client.post(
                "/api/calendars",
                json={
                    "name": "Aula",
                    "url": "https://example.com/school.ics",
                    "kind": "school",
                },
                headers=headers,
            )
            calendar_response = client.post(
                "/api/calendars",
                json={
                    "name": "Familie",
                    "url": "https://example.com/family.ics",
                    "kind": "calendar",
                },
                headers=headers,
            )
            assert school_response.status_code == 201
            assert calendar_response.status_code == 201
            school_id = school_response.json()["calendar"]["id"]
            calendar_id = calendar_response.json()["calendar"]["id"]
            database.replace_source_events(
                school_id,
                [
                    {
                        "id": "school-event",
                        "uid": "school-event",
                        "title": "Skoletime",
                        "start_at": "2026-09-25T08:00:00+00:00",
                        "end_at": "2026-09-25T09:00:00+00:00",
                    }
                ],
            )
            database.replace_source_events(
                calendar_id,
                [
                    {
                        "id": "family-event",
                        "uid": "family-event",
                        "title": "FAMILIEAFTALE",
                        "start_at": "2026-09-25T10:00:00+00:00",
                        "end_at": "2026-09-25T11:00:00+00:00",
                    }
                ],
            )
            params = "start=2026-09-25&end=2026-09-25"
            school_events = client.get(f"/api/events?{params}&kind=school").json()["events"]
            calendar_events = client.get(f"/api/events?{params}&kind=calendar").json()["events"]
            all_events = client.get(f"/api/events?{params}").json()["events"]
            assert [event["source_kind"] for event in school_events] == ["school"]
            assert school_events[0]["local_date"] == "2026-09-25"
            assert school_events[0]["local_start_time"] == "10:00"
            assert [event["source_kind"] for event in calendar_events] == ["calendar"]
            assert {event["title"] for event in all_events} == {"Skoletime", "FAMILIEAFTALE"}
            assert client.get(f"/api/events?{params}&kind=invalid").status_code == 422
            assert client.get("/api/events?start=2026-01-01&end=2027-01-03").status_code == 422
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
