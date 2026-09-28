from __future__ import annotations

import asyncio
import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from icalendar import Calendar, Todo

from .db import Database
from .reminders_bridge import BridgeError, BridgeRemindersClient

logger = logging.getLogger(__name__)

DAV_NS = "DAV:"
CAL_NS = "urn:ietf:params:xml:ns:caldav"
ICLOUD_CALDAV = "https://caldav.icloud.com/"

ICLOUD_USER_AGENT = (
    "DAVKit/4.0.1 (730); CalendarStore/4.0.1 (973); "
    "iCal/4.0.1 (1374); Mac OS X/10.6.2 (10C540)"
)

APPLE_ID_DOMAINS = ("icloud.com", "me.com", "mac.com")

SOURCE_CALDAV = "caldav"
SOURCE_BRIDGE = "bridge"
REMINDER_SOURCES = (SOURCE_CALDAV, SOURCE_BRIDGE)

_STATUS_OPEN = "NEEDS-ACTION"
_STATUS_DONE = "COMPLETED"


class CalDAVError(RuntimeError):
    pass


def _md5(value: str) -> str:
    import hashlib

    return hashlib.md5(value.encode("utf-8")).hexdigest()


def build_todo(summary: str, uid: str, done: bool = False, due: Optional[date] = None) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//Family Dashboard//EN")
    calendar.add("version", "2.0")
    todo = Todo()
    todo.add("uid", uid)
    todo.add("dtstamp", datetime.now(timezone.utc))
    todo.add("summary", summary)
    if due is not None:
        todo.add("due", due)
    todo.add("status", _STATUS_DONE if done else _STATUS_OPEN)
    if done:
        todo.add("completed", datetime.now(timezone.utc))
        todo.add("percent-complete", 100)
    else:
        todo.add("percent-complete", 0)
    calendar.add_component(todo)
    return calendar.to_ical()


def parse_todo(ics: bytes) -> dict[str, Any]:
    try:
        calendar = Calendar.from_ical(ics)
        todo = next((component for component in calendar.walk() if component.name == "VTODO"), None)
    except Exception as exc:  # noqa: BLE001
        raise CalDAVError(f"Invalid VTODO payload: {exc}") from exc
    if todo is None:
        raise CalDAVError("No VTODO component found")
    uid = str(todo.get("uid") or "")
    summary = str(todo.get("summary") or "").strip()
    status = str(todo.get("status") or "").upper()
    try:
        percent = todo.get("percent-complete")
        percent = int(percent) if percent is not None else 0
    except (TypeError, ValueError):
        percent = 0
    completed = todo.get("completed") is not None
    done = status == _STATUS_DONE or percent >= 100 or completed
    due_value = None
    try:
        decoded_due = todo.decoded("due")
    except (ValueError, KeyError):
        decoded_due = None
    if isinstance(decoded_due, datetime):
        due_value = decoded_due.date().isoformat()
    elif isinstance(decoded_due, date):
        due_value = decoded_due.isoformat()
    return {
        "uid": uid,
        "summary": summary,
        "done": bool(done),
        "due": due_value,
    }


def _escape(value: str) -> str:
    from xml.sax.saxutils import escape

    return escape(value)


