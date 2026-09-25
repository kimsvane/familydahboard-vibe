from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .config import Settings
from .db import Database
from .ics import expand_events

logger = logging.getLogger(__name__)


def timezone_is_valid(name: str) -> bool:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True


def get_timezone(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def date_range(start: date, end: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = get_timezone(timezone_name)
    return (
        datetime.combine(start, time.min, tzinfo=zone),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone),
    )


def _birthday_candidate(birth_date: date, year: int) -> date:
    try:
        return birth_date.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def upcoming_birthdays(
    birthdays: list[dict[str, Any]],
    reference: date,
    limit: int = 8,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for birthday in birthdays:
        try:
            birth_date = date.fromisoformat(str(birthday["birth_date"]))
        except ValueError:
            continue
        candidate = _birthday_candidate(birth_date, reference.year)
        if candidate < reference:
            candidate = _birthday_candidate(birth_date, reference.year + 1)
        days_until = (candidate - reference).days
        result.append(
            {
                **birthday,
                "next_occurrence": candidate.isoformat(),
                "days_until": days_until,
                "age": candidate.year - birth_date.year,
                "is_today": days_until == 0,
            }
        )
    result.sort(key=lambda item: (item["days_until"], item["name"].casefold()))
    return result[:limit]


def events_for_range(
    database: Database,
    start: date,
    end: date,
    timezone_name: str,
    kind: Optional[str] = None,
) -> list[dict[str, Any]]:
    if end < start:
        start, end = end, start
    sources = database.list_sources(enabled_only=True)
    if kind is not None:
        sources = [source for source in sources if source.get("kind", "calendar") == kind]
    enabled_ids = {int(source["id"]) for source in sources}
    raw_events = [
        event
        for event in database.list_events(list(enabled_ids))
        if event["source_id"] in enabled_ids
    ]
    range_start, range_end = date_range(start, end, timezone_name)
    return expand_events(raw_events, range_start, range_end, timezone_name)


class WeatherService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[tuple[float, float, str, str], tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def current(
        self,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        timezone_name: Optional[str] = None,
        location_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        latitude = self.settings.latitude if latitude is None else float(latitude)
        longitude = self.settings.longitude if longitude is None else float(longitude)
        timezone_name = self.settings.timezone if timezone_name is None else timezone_name
        location_name = self.settings.location_name if location_name is None else location_name
        key = (latitude, longitude, timezone_name, location_name)
        now = datetime.now(timezone.utc).timestamp()
        cached = self._cache.get(key)
        if cached and now - cached[0] < 600:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(key)
            now = datetime.now(timezone.utc).timestamp()
            if cached and now - cached[0] < 600:
                return cached[1]
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                    response = await client.get(
                        "https://api.open-meteo.com/v1/forecast",
                        params={
                            "latitude": latitude,
                            "longitude": longitude,
                            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                            "timezone": timezone_name,
                        },
                    )
                    response.raise_for_status()
                    current = response.json().get("current", {})
                    result = {
                        "temperature": current.get("temperature_2m"),
                        "apparent_temperature": current.get("apparent_temperature"),
                        "weather_code": current.get("weather_code"),
                        "wind_speed": current.get("wind_speed_10m"),
                        "location": location_name,
                    }
                    self._cache[key] = (now, result)
                    return result
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("Weather request failed: %s", exc)
                return cached[1] if cached else None
