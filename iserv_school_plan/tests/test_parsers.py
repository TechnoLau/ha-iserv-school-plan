"""Tests fuer die Plan-Parser (ICS, HTML, JSON)."""

from __future__ import annotations

from datetime import date, time

from app.parsers import (
    detect_format,
    extract_period,
    find_date,
    is_cancelled,
    parse_html,
    parse_ics,
    parse_json,
    parse_payload,
    parse_time,
    parse_time_range,
)

ICS_SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//IServ//Stundenplan//DE
BEGIN:VEVENT
UID:1@iserv
DTSTART;TZID=Europe/Berlin:20261021T074500
DTEND;TZID=Europe/Berlin:20261021T083000
SUMMARY:1. Stunde Mathematik
LOCATION:A1
END:VEVENT
BEGIN:VEVENT
UID:2@iserv
DTSTART;TZID=Europe/Berlin:20261021T083500
DTEND;TZID=Europe/Berlin:20261021T092000
SUMMARY:2. Stunde Deutsch (faellt aus)
DESCRIPTION:Lehrkraft erkrankt
LOCATION:B2
END:VEVENT
BEGIN:VEVENT
UID:3@iserv
DTSTART;VALUE=DATE:20261022
SUMMARY:Wandertag
END:VEVENT
END:VCALENDAR
"""

HTML_SAMPLE = """
<html><body>
<h1>Vertretungsplan</h1>
<h2>Mittwoch, 21.10.2026</h2>
<table>
  <tr><th>Stunde</th><th>Zeit</th><th>Fach</th><th>Raum</th><th>Klasse</th><th>Art</th></tr>
  <tr><td>1</td><td>07:45 - 08:30</td><td>Mathematik</td><td>A1</td><td>8b</td><td></td></tr>
  <tr><td>2</td><td>08:35 - 09:20</td><td>Deutsch</td><td>B2</td><td>8b</td><td>Faellt aus</td></tr>
  <tr><td>3</td><td>09:50 - 10:35</td><td>Englisch</td><td>C3</td><td>8a</td><td></td></tr>
  <tr><td>4</td><td>10:40 - 11:25</td><td>Sport</td><td>Halle</td><td>8b</td><td></td></tr>
</table>
<h2>Donnerstag, 22.10.2026</h2>
<table>
  <tr><th>Stunde</th><th>Fach</th><th>Raum</th><th>Lehrer</th></tr>
  <tr><td>1</td><td>Physik</td><td>D4</td><td>Herr Muster</td></tr>
</table>
</body></html>
"""

JSON_SAMPLE = """
{
  "days": [
    {
      "date": "2026-10-21",
      "is_school_day": true,
      "lessons": [
        {"period": 1, "start": "07:45", "end": "08:30", "subject": "Mathematik", "room": "A1"},
        {"period": 2, "start": "08:35", "subject": "Deutsch", "cancelled": true, "note": "Entfall"}
      ]
    },
    {"date": "2026-10-22", "is_school_day": false, "holiday_name": "Ferien"}
  ]
}
"""


def test_helpers() -> None:
    assert extract_period("1") == 1
    assert extract_period("1.") == 1
    assert extract_period("Stunde 3") == 3
    assert extract_period("Fach") is None
    assert parse_time("07:45") == time(7, 45)
    assert parse_time("0745") == time(7, 45)
    assert parse_time_range("07:45 - 08:30") == (time(7, 45), time(8, 30))
    assert find_date("Mittwoch, 21.10.2026") == date(2026, 10, 21)
    assert find_date("21.10.26") == date(2026, 10, 21)
    assert is_cancelled("Unterricht fällt aus") is True
    assert is_cancelled("Vertretung") is False


def test_detect_format() -> None:
    assert detect_format(ICS_SAMPLE) == "ics"
    assert detect_format(JSON_SAMPLE) == "json"
    assert detect_format(HTML_SAMPLE) == "html"
    assert detect_format("x", content_type="text/calendar") == "ics"


def test_parse_ics() -> None:
    dataset = parse_ics(ICS_SAMPLE)
    assert len(dataset.days) == 1
    day = dataset.days[date(2026, 10, 21)]
    assert len(day.lessons) == 2
    first, second = day.lessons
    assert first.period == 1
    assert first.start == time(7, 45)
    assert first.subject == "1. Stunde Mathematik"
    assert first.room == "A1"

    assert second.cancelled is True
    assert day.held_lessons == [first]


def test_parse_html() -> None:
    dataset = parse_html(HTML_SAMPLE)
    assert dataset.days[date(2026, 10, 21)].lessons[0].start == time(7, 45)
    day1 = dataset.days[date(2026, 10, 21)]
    assert [lesson.period for lesson in day1.lessons] == [1, 2, 3, 4]
    assert day1.lessons[1].cancelled is True
    assert day1.cancelled_periods == [2]
    assert day1.first_planned_period() == 1

    day2 = dataset.days[date(2026, 10, 22)]
    assert day2.lessons[0].subject == "Physik"
    assert day2.lessons[0].note == "Herr Muster"
    # Ohne Zeit-Spalte bleibt der Beginn offen (wird per Stundenzeiten gefuellt)
    assert day2.lessons[0].start is None


def test_parse_html_filters_class() -> None:
    dataset = parse_html(HTML_SAMPLE, klasse="8b")
    day1 = dataset.days[date(2026, 10, 21)]
    assert [lesson.period for lesson in day1.lessons] == [1, 2, 4]


def test_parse_html_accepts_german_month_year() -> None:
    html = '<h2>Montag, 19.10.26</h2><table><tr><th>Std</th><th>Fach</th>' \
           "</tr><tr><td>1</td><td>Kunst</td></tr></table>"
    dataset = parse_html(html)
    assert date(2026, 10, 19) in dataset.days


def test_parse_json() -> None:
    dataset = parse_json(JSON_SAMPLE)
    day = dataset.days[date(2026, 10, 21)]
    assert day.held_lessons == [day.lessons[0]]
    assert day.lessons[0].start == time(7, 45)
    assert day.lessons[1].cancelled is True
    holiday = dataset.days[date(2026, 10, 22)]
    assert holiday.is_school_day is False
    assert holiday.holiday_name == "Ferien"


def test_parse_payload_autodetect() -> None:
    assert parse_payload(ICS_SAMPLE).source == "ics"
    assert parse_payload(HTML_SAMPLE).source == "html"
    assert parse_payload(JSON_SAMPLE).source == "json"


def test_html_without_tables_warns() -> None:
    dataset = parse_html("<html><body><p>Kein Plan</p></body></html>")
    assert dataset.days == {}
    assert dataset.warnings

