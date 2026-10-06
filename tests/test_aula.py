import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.aula import (
    AulaApiGone,
    AulaAuthError,
    AulaClient,
    AulaError,
    AulaSync,
    _children_from_context,
    _parse_datetime,
    _pkce_pair,
    _strip_html,
    _traek_kode,
)
from app.db import Database


@pytest.fixture()
def aula_db(tmp_path):
    return Database(tmp_path / "aula.db")


def aula_response(status, json=None, cookie=None):
    """httpx-svar med request vedhæftet, så cookies kan læses som i virkeligheden."""
    headers = {"set-cookie": cookie} if cookie else None
    return httpx.Response(
        status, json=json, headers=headers,
        request=httpx.Request("GET", "https://www.aula.dk/api/v23/"),
    )


def swap_aula(database):
    """Bytter database i main, så API-routes taler med testens database."""
    old_database = main.database
    old_synchronizer = main.synchronizer.database
    old_aula = main.aula
    main.database = database
    main.synchronizer.database = database
    main.aula = AulaSync(database)
    return old_database, old_synchronizer, old_aula


def restore_aula(saved):
    main.database, main.synchronizer.database, main.aula = saved


def auth_headers(client):
    assert client.post("/api/auth/login", json={"password": "family"}).status_code == 200
    csrf = client.cookies.get("fd_csrf")
    assert csrf
    return {"X-FD-CSRF": csrf}


# --- hjælpefunktioner ------------------------------------------------------


def test_pkce_pair_is_url_safe_and_verifiable():
    verifier, challenge = _pkce_pair()
    assert "=" not in verifier and "=" not in challenge
    assert verifier and challenge
    assert verifier != challenge
    # En anden login må ikke få samme verifikationsstreng.
    assert _pkce_pair()[0] != verifier


def test_strip_html_giver_rent_tekst():
    html = "<p>Hej <b>Emil</b></p><p>Vi m&oslash;des i morgen</p><script>alert(1)</script>"
    text = _strip_html(html)
    assert "<" not in text
    assert "alert" not in text
    assert "Hej Emil" in text
    # Aula bruger entity'er for de danske bogstaver, så de skal oversættes.
    assert "mødes" in text
    assert _strip_html(None) == ""


def test_parse_datetime_håndterer_sekunder_millisOgIso():
    assert _parse_datetime(1_700_000_000).startswith("2023-11-14")
    # Millisekunder skal give samme svar som sekunder.
    assert _parse_datetime(1_700_000_000_000) == _parse_datetime(1_700_000_000)
    assert _parse_datetime("2026-10-05T08:00:00Z") == "2026-10-05T08:00:00+00:00"
    # Uden tidszone antager vi UTC, ellers ville tidsstempler hoppe i filtrene.
    assert _parse_datetime("2026-10-05T08:00:00") == "2026-10-05T08:00:00+00:00"
    assert _parse_datetime("ikke et tidspunkt") is None
    assert _parse_datetime(None) is None
    assert _parse_datetime("") is None


def test_children_from_context_finder_børn_i_institutioner():
    context = {
        "institutions": [
            {
                "name": "Skole Nord",
                "children": [
                    {"institutionProfileId": 111, "name": "Emil"},
                    {"profileId": 222, "name": "Mia"},
                ],
            }
        ]
    }
    children = _children_from_context(context)
    assert [child["profile_id"] for child in children] == ["111", "222"]
    assert all(child["institution"] == "Skole Nord" for child in children)
    assert _children_from_context({}) == []
    assert _children_from_context("noget andet") == []


# --- database --------------------------------------------------------------


def test_aula_profiles_erstattes_ved_nyt_login(aula_db):
    aula_db.replace_aula_profiles([{"profile_id": "111", "name": "Emil", "institution": "Nord"}])
    assert [row["name"] for row in aula_db.list_aula_profiles()] == ["Emil"]
    # Et nyt login må fjerne børn, der ikke længere er på kontoen.
    aula_db.replace_aula_profiles([{"profile_id": "222", "name": "Mia", "institution": "Nord"}])
    assert [row["profile_id"] for row in aula_db.list_aula_profiles()] == ["222"]


def test_stjerne_og_læst_marker_surviver_sync(aula_db):
    aula_db.upsert_aula_thread(
        thread_id="t1", subject="Hej", sender="Lærer", received_at="2026-10-05T08:00:00+00:00",
        participants=["Emil"], unread=True,
    )
    assert aula_db.set_aula_thread_starred("t1", True)
    assert aula_db.set_aula_thread_read("t1", True)
    # En ny synk må ikke slette det, brugeren selv har sat.
    aula_db.upsert_aula_thread(
        thread_id="t1", subject="Hej", sender="Lærer", received_at="2026-10-05T09:00:00+00:00",
        participants=["Emil"], unread=True,
    )
    thread = aula_db.get_aula_thread("t1")
    assert thread["starred"] is True
    assert thread["is_unread"] is False
    assert thread["received_at"] == "2026-10-05T09:00:00+00:00"
    assert thread["participants"] == ["Emil"]
    # Ukendt tråd skal give False, så API'et kan svare 404.
    assert aula_db.set_aula_thread_starred("findes-ikke", True) is False


