"""Læser beskeder, opslag og kalender fra Aula.

Aula har intet offentligt forældre-API, så klienten taler med de samme
endepunkter som Aula-apps og andre open-source-klienter gør: et
OAuth2/PKCE-login mod login.aula.dk og derefter `?method=`-kald på
www.aula.dk. Det er uofficielt, så alle fejl bliver håndteret som almindelige
synkroniseringsfejl, så dashboardet aldrig går ned over det.

Loginflowet er delt i to trin, fordi dashboardet kører på en server uden
browser: `login_url()` giver et link brugeren åbner på sin egen telefon med
MitID, og `complete_login()` bytter den kode, som MitID-login'en sender
tilbage, til access- og refresh-token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .db import Database

logger = logging.getLogger("family-dashboard")

# Aula-native app'en for forældre. Login og token-udveksling sker mod
# Aula's SimpleSAML/OIDC-endepunkter.
AUTH_URL = "https://login.aula.dk/simplesaml/module.php/oidc/authorize.php"
TOKEN_URL = "https://login.aula.dk/simplesaml/module.php/oidc/token.php"
CLIENT_ID = "_742adb5e2759028d86dbadf4af44ef70e8b1f407a6"
# MitID-trinnet (niveau 3) bruger en anden client og et andet scope. Aula's
# egen app beder om begge, så login'et virker for forældre derlogger ind med
# MitID direkte.
CLIENT_ID_MITID = "_99949a54b8b65423862aac1bf629599ed64231607a"
SCOPE = "aula"
SCOPE_MITID = "aula-sensitive"
REDIRECT_URI = "https://app-private.aula.dk"
API_BASE = "https://www.aula.dk/api/v23"

# Aula identificerer klienten på de samme overskrifter som Android-app'en.
# Uden dem svarer API'et med en fejlside i stedet for JSON.
APP_HEADERS = {
    "User-Agent": "Android",
    "App-Version": "2.14.8",
    "App-Device-Type": "android-private",
    "Accept": "application/json",
}

# Beskeder og opslag er nye hver time eller derunder, så 15 minutter er
# nok til at fange alt på en skolefridag.
DEFAULT_THREAD_LIMIT = 50
DEFAULT_POST_LIMIT = 50
# Aula's kalenderposter kan være flere år fremme, men dashboardet har kun
# brug for et fornuftigt vindue.
DEFAULT_CALENDAR_DAYS_BACK = 30
DEFAULT_CALENDAR_DAYS_FORWARD = 180


class AulaError(Exception):
    """Aula svarede, men ikke med noget vi kan bruge."""


class AulaAuthError(AulaError):
    """Login eller token er udløbet og skal fornyes med MitID."""


def _pkce_pair() -> tuple[str, str]:
    """Returnerer (code_verifier, code_challenge) til PKCE-loginnet."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _client_id(scope: str) -> str:
    return CLIENT_ID_MITID if scope == SCOPE_MITID else CLIENT_ID


