import asyncio
import email
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

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
    _parse_calendar_home,
    build_todo,
    parse_todo,
)
from app.reolink import CameraMonitor, ReolinkCamera, normalize_host


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