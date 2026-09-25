from datetime import date

from app.services import upcoming_birthdays


def test_upcoming_birthdays_handles_leap_day():
    result = upcoming_birthdays(
        [{"id": 1, "name": "Leap", "birth_date": "2020-02-29", "color": "#fff", "notes": ""}],
        date(2026, 3, 1),
    )
    assert result[0]["next_occurrence"] == "2027-02-28"
    assert result[0]["age"] == 7