class AulaClient:
    """Én klient til Aula's login og data-API."""

    def __init__(
        self,
        access_token: str,
        csrf_token: str = "",
        session_cookie: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.access_token = access_token
        self.csrf_token = csrf_token
        self.session_cookie = session_cookie
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = dict(APP_HEADERS)
        if self.csrf_token:
            headers["Csrfp-Token"] = self.csrf_token
        return headers

    def _cookies(self) -> dict[str, str]:
        return {"PHPSESSID": self.session_cookie} if self.session_cookie else {}

    async def call(
        self, method: str, extra_params: Optional[dict[str, Any]] = None
    ) -> Any:
        """Kører ét `?method=`-kald og returnerer `data`-delen af svaret."""
        params: dict[str, Any] = {"method": method, "access_token": self.access_token}
        params.update(extra_params or {})
        query = urlencode(params, doseq=True)
        url = f"{API_BASE}/?{query}"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                headers=self._headers(),
                cookies=self._cookies(),
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise AulaError(f"Aula svarede ikke: {exc}") from exc

        if response.status_code in (401, 403):
            raise AulaAuthError("Aula-sessionen er udløbet. Log ind igen med MitID.")
        if response.status_code >= 400:
            raise AulaError(f"Aula svarede med HTTP {response.status_code}")

        # Aula's PHP-backend sætter en session og en CSRF-token i første
        # svar. Skal kaldes op ad, eller afvises de næste kald.
        self._absorb_session(response)

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AulaError("Aula svarede med noget der ikke var JSON") from exc

        if isinstance(payload, dict) and payload.get("error"):
            message = payload.get("error_description") or payload.get("error")
            if "token" in str(message).lower() or "session" in str(message).lower():
                raise AulaAuthError(f"Aula-sessionen er udløbet: {message}")
            raise AulaError(str(message))
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _absorb_session(self, response: httpx.Response) -> None:
        for cookie in response.cookies.jar:
            if cookie.name == "Csrfp-Token" and cookie.value:
                self.csrf_token = cookie.value
            elif cookie.name == "PHPSESSID" and cookie.value:
                self.session_cookie = cookie.value

    # --- profil og kontekst ------------------------------------------------

    async def profiles(self) -> list[dict[str, Any]]:
        """Finder børn og forældre-profiler, der hører til den loggede ind."""
        data = await self.call("profiles.getProfilesByLogin")
        profiles = data.get("profiles") if isinstance(data, dict) else data
        return list(profiles or [])

    async def establish_context(self) -> dict[str, Any]:
        """Sætter aktiv profil til forældre, så kaldene giver børnenes data."""
        return await self.call("profiles.getProfileContext", {"portalrole": "guardian"})

    # --- beskeder ----------------------------------------------------------

    async def threads(self, limit: int = DEFAULT_THREAD_LIMIT) -> list[dict[str, Any]]:
        """Henteste beskedtråde, nyeste først."""
        data = await self.call(
            "messaging.getThreads", {"page": 0, "sortOn": "date", "orderDirection": "desc"}
        )
        return _rows(data)

    async def messages(self, thread_id: str, limit: int = DEFAULT_POST_LIMIT) -> list[dict[str, Any]]:
        """Henteste beskederne i én tråd."""
        data = await self.call("messaging.getMessagesForThread", {"threadId": thread_id})
        return _rows(data)

    # --- opslag ------------------------------------------------------------

    async def posts(
        self, institution_profile_ids: list[str], limit: int = DEFAULT_POST_LIMIT
    ) -> list[dict[str, Any]]:
        """Henteste opslag. Aula kræver både forældre- og børne-id."""
        if not institution_profile_ids:
            raise AulaError("Aula kender ingen børne-profiler endnu")
        extra: dict[str, Any] = {"page": 0, "limit": limit}
        for index, profile_id in enumerate(institution_profile_ids):
            extra[f"institutionProfileIds[{index}]"] = profile_id
        return _rows(await self.call("posts.getAllPosts", extra))

    # --- kalender ----------------------------------------------------------

    async def calendar_events(
        self,
        institution_profile_ids: list[str],
        from_date: str,
        to_date: str,
    ) -> list[dict[str, Any]]:
        """Henter kalenderposter for de valgte børn i et datointerval."""
        if not institution_profile_ids:
            raise AulaError("Aula kender ingen børne-profiler endnu")
        extra: dict[str, Any] = {
            "fromDate": from_date,
            "toDate": to_date,
            "rangeStartDate": from_date,
            "rangeEndDate": to_date,
        }
        for index, profile_id in enumerate(institution_profile_ids):
            extra[f"institutionProfileIds[{index}]"] = profile_id
        try:
            return _rows(await self.call("childrensschedule.getMeetings", extra))
        except AulaError:
            # Nyere Aula-installationer har skiftet endepunkt-navn, så vi
            # prøver den klassiske sti, før vi giver op.
            return _rows(await self.call("calendar.getCalendarItems", extra))


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Aula svarer nogle gange med en liste og andre gange med en pakke."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("items", "rows", "result", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _strip_html(value: Any) -> str:
    """Aula leverer beskeder og opslag som HTML. Vi vil have ren tekst."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _content_text(value: Any) -> str:
    """Henter teksten ud af Aula's indholdsformat.

    Beskeder har indholdet som en streng, mens opslag ofte har det som et
    objekt med html-feltet, så begge skal give ren tekst.
    """
    if isinstance(value, str):
        return _strip_html(value)
    if isinstance(value, dict):
        for key in ("html", "text", "value", "content"):
            if key in value:
                return _content_text(value[key])
        return ""
    return ""


def _first(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _parse_datetime(value: Any) -> Optional[str]:
    """Gør Aula's tidsstempler om til ISO-8601 i UTC.

    Aula bruger både unix-tidsstempler i sekunder og millisekunder samt
    almindelige ISO-strenge, så vi håndterer dem alle og giver op i stedet
    for at gætte.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        # Tal over ca. år 2286 er millisekunder; alt under er sekunder.
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


class AulaSync:
    """Henter Aula og gemmer beskeder, opslag og kalender i dashboardet."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock: Optional[asyncio.Lock] = None

    def _lock_instance(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # --- livscyklus --------------------------------------------------------

    def configured(self) -> bool:
        return bool(self.database.get_setting("aula_refresh_token"))

    def enabled(self) -> bool:
        raw = self.database.get_setting("aula_enabled", "false")
        return raw.strip().lower() in {"1", "true", "yes", "on"} and self.configured()

    async def run_periodically(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if self.enabled():
                await self.sync()
            try:
                minutes = int(self.database.get_setting("aula_sync_minutes", "15") or 15)
            except ValueError:
                minutes = 15
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(1, minutes) * 60)
            except asyncio.TimeoutError:
                pass

    # --- login -------------------------------------------------------------

    async def login_url(self, scope: str = SCOPE) -> dict[str, Any]:
        """Starter PKCE-login og gemmer verifier, så koden kan byttes senere."""
        if scope not in {SCOPE, SCOPE_MITID}:
            raise AulaError(f"Ukendt login-rækkevidde: {scope}")
        verifier, challenge = _pkce_pair()
        state = secrets.token_hex(16)
        expires = datetime.now(timezone.utc) + timedelta(minutes=20)
        self.database.update_settings(
            {
                "aula_login_verifier": verifier,
                "aula_login_state": state,
                "aula_login_scope": scope,
                "aula_login_expires": expires.isoformat(),
            }
        )
        query = urlencode(
            {
                "client_id": _client_id(scope),
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return {"url": f"{AUTH_URL}?{query}", "state": state}

    async def complete_login(self, code: str, state: str = "") -> dict[str, Any]:
        """Bytter login-koden til tokens og henter børnenes profiler."""
        verifier = self.database.get_setting("aula_login_verifier") or ""
        expected_state = self.database.get_setting("aula_login_state") or ""
        expires = self.database.get_setting("aula_login_expires") or ""
        if not verifier:
            raise AulaAuthError("Der er ikke startet et Aula-login. Start det igen.")
        if expected_state and state and state != expected_state:
            raise AulaAuthError("Login'et hører ikke til denne dashboard. Start det igen.")
        if expires:
            try:
                if datetime.fromisoformat(expires) < datetime.now(timezone.utc):
                    raise AulaAuthError("Login-linket er udløbet. Start login igen med MitID.")
            except ValueError:
                pass
        code = code.strip()
        if not code:
            raise AulaError("Ingen kode modtaget fra Aula")

        scope = self.database.get_setting("aula_login_scope") or SCOPE
        tokens = await self._exchange(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": _client_id(scope),
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        )
        self.database.update_settings(
            {
                "aula_access_token": tokens.get("access_token", ""),
                "aula_refresh_token": tokens.get("refresh_token", ""),
                "aula_token_expires": _expires_at(tokens.get("expires_in")),
                "aula_login_verifier": "",
                "aula_login_state": "",
                "aula_login_scope": "",
                "aula_login_expires": "",
                "aula_last_error": "",
            }
        )
        client = self._client()
        await self._refresh_profiles(client)
        result = await self.sync()
        return {
            "ok": True,
            "children": self.children(),
            "threads": result.get("threads", 0),
            "posts": result.get("posts", 0),
        }

    async def logout(self) -> dict[str, Any]:
        self.database.update_settings(
            {
                "aula_access_token": "",
                "aula_refresh_token": "",
                "aula_token_expires": "",
                "aula_login_verifier": "",
                "aula_login_state": "",
                "aula_login_scope": "",
                "aula_login_expires": "",
            }
        )
        self.database.clear_aula_items()
        return {"ok": True}

    async def _exchange(self, payload: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                response = await client.post(TOKEN_URL, data=payload)
        except httpx.HTTPError as exc:
            raise AulaError(f"Aula-login nede nået: {exc}") from exc
        if response.status_code >= 400:
            raise AulaAuthError("Aula afviste login-koden. Prøv igen med MitID.")
        try:
            tokens = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AulaError("Aula svarede med noget der ikke var JSON") from exc
        if not tokens.get("access_token"):
            raise AulaAuthError("Aula svarede uden adgangstoken")
        return tokens

    def _client(self) -> AulaClient:
        return AulaClient(
            access_token=self.database.get_setting("aula_access_token") or "",
            csrf_token=self.database.get_setting("aula_csrf_token") or "",
            session_cookie=self.database.get_setting("aula_session_cookie") or "",
        )

    async def _ensure_fresh_token(self, client: AulaClient) -> AulaClient:
        """Fornyer access-token, hvis den er ved at løbe ud."""
        expires = self.database.get_setting("aula_token_expires") or ""
        if not expires:
            return client
        try:
            moment = datetime.fromisoformat(expires)
        except ValueError:
            return client
        if moment - datetime.now(timezone.utc) > timedelta(minutes=5):
            return client
        refresh = self.database.get_setting("aula_refresh_token") or ""
        if not refresh:
            return client
        tokens = await self._exchange(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": CLIENT_ID,
            }
        )
        self.database.update_settings(
            {
                "aula_access_token": tokens.get("access_token", client.access_token),
                "aula_refresh_token": tokens.get("refresh_token", refresh),
                "aula_token_expires": _expires_at(tokens.get("expires_in")),
            }
        )
        return self._client()

    async def _refresh_profiles(self, client: AulaClient) -> list[dict[str, Any]]:
        """Gemmer de barneprofiler, Aula giver adgang til."""
        client = await self._ensure_fresh_token(client)
        await client.profiles()
        context = await client.establish_context()
        self._persist_session(client)
        children = _children_from_context(context)
        self.database.replace_aula_profiles(children)
        return children

    def _persist_session(self, client: AulaClient) -> None:
        updates: dict[str, str] = {}
        if client.csrf_token:
            updates["aula_csrf_token"] = client.csrf_token
        if client.session_cookie:
            updates["aula_session_cookie"] = client.session_cookie
        if updates:
            self.database.update_settings(updates)

    def children(self) -> list[dict[str, Any]]:
        return self.database.list_aula_profiles()

    def _profile_ids(self, child_ids: Optional[list[str]] = None) -> list[str]:
        if child_ids:
            return [str(child_id) for child_id in child_ids if child_id]
        return [str(row["profile_id"]) for row in self.database.list_aula_profiles()]

    # --- synk --------------------------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "message": "Aula er ikke logt ind. Log ind med MitID først."}
        try:
            children = await self._refresh_profiles(self._client())
        except AulaAuthError as exc:
            return {"ok": False, "message": str(exc)}
        except (AulaError, httpx.HTTPError) as exc:
            return {"ok": False, "message": str(exc)}
        names = ", ".join(row["name"] for row in children[:6]) or "ingen børn fundet"
        return {"ok": True, "message": f"Forbindelsen virker – børn hos Aula: {names}"}

    async def sync(
        self,
        child_ids: Optional[list[str]] = None,
        with_messages: bool = True,
        with_posts: bool = True,
        with_calendar: bool = True,
    ) -> dict[str, Any]:
        if not self.configured():
            return {"synced": False, "reason": "not_configured"}

        async with self._lock_instance():
            client = self._client()
            try:
                children = await self._refresh_profiles(client)
                profile_ids = self._profile_ids(child_ids)
                if child_ids:
                    wanted = {str(child_id) for child_id in child_ids}
                    children = [row for row in children if row["profile_id"] in wanted]

                counts = {"threads": 0, "messages": 0, "posts": 0, "events": 0}
                if with_messages:
                    counts["threads"] = await self._sync_messages(client)
                if with_posts:
                    counts["posts"] = await self._sync_posts(client, profile_ids)
                if with_calendar:
                    counts["events"] = await self._sync_calendar(client, profile_ids)

                self.database.update_settings(
                    {
                        "aula_last_sync": datetime.now(timezone.utc).isoformat(),
                        "aula_last_error": "",
                    }
                )
                return {"synced": True, "children": children, **counts}
            except AulaAuthError as exc:
                logger.warning("Aula sync needs new login: %s", exc)
                self.database.update_settings({"aula_last_error": str(exc)[:500]})
                self.database.update_settings(
                    {"aula_access_token": "", "aula_refresh_token": ""}
                )
                return {"synced": False, "error": str(exc), "needs_login": True}
            except (AulaError, httpx.HTTPError) as exc:
                logger.warning("Aula sync failed: %s", exc)
                self.database.update_settings({"aula_last_error": str(exc)[:500]})
                return {"synced": False, "error": str(exc)}

    async def _sync_messages(self, client: AulaClient) -> int:
        """Gemmer beskedtrådene. Trådenes fulde beskeder hentes kun for nye tråde."""
        client = await self._ensure_fresh_token(client)
        threads = await client.threads()
        known = {row["thread_id"] for row in self.database.list_aula_threads()}
        stored = 0
        for thread in threads[:DEFAULT_THREAD_LIMIT]:
            thread_id = str(_first(thread, "threadId", "id", "guid", default=""))
            if not thread_id:
                continue
            unread = bool(_first(thread, "unread", "isUnread", "unreadCount", default=0))
            self.database.upsert_aula_thread(
                thread_id=thread_id,
                subject=str(_first(thread, "subject", "title", default="(uden emne)")),
                sender=str(_first(thread, "senderName", "sender", "creator", default="")),
                received_at=_parse_datetime(
                    _first(thread, "date", "timestamp", "lastMessageDate", "receivedDate")
                ),
                participants=_as_list(_first(thread, "participants", "recipientNames")),
                unread=unread,
            )
            stored += 1
            # Beskederne i en tråd hentes kun første gang, så tælleformen
            # på en gammel tråd ikke hentes igen for hver synkronisering.
            if thread_id not in known:
                try:
                    messages = await client.messages(thread_id)
                except AulaError:
                    messages = []
                for message in messages:
                    message_id = str(_first(message, "messageId", "id", "guid", default=""))
                    if not message_id:
                        continue
                    self.database.upsert_aula_message(
                        message_id=message_id,
                        thread_id=thread_id,
                        sender=str(_first(message, "senderName", "sender", default="")),
                        body=_content_text(_first(message, "content", "body", "message")),
                        sent_at=_parse_datetime(_first(message, "date", "timestamp")),
                        is_from_me=bool(_first(message, "isOwn", "isFromMe", default=False)),
                    )
                # Tråden er nu hentet, så næste kørsel springer over den.
                self.database.mark_aula_thread_fetched(thread_id)
        self._persist_session(client)
        return stored

    async def _sync_posts(self, client: AulaClient, profile_ids: list[str]) -> int:
        client = await self._ensure_fresh_token(client)
        posts = await client.posts(profile_ids)
        stored = 0
        for post in posts[:DEFAULT_POST_LIMIT]:
            post_id = str(_first(post, "id", "postId", "guid", default=""))
            if not post_id:
                continue
            content = _content_text(_first(post, "content", "contentHtml", "body", default=""))
            self.database.upsert_aula_post(
                post_id=post_id,
                title=str(_first(post, "title", "subject", default="(uden titel)")).strip()
                or (content.strip().splitlines()[0][:80] if content.strip() else "(uden titel)"),
                author=str(_first(post, "authorName", "author", "creator", default="")),
                body=content,
                published_at=_parse_datetime(_first(post, "timestamp", "date", "publishedDate")),
                audience=_as_list(_first(post, "audience", "groupNames", "labels")),
                tags=_as_list(_first(post, "tags", "keywords")),
                unread=not _is_read_flag(post),
            )
            stored += 1
        self._persist_session(client)
        return stored

    async def _sync_calendar(self, client: AulaClient, profile_ids: list[str]) -> int:
        """Henter Aula-kalenderen. Fejl her må ikke stoppe beskeder og opslag."""
        client = await self._ensure_fresh_token(client)
        today = datetime.now(timezone.utc).date()
        from_date = (today - timedelta(days=DEFAULT_CALENDAR_DAYS_BACK)).isoformat()
        to_date = (today + timedelta(days=DEFAULT_CALENDAR_DAYS_FORWARD)).isoformat()
        try:
            events = await client.calendar_events(profile_ids, from_date, to_date)
        except AulaError as exc:
            logger.info("Aula kalender kunne ikke hentes: %s", exc)
            return 0
        stored = 0
        for event in events:
            event_id = str(_first(event, "id", "meetingId", "guid", default=""))
            if not event_id:
                continue
            start = _parse_datetime(_first(event, "startDate", "start", "meetingDate", "begin"))
            end = _parse_datetime(_first(event, "endDate", "end", default=None))
            self.database.upsert_aula_event(
                event_id=event_id,
                title=str(_first(event, "title", "subject", "name", default="(uden titel)")),
                description=_content_text(_first(event, "description", "content", default="")),
                location=str(_first(event, "location", "room", default="")),
                start_at=start,
                end_at=end or start,
                category=str(_first(event, "category", "type", default="")),
                profile_ids=_as_list(_first(event, "institutionProfileIds", "profiles")),
            )
            stored += 1
        self._persist_session(client)
        return stored

    # --- lokal prioritering ------------------------------------------------

    def set_starred(self, kind: str, item_id: str, starred: bool) -> dict[str, Any]:
        """Stjernemarkerer en besked, et opslag eller en kalenderpost."""
        if kind == "thread":
            changed = self.database.set_aula_thread_starred(item_id, starred)
        elif kind == "post":
            changed = self.database.set_aula_post_starred(item_id, starred)
        elif kind == "event":
            changed = self.database.set_aula_event_starred(item_id, starred)
        else:
            raise AulaError(f"Ukendt type: {kind}")
        if not changed:
            raise AulaError(f"{kind} {item_id} findes ikke")
        return {"ok": True, "kind": kind, "id": item_id, "starred": starred}

    def set_read(self, kind: str, item_id: str, is_read: bool) -> dict[str, Any]:
        if kind == "thread":
            changed = self.database.set_aula_thread_read(item_id, is_read)
        elif kind == "post":
            changed = self.database.set_aula_post_read(item_id, is_read)
        else:
            raise AulaError(f"Ukendt type: {kind}")
        if not changed:
            raise AulaError(f"{kind} {item_id} findes ikke")
        return {"ok": True, "kind": kind, "id": item_id, "read": is_read}


def _is_read_flag(row: dict[str, Any]) -> bool:
    value = _first(row, "isRead", "read", "isSeen", default=None)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes"}


def _expires_at(expires_in: Any) -> str:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 3600
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _children_from_context(context: Any) -> list[dict[str, Any]]:
    """Plukker børnene ud af Aula's profilkontekst.

    Aula's svar afhænger af, hvilket niveau brugeren har valgt i app'en, så
    vi leder i to niveauer og tager det første der har børn med.
    """
    if not isinstance(context, dict):
        return []
    institutions = context.get("institutions")
    if not isinstance(institutions, list):
        return []

    # Nogle installationer ligger i "children" direkte på konteksten.
    direct = _children_from_institution(context)
    if direct:
        return direct

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for institution in institutions:
        if not isinstance(institution, dict):
            continue
        institution_name = str(_first(institution, "name", "institutionName", default=""))
        for child in _children_from_institution(institution):
            child["institution"] = institution_name
            key = child["profile_id"]
            if key in seen:
                continue
            seen.add(key)
            rows.append(child)
    return rows


def _children_from_institution(institution: dict[str, Any]) -> list[dict[str, Any]]:
    children = institution.get("children")
    if not isinstance(children, list):
        return []
    rows: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        profile_id = _first(child, "institutionProfileId", "profileId", "id")
        if profile_id in (None, ""):
            continue
        rows.append(
            {
                "profile_id": str(profile_id),
                "name": str(_first(child, "name", "displayName", "firstName", default="Barn")),
                "institution": "",
            }
        )
    return rows
