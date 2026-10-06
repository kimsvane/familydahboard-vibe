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
from urllib.parse import unquote, urlencode

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
API_HOST = "https://www.aula.dk"

# Aula skifter API-version løbende, og en version der er taget ned svarer
# 410 Gone. Derfor sonderer vi opad, indtil en version svarer, og gemmer
# den, så det kun koster ét par kald når der har været et skift.
API_VERSION_DEFAULT = 24
API_VERSION_MAX = 40

# Aula identificerer klienten på de samme overskrifter som Android-app'en.
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


def _b64_tekst(vaerdi: str) -> str:
    """Dekoder base64, også hvis Aula har skrevet det uden '='-tegn.

    app-redirect læser `returnUri` som en query-parameter, så et '+' i
    base64'en bliver til et mellemrum, ligesom i PHP. Hvis den ikke kan
    læses, prøver vi derfor igen med mellerum tilbage som '+'.
    """
    fyld = "=" * (-len(vaerdi) % 4)
    for forsøg in (vaerdi, vaerdi.replace(" ", "+")):
        try:
            return base64.urlsafe_b64decode(forsøg + fyld).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
    raise ValueError("returnUri er ikke gyldig base64")


def _find_param(hoved: str, navn: str) -> str:
    """Plukker én parameter ud af en URL eller en `a=b&c=d`-streng."""
    # parse_qs ville være nemmere, men den finder ikke `code=` i en
    # base64-kodet returadresse, som er den form brugeren lander på.
    match = re.search(rf"[?&#]{re.escape(navn)}=([^&#\s]+)", hoved)
    return unquote(match.group(1)) if match else ""


def _traek_kode(indhold: str) -> tuple[str, str]:
    """Finder login-koden i hvad brugeren nu har indsat.

    MitID-login'en ender på app-redirect.aula.dk, som er en mellemside til
    Aulas mobilapp. Den har koden indpakket i `returnUri` som base64, og
    "Fortsæt login"-knappen fører videre til Aulas egen webapp, som ikke
    kan bruges herfra. Derfor læser vi koden direkte ud af den adresse,
    brugeren lander på, i stedet for at bede dem om at trykke videre.

    Godtages også app-private-adressen, `code=..&state=..` og koden alene.
    """
    tekst = (indhold or "").strip().strip('"').strip("'")
    for _ in range(4):  # returnUri kan pege videre, men ikke i en løkke
        if not tekst:
            return "", ""
        pakket = _find_param(tekst, "returnUri")
        if pakket and "code=" not in tekst:
            try:
                tekst = _b64_tekst(pakket)
            except (ValueError, UnicodeDecodeError):
                return "", ""
            continue
        kode = _find_param(tekst, "code")
        if kode:
            return kode, _find_param(tekst, "state")
        # Ren `a=b&c=d` uden kodesti: find den som hedder code.
        par = {}
        for delel in tekst.split("&"):
            if "=" in delel:
                nøgle, værdi = delel.split("=", 1)
                par.setdefault(nøgle.strip(), unquote(værdi.strip()))
        if par.get("code"):
            return par["code"], par.get("state", "")
        # Ellers er det koden alene.
        return tekst, ""
    return "", ""


class AulaApiGone(AulaError):
    """Aula har fjernet den API-version, vi prøvede."""

    def __init__(self, version: int) -> None:
        super().__init__(f"Aula svarer ikke længere på API-version {version}")
        self.version = version


