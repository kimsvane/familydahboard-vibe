import asyncio
import email
import json
import logging
import os
import pathlib
import tempfile
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Database
from app.icloud_notes import ICloudNotes, _parse_list_mailboxes, extract_note, html_to_text
from app.icloud_reminders import (
    CalDAVError,
    CalDAVRemindersClient,
    RemindersSync,
    _find_calendar_data,
    _find_property_href,
    _parse_calendar_home,
    build_todo,
    parse_todo,
)
from app.reolink import (
    CameraMonitor,
    ReolinkCamera,
    ffmpeg_mjpeg_args,
    is_streaming_url,
    normalize_host,
    stream_camera_mjpeg,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_build_and_parse_todo_roundtrip():
    uid = "TEST-UID-1"
    ics = build_todo("Køb ind", uid, done=False, due=date(2026, 10, 1))
    parsed = parse_todo(ics)
    assert parsed["uid"] == uid
    assert parsed["summary"] == "Køb ind"
    assert parsed["done"] is False
    assert parsed["due"] == "2026-10-01"

    done_ics = build_todo("Hent pakke", uid, done=True)
    done_parsed = parse_todo(done_ics)
    assert done_parsed["done"] is True
    assert done_parsed["due"] is None


def test_parse_todo_rejects_non_todo():
    with pytest.raises(CalDAVError):
        parse_todo(b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:X\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    with pytest.raises(CalDAVError):
        parse_todo(b"not ical at all")


def test_parse_calendar_home_filters_vtodo():
    xml = b"""\
<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/calendar-homes/kim/781.zip/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Family</d:displayname>
        <d:resourcetype><c:calendar/></d:resourcetype>
        <c:supported-calendar-component-set>
          <c:comp name="VTODO"/>
        </c:supported-calendar-component-set>
      </d:prop>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/calendar-homes/kim/782.zip/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Kalender</d:displayname>
        <d:resourcetype><c:calendar/></d:resourcetype>
        <c:supported-calendar-component-set>
          <c:comp name="VEVENT"/>
        </c:supported-calendar-component-set>
      </d:prop>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/calendar-homes/kim/783.zip/</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype><c:calendar/></d:resourcetype>
        <c:supported-calendar-component-set>
          <c:comp name="VTODO"/>
        </c:supported-calendar-component-set>
      </d:prop>
    </d:propstat>
  </d:response>
</d:multistatus>
"""
    lists = _parse_calendar_home(xml)
    assert [item["name"] for item in lists] == ["Family", "783.zip"]


def test_find_calendar_data():
    uid = "TODO-ABC"
    todo_ics = build_todo("Task", uid).decode("utf-8")
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:response><d:href>/cal/ABC.ics</d:href><d:propstat><d:prop>"
        f"<c:calendar-data>{todo_ics}</c:calendar-data>"
        "</d:prop></d:propstat></d:response>"
        "<d:response><d:href>/cal/EMPTY.ics</d:href><d:propstat><d:prop>"
        "<c:calendar-data/></d:prop></d:propstat></d:response>"
        "</d:multistatus>"
    ).encode("utf-8")
    results = _find_calendar_data(xml)
    assert len(results) == 1
    href, payload = results[0]
    assert href.endswith("/cal/ABC.ics")
    assert parse_todo(payload)["uid"] == uid


def test_reminders_enabled_gates_on_configuration(tmp_path):
    database = Database(tmp_path / "reminders.db")
    sync = RemindersSync(database)
    assert sync.configured() is False
    assert sync.enabled() is False
    database.update_settings(
        {
            "reminders_enabled": "true",
            "reminders_username": "kim@icloud.com",
            "reminders_app_password": "app-pw",
            "reminders_list_href": "/k/",
        }
    )
    assert sync.configured() is True
    assert sync.enabled() is True
    database.update_settings({"reminders_enabled": "false"})
    assert sync.enabled() is False


def test_reminders_sync_upserts_and_removes(tmp_path):
    database = Database(tmp_path / "reminders-sync.db")
    database.update_settings(
        {
            "reminders_enabled": "true",
            "reminders_username": "kim@icloud.com",
            "reminders_app_password": "app-pw",
            "reminders_list_href": "/k/",
        }
    )
    sync = RemindersSync(database)
    tasks = [
        {
            "external_id": "A",
            "href": "https://caldav.icloud.com/k/A.ics",
            "summary": "Aftale 1",
            "done": False,
            "due": "2026-10-01",
        },
        {
            "external_id": "B",
            "href": "https://caldav.icloud.com/k/B.ics",
            "summary": "Aftale 2",
            "done": True,
            "due": None,
        },
    ]
    with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
        CalDAVRemindersClient, "list_tasks", new=AsyncMock(return_value=tasks)
    ):
        result = asyncio.run(sync.sync())
    assert result["synced"] is True
    assert result["count"] == 2
    items = database.list_checklist()
    assert {item["source"] for item in items} == {"icloud"}
    assert {item["external_id"] for item in items} == {"A", "B"}
    by_id = {item["external_id"]: item for item in items}
    assert by_id["B"]["done"] is True

    with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
        CalDAVRemindersClient, "list_tasks", new=AsyncMock(return_value=[tasks[0]])
    ):
        asyncio.run(sync.sync())
    remaining = [item["external_id"] for item in database.list_checklist()]
    assert remaining == ["A"]


