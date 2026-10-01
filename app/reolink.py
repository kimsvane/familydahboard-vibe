from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import shutil
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional
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
    "dog_cat": "Kæledyr",
    "abnormal": "Unormalt",
}

DEFAULT_TIMEOUT = 8.0
TOKEN_REFRESH_SECONDS = 600

STREAM_BOUNDARY = "ffmpeg"
STREAM_READ_SIZE = 65536


class ReolinkError(RuntimeError):
    pass


class ReolinkProtocolError(ReolinkError):
    """Kameraet forstod ikke anmodningen, fordi det taler en anden
    protokol end appen.

    Det er ikke et forkert password. Ældre Reolink-firmware afviser
    login-kroppen med rspCode -4 og uden auth_warning_info, altså uden
    at tælle mod låsningen. Derfor må vi prøve en anden metode.
    """


def is_streaming_url(value: str) -> bool:
    return str(value or "").strip().lower().startswith(
        ("rtsp://", "rtsps://", "rtmp://")
    )


def ffmpeg_mjpeg_args(url: str, height: int = 720, fps: int = 10, quality: int = 5) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-analyzeduration", "1000000",
        "-probesize", "500000",
        "-i", str(url),
        "-an",
        "-vf", f"scale=-2:{int(height)}",
        "-r", str(fps),
        "-q:v", str(quality),
        "-f", "mpjpeg",
        "pipe:1",
    ]


