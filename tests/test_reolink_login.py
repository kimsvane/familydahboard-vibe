"""Login til Reolink på både moderne og gammel firmware.

Et gammelt Reolink-kamera kræver HTTP Digest og afviser den moderne
JSON-login med rspCode -4 ("param error"). Det er protokol, ikke
password, så appen skal kunne finde ud af det uden at brænde
forsøgstælleren hos kameraet.
"""
from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from app.reolink import (
    ReolinkCamera,
    ReolinkError,
    build_digest_header,
    build_digest_response,
    parse_digest_challenge,
)

KAMERA = {
    "id": 1,
    "host": "http://10.0.0.5:80",
    "username": "opencode",
    "password": "hemmeligt",
    "channel": 0,
    "person_enabled": True,
    "vehicle_enabled": True,
    "snapshots_enabled": True,
}

DIGEST = hashlib.md5("hemmeligt".encode()).hexdigest()


def md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


# ---------- Udfordrings-headeren ----------

def test_udfordring_laeses() -> None:
    header = 'Digest realm="IPCamera", qop="auth", nonce="abc123", opaque="xyz"'
    felter = parse_digest_challenge(header)
    assert felter is not None
    assert felter["realm"] == "IPCamera"
    assert felter["nonce"] == "abc123"
    assert felter["qop"] == "auth"


def test_udfordring_uden_nonce_er_ikke_en_udfordring() -> None:
    # Uden nonce kan vi ikke svare, så det må ikke tages som en.
    assert parse_digest_challenge('Digest realm="IPCamera"') is None


def test_ikke_digest_header_ignores() -> None:
    assert parse_digest_challenge("") is None
    assert parse_digest_challenge("Basic realm=\"IPCamera\"") is None


def test_qop_kan_komme_som_liste() -> None:
    felt = parse_digest_challenge('Digest realm="R", qop="auth,auth-int", nonce="n1"')
    assert felt is not None
    # Vi bruger altid "auth", aldrig auth-int.
    assert "auth" in felt["qop"]


# ---------- Digest-beregningen ----------

def test_digest_svar_med_qop() -> None:
    udfordring = {"realm": "IPCamera", "nonce": "abc", "qop": "auth"}
    svar = build_digest_response(udfordring, "opencode", "hemmeligt", "POST", "/cgi-bin/api.cgi", "c1", "00000001")
    ha1 = md5("opencode:IPCamera:hemmeligt")
    ha2 = md5("POST:/cgi-bin/api.cgi")
    forventet = md5(f"{ha1}:abc:00000001:c1:auth:{ha2}")
    assert svar == forventet


def test_digest_svar_uden_qop() -> None:
    # Ældre kameraer sender nogle gange ingen qop. Så bruges den
    # enklere formel, ellers ville svaret aldrig blive accepteret.
    udfordring = {"realm": "IPCamera", "nonce": "abc"}
    svar = build_digest_response(udfordring, "opencode", "hemmeligt", "POST", "/x", "c1")
    ha1 = md5("opencode:IPCamera:hemmeligt")
    ha2 = md5("POST:/x")
    assert svar == md5(f"{ha1}:abc:{ha2}")


def test_digest_header_indholder_noetagen_og_respons() -> None:
    header = build_digest_header(
        {"realm": "IPCamera", "nonce": "n1", "qop": "auth"},
        "opencode", "hemmeligt", "POST", "/cgi-bin/api.cgi",
    )
    assert header.startswith("Digest ")
    assert 'username="opencode"' in header
    assert 'realm="IPCamera"' in header
    assert 'nonce="n1"' in header
    assert 'uri="/cgi-bin/api.cgi"' in header
    assert "qop=auth" in header
    assert "response=" in header
    # Adgangskoden må aldrig stå i headeren i klar tekst.
    assert "hemmeligt" not in header


# ---------- Fallback mellem protokoller ----------

def gammelt_kamera() -> tuple[httpx.MockTransport, list[dict]]:
    """Et kamera der kun svarer på webinterfacets format."""
    kalde: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        krop = request.read().decode()
        kalde.append({"krop": krop, "auth": request.headers.get("authorization", "")})
        if '"action"' in krop:
            return httpx.Response(200, json=[{
                "cmd": "Login", "code": 0,
                "value": {"Token": {"name": "token-abc"}},
            }])
        # Uden action: protokolfejl, præcis som det virkelige kamera.
        return httpx.Response(200, json=[{
            "cmd": "Login", "code": 1,
            "error": {"detail": "param error", "rspCode": -4},
        }])

    return httpx.MockTransport(handler), kalde