def test_reminders_sync_handles_empty_remote_list(tmp_path):
    database = Database(tmp_path / "reminders-empty.db")
    database.update_settings(
        {
            "reminders_enabled": "true",
            "reminders_username": "kim@icloud.com",
            "reminders_app_password": "app-pw",
            "reminders_list_href": "/k/",
        }
    )
    database.update_settings({"reminders_enabled": "true"})
    sync = RemindersSync(database)
    database.create_checklist_item("Lokal", None)
    database.upsert_icloud_checklist_item(
        external_id="OLD", text="Væk fra iCloud", done=False, due_date=None, sort_order=0
    )
    with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
        CalDAVRemindersClient, "list_tasks", new=AsyncMock(return_value=[])
    ):
        result = asyncio.run(sync.sync())
    assert result["synced"] is True
    remaining = database.list_checklist()
    assert len(remaining) == 1
    assert remaining[0]["source"] == "local"


def test_reminders_create_update_delete(tmp_path):
    database = Database(tmp_path / "reminders-cud.db")
    database.update_settings(
        {
            "reminders_enabled": "true",
            "reminders_username": "kim@icloud.com",
            "reminders_app_password": "app-pw",
            "reminders_list_href": "/k/",
        }
    )
    sync = RemindersSync(database)
    with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
        CalDAVRemindersClient, "create_todo", new=AsyncMock(return_value="NEW-UID")
    ):
        item = asyncio.run(sync.create("Ny opgave", date(2026, 12, 24)))
    assert item["source"] == "icloud"
    assert item["text"] == "Ny opgave"
    assert item["external_id"] == "NEW-UID"

    with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
        CalDAVRemindersClient, "update_todo", new=AsyncMock()
    ):
        updated = asyncio.run(
            sync.update(item, {"text": "Opdateret", "done": True, "due_date": None})
        )
    assert updated["done"] is True
    assert updated["text"] == "Opdateret"

    with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
        CalDAVRemindersClient, "delete_todo", new=AsyncMock()
    ):
        asyncio.run(sync.delete(updated))
    assert database.list_checklist() == []


def test_camera_host_normalization():
    assert normalize_host("192.168.1.10") == "http://192.168.1.10"
    assert normalize_host(" https://cam.example:8443/ ") == "https://cam.example:8443"
    with pytest.raises(ValueError):
        normalize_host("ftp://x")
    with pytest.raises(ValueError):
        normalize_host("http://user:pass@host/")


def test_camera_get_ai_state_maps_and_filters():
    camera = ReolinkCamera(
        {
            "id": 1,
            "host": "10.0.0.5",
            "username": "admin",
            "password": "pw",
            "channel": 0,
            "person_enabled": True,
            "vehicle_enabled": False,
        }
    )
    body = [
        {
            "value": {
                "data": {
                    "aiState": [
                        {
                            "aiState": [
                                {"type": "people", "state": 1},
                                {"type": "vehicle", "state": 1},
                                {"type": "face", "state": 0},
                            ]
                        }
                    ]
                }
            }
        }
    ]

    async def fake_post(_self, _client, command, payload, token):
        return body

    with patch.object(ReolinkCamera, "_post", new=fake_post), patch.object(
        ReolinkCamera, "_ensure_token", new=AsyncMock(return_value="tok")
    ):
        async def run():
            return await camera.get_ai_state(None)

        active = asyncio.run(run())
    assert [item["type"] for item in active] == ["people"]
    assert active[0]["label"] == "Person"


def test_camera_snapshot_decodes_base64():
    camera = ReolinkCamera(
        {
            "id": 1,
            "host": "10.0.0.5",
            "username": "admin",
            "password": "pw",
            "channel": 0,
        }
    )

    async def fake_post(_self, _client, command, payload, token):
        if command == "Snap":
            return [{"value": {"snap": "ZnJrLWpwZWc="}}]
        raise AssertionError(f"unexpected command {command}")

    with patch.object(ReolinkCamera, "_post", new=fake_post), patch.object(
        ReolinkCamera, "_ensure_token", new=AsyncMock(return_value="tok")
    ):
        async def run():
            return await camera.snapshot(None)

        assert asyncio.run(run()) == b"frk-jpeg"


def test_camera_monitor_poll_detection_transitions(tmp_path):
    database = Database(tmp_path / "camera-poll.db")
    camera = database.create_camera(
        "Entré",
        "http://10.0.0.5/",
        "admin",
        "pw",
        0,
        True,
        True,
        False,
        "",
    )
    monitor = CameraMonitor(database)

    async def active_state(_self, _client):
        return [{"type": "people", "label": "Person"}]

    with patch.object(ReolinkCamera, "get_ai_state", new=active_state):
        asyncio.run(monitor.poll())
    assert monitor._active[camera["id"]]["types"][0]["type"] == "people"
    activity = database.recent_camera_activity()
    assert len(activity) == 1
    assert activity[0]["detection_type"] == "people"
    assert activity[0]["ended_at"] is None

    async def empty_state(_self, _client):
        return []

    with patch.object(ReolinkCamera, "get_ai_state", new=empty_state):
        asyncio.run(monitor.poll())
    assert not monitor._active
    closed = database.recent_camera_activity()[0]
    assert closed["ended_at"] is not None