class AulaClient:
    """Én klient til Aula's login og data-API."""

    def __init__(
        self,
        access_token: str,
        csrf_token: str = "",
        session_cookie: str = "",
        timeout: float = 20.0,
        api_version: int = API_VERSION_DEFAULT,
    ) -> None:
        self.access_token = access_token
        self.csrf_token = csrf_token
        self.session_cookie = session_cookie
        self.api_version = api_version
        self._timeout = timeout
        self._context_ready = False
        self._http: Optional[httpx.AsyncClient] = None

    def _client(self) -> httpx.AsyncClient:
        """Én HTTP-klient pr. Aula-klient, så cookiesne lever sammen.

        Aula sætter PHPSESSID på ét svar og Csrp-Token på et andet, og
        kalenderens POST afvises med 403, hvis de ikke sendes i samme
        cookie-jar. Derfor genbruges klienten i stedet for at åbne en ny
        for hvert kald.
        """
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                headers=self._headers(),
                follow_redirects=True,
            )
            if self.session_cookie:
                self._http.cookies.set("PHPSESSID", self.session_cookie)
            if self.csrf_token:
                self._http.cookies.set("Csrfp-Token", self.csrf_token)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _headers(self) -> dict[str, str]:
        headers = dict(APP_HEADERS)
        if self.csrf_token:
            headers["Csrfp-Token"] = self.csrf_token
        return headers

    def _url(self, method: str, params: dict[str, Any]) -> str:
        query = urlencode(
            {"method": method, "access_token": self.access_token, **params}, doseq=True
        )
        return f"{API_HOST}/api/v{self.api_version}/?{query}"

    def _fejl(self, response: httpx.Response) -> AulaError:
        """Oversætter et HTTP-svar til den undtagelse, der passer til."""
        if response.status_code == 410:
            # Aula svarer 410, når en API-version er taget ned. Det er ikke
            # en fejl i opsætningen, så den skal prøves igen på en ny version.
            return AulaApiGone(self.api_version)
        if response.status_code == 404:
            return AulaError(f"Aula har ikke længere metoden {response.request.url.params.get('method')}")
        if response.status_code in (401, 403):
            return AulaAuthError("Aula-sessionen er udløbet. Log ind igen med MitID.")
        return AulaError(f"Aula svarede med HTTP {response.status_code}")

    async def call(
        self, method: str, extra_params: Optional[dict[str, Any]] = None
    ) -> Any:
        """Kører ét `?method=`-kald og returnerer `data`-delen af svaret."""
        try:
            response = await self._client().get(self._url(method, dict(extra_params or {})))
        except httpx.HTTPError as exc:
            raise AulaError(f"Aula svarede ikke: {exc}") from exc

        if response.status_code >= 400:
            raise self._fejl(response)

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

    async def post_call(self, method: str, body: dict[str, Any]) -> Any:
        """Kører et POST-kald med JSON-krop.

        Aula afviser alle POST-metoder med 403, indtil `getProfileContext`
        har valgt den aktive profil, så den kaldes automatisk først.
        """
        await self.establish_context()
        if not self.csrf_token:
            raise AulaError("Aula gav ingen CSRF-token, så kaldet ikke kan godkendes")
        klient = self._client()
        # CSRF-token kan skifte mellem kald, så den sendes med hver gang.
        klient.headers["Csrfp-Token"] = self.csrf_token
        try:
            response = await klient.post(self._url(method, {}), json=body)
        except httpx.HTTPError as exc:
            raise AulaError(f"Aula svarede ikke: {exc}") from exc

        if response.status_code >= 400:
            raise self._fejl(response)
        self._absorb_session(response)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AulaError("Aula svarede med noget der ikke var JSON") from exc
        return payload.get("data") if isinstance(payload, dict) else payload

    def _absorb_session(self, response: httpx.Response) -> None:
        """Gemmer de cookies Aula sætter, så de kan gemmes og genbruges.

        httx sørger selv for at sende dem igen på den samme klient, så her
        er det kun værdierne vi skal huske på tværs af klienter.
        """
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
        """Sætter aktiv profil til forældre, så kaldene giver børnenes data.

        Kaldet sætter også den PHPSESSID og CSRF-token, som Aula kræver til
        de efterfølgende POST-kald, så det kører kun én gang pr. klient.
        """
        if self._context_ready:
            return {}
        data = await self.call("profiles.getProfileContext", {"portalrole": "guardian"})
        self._context_ready = True
        return data if isinstance(data, dict) else {}

    # --- beskeder ----------------------------------------------------------

    async def threads(
        self, active_children_ids: list[str], limit: int = DEFAULT_THREAD_LIMIT
    ) -> list[dict[str, Any]]:
        """Henteste beskedtråde, nyeste først.

        Aula leverer 20 tråde pr. side og ignorerer et bede om mere, så
        vi henter den ene side og lader sync'en tage de næste.
        """
        if not active_children_ids:
            raise AulaError("Aula kender ingen børn endnu")
        data = await self.call(
            "messaging.getThreads",
            {
                "page": 0,
                "sortOn": "date",
                "orderDirection": "desc",
                "activeChildrenIds[]": list(active_children_ids),
            },
        )
        return _rows(data, "threads")

    async def messages(self, thread_id: str, page: int = 0) -> list[dict[str, Any]]:
        """Henteste beskederne i én tråd. Aula kræver også `page`."""
        data = await self.call(
            "messaging.getMessagesForThread", {"threadId": thread_id, "page": page}
        )
        return _rows(data, "messages")

    # --- opslag ------------------------------------------------------------

    async def posts(
        self, institution_profile_ids: list[str], limit: int = DEFAULT_POST_LIMIT
    ) -> list[dict[str, Any]]:
        """Henteste opslag.

        Aula afviser kaldet med 403, hvis der står børne-`profileId` i stedet
        for `institutionProfileId`, og svarer tomt, hvis forældrenes egne
        institutioner ikke er med. Derfor sendes begge dele.
        """
        if not institution_profile_ids:
            raise AulaError("Aula kender ingen børne-profiler endnu")
        data = await self.call(
            "posts.getAllPosts",
            {
                "parent": "profile",
                "index": 0,
                "size": limit,
                "institutionProfileIds[]": list(institution_profile_ids),
            },
        )
        return _rows(data, "posts")

    # --- kalender ----------------------------------------------------------

    async def calendar_events(
        self,
        institution_profile_ids: list[str],
        from_date: str,
        to_date: str,
    ) -> list[dict[str, Any]]:
        """Henter kalenderposter for de valgte børn i et datointerval.

        Kalenderen er et POST-kald med datoer som ISO-strenge, i modsætning
        til de ældre `childrensschedule`-metoder, som Aula har fjernet.
        """
        if not institution_profile_ids:
            raise AulaError("Aula kender ingen børne-profiler endnu")
        return _rows(
            await self.post_call(
                "calendar.getEventsByProfileIdsAndResourceIds",
                {
                    "instProfileIds": list(institution_profile_ids),
                    "resourceIds": [],
                    "start": from_date,
                    "end": to_date,
                },
            )
        )