def test_ulæst_fra_aula_tæller_når_brugeren_ikke_har_læst(aula_db):
    aula_db.upsert_aula_thread(
        thread_id="t2", subject="Uden svar", sender="Lærer", received_at=None, participants=[], unread=True,
    )
    assert aula_db.get_aula_thread("t2")["is_unread"] is True
    # Brugeren kan også markere noget ulæst igen.
    assert aula_db.set_aula_thread_read("t2", True)
    assert aula_db.set_aula_thread_read("t2", False)
    assert aula_db.get_aula_thread("t2")["is_unread"] is True


def test_beskeder_knyttes_til_tråd_og_sorteres(aula_db):
    aula_db.upsert_aula_message(
        message_id="m2", thread_id="t1", sender="Mig", body="Svælg", sent_at="2026-10-05T09:00:00+00:00", is_from_me=True,
    )
    aula_db.upsert_aula_message(
        message_id="m1", thread_id="t1", sender="Lærer", body="Hej", sent_at="2026-10-05T08:00:00+00:00", is_from_me=False,
    )
    messages = aula_db.list_aula_messages("t1")
    assert [message["message_id"] for message in messages] == ["m1", "m2"]
    assert messages[1]["is_from_me"] is True
    assert aula_db.list_aula_messages("anden-tråd") == []


def test_clear_aula_items_tømmer_cache_men_beholder_børn(aula_db):
    aula_db.replace_aula_profiles([{"profile_id": "111", "name": "Emil", "institution": "Nord"}])
    aula_db.upsert_aula_post(
        post_id="p1", title="Opslag", author="Lærer", body="Tekst", published_at=None, audience=[], tags=[], unread=True,
    )
    aula_db.clear_aula_items()
    assert aula_db.list_aula_posts() == []
    assert len(aula_db.list_aula_profiles()) == 1


# --- klient ----------------------------------------------------------------


def test_client_absorberer_session_og_csrf():
    client = AulaClient(access_token="token")
    seen = {}

    def handler(url, **_):
        seen["url"] = url
        return aula_response(200, {"data": {"ok": True}}, cookie="PHPSESSID=abc123; Path=/")

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=handler)):
        data = asyncio.run(client.call("profiles.getProfilesByLogin"))
    assert data == {"ok": True}
    assert client.session_cookie == "abc123"
    assert "method=profiles.getProfilesByLogin" in seen["url"]
    assert "access_token=token" in seen["url"]
    # Aula afviser klienter der ikke ligner deres egen app.
    assert client._headers()["User-Agent"] == "Android"


def test_client_behandler_401_som_udlobet_login():
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=aula_response(401))):
        with pytest.raises(AulaAuthError):
            asyncio.run(AulaClient(access_token="token").call("messaging.getThreads"))


def test_client_oversaetter_401_ved_aula_error():
    payload = {"error": "invalid_token", "error_description": "Token expired"}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=aula_response(200, payload))):
        with pytest.raises(AulaAuthError):
            asyncio.run(AulaClient(access_token="token").call("messaging.getThreads"))


def test_client_ved_networkfejl_bliver_aula_error():
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("nede"))):
        with pytest.raises(AulaError):
            asyncio.run(AulaClient(access_token="token").call("messaging.getThreads"))


# --- login -----------------------------------------------------------------


def test_login_url_indholder_pkce_og_uder_state_og_verifier(aula_db):
    sync = AulaSync(aula_db)
    result = asyncio.run(sync.login_url())
    assert result["url"].startswith("https://login.aula.dk/")
    assert "code_challenge_method=S256" in result["url"]
    assert "code_challenge=" in result["url"]
    assert f"state={result['state']}" in result["url"]
    # Verifieren skal ligge i databasen, men aldrig i linket.
    assert aula_db.get_setting("aula_login_verifier")
    assert aula_db.get_setting("aula_login_verifier") not in result["url"]


def test_login_med_mitid_scopes_korrekt(aula_db):
    sync = AulaSync(aula_db)
    result = asyncio.run(sync.login_url("aula-sensitive"))
    assert "scope=aula-sensitive" in result["url"]
    assert "_99949a54b8b65423862aac1bf629599ed64231607a" in result["url"]
    assert aula_db.get_setting("aula_login_scope") == "aula-sensitive"


def test_komplet_login_uden_start_gives_klar_fejl(aula_db):
    sync = AulaSync(aula_db)
    with pytest.raises(AulaAuthError):
        asyncio.run(sync.complete_login("some-code"))


def test_komplet_login_med_forkert_state_afvises(aula_db):
    sync = AulaSync(aula_db)
    asyncio.run(sync.login_url())
    with pytest.raises(AulaAuthError):
        asyncio.run(sync.complete_login("some-code", "en-anden-state"))


def test_komplet_login_udveksler_kode_og_gemmer_tokens(aula_db):
    async def scenario():
        sync = AulaSync(aula_db)
        started = await sync.login_url()
        with patch.object(sync, "_exchange", new=AsyncMock(return_value={
            "access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
        })), patch.object(sync, "_refresh_profiles", new=AsyncMock(return_value=[])), patch.object(
            sync, "sync", new=AsyncMock(return_value={"threads": 0, "posts": 0})
        ):
            result = await sync.complete_login("kode-fra-aula", started["state"])
        return result

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert aula_db.get_setting("aula_access_token") == "access-1"
    assert aula_db.get_setting("aula_refresh_token") == "refresh-1"
    assert aula_db.get_setting("aula_token_expires")
    # Login-hjælpen skal ryddes, så et gammelt link ikke kan bruges igen.
    assert aula_db.get_setting("aula_login_verifier") == ""
    assert aula_db.get_setting("aula_login_state") == ""
    assert AulaSync(aula_db).configured() is True