def test_camera_monitor_activity_shape(tmp_path):
    database = Database(tmp_path / "camera-activity.db")
    monitor = CameraMonitor(database)
    activity = monitor.activity()
    assert activity["active"] == []
    assert activity["recent"] == []
    assert activity["close_delay"] == 0
    assert activity["poll_seconds"] == 5.0


def test_html_to_text_and_extract_note():
    assert html_to_text("<p>Hej</p><ul><li>A</li><li>B</li></ul>") == "Hej\n- A\n- B"
    message = email.message.EmailMessage()
    message["Subject"] = "Indkøb"
    message.set_content("Hej verden")
    message.add_alternative("<p>Hej <b>verden</b></p>", subtype="html")
    html, plain = extract_note(message)
    assert "verden" in html
    assert plain.strip() == "Hej verden"


def test_notes_parse_mailboxes():
    assert _parse_list_mailboxes('(\\HasNoChildren) "/" "Notes"') == ["Notes"]
    assert _parse_list_mailboxes('(\\HasNoChildren) "/" INBOX') == ["INBOX"]


def test_notes_enabled_gates(tmp_path):
    database = Database(tmp_path / "notes.db")
    notes = ICloudNotes(database)
    assert notes.configured() is False
    assert notes.enabled() is False
    database.update_settings(
        {
            "notes_imap_enabled": "true",
            "notes_imap_username": "kim@icloud.com",
            "notes_imap_app_password": "app-pw",
            "notes_imap_note_title": "Indkøb",
        }
    )
    assert notes.configured() is True
    assert notes.enabled() is True


def test_notes_fetch_save_disabled_are_safe(tmp_path):
    database = Database(tmp_path / "notes-disabled.db")
    notes = ICloudNotes(database)
    result = asyncio.run(notes.fetch())
    assert result == {
        "enabled": False,
        "title": "",
        "content": "",
        "error": "disabled",
        "configured": False,
        "last_error": "",
    }
    saved = asyncio.run(notes.save("Hej"))
    assert saved == {"saved": False, "error": "disabled"}


def _auth_client(client: TestClient):
    assert client.post("/api/auth/login", json={"password": "family"}).status_code == 200
    csrf = client.cookies.get("fd_csrf")
    assert csrf
    return {"X-FD-CSRF": csrf}