def login(kamera: ReolinkCamera, transport: httpx.MockTransport) -> str:
    """Kør _ensure_token i en isoleret event-loop, som resten af
    testsuiten gør med asyncio.run."""

    async def koer() -> str:
        async with httpx.AsyncClient(transport=transport) as klient:
            return await kamera._ensure_token(klient)

    return asyncio.run(koer())


def test_gammelt_kamera_finder_selv_det_arbejdende_format() -> None:
    transport, kalde = gammelt_kamera()
    token = login(ReolinkCamera(dict(KAMERA)), transport)
    assert token == "token-abc"
    # Første kald bruger det moderne format og fejler, næste bruger
    # webinterfacets. Vi må altså give op efter præcis to forsøg.
    assert len(kalde) == 2
    assert '"action"' not in kalde[0]["krop"]
    assert '"action"' in kalde[1]["krop"]


def test_det_arbejdende_format_huskes_til_naeste_kal() -> None:
    # Ellers prøver vi den forkerte protokol igen hvert femte sekund.
    transport, kalde = gammelt_kamera()
    kamera = ReolinkCamera(dict(KAMERA))
    login(kamera, transport)
    antal_efter_første = len(kalde)
    kamera._token = None
    login(kamera, transport)
    assert len(kalde) == antal_efter_første + 1, "det kendte format skal prøves først"


def test_moderne_kamera_bereres_ikke_om() -> None:
    kalde: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kalde.append(1)
        return httpx.Response(200, json=[{
            "cmd": "Login", "code": 0, "value": {"Token": {"name": "tok"}},
        }])

    assert login(ReolinkCamera(dict(KAMERA)), httpx.MockTransport(handler)) == "tok"
    assert len(kalde) == 1, "et moderne kamera skal kun kræve ét forsøg"


def test_forkert_adgangskode_stopper_med_det_samme() -> None:
    # Det vigtigste. Hvis koden er forkert, må vi ikke prøve den anden
    # protokol bagefter: hvert forsøg tæller mod kameraets låsning.
    kalde: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kalde.append(1)
        return httpx.Response(200, json=[{
            "cmd": "Login", "code": 1,
            "error": {
                "detail": "login failed",
                "rspCode": -7,
                "auth_warning_info": {"remain_times": 9, "unlock_time": 0},
            },
        }])

    with pytest.raises(ReolinkError) as greb:
        login(ReolinkCamera(dict(KAMERA)), httpx.MockTransport(handler))
    assert len(kalde) == 1, "vi må ikke brænde flere forsøg på en lås-tæller"
    assert "adgangskode" in str(greb.value).lower()


def test_alle_fejlede_formater_giver_en_forstaaelig_fejl() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "cmd": "Login", "code": 1,
            "error": {"detail": "param error", "rspCode": -4},
        }])

    with pytest.raises(ReolinkError) as greb:
        login(ReolinkCamera(dict(KAMERA)), httpx.MockTransport(handler))
    assert "param error" in str(greb.value)


def test_et_kamera_uden_adgangskode_faar_klar_vejledning() -> None:
    # Vi må ikke sende nogen anmodning, hvis vi intet har at logge ind med.
    kalde: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kalde.append(1)
        return httpx.Response(200)

    with pytest.raises(ReolinkError) as greb:
        login(ReolinkCamera({**KAMERA, "password": ""}), httpx.MockTransport(handler))
    assert "password" in str(greb.value).lower()
    assert kalde == []


# ---------- Digest som HTTP-header ----------

def test_digest_udfordring_svares_og_login_lykkes() -> None:
    kalde: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        kalde.append(auth)
        if not auth:
            # Første gang: kameraet stiller udfordringen.
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Digest realm="IPCamera", qop="auth", nonce="n1"'},
            )
        return httpx.Response(200, json=[{
            "cmd": "Login", "code": 0, "value": {"Token": {"name": "digest-tok"}},
        }])

    token = login(ReolinkCamera(dict(KAMERA)), httpx.MockTransport(handler))
    assert token == "digest-tok"
    assert kalde[0] == ""
    assert kalde[1].startswith("Digest "), "svaret skal sendes tilbage i Authorization"


def test_401_uden_udfordring_giver_klar_fejl() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="IPCamera"'})

    with pytest.raises(ReolinkError) as greb:
        login(ReolinkCamera(dict(KAMERA)), httpx.MockTransport(handler))
    assert "401" in str(greb.value)