async def stream_camera_mjpeg(
    camera_row: dict[str, Any],
    *,
    max_seconds: float = 120.0,
    height: int = 720,
    restart: bool = True,
) -> AsyncIterator[bytes]:
    rtsp = str(camera_row.get("live_stream_url") or "").strip()
    if not is_streaming_url(rtsp):
        raise ReolinkError(
            "Kameraet har ingen gyldig live-stream-URL – brug fx "
            "rtsp://brugernavn:kode@IP:554/h264Preview_01_main"
        )
    if shutil.which("ffmpeg") is None:
        raise ReolinkError("ffmpeg er ikke installeret i containeren")
    deadline = time.monotonic() + max(1.0, max_seconds)
    while time.monotonic() < deadline:
        process = await asyncio.create_subprocess_exec(
            *ffmpeg_mjpeg_args(rtsp, height=height),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = await process.stdout.read(STREAM_READ_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if not restart:
            break
        await asyncio.sleep(1.0)


_REOLINK_ERROR_CODES = {
    1: "ugyldige parametre",
    2: "forkert brugernavn eller adgangskode",
    12: "ingen adgang",
    13: "ingen adgang / ikke logget ind",
    19: "ny adgangskodekrævet",
    25: "konto blokeret",
}

#: Reolink-fejl, der betyder "jeg forstod ikke din login", altså at
#: kameraet bruger en ældre autentificeringsmetode frem for den
#: JSON-login, appen sender. rspCode -4 er "param error".
_PROTOCOL_ERRORS = (-4, -6)


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def parse_digest_challenge(header: str) -> Optional[dict[str, str]]:
    """Læs en WWW-Authenticate: Digest-header.

    Ældre Reolink-firmware kræver HTTP Digest Authentication, som
    webinterfacet svarer på. Uden den afviser kameraet login med
    "param error", som er protokol-inkompatibilitet og ikke et
    forkert password.

    Værdier kan selv være kommaseparerede (qop="auth,auth-int"), så
    vi kan ikke bare splitte på kommaer. Vi går tegn for tegn og
    holder øje med citater.
    """
    if not header or "digest" not in header.lower():
        return None
    _, _, rest = header.partition(" ")
    felter: dict[str, str] = {}
    i = 0
    segment_start = 0
    in_quote = False
    while i < len(rest):
        tegn = rest[i]
        if tegn == '"':
            in_quote = not in_quote
        elif tegn == "," and not in_quote:
            # Nyt felt begynder. Gem det vi netop så.
            navn, _, vaerdi = rest[segment_start:i].partition("=")
            if navn.strip():
                felter[navn.strip().lower()] = vaerdi.strip().strip('"')
            segment_start = i + 1
        i += 1
    navn, _, vaerdi = rest[segment_start:].partition("=")
    if navn.strip():
        felter[navn.strip().lower()] = vaerdi.strip().strip('"')
    if not felter.get("nonce"):
        return None
    return felter


def build_digest_response(
    challenge: dict[str, str],
    username: str,
    password: str,
    method: str,
    uri: str,
    cnonce: str,
    nc: str = "00000001",
) -> str:
    """Beregn et digest-svar (RFC 7616) til en udfordring fra kameraet."""
    realm = challenge.get("realm", "")
    nonce = challenge["nonce"]
    qop = challenge.get("qop", "")
    # qop kommer nogle gange som "auth,auth-int". Vi bruger altid auth.
    qop_valdi = "auth" if "auth" in [v.strip() for v in qop.split(",")] else ""
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop_valdi:
        return _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop_valdi}:{ha2}")
    return _md5(f"{ha1}:{nonce}:{ha2}")


def build_digest_header(
    challenge: dict[str, str],
    username: str,
    password: str,
    method: str,
    uri: str,
) -> str:
    """Byg en komplet Authorization: Digest-header."""
    cnonce = _md5(f"{username}:{challenge['nonce']}:{time.time()}")
    nc = "00000001"
    svar = build_digest_response(challenge, username, password, method, uri, cnonce, nc)
    dele = [
        f'username="{username}"',
        f'realm="{challenge.get("realm", "")}"',
        f'nonce="{challenge["nonce"]}"',
        f'uri="{uri}"',
        f'response="{svar}"',
    ]
    if challenge.get("algorithm"):
        dele.append(f'algorithm="{challenge["algorithm"]}"')
    qop = challenge.get("qop", "")
    if "auth" in [v.strip() for v in qop.split(",")]:
        dele.append("qop=auth")
        dele.append(f"nc={nc}")
        dele.append(f'cnonce="{cnonce}"')
    return "Digest " + ", ".join(dele)


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
        # Et kamera kan være slået fra for popup. Det skal stadig logge
        # aktivitet, så kamerakortet ikke går i sort, men det må ikke
        # springe en fuld skærm frem midt i en kveld.
        self.popup_enabled = bool(camera.get("popup_enabled", True))
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_at = 0.0
        # Gemmer en eventuel Digest-udfordring, så den kun forhandles
        # én gang pr. kamera i stedet for ved hvert kald.
        self._digest_challenge: Optional[dict[str, str]] = None
        # Hvilket login-format der virkede sidst, så vi prøver det
        # først næste gang i stedet for at lede fra bunden hver gang.
        self._login_format: Optional[int] = None

    async def _request(
        self,
        client: httpx.AsyncClient,
        command: str,
        payload: list[Any],
        token: bool,
        allow_digest: bool = True,
    ) -> httpx.Response:
        """Send CGI-kaldet og håndtér auth, uden at tolke svaret.

        Snap på ældre firmware svarer med rå JPEG i stedet for JSON, så
        selve HTTP-delen er skilt ud fra JSON-tolkningen.
        """
        token_part = f"&token={self._token}" if token and self._token else "&token=null"
        uri = f"/cgi-bin/api.cgi?cmd={command}{token_part}"
        url = f"{self.base_url}{uri}"
        headers: dict[str, str] = {}
        # Et kamera på gammel firmware kræver Digest. Sender vi ikke
        # Authorization, svarer det "param error" uden at fortælle hvorfor.
        # Når vi engang har modtaget en udfordring, svarer vi på den ved
        # hvert kald, så vi ikke skal genforhandle hver gang.
        if self._digest_challenge and self.username and self.password:
            headers["Authorization"] = build_digest_header(
                self._digest_challenge, self.username, self.password, "POST", uri
            )
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ReolinkError(f"Kameraet svarede ikke ({exc.__class__.__name__})") from exc
        # Et 401 med en udfordring er ikke en fejl endnu, men en
        # invitation: vi svarer med digest på næste forsøg. Vi gør
        # det højst én gang, ellers kan et kamera, der fortsætter
        # med at svare 401, få os til at køre i sløjfe.
        if response.status_code == 401:
            udfordring = parse_digest_challenge(response.headers.get("www-authenticate", ""))
            if udfordring and self.username and self.password and allow_digest:
                self._digest_challenge = udfordring
                logger.debug(
                    "Reolink %s kræver HTTP Digest på %s; svarer med udfordringen",
                    command,
                    self.base_url,
                )
                return await self._request(client, command, payload, token, allow_digest=False)
            raise ReolinkError(
                "Kameraet kræver godkendelse (HTTP 401) – kontrollér brugernavn/adgangskode"
            )
        if response.status_code != 200:
            raise ReolinkError(f"Kameraet returnerede HTTP {response.status_code}")
        return response

    async def _post(
        self,
        client: httpx.AsyncClient,
        command: str,
        payload: list[Any],
        token: bool,
    ) -> httpx.Response:
        response = await self._request(client, command, payload, token)

        try:
            body = response.json()
        except ValueError as exc:
            logger.debug(
                "Reolink %s svarede %s (ikke JSON): %r",
                command,
                response.headers.get("content-type"),
                response.text[:300],
            )
            raise ReolinkError("Kameraet returnerede ugyldigt JSON") from exc
        if not isinstance(body, list) or not body:
            raise ReolinkError("Kameraet returnerede en tom respons")
        code = body[0].get("code")
        if code not in (0, None):
            meaning = _REOLINK_ERROR_CODES.get(
                int(code), "ukendt fejl"
            )
            fejl = body[0].get("error") or {}
            rsp_code = fejl.get("rspCode")
            logger.debug(
                "Reolink %s på %s -> code=%s rspCode=%s; rå svar: %r",
                command,
                self.base_url,
                code,
                rsp_code,
                body[0],
            )
            if code == 1 and not self.password:
                raise ReolinkError(
                    "Kameraet afviste login (code=1) – intet password er gemt for "
                    "kameraet. Indtast adgangskoden under Kameraer."
                )
            # "param error" og "please login first" betyder, at kameraet
            # ikke forstod formatet. Det er en protokolforskel, ikke et
            # dårligt password, så kalderen skal prøve en anden metode.
            # auth_warning_info betyder derimod at kameraet regner
            # credentials som forkerte, og da må vi ikke prøve igen.
            if rsp_code in _PROTOCOL_ERRORS and "auth_warning_info" not in fejl:
                raise ReolinkProtocolError(
                    f"Kameraet svarer med {fejl.get('detail') or 'ukendt protokolfejl'} "
                    f"(rspCode={rsp_code}) på {command}"
                )
            if "auth_warning_info" in fejl:
                # Forsøg mod en lås-tæller. Vi giver op i stedet for at
                # brænde flere forsøg af, så brugeren kan nulstille den.
                raise ReolinkError(
                    "Kameraet afviste brugernavn/adgangskode. Hvis du for nylig har "
                    "ændret koden, kan kameraet være låst i et stykke tid – nulstil "
                    "forsøgstælleren i Reolinks egen webinterface."
                )
            raise ReolinkError(f"Kameraet afviste forespørgslen (code={code} – {meaning})")
        return body

    async def _login_body(self, client: httpx.AsyncClient, payload: list[Any]) -> httpx.Response:
        return await self._post(client, "Login", payload, token=False)

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() - self._token_at < TOKEN_REFRESH_SECONDS:
            return self._token
        if not self.password:
            raise ReolinkError(
                "Intet password er gemt for kameraet – indtast kameraets adgangskode "
                "under Kameraer, ellers afviser Reolink login (code=1)"
            )
        digest = hashlib.md5(self.password.encode("utf-8")).hexdigest()
        # Hvordan adgangskoden skal sendes. Nyere Reolink vil have den
        # MD5-hashet, ældre firmware vil have den i klar tekst. Det er
        # den eneste forskel, og den kan ikke forudses af firmware-
        # versionen, så vi prøver den ene og husker resultatet.
        #  0 = MD5 (moderne)
        #  1 = klar tekst (ældre)
        formater = [digest, self.password]
        valgte = self._login_format if self._login_format is not None else 0
        # Vi prøver den valgte metode først, og derefter den anden, men kun
        # efter en protokolfejl. En afvist adgangskode afbryder med det samme.
        rækkefølge = [valgte] + [i for i in range(len(formater)) if i != valgte]
        sidste_fejl: Optional[ReolinkError] = None
        for indeks in rækkefølge:
            try:
                body = await self._login_body(
                    client,
                    [
                        {
                            "cmd": "Login",
                            "param": {
                                "User": {"userName": self.username, "password": formater[indeks]},
                                "token": {"name": "null"},
                            },
                        }
                    ],
                )
            except ReolinkProtocolError as fejl:
                sidste_fejl = fejl
                logger.debug(
                    "Reolink login-format %d på %s -> protokolfejl: %s",
                    indeks,
                    self.base_url,
                    fejl,
                )
                continue
            value = body[0].get("value") or {}
            token_wrapper = value.get("Token") or {}
            token = token_wrapper.get("name")
            if token:
                self._login_format = indeks
                self._token = str(token)
                self._token_at = time.monotonic()
                logger.info(
                    "Reolink login på %s lykkedes med adgangskode-format %d",
                    self.base_url,
                    indeks,
                )
                return self._token
            raise ReolinkError("Login mislykkedes – kontrollér brugernavn/adgangskode")
        # Alle formater er prøvet. Hvis intet virkede, er det næsten
        # sikkert credentials, så vi skal ikke blive ved med at prøve.
        raise sidste_fejl or ReolinkError("Login mislykkedes – kontrollér brugernavn/adgangskode")

    async def get_ai_state(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        await self._ensure_token(client)
        body = await self._post(
            client,
            "GetAiState",
            [{"cmd": "GetAiState", "param": {"channel": self.channel}}],
            token=True,
        )
        value = body[0].get("value") or {}
        active: list[dict[str, Any]] = []
        data = value.get("data") or {}
        if isinstance(data, dict) and data.get("aiState") is not None:
            # Nyere firmware: value.data.aiState[0].aiState[] med type/state.
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
        # Ældre firmware: value har en nøgle pr. type, hver med
        # {alarm_state, support}. Det er denne form vores testkamera
        # svarer i, og den gav tidligere altid tom, fordi appen ledte
        # efter et data.aiState-felt der ikke findes.
        for raw_type, info in value.items():
            if not isinstance(info, dict):
                continue
            detection_type = str(raw_type).lower()
            if detection_type not in DETECTION_LABELS:
                continue
            if int(info.get("support") or 0) == 0:
                continue
            try:
                is_active = int(info.get("alarm_state") or 0) > 0
            except (TypeError, ValueError):
                is_active = bool(info.get("alarm_state"))
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
        response = await self._request(
            client, "Snap", [{"cmd": "Snap", "param": {"channel": self.channel}}], token=True
        )
        # Nyere firmware svarer med base64 i JSON. Ældre svarer med rå
        # JPEG, og det er den, vores testkamera gør.
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type.startswith("image/") or response.content[:2] == b"\xff\xd8":
            if not response.content:
                raise ReolinkError("Kameraet returnerede intet snapshot")
            return response.content
        try:
            body = response.json()
        except ValueError as exc:
            raise ReolinkError("Kameraet returnerede hverken billede eller JSON") from exc
        if not isinstance(body, list) or not body:
            raise ReolinkError("Kameraet returnerede en tom respons")
        code = body[0].get("code")
        if code not in (0, None):
            raise ReolinkError(
                f"Kameraet afviste snapshot (code={code} – "
                f"{_REOLINK_ERROR_CODES.get(int(code), 'ukendt fejl')})"
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
            tidligere = self._active.get(camera_id) or {}
            popup_enabled = camera.popup_enabled
            self._active[camera_id] = {
                "id": camera_id,
                "name": camera_row.get("name") or "Kamera",
                "types": [
                    {"type": item["type"], "label": item["label"]} for item in active
                ],
                # since er det FØRSTE tidspunkt denne hændelse blev set,
                # ikke det seneste. Poppen bruger det til at genkende den
                # samme detektion, så hvis det flyttede sig ved hvert poll,
                # blev hele poppen bygget om hvert femte sekund. Så døde
                # live-streamen, fordi billedet blev ødelagt og lavet på
                # ny, mens ffmpeg knap nåede at starte.
                "since": tidligere.get("since") or now,
                # sendes med, så forsiden kan skelne mellem "aktivt" i
                # loggen og "skal give popup".
                "popup_enabled": popup_enabled,
            }
        else:
            self._active.pop(camera_id, None)

    def activity(self) -> dict[str, Any]:
        try:
            close_delay = int(self.database.get_setting("reolink_close_delay", "0") or 0)
        except ValueError:
            close_delay = 0
        try:
            live_delay = int(self.database.get_setting("reolink_live_delay", "3") or 0)
        except ValueError:
            live_delay = 3
        return {
            "active": list(self._active.values()),
            "recent": self.database.recent_camera_activity(limit=15),
            "close_delay": max(0, close_delay),
            "live_delay": max(0, live_delay),
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