"""Tests fuer die Ferien-/Feiertagsdaten (ohne echte Netzwerkabfrage)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

import app.holidays as holidays_module
from app.holidays import (
    BUNDESLAENDER,
    CACHE_MAX_AGE,
    HolidayCalendar,
    HolidayError,
    _api_url,
    fetch_holidays,
    load_holidays,
    normalize_bundesland,
    parse_holidays,
    read_cache,
    write_cache,
)
from app.plan import TZ, Holiday

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=TZ)

SCHOOL_PAYLOAD = [
    {
        "startDate": "2026-10-05",
        "endDate": "2026-10-24",
        "type": "School",
        "name": [{"language": "DE", "text": "Herbstferien"}],
        "regionalScope": "Regional",
    },
    {
        "startDate": "2026-10-12",
        "endDate": "2026-10-24",
        "type": "School",
        "name": [{"language": "DE", "text": "Herbstferien"}],
    },
]

PUBLIC_PAYLOAD = [
    {
        "startDate": "2026-10-31",
        "endDate": "2026-10-31",
        "type": "Public",
        "name": [{"language": "DE", "text": "Reformationstag"}],
    }
]


def test_all_bundeslaender_have_iso_codes() -> None:
    assert len(BUNDESLAENDER) == 16
    assert BUNDESLAENDER["Schleswig-Holstein"] == "DE-SH"
    assert all(code.startswith("DE-") for code in BUNDESLAENDER.values())


def test_normalize_bundesland_accepts_name_and_code() -> None:
    assert normalize_bundesland("Schleswig-Holstein") == "Schleswig-Holstein"
    assert normalize_bundesland("schleswig-holstein") == "Schleswig-Holstein"
    assert normalize_bundesland("SH") == "Schleswig-Holstein"
    assert normalize_bundesland("DE-BY") == "Bayern"
    assert normalize_bundesland("   ") == "Schleswig-Holstein"
    assert normalize_bundesland("Mars") == "Schleswig-Holstein"


def test_parse_holidays_reads_dates_and_names() -> None:
    entries = parse_holidays(SCHOOL_PAYLOAD, "ferien")
    assert entries[0].start == date(2026, 10, 5)
    assert entries[0].end == date(2026, 10, 24)
    assert entries[0].name == "Herbstferien"
    assert entries[0].kind == "ferien"
    public = parse_holidays(PUBLIC_PAYLOAD, "feiertag")
    assert public[0].kind == "feiertag"
    assert public[0].start == public[0].end


def test_parse_holidays_prefers_german_and_skips_broken_entries() -> None:
    payload = [
        {"startDate": "2026-12-24", "endDate": "2026-12-24",
         "name": [{"language": "EN", "text": "Christmas Eve"},
                  {"language": "DE", "text": "Heiligabend"}]},
        {"startDate": "kaputt", "endDate": "2026-12-24"},
        {"endDate": "2026-12-24"},
        "kein objekt",
        {"startDate": "2026-12-27", "endDate": "2026-12-24",
         "name": [{"text": "Ohne Sprache"}]},
    ]
    entries = parse_holidays(payload, "feiertag")
    assert [entry.name for entry in entries] == ["Heiligabend", "Ohne Sprache"]
    # Verdrehte Zeitraeume werden korrigiert.
    assert entries[1].start == date(2026, 12, 24)
    assert entries[1].end == date(2026, 12, 27)


def test_parse_holidays_rejects_non_list() -> None:
    with pytest.raises(HolidayError):
        parse_holidays({"error": "nope"}, "ferien")


def test_api_url_contains_query_parameters() -> None:
    url = _api_url("SchoolHolidays", "DE-SH", date(2026, 10, 21), date(2027, 11, 25))
    assert url.startswith("https://openholidaysapi.org/SchoolHolidays?")
    assert "countryIsoCode=DE" in url
    assert "subdivisionCode=DE-SH" in url
    assert "validFrom=2026-10-21" in url
    assert "validTo=2027-11-25" in url
    assert "languageIsoCode=DE" in url


def test_fetch_holidays_collects_both_endpoints(monkeypatch) -> None:
    seen: list[tuple] = []

    def fake_fetch(endpoint, iso_code, start, end, timeout):
        seen.append((endpoint, iso_code, start, end, timeout))
        kind = "ferien" if endpoint == "SchoolHolidays" else "feiertag"
        payload = SCHOOL_PAYLOAD if endpoint == "SchoolHolidays" else PUBLIC_PAYLOAD
        return parse_holidays(payload, kind)

    monkeypatch.setattr(holidays_module, "_fetch_endpoint", fake_fetch)
    calendar = fetch_holidays("Schleswig-Holstein", now=NOW, timeout=3)

    assert calendar.bundesland == "Schleswig-Holstein"
    assert calendar.fetched_at == NOW
    assert {entry.kind for entry in calendar.ranges} == {"ferien", "feiertag"}
    assert [entry.start for entry in calendar.ranges] == sorted(
        entry.start for entry in calendar.ranges
    )
    assert [item[0] for item in seen] == ["SchoolHolidays", "PublicHolidays"]
    assert all(item[1] == "DE-SH" and item[4] == 3 for item in seen)
    assert seen[0][2] == NOW.date()
    assert seen[0][3] == NOW.date() + timedelta(days=holidays_module.COVERAGE_DAYS)


def test_fetch_holidays_keeps_partial_result(monkeypatch) -> None:
    def fake_fetch(endpoint, iso_code, start, end, timeout):  # noqa: ARG001 - Testdouble
        if endpoint == "PublicHolidays":
            raise OSError("kein Netz")
        return parse_holidays(SCHOOL_PAYLOAD, "ferien")

    monkeypatch.setattr(holidays_module, "_fetch_endpoint", fake_fetch)
    calendar = fetch_holidays("Schleswig-Holstein", now=NOW)

    assert len(calendar.ranges) == 2
    assert calendar.warnings and "PublicHolidays" in calendar.warnings[0]


def test_fetch_holidays_raises_when_api_unreachable(monkeypatch) -> None:
    def fake_fetch(endpoint, iso_code, start, end, timeout):  # noqa: ARG001 - Testdouble
        raise OSError("offline")

    monkeypatch.setattr(holidays_module, "_fetch_endpoint", fake_fetch)
    with pytest.raises(HolidayError):
        fetch_holidays("Schleswig-Holstein", now=NOW)


def test_cache_roundtrip(tmp_path) -> None:
    path = tmp_path / "holidays.json"
    calendar = HolidayCalendar(
        bundesland="Bayern",
        fetched_at=NOW,
        source="test",
        ranges=[Holiday(date(2026, 11, 2), date(2026, 11, 6), "Herbstferien")],
    )
    write_cache(calendar, path)

    restored = read_cache(path)
    assert restored is not None
    assert restored.bundesland == "Bayern"
    assert restored.fetched_at == NOW
    assert restored.source == "test"
    assert restored.ranges == calendar.ranges


def test_read_cache_handles_broken_file(tmp_path) -> None:
    path = tmp_path / "holidays.json"
    path.write_text("{kaputt", encoding="utf-8")
    assert read_cache(path) is None



def test_fresh_cache_avoids_network(tmp_path, monkeypatch) -> None:
    path = tmp_path / "holidays.json"
    write_cache(
        HolidayCalendar(
            bundesland="Bayern",
            fetched_at=NOW,
            ranges=[Holiday(date(2026, 11, 2), date(2026, 11, 6), "Herbstferien")],
        ),
        path,
    )

    def boom(*args, **kwargs):  # noqa: ARG001 - darf nicht aufgerufen werden
        raise AssertionError("Netzwerk nicht erwartet")

    monkeypatch.setattr(holidays_module, "fetch_holidays", boom)
    calendar = load_holidays("Bayern", path=path, now=NOW + timedelta(days=1))
    assert len(calendar.ranges) == 1


def test_stale_cache_is_used_when_api_fails(tmp_path, monkeypatch) -> None:
    path = tmp_path / "holidays.json"
    write_cache(
        HolidayCalendar(
            bundesland="Bayern",
            fetched_at=NOW - 2 * CACHE_MAX_AGE,
            ranges=[Holiday(date(2026, 11, 2), date(2026, 11, 6), "Herbstferien")],
        ),
        path,
    )

    def offline(*args, **kwargs):  # noqa: ARG001 - Testdouble
        raise HolidayError("offline")

    monkeypatch.setattr(holidays_module, "fetch_holidays", offline)
    calendar = load_holidays("Bayern", path=path, now=NOW)

    assert len(calendar.ranges) == 1
    assert any("Stand" in warning for warning in calendar.warnings)


def test_load_holidays_offline_without_cache(tmp_path, monkeypatch) -> None:
    def offline(*args, **kwargs):  # noqa: ARG001 - Testdouble
        raise HolidayError("offline")

    monkeypatch.setattr(holidays_module, "fetch_holidays", offline)
    calendar = load_holidays("Hessen", path=tmp_path / "holidays.json", now=NOW)

    assert calendar.ranges == []
    assert calendar.bundesland == "Hessen"
    assert any("nicht verfuegbar" in warning for warning in calendar.warnings)


def test_changed_bundesland_refetches(tmp_path, monkeypatch) -> None:
    path = tmp_path / "holidays.json"
    write_cache(
        HolidayCalendar(
            bundesland="Bayern",
            fetched_at=NOW,
            ranges=[Holiday(date(2026, 11, 2), date(2026, 11, 6), "Herbstferien")],
        ),
        path,
    )
    called: list[str] = []

    def fake_fetch(bundesland, now=None, timeout=0):  # noqa: ARG001 - Testdouble
        called.append(bundesland)
        return HolidayCalendar(
            bundesland=bundesland,
            fetched_at=NOW,
            ranges=[Holiday(date(2026, 10, 12), date(2026, 10, 24), "Herbstferien")],
        )

    monkeypatch.setattr(holidays_module, "fetch_holidays", fake_fetch)
    calendar = load_holidays("Hessen", path=path, now=NOW)

    assert called == ["Hessen"]
    assert calendar.bundesland == "Hessen"
    assert read_cache(path).bundesland == "Hessen"


def test_successful_fetch_is_cached(tmp_path, monkeypatch) -> None:
    path = tmp_path / "holidays.json"

    def fake_fetch(bundesland, now=None, timeout=0):  # noqa: ARG001 - Testdouble
        return HolidayCalendar(
            bundesland=bundesland,
            fetched_at=NOW,
            ranges=[Holiday(date(2026, 11, 2), date(2026, 11, 6), "Herbstferien")],
        )

    monkeypatch.setattr(holidays_module, "fetch_holidays", fake_fetch)
    load_holidays("Bayern", path=path, now=NOW)

    cached = read_cache(path)
    assert cached is not None
    assert cached.fetched_at == NOW


def test_is_stale() -> None:
    entry = Holiday(date(2026, 11, 2), date(2026, 11, 6), "Herbstferien")
    assert HolidayCalendar().is_stale(NOW) is True
    assert HolidayCalendar(ranges=[entry], fetched_at=NOW).is_stale(NOW) is False
    old = HolidayCalendar(ranges=[entry], fetched_at=NOW - CACHE_MAX_AGE)
    assert old.is_stale(NOW) is True

