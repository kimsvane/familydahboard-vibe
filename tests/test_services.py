from datetime import date

from app.services import parse_hourly, upcoming_birthdays


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