def _rows(payload: Any, *nøgler: str) -> list[dict[str, Any]]:
    """Aula svarer nogle gange med en liste og andre gange med en pakke."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in (*nøgler, "items", "rows", "result", "data"):
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


def _as_rows(value: Any) -> list[dict[str, Any]]:
    """Aula leverer nogle gange en liste af objekter, andre gange en streng."""
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _person_name(value: Any) -> str:
    """Finder et navn i Aula's forskellige person-objekter.

    Tråde, beskeder og opslag gemmer afsenderen som et objekt med `fullName`
    på tråden og opslaget, men som et indlejret objekt på beskeden.
    """
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    name = _first(value, "fullName", "displayName", "name", "firstName", default="")
    if isinstance(name, str):
        return name.strip()
    return ""


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
        kode, url_state = _traek_kode(code)
        state = url_state or (state or "").strip()
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
        if not kode:
            raise AulaError("Ingen kode modtaget fra Aula")

        scope = self.database.get_setting("aula_login_scope") or SCOPE
        tokens = await self._exchange(
            {
                "grant_type": "authorization_code",
                "code": kode,
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
        try:
            await self._refresh_profiles(client)
        finally:
            await client.aclose()
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
        raw = self.database.get_setting("aula_api_version") or ""
        try:
            version = int(raw)
        except (TypeError, ValueError):
            version = API_VERSION_DEFAULT
        return AulaClient(
            access_token=self.database.get_setting("aula_access_token") or "",
            csrf_token=self.database.get_setting("aula_csrf_token") or "",
            session_cookie=self.database.get_setting("aula_session_cookie") or "",
            api_version=version,
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
        # Begge kald skal prøve en ny version: profilkallet er det allerførste
        # vi gør mod Aula, så en død version rammer dér først.
        await self._with_version_retry(client, client.profiles)
        context = await self._with_version_retry(client, client.establish_context)
        self._persist_session(client)
        children = _children_from_context(context)
        self.database.replace_aula_profiles(children)
        self.database.update_settings(
            {
                "aula_institution_profile_ids": json.dumps(
                    _parent_institution_ids(context), separators=(",", ":")
                )
            }
        )
        return children

    async def _with_version_retry(self, client: AulaClient, handling: Any) -> Any:
        """Kører et kald igen på en ny API-version, hvis den gamle er væk.

        Aula svarer 410 Gone, når en API-version er taget ned. I stedet for
        at fejle prøver vi den næste version og gemmer den, så næste kørsel
        bruger den rigtige med det samme.
        """
        for _ in range(API_VERSION_MAX):
            try:
                return await handling()
            except AulaApiGone:
                næste = client.api_version + 1
                if næste > API_VERSION_MAX:
                    raise AulaError(
                        f"Ingen af Aulas API-versioner op til {API_VERSION_MAX} svarer. "
                        "Aula har sikkert ændret API'et igen."
                    )
                logger.info("Aula API-version %s er væk, prøver v%s", client.api_version, næste)
                client.api_version = næste
                client._context_ready = False
                self.database.update_settings({"aula_api_version": str(næste)})
        raise AulaError("Aula svarer ikke på nogen kendt API-version")

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
        """Samler de `institutionProfileId`, som opslag og kalender kræver.

        Aula afviser kaldet, hvis der står et `profileId`, og svarer tomt,
        hvis forældrenes egne institutioner mangler, så begge dele skal med.
        """
        raw = self.database.get_setting("aula_institution_profile_ids") or "[]"
        try:
            parent_ids = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parent_ids = []
        if not isinstance(parent_ids, list):
            parent_ids = []
        if child_ids:
            valgt = {str(child_id) for child_id in child_ids}
            børn = [
                str(row.get("institution_profile_id") or row["profile_id"])
                for row in self.database.list_aula_profiles()
                if row["profile_id"] in valgt
            ]
        else:
            børn = [
                str(row.get("institution_profile_id") or row["profile_id"])
                for row in self.database.list_aula_profiles()
            ]
        return list(dict.fromkeys([str(value) for value in parent_ids] + børn))

    # --- synk --------------------------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "message": "Aula er ikke logt ind. Log ind med MitID først."}
        client = self._client()
        try:
            children = await self._refresh_profiles(client)
        except AulaAuthError as exc:
            return {"ok": False, "message": str(exc)}
        except (AulaError, httpx.HTTPError) as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            await client.aclose()
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
                    counts["threads"], counts["messages"] = await self._sync_messages(client)
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
            finally:
                await client.aclose()

    async def _sync_messages(self, client: AulaClient) -> tuple[int, int]:
        """Gemmer beskedtrådene. Trådenes fulde beskeder hentes kun for nye tråde.

        Returnerer (antal tråde,antal beskeder).
        """
        client = await self._ensure_fresh_token(client)
        threads = await self._with_version_retry(client, lambda: client.threads(self._child_ids()))
        known = {row["thread_id"] for row in self.database.list_aula_threads()}
        stored = 0
        beskeder_talt = 0
        for thread in threads[:DEFAULT_THREAD_LIMIT]:
            thread_id = str(_first(thread, "threadId", "id", "guid", default=""))
            if not thread_id:
                continue
            # Aula giver `read`, altså modsat af ulæst.
            ulæst = thread.get("read")
            unread = (
                not bool(ulæst)
                if ulæst is not None
                else bool(_first(thread, "unread", "isUnread", default=0))
            )
            self.database.upsert_aula_thread(
                thread_id=thread_id,
                subject=str(_first(thread, "subject", "title", default="(uden emne)")),
                sender=_person_name(thread.get("creator")),
                received_at=_parse_datetime(
                    _first(
                        thread,
                        "startedTime",
                        "date",
                        "timestamp",
                        "lastMessageDate",
                        "receivedDate",
                    )
                ),
                participants=[
                    str(_first(child, "name", "displayName", default=""))
                    for child in _as_rows(thread.get("regardingChildren"))
                ],
                unread=unread,
            )
            stored += 1
            # Beskederne i en tråd hentes kun første gang, så tælleformen
            # på en gammel tråd ikke hentes igen for hver synkronisering.
            if thread_id not in known:
                try:
                    messages = await self._with_version_retry(
                        client, lambda: client.messages(thread_id)
                    )
                except AulaError:
                    messages = []
                for message in messages:
                    message_id = str(_first(message, "messageId", "id", "guid", default=""))
                    if not message_id:
                        continue
                    self.database.upsert_aula_message(
                        message_id=message_id,
                        thread_id=thread_id,
                        sender=_person_name(message.get("sender")),
                        body=_content_text(_first(message, "text", "content", "body", "message")),
                        sent_at=_parse_datetime(_first(message, "sendDateTime", "date", "timestamp")),
                        is_from_me=bool(_first(message, "isOwn", "isFromMe", default=False)),
                        attachments=_as_rows(message.get("attachments")),
                    )
                    beskeder_talt += 1
                # Tråden er nu hentet, så næste kørsel springer over den.
                self.database.mark_aula_thread_fetched(thread_id)
        self._persist_session(client)
        return stored, beskeder_talt

    def _child_ids(self) -> list[str]:
        """Børnenes `institutionProfileId`, som `messaging.getThreads` vil have."""
        return [
            str(row.get("institution_profile_id") or row["profile_id"])
            for row in self.database.list_aula_profiles()
        ]

    async def _sync_posts(self, client: AulaClient, profile_ids: list[str]) -> int:
        client = await self._ensure_fresh_token(client)
        posts = await self._with_version_retry(client, lambda: client.posts(profile_ids))
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
                author=_person_name(post.get("ownerProfile")),
                body=content,
                published_at=_parse_datetime(_first(post, "publishAt", "timestamp", "date")),
                audience=[
                    str(_first(row, "name", "displayName", default=""))
                    for row in _as_rows(_first(post, "relatedProfiles", "audience"))
                ],
                tags=_as_list(_first(post, "tags", "keywords")),
                unread=not _is_read_flag(post),
            )
            stored += 1
        self._persist_session(client)
        return stored

    async def _sync_calendar(self, client: AulaClient, profile_ids: list[str]) -> int:
        """Henter Aula-kalenderen. Fejl her må ikke stoppe beskeder og opslag."""
        client = await self._ensure_fresh_token(client)
        today = datetime.now(timezone.utc)
        stored = 0
        events_map: dict[str, dict[str, Any]] = {}

        # Aula kan være striks på store intervaller, så vi deler op i måneder
        # for at få alle events uden at ramme sessiongrænser.
        current = today - timedelta(days=DEFAULT_CALENDAR_DAYS_BACK)
        end = today + timedelta(days=DEFAULT_CALENDAR_DAYS_FORWARD)

        while current < end:
            window_end = current + timedelta(days=30)
            if window_end > end:
                window_end = end
            from_date = current.replace(microsecond=0).isoformat()
            to_date = window_end.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

            try:
                events = await self._with_version_retry(
                    client, lambda: client.calendar_events(profile_ids, from_date, to_date)
                )
            except AulaError as exc:
                logger.info("Aula kalender kunne ikke hentes: %s", exc)
                return stored

            for event in events:
                event_id = str(_first(event, "id", "meetingId", "guid", default=""))
                if not event_id:
                    continue
                if event_id in events_map:
                    continue
                events_map[event_id] = event

            current = window_end + timedelta(days=1)

        for event in events_map.values():
            start = _parse_datetime(
                _first(event, "startDateTime", "startDate", "start", "meetingDate", "begin")
            )
            end_dt = _parse_datetime(_first(event, "endDateTime", "endDate", "end", default=None))
            self.database.upsert_aula_event(
                event_id=str(_first(event, "id", "meetingId", "guid", default="")),
                title=str(_first(event, "title", "subject", "name", default="(uden titel)")),
                description=_content_text(_first(event, "description", "content", default="")),
                location=str(
                    _first(event, "primaryResourceText", "location", "room", default="") or ""
                ),
                start_at=start,
                end_at=end_dt or start,
                category=str(_first(event, "type", "category", default="")),
                profile_ids=_as_list(
                    _first(event, "belongsToProfiles", "institutionProfileIds", "profiles")
                ),
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
    """Plukker børnene ud af én institution.

    Aula giver hvert barn to id'er: `profileId` er barnet selv, og `id` er
    barnets `institutionProfileId`. Kun det sidste accepterer Aula i opslag
    og kalender, så begge gemmes.
    """
    children = institution.get("children")
    if not isinstance(children, list):
        return []
    rows: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        profile_id = _first(child, "profileId", "profile_id")
        institution_profile_id = _first(child, "id", "institutionProfileId")
        if profile_id in (None, "") and institution_profile_id in (None, ""):
            continue
        rows.append(
            {
                "profile_id": str(profile_id or institution_profile_id),
                "institution_profile_id": str(institution_profile_id or profile_id),
                "name": str(_first(child, "name", "displayName", "firstName", default="Barn")),
                "institution": "",
            }
        )
    return rows


def _parent_institution_ids(context: Any) -> list[str]:
    """Findes forældrenes egne institutionProfileId.

    Aula svarer tomt på opslag, medmindre forældrenes institutioner også er
    med i kaldet, så de skal ind sammen med børnenes.
    """
    if not isinstance(context, dict):
        return []
    ids: list[str] = []
    egen = context.get("institutionProfile")
    if isinstance(egen, dict):
        value = _first(egen, "id", "institutionProfileId")
        if value not in (None, ""):
            ids.append(str(value))
    for institution in context.get("institutions") or []:
        if not isinstance(institution, dict):
            continue
        value = _first(institution, "institutionProfileId", "id")
        if value not in (None, ""):
            ids.append(str(value))
    return list(dict.fromkeys(ids))