def _qname(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


class CalDAVRemindersClient:
    """Minimal CalDAV client for iCloud Reminders (VTODO task lists)."""

    def __init__(
        self,
        username: str,
        app_password: str,
        base_url: str = ICLOUD_CALDAV,
        timeout: float = 15.0,
        extra_auth: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        self.base_url = base_url
        self.auth = (username, app_password)
        self.timeout = timeout
        self._credential_sets = [(username, app_password)] + list(extra_auth or [])
        self.resolved_username: Optional[str] = None
        self.resolved_password: Optional[str] = None
        self._home: Optional[str] = None

    def _credential_candidates(self) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for user, password in self._credential_sets:
            if not user or not password:
                continue
            if (user, password) in seen:
                continue
            seen.add((user, password))
            result.append((user, password))
            if "@" not in user:
                for domain in APPLE_ID_DOMAINS:
                    combo = (f"{user}@{domain}", password)
                    if combo not in seen:
                        seen.add(combo)
                        result.append(combo)
        return result

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        body: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        depth: Optional[str] = None,
        accept: Optional[str] = None,
    ) -> httpx.Response:
        merged = dict(headers or {})
        merged.setdefault("User-Agent", ICLOUD_USER_AGENT)
        if body is not None and "Content-Type" not in merged:
            merged["Content-Type"] = "application/xml; charset=UTF-8"
        if depth:
            merged["Depth"] = depth
        if accept:
            merged["Accept"] = accept
        try:
            response = await client.request(
                method, url, content=body, headers=merged, auth=self.auth, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise CalDAVError(f"CalDAV request failed: {exc}") from exc
        if response.status_code >= 400:
            logger.debug(
                "CalDAV %s %s -> %s; headers=%s; body=%r",
                method,
                url,
                response.status_code,
                dict(response.headers),
                (response.text or "")[:300],
            )
        if response.status_code == 401:
            raise CalDAVError(
                "Login blev afvist (401) mod caldav.icloud.com – tjek at Apple-id og "
                "app-specifikt password er korrekte (to-faktor-login skal være slået til "
                "på appleid.apple.com), og at passwordet kopieres præcist uden mellemrum."
            )
        if response.status_code == 400:
            raise CalDAVError(
                f"iCloud afviste CalDAV-anmodningen (HTTP 400) – {_apple_response_snippet(response)}"
            )
        if response.status_code in (403, 429):
            raise CalDAVError(
                f"iCloud afviste CalDAV-anmodningen (HTTP {response.status_code}) – "
                "prøv igen om et minut."
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise CalDAVError(f"CalDAV {method} {url} returned {response.status_code}")
        return response

    def _home_url(self) -> str:
        if self._home is None:
            raise CalDAVError("Calendar home not discovered")
        return urljoin(self.base_url, self._home)

    async def prepare(self, client: httpx.AsyncClient) -> None:
        candidates = self._credential_candidates()
        for index, (username, password) in enumerate(candidates):
            self.auth = (username, password)
            try:
                principal = await self._discover_principal(client)
                home = await self._discover_home(client, principal)
            except CalDAVError as exc:
                logger.debug("CalDAV prepare attempt %s (%s) failed: %s", index + 1, username, exc)
                if index < len(candidates) - 1 and _is_auth_rejection(exc):
                    continue
                raise
            self._home = home
            self.resolved_username = username
            self.resolved_password = password
            logger.info("CalDAV logged in as %s (home=%s)", username, home)
            return

    async def _discover_principal(self, client: httpx.AsyncClient) -> str:
        body = (
            f'<d:propfind xmlns:d="{DAV_NS}">'
            "<d:prop><d:current-user-principal/></d:prop>"
            "</d:propfind>"
        )
        response = await self._request(
            client, "PROPFIND", self.base_url, body, depth="0"
        )
        href_value = _find_property_href(response.content, "current-user-principal")
        if not href_value or href_value == "/":
            for candidate in _iter_hrefs(response.content):
                if "principal" in candidate.lower():
                    href_value = candidate
                    break
        logger.debug("CalDAV current-user-principal -> %r", href_value)
        if not href_value or href_value == "/":
            logger.debug("CalDAV principal-svar: %r", response.text[:800])
            raise CalDAVError("Could not discover iCloud CalDAV principal")
        return href_value

    def _home_from_response(self, content: bytes) -> Optional[str]:
        value = _find_property_href(content, "calendar-home-set", "calendarHomeSet")
        if value and value != "/":
            return value
        for candidate in _iter_hrefs(content):
            lowered = candidate.lower()
            if "/calendar" in lowered and ".ics" not in lowered and "/calendar-home" not in lowered:
                return candidate
        return None

    def _home_candidates(self, principal_href: str) -> list[str]:
        parts = urlsplit(principal_href)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else self.base_url
        candidates: list[str] = []
        segments = [segment for segment in parts.path.split("/") if segment]
        if segments:
            candidates.append(urljoin(f"{origin}/", f"{segments[0]}/calendars/"))
        candidates.append(urljoin(f"{origin}/", "calendars/"))
        unique: list[str] = []
        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)
        return unique

    async def _discover_home(self, client: httpx.AsyncClient, principal_href: str) -> str:
        body = (
            f'<d:propfind xmlns:d="{DAV_NS}" xmlns:c="{CAL_NS}">'
            "<d:prop><d:calendar-home-set/></d:prop>"
            "</d:propfind>"
        )
        diagnostic: list[str] = []
        for target in (urljoin(self.base_url, principal_href), self.base_url):
            response = await self._request(client, "PROPFIND", target, body, depth="0")
            href_value = self._home_from_response(response.content)
            if href_value:
                logger.debug("CalDAV calendar-home-set -> %r (fra %s)", href_value, target)
                return href_value
            hrefs = _iter_hrefs(response.content)
            diagnostic.append(f"{target} gav {hrefs[:4]}")
            logger.debug("Ingen calendar-home-set fra %s: %r", target, response.text[:600])
        for candidate in self._home_candidates(principal_href):
            try:
                probe = await self._request(
                    client, "PROPFIND", candidate, self._list_body(), depth="1"
                )
            except CalDAVError as exc:
                diagnostic.append(f"{candidate} -> {exc}")
                continue
            if _parse_calendar_home(probe.content):
                logger.debug("CalDAV calendar-home fundet via kandidat %r", candidate)
                return candidate
            diagnostic.append(f"{candidate} -> ingen lister")
        raise CalDAVError(
            "Could not discover iCloud calendar home – " + "; ".join(diagnostic)
        )

    def _list_body(self) -> str:
        return (
            f'<d:propfind xmlns:d="{DAV_NS}" xmlns:c="{CAL_NS}">'
            "<d:prop><d:displayname/><d:resourcetype/>"
            "<c:supported-calendar-component-set/></d:prop></d:propfind>"
        )

    async def list_task_lists(self, client: httpx.AsyncClient) -> list[dict[str, str]]:
        response = await self._request(
            client, "PROPFIND", self._home_url(), self._list_body(), depth="1"
        )
        return _parse_calendar_home(response.content)

    async def list_tasks(self, client: httpx.AsyncClient, list_href: str) -> list[dict[str, Any]]:
        body = (
            f'<c:calendar-query xmlns:d="{DAV_NS}" xmlns:c="{CAL_NS}">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            "<c:filter><c:comp-filter name=\"VCALENDAR\">"
            '<c:comp-filter name="VTODO"/></c:comp-filter></c:filter>'
            "</c:calendar-query>"
        )
        url = urljoin(self.base_url, list_href)
        response = await self._request(
            client,
            "REPORT",
            url,
            body,
            depth="1",
            accept="text/calendar, application/octet-stream",
        )
        results: list[dict[str, Any]] = []
        for href, payload in _find_calendar_data(response.content):
            try:
                todo = parse_todo(payload)
            except CalDAVError:
                continue
            if not todo["uid"]:
                continue
            results.append(
                {
                    "external_id": todo["uid"],
                    "href": urljoin(self.base_url, href),
                    "summary": todo["summary"],
                    "done": todo["done"],
                    "due": todo["due"],
                }
            )
        return results

    async def create_todo(
        self, client: httpx.AsyncClient, list_href: str, summary: str, due: Optional[date]
    ) -> str:
        uid = str(uuid.uuid4()).upper()
        payload = build_todo(summary, uid, done=False, due=due)
        collection = urljoin(self.base_url, list_href)
        url = urljoin(collection if collection.endswith("/") else collection + "/", f"{uid}.ics")
        await self._request(
            client,
            "PUT",
            url,
            payload.decode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8; component=VTODO"},
        )
        return uid

    async def update_todo(
        self,
        client: httpx.AsyncClient,
        item_href: str,
        summary: str,
        done: bool,
        due: Optional[date] = None,
    ) -> None:
        uid = _href_basename(item_href).removesuffix(".ics")
        payload = build_todo(summary, uid, done=done, due=due)
        await self._request(
            client,
            "PUT",
            item_href,
            payload.decode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8; component=VTODO"},
        )

    async def delete_todo(self, client: httpx.AsyncClient, item_href: str) -> None:
        await self._request(client, "DELETE", item_href)


def _href_basename(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


def _apple_response_snippet(response: httpx.Response) -> str:
    text = " ".join((response.text or "").split()).strip()
    if text:
        prefix = text[:180]
        return f"Apples svar: {prefix!r}"
    return "Apples svar var tomt."


def _is_auth_rejection(error: CalDAVError) -> bool:
    message = str(error)
    return "HTTP 401" in message or "Login blev afvist (401)" in message or "returned 401" in message


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _iter_hrefs(xml: bytes) -> list[str]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return [
        element.text.strip()
        for element in root.iter()
        if _local_name(element.tag) == "href" and element.text and element.text.strip()
    ]


def _find_property_href(xml: bytes, *names: str) -> str:
    """Find href'en inde i et DAV-property (matcher på lokalt navn, uafhængig af namespace)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    wanted = {name.lower() for name in names}
    for element in root.iter():
        if _local_name(element.tag) not in wanted:
            continue
        for child in element.iter():
            if _local_name(child.tag) == "href" and child.text and child.text.strip():
                return child.text.strip()
    return ""


def _find_text(xml: bytes, *path: str) -> str:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    for qname in path:
        element = root.find(f".//{qname}")
        if element is not None and element.text:
            return element.text
    return ""


def _parse_calendar_home(xml: bytes) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    response_qname = _qname(DAV_NS, "response")
    href_qname = _qname(DAV_NS, "href")
    displayname_qname = _qname(DAV_NS, "displayname")
    components_qname = _qname(CAL_NS, "supported-calendar-component-set")
    comp_qname = f"{_qname(CAL_NS, 'comp')}"
    result: list[dict[str, str]] = []
    for response in root.iter(response_qname):
        href = _direct_text(response, href_qname)
        if not href:
            continue
        components = response.find(f".//{components_qname}")
        supported = {
            (component.get("name") or "").upper()
            for component in (components.findall(comp_qname) if components is not None else [])
        }
        if not supported:
            supported.add("VEVENT")
        if "VTODO" not in supported:
            continue
        displayname = _direct_text(response, displayname_qname) or _href_basename(href)
        result.append({"href": href, "name": displayname})
    return result


def _direct_text(element: ET.Element, qname: str) -> str:
    for match in element.findall(f".//{qname}"):
        if match.text:
            return match.text
    return ""


def _find_calendar_data(xml: bytes) -> list[tuple[str, bytes]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    response_qname = _qname(DAV_NS, "response")
    href_qname = _qname(DAV_NS, "href")
    data_qname = _qname(CAL_NS, "calendar-data")
    results: list[tuple[str, bytes]] = []
    for response in root.iter(response_qname):
        href = _direct_text(response, href_qname)
        if not href:
            continue
        for data in response.findall(f".//{data_qname}"):
            if data.text:
                results.append((href, data.text.encode("utf-8")))
    return results


def _due_iso(value: Any) -> Optional[str]:
    """Providers may return a date, datetime or ISO string; the DB wants a string."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] or None


class RemindersProvider:
    """Common interface for the two ways the dashboard can reach Apple Reminders."""

    name = "?"

    def configured(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def credentials_configured(self) -> bool:
        """Backend login works, but no task list has been picked yet."""
        raise NotImplementedError  # pragma: no cover - interface

    async def discover_lists(self) -> list[dict[str, str]]:
        raise NotImplementedError  # pragma: no cover - interface

    async def list_tasks(self, list_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError  # pragma: no cover - interface

    async def create(self, list_id: str, text: str, due: Optional[date]) -> str:
        raise NotImplementedError  # pragma: no cover - interface

    async def update(
        self,
        list_id: str,
        item: dict[str, Any],
        text: str,
        done: bool,
        due: Optional[date],
    ) -> None:
        raise NotImplementedError  # pragma: no cover - interface

    async def delete(self, list_id: str, item: dict[str, Any]) -> None:
        raise NotImplementedError  # pragma: no cover - interface


class CalDAVRemindersProvider(RemindersProvider):
    """Talte til iCloud via CalDAV.

    Bemærk: iCloud Reminders over IMAP er en ANDEN database end den i
    Påmindelser-appen på Apple-enheder. CalDAV er den rigtige fjernadgang,
    men kræver en app-specifik adgangskode.
    """

    name = SOURCE_CALDAV

    def __init__(self, database: Database) -> None:
        self.database = database

    def _auth_candidates(self) -> list[dict[str, str]]:
        settings = self.database.get_settings()
        candidates: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for user_key, password_key in (
            ("reminders_username", "reminders_app_password"),
            ("notes_imap_username", "notes_imap_app_password"),
        ):
            user = (settings.get(user_key) or "").strip()
            password = settings.get(password_key) or ""
            if not user or not password or (user, password) in seen:
                continue
            seen.add((user, password))
            candidates.append({"user": user, "password": password, "source": user_key})
        return candidates

    def _client(self) -> CalDAVRemindersClient:
        candidates = self._auth_candidates()
        primary = candidates[0] if candidates else {"user": "", "password": ""}
        extras = [(item["user"], item["password"]) for item in candidates[1:]]
        return CalDAVRemindersClient(
            primary["user"],
            primary["password"],
            extra_auth=extras,
        )

    def _todo_url(self, list_id: str, external_id: str) -> str:
        collection = urljoin(self._client().base_url, list_id)
        if not collection.endswith("/"):
            collection += "/"
        return urljoin(collection, f"{external_id}.ics")

    def _remember_resolved(self, client: CalDAVRemindersClient) -> None:
        resolved_user = client.resolved_username
        resolved_password = client.resolved_password
        settings = self.database.get_settings()
        updates: dict[str, str] = {}
        if resolved_user and resolved_user != (settings.get("reminders_username") or ""):
            updates["reminders_username"] = resolved_user
        if resolved_password and resolved_password != (settings.get("reminders_app_password") or ""):
            updates["reminders_app_password"] = resolved_password
        if updates:
            self.database.update_settings(updates)

    def credentials_configured(self) -> bool:
        settings = self.database.get_settings()
        return bool(settings.get("reminders_username") and settings.get("reminders_app_password"))

    def configured(self) -> bool:
        return self.credentials_configured() and bool(
            self.database.get_settings().get("reminders_list_href")
        )

    async def discover_lists(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient() as client:
            dav = self._client()
            await dav.prepare(client)
            lists = await dav.list_task_lists(client)
            self._remember_resolved(dav)
            return lists

    async def list_tasks(self, list_id: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            dav = self._client()
            await dav.prepare(client)
            tasks = await dav.list_tasks(client, list_id)
            self._remember_resolved(dav)
            return tasks

    async def create(self, list_id: str, text: str, due: Optional[date]) -> str:
        async with httpx.AsyncClient() as client:
            dav = self._client()
            await dav.prepare(client)
            uid = await dav.create_todo(client, list_id, text, due)
            self._remember_resolved(dav)
            return uid

    async def update(
        self,
        list_id: str,
        item: dict[str, Any],
        text: str,
        done: bool,
        due: Optional[date],
    ) -> None:
        async with httpx.AsyncClient() as client:
            dav = self._client()
            await dav.prepare(client)
            await dav.update_todo(
                client,
                self._todo_url(list_id, item["external_id"]),
                text,
                done,
                due=due,
            )
            self._remember_resolved(dav)

    async def delete(self, list_id: str, item: dict[str, Any]) -> None:
        async with httpx.AsyncClient() as client:
            dav = self._client()
            await dav.prepare(client)
            await dav.delete_todo(client, self._todo_url(list_id, item["external_id"]))
            self._remember_resolved(dav)


class BridgeRemindersProvider(RemindersProvider):
    """Talte til en Mac mini med FamilyBridge (EventKit).

    EventKit læser den ÆGTE lokale Påmindelser-database – altså præcis de
    påmindelser, der vises i Påmindelser-appen på alle Apple-enheder.
    """

    name = SOURCE_BRIDGE

    def __init__(self, database: Database) -> None:
        self.database = database

    def client(self) -> BridgeRemindersClient:
        settings = self.database.get_settings()
        return BridgeRemindersClient(
            settings.get("reminders_bridge_url") or "",
            settings.get("reminders_bridge_token") or "",
        )

    def credentials_configured(self) -> bool:
        settings = self.database.get_settings()
        return bool(settings.get("reminders_bridge_url") and settings.get("reminders_bridge_token"))

    def configured(self) -> bool:
        return self.credentials_configured() and bool(
            self.database.get_settings().get("reminders_list_href")
        )

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            return await self.client().health(client)

    async def discover_lists(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient() as client:
            return await self.client().list_task_lists(client)

    async def list_tasks(self, list_id: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            return await self.client().list_tasks(client, list_id)

    async def create(self, list_id: str, text: str, due: Optional[date]) -> str:
        async with httpx.AsyncClient() as client:
            return await self.client().create_todo(client, list_id, text, due)

    async def update(
        self,
        list_id: str,
        item: dict[str, Any],
        text: str,
        done: bool,
        due: Optional[date],
    ) -> None:
        async with httpx.AsyncClient() as client:
            await self.client().update_todo(client, item["external_id"], text, done, due=due)

    async def delete(self, list_id: str, item: dict[str, Any]) -> None:
        async with httpx.AsyncClient() as client:
            await self.client().delete_todo(client, item["external_id"])


class RemindersSync:
    """Two-way bridge between the dashboard checklist and Apple Reminders.

    Two backends are supported:
      * caldav  – direct to iCloud (needs an app-specific password)
      * bridge – via a Mac mini running FamilyBridge (EventKit, same data as
                 the Reminders app on every Apple device)
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock: Optional[asyncio.Lock] = None

    def _lock_instance(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # --- backend selection -------------------------------------------------

    def source(self) -> str:
        value = (self.database.get_setting("reminders_source") or SOURCE_CALDAV).strip().lower()
        return value if value in REMINDER_SOURCES else SOURCE_CALDAV

    def provider(self) -> RemindersProvider:
        if self.source() == SOURCE_BRIDGE:
            return BridgeRemindersProvider(self.database)
        return CalDAVRemindersProvider(self.database)

    def _list_id(self) -> str:
        return self.database.get_setting("reminders_list_href") or ""

    # --- lifecycle ---------------------------------------------------------

    def configured(self) -> bool:
        return self.provider().configured()

    def enabled(self) -> bool:
        settings = self.database.get_settings()
        return settings.get("reminders_enabled", "false").lower() == "true" and self.configured()

    async def run_periodically(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if self.enabled():
                await self.sync()
            try:
                minutes = int(self.database.get_setting("reminders_sync_minutes", "5") or 5)
            except ValueError:
                minutes = 5
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(1, minutes) * 60)
            except asyncio.TimeoutError:
                pass

    # --- operations --------------------------------------------------------

    async def discover_lists(self) -> tuple[list[dict[str, str]], Optional[str]]:
        if not self.provider().credentials_configured():
            if self.source() == SOURCE_BRIDGE:
                return [], "Indtast Mac'ens adresse og token for Mac mini-bridge"
            return [], "Indtast brugernavn og app-adgangskode for iCloud påmindelser"
        try:
            return await self.provider().discover_lists(), None
        except (CalDAVError, BridgeError, httpx.HTTPError) as exc:
            logger.warning("Reminders discovery failed: %s", exc)
            return [], str(exc)

    async def test_connection(self) -> dict[str, Any]:
        """Validates the configured backend and reports what it can see."""
        provider = self.provider()
        if not provider.credentials_configured():
            message = "Påmindelser er ikke konfigureret endnu"
            if provider.name == SOURCE_BRIDGE:
                message = "Indtast Mac'ens adresse og token for Mac mini-bridge"
            return {"ok": False, "message": message}
        try:
            if provider.name == SOURCE_BRIDGE:
                health = await BridgeRemindersProvider(self.database).health()
                if not health.get("reminders_access"):
                    return {
                        "ok": False,
                        "message": "FamilyBridge svarer, men mangler adgang til Påmindelser. "
                        "Godkend under Systemindstillinger > Anonymitet og sikkerhed > Påmindelser.",
                    }
            lists = await provider.discover_lists()
        except (CalDAVError, BridgeError, httpx.HTTPError) as exc:
            return {"ok": False, "message": str(exc)}
        names = ", ".join(item.get("name", "") for item in lists[:6]) or "ingen lister"
        return {
            "ok": True,
            "message": f"Forbindelsen virker – {len(lists)} lister fundet: {names}",
        }

    async def sync(self) -> dict[str, Any]:
        if not self.enabled():
            return {"synced": False, "count": 0, "reason": "disabled"}
        provider = self.provider()
        list_id = self._list_id()
        async with self._lock_instance():
            try:
                tasks = await provider.list_tasks(list_id)
                for index, task in enumerate(tasks):
                    self.database.upsert_icloud_checklist_item(
                        external_id=task["external_id"],
                        text=task["summary"] or "Uden titel",
                        done=task["done"],
                        due_date=_due_iso(task.get("due")),
                        sort_order=index,
                    )
                self.database.remove_icloud_checklist_missing(
                    {task["external_id"] for task in tasks}
                )
                self.database.update_settings(
                    {
                        "reminders_last_sync": datetime.now(timezone.utc).isoformat(),
                        "reminders_last_error": "",
                    }
                )
                return {"synced": True, "count": len(tasks)}
            except (CalDAVError, BridgeError, httpx.HTTPError) as exc:
                logger.warning("Reminders sync failed: %s", exc)
                self.database.update_settings({"reminders_last_error": str(exc)[:500]})
                return {"synced": False, "count": 0, "error": str(exc)}

    async def create(self, text: str, due: Optional[date]) -> dict[str, Any]:
        provider = self.provider()
        list_id = self._list_id()
        async with self._lock_instance():
            external_id = await provider.create(list_id, text, due)
            return self.database.upsert_icloud_checklist_item(
                external_id=external_id,
                text=text,
                done=False,
                due_date=due.isoformat() if due else None,
                sort_order=0,
            )

    async def update(self, item: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        text = values.get("text", item["text"])
        done = bool(values.get("done", item["done"]))
        due = values.get("due_date", item.get("due_date"))
        due_date: Optional[date] = None
        if due:
            try:
                due_date = date.fromisoformat(due)
            except ValueError:
                due_date = None
        provider = self.provider()
        list_id = self._list_id()
        async with self._lock_instance():
            await provider.update(list_id, item, text, done, due_date)
        return self.database.upsert_icloud_checklist_item(
            external_id=item["external_id"],
            text=text,
            done=done,
            due_date=due_date.isoformat() if due_date else None,
            sort_order=int(item.get("sort_order") or 0),
        )

    async def delete(self, item: dict[str, Any]) -> None:
        provider = self.provider()
        list_id = self._list_id()
        async with self._lock_instance():
            await provider.delete(list_id, item)
        self.database.delete_checklist_item(item["id"])
