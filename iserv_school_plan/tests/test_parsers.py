"""Tests fuer die Plan-Parser (ICS, HTML, JSON)."""

from __future__ import annotations

import json
from datetime import date, time

from app.parsers import (
    detect_format,
    extract_period,
    find_date,
    is_cancelled,
    json_summary,
    parse_html,
    parse_ics,
    parse_json,
    parse_payload,
    parse_portal_json,
    parse_time,
    parse_time_range,
    table_summary,
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

# IServ-Stundenplanansicht: Spalten = Wochentage, verbundene Zelle = Doppelstunde.
GRID_SAMPLE = """
<html><body>
<h1>Stundenplan</h1>
<table class="timetable">
  <tr>
    <th>Stunde</th>
    <th>Montag<br>19.10.</th>
    <th>Dienstag<br>20.10.</th>
  </tr>
  <tr>
    <td>1<br>07:45 - 08:30</td>
    <td>Mathematik<br>Frau Muster<br>A1</td>
    <td rowspan="2">Deutsch<br>Herr Beispiel<br>B2</td>
  </tr>
  <tr>
    <td>2<br>08:35 - 09:20</td>
    <td>Englisch<br>Frau Test<br>---</td>
  </tr>
</table>
</body></html>
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


def test_parse_html_week_grid_with_dates() -> None:
    """IServ-Stundenplanansicht: Tage als Spalten, Stunde in der Zeilenkopfzelle."""
    dataset = parse_html(GRID_SAMPLE, default_year=2026)
    # Nach erfolgreicher Auswertung keine "ignoriert"-Hinweise im Status.
    assert dataset.warnings == []

    monday = dataset.days[date(2026, 10, 19)]
    by_period = {lesson.period: lesson for lesson in monday.lessons}
    assert by_period[1].subject == "Mathematik"
    assert by_period[1].start == time(7, 45)
    assert by_period[1].end == time(8, 30)
    assert by_period[1].room == "A1"
    assert by_period[1].note == "Frau Muster"
    assert by_period[2].subject == "Englisch"
    assert by_period[2].room == ""

    # Doppelstunde (rowspan) gilt fuer beide Stunden und verschiebt keine Spalte.
    tuesday = dataset.days[date(2026, 10, 20)]
    assert [lesson.period for lesson in tuesday.lessons] == [1, 2]
    assert {lesson.subject for lesson in tuesday.lessons} == {"Deutsch"}
    assert {lesson.room for lesson in tuesday.lessons} == {"B2"}


def test_parse_html_week_grid_without_date_is_reported() -> None:
    html = (
        "<table><tr><th>Std</th><th>Montag</th><th>Dienstag</th></tr>"
        "<tr><td>1</td><td>Mathematik</td><td>Deutsch</td></tr></table>"
    )
    dataset = parse_html(html)
    assert dataset.days == {}
    assert any("ohne Datum" in warning for warning in dataset.warnings)


def test_parse_html_grid_with_date_columns_only() -> None:
    """Manche Zeitplaene beschriften die Spalten nur mit Datum (ohne Wochentag)."""
    html = (
        "<table><tr><th>Stunde</th><th>19.10.2026</th><th>20.10.2026</th></tr>"
        "<tr><td>1</td><td>Mathematik<br>A1</td><td>Deutsch<br>B2</td></tr></table>"
    )
    dataset = parse_html(html)
    assert dataset.days[date(2026, 10, 19)].lessons[0].subject == "Mathematik"
    assert dataset.days[date(2026, 10, 20)].lessons[0].subject == "Deutsch"


def test_parse_html_date_table_without_period_column_is_reported() -> None:
    html = (
        "<table><tr><th>Termin</th><th>19.10.2026</th><th>20.10.2026</th></tr>"
        "<tr><td>Wandertag</td><td>x</td><td>y</td></tr></table>"
    )
    dataset = parse_html(html)
    assert dataset.days == {}
    assert any("Datumsspalten" in warning for warning in dataset.warnings)


def test_table_summary_describes_week_grid() -> None:
    summary = table_summary(GRID_SAMPLE)
    assert "Wochengitter mit Datum" in summary
    assert "Montag" in summary
    assert "3 Zeilen" in summary


def test_html_without_table_hints_at_iserv_web_app() -> None:
    dataset = parse_html('<html><body><div id="app"></div><script src="/iserv/app.js"></script></body></html>')
    assert dataset.days == {}
    assert any("JavaScript-Web-App" in warning for warning in dataset.warnings)


# --- JSON der IServ-Stundenplanansicht ---------------------------------------


def test_parse_portal_json_flat_list() -> None:
    """Flache Liste (``data`` -> Stunden) mit englischen Feldnamen."""
    text = json.dumps(
        {
            "data": [
                {
                    "date": "2026-10-19",
                    "start": "07:45",
                    "end": "08:30",
                    "subject": "Mathematik",
                    "room": "A1",
                    "period": 1,
                },
                {
                    "date": "2026-10-19",
                    "start": "08:35",
                    "end": "09:20",
                    "subject": "Deutsch",
                    "room": "B2",
                    "period": 2,
                },
            ]
        }
    )
    dataset = parse_portal_json(text, default_year=2026)
    day = dataset.days[date(2026, 10, 19)]
    assert [lesson.subject for lesson in day.lessons] == ["Mathematik", "Deutsch"]
    assert [lesson.period for lesson in day.lessons] == [1, 2]
    assert day.lessons[0].start == time(7, 45)
    assert day.lessons[0].room == "A1"
    assert day.is_school_day is True


def test_parse_portal_json_grouped_by_day_with_nested_names() -> None:
    """Nach Tagen gruppierte Antwort, Fach/Raum/Lehrer als Objekt, ``art`` = Entfall."""
    text = json.dumps(
        {
            "days": {
                "2026-10-20": {
                    "stunden": [
                        {
                            "stunde": 1,
                            "beginn": "07:45",
                            "ende": "08:30",
                            "fach": {"name": "Mathematik"},
                            "raum": {"name": "A1"},
                            "lehrer": {"name": "Muster"},
                        },
                        {
                            "stunde": 2,
                            "beginn": "08:35",
                            "fach": {"name": "Deutsch"},
                            "art": "Entfall",
                        },
                    ]
                }
            }
        }
    )
    dataset = parse_portal_json(text, default_year=2026)
    lessons = dataset.days[date(2026, 10, 20)].lessons
    assert lessons[0].subject == "Mathematik"
    assert lessons[0].room == "A1"
    assert lessons[0].note == "Muster"
    assert lessons[0].cancelled is False
    assert lessons[1].cancelled is True
    assert lessons[1].room == ""


def test_parse_portal_json_date_from_branch_key() -> None:
    """Datum nur am Zweig (``"19.10.2026": [...]``) - gilt fuer alle Eintraege."""
    text = json.dumps(
        {"plan": {"19.10.2026": [{"stunde": 3, "fach": "Physik", "raum": "C3"}]}}
    )
    dataset = parse_portal_json(text, default_year=2026)
    lesson = dataset.days[date(2026, 10, 19)].lessons[0]
    assert (lesson.period, lesson.subject) == (3, "Physik")


def test_parse_portal_json_ignores_configuration_entries() -> None:
    """Eintraege ohne Datum/Fach (z. B. Zeitraster) erzeugen keine Stunden."""
    text = json.dumps(
        {
            "periods": [{"period": 1, "start": "07:45", "end": "08:30"}],
            "settings": {"week": "A", "title": "Stundenplan"},
            "data": [],
        }
    )
    dataset = parse_portal_json(text, default_year=2026)
    assert dataset.days == {}
    assert any("Struktur" in warning for warning in dataset.warnings)


def test_parse_portal_json_without_date_is_ignored() -> None:
    text = json.dumps({"data": [{"stunde": 1, "fach": "Mathematik"}]})
    dataset = parse_portal_json(text)
    assert dataset.days == {}


def test_json_summary_names_keys_without_text_values() -> None:
    summary = json_summary(
        json.dumps(
            {
                "data": [
                    {"start": "07:45", "subject": "Mathematik", "period": 1}
                ]
            }
        )
    )
    assert "data" in summary
    assert "start" in summary
    assert "'07:45'" in summary
    assert "Mathematik" not in summary
    assert "<str 10>" in summary


def test_parse_portal_json_nested_rooms_and_teachers() -> None:
    """Echte Portal-Form: Fach/Raum/Lehrkraft als Objekte bzw. Listen davon."""
    text = json.dumps(
        {
            "data": [
                {
                    "type": "lesson",
                    "data": {
                        "id": 4711,
                        "date": "2026-10-19",
                        "lesson": 2,
                        "startTime": "08:35",
                        "endTime": "09:20",
                        "subject": {"name": "Deutsch"},
                        "rooms": [{"name": "B2"}],
                        "teachers": [{"name": "Frau Muster"}],
                        "info": "Klausur",
                    },
                }
            ],
            "config": {"week": "A"},
        }
    )
    dataset = parse_portal_json(text, default_year=2026)
    lesson = dataset.days[date(2026, 10, 19)].lessons[0]
    assert lesson.period == 2
    assert lesson.subject == "Deutsch"
    assert lesson.room == "B2"
    assert lesson.start == time(8, 35)
    assert lesson.note == "Frau Muster, Klausur"
    assert dataset.warnings == []


def test_parse_portal_json_grid_uses_branch_key_as_period() -> None:
    """Gitterform: ``{"2026-10-19": {"3": {"fach": ...}}}``."""
    text = json.dumps(
        {"week": {"2026-10-19": {"3": {"fach": "Physik", "raum": "C3"}}}}
    )
    dataset = parse_portal_json(text, default_year=2026)
    lesson = dataset.days[date(2026, 10, 19)].lessons[0]
    assert lesson.period == 3
    assert lesson.subject == "Physik"
    assert lesson.room == "C3"


def test_parse_portal_json_period_grid_without_subject_is_ignored() -> None:
    """Das Zeitraster (``"3": {"start": ...}``) ist keine Unterrichtsstunde."""
    text = json.dumps(
        {"2026-10-19": {"3": {"start": "09:30", "end": "10:15"}}}
    )
    dataset = parse_portal_json(text, default_year=2026)
    assert dataset.days == {}



def test_parse_portal_json_falls_back_to_plain_timetable() -> None:
    """Ohne ``data.timetable`` gilt ``plain-timetable`` - aber nie beide Listen."""
    text = json.dumps(
        {
            "meta": {"filter": {"startDate": "19.10.2026"}},
            "data": {
                "timetable": [],
                "plain-timetable": [
                    {"dow": 1, "period": 1, "subject": "Mathematik", "room": "A1"}
                ],
            },
            "plain-changes": [
                {"dow": 1, "period": 1, "subject": "Mathematik", "room": "A1"}
            ],
        }
    )
    dataset = parse_portal_json(text, default_year=2026)
    monday = dataset.days[date(2026, 10, 19)]
    assert [lesson.subject for lesson in monday.lessons] == ["Mathematik"]


def test_parse_portal_json_uses_meta_filter_as_week_anchor() -> None:
    """``meta.filter.startDate`` liefert das Datum zu ``dow`` (IServ: 1 = Montag)."""
    text = json.dumps(
        {
            "meta": {
                "filter": {
                    "startDate": "19.10.2026",
                    "endDate": "25.10.2026",
                    "classes": ["10a"],
                }
            },
            "data": {
                "timetable": [
                    {"dow": 1, "period": 2, "subject": "Biologie", "room": "C3"},
                    {"dow": 5, "period": 1, "subject": "Sport", "room": "Halle"},
                ],
                "plain-timetable": [
                    {"dow": 1, "period": 2, "subject": "Biologie", "room": "C3"}
                ],
            },
        }
    )
    dataset = parse_portal_json(text, default_year=2026)
    assert sorted(dataset.days) == [date(2026, 10, 19), date(2026, 10, 23)]
    assert dataset.days[date(2026, 10, 19)].lessons[0].subject == "Biologie"
    assert dataset.days[date(2026, 10, 23)].lessons[0].subject == "Sport"


def test_parse_portal_json_without_week_anchor_ignores_dow() -> None:
    """Ohne Wochenanker ist ein Wochentag kein Datum - keine geratenen Stunden."""
    text = json.dumps(
        {"data": {"timetable": [{"dow": 1, "period": 1, "subject": "Mathematik"}]}}
    )
    dataset = parse_portal_json(text, default_year=2026)
    assert dataset.days == {}


def test_parse_portal_json_change_types_cancels_lesson() -> None:
    """``change_types`` enthaelt bei Ausfall ``"0"``; Ersatzfach/-raum gilt."""
    text = json.dumps(
        {
            "meta": {"filter": {"startDate": "19.10.2026"}},
            "data": {
                "timetable": [
                    {
                        "dow": 1,
                        "period": 1,
                        "subject": "Mathematik",
                        "room": "A1",
                        "change": {
                            "change_types": ["0"],
                            "substitutionSubject": "",
                            "substitutionRoom": "",
                        },
                    },
                    {
                        "dow": 2,
                        "period": 1,
                        "subject": "Englisch",
                        "room": "B2",
                        "change": {
                            "change_types": ["1"],
                            "substitutionSubject": "Franzoesisch",
                            "substitutionRoom": "C1",
                        },
                    },
                ]
            },
        }
    )
    dataset = parse_portal_json(text, default_year=2026)
    monday = dataset.days[date(2026, 10, 19)].lessons[0]
    assert monday.cancelled is True
    assert monday.subject == "Mathematik"
    tuesday = dataset.days[date(2026, 10, 20)].lessons[0]
    assert tuesday.cancelled is False
    assert (tuesday.subject, tuesday.room) == ("Franzoesisch", "C1")


def test_parse_portal_json_reads_dsa_students_entries() -> None:
    """DieSchulApp: ``students[].entries[]`` mit ``weekday`` 0=Montag."""
    text = json.dumps(
        {
            "meta": {"filter": {"startDate": "19.10.2026"}},
            "students": [
                {
                    "entries": [
                        {
                            "weekday": 2,
                            "timeTableSlot": {
                                "number": 4,
                                "startTime": "10:40",
                                "endTime": "11:25",
                            },
                            "courseSubject": {
                                "subject": {"name": "Physik", "acronym": "PH"},
                                "course": {"name": "10a"},
                                "teachers": [{"surname": "Muster"}],
                            },
                            "room": {"name": "C3"},
                        }
                    ]
                }
            ],
        }
    )
    dataset = parse_portal_json(text, default_year=2026)
    lesson = dataset.days[date(2026, 10, 21)].lessons[0]
    assert (lesson.period, lesson.subject, lesson.room) == (4, "Physik", "C3")
    assert lesson.start == time(10, 40)




# --- Wochengitter ohne Datum: Wochenanker ------------------------------------


def test_week_grid_without_dates_uses_page_filter_anchor() -> None:
    """Spalten nur mit Wochentagen: das Filter-JSON der Seite liefert die Daten."""
    html = (
        "<html><body>"
        "<script type=\"application/json\">"
        '{"filter":{"startDate":"19.10.2026","endDate":"25.10.2026"}}'
        "</script>"
        "<table><tr><th>Std.</th><th>Montag</th><th>Dienstag</th></tr>"
        "<tr><td>1</td><td>Mathematik<br>A1</td><td>Deutsch<br>B2</td></tr></table>"
        "</body></html>"
    )
    dataset = parse_html(html)
    assert dataset.days[date(2026, 10, 19)].lessons[0].subject == "Mathematik"
    assert dataset.days[date(2026, 10, 19)].lessons[0].room == "A1"
    assert dataset.days[date(2026, 10, 20)].lessons[0].subject == "Deutsch"


def test_week_grid_without_dates_uses_heading_range_anchor() -> None:
    """Ueberschrift ``19.10. - 25.10.2026`` legt den Wochenanfang fest."""
    html = (
        "<html><body><h2>Woche 19.10. - 25.10.2026</h2>"
        "<table><tr><th>Std</th><th>Montag</th><th>Freitag</th></tr>"
        "<tr><td>2</td><td>Physik<br>C3</td><td>Kunst<br>D4</td></tr></table>"
        "</body></html>"
    )
    dataset = parse_html(html)
    assert dataset.days[date(2026, 10, 19)].lessons[0].subject == "Physik"
    assert dataset.days[date(2026, 10, 23)].lessons[0].subject == "Kunst"


def test_week_grid_without_dates_uses_calendar_week_anchor() -> None:
    """``KW 43`` genuegt als Anker, wenn die Seite keinen Zeitraum nennt."""
    html = (
        "<html><body><h3>Stundenplan KW 43/2026</h3>"
        "<table><tr><th>Std</th><th>Mittwoch</th><th>Donnerstag</th></tr>"
        "<tr><td>1</td><td>Chemie<br>E2</td><td>Musik<br>M1</td></tr></table>"
        "</body></html>"
    )
    dataset = parse_html(html)
    # KW 43/2026: Montag 19.10., Mittwoch 21.10., Donnerstag 22.10.
    assert dataset.days[date(2026, 10, 21)].lessons[0].subject == "Chemie"
    assert dataset.days[date(2026, 10, 22)].lessons[0].subject == "Musik"


def test_week_grid_without_anchor_is_ignored_with_context() -> None:
    """Ohne Datum und ohne Anker bleibt das Gitter unausgewertet - mit Details."""
    html = (
        "<table><tr><th>Std</th><th>Montag</th><th>Dienstag</th></tr>"
        "<tr><td>1</td><td>Mathematik</td><td>Deutsch</td></tr></table>"
    )
    dataset = parse_html(html)
    assert dataset.days == {}
    warning = next(w for w in dataset.warnings if "ohne Wochenanker" in w)
    assert "2 Zeilen" in warning
    assert "Montag" in warning


def test_table_summary_names_missing_week_anchor() -> None:
    summary = table_summary(
        "<table><tr><th>Std</th><th>Montag</th><th>Dienstag</th></tr></table>"
    )
    assert "Wochengitter ohne Datum" in summary
    assert "kein Wochenanker" in summary


def test_table_summary_names_week_anchor() -> None:
    summary = table_summary(
        "<h2>19.10. - 25.10.2026</h2>"
        "<table><tr><th>Std</th><th>Montag</th><th>Dienstag</th></tr></table>"
    )
    assert "Wochengitter ohne Datum" in summary
    assert "Wochenanker 19.10.2026" in summary