# --- det brugeren indsætter -------------------------------------------------

# MitID-login'en lander på app-redirect.aula.dk, som er en mellemside til
# Aulas mobilapp. Den har koden base64-kodet i `returnUri`, og "Fortsæt
# login"-knappen fører videre til Aulas egen webapp, som ikke kan bruges
# herfra. Sådan ser en af de rigtige adresser ud.
#
# Adressen fremstilles syntetisk i stedet for at blive kopieret fra en rigtig
# login, så ingen brugbare engangskoder ender i git.
_SYNTETISK_KODE = "deadbeefcafe" * 74 + "dead"  # 892 tegn, som i virkeligheden
_SYNTETISK_STATE = "0123456789abcdef0123456789abcd"
EN_MELLEMSIDE = (
    "https://app-redirect.aula.dk/?returnUri="
    + base64.b64encode(
        f"https://app-private.aula.dk/?code={_SYNTETISK_KODE}&state={_SYNTETISK_STATE}".encode()
    ).decode()
)


def test_kode_læses_ud_app_redirect_adressen():
    kode, state = _traek_kode(EN_MELLEMSIDE)
    assert kode == _SYNTETISK_KODE
    assert len(kode) == 892
    assert state == _SYNTETISK_STATE


def test_mellemside_adressen_indholder_ingen_rigtige_tokens():
    """Adressen i testene skal være syntetisk, ikke kopieret fra en rigtig login."""
    assert "eyJ" not in EN_MELLEMSIDE
    assert _SYNTETISK_KODE.startswith("deadbeef")


def test_kode_læses_ud_app_private_adressen():
    adresse = "https://app-private.aula.dk/?code=deadbeefcafe&state=abc123"
    assert _traek_kode(adresse) == ("deadbeefcafe", "abc123")


def test_kode_og_state_som_query_uden_scheme():
    assert _traek_kode("code=deadbeefcafe&state=abc123") == ("deadbeefcafe", "abc123")


def test_bare_kode_gives_uaendret_tilbage():
    assert _traek_kode("  deadbeefcafe \n") == ("deadbeefcafe", "")


def test_adresse_kopieret_med_omkringende_tegn_virker():
    """ Kopierer man fra en chat eller et link, kan der være citater omkring."""
    adresse = "https://app-private.aula.dk/?code=deadbeefcafe&state=abc123"
    assert _traek_kode(f'"{adresse}"') == ("deadbeefcafe", "abc123")


def test_ugyldig_base64_giver_tom_kode_og_ingen_fejl():
    assert _traek_kode("https://app-redirect.aula.dk/?returnUri=%%%") == ("", "")
    assert _traek_kode("") == ("", "")
    assert _traek_kode(None) == ("", "")


def test_plus_i_adressen_er_stadig_en_kode():
    """'+' bliver til et mellemrum i en query-streng, som app-redirect gør."""
    kode = "abc" * 100  # 300 tegn -> base64 med '+' i sig
    adresse = _mellemside(kode, "stat")
    assert "+" in adresse or "/" in adresse
    hvervet = adresse.replace("+", "%20")
    assert _traek_kode(hvervet) == (kode, "stat")


def _mellemside(kode: str, state: str) -> str:
    """Bygger den adresse app-redirect.aula.dk sender brugeren videre til."""
    indre = f"https://app-private.aula.dk/?code={kode}&state={state}"
    return "https://app-redirect.aula.dk/?returnUri=" + base64.b64encode(indre.encode()).decode()


def test_komplet_login_godtar_hele_adressen_fra_mellemsiden(aula_db):
    """Brugeren skal kunne indsætte adressen han lander på, ukodet."""
    async def scenario():
        sync = AulaSync(aula_db)
        started = await sync.login_url()
        byttet = {}

        async def fake_udveksling(payload):
            byttet.update(payload)
            return {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}

        with patch.object(sync, "_exchange", new=AsyncMock(side_effect=fake_udveksling)), patch.object(
            sync, "_refresh_profiles", new=AsyncMock(return_value=[])
        ), patch.object(sync, "sync", new=AsyncMock(return_value={"threads": 0, "posts": 0})):
            adresse = _mellemside("kode-fra-mellemsiden", started["state"])
            assert (await sync.complete_login(adresse))["ok"] is True
        return byttet

    byttet = asyncio.run(scenario())
    # Rå-adressen må ikke sendes videre som kode, kun den kode den indeholder.
    assert byttet["code"] == "kode-fra-mellemsiden"
    assert byttet["code_verifier"]
    assert aula_db.get_setting("aula_access_token") == "access-1"
    assert AulaSync(aula_db).configured() is True


def test_komplet_login_afviser_state_fra_en_anden_gammel_adresse(aula_db):
    sync = AulaSync(aula_db)
    asyncio.run(sync.login_url())
    gammel = "https://app-private.aula.dk/?code=deadbeef&state=state-fra-et-andet-login"
    with pytest.raises(AulaAuthError):
        asyncio.run(sync.complete_login(gammel))


# --- synkronisering --------------------------------------------------------


