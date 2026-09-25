from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings
from .db import Database, utc_now
from .ics import CalendarParseError, parse_ics

logger = logging.getLogger(__name__)
MAX_CALENDAR_BYTES = 5 * 1024 * 1024
MAX_CALENDAR_REDIRECTS = 5


def normalize_feed_url(url: str) -> str:
    candidate = url.strip()
    if candidate.lower().startswith("webcal://"):
        return "https://" + candidate[len("webcal://") :]
    return candidate


async def _validated_feed_url(url: str, allow_private: bool) -> str:
    normalized = normalize_feed_url(url)
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CalendarParseError("Calendar feed URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise CalendarParseError("Calendar feed URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise CalendarParseError("Calendar feed URL must not contain credentials")
    if not allow_private:
        try:
            literal_address = ipaddress.ip_address(hostname.split("%", 1)[0])
        except ValueError:
            literal_address = None
        if literal_address is not None:
            addresses = [literal_address]
        else:
            try:
                address_info = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(
                        hostname, port or 443, type=socket.SOCK_STREAM
                    ),
                    timeout=5.0,
                )
            except (asyncio.TimeoutError, socket.gaierror, UnicodeError) as exc:
                raise CalendarParseError("Calendar feed host could not be resolved") from exc
            if not address_info:
                raise CalendarParseError("Calendar feed host could not be resolved")
            addresses = [ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in address_info]
        if any(not address.is_global for address in addresses):
            raise CalendarParseError("Private calendar feed addresses are not allowed")
    return normalized


async def _download_feed(client: httpx.AsyncClient, url: str, allow_private: bool) -> bytes:
    current_url = url
    for redirect_count in range(MAX_CALENDAR_REDIRECTS + 1):
        current_url = await _validated_feed_url(current_url, allow_private)
        async with client.stream("GET", current_url) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise CalendarParseError("Calendar feed redirect is missing a target")
                if redirect_count >= MAX_CALENDAR_REDIRECTS:
                    raise CalendarParseError("Calendar feed redirected too many times")
                current_url = urljoin(current_url, location)
                continue
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_CALENDAR_BYTES:
                raise CalendarParseError("Calendar feed exceeds the 5 MB limit")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_CALENDAR_BYTES:
                    raise CalendarParseError("Calendar feed exceeds the 5 MB limit")
            return bytes(content)
    raise CalendarParseError("Calendar feed redirected too many times")


class CalendarSynchronizer:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    async def sync_all(self) -> dict[str, object]:
        sources = self.database.list_sources(enabled_only=True)
        timezone_name = self.database.get_setting("timezone", self.settings.timezone)
        results: list[dict[str, object]] = []
        for source in sources:
            results.append(await self.sync_source(source, timezone_name))
        return {
            "synced": len(results),
            "failed": sum(1 for item in results if item["ok"] is False),
            "sources": results,
        }

    async def sync_source(
        self, source: dict[str, object], timezone_name: Optional[str] = None
    ) -> dict[str, object]:
        source_id = int(source["id"])
        timezone_name = timezone_name or self.database.get_setting(
            "timezone", self.settings.timezone
        )
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(20.0, connect=10.0),
                headers={"User-Agent": "FamilyDashboard/0.1"},
            ) as client:
                payload = await _download_feed(
                    client, str(source["url"]), self.settings.allow_private_calendars
                )
                events = parse_ics(payload, source_id, timezone_name)
            self.database.replace_source_events(source_id, events)
            self.database.set_source_sync(source_id, utc_now(), None)
            return {"id": source_id, "name": source["name"], "ok": True, "events": len(events)}
        except (httpx.HTTPError, CalendarParseError, ValueError) as exc:
            message = str(exc)[:500]
            self.database.set_source_sync(source_id, None, message)
            logger.warning(
                "Calendar sync failed for %s: %s", source.get("name", source_id), message
            )
            return {"id": source_id, "name": source["name"], "ok": False, "error": message}
        except Exception as exc:
            message = str(exc)[:500]
            self.database.set_source_sync(source_id, None, message)
            logger.exception(
                "Unexpected calendar sync failure for %s", source.get("name", source_id)
            )
            return {"id": source_id, "name": source["name"], "ok": False, "error": message}

    async def run_periodically(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.sync_all()
            except Exception:
                logger.exception("Calendar background sync failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.settings.sync_interval)
            except asyncio.TimeoutError:
                pass