def test_camera_crud_api_and_password_redaction(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    old_camera_monitor = main.camera_monitor
    old_reminders = main.reminders
    database = Database(tmp_path / "camera-api.db")
    main.database = database
    main.synchronizer.database = database
    main.camera_monitor = CameraMonitor(database)
    main.reminders = RemindersSync(database)
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            created = client.post(
                "/api/cameras",
                json={
                    "name": "Garage",
                    "host": "10.0.0.5",
                    "username": "admin",
                    "password": "hemmeligt",
                    "channel": 0,
                },
                headers=headers,
            )
            assert created.status_code == 201
            camera = created.json()["camera"]
            assert camera["password"] == ""
            assert camera["host"] == "http://10.0.0.5"

            assert (
                client.post(
                    "/api/cameras",
                    json={"name": "Bad", "host": "ftp://x"},
                    headers=headers,
                ).status_code
                == 422
            )

            updated = client.patch(
                f"/api/cameras/{camera['id']}",
                json={"name": "Garage syd", "password": ""},
                headers=headers,
            )
            assert updated.status_code == 200
            assert updated.json()["camera"]["name"] == "Garage syd"

            listed = client.get("/api/cameras").json()["cameras"]
            assert len(listed) == 1
            assert listed[0]["password"] == ""

            assert (
                client.delete(f"/api/cameras/{camera['id']}", headers=headers).json()["deleted"]
                is True
            )
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
        main.camera_monitor = old_camera_monitor
        main.reminders = old_reminders


def test_camera_snapshot_route_serves_cached_image(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    old_camera_monitor = main.camera_monitor
    old_reminders = main.reminders
    database = Database(tmp_path / "camera-snapshot.db")
    camera = database.create_camera("Entré", "http://10.0.0.5/", "admin", "pw", 0, True, True, True, "")
    main.database = database
    main.synchronizer.database = database
    main.camera_monitor = CameraMonitor(database)
    main.reminders = RemindersSync(database)
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            with patch.object(
                main.camera_monitor, "snapshot_bytes", return_value=b"fake-jpeg-bytes"
            ):
                response = client.get(f"/api/cameras/{camera['id']}/snapshot", headers=headers)
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/jpeg"
            assert response.content == b"fake-jpeg-bytes"
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
        main.camera_monitor = old_camera_monitor
        main.reminders = old_reminders


def test_build_is_visible_in_health_index_and_service_worker():
    with TestClient(main.app) as client:
        health = client.get("/api/health").json()
        assert health["build"] == main.BUILD_ID
        assert health["version"] == main.VERSION
        assert health["started_at"]

        index = client.get("/").text
        assert "{{BUILD}}" not in index
        assert main.BUILD_ID in index
        assert f"/assets/app.js?v={main.BUILD_ID}" in index
        assert f"/assets/styles.css?v={main.BUILD_ID}" in index
        assert '"/assets/app.js"' not in index

        worker = client.get("/sw.js").text
        assert "{{BUILD_ID}}" not in index
        assert f'window.__FD_BUILD__ = "{main.BUILD_ID}"' in index
        assert "__BUILD__" not in worker
        assert main.BUILD_ID in worker


def test_every_settings_field_can_be_saved(tmp_path):
    from app.schemas import SettingsUpdate

    fields = SettingsUpdate.model_fields
    database = Database(tmp_path / "settings-allowlist.db")
    sample = {
        "log_level": "debug",
        "theme": "dark",
        "header_title": "Hej",
        "greeting": "Godmorgen",
        "display_name": "Familien",
        "reminders_enabled": True,
        "reolink_poll_seconds": 7,
    }
    for field, info in fields.items():
        if field in sample:
            continue
        annotation = info.annotation
        if annotation is bool:
            sample[field] = True
        elif annotation is int:
            sample[field] = 3
        elif annotation is float:
            sample[field] = 1.5
        else:
            sample[field] = "test"
    saved = database.update_settings(sample)
    for field in fields:
        if field.endswith("_href") or field.endswith("_password"):
            continue
        assert field in saved, f"{field} afvises af update_settings()"


def test_home_candidates_derive_from_principal_href():
    client = CalDAVRemindersClient("kim@icloud.com", "x")
    assert client._home_candidates("/141421/141213/") == [
        "https://caldav.icloud.com/141421/calendars/",
        "https://caldav.icloud.com/calendars/",
    ]
    assert client._home_candidates("https://p07-caldav.icloud.com:443/98765/principals/users/1/") == [
        "https://p07-caldav.icloud.com:443/98765/calendars/",
        "https://p07-caldav.icloud.com:443/calendars/",
    ]


def test_log_level_setting_applies_after_save(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    old_reminders = main.reminders
    old_notes = main.icloud_notes
    database = Database(tmp_path / "log-level.db")
    main.database = database
    main.synchronizer.database = database
    main.reminders = RemindersSync(database)
    main.icloud_notes = ICloudNotes(database)
    access_logger = logging.getLogger("uvicorn.access")
    previous = access_logger.level
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            response = client.patch(
                "/api/settings", json={"log_level": "debug"}, headers=headers
            )
            assert response.status_code == 200
            assert response.json()["settings"]["log_level"] == "debug"
            assert database.get_setting("log_level") == "debug"
            assert access_logger.level == logging.DEBUG
            client.patch("/api/settings", json={"log_level": "error"}, headers=headers)
            assert access_logger.level == logging.ERROR
    finally:
        access_logger.setLevel(previous)
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
        main.reminders = old_reminders
        main.icloud_notes = old_notes


def test_settings_redaction_hides_secrets(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    old_reminders = main.reminders
    old_notes = main.icloud_notes
    database = Database(tmp_path / "settings-redact.db")
    database.update_settings(
        {
            "reminders_enabled": "true",
            "reminders_username": "kim@icloud.com",
            "reminders_app_password": "super-hemmelig",
            "reminders_list_href": "/k/",
            "notes_source": "imap",
            "notes_imap_enabled": "true",
            "notes_imap_username": "kim@icloud.com",
            "notes_imap_app_password": "note-hemmelig",
            "notes_imap_note_title": "Indkøb",
        }
    )
    main.database = database
    main.synchronizer.database = database
    main.reminders = RemindersSync(database)
    main.icloud_notes = ICloudNotes(database)
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            settings = client.get("/api/settings", headers=headers).json()["settings"]
            assert settings["reminders_app_password"] == ""
            assert settings["notes_imap_app_password"] == ""
            assert settings["reminders_configured"] is True
            assert settings["notes_imap_configured"] is True
            summary = client.get("/api/dashboard/summary").json()
            assert summary["settings"]["reminders_app_password"] == ""
            assert "super-hemmelig" not in str(summary["settings"])
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
        main.reminders = old_reminders
        main.icloud_notes = old_notes


def test_checklist_routes_route_to_icloud_when_enabled(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    old_reminders = main.reminders
    database = Database(tmp_path / "checklist-routing.db")
    database.update_settings(
        {
            "reminders_enabled": "true",
            "reminders_username": "kim@icloud.com",
            "reminders_app_password": "app-pw",
            "reminders_list_href": "/k/",
        }
    )
    main.database = database
    main.synchronizer.database = database
    main.reminders = RemindersSync(database)
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
                CalDAVRemindersClient, "create_todo", new=AsyncMock(return_value="X")
            ):
                response = client.post(
                    "/api/checklist", json={"text": "Fra iCloud"}, headers=headers
                )
            assert response.status_code == 201
            assert response.json()["item"]["source"] == "icloud"

            item = database.get_checklist_item_by_external("X")
            assert item is not None
            assert item["text"] == "Fra iCloud"

            with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
                CalDAVRemindersClient, "update_todo", new=AsyncMock()
            ):
                response = client.patch(
                    f"/api/checklist/{item['id']}",
                    json={"done": True},
                    headers=headers,
                )
            assert response.status_code == 200
            assert response.json()["item"]["done"] is True
            assert database.get_checklist_item(item["id"])["done"] is True

            with patch.object(CalDAVRemindersClient, "prepare", new=AsyncMock()), patch.object(
                CalDAVRemindersClient, "delete_todo", new=AsyncMock()
            ):
                response = client.delete(f"/api/checklist/{item['id']}", headers=headers)
            assert response.json()["deleted"] is True
            assert database.get_checklist_item(item["id"]) is None
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
        main.reminders = old_reminders


def test_checklist_routes_are_local_when_disabled(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    old_reminders = main.reminders
    database = Database(tmp_path / "checklist-local.db")
    main.database = database
    main.synchronizer.database = database
    main.reminders = RemindersSync(database)
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            response = client.post("/api/checklist", json={"text": "Lokal"})
            assert response.status_code in (401, 403)
            response = client.post(
                "/api/checklist", json={"text": "Lokal"}, headers=headers
            )
            assert response.status_code == 201
            item = response.json()["item"]
            assert item["source"] == "local"
            assert database.get_checklist_item(item["id"]) is not None
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
        main.reminders = old_reminders


def test_notes_icloud_routes_disabled_are_safe(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    old_notes = main.icloud_notes
    database = Database(tmp_path / "notes-routes.db")
    main.database = database
    main.synchronizer.database = database
    main.icloud_notes = ICloudNotes(database)
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            fetched = client.get("/api/notes/icloud", headers=headers).json()
            assert fetched["enabled"] is False
            saved = client.post(
                "/api/notes/icloud/save", json={"content": "Hej"}, headers=headers
            ).json()
            assert saved["saved"] is False
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
        main.icloud_notes = old_notes


def test_icloud_caldav_credential_candidates():
    client = CalDAVRemindersClient("kim", "xxxx-xxxx-xxxx-xxxx")
    assert client._credential_candidates() == [
        ("kim", "xxxx-xxxx-xxxx-xxxx"),
        ("kim@icloud.com", "xxxx-xxxx-xxxx-xxxx"),
        ("kim@me.com", "xxxx-xxxx-xxxx-xxxx"),
        ("kim@mac.com", "xxxx-xxxx-xxxx-xxxx"),
    ]
    fully_qualified = CalDAVRemindersClient("kim@icloud.com", "x")
    assert fully_qualified._credential_candidates() == [("kim@icloud.com", "x")]


def test_client_falls_back_to_notes_credentials():
    client = CalDAVRemindersClient(
        "kim",
        "reminders-pass",
        extra_auth=[("kim@icloud.com", "notes-pass")],
    )
    candidates = client._credential_candidates()
    assert ("kim@icloud.com", "notes-pass") in candidates
    assert candidates[0] == ("kim", "reminders-pass")


async def _prepare_with_retry():
    client = CalDAVRemindersClient("kim", "xxxx-xxxx-xxxx-xxxx")
    attempts = {"count": 0}

    async def fake_principal(inner):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise CalDAVError("Login blev afvist (401) mod caldav.icloud.com – tjek…")
        return "/principal/"

    async def fake_home(inner, principal_href):
        return "/calendar-home/"

    with patch.object(client, "_discover_principal", side_effect=fake_principal), patch.object(
        client, "_discover_home", side_effect=fake_home
    ):
        await client.prepare(AsyncMock())

    assert attempts["count"] == 2
    assert client.resolved_username == "kim@icloud.com"


def test_prepare_retries_with_full_apple_id():
    asyncio.run(_prepare_with_retry())


def _xml_response(payload: bytes) -> httpx.Response:
    return httpx.Response(200, content=payload, headers={"Content-Type": "application/xml"})


def test_find_property_href_ignores_resource_href_and_prefixes():
    xml = (
        b'<?xml version="1.0"?>'
        b'<x:multistatus xmlns:x="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        b"<x:response><x:href>/</x:href><x:propstat><x:prop>"
        b"<x:calendar-home-set><x:href>/141421/141213/calendars/</x:href></x:calendar-home-set>"
        b"</x:prop></x:propstat></x:response></x:multistatus>"
    )
    assert _find_property_href(xml, "calendar-home-set") == "/141421/141213/calendars/"

    nested = (
        b'<multistatus xmlns="DAV:"><response><href>/</href><propstat><prop>'
        b"<calendar-home-set><href>https://p07-caldav.icloud.com:443/123/calendars/</href>"
        b"</calendar-home-set></prop></propstat></response></multistatus>"
    )
    assert _find_property_href(nested, "calendar-home-set", "calendarHomeSet") == (
        "https://p07-caldav.icloud.com:443/123/calendars/"
    )
    assert _find_property_href(nested, "current-user-principal") == ""


async def _discover_home_with_root_fallback():
    client = CalDAVRemindersClient("kim@icloud.com", "xxxx-xxxx-xxxx-xxxx")
    responses = [
        _xml_response(b'<multistatus xmlns="DAV:"><response><href>/1/</href></response></multistatus>'),
        _xml_response(
            b'<multistatus xmlns="DAV:"><response><href>/</href><propstat><prop>'
            b"<calendar-home-set><href>/2/calendars/</href></calendar-home-set>"
            b"</prop></propstat></response></multistatus>"
        ),
    ]
    seen: list[str] = []

    async def fake_request(_client, _method, url, *_args, **_kwargs):
        seen.append(url)
        return responses.pop(0)

    with patch.object(client, "_request", side_effect=fake_request):
        home = await client._discover_home(AsyncMock(), "/1/principal/")

    assert home == "/2/calendars/"
    assert seen == ["https://caldav.icloud.com/1/principal/", "https://caldav.icloud.com/"]


def test_discover_home_falls_back_to_root():
    asyncio.run(_discover_home_with_root_fallback())


def test_ffmpeg_mjpeg_args():
    args = ffmpeg_mjpeg_args("rtsp://u:p@1.2.3.4:554/h264Preview_01_main", height=720)
    assert args[0] == "ffmpeg"
    assert "-rtsp_transport" in args
    assert "mpjpeg" in args
    assert args[args.index("-i") + 1] == "rtsp://u:p@1.2.3.4:554/h264Preview_01_main"
    assert "h264Preview_01_main" in " ".join(args)


def test_is_streaming_url():
    assert is_streaming_url("rtsp://u:p@1.2.3.4:554/h264Preview_01_main")
    assert not is_streaming_url("")
    assert not is_streaming_url("http://1.2.3.4/live")


def test_stream_camera_mjpeg_yields_and_kills():
    class FakeProc:
        def __init__(self) -> None:
            self.returncode = None
            self.stdout = AsyncMock()
            self.stdout.read = AsyncMock(side_effect=[b"chunk-1", b""])
            self.kill = Mock()
            self.wait = AsyncMock(return_value=0)

    async def collect():
        with patch("app.reolink.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
            "app.reolink.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ):
            return [
                chunk
                async for chunk in stream_camera_mjpeg(
                    {"live_stream_url": "rtsp://u:p@1.2.3.4:554/h264Preview_01_main"},
                    max_seconds=30,
                    restart=False,
                )
            ]

    proc = FakeProc()
    chunks = asyncio.run(collect())
    assert chunks == [b"chunk-1"]
    proc.kill.assert_called_once()

# --- Mac mini-bridge (EventKit) -------------------------------------------


class mock_bridge_transport:
    """Injects an httpx.MockTransport into every AsyncClient the app creates."""

    def __init__(self, handler):
        self.handler = handler
        self._real = httpx.AsyncClient

    def __call__(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(self.handler)
        return self._real(*args, **kwargs)





def test_normalize_bridge_url_accepts_forms():
    from app.reminders_bridge import normalize_bridge_url

    assert normalize_bridge_url("192.168.1.100:8787") == "http://192.168.1.100:8787"
    assert normalize_bridge_url("http://mac-mini.local:8787/") == "http://mac-mini.local:8787"
    assert normalize_bridge_url("https://mac.example.com/bridge/") == "https://mac.example.com/bridge"
    with pytest.raises(ValueError):
        normalize_bridge_url("")
    with pytest.raises(ValueError):
        normalize_bridge_url("ftp://mac:8787")


def test_bridge_client_parses_lists_and_tasks():
    from app.reminders_bridge import BridgeRemindersClient

    client = BridgeRemindersClient("http://mac:8787", "secret")
    assert client.configured is True

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/lists":
            return httpx.Response(
                200,
                json={
                    "lists": [
                        {"id": "list-1", "name": "Familie", "color": "#ff0000"},
                        {"name": "uden id"},
                    ]
                },
            )
        if request.url.path == "/todos":
            assert request.url.params["list"] == "list-1"
            return httpx.Response(
                200,
                json={
                    "todos": [
                        {
                            "id": "t1",
                            "title": "Køb mælk",
                            "done": False,
                            "due": "2026-10-01T00:00:00Z",
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": "nope"})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            lists = await client.list_task_lists(http)
            tasks = await client.list_tasks(http, "list-1")
            return lists, tasks

    lists, tasks = asyncio.run(run())
    assert lists == [{"href": "list-1", "name": "Familie", "color": "#ff0000"}]
    assert tasks[0]["external_id"] == "t1"
    assert tasks[0]["summary"] == "Køb mælk"
    assert tasks[0]["due"] == date(2026, 10, 1)


def test_bridge_client_reports_auth_and_network_errors():
    from app.reminders_bridge import BridgeError, BridgeRemindersClient

    client = BridgeRemindersClient("http://mac:8787", "secret")

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Ugyldigt token"})

    async def run_auth():
        transport = httpx.MockTransport(unauthorized)
        async with httpx.AsyncClient(transport=transport) as http:
            await client.list_task_lists(http)

    with pytest.raises(BridgeError) as error:
        asyncio.run(run_auth())
    assert "token" in str(error.value).lower()

    offline = BridgeRemindersClient("http://mac:9999", "secret")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def run_offline():
        transport = httpx.MockTransport(boom)
        async with httpx.AsyncClient(transport=transport) as http:
            await offline.list_task_lists(http)

    with pytest.raises(BridgeError):
        asyncio.run(run_offline())


def test_reminders_sync_uses_bridge_provider(tmp_path):
    database = Database(tmp_path / "bridge-sync.db")
    database.update_settings(
        {
            "reminders_enabled": "true",
            "reminders_source": "bridge",
            "reminders_bridge_url": "http://192.168.1.100:8787",
            "reminders_bridge_token": "secret",
            "reminders_list_href": "list-1",
        }
    )
    sync = RemindersSync(database)
    assert sync.source() == "bridge"
    assert sync.provider().name == "bridge"
    assert sync.configured() is True

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        if request.url.path == "/todos":
            return httpx.Response(
                200,
                json={
                    "todos": [
                        {"id": "A", "title": "Hent brød", "done": False, "due": "2026-10-02"},
                        {"id": "B", "title": "Betalt", "done": True, "due": None},
                    ]
                },
            )
        return httpx.Response(404, json={"error": "nope"})

    with patch("httpx.AsyncClient", new=mock_bridge_transport(handler)):
        result = asyncio.run(sync.sync())
    assert result["synced"] is True
    assert result["count"] == 2
    items = {item["external_id"]: item for item in database.list_checklist()}
    assert items["A"]["text"] == "Hent brød"
    assert items["A"]["due_date"] == "2026-10-02"
    assert items["B"]["done"] is True
    assert result == {"synced": True, "count": 2}
    items = {item["external_id"]: item for item in database.list_checklist()}
    assert items["A"]["text"] == "Hent brød"
    assert items["A"]["due_date"] == "2026-10-02"
    assert items["B"]["done"] is True


def test_reminders_test_connection_flags_missing_bridge_access(tmp_path):
    database = Database(tmp_path / "bridge-test.db")
    database.update_settings(
        {
            "reminders_source": "bridge",
            "reminders_bridge_url": "http://192.168.1.100:8787",
            "reminders_bridge_token": "secret",
            "reminders_list_href": "list-1",
        }
    )
    sync = RemindersSync(database)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"ok": True, "reminders_access": False, "authorization": "denied"},
            )
        return httpx.Response(200, json={"lists": []})

    with patch("httpx.AsyncClient", new=mock_bridge_transport(handler)):
        result = asyncio.run(sync.test_connection())
    assert result["ok"] is False
    assert "Påmindelser" in result["message"]


def test_reminders_test_connection_reports_lists(tmp_path):
    database = Database(tmp_path / "bridge-ok.db")
    database.update_settings(
        {
            "reminders_source": "bridge",
            "reminders_bridge_url": "http://192.168.1.100:8787",
            "reminders_bridge_token": "secret",
            "reminders_list_href": "list-1",
        }
    )
    sync = RemindersSync(database)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "reminders_access": True})
        if request.url.path == "/lists":
            return httpx.Response(200, json={"lists": [{"id": "l1", "name": "Familie"}]})
        return httpx.Response(404, json={"error": "nope"})

    with patch("httpx.AsyncClient", new=mock_bridge_transport(handler)):
        result = asyncio.run(sync.test_connection())
    assert result["ok"] is True
    assert "Familie" in result["message"]


def test_notes_fall_back_to_bridge(tmp_path):
    database = Database(tmp_path / "notes-bridge.db")
    database.update_settings(
        {
            "notes_source": "bridge",
            "notes_imap_enabled": "true",
            "notes_imap_note_title": "FMD",
            "reminders_bridge_url": "http://192.168.1.100:8787",
            "reminders_bridge_token": "secret",
        }
    )
    notes = ICloudNotes(database)
    assert notes.use_bridge() is True
    assert notes.configured() is True

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/notes"
        assert request.url.params["title"] == "FMD"
        return httpx.Response(
            200,
            json={
                "notes": [
                    {
                        "id": "xcore",
                        "title": "FMD",
                        "text": "Husk at ringe til Bagedystet",
                        "html": "<p>Husk at ringe til Bagedystet</p>",
                    }
                ]
            },
        )

    with patch("httpx.AsyncClient", new=mock_bridge_transport(handler)):
        result = asyncio.run(notes.fetch())
    assert result["found"] is True
    assert result["title"] == "FMD"
    assert result["content"] == "Husk at ringe til Bagedystet"


def test_notes_bridge_reports_missing_note(tmp_path):
    database = Database(tmp_path / "notes-bridge-missing.db")
    database.update_settings(
        {
            "notes_source": "bridge",
            "notes_imap_enabled": "true",
            "notes_imap_note_title": "Findes ikke",
            "reminders_bridge_url": "http://192.168.1.100:8787",
            "reminders_bridge_token": "secret",
        }
    )
    notes = ICloudNotes(database)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"notes": []})

    with patch("httpx.AsyncClient", new=mock_bridge_transport(handler)):
        result = asyncio.run(notes.fetch())
    assert result["found"] is False
    assert "Notes.app" in result["debug"]


def test_notes_bridge_save_reports_error(tmp_path):
    database = Database(tmp_path / "notes-bridge-save.db")
    database.update_settings(
        {
            "notes_source": "bridge",
            "notes_imap_enabled": "true",
            "notes_imap_note_title": "FMD",
            "reminders_bridge_url": "http://192.168.1.100:8787",
            "reminders_bridge_token": "secret",
        }
    )
    notes = ICloudNotes(database)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False, "error": "Notes.app nægtede adgang"})

    with patch("httpx.AsyncClient", new=mock_bridge_transport(handler)):
        result = asyncio.run(notes.save("ny tekst"))
    assert result["saved"] is False
    assert "nægtede adgang" in result["error"]


def test_settings_endpoint_hides_bridge_token_and_validates_source(tmp_path):
    old_database = main.database
    old_synchronizer = main.synchronizer.database
    old_reminders = main.reminders
    old_notes = main.icloud_notes
    database = Database(tmp_path / "settings-bridge.db")
    main.database = database
    main.synchronizer.database = database
    main.reminders = RemindersSync(database)
    main.icloud_notes = ICloudNotes(database)
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            response = client.patch(
                "/api/settings",
                headers=headers,
                json={
                    "reminders_source": "bridge",
                    "reminders_bridge_url": "192.168.1.100:8787",
                    "reminders_bridge_token": "hemmeligt-token",
                    "reminders_enabled": True,
                },
            )
            assert response.status_code == 200
            settings = response.json()["settings"]
            assert settings["reminders_source"] == "bridge"
            assert settings["reminders_bridge_url"] == "http://192.168.1.100:8787"
            assert settings["reminders_bridge_token"] == ""
            assert "hemmeligt-token" not in str(settings)
            assert settings["reminders_bridge_configured"] is True

            missing = client.patch(
                "/api/settings", headers=headers, json={"reminders_source": "bridge", "reminders_bridge_url": ""}
            )
            assert missing.status_code == 422
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer
        main.reminders = old_reminders
        main.icloud_notes = old_notes


def test_camera_requires_password():
    from app.reolink import ReolinkCamera, ReolinkError

    camera = ReolinkCamera(
        {
            "id": 1,
            "name": "Indkørsel",
            "host": "192.168.1.219",
            "username": "admin",
            "password": "",
            "channel": 0,
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cmd": "Login", "code": 1})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            await camera._ensure_token(http)

    with pytest.raises(ReolinkError) as error:
        asyncio.run(run())
    assert "password" in str(error.value).lower()


def test_lists_can_be_discovered_before_a_list_is_selected(tmp_path):
    database = Database(tmp_path / "bridge-first-list.db")
    database.update_settings(
        {
            "reminders_source": "bridge",
            "reminders_bridge_url": "http://192.168.1.100:8787",
            "reminders_bridge_token": "secret",
        }
    )
    sync = RemindersSync(database)
    assert sync.configured() is False  # no list chosen yet

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"lists": [{"id": "l1", "name": "Familie"}, {"id": "l2", "name": "Indkøb"}]})

    with patch("httpx.AsyncClient", new=mock_bridge_transport(handler)):
        lists, error = asyncio.run(sync.discover_lists())
    assert error is None
    assert [item["name"] for item in lists] == ["Familie", "Indkøb"]


def test_list_discovery_without_credentials_explains_what_is_missing(tmp_path):
    database = Database(tmp_path / "bridge-no-credentials.db")
    database.update_settings({"reminders_source": "bridge"})
    sync = RemindersSync(database)

    lists, error = asyncio.run(sync.discover_lists())
    assert lists == []
    assert "token" in error
    result = asyncio.run(sync.test_connection())
    assert result["ok"] is False
    assert "token" in result["message"]


def test_http_client_logging_is_not_debug():
    import logging
    import subprocess
    import sys

    script = (
        "import logging, app.main;"
        "print(logging.getLogger('httpx').level, logging.getLogger('httpcore').level)"
    )
    env = dict(os.environ, FAMILY_DASHBOARD_PASSWORD="test", FAMILY_DASHBOARD_DATA_DIR=tempfile.mkdtemp())
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(pathlib.Path(__file__).resolve().parents[1])
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [str(logging.WARNING), str(logging.WARNING)]


def test_today_layout_settings_roundtrip_and_validation(tmp_path):
    old_database = main.database
    old_synchronizer_database = main.synchronizer.database
    database = Database(tmp_path / "layout-api.db")
    main.database = database
    main.synchronizer.database = database
    try:
        with TestClient(main.app) as client:
            headers = _auth_client(client)
            layout = [
                {"id": "hero", "span": 2, "row": 1, "align": "center", "valign": "top", "hidden": False},
                {"id": "checklist", "span": 1, "row": 2, "align": "left", "valign": "top", "hidden": True},
            ]
            response = client.patch(
                "/api/settings",
                json={
                    "today_layout_mode": "manual",
                    "today_layout": json.dumps(layout),
                    "today_columns": 5,
                    "today_custom_css": "#view-today .hero-card { gap: 40px; }",
                },
                headers=headers,
            )
            assert response.status_code == 200
            settings = response.json()["settings"]
            assert settings["today_layout_mode"] == "manual"
            assert json.loads(settings["today_layout"]) == layout
            assert settings["today_columns"] == "5"
            assert "gap: 40px" in settings["today_custom_css"]

            # Layoutet skal være gyldig JSON, ellers ville alle kort forsvinde.
            assert client.patch(
                "/api/settings", json={"today_layout": "{ikke json"}, headers=headers
            ).status_code == 422
            assert client.patch(
                "/api/settings", json={"today_layout": '{"id": "hero"}'}, headers=headers
            ).status_code == 422
            assert client.patch(
                "/api/settings", json={"today_layout": json.dumps([{"id": f"c{i}"} for i in range(41)])},
                headers=headers,
            ).status_code == 422

            # Kun auto og manual er gyldige tilstande, og kolonner skal være 1-8.
            assert client.patch(
                "/api/settings", json={"today_layout_mode": "tilfældig"}, headers=headers
            ).status_code == 422
            assert client.patch(
                "/api/settings", json={"today_columns": 0}, headers=headers
            ).status_code == 422
            assert client.patch(
                "/api/settings", json={"today_columns": 9}, headers=headers
            ).status_code == 422

            # Det gemte layout skal komme med i summary, så browseren kan anvende det.
            summary = client.get("/api/dashboard/summary?days=14").json()
            assert summary["settings"]["today_layout_mode"] == "manual"
    finally:
        main.database = old_database
        main.synchronizer.database = old_synchronizer_database
