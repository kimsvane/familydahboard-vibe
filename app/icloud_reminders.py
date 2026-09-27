from __future__ import annotations

import asyncio
import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from icalendar import Calendar, Todo

from .db import Database

logger = logging.getLogger(__name__)

DAV_NS = "DAV:"
CAL_NS = "urn:ietf:params:xml:ns:caldav"
ICLOUD_CALDAV = "https://caldav.icloud.com/"

ICLOUD_USER_AGENT = (
    "DAVKit/4.0.1 (730); CalendarStore/4.0.1 (973); "
    "iCal/4.0.1 (1374); Mac OS X/10.6.2 (10C540)"
)

APPLE_ID_DOMAINS = ("icloud.com", "me.com", "mac.com")

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
                if index < len(candidates) - 1 and _is_auth_rejection(exc):
                    continue
                raise
            self._home = home
            self.resolved_username = username
            self.resolved_password = password
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
        href_value = _find_text(response.content, _qname(DAV_NS, "current-user-principal"), _qname(DAV_NS, "href"))
        if not href_value:
            raise CalDAVError("Could not discover iCloud CalDAV principal")
        return href_value

    async def _discover_home(self, client: httpx.AsyncClient, principal_href: str) -> str:
        body = (
            f'<d:propfind xmlns:d="{DAV_NS}" xmlns:c="{CAL_NS}">'
            "<d:prop><d:calendar-home-set/></d:prop>"
            "</d:propfind>"
        )
        url = urljoin(self.base_url, principal_href)
        response = await self._request(client, "PROPFIND", url, body, depth="0")
        href_value = _find_text(response.content, _qname(DAV_NS, "calendar-home-set"), _qname(DAV_NS, "href"))
        if not href_value:
            raise CalDAVError("Could not discover iCloud calendar home")
        return href_value

    async def list_task_lists(self, client: httpx.AsyncClient) -> list[dict[str, str]]:
        body = (
            f'<d:propfind xmlns:d="{DAV_NS}" xmlns:c="{CAL_NS}">'
            "<d:prop><d:displayname/><d:resourcetype/>"
            "<c:supported-calendar-component-set/></d:prop></d:propfind>"
        )
        response = await self._request(
            client, "PROPFIND", self._home_url(), body, depth="1"
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


class RemindersSync:
    """Two-way bridge between the dashboard checklist and an iCloud Reminders list."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock: Optional[asyncio.Lock] = None

    def _lock_instance(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def configured(self) -> bool:
        settings = self.database.get_settings()
        return bool(
            settings.get("reminders_username")
            and settings.get("reminders_app_password")
            and settings.get("reminders_list_href")
        )

    def enabled(self) -> bool:
        settings = self.database.get_settings()
        return settings.get("reminders_enabled", "false").lower() == "true" and self.configured()

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

    def _list_href(self) -> str:
        return self.database.get_setting("reminders_list_href") or ""

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

    def _todo_url(self, uid: str) -> str:
        collection = urljoin(self._client().base_url, self._list_href())
        if not collection.endswith("/"):
            collection += "/"
        return urljoin(collection, f"{uid}.ics")

    async def discover_lists(self) -> tuple[list[dict[str, str]], Optional[str]]:
        try:
            async with httpx.AsyncClient() as client:
                dav = self._client()
                await dav.prepare(client)
                lists = await dav.list_task_lists(client)
                self._remember_resolved(dav)
                return lists, None
        except (CalDAVError, httpx.HTTPError) as exc:
            logger.warning("Reminders discovery failed: %s", exc)
            return [], str(exc)

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

    async def sync(self) -> dict[str, Any]:
        if not self.enabled():
            return {"synced": False, "count": 0, "reason": "disabled"}
        async with self._lock_instance():
            try:
                async with httpx.AsyncClient() as client:
                    dav = self._client()
                    await dav.prepare(client)
                    tasks = await dav.list_tasks(client, self._list_href())
                for index, task in enumerate(tasks):
                    self.database.upsert_icloud_checklist_item(
                        external_id=task["external_id"],
                        text=task["summary"] or "Uden titel",
                        done=task["done"],
                        due_date=task["due"],
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
                self._remember_resolved(dav)
                return {"synced": True, "count": len(tasks)}
            except (CalDAVError, httpx.HTTPError) as exc:
                logger.warning("Reminders sync failed: %s", exc)
                self.database.update_settings(
                    {"reminders_last_error": str(exc)[:500]}
                )
                return {"synced": False, "count": 0, "error": str(exc)}

    async def create(self, text: str, due: Optional[date]) -> dict[str, Any]:
        async with self._lock_instance():
            async with httpx.AsyncClient() as client:
                dav = self._client()
                await dav.prepare(client)
                uid = await dav.create_todo(client, self._list_href(), text, due)
                self._remember_resolved(dav)
            return self.database.upsert_icloud_checklist_item(
                external_id=uid,
                text=text,
                done=False,
                due_date=due.isoformat() if due else None,
                sort_order=0,
            )

    async def update(self, item: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        text = values.get("text", item["text"])
        done = bool(values.get("done", item["done"]))
        due = values.get("due_date", item.get("due_date"))
        due_date = None
        if due:
            try:
                due_date = date.fromisoformat(due)
            except ValueError:
                due_date = None
        async with self._lock_instance():
            async with httpx.AsyncClient() as client:
                dav = self._client()
                await dav.prepare(client)
                await dav.update_todo(
                    client,
                    self._todo_url(item["external_id"]),
                    text,
                    done,
                    due=due_date,
                )
                self._remember_resolved(dav)
        return self.database.upsert_icloud_checklist_item(
            external_id=item["external_id"],
            text=text,
            done=done,
            due_date=due_date.isoformat() if due_date else None,
            sort_order=int(item.get("sort_order") or 0),
        )

    async def delete(self, item: dict[str, Any]) -> None:
        async with self._lock_instance():
            async with httpx.AsyncClient() as client:
                dav = self._client()
                await dav.prepare(client)
                await dav.delete_todo(client, self._todo_url(item["external_id"]))
                self._remember_resolved(dav)
        self.database.delete_checklist_item(item["id"])