from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from icalendar import Calendar

MAX_OCCURRENCES_PER_EVENT = 5000
ALLOWED_RECURRENCE_FREQUENCIES = {"HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}


class CalendarParseError(ValueError):
    pass


def _timezone(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _text(component: Any, key: str, default: str = "") -> str:
    value = component.get(key)
    if value is None:
        return default
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value)


def _property_value(component: Any, key: str) -> Any:
    value = component.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _property_timezone(component: Any, key: str) -> str:
    value = _property_value(component, key)
    timezone_name = getattr(value, "params", {}).get("TZID")
    return str(timezone_name) if timezone_name else ""


def _as_utc(value: Any, timezone_name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
        if result.tzinfo is None:
            result = result.replace(tzinfo=_timezone(timezone_name))
        return result.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=_timezone(timezone_name)).astimezone(
            timezone.utc
        )
    raise CalendarParseError(f"Unsupported calendar date value: {value!r}")


def _date_list(component: Any, key: str, timezone_name: str) -> list[str]:
    raw = component.get(key)
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    result: list[str] = []
    for item in values:
        candidates = getattr(item, "dts", None)
        if candidates is not None:
            for candidate in candidates:
                value = getattr(candidate, "dt", candidate)
                result.append(_as_utc(value, timezone_name).isoformat())
        else:
            value = getattr(item, "dt", item)
            if isinstance(value, datetime):
                result.append(_as_utc(value, timezone_name).isoformat())
            elif isinstance(value, date):
                result.append(value.isoformat())
    return result


def _stable_id(source_id: int, uid: str) -> str:
    return hashlib.sha256(f"{source_id}:{uid}".encode()).hexdigest()[:32]


def parse_ics(
    payload: bytes | str, source_id: int, timezone_name: str = "Europe/Copenhagen"
) -> list[dict[str, Any]]:
    try:
        calendar = Calendar.from_ical(payload)
    except Exception as exc:
        raise CalendarParseError("Calendar feed could not be parsed") from exc
    events: list[dict[str, Any]] = []
    seen_uids: set[str] = set()
    for component in calendar.walk("VEVENT"):
        if str(component.get("STATUS", "CONFIRMED")).upper() == "CANCELLED":
            continue
        start_property = _property_value(component, "DTSTART")
        if start_property is None:
            continue
        start_value = getattr(start_property, "dt", start_property)
        if not isinstance(start_value, (date, datetime)):
            continue
        all_day = isinstance(start_value, date) and not isinstance(start_value, datetime)
        start_timezone = _property_timezone(component, "DTSTART")
        if all_day:
            start_at = start_value.isoformat()
            recurrence_timezone = None
        elif start_value.tzinfo is None:
            start_at = start_value.isoformat()
            recurrence_timezone = start_timezone or timezone_name
        elif start_timezone:
            start_at = start_value.replace(tzinfo=None).isoformat()
            recurrence_timezone = start_timezone
        else:
            start_at = _as_utc(start_value, timezone_name).isoformat()
            recurrence_timezone = None
        end_property = _property_value(component, "DTEND")
        end_value = getattr(end_property, "dt", end_property) if end_property is not None else None
        end_timezone = _property_timezone(component, "DTEND")
        if end_value is None:
            if all_day:
                end_at = (start_value + timedelta(days=1)).isoformat()
            elif recurrence_timezone:
                end_at = (start_value.replace(tzinfo=None) + timedelta(hours=1)).isoformat()
            else:
                end_at = (_as_utc(start_value, "UTC") + timedelta(hours=1)).isoformat()
        elif isinstance(end_value, date) and not isinstance(end_value, datetime):
            end_at = end_value.isoformat()
        elif end_timezone and recurrence_timezone and end_timezone == recurrence_timezone:
            end_at = end_value.replace(tzinfo=None).isoformat()
        elif end_timezone and end_timezone != recurrence_timezone:
            end_at = end_value.astimezone(timezone.utc).isoformat()
        elif end_value.tzinfo is None and recurrence_timezone:
            end_at = end_value.isoformat()
        else:
            end_at = _as_utc(end_value, "UTC").isoformat()
        uid = (
            _text(component, "UID")
            or hashlib.sha256(f"{start_at}:{_text(component, 'SUMMARY')}".encode()).hexdigest()
        )
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        rrule_property = component.get("RRULE")
        rrule = ""
        if rrule_property is not None:
            if isinstance(rrule_property, list):
                rrule_property = rrule_property[0]
            rrule = rrule_property.to_ical().decode("utf-8")
        if rrule:
            frequency = next(
                (
                    part.removeprefix("FREQ=").upper()
                    for part in rrule.split(";")
                    if part.upper().startswith("FREQ=")
                ),
                "",
            )
            if frequency not in ALLOWED_RECURRENCE_FREQUENCIES:
                raise CalendarParseError("Calendar recurrence frequency is not supported")
        rdates = _date_list(component, "RDATE", timezone_name)
        exdates = _date_list(component, "EXDATE", timezone_name)
        events.append(
            {
                "id": _stable_id(source_id, uid),
                "uid": uid,
                "title": _text(component, "SUMMARY", "Uden titel"),
                "description": _text(component, "DESCRIPTION"),
                "location": _text(component, "LOCATION"),
                "start_at": start_at,
                "end_at": end_at,
                "all_day": all_day,
                "recurrence_timezone": recurrence_timezone,
                "rrule": rrule or None,
                "rdates": rdates,
                "exdates": exdates,
                "url": _text(component, "URL") or None,
                "status": _text(component, "STATUS", "CONFIRMED").upper(),
            }
        )
    return events


def _parse_value(value: Any, timezone_name: str) -> Any:
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_timezone(timezone_name))
        return parsed
    return value


