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

# Felterne der hentes time for time. Rækkefølgen bruges ikke, men den
# skal passe til de arrays Open-Meteo svarer med, og de skal have samme
# længde. Mangler en, sættes den til None i stedet for at sprænge svaret.
HOURLY_FIELDS = (
    ("temperature_2m", "temperature"),
    ("apparent_temperature", "apparent_temperature"),
    ("precipitation_probability", "precipitation_probability"),
    ("weather_code", "weather_code"),
)


def parse_hourly(payload: dict[str, Any], timezone_name: Optional[str] = None, limit: int = 24) -> list[dict[str, Any]]:
    """Timebaseret vejr fra denne time og fremad.

    Open-Meteo svarer med parallelle arrays, én time ad gangen. Tidsstemplerne
    er i lokal tid, fordi vi beder om en tidszone, så de skal have den samme
    tidszone hæftet på før de kan sammenlignes med nu.
    """
    hourly = payload.get("hourly") or {}
    stamps = hourly.get("time") or []
    if not stamps or limit <= 0:
        return []
    zone = get_timezone(timezone_name) if timezone_name else None
    current_hour = datetime.now(zone).replace(minute=0, second=0, microsecond=0)
    rows: list[dict[str, Any]] = []
    for index, stamp in enumerate(stamps):
        try:
            moment = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            continue
        if zone is not None:
            moment = moment.replace(tzinfo=zone)
        if moment < current_hour:
            continue
        row: dict[str, Any] = {"time": str(stamp)}
        for source, target in HOURLY_FIELDS:
            values = hourly.get(source) or []
            row[target] = values[index] if index < len(values) else None
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


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


def _title_matches(title: str, mode: str, value: str) -> bool:
    """Sammenligner en begivenhedstitel med det, brugeren har skrevet.

    Nøjagtig er standard, fordi den er forudsigelig: en linje på "IDR"
    rammer præcis lektionen der hedder IDR og ikke en hvilken som helst
    titel hvor IDR bare optræder et sted. Indeholder er der til dem der
    har titler med en fast indpakning, som "Microracer i Østerbro".
    """
    haystack = " ".join(str(title or "").split()).casefold()
    needle = " ".join(str(value or "").split()).casefold()
    if not haystack or not needle:
        return False
    if mode == "contains":
        return needle in haystack
    return haystack == needle


def active_event_hints(
    rules: list[dict[str, Any]],
    events: list[dict[str, Any]],
    now: datetime,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Finder de huskelinjer der er aktive lige nu.

    En linje er aktiv fra lead_hours før aftalen begynder og indtil den
    er slut, så "husk idrætstøj" dukker op om aftenen før en tidlig
    idrætstime og forsvinder igen bagefter. Holdagser og aftaler der
    allerede er i gang tæller stadig, ellers forsvandt de præcis mens
    nogen skulle bruge dem.

    Hver regel bidrager højst én gang, selv hvis den rammer flere
    forekomster af det samme skemalelemne. To forskellige regler om den
    samme aftale vises begge, for det er to forskellige ting at huske.
    """
    active_rules = [rule for rule in rules if rule.get("enabled", True)]
    if not active_rules or not events:
        return []
    moment = now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    result: list[dict[str, Any]] = []
    for event in events:
        start = _parse_moment(event.get("start_at"))
        if start is None:
            continue
        end = _parse_moment(event.get("end_at")) or start
        for rule in active_rules:
            if str(rule.get("source_kind") or "calendar") != str(
                event.get("source_kind") or "calendar"
            ):
                continue
            # En linje kan være snævert til én kalender, eller gælde alle
            # af samme slags. source_id None betyder alle.
            rule_source = rule.get("source_id")
            if rule_source not in (None, "") and int(rule_source) != int(
                event.get("source_id") or 0
            ):
                continue
            if not _title_matches(
                event.get("title"),
                str(rule.get("match_mode") or "exact"),
                rule.get("match_value"),
            ):
                continue
            try:
                lead_hours = max(0, min(168, int(rule.get("lead_hours", 12))))
            except (TypeError, ValueError):
                lead_hours = 12
            if moment < start - timedelta(hours=lead_hours):
                continue
            if moment > end:
                continue
            result.append(
                {
                    "id": int(rule["id"]),
                    "text": str(rule["text"]),
                    "event_title": event.get("title", ""),
                    "event_id": event.get("id"),
                    "local_time": event.get("local_start_time") or "",
                    "all_day": bool(event.get("all_day")),
                    "source_name": event.get("source_name", ""),
                    "source_kind": event.get("source_kind", "calendar"),
                    "in_progress": bool(event.get("all_day")) or start <= moment <= end,
                }
            )
    # Én regel én huskelinje, uanset hvor mange forekomster den ramte.
    kun_en_pr_regel = {}
    for item in result:
        kun_en_pr_regel.setdefault(item["id"], item)
    result = list(kun_en_pr_regel.values())
    # Og samme tekst skal ikke stå to gange på væggen, selv om den
    # kommer fra to forskellige regler.
    unik_tekst = {}
    for item in result:
        unik_tekst.setdefault(item["text"], item)
    result = list(unik_tekst.values())
    result.sort(key=lambda item: (item["local_time"] == "", item["local_time"], item["text"]))
    return result[:limit]


def _parse_moment(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
                            # timebaseret vejr til forsiden. To dage, fordi
                            # tolv timer kan naa over i morgen.
                            "hourly": ",".join(source for source, _ in HOURLY_FIELDS),
                            "forecast_days": 2,
                            "timezone": timezone_name,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    current = payload.get("current", {})
                    result = {
                        "temperature": current.get("temperature_2m"),
                        "apparent_temperature": current.get("apparent_temperature"),
                        "weather_code": current.get("weather_code"),
                        "wind_speed": current.get("wind_speed_10m"),
                        "location": location_name,
                        "hourly": parse_hourly(payload, timezone_name),
                    }
                    self._cache[key] = (now, result)
                    return result
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("Weather request failed: %s", exc)
                return cached[1] if cached else None
