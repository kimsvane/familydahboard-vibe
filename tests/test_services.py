from datetime import date, datetime, timedelta

from app.services import _title_matches, active_event_hints, parse_hourly, upcoming_birthdays


def test_upcoming_birthdays_handles_leap_day():
    result = upcoming_birthdays(
        [{"id": 1, "name": "Leap", "birth_date": "2020-02-29", "color": "#fff", "notes": ""}],
        date(2026, 3, 1),
    )
    assert result[0]["next_occurrence"] == "2027-02-28"
    assert result[0]["age"] == 7


def _hourly_payload(times, temperatures=None, probabilities=None, codes=None):
    """Bygger et svar i samme form som Open-Meteo: parallelle arrays."""
    count = len(times)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": temperatures if temperatures is not None else [10.0] * count,
            "apparent_temperature": [9.0] * count,
            "precipitation_probability": probabilities if probabilities is not None else [0] * count,
            "weather_code": codes if codes is not None else [3] * count,
        }
    }


def test_parse_hourly_starts_at_the_current_hour():
    from datetime import datetime, timedelta

    now = datetime.now()
    base = now.replace(minute=0, second=0, microsecond=0)
    times = [(base + timedelta(hours=offset)).isoformat(timespec="minutes") for offset in range(-2, 5)]
    rows = parse_hourly(_hourly_payload(times), limit=3)
    assert [row["time"] for row in rows] == [stamp for stamp in times[2:5]]


def test_parse_hourly_keeps_rain_and_weather_code_per_hour():
    from datetime import datetime, timedelta

    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    times = [(base + timedelta(hours=offset)).isoformat(timespec="minutes") for offset in range(3)]
    rows = parse_hourly(
        _hourly_payload(times, temperatures=[7.0, 6.0, 5.0], probabilities=[0, 40, 90], codes=[0, 3, 61]),
        limit=3,
    )
    assert [row["temperature"] for row in rows] == [7.0, 6.0, 5.0]
    assert [row["precipitation_probability"] for row in rows] == [0, 40, 90]
    assert [row["weather_code"] for row in rows] == [0, 3, 61]


def test_parse_hourly_survives_a_short_field():
    """Et felt der mangler en time skal give None, ikke sprænge alt."""
    from datetime import datetime, timedelta

    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    times = [(base + timedelta(hours=offset)).isoformat(timespec="minutes") for offset in range(3)]
    payload = _hourly_payload(times)
    payload["hourly"]["precipitation_probability"] = [0, 30]
    rows = parse_hourly(payload, limit=3)
    assert [row["precipitation_probability"] for row in rows] == [0, 30, None]


def test_parse_hourly_handles_no_hourly_data():
    assert parse_hourly({}) == []
    assert parse_hourly({"hourly": {"time": []}}) == []
    assert parse_hourly(_hourly_payload(["2026-01-01T10:00"]), limit=0) == []


def test_parse_hourly_skips_unreadable_timestamps():
    from datetime import datetime, timedelta

    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    good = (base + timedelta(hours=1)).isoformat(timespec="minutes")
    rows = parse_hourly(_hourly_payload(["ikke-et-tidspunkt", good]), limit=1)
    assert [row["time"] for row in rows] == [good]


def test_parse_hourly_accepts_a_timezone():
    """Med tidszone skal hver time stadig regnes fra det nuværende timepunkt."""
    from datetime import datetime, timedelta

    zone = "Europe/Copenhagen"
    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    times = [(base + timedelta(hours=offset)).isoformat(timespec="minutes") for offset in range(2)]
    rows = parse_hourly(_hourly_payload(times), timezone_name=zone, limit=2)
    assert len(rows) == 2
    assert all("time" in row for row in rows)


def _regel(**overrides):
    base = {
        "id": 1,
        "source_kind": "calendar",
        "source_id": None,
        "match_mode": "exact",
        "match_value": "Microracer",
        "text": "Husk gymnastiktøj",
        "lead_hours": 12,
        "enabled": True,
    }
    base.update(overrides)
    return base


def _aftale(titel, start, slut=None, **overrides):
    base = {
        "id": "e1",
        "title": titel,
        "start_at": start,
        "end_at": slut or start,
        "source_kind": "calendar",
        "source_id": 1,
        "source_name": "Familie",
        "local_start_time": "10:00",
        "all_day": False,
    }
    base.update(overrides)
    return base


