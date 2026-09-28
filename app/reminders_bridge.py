from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8787"
_TIMEOUT = 20.0


class BridgeError(RuntimeError):
    pass


def normalize_bridge_url(value: str) -> str:
    """Accepts host, host:port or full URL and returns a clean base URL."""
    text = (value or "").strip()
    if not text:
        raise ValueError("Indtast Mac'ens adresse, fx 192.168.1.100:8787")
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Kun http eller https understøttes")
    if not parts.netloc:
        raise ValueError("Ugyldig adresse")
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _optional_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class BridgeRemindersClient:
    """HTTP client for FamilyBridge (the Mac mini EventKit bridge).

    The bridge reads the real local Reminders database via EventKit, which is the
    only way to see the same reminders as the Apple Reminders app. iCloud
    Reminders are NOT exposed over IMAP.
    """

    def __init__(self, base_url: str, token: str, timeout: float = _TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.configured:
            raise BridgeError("Mac mini-bridge er ikke konfigureret (mangler adresse eller token)")
        url = f"{self.base_url}{path}"
        try:
            response = await client.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise BridgeError(
                f"Ingen svar fra Mac mini-bridge ({url}) – er Mac'en tændt og FamilyBridge kørende?"
            ) from exc
        except httpx.HTTPError as exc:
            raise BridgeError(f"Kunne ikke nå Mac mini-bridge ({url}): {exc}") from exc

        if response.status_code == 401:
            raise BridgeError("Bridge afviste tokenet – tjek tokenet fra Mac mini'en")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            message = payload.get("error") if isinstance(payload, dict) else None
            raise BridgeError(
                f"Bridge svarede HTTP {response.status_code}"
                + (f": {message}" if message else "")
            )
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise BridgeError(str(payload.get("error") or "Ukendt bridge-fejl"))
        return payload if isinstance(payload, dict) else {}

    async def health(self, client: httpx.AsyncClient) -> dict[str, Any]:
        return await self._request(client, "GET", "/health")

    async def list_task_lists(self, client: httpx.AsyncClient) -> list[dict[str, str]]:
        payload = await self._request(client, "GET", "/lists")
        lists: list[dict[str, str]] = []
        for item in payload.get("lists") or []:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or "")
            if not identifier:
                continue
            lists.append(
                {
                    "href": identifier,
                    "name": str(item.get("name") or identifier),
                    "color": str(item.get("color") or ""),
                }
            )
        return lists

    async def list_tasks(
        self, client: httpx.AsyncClient, list_id: str, include_done: bool = False
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if list_id:
            params["list"] = list_id
        if include_done:
            params["done"] = "1"
        payload = await self._request(client, "GET", "/todos", params=params)
        results: list[dict[str, Any]] = []
        for item in payload.get("todos") or []:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or "")
            if not identifier:
                continue
            results.append(
                {
                    "external_id": identifier,
                    "summary": str(item.get("title") or ""),
                    "done": bool(item.get("done")),
                    "due": _optional_date(item.get("due")),
                }
            )
        return results

    async def create_todo(
        self, client: httpx.AsyncClient, list_id: str, summary: str, due: Optional[date]
    ) -> str:
        body: dict[str, Any] = {"title": summary, "list_id": list_id}
        if due:
            body["due"] = due.isoformat()
        payload = await self._request(client, "POST", "/todos", json=body)
        todo = payload.get("todo") or {}
        identifier = str(todo.get("id") or "")
        if not identifier:
            raise BridgeError("Bridge returnerede ikke et id for den nye påmindelse")
        return identifier

    async def update_todo(
        self,
        client: httpx.AsyncClient,
        external_id: str,
        summary: str,
        done: bool,
        due: Optional[date] = None,
    ) -> None:
        body: dict[str, Any] = {"id": external_id, "title": summary, "done": done}
        body["due"] = due.isoformat() if due else ""
        await self._request(client, "PATCH", "/todos", json=body)

    async def delete_todo(self, client: httpx.AsyncClient, external_id: str) -> None:
        await self._request(client, "DELETE", "/todos", json={"id": external_id})

    async def notes(
        self, client: httpx.AsyncClient, title: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"limit": str(limit)}
        if title:
            params["title"] = title
        payload = await self._request(client, "GET", "/notes", params=params)
        return [item for item in payload.get("notes") or [] if isinstance(item, dict)]

    async def save_note(
        self, client: httpx.AsyncClient, title: str, content: str
    ) -> dict[str, Any]:
        payload = await self._request(
            client, "POST", "/notes", json={"title": title, "content": content}
        )
        note = payload.get("note")
        return note if isinstance(note, dict) else {}
