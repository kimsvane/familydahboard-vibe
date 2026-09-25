import asyncio

import httpx
import pytest

from app.ics import CalendarParseError
from app.sync import MAX_CALENDAR_BYTES, _download_feed, _validated_feed_url


def test_private_calendar_hosts_are_blocked_by_default():
    with pytest.raises(CalendarParseError, match="Private"):
        asyncio.run(_validated_feed_url("http://127.0.0.1/calendar.ics", False))


def test_private_calendar_hosts_can_be_enabled_explicitly():
    url = asyncio.run(_validated_feed_url("webcal://calendar.internal/feed.ics", True))
    assert url == "https://calendar.internal/feed.ics"


def test_redirect_to_private_calendar_host_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/feed.ics"})
        return httpx.Response(500)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CalendarParseError, match="Private"):
                await _download_feed(client, "https://93.184.216.34/feed.ics", False)

    asyncio.run(run())


def test_calendar_feed_size_is_capped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_CALENDAR_BYTES + 1))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CalendarParseError, match="5 MB"):
                await _download_feed(client, "https://93.184.216.34/feed.ics", False)

    asyncio.run(run())
