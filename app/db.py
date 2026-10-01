import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_SETTINGS = {
    "display_name": "Familiedashboard",
    "timezone": "Europe/Copenhagen",
    "location_name": "København",
    "latitude": "55.6761",
    "longitude": "12.5683",
    "temperature_unit": "celsius",
    # Tema. "auto" skifter mellem lyst og mørkt efter klokkeslæt,
    # så vægskærmen ikke er lys i en mørk stue hele aftenen.
    "theme": "auto",
    "theme_day_start": "07:00",
    "theme_night_start": "20:00",
    "show_seconds": "true",
    "calendar_refresh_minutes": "15",
    "weather_enabled": "true",
    "reminders_enabled": "false",
    "reminders_source": "caldav",
    "reminders_bridge_url": "",
    "reminders_bridge_token": "",
    "reminders_username": "",
    "reminders_app_password": "",
    "reminders_list_name": "",
    "reminders_list_href": "",
    "reminders_sync_minutes": "5",
    "reminders_last_sync": "",
    "reminders_last_error": "",
    "notes_source": "bridge",
    "notes_imap_enabled": "false",
    "notes_imap_username": "",
    "notes_imap_app_password": "",
    "notes_imap_host": "imap.mail.me.com",
    "notes_imap_note_title": "",
    "notes_imap_last_sync": "",
    "notes_imap_last_error": "",
    "reolink_poll_seconds": "5",
    "reolink_close_delay": "0",
    "reolink_live_delay": "3",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path, default_settings: Optional[dict[str, str]] = None) -> None:
        self.path = Path(path)
        self.default_settings = dict(DEFAULT_SETTINGS)
        self.default_settings.update(default_settings or {})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS family_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT '#5c7cfa',
                    avatar_url TEXT,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS birthdays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    birth_date TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT '#f59e0b',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calendar_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'ics',
                    kind TEXT NOT NULL DEFAULT 'calendar',
                    color TEXT NOT NULL DEFAULT '#5c7cfa',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_synced_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES calendar_sources(id) ON DELETE CASCADE,
                    uid TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    start_at TEXT NOT NULL,
                    end_at TEXT,
                    all_day INTEGER NOT NULL DEFAULT 0,
                    recurrence_timezone TEXT,
                    rrule TEXT,
                    rdate_json TEXT NOT NULL DEFAULT '[]',
                    exdate_json TEXT NOT NULL DEFAULT '[]',
                    url TEXT,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_id, uid)
                );
                CREATE INDEX IF NOT EXISTS events_start_idx ON events(start_at);
                CREATE INDEX IF NOT EXISTS events_source_idx ON events(source_id);
                CREATE TABLE IF NOT EXISTS frames (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    height INTEGER NOT NULL DEFAULT 320,
                    accent TEXT NOT NULL DEFAULT '#5c7cfa',
                    visible INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT '#64748b',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checklist_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    due_date TEXT,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_kind TEXT NOT NULL DEFAULT 'calendar',
                    source_id INTEGER,
                    match_mode TEXT NOT NULL DEFAULT 'exact',
                    match_value TEXT NOT NULL,
                    text TEXT NOT NULL,
                    lead_hours INTEGER NOT NULL DEFAULT 12,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_reminders_kind_idx
                    ON event_reminders(source_kind, enabled);
                CREATE TABLE IF NOT EXISTS cameras (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    channel INTEGER NOT NULL DEFAULT 0,
                    person_enabled INTEGER NOT NULL DEFAULT 1,
                    vehicle_enabled INTEGER NOT NULL DEFAULT 1,
                    snapshots_enabled INTEGER NOT NULL DEFAULT 1,
                    popup_enabled INTEGER NOT NULL DEFAULT 1,
                    live_stream_url TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS camera_activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                    detection_type TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );
                """
            )
            source_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(calendar_sources)").fetchall()
            }
            if "kind" not in source_columns:
                connection.execute(
                    "ALTER TABLE calendar_sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'calendar'"
                )
                connection.execute(
                    "UPDATE calendar_sources SET kind = 'school' "
                    "WHERE LOWER(TRIM(name)) = 'aula' "
                    "OR LOWER(TRIM(name)) LIKE 'aula %' "
                    "OR LOWER(TRIM(name)) LIKE '% aula'"
                )
            event_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "recurrence_timezone" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN recurrence_timezone TEXT")
            checklist_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(checklist_items)").fetchall()
            }
            if "source" not in checklist_columns:
                connection.execute(
                    "ALTER TABLE checklist_items ADD COLUMN source TEXT NOT NULL DEFAULT 'local'"
                )
            if "external_id" not in checklist_columns:
                connection.execute("ALTER TABLE checklist_items ADD COLUMN external_id TEXT")
            # Hvilke kameraer der skal give popup. Kun den indkørsel, man
            # faktisk vil høre fra, skal springe en fuld skærm frem.
            camera_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(cameras)").fetchall()
            }
            if "popup_enabled" not in camera_columns:
                connection.execute(
                    "ALTER TABLE cameras ADD COLUMN popup_enabled INTEGER NOT NULL DEFAULT 1"
                )
            for key, value in self.default_settings.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
                )

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def get_settings(self) -> dict[str, str]:
        with self.connection() as connection:
            rows = connection.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def update_settings(self, values: dict[str, Any]) -> dict[str, str]:
        allowed = set(self.default_settings) | {
            "theme",
            "header_title",
            "greeting",
            "log_level",
            "today_layout_mode",
            "today_layout",
            "today_columns",
            "today_custom_css",
        }
        with self.connection() as connection:
            for key, value in values.items():
                if key not in allowed:
                    continue
                if isinstance(value, bool):
                    value = "true" if value else "false"
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )
        return self.get_settings()

    def list_members(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM family_members ORDER BY sort_order, id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def create_member(self, name: str, color: str, avatar_url: Optional[str]) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM family_members"
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO family_members(name, color, avatar_url, sort_order, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, color, avatar_url, order, now),
            )
            row = connection.execute(
                "SELECT * FROM family_members WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._public(row)

    def update_member(self, member_id: int, values: dict[str, Any]) -> Optional[dict[str, Any]]:
        fields = {
            key: value
            for key, value in values.items()
            if key in {"name", "color", "avatar_url", "sort_order"}
        }
        if not fields:
            return self.get_member(member_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE family_members SET {assignments} WHERE id = ?",
                [*fields.values(), member_id],
            )
        return self.get_member(member_id)

    def get_member(self, member_id: int) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM family_members WHERE id = ?", (member_id,)
            ).fetchone()
        return self._public(row) if row else None

    def delete_member(self, member_id: int) -> bool:
        with self.connection() as connection:
            return (
                connection.execute("DELETE FROM family_members WHERE id = ?", (member_id,)).rowcount
                > 0
            )

    def list_birthdays(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM birthdays ORDER BY name COLLATE NOCASE, id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def create_birthday(self, name: str, birth_date: str, color: str, notes: str) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO birthdays(name, birth_date, color, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, birth_date, color, notes, now),
            )
            row = connection.execute(
                "SELECT * FROM birthdays WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._public(row)

    def update_birthday(self, birthday_id: int, values: dict[str, Any]) -> Optional[dict[str, Any]]:
        fields = {
            key: value
            for key, value in values.items()
            if key in {"name", "birth_date", "color", "notes"}
        }
        if not fields:
            return self.get_birthday(birthday_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE birthdays SET {assignments} WHERE id = ?",
                [*fields.values(), birthday_id],
            )
        return self.get_birthday(birthday_id)

    def get_birthday(self, birthday_id: int) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM birthdays WHERE id = ?", (birthday_id,)
            ).fetchone()
        return self._public(row) if row else None

    def delete_birthday(self, birthday_id: int) -> bool:
        with self.connection() as connection:
            return (
                connection.execute("DELETE FROM birthdays WHERE id = ?", (birthday_id,)).rowcount
                > 0
            )

    def list_sources(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM calendar_sources"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        with self.connection() as connection:
            rows = connection.execute(query).fetchall()
        return [self._public(row) for row in rows]

    def get_source(self, source_id: int) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM calendar_sources WHERE id = ?", (source_id,)
            ).fetchone()
        return self._public(row) if row else None

    def create_source(
        self,
        name: str,
        url: str,
        source_type: str,
        kind: str,
        color: str,
        enabled: bool,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO calendar_sources(name, url, source_type, kind, color, enabled, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, url, source_type, kind, color, int(enabled), now, now),
            )
            row = connection.execute(
                "SELECT * FROM calendar_sources WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._public(row)

    def update_source(self, source_id: int, values: dict[str, Any]) -> Optional[dict[str, Any]]:
        fields = {
            key: value
            for key, value in values.items()
            if key in {"name", "url", "source_type", "kind", "color", "enabled"}
        }
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        if not fields:
            return self.get_source(source_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE calendar_sources SET {assignments}, updated_at = ? WHERE id = ?",
                [*fields.values(), utc_now(), source_id],
            )
        return self.get_source(source_id)

    def delete_source(self, source_id: int) -> bool:
        with self.connection() as connection:
            return (
                connection.execute(
                    "DELETE FROM calendar_sources WHERE id = ?", (source_id,)
                ).rowcount
                > 0
            )

    def set_source_sync(
        self, source_id: int, synced_at: Optional[str], error: Optional[str]
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE calendar_sources SET last_synced_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (synced_at, error, utc_now(), source_id),
            )

    def replace_source_events(self, source_id: int, events: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("DELETE FROM events WHERE source_id = ?", (source_id,))
            for event in events:
                connection.execute(
                    "INSERT INTO events(id, source_id, uid, title, description, location, start_at, end_at, "
                    "all_day, recurrence_timezone, rrule, rdate_json, exdate_json, url, status, "
                    "last_seen_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["id"],
                        source_id,
                        event["uid"],
                        event["title"],
                        event.get("description", ""),
                        event.get("location", ""),
                        event["start_at"],
                        event.get("end_at"),
                        int(event.get("all_day", False)),
                        event.get("recurrence_timezone"),
                        event.get("rrule"),
                        _json(event.get("rdates", [])),
                        _json(event.get("exdates", [])),
                        event.get("url"),
                        event.get("status", "confirmed"),
                        now,
                        now,
                    ),
                )

    def list_events(self, source_ids: Optional[list[int]] = None) -> list[dict[str, Any]]:
        query = (
            "SELECT events.*, calendar_sources.name AS source_name, "
            "calendar_sources.color AS source_color, calendar_sources.kind AS source_kind "
            "FROM events JOIN calendar_sources ON calendar_sources.id = events.source_id"
        )
        params: list[Any] = []
        if source_ids:
            query += f" WHERE events.source_id IN ({','.join('?' for _ in source_ids)})"
            params.extend(source_ids)
        query += " ORDER BY events.start_at"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = self._public(row)
            item["rdates"] = json.loads(item.pop("rdate_json") or "[]")
            item["exdates"] = json.loads(item.pop("exdate_json") or "[]")
            item["all_day"] = bool(item["all_day"])
            result.append(item)
        return result

    def list_frames(self, visible_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM frames"
        if visible_only:
            query += " WHERE visible = 1"
        query += " ORDER BY sort_order, id"
        with self.connection() as connection:
            rows = connection.execute(query).fetchall()
        return [self._public(row) for row in rows]

    def create_frame(
        self, name: str, url: str, height: int, accent: str, visible: bool
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM frames"
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO frames(name, url, height, accent, visible, sort_order, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, url, height, accent, int(visible), order, now, now),
            )
            row = connection.execute(
                "SELECT * FROM frames WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._public(row)

    def update_frame(self, frame_id: int, values: dict[str, Any]) -> Optional[dict[str, Any]]:
        fields = {
            key: value
            for key, value in values.items()
            if key in {"name", "url", "height", "accent", "visible", "sort_order"}
        }
        if "visible" in fields:
            fields["visible"] = int(bool(fields["visible"]))
        if not fields:
            return self.get_frame(frame_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE frames SET {assignments}, updated_at = ? WHERE id = ?",
                [*fields.values(), utc_now(), frame_id],
            )
        return self.get_frame(frame_id)

    def get_frame(self, frame_id: int) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM frames WHERE id = ?", (frame_id,)).fetchone()
        return self._public(row) if row else None

    def delete_frame(self, frame_id: int) -> bool:
        with self.connection() as connection:
            return connection.execute("DELETE FROM frames WHERE id = ?", (frame_id,)).rowcount > 0

    def list_cameras(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM cameras ORDER BY sort_order, id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def get_camera(self, camera_id: int) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
        return self._public(row) if row else None

    def create_camera(
        self,
        name: str,
        host: str,
        username: str,
        password: str,
        channel: int,
        person_enabled: bool,
        vehicle_enabled: bool,
        snapshots_enabled: bool,
        live_stream_url: str,
        popup_enabled: bool = True,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM cameras"
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO cameras(name, host, username, password, channel, person_enabled, "
                "vehicle_enabled, snapshots_enabled, popup_enabled, live_stream_url, sort_order, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    host,
                    username,
                    password,
                    channel,
                    int(person_enabled),
                    int(vehicle_enabled),
                    int(snapshots_enabled),
                    int(popup_enabled),
                    live_stream_url,
                    order,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cameras WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._public(row)

    def update_camera(self, camera_id: int, values: dict[str, Any]) -> Optional[dict[str, Any]]:
        fields = {
            key: value
            for key, value in values.items()
            if key
            in {
                "name",
                "host",
                "username",
                "password",
                "channel",
                "person_enabled",
                "vehicle_enabled",
                "snapshots_enabled",
                "popup_enabled",
                "live_stream_url",
                "sort_order",
            }
        }
        for key in ("person_enabled", "vehicle_enabled", "snapshots_enabled", "popup_enabled"):
            if key in fields:
                fields[key] = int(bool(fields[key]))
        if not fields:
            return self.get_camera(camera_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE cameras SET {assignments}, updated_at = ? WHERE id = ?",
                [*fields.values(), utc_now(), camera_id],
            )
            if "host" in fields:
                connection.execute(
                    "DELETE FROM camera_activity_log WHERE camera_id = ?", (camera_id,)
                )
        return self.get_camera(camera_id)

    def delete_camera(self, camera_id: int) -> bool:
        with self.connection() as connection:
            return connection.execute("DELETE FROM cameras WHERE id = ?", (camera_id,)).rowcount > 0

    def log_detection_start(self, camera_id: int, detection_type: str, started_at: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO camera_activity_log(camera_id, detection_type, started_at) "
                "VALUES (?, ?, ?)",
                (camera_id, detection_type, started_at),
            )

    def log_detection_end(self, camera_id: int, detection_type: str, ended_at: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE camera_activity_log SET ended_at = ? "
                "WHERE camera_id = ? AND detection_type = ? AND ended_at IS NULL",
                (ended_at, camera_id, detection_type),
            )

    def recent_camera_activity(self, limit: int = 15) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT camera_activity_log.*, cameras.name AS camera_name "
                "FROM camera_activity_log JOIN cameras ON cameras.id = camera_activity_log.camera_id "
                "ORDER BY camera_activity_log.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def upsert_icloud_checklist_item(
        self,
        external_id: str,
        text: str,
        done: bool,
        due_date: Optional[str],
        sort_order: int,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT id FROM checklist_items WHERE source = 'icloud' AND external_id = ?",
                (external_id,),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE checklist_items SET text = ?, done = ?, due_date = ?, "
                    "sort_order = ?, updated_at = ? WHERE id = ?",
                    (text, int(done), due_date, sort_order, now, existing["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM checklist_items WHERE id = ?", (existing["id"],)
                ).fetchone()
            else:
                cursor = connection.execute(
                    "INSERT INTO checklist_items(text, done, due_date, sort_order, source, "
                    "external_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'icloud', ?, ?, ?)",
                    (text, int(done), due_date, sort_order, external_id, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM checklist_items WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        return self._public(row)

    def remove_icloud_checklist_missing(self, external_ids: set[str]) -> None:
        with self.connection() as connection:
            if not external_ids:
                connection.execute(
                    "DELETE FROM checklist_items WHERE source = 'icloud'"
                )
                return
            connection.execute(
                "DELETE FROM checklist_items WHERE source = 'icloud' "
                "AND external_id NOT IN ({})".format(
                    ",".join("?" for _ in external_ids)
                ),
                list(external_ids),
            )

    def get_checklist_item_by_external(self, external_id: str) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM checklist_items WHERE source = 'icloud' AND external_id = ?",
                (external_id,),
            ).fetchone()
        return self._public(row) if row else None

    def list_notes(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM notes ORDER BY pinned DESC, sort_order, id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def create_note(self, title: str, body: str, color: str, pinned: bool) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM notes"
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO notes(title, body, color, pinned, sort_order, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, body, color, int(pinned), order, now, now),
            )
            row = connection.execute(
                "SELECT * FROM notes WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._public(row)

    def update_note(self, note_id: int, values: dict[str, Any]) -> Optional[dict[str, Any]]:
        fields = {
            key: value
            for key, value in values.items()
            if key in {"title", "body", "color", "pinned", "sort_order"}
        }
        if "pinned" in fields:
            fields["pinned"] = int(bool(fields["pinned"]))
        if not fields:
            return self.get_note(note_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE notes SET {assignments}, updated_at = ? WHERE id = ?",
                [*fields.values(), utc_now(), note_id],
            )
        return self.get_note(note_id)

    def get_note(self, note_id: int) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return self._public(row) if row else None

    def delete_note(self, note_id: int) -> bool:
        with self.connection() as connection:
            return connection.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0

    def list_checklist(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM checklist_items ORDER BY done, sort_order, id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def create_checklist_item(self, text: str, due_date: Optional[str]) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM checklist_items"
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO checklist_items(text, due_date, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (text, due_date, order, now, now),
            )
            row = connection.execute(
                "SELECT * FROM checklist_items WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._public(row)

    def update_checklist_item(
        self, item_id: int, values: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        fields = {
            key: value
            for key, value in values.items()
            if key in {"text", "done", "due_date", "sort_order"}
        }
        if "done" in fields:
            fields["done"] = int(bool(fields["done"]))
        if not fields:
            return self.get_checklist_item(item_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE checklist_items SET {assignments}, updated_at = ? WHERE id = ?",
                [*fields.values(), utc_now(), item_id],
            )
        return self.get_checklist_item(item_id)

    def get_checklist_item(self, item_id: int) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM checklist_items WHERE id = ?", (item_id,)
            ).fetchone()
        return self._public(row) if row else None

    def delete_checklist_item(self, item_id: int) -> bool:
        with self.connection() as connection:
            return (
                connection.execute("DELETE FROM checklist_items WHERE id = ?", (item_id,)).rowcount
                > 0
            )

    def list_event_reminders(self) -> list[dict[str, Any]]:
        """Alle huskelinjer, uanset om de er aktive lige nu."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM event_reminders ORDER BY enabled DESC, id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def create_event_reminder(
        self,
        source_kind: str,
        source_id: Optional[int],
        match_mode: str,
        match_value: str,
        text: str,
        lead_hours: int,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO event_reminders(source_kind, source_id, match_mode, match_value,"
                " text, lead_hours, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source_kind, source_id, match_mode, match_value, text, lead_hours, now, now),
            )
            row = connection.execute(
                "SELECT * FROM event_reminders WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._public(row)

    def get_event_reminder(self, item_id: int) -> Optional[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM event_reminders WHERE id = ?", (item_id,)
            ).fetchone()
        return self._public(row) if row else None

    def update_event_reminder(self, item_id: int, values: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not values:
            return self.get_event_reminder(item_id)
        sets = ", ".join(f"{key} = ?" for key in values)
        with self.connection() as connection:
            changed = connection.execute(
                f"UPDATE event_reminders SET {sets}, updated_at = ? WHERE id = ?",
                (*values.values(), utc_now(), item_id),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM event_reminders WHERE id = ?", (item_id,)
            ).fetchone()
        return self._public(row) if row and changed else None

    def delete_event_reminder(self, item_id: int) -> bool:
        with self.connection() as connection:
            return (
                connection.execute(
                    "DELETE FROM event_reminders WHERE id = ?", (item_id,)
                ).rowcount
                > 0
            )

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("enabled", "visible", "pinned", "done", "all_day", "popup_enabled"):
            if key in item:
                item[key] = bool(item[key])
        if item.get("source_id") is not None:
            item["source_id"] = int(item["source_id"])
        if "password" in item:
            item["has_password"] = bool(item["password"])
            item["password"] = ""
        return item