def test_exakt_match_rammer_kun_præcis_titlen():
    assert _title_matches("Microracer", "exact", "microracer")
    assert _title_matches("  Microracer  ", "exact", "Microracer")
    assert not _title_matches("Microracer i Østerbro", "exact", "Microracer")
    # "IDR" må ikke ramme "Idræt", ellers ville en linje for idræt også
    # sætte en påmindelse på hver idrætsaften.
    assert not _title_matches("Idræt", "exact", "IDR")
    # Mellemrum i det brugeren skriver er ligegyldigt.
    assert _title_matches("IDR", "exact", "IDR ")


def test_indeholder_match_rammer_en_fast_indpakning():
    assert _title_matches("Microracer i Østerbro", "contains", "microracer")
    assert not _title_matches("Søndagsfodbold", "contains", "microracer")
    assert not _title_matches("", "contains", "microracer")
    assert not _title_matches("Microracer", "contains", "")


def test_huskelinje_vises_inden_aftalen_starter():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    # Aftalen begynder om tre timer. Med tolv timers forvarsel er
    # huskelinjen aktiv nu.
    start = datetime.fromisoformat("2026-10-03T10:00:00+02:00")
    result = active_event_hints([_regel()], [_aftale("Microracer", start.isoformat())], nu)
    assert len(result) == 1
    assert result[0]["text"] == "Husk gymnastiktøj"
    assert result[0]["event_title"] == "Microracer"
    assert result[0]["id"] == 1


def test_huskelinje_er_ikke_aktiv_for_tidligt():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    # Fireogtyve timer før aftalen er længere end forvarslet.
    start = datetime.fromisoformat("2026-10-04T07:00:00+02:00")
    assert active_event_hints([_regel()], [_aftale("Microracer", start.isoformat())], nu) == []
    # Men med et langt forvarsel kommer den alligevel.
    lang = _regel(lead_hours=48)
    assert len(active_event_hints([lang], [_aftale("Microracer", start.isoformat())], nu)) == 1


def test_huskelinje_forsvinder_naar_aftalen_er_slut():
    start = datetime.fromisoformat("2026-10-03T10:00:00+02:00")
    slut = datetime.fromisoformat("2026-10-03T12:00:00+02:00")
    efter = datetime.fromisoformat("2026-10-03T12:01:00+02:00")
    assert active_event_hints([_regel()], [_aftale("Microracer", start.isoformat(), slut.isoformat())], efter) == []


def test_huskelinje_er_aktiv_mens_aftalen_er_i_gang():
    start = datetime.fromisoformat("2026-10-03T10:00:00+02:00")
    slut = datetime.fromisoformat("2026-10-03T12:00:00+02:00")
    undervejs = datetime.fromisoformat("2026-10-03T11:00:00+02:00")
    result = active_event_hints([_regel()], [_aftale("Microracer", start.isoformat(), slut.isoformat())], undervejs)
    assert len(result) == 1
    assert result[0]["in_progress"] is True


def test_skoleleminne_rammer_kun_skoleleminner():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    start = datetime.fromisoformat("2026-10-03T08:00:00+02:00")
    regel = _regel(source_kind="school", match_value="IDR", text="Husk idrætstøj")
    # Samme titel i familiekalenderen skal ikke give idrætstøj.
    anden = _aftale("IDR", start.isoformat(), source_kind="calendar")
    assert active_event_hints([regel], [anden], nu) == []
    rigtig = _aftale("IDR", start.isoformat(), source_kind="school", source_id=7)
    result = active_event_hints([regel], [rigtig], nu)
    assert len(result) == 1
    assert result[0]["text"] == "Husk idrætstøj"


def test_linje_kan_begrænses_til_en_bestemt_kalender():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    start = datetime.fromisoformat("2026-10-03T10:00:00+02:00")
    kun_mig = _regel(source_id=2)
    # Samme titel i en anden kalender skal ignoreres.
    assert active_event_hints([kun_mig], [_aftale("Microracer", start.isoformat(), source_id=3)], nu) == []
    assert len(active_event_hints([kun_mig], [_aftale("Microracer", start.isoformat(), source_id=2)], nu)) == 1
    # Uden en kalender gælder linjen alle med samme slags.
    alle = _regel()
    assert len(active_event_hints([alle], [_aftale("Microracer", start.isoformat(), source_id=3)], nu)) == 1


def test_slettet_linje_giver_intet():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    start = datetime.fromisoformat("2026-10-03T10:00:00+02:00")
    slettet = _regel(enabled=False)
    assert active_event_hints([slettet], [_aftale("Microracer", start.isoformat())], nu) == []