def _event_start_end(event: dict[str, Any], timezone_name: str) -> tuple[Any, Any, bool]:
    if event.get("all_day"):
        start_date = date.fromisoformat(str(event["start_at"])[:10])
        end_date = date.fromisoformat(str(event.get("end_at") or start_date)[:10])
        if end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        zone = _timezone(timezone_name)
        return (
            datetime.combine(start_date, time.min, tzinfo=zone),
            datetime.combine(end_date, time.min, tzinfo=zone),
            True,
        )
    event_timezone = str(event.get("recurrence_timezone") or timezone_name)
    start = _parse_value(event["start_at"], event_timezone)
    end = _parse_value(event.get("end_at") or event["start_at"], event_timezone)
    if end <= start:
        end = start + timedelta(hours=1)
    return start, end, False


def _excluded(value: str, exdates: set[str]) -> bool:
    if value in exdates:
        return True
    try:
        parsed = _parse_value(value, "UTC")
        return any(_parse_value(item, "UTC") == parsed for item in exdates)
    except (TypeError, ValueError):
        return False


def expand_event(
    event: dict[str, Any],
    range_start: datetime,
    range_end: datetime,
    timezone_name: str = "Europe/Copenhagen",
) -> list[dict[str, Any]]:
    event_timezone = str(event.get("recurrence_timezone") or timezone_name)
    base_start, base_end, all_day = _event_start_end(event, timezone_name)
    duration = base_end - base_start
    exdates = set(event.get("exdates") or [])
    starts: list[datetime] = []
    rule_value = event.get("rrule")
    if rule_value:
        try:
            rule = rrulestr(f"RRULE:{rule_value}", dtstart=base_start)
            for occurrence in rule.xafter(range_start - duration, inc=True):
                if occurrence >= range_end:
                    break
                starts.append(occurrence)
                if len(starts) >= MAX_OCCURRENCES_PER_EVENT:
                    break
        except (ValueError, TypeError, OverflowError):
            starts.append(base_start)
    for value in event.get("rdates") or []:
        if len(starts) >= MAX_OCCURRENCES_PER_EVENT:
            break
        try:
            starts.append(_parse_value(value, event_timezone))
        except (TypeError, ValueError):
            continue
    if not rule_value and not event.get("rdates"):
        starts.append(base_start)
    seen: set[str] = set()
    occurrences: list[dict[str, Any]] = []
    for start in starts:
        end = start + duration
        key = start.isoformat()
        if key in seen or _excluded(key, exdates):
            continue
        seen.add(key)
        if all_day:
            occurrence_start_date = start.date()
            occurrence_end_date = end.date()
            if occurrence_end_date <= occurrence_start_date:
                occurrence_end_date = occurrence_start_date + timedelta(days=1)
            if (
                occurrence_end_date <= range_start.date()
                or occurrence_start_date >= range_end.date()
            ):
                continue
            occurrence_start = occurrence_start_date.isoformat()
            occurrence_end = occurrence_end_date.isoformat()
            local_date = occurrence_start_date
            local_start_time = None
            local_end_time = None
        else:
            start_utc = start.astimezone(timezone.utc)
            end_utc = end.astimezone(timezone.utc)
            if end_utc <= range_start.astimezone(timezone.utc) or start_utc >= range_end.astimezone(
                timezone.utc
            ):
                continue
            occurrence_start = start_utc.isoformat()
            occurrence_end = end_utc.isoformat()
            local_start = start.astimezone(_timezone(timezone_name))
            local_end = end.astimezone(_timezone(timezone_name))
            local_date = local_start.date().isoformat()
            local_start_time = local_start.strftime("%H:%M")
            local_end_time = local_end.strftime("%H:%M")
        occurrences.append(
            {
                "id": event["id"],
                "uid": event["uid"],
                "title": event.get("title", "Uden titel"),
                "description": event.get("description", ""),
                "location": event.get("location", ""),
                "start_at": occurrence_start,
                "end_at": occurrence_end,
                "local_date": local_date,
                "local_start_time": local_start_time,
                "local_end_time": local_end_time,
                "all_day": all_day,
                "source_id": event.get("source_id"),
                "source_name": event.get("source_name", "Kalender"),
                "source_color": event.get("source_color", "#5c7cfa"),
                "source_kind": event.get("source_kind", "calendar"),
                "url": event.get("url"),
            }
        )
    return occurrences


def expand_events(
    events: list[dict[str, Any]],
    range_start: datetime,
    range_end: datetime,
    timezone_name: str = "Europe/Copenhagen",
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        result.extend(expand_event(event, range_start, range_end, timezone_name))
    result.sort(key=lambda item: (item["all_day"], item["start_at"], item["title"]))
    return result


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
