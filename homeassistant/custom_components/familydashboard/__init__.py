"""Family Dashboard Home Assistant integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

DOMAIN = "familydashboard"
SERVICE_REFRESH = "refresh"
API_TIMEOUT = 15
SCAN_INTERVAL = timedelta(minutes=5)
PLATFORMS: list[Platform] = [Platform.SENSOR]

SUMMARY_ENDPOINT = "/api/dashboard/summary"
EVENTS_ENDPOINT = "/api/events"
BIRTHDAYS_ENDPOINT = "/api/birthdays"

NEXT_EVENT = "next_event"
NEXT_BIRTHDAY = "next_birthday"

_EVENT_KEYS = ("events", "upcoming_events", "items", "results", "data")
_BIRTHDAY_KEYS = ("birthdays", "upcoming_birthdays", "items", "results", "data")
_EVENT_NAME_KEYS = ("title", "name", "summary", "label", "event")
_BIRTHDAY_NAME_KEYS = (
    "person",
    "name",
    "display_name",
    "title",
    "first_name",
    "label",
)
_EVENT_START_KEYS = (
    "start_datetime",
    "starts_at",
    "start_time",
    "scheduled_for",
    "start",
    "datetime",
    "date",
    "when",
)
_EVENT_END_KEYS = ("end_datetime", "ends_at", "end_time", "end", "until")
_BIRTHDAY_DATE_KEYS = (
    "next_date",
    "next_birthday_date",
    "date",
    "birthday_date",
    "birthday",
    "date_of_birth",
    "dob",
)


class FamilyDashboardApiError(Exception):
    """Error returned by the Family Dashboard API."""


class FamilyDashboardAuthError(FamilyDashboardApiError):
    """Authentication error returned by the Family Dashboard API."""


def normalize_dashboard_url(value: Any) -> str:
    """Validate and normalize the Family Dashboard base URL."""
    if not isinstance(value, str):
        raise ValueError("Dashboard URL must be text")

    value = value.strip()
    if not value:
        raise ValueError("Dashboard URL is required")

    if "://" not in value:
        value = f"http://{value}"

    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("Dashboard URL must use HTTP or HTTPS")
        if not parsed.hostname:
            raise ValueError("Dashboard URL must include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Credentials must not be included in the dashboard URL")
        if parsed.query or parsed.fragment:
            raise ValueError("Dashboard URL must not include a query or fragment")
        _ = parsed.port
    except ValueError as err:
        raise ValueError("Dashboard URL is invalid") from err

    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


async def _async_request_json(
    session: aiohttp.ClientSession,
    base_url: str,
    endpoint: str,
    token: str | None,
) -> Any:
    """Fetch and decode one Family Dashboard API endpoint."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with session.get(
        f"{base_url}{endpoint}", headers=headers, allow_redirects=False
    ) as response:
        if response.status in {401, 403}:
            raise FamilyDashboardAuthError("Authentication was rejected")
        if 300 <= response.status < 400:
            raise FamilyDashboardApiError("The API endpoint redirected unexpectedly")
        response.raise_for_status()
        try:
            payload = await response.json(content_type=None)
        except (TypeError, UnicodeError, ValueError) as err:
            raise FamilyDashboardApiError(
                f"{endpoint} did not return valid JSON"
            ) from err

    if not isinstance(payload, (dict, list)):
        raise FamilyDashboardApiError(f"{endpoint} returned an unsupported JSON value")
    return payload


async def async_fetch_payloads(
    hass: HomeAssistant, base_url: str, token: str | None
) -> tuple[Any, Any, Any]:
    """Fetch all Family Dashboard API payloads concurrently."""
    session = async_get_clientsession(hass)
    async with asyncio.timeout(API_TIMEOUT):
        return await asyncio.gather(
            _async_request_json(session, base_url, SUMMARY_ENDPOINT, token),
            _async_request_json(session, base_url, EVENTS_ENDPOINT, token),
            _async_request_json(session, base_url, BIRTHDAYS_ENDPOINT, token),
        )


