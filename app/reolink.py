from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .db import Database

logger = logging.getLogger(__name__)

DETECTION_LABELS = {
    "people": "Person",
    "person": "Person",
    "vehicle": "Køretøj",
    "face": "Ansigt",
    "pet": "Kæledyr",
    "animal": "Dyr",
    "abnormal": "Unormalt",
}

DEFAULT_TIMEOUT = 8.0
TOKEN_REFRESH_SECONDS = 600


class ReolinkError(RuntimeError):
    pass


def normalize_host(host: str) -> str:
    value = host.strip().rstrip("/")
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Kamera-adresse skal være http:// eller https:// efterfulgt af IP/host")
    if parsed.username or parsed.password:
        raise ValueError("Brugernavn/adgangskode må ikke stå i adressefeltet")
    return value


class ReolinkCamera:
    def __init__(self, camera: dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> None:
        self.camera_id = int(camera["id"])
        self.base_url = normalize_host(str(camera.get("host") or ""))
        self.username = str(camera.get("username") or "")
        self.password = str(camera.get("password") or "")
        self.channel = int(camera.get("channel") or 0)
        self.person_enabled = bool(camera.get("person_enabled", True))
        self.vehicle_enabled = bool(camera.get("vehicle_enabled", True))
        self.snapshots_enabled = bool(camera.get("snapshots_enabled", True))
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_at = 0.0

    async def _post(self, client: httpx.AsyncClient, command: str, payload: list[Any], token: bool) -> httpx.Response:
        token_part = f"&token={self._token}" if token and self._token else "&token=null"
        url = f"{self.base_url}/cgi-bin/api.cgi?cmd={command}{token_part}"
        try:
            response = await client.post(url, json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ReolinkError(f"Kameraet svarede ikke ({exc.__class__.__name__})") from exc
        if response.status_code != 200:
            raise ReolinkError(f"Kameraet returnerede HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ReolinkError("Kameraet returnerede ugyldigt JSON") from exc
        if not isinstance(body, list) or not body:
            raise ReolinkError("Kameraet returnerede en tom respons")
        code = body[0].get("code")
        if code not in (0, None):
            raise ReolinkError(f"Kameraet afviste forespørgslen (code={code})")
        return body

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() - self._token_at < TOKEN_REFRESH_SECONDS:
            return self._token
        digest = hashlib.md5(self.password.encode("utf-8")).hexdigest()
        body = await self._post(
            client,
            "Login",
            [
                {
                    "cmd": "Login",
                    "param": {
                        "User": {"userName": self.username, "password": digest},
                        "token": {"name": "null"},
                    },
                }
            ],
            token=False,
        )
        value = body[0].get("value") or {}
        token_wrapper = value.get("Token") or {}
        token = token_wrapper.get("name")
        if not token:
            raise ReolinkError("Login mislykkedes – kontrollér brugernavn/adgangskode")
        self._token = str(token)
        self._token_at = time.monotonic()
        return self._token

    async def get_ai_state(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        await self._ensure_token(client)
        body = await self._post(
            client,
            "GetAiState",
            [{"cmd": "GetAiState", "param": {"channel": self.channel}}],
            token=True,
        )
        active: list[dict[str, Any]] = []
        data = ((body[0].get("value") or {}).get("data") or {})
        ai_state = data.get("aiState") or []
        entry = ai_state[0] if ai_state else {}
        states = entry.get("aiState") or []
        for item in states:
            detection_type = str(item.get("type") or "").lower()
            state = item.get("state")
            try:
                is_active = int(state) > 0 if state is not None else False
            except (TypeError, ValueError):
                is_active = bool(state)
            if not is_active:
                continue
            if detection_type == "people" and not self.person_enabled:
                continue
            if detection_type == "vehicle" and not self.vehicle_enabled:
                continue
            active.append(
                {
                    "type": detection_type,
                    "label": DETECTION_LABELS.get(detection_type, detection_type),
                }
            )
        return active

    async def snapshot(self, client: httpx.AsyncClient) -> bytes:
        await self._ensure_token(client)
        body = await self._post(
            client,
            "Snap",
            [{"cmd": "Snap", "param": {"channel": self.channel}}],
            token=True,
        )
        snap = ((body[0].get("value") or {}) or {}).get("snap")
        if not snap:
            raise ReolinkError("Kameraet returnerede ingen snapshot")
        try:
            return base64.b64decode(snap)
        except (ValueError, TypeError) as exc:
            raise ReolinkError("Snapshot var ikke gyldigt base64") from exc


class CameraMonitor:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._snapshots: dict[int, bytes] = {}
        self._previous: dict[int, set[str]] = {}
        self._active: dict[int, dict[str, Any]] = {}
        self._populated_at: dict[int, float] = {}

    def poll_interval(self) -> float:
        try:
            return float(self.database.get_setting("reolink_poll_seconds", "5") or 5)
        except ValueError:
            return 5.0

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.poll()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Camera poll failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval())
            except asyncio.TimeoutError:
                pass

    async def poll(self) -> None:
        cameras = self.database.list_cameras()
        if not cameras:
            self._active = {}
            return
        for camera_row in cameras:
            await self._poll_camera(camera_row)

    async def _poll_camera(self, camera_row: dict[str, Any]) -> None:
        camera = ReolinkCamera(camera_row)
        camera_id = camera.camera_id
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with httpx.AsyncClient() as client:
                active = await camera.get_ai_state(client)
                if active and camera.snapshots_enabled:
                    try:
                        self._snapshots[camera_id] = await camera.snapshot(client)
                        self._populated_at[camera_id] = time.monotonic()
                    except ReolinkError:
                        pass
        except ReolinkError as exc:
            logger.debug("Camera %s: %s", camera_row.get("name"), exc)
            previous_types = self._previous.get(camera_id, set())
            for detection_type in previous_types:
                self.database.log_detection_end(camera_id, detection_type, now)
            self._previous[camera_id] = set()
            self._active.pop(camera_id, None)
            return

        types = {item["type"] for item in active}
        previous = self._previous.get(camera_id, set())
        for detection_type in types - previous:
            self.database.log_detection_start(camera_id, detection_type, now)
        for detection_type in previous - types:
            self.database.log_detection_end(camera_id, detection_type, now)
        self._previous[camera_id] = types

        if types:
            self._active[camera_id] = {
                "id": camera_id,
                "name": camera_row.get("name") or "Kamera",
                "types": [
                    {"type": item["type"], "label": item["label"]} for item in active
                ],
                "since": now,
            }
        else:
            self._active.pop(camera_id, None)

    def activity(self) -> dict[str, Any]:
        try:
            close_delay = int(self.database.get_setting("reolink_close_delay", "0") or 0)
        except ValueError:
            close_delay = 0
        return {
            "active": list(self._active.values()),
            "recent": self.database.recent_camera_activity(limit=15),
            "close_delay": max(0, close_delay),
            "poll_seconds": self.poll_interval(),
        }

    def snapshot_bytes(self, camera_id: int, max_age: float = 3.0) -> Optional[bytes]:
        populated = self._populated_at.get(camera_id)
        if populated and time.monotonic() - populated > max_age * 60:
            self._snapshots.pop(camera_id, None)
        return self._snapshots.get(camera_id)

    def invalidate_snapshot(self, camera_id: int) -> None:
        self._snapshots.pop(camera_id, None)
        self._populated_at.pop(camera_id, None)
        self._previous.pop(camera_id, None)

    async def test(self, camera_row: dict[str, Any]) -> dict[str, Any]:
        camera = ReolinkCamera(camera_row)
        result: dict[str, Any] = {"ok": False, "message": "", "active": []}
        try:
            async with httpx.AsyncClient() as client:
                active = await camera.get_ai_state(client)
                snapshot_ok = False
                if camera.snapshots_enabled:
                    try:
                        snapshot = await camera.snapshot(client)
                        snapshot_ok = bool(snapshot)
                    except ReolinkError:
                        snapshot_ok = False
            result["ok"] = True
            result["message"] = "Forbindelse OK – AI-tilstand"
            if active:
                result["message"] = (
                    "Forbindelse OK – aktivitet registreret: "
                    + ", ".join(item["label"] for item in active)
                )
            result["active"] = active
            result["snapshot_ok"] = snapshot_ok
        except ReolinkError as exc:
            result["message"] = str(exc)
        except ValueError as exc:
            result["message"] = str(exc)
        return result