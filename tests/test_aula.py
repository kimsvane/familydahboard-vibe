import asyncio
import base64
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.aula import (
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
EN_VIRKELIG_MELLEMSIDE = (
    "https://app-redirect.aula.dk/?returnUri=aHR0cHM6Ly9hcHAtcHJpdmF0ZS5hdWxhLmRr"
    "Lz9jb2RlPWRlZjUwMjAwMzEzYjMxNDMxZWVkMDY5YjM2ZTUyYzY1MzYyOTgxNzExNTJiMTk3"
    "NjJkZThlMDZlZjk0NmYwZWNmMjMzZjI2ODZmMDg5NTRmMTIwYTY2YjljYjU0NWY1YWFjODdjMWZh"
    "MjMwNGExMTAzMDgyYjQyODZiOGQ0MmEzZjU2YjQ5ZjdkYjgyMDc1YmRmM2FlOGY2M2Q0Yzg2OTE5"
    "N2Q3MzA1NDY5ZDgxYTE1ZmI3MmI2OWRiNDk1NzU1MjQwZjhlM2Y0MzU0ODFkMzUwMDA5OGI1Mjgx"
    "OWNhZWIwNDg2NWYyNTVkY2Q4Zjc2NTczMTZhODQwNmE3MTg1YWRjYjFlMjYwZTA0NjMyNWViMTkw"
    "NzM4ZmQzMGNkYTk2MGVjODJjNjdjNzQ4NDYwZGI0ODhmMTUwY2Y0OGQ1YmM3NDY1NmNiNGI5NWQ5"
    "NmIyNjhhNzE3NjY3MzJlNzJiNDFkZWQwZjhkNTA5YzdjNGJmNGUxYjJiZDdiNDEwYzBhOTg4MjY5"
    "ODUyZWQ0NzY4ODVmZTE1YzI2M2FiZDJlZWRmN2YzNDRlY2M1OWRjMWI1MTk1Y2Q4ZDI3OTI3"
    "OTUwNWI2MzczNjY0ZThhODk0OGNkNWNjYTQxNTExNDlhNGY5MzVmOGE5MGRkNzUyMzg2OGM1ODc4"
    "MDYzMWY4NDEzZjA5YTUwMWYwZjAyYTc0NjcwMmY5NmIzNGI5MzBiNWRiYjNmZDM5NzA4MzFhNzll"
    "OWQ5OWMwMjIwNTFiOGY4OTUwNTYyMjQ5MDllYTRjNzY4NDBkNjA5OWFhYjhkODE5YTYwMTBkNWJh"
    "MWFlYzZiM2MyNjlmNzg5OWRmMTQ1MTVhYzhiNDY4Y2MzZjE1ZDVmZmM4YmFjYzgyZTk1ODk2Y2Y0OWVj"
    "ZWQyNGVhOGExMThkYjA0Njk0MWY4M2E5MGUwODcyMGZiZDg4MGI4NzYxNGQ2ZjQ0Zjk5MDFmMmI5"
    "ZjgwYjg4MDQzZDk4OTdkOTRjZWFkYTZkOTc1MTJkZDczMGE5MjRjNGNlNjdjZmFlYjA1Y2FlN2Mx"
    "MjM0YTQxY2I4NDM0NWZhMTE2OWFmZTczMTc0ZWI2NzEyZTQ2NDc3NTllYTBlYjY5ZDg0NmYzMTM0"
    "NTFiNTBmOTIyNzM4OTY4MmNlOTQ3MjM5NzE5ZWQ0Mzk0NmRjMTQ5NWUwZWZkNWEmc3RhdGU9YzI0"
    "OTMwYjdiOGI1ODk0YWZmNTIyODgwYWRiOWM1ZDQ="
)


def test_kode_læses_ud_app_redirect_adressen():
    kode, state = _traek_kode(EN_VIRKELIG_MELLEMSIDE)
    assert kode.startswith("def50200313b31431eed069b36e")
    assert len(kode) == 892
    assert state == "c24930b7b8b5894aff522880adb9c5d4"


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
    """Aula-klient med forudbestemte svar, så intet går på nettet."""
    defaults = {
        "profiles": AsyncMock(return_value=[{"institutionProfileId": 111, "name": "Emil"}]),
        "establish_context": AsyncMock(return_value={"institutions": [
            {"name": "Nord", "children": [{"institutionProfileId": 111, "name": "Emil"}]}
        ]}),
        "threads": AsyncMock(return_value=[
            {"threadId": "t1", "subject": "Hej", "senderName": "Lærer", "date": "2026-10-05T08:00:00Z", "unread": 1, "participants": ["Emil"]},
        ]),
        "messages": AsyncMock(return_value=[
            {"messageId": "m1", "senderName": "Lærer", "content": "<p>Hej med dig</p>", "date": "2026-10-05T08:00:00Z"},
        ]),
        "posts": AsyncMock(return_value=[
            {"id": "p1", "title": "Ture i morgen", "authorName": "Lærer", "content": {"html": "<p>Medtag regntøj</p>"}, "timestamp": 1_700_000_000},
        ]),
        "calendar_events": AsyncMock(return_value=[
            {"id": "e1", "title": "Matematik", "startDate": "2026-10-06T08:00:00Z", "endDate": "2026-10-06T08:45:00Z", "location": "1. sal", "institutionProfileIds": [111]},
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