def fake_client(**overrides):
    """Aula-klient med forudbestemte svar, så intet går på nettet.

    Svarene er klippet ud af rigtige v24-svar, så testene passer på de
    feltnavne Aula faktisk leverer.
    """
    defaults = {
        "profiles": AsyncMock(return_value=[{"institutionProfileId": 111, "name": "Emil"}]),
        "establish_context": AsyncMock(return_value={
            "institutionProfile": {"id": 100, "profileId": 90},
            "institutions": [
                {
                    "institutionProfileId": 100,
                    "name": "Nord Skole",
                    "children": [{"id": 111, "profileId": 222, "name": "Emil"}],
                }
            ],
        }),
        "threads": AsyncMock(return_value=[
            {
                "id": "t1",
                "subject": "Hej",
                "startedTime": "2026-10-05T08:00:00+00:00",
                "read": False,
                "creator": {"fullName": "Lærer Lise", "mailBoxOwner": {"portalRole": "employee"}},
                "regardingChildren": [{"profileId": 222, "name": "Emil"}],
            },
        ]),
        "messages": AsyncMock(return_value=[
            {
                "id": "m1",
                "sendDateTime": "2026-10-05T08:00:00+00:00",
                "sender": {"fullName": "Lærer Lise", "shortName": "LL"},
                "text": {"html": "<p>Hej med dig</p>"},
            },
        ]),
        "posts": AsyncMock(return_value=[
            {
                "id": "p1",
                "title": "Ture i morgen",
                "publishAt": "2026-10-05T07:00:00+00:00",
                "ownerProfile": {"fullName": "Lærer Lise"},
                "content": "<p>Medtag regntøj</p>",
            },
        ]),
        "calendar_events": AsyncMock(return_value=[
            {
                "id": "e1",
                "title": "Matematik",
                "startDateTime": "2026-10-06T08:00:00+00:00",
                "endDateTime": "2026-10-06T08:45:00+00:00",
                "primaryResourceText": "1. sal",
                "type": "event",
                "belongsToProfiles": [111],
            },
        ]),
    }
    defaults.update(overrides)
    client = AulaClient(access_token="token")
    for name, value in defaults.items():
        setattr(client, name, value)
    return client


def test_sync_gemmer_beskeder_opslag_og_kalender(aula_db):
    sync = AulaSync(aula_db)
    aula_db.update_settings({"aula_refresh_token": "refresh-1", "aula_enabled": "true"})
    client = fake_client()
    with patch.object(AulaSync, "_client", return_value=client):
        result = asyncio.run(sync.sync())

    assert result["synced"] is True
    assert result["threads"] == 1
    threads = aula_db.list_aula_threads()
    assert threads[0]["subject"] == "Hej"
    assert threads[0]["is_unread"] is True
    # Beskederne i tråden hentes kun første gang, så et gammelt svar springes over.
    assert len(aula_db.list_aula_messages("t1")) == 1
    client.messages.assert_awaited_once()

    post = aula_db.list_aula_posts()[0]
    assert post["title"] == "Ture i morgen"
    # Aula leverer opslag som et objekt med html, ikke som ren tekst.
    assert post["body"] == "Medtag regntøj"
    assert post["is_unread"] is True

    event = aula_db.list_aula_events()[0]
    assert event["title"] == "Matematik"
    assert event["profile_ids"] == ["111"]
    assert event["start_at"] == "2026-10-06T08:00:00+00:00"

    assert [child["name"] for child in aula_db.list_aula_profiles()] == ["Emil"]
    assert aula_db.get_setting("aula_last_sync")
    assert aula_db.get_setting("aula_last_error") == ""


def test_anden_synk_henter_ikke_trådens_beskeder_igen(aula_db):
    sync = AulaSync(aula_db)
    aula_db.update_settings({"aula_refresh_token": "refresh-1"})
    with patch.object(AulaSync, "_client", return_value=fake_client()):
        asyncio.run(sync.sync())
    client = fake_client()
    with patch.object(AulaSync, "_client", return_value=client):
        asyncio.run(sync.sync())
    client.messages.assert_not_awaited()
    assert len(aula_db.list_aula_messages("t1")) == 1


def test_kalenderfejl_tager_ikke_beskeder_med(aula_db):
    sync = AulaSync(aula_db)
    aula_db.update_settings({"aula_refresh_token": "refresh-1"})
    client = fake_client(calendar_events=AsyncMock(side_effect=AulaError("kalender lukket")))
    with patch.object(AulaSync, "_client", return_value=client):
        result = asyncio.run(sync.sync())

    assert result["synced"] is True
    assert result["events"] == 0
    assert aula_db.list_aula_threads()
    assert aula_db.list_aula_posts()
    assert aula_db.get_setting("aula_last_error") == ""


def test_udlobet_token_giver_klar_til_nyt_login(aula_db):
    sync = AulaSync(aula_db)
    aula_db.update_settings({"aula_refresh_token": "refresh-1"})
    client = fake_client()
    client.profiles = AsyncMock(side_effect=AulaAuthError("udløbet"))
    with patch.object(AulaSync, "_client", return_value=client):
        result = asyncio.run(sync.sync())

    assert result["synced"] is False
    assert result["needs_login"] is True
    assert "udløbet" in aula_db.get_setting("aula_last_error")
    # Tokenerne ryddes, så login-skærmen vises i stedet for et tomt skærmbillede.
    assert aula_db.get_setting("aula_refresh_token") == ""
    assert AulaSync(aula_db).configured() is False