def _coerce_record(value: Any) -> dict[str, Any] | None:
    """Convert a supported API value into a record."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, int, float, bool)):
        return {"value": value}
    return None


def _extract_explicit_record(
    payload: Any, keys: tuple[str, ...]
) -> dict[str, Any] | None:
    """Extract an explicitly selected next record from a payload."""
    if not isinstance(payload, dict):
        return None

    for key in keys:
        if (record := _coerce_record(payload.get(key))) is not None:
            return record

    for container in ("data", "result"):
        if isinstance(nested := payload.get(container), dict):
            if (record := _extract_explicit_record(nested, keys)) is not None:
                return record
    return None


def _extract_records(
    payload: Any, keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Extract a list of records from common API response shapes."""
    values: Any
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        values = None
        for key in (*keys, "items", "results", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                values = candidate
                break
        if values is None:
            values = [payload]
    else:
        return []

    records: list[dict[str, Any]] = []
    for value in values:
        if (record := _coerce_record(value)) is not None:
            records.append(record)
    return records


def _safe_date(year: int, month: int, day: int) -> date:
    """Return a valid date, using February 28 for leap-day anniversaries."""
    try:
        return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return date(year, 2, 28)
        raise


def _next_anniversary(
    value: datetime, now: datetime, time_zone: ZoneInfo
) -> datetime | None:
    """Return the next annual occurrence of a birthday."""
    for year in (now.year, now.year + 1):
        birthday = _safe_date(year, value.month, value.day)
        candidate = datetime.combine(birthday, time.min, tzinfo=time_zone)
        if candidate >= now:
            return candidate
    return None


def _parse_datetime_value(
    value: Any, now: datetime, time_zone: ZoneInfo
) -> datetime | None:
    """Parse an API date, timestamp, or recurring month-day value."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) > 100_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(
                time_zone
            )
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return _parse_datetime_value(int(value), now, time_zone)

    try:
        if parsed := dt_util.parse_datetime(value):
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=time_zone)
            return parsed.astimezone(time_zone)
    except ValueError:
        pass

    recurring = value.removeprefix("--")
    separator = "/" if "/" in recurring else "-"
    parts = recurring.split(separator)
    try:
        if separator == "-" and len(parts) == 3:
            parsed_date = date.fromisoformat(value)
        elif len(parts) == 2:
            month = int(parts[0])
            day = int(parts[1])
            parsed_date = _safe_date(now.year, month, day)
        else:
            return None
    except ValueError:
        return None

    parsed_datetime = datetime.combine(parsed_date, time.min, tzinfo=time_zone)
    if len(parts) == 2:
        return _next_anniversary(parsed_datetime, now, time_zone)
    return parsed_datetime


def _record_datetime(
    record: dict[str, Any],
    kind: str,
    now: datetime,
    time_zone: ZoneInfo,
) -> datetime | None:
    """Return the next date associated with an event or birthday record."""
    keys = _BIRTHDAY_DATE_KEYS if kind == NEXT_BIRTHDAY else _EVENT_START_KEYS
    for key in keys:
        if (parsed := _parse_datetime_value(record.get(key), now, time_zone)) is None:
            continue
        if kind == NEXT_BIRTHDAY and parsed < now:
            return _next_anniversary(parsed, now, time_zone)
        return parsed
    return None


def _record_name(record: dict[str, Any], kind: str) -> str | None:
    """Return the display name for an event or birthday record."""
    keys = _BIRTHDAY_NAME_KEYS if kind == NEXT_BIRTHDAY else _EVENT_NAME_KEYS
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)

    if kind == NEXT_BIRTHDAY:
        first_name = record.get("first_name")
        last_name = record.get("last_name")
        if isinstance(first_name, str) or isinstance(last_name, str):
            full_name = " ".join(
                part.strip()
                for part in (str(first_name or ""), str(last_name or ""))
                if part.strip()
            )
            if full_name:
                return full_name

    value = record.get("value")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _next_record(
    records: list[dict[str, Any]], kind: str, hass: HomeAssistant
) -> dict[str, Any] | None:
    """Select the earliest current or future record."""
    if not records:
        return None

    time_zone = ZoneInfo(hass.config.time_zone)
    now = dt_util.now().astimezone(time_zone)
    dated: list[tuple[datetime, dict[str, Any]]] = []
    undated: list[dict[str, Any]] = []

    for record in records:
        start = _record_datetime(record, kind, now, time_zone)
        if start is None:
            undated.append(record)
            continue
        if kind == NEXT_BIRTHDAY:
            if start < now:
                continue
        else:
            end = None
            for key in _EVENT_END_KEYS:
                if (
                    end := _parse_datetime_value(record.get(key), now, time_zone)
                ) is not None:
                    break
            is_current = bool(end and end >= now) or start >= now
            is_all_day = start.date() >= now.date()
            if not is_current and not is_all_day:
                continue
        dated.append((start, record))

    if dated:
        return min(dated, key=lambda item: item[0])[1]
    return undated[0] if undated else None


def _select_next(
    summary: Any,
    endpoint_payload: Any,
    kind: str,
    record_keys: tuple[str, ...],
    explicit_keys: tuple[str, ...],
    hass: HomeAssistant,
) -> dict[str, Any] | None:
    """Select an explicit next item or derive it from endpoint records."""
    if (record := _extract_explicit_record(summary, explicit_keys)) is not None:
        return record
    if (
        record := _extract_explicit_record(endpoint_payload, explicit_keys)
    ) is not None:
        return record
    return _next_record(_extract_records(endpoint_payload, record_keys), kind, hass)


class FamilyDashboardCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate Family Dashboard API updates."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, session: aiohttp.ClientSession
    ) -> None:
        """Initialize the Family Dashboard coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Family Dashboard",
            update_interval=SCAN_INTERVAL,
            always_update=False,
        )
        self.session = session
        self.base_url = entry.data[CONF_URL]
        self.token = entry.data.get(CONF_TOKEN)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and normalize current Family Dashboard data."""
        try:
            async with asyncio.timeout(API_TIMEOUT):
                summary, events, birthdays = await asyncio.gather(
                    _async_request_json(
                        self.session, self.base_url, SUMMARY_ENDPOINT, self.token
                    ),
                    _async_request_json(
                        self.session, self.base_url, EVENTS_ENDPOINT, self.token
                    ),
                    _async_request_json(
                        self.session, self.base_url, BIRTHDAYS_ENDPOINT, self.token
                    ),
                )
        except FamilyDashboardAuthError as err:
            raise ConfigEntryAuthFailed(
                "Family Dashboard authentication failed"
            ) from err
        except (TimeoutError, aiohttp.ClientError, FamilyDashboardApiError) as err:
            raise UpdateFailed(f"Unable to refresh Family Dashboard: {err}") from err

        return {
            "summary": summary,
            "events": events,
            "birthdays": birthdays,
            NEXT_EVENT: _select_next(
                summary,
                events,
                NEXT_EVENT,
                _EVENT_KEYS,
                ("next_event", "nextEvent", "next-event", "event"),
                self.hass,
            ),
            NEXT_BIRTHDAY: _select_next(
                summary,
                birthdays,
                NEXT_BIRTHDAY,
                _BIRTHDAY_KEYS,
                ("next_birthday", "nextBirthday", "next-birthday", "birthday"),
                self.hass,
            ),
        }


def _async_register_refresh_service(hass: HomeAssistant) -> None:
    """Register the integration-wide refresh service once."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def _async_handle_refresh(_call: ServiceCall) -> None:
        coordinators = [
            entry.runtime_data
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
            and isinstance(
                getattr(entry, "runtime_data", None), FamilyDashboardCoordinator
            )
        ]
        if coordinators:
            await asyncio.gather(
                *(
                    coordinator.async_request_refresh()
                    for coordinator in coordinators
                )
            )

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _async_handle_refresh)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Family Dashboard from a config entry."""
    session = async_get_clientsession(hass)
    coordinator = FamilyDashboardCoordinator(hass, entry, session)
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_refresh_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Family Dashboard config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and not any(
        other_entry.entry_id != entry.entry_id
        and other_entry.state is ConfigEntryState.LOADED
        for other_entry in hass.config_entries.async_entries(DOMAIN)
    ):
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
    return unload_ok