def test_ingen_regler_giver_ingen_paamindelser():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    start = datetime.fromisoformat("2026-10-03T10:00:00+02:00")
    assert active_event_hints([], [_aftale("Microracer", start.isoformat())], nu) == []
    assert active_event_hints([_regel()], [], nu) == []


def test_flere_paamindelser_sorteres_efter_tid():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    regler = [
        _regel(id=1, match_value="Lincedance", text="Husk sko"),
        _regel(id=2, match_value="Microracer", text="Husk gymnastiktøj"),
    ]
    aftaler = [
        _aftale("Microracer", "2026-10-03T14:00:00+02:00", id="a", local_start_time="14:00"),
        _aftale("Lincedance", "2026-10-03T10:00:00+02:00", id="b", local_start_time="10:00"),
    ]
    result = active_event_hints(regler, aftaler, nu)
    assert [item["text"] for item in result] == ["Husk sko", "Husk gymnastiktøj"]


def test_mest_muligt_af_paamindelser():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    regler = [_regel(id=i, match_value=f"Aftale {i}", text=f"Husk {i}") for i in range(1, 7)]
    aftaler = [
        _aftale(f"Aftale {i}", "2026-10-03T10:00:00+02:00", id=str(i), local_start_time="10:00")
        for i in range(1, 7)
    ]
    assert len(active_event_hints(regler, aftaler, nu, limit=3)) == 3


def test_ugyldig_starttid_springes_over():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    result = active_event_hints([_regel()], [_aftale("Microracer", "ikke-en-dato")], nu)
    assert result == []


def test_heldagsaftale_er_aktiv_hele_dagen():
    nu = datetime.fromisoformat("2026-10-03T09:00:00+02:00")
    heldag = _aftale("Lincedance", "2026-10-03T00:00:00+02:00", "2026-10-04T00:00:00+02:00", all_day=True)
    result = active_event_hints([_regel(match_value="Lincedance")], [heldag], nu)
    assert len(result) == 1
    assert result[0]["all_day"] is True


def test_to_linjer_om_samme_aftale_vises_begge():
    # Det er to forskellige ting at huske, så begge skal stå der.
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    regler = [
        _regel(id=1, source_kind="school", match_value="IDR", text="Husk idrætstøj"),
        _regel(id=2, source_kind="school", match_value="IDR", text="Husk vandflaske"),
    ]
    lektion = _aftale("IDR", "2026-10-03T08:00:00+02:00", source_kind="school", source_id=7)
    result = active_event_hints(regler, [lektion], nu)
    assert {item["text"] for item in result} == {"Husk idrætstøj", "Husk vandflaske"}


def test_samme_tekst_staar_kun_en_gang():
    # To regler med samme tekst, som en nedarvet og en ny, må ikke give
    # to kasser med præcis det samme ord.
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    regler = [
        _regel(id=1, source_kind="school", match_mode="exact", match_value="IDR", text="Husk idrætstøj"),
        _regel(id=2, source_kind="school", match_mode="contains", match_value="idr", text="Husk idrætstøj"),
    ]
    lektion = _aftale("IDR", "2026-10-03T08:00:00+02:00", source_kind="school", source_id=7)
    result = active_event_hints(regler, [lektion], nu)
    assert [item["text"] for item in result] == ["Husk idrætstøj"]


def test_en_regel_der_rammer_flere_forekomster_staar_kun_en_gang():
    # Et ugentligt skemalelemne udvides til syv datoer. Den samme regel
    # må ikke give syv kasser med det samme.
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    regler = [_regel(id=1, match_value="Lincedance", text="Husk sko")]
    forekomster = [
        _aftale("Lincedance", f"2026-10-0{3 + i}T16:00:00+02:00", id=f"e{i}", local_start_time="16:00")
        for i in range(5)
    ]
    result = active_event_hints(regler, forekomster, nu)
    assert len(result) == 1
    assert result[0]["text"] == "Husk sko"


def test_ugyldigt_forvarsel_faller_tilbage_pa_tolv_timer():
    nu = datetime.fromisoformat("2026-10-03T07:00:00+02:00")
    start = datetime.fromisoformat("2026-10-03T10:00:00+02:00")
    # En beskadiget værdi må ikke få hele huskelinjen til at forsvinde.
    rodet = _regel(lead_hours="meget lang tid")
    tidlig = datetime.fromisoformat("2026-10-02T20:00:00+02:00")
    assert len(active_event_hints([rodet], [_aftale("Microracer", start.isoformat())], nu)) == 1
    assert active_event_hints([rodet], [_aftale("Microracer", start.isoformat())], tidlig) == []