def test_sync_uden_login_gør_ingenting(aula_db):
    result = asyncio.run(AulaSync(aula_db).sync())
    assert result == {"synced": False, "reason": "not_configured"}


def test_token_forlænges_når_det_er_til_at_udløbe(aula_db):
    from datetime import datetime, timedelta, timezone

    async def scenario():
        sync = AulaSync(aula_db)
        aula_db.update_settings({
            "aula_access_token": "gammel",
            "aula_refresh_token": "refresh-1",
            "aula_token_expires": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        })
        exchanged = AsyncMock(return_value={
            "access_token": "ny", "refresh_token": "refresh-2", "expires_in": 3600,
        })
        with patch.object(sync, "_exchange", new=exchanged):
            client = await sync._ensure_fresh_token(sync._client())
        return client

    client = asyncio.run(scenario())
    assert client.access_token == "ny"
    assert aula_db.get_setting("aula_access_token") == "ny"
    assert aula_db.get_setting("aula_refresh_token") == "refresh-2"


def test_token_med_masser_tid_tilbage_røres_ikke(aula_db):
    from datetime import datetime, timedelta, timezone

    async def scenario():
        sync = AulaSync(aula_db)
        aula_db.update_settings({
            "aula_access_token": "gyldig",
            "aula_refresh_token": "refresh-1",
            "aula_token_expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        })
        exchange = AsyncMock()
        with patch.object(sync, "_exchange", new=exchange):
            client = await sync._ensure_fresh_token(sync._client())
        exchange.assert_not_awaited()
        return client

    assert asyncio.run(scenario()).access_token == "gyldig"


def test_test_connection_uden_login_svarer_tydeligt(aula_db):
    result = asyncio.run(AulaSync(aula_db).test_connection())
    assert result["ok"] is False
    assert "MitID" in result["message"]


def test_test_connection_navner_børnene(aula_db):
    sync = AulaSync(aula_db)
    aula_db.update_settings({"aula_refresh_token": "refresh-1"})
    with patch.object(AulaSync, "_client", return_value=fake_client()):
        result = asyncio.run(sync.test_connection())
    assert result["ok"] is True
    assert "Emil" in result["message"]


def test_logout_tømmer_token_og_data(aula_db):
    sync = AulaSync(aula_db)
    aula_db.update_settings({"aula_refresh_token": "refresh-1"})
    with patch.object(AulaSync, "_client", return_value=fake_client()):
        asyncio.run(sync.sync())
    asyncio.run(sync.logout())
    assert sync.configured() is False
    assert aula_db.list_aula_threads() == []
    assert aula_db.list_aula_posts() == []


def test_set_starred_og_read_uden_id_giver_fejl(aula_db):
    sync = AulaSync(aula_db)
    with pytest.raises(AulaError):
        sync.set_starred("ukendt", "x", True)
    with pytest.raises(AulaError):
        sync.set_read("event", "e1", True)
    with pytest.raises(AulaError):
        sync.set_starred("thread", "findes-ikke", True)


def test_stjerne_og_læst_via_sync_hjælperne(aula_db):
    sync = AulaSync(aula_db)
    aula_db.upsert_aula_post(
        post_id="p1", title="Opslag", author="Lærer", body="", published_at=None, audience=[], tags=[], unread=True,
    )
    assert sync.set_starred("post", "p1", True)["starred"] is True
    assert sync.set_read("post", "p1", True)["read"] is True
    post = aula_db.list_aula_posts()[0]
    assert post["starred"] is True
    assert post["is_unread"] is False


# --- API -------------------------------------------------------------------


def test_api_aula_er_tom_inden_login(tmp_path):
    saved = swap_aula(Database(tmp_path / "aula-api.db"))
    try:
        with TestClient(main.app) as client:
            response = client.get("/api/aula", headers=auth_headers(client))
            assert response.status_code == 200
            data = response.json()
            assert data["configured"] is False
            assert data["threads"] == [] and data["posts"] == [] and data["events"] == []
            assert data["unread_threads"] == 0
    finally:
        restore_aula(saved)


def test_api_aula_viser_cachede_data_og_ulæste(tmp_path):
    database = Database(tmp_path / "aula-api2.db")
    saved = swap_aula(database)
    try:
        with TestClient(main.app) as client:
            headers = auth_headers(client)
            started = client.post("/api/aula/login/start", headers=headers).json()
            with patch.object(main.aula, "_exchange", new=AsyncMock(return_value={
                "access_token": "a", "refresh_token": "r", "expires_in": 3600,
            })), patch.object(main.aula, "_client", return_value=fake_client()):
                done = client.post(
                    "/api/aula/login/complete",
                    headers=headers,
                    json={"code": "kode", "state": started["state"]},
                )
            assert done.status_code == 200

            data = client.get("/api/aula").json()
            assert data["configured"] is True
            assert [child["name"] for child in data["children"]] == ["Emil"]
            assert data["threads"][0]["subject"] == "Hej"
            assert data["unread_threads"] == 1
            assert data["unread_posts"] == 1
            assert data["last_sync"]
            assert data["last_error"] == ""

            messages = client.get("/api/aula/threads/t1/messages").json()
            assert messages["messages"][0]["body"] == "Hej med dig"
    finally:
        restore_aula(saved)


def test_api_aula_gemner_tokens_og_verifier_i_settings(tmp_path):
    database = Database(tmp_path / "aula-api3.db")
    saved = swap_aula(database)
    try:
        with TestClient(main.app) as client:
            auth_headers(client)
            database.update_settings({
                "aula_access_token": "hemmelighed",
                "aula_refresh_token": "hemmelighed2",
                "aula_csrf_token": "hemmelighed3",
                "aula_session_cookie": "hemmelighed4",
                "aula_login_verifier": "hemmelighed5",
            })
            settings = client.get("/api/dashboard/summary").json()["settings"]
            assert settings["aula_configured"] is True
            for key in (
                "aula_access_token", "aula_refresh_token", "aula_csrf_token",
                "aula_session_cookie", "aula_login_verifier",
            ):
                assert settings[key] == ""
    finally:
        restore_aula(saved)


def test_api_stjerne_og_læst(tmp_path):
    database = Database(tmp_path / "aula-api4.db")
    saved = swap_aula(database)
    try:
        with TestClient(main.app) as client:
            headers = auth_headers(client)
            database.update_settings({"aula_refresh_token": "refresh-1"})
            with patch.object(main.aula, "_client", return_value=fake_client()):
                client.post("/api/aula/sync", headers=headers, json={})

            starred = client.post("/api/aula/post/p1/star", headers=headers, json={"starred": True})
            assert starred.status_code == 200
            assert starred.json()["starred"] is True

            read = client.post("/api/aula/post/p1/read", headers=headers, json={"read": True})
            assert read.status_code == 200
            assert read.json()["read"] is True

            post = database.list_aula_posts()[0]
            assert post["starred"] is True
            assert post["is_unread"] is False

            missing = client.post("/api/aula/post/ukendt/star", headers=headers, json={"starred": True})
            assert missing.status_code == 404
    finally:
        restore_aula(saved)


def test_api_ulast_type_giver_404(tmp_path):
    saved = swap_aula(Database(tmp_path / "aula-api5.db"))
    try:
        with TestClient(main.app) as client:
            headers = auth_headers(client)
            response = client.post("/api/aula/trompet/x1/star", headers=headers, json={"starred": True})
            assert response.status_code == 404
    finally:
        restore_aula(saved)


def test_api_synk_uden_login_melder_det_tydeligt(tmp_path):
    saved = swap_aula(Database(tmp_path / "aula-api6.db"))
    try:
        with TestClient(main.app) as client:
            headers = auth_headers(client)
            result = client.post("/api/aula/sync", headers=headers, json={}).json()
            assert result == {"synced": False, "reason": "not_configured"}
    finally:
        restore_aula(saved)


def test_api_login_start_kan_vælge_mitid_scope(tmp_path):
    saved = swap_aula(Database(tmp_path / "aula-api7.db"))
    try:
        with TestClient(main.app) as client:
            headers = auth_headers(client)
            result = client.post("/api/aula/login/start?scope=aula-sensitive", headers=headers).json()
            assert "scope=aula-sensitive" in result["url"]
    finally:
        restore_aula(saved)


def test_api_komplet_login_med_forkert_kode_giver_400(tmp_path):
    saved = swap_aula(Database(tmp_path / "aula-api8.db"))
    try:
        with TestClient(main.app) as client:
            headers = auth_headers(client)
            response = client.post("/api/aula/login/complete", headers=headers, json={"code": "x"})
            assert response.status_code == 400
    finally:
        restore_aula(saved)


def test_api_logout_rydder_alt(tmp_path):
    database = Database(tmp_path / "aula-api9.db")
    saved = swap_aula(database)
    try:
        with TestClient(main.app) as client:
            headers = auth_headers(client)
            database.update_settings({"aula_refresh_token": "refresh-1"})
            with patch.object(main.aula, "_client", return_value=fake_client()):
                client.post("/api/aula/sync", headers=headers, json={})
            assert client.post("/api/aula/logout", headers=headers).status_code == 200
            assert client.get("/api/aula").json()["configured"] is False
    finally:
        restore_aula(saved)


# --- Aula API v24 ---------------------------------------------------------
#
# Aula trak api/v23 tilbage og svarer 410 Gone på den. Det sker i praksis
# hver gang en installation med en gammel gemt version rammer en nyere
# Aula-backend, så det er en fejl at blive hængt fast i en version:
# klienten skal finde den nyeste selv og huske resultatet.


def test_410_giver_en_versionsfejl_ikke_en_loginfejl():
    """410 betyder "udgået version", ikke "log ind igen".

    Hvis 410 blev behandlet som en udløbet login, ville brugeren blive bedt om
    at logge ind igen, hver gang Aula har opdateret sit API.
    """
    client = AulaClient(access_token="token", api_version=23)
    response = aula_response(410, {"status": {"code": 10, "message": "Version gone"}})

    fejl = client._fejl(response)

    assert isinstance(fejl, AulaApiGone)
    assert not isinstance(fejl, AulaAuthError)
    assert fejl.version == 23


def test_401_og_403_giver_loginfejl(aula_db):
    """Et udløbet token skal derimod bede om nyt login."""
    client = AulaClient(access_token="token")
    for status in (401, 403):
        assert isinstance(client._fejl(aula_response(status)), AulaAuthError)


def _gammel_version(db):
    """Efterligner en installation, der har en død version gemt."""
    db.update_settings(
        {
            "aula_access_token": "token",
            "aula_refresh_token": "refresh",
            "aula_api_version": "23",
        }
    )


def test_gammel_version_bumper_til_næste_og_gemmes(aula_db):
    _gammel_version(aula_db)
    sync = AulaSync(aula_db)
    kald = {"n": 0}

    async def kal():
        kald["n"] += 1
        if kald["n"] == 1:
            raise AulaApiGone(23)
        return {"ok": True}

    resultat = asyncio.run(sync._with_version_retry(sync._client(), kal))

    assert resultat == {"ok": True}
    assert kald["n"] == 2
    assert aula_db.get_setting("aula_api_version") == "24"


def test_flere_udgåede_versioner_skaler_frem_til_en_levende(aula_db):
    """Aula kan have lukket flere versioner på én gang, så vi prøver videre."""
    _gammel_version(aula_db)
    sync = AulaSync(aula_db)
    kald = {"n": 0}

    async def kal():
        kald["n"] += 1
        if kald["n"] <= 3:
            raise AulaApiGone(0)
        return "levende"

    assert asyncio.run(sync._with_version_retry(sync._client(), kal)) == "levende"
    # 23 -> 24 -> 25 -> 26, og den version bliver gemt til næste kørsel.
    assert aula_db.get_setting("aula_api_version") == "26"


def test_aldrig_prøv_ud_over_den_sidste_version(aula_db):
    """Vi må ikke blive ved med at bumpe versioner for evigt."""
    _gammel_version(aula_db)
    sync = AulaSync(aula_db)
    client = sync._client()
    client.api_version = 40

    async def kal():
        raise AulaApiGone(40)

    with pytest.raises(AulaError) as fanget:
        asyncio.run(sync._with_version_retry(client, kal))
    assert not isinstance(fanget.value, AulaAuthError)
    assert "40" in str(fanget.value)


def test_uden_gemt_version_vi_vil_kigge_paa_v24(aula_db):
    """En ny installation skal ramme den version Aula faktisk serverer."""
    assert AulaSync(aula_db)._client().api_version == 24


def test_ogsaa_profilkaldet_prøver_en_ny_version(aula_db):
    """Profilkallet er det første Aula bliver kontaktet med, så det skal
    prøve en ny version ligesom resten."""
    _gammel_version(aula_db)
    sync = AulaSync(aula_db)
    client = sync._client()
    client.profiles = AsyncMock(side_effect=[AulaApiGone(23), [{"institutionProfileId": 1}]])
    client.establish_context = AsyncMock(return_value={"institutions": []})

    asyncio.run(sync._refresh_profiles(client))

    assert aula_db.get_setting("aula_api_version") == "24"


def test_session_og_csrf_lever_i_den_samme_cookie_jar():
    """Kalenderens POST får 403, hvis ikke begge cookies sendes samlet.

    Aula sætter PHPSESSID på ét svar og Csrfp-Token på et andet, så klienten
    skal genbruge én HTTP-klient i stedet for at åbne en ny til hvert kald.
    """
    kaldte = []

    async def handler(request: httpx.Request) -> httpx.Response:
        kaldte.append(request)
        if "profiles.getProfilesByLogin" in str(request.url):
            return httpx.Response(
                200, json={"data": {}},
                headers={"set-cookie": "PHPSESSID=abc123; path=/; secure"},
            )
        return httpx.Response(
            200, json={"data": {"institutionProfile": {"id": 1}}},
            headers={"set-cookie": "Csrfp-Token=tok987; path=/; secure"},
        )

    async def kør():
        client = AulaClient(access_token="token")
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await client.profiles()
        await client.establish_context()
        assert client.session_cookie == "abc123"
        assert client.csrf_token == "tok987"
        assert client._http.cookies.get("PHPSESSID") == "abc123"
        assert client._http.cookies.get("Csrfp-Token") == "tok987"
        await client.aclose()

    asyncio.run(kør())
    assert len(kaldte) == 2


def test_kalender_er_et_post_kald_med_json():
    """getCalendarItems findes ikke i v24 - kalenderen kræver et POST-kald."""
    sete = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sete["body"] = json.loads(request.content)
            sete["csrf"] = request.headers.get("Csrfp-Token")
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200, json={"data": {"institutions": []}},
            headers={"set-cookie": "Csrfp-Token=tok987; path=/"},
        )

    async def kør():
        client = AulaClient(access_token="token")
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await client.calendar_events(["111", "222"], "2026-10-01", "2026-10-31")
        await client.aclose()

    asyncio.run(kør())
    assert sete["body"]["instProfileIds"] == ["111", "222"]
    assert sete["body"]["resourceIds"] == []
    assert sete["body"]["start"] == "2026-10-01"
    assert sete["body"]["end"] == "2026-10-31"
    assert sete["csrf"] == "tok987"


def test_synk_sender_hele_iso_datetime_til_kalenderen(aula_db):
    """Kun datoer gav et tomt svar, så dagene sendes som start og slut på dagen."""
    kal = AsyncMock(return_value=[])

    asyncio.run(AulaSync(aula_db)._sync_calendar(fake_client(calendar_events=kal), ["111"]))

    sendt = kal.await_args.args[1:]
    assert sendt[0].count("T") == 1, "start skal være en hel ISO-datetime"
    assert "T23:59:59" in sendt[1], "slut på dagen skal være med"


def test_opslag_får_både_forældre_og_børne_id(aula_db):
    """Opslag skal have institutionens id med, ellers kommer der intet."""
    aula_db.replace_aula_profiles(
        [{"profile_id": "222", "name": "Emil", "institution_profile_id": "111"}]
    )
    aula_db.update_settings({"aula_institution_profile_ids": '["100", "111"]'})

    kal = AsyncMock(return_value=[])
    client = fake_client(posts=kal)
    sync = AulaSync(aula_db)
    asyncio.run(AulaSync(aula_db)._sync_posts(client, sync._profile_ids()))

    sendte = kal.await_args.args[0] if kal.await_args.args else kal.await_args.kwargs
    if isinstance(sendte, dict):
        sendte = sendte["institutionProfileIds"]

    # Både forældrenes id og barnets egen id skal med, så opslag fra
    # institutionen og fra barnets side begge kommer med.
    assert "100" in sendte
    assert "111" in sendte


def test_barnet_gemmes_med_institution_profile_id(aula_db):
    """Børnene skal have v24's institutionProfileId, ellers fejler opslag."""
    kontekst = {
        "institutionProfile": {"id": 100},
        "institutions": [
            {
                "institutionProfileId": 100,
                "name": "Nord Skole",
                "children": [{"id": 111, "profileId": 222, "name": "Emil"}],
            }
        ],
    }

    assert _children_from_context(kontekst) == [
        {
            "profile_id": "222",
            "name": "Emil",
            "institution_profile_id": "111",
            "institution": "Nord Skole",
        }
    ]

    asyncio.run(AulaSync(aula_db)._refresh_profiles(fake_client()))

    gemt = aula_db.list_aula_profiles()[0]
    assert gemt["profile_id"] == "222"
    assert gemt["institution_profile_id"] == "111"
    # Institutionens id skal også gemmes, det skal opslag og kalender bruge.
    assert "100" in aula_db.get_setting("aula_institution_profile_ids")



def _sync_en_tråd(aula_db, tråd, besked):
    """Kører besked-synkroniseringen med lige pr. ét svar."""
    client = fake_client(threads=AsyncMock(return_value=[tråd]),
                         messages=AsyncMock(return_value=[besked]))
    return asyncio.run(AulaSync(aula_db)._sync_messages(client))


def test_ulæst_udledes_af_read_flaget(aula_db):
    """v24 har ingen `unread` - det er `read` der er sand, når den er læst."""
    tråd = {
        "id": "t1",
        "subject": "Hej",
        "startedTime": "2026-10-05T08:00:00+00:00",
        "read": False,
        "creator": {"fullName": "Lærer Lise"},
        "regardingChildren": [{"profileId": 222, "name": "Emil"}],
    }
    besked = {"id": "m1", "sendDateTime": "2026-10-05T08:00:00+00:00",
              "sender": {"fullName": "Lærer Lise"}, "text": {"html": "<p>Hej</p>"}}

    _sync_en_tråd(aula_db, tråd, besked)
    assert aula_db.list_aula_threads()[0]["unread"] == 1

    # Samme tråd, men markeret som læst.
    aula_db.mark_aula_thread_fetched("t1")
    _sync_en_tråd(aula_db, dict(tråd, read=True), besked)
    assert aula_db.list_aula_threads()[0]["unread"] == 0


def test_beskedsender_kan_være_indlejret_person_object(aula_db):
    """Beskeder har afsenderen i et objekt, tråde har den i et andet felt."""
    tråd = {
        "id": "t1",
        "subject": "Hej",
        "startedTime": "2026-10-05T08:00:00+00:00",
        "creator": {"fullName": "Lærer Lise"},
        "regardingChildren": [{"profileId": 222, "name": "Emil"}],
    }
    besked = {
        "id": "m1",
        "sendDateTime": "2026-10-05T08:00:00+00:00",
        "sender": {"fullName": "Lærer Lise", "shortName": "LL"},
        "text": {"html": "<p>Hej med dig</p>"},
    }

    _sync_en_tråd(aula_db, tråd, besked)

    gemt = aula_db.list_aula_messages("t1")[0]
    assert gemt["sender"] == "Lærer Lise"
    assert gemt["body"] == "Hej med dig"
    assert gemt["thread_id"] == "t1"


def test_synk_tæller_både_tråde_og_beskeder(aula_db):
    """Rapporteringen skal ikke lade som om der er 0 beskeder."""
    tråd = {
        "id": "t1",
        "subject": "Hej",
        "startedTime": "2026-10-05T08:00:00+00:00",
        "creator": {"fullName": "Lærer"},
        "regardingChildren": [{"profileId": 222, "name": "Emil"}],
    }
    beskeder = [
        {"id": f"m{i}", "sendDateTime": "2026-10-05T08:00:00+00:00",
         "sender": {"fullName": "Lærer"}, "text": {"html": f"<p>Besked {i}</p>"}}
        for i in range(3)
    ]
    client = fake_client(threads=AsyncMock(return_value=[tråd]),
                         messages=AsyncMock(return_value=beskeder))

    antal_tråde, antal_beskeder = asyncio.run(AulaSync(aula_db)._sync_messages(client))

    assert antal_tråde == 1
    assert antal_beskeder == 3
