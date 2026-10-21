"""Ferien und Feiertage aus openholidaysapi.org (ohne API-Key, ohne Zusatzpaket).

Der Dienst ist kostenlos und liefert JSON: ``SchoolHolidays`` enthaelt die
Schulferien, ``PublicHolidays`` die gesetzlichen Feiertage je Bundesland.
Die Antworten werden sieben Tage lang in ``/data/holidays.json`` zwischen-
gespeichert; ist der Dienst nicht erreichbar, gelten die gespeicherten Daten
weiter (mit Warnung im Status-Attribut).

Der Kalender ist nur eine Ergaenzung: Liefert IServ fuer einen Tag Daten, haben
diese immer Vorrang (siehe ``plan.select_plan``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .plan import TZ, Holiday

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://openholidaysapi.org"
CACHE_PATH = Path(os.environ.get("ISERV_DATA", "/data")) / "holidays.json"
CACHE_MAX_AGE = timedelta(days=7)
# So weit im Voraus werden Ferien/Feiertage geladen (deckt das Schuljahr ab).
COVERAGE_DAYS = 400
REQUEST_TIMEOUT = 10

DEFAULT_BUNDESLAND = "Schleswig-Holstein"

# Anzeigename (Add-on-Option) -> ISO-3166-2-Code der API.
BUNDESLAENDER: dict[str, str] = {
    "Baden-Württemberg": "DE-BW",
    "Bayern": "DE-BY",
    "Berlin": "DE-BE",
    "Brandenburg": "DE-BB",
    "Bremen": "DE-HB",
    "Hamburg": "DE-HH",
    "Hessen": "DE-HE",
    "Mecklenburg-Vorpommern": "DE-MV",
    "Niedersachsen": "DE-NI",
    "Nordrhein-Westfalen": "DE-NW",
    "Rheinland-Pfalz": "DE-RP",
    "Saarland": "DE-SL",
    "Sachsen": "DE-SN",
    "Sachsen-Anhalt": "DE-ST",
    "Schleswig-Holstein": "DE-SH",
    "Thüringen": "DE-TH",
}

# (API-Endpunkt, Art im Datenmodell)
ENDPOINTS = (("SchoolHolidays", "ferien"), ("PublicHolidays", "feiertag"))


class HolidayError(RuntimeError):
    """Ferien-/Feiertagsdaten konnten nicht geladen werden."""


@dataclass
class HolidayCalendar:
    """Ferien und Feiertage eines Bundeslands inkl. Abrufzeitpunkt."""

    bundesland: str = DEFAULT_BUNDESLAND
    ranges: list[Holiday] = field(default_factory=list)
    fetched_at: datetime | None = None
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def is_stale(self, now: datetime, max_age: timedelta = CACHE_MAX_AGE) -> bool:
        if self.fetched_at is None or not self.ranges:
            return True
        return now - self.fetched_at >= max_age

    def to_dict(self) -> dict:
        return {
            "bundesland": self.bundesland,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "source": self.source,
            "holidays": [entry.to_dict() for entry in self.ranges],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HolidayCalendar":
        calendar = cls(
            bundesland=normalize_bundesland(data.get("bundesland")),
            source=data.get("source") or "",
        )
        if data.get("fetched_at"):
            try:
                calendar.fetched_at = datetime.fromisoformat(data["fetched_at"])
            except ValueError:
                calendar.fetched_at = None
        for item in data.get("holidays") or []:
            try:
                calendar.ranges.append(Holiday.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        calendar.ranges.sort(key=lambda entry: (entry.start, entry.name))
        return calendar


def normalize_bundesland(value: str | None) -> str:
    """Akzeptiert Anzeigename, Kuerzel (``SH``) oder ISO-Code (``DE-SH``)."""
    text = (value or "").strip()
    if not text:
        return DEFAULT_BUNDESLAND
    for name in BUNDESLAENDER:
        if name.casefold() == text.casefold():
            return name
    code = text.upper()
    for name, iso in BUNDESLAENDER.items():
        if code in (iso, iso.removeprefix("DE-")):
            return name
    return DEFAULT_BUNDESLAND


def _name(value) -> str:
    """Name aus ``[{"language": "DE", "text": "Herbstferien"}]`` lesen."""
    if isinstance(value, str):
        return value.strip()
    entries = [entry for entry in (value or []) if isinstance(entry, dict)]
    for entry in entries:
        language = str(entry.get("language") or "").upper()
        if language.startswith("DE") and entry.get("text"):
            return str(entry["text"]).strip()
    for entry in entries:
        if entry.get("text"):
            return str(entry["text"]).strip()
    return ""


def parse_holidays(payload, kind: str) -> list[Holiday]:
    """JSON-Antwort der API in ``Holiday``-Eintraege uebersetzen."""
    if not isinstance(payload, list):
        raise HolidayError(f"Unerwartete Antwort fuer {kind}")
    holidays: list[Holiday] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            start = date.fromisoformat(str(item["startDate"]))
            end = date.fromisoformat(str(item["endDate"]))
        except (KeyError, ValueError):
            continue
        if end < start:
            start, end = end, start
        holidays.append(Holiday(start=start, end=end, name=_name(item.get("name")), kind=kind))
    return holidays


def _api_url(endpoint: str, iso_code: str, start: date, end: date) -> str:
    query = urlencode(
        {
            "countryIsoCode": "DE",
            "subdivisionCode": iso_code,
            "languageIsoCode": "DE",
            "validFrom": start.isoformat(),
            "validTo": end.isoformat(),
        }
    )
    return f"{API_BASE}/{endpoint}?{query}"


def _fetch_endpoint(
    endpoint: str, iso_code: str, start: date, end: date, timeout: int
) -> list[Holiday]:
    request = Request(
        _api_url(endpoint, iso_code, start, end),
        headers={"Accept": "application/json", "User-Agent": "ha-iserv-school-plan"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - feste API-Adresse
        payload = json.loads(response.read().decode("utf-8"))
    return parse_holidays(payload, dict(ENDPOINTS)[endpoint])


def fetch_holidays(
    bundesland: str,
    now: datetime | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> HolidayCalendar:
    """Ferien und Feiertage fuer die naechsten ``COVERAGE_DAYS`` Tage laden."""
    name = normalize_bundesland(bundesland)
    iso_code = BUNDESLAENDER[name]
    now = now or datetime.now(TZ)
    start = now.date()
    end = start + timedelta(days=COVERAGE_DAYS)

    calendar = HolidayCalendar(
        bundesland=name,
        fetched_at=now,
        source=f"{API_BASE} ({iso_code})",
    )
    errors: list[str] = []
    for endpoint, kind in ENDPOINTS:
        try:
            calendar.ranges.extend(_fetch_endpoint(endpoint, iso_code, start, end, timeout))
        except (OSError, URLError, ValueError, HolidayError) as err:
            errors.append(f"{endpoint}: {err}")
            continue
    if len(errors) == len(ENDPOINTS):
        raise HolidayError("; ".join(errors))
    if errors:
        calendar.warnings.extend(f"Ferien/Feiertage unvollstaendig ({err})" for err in errors)
    calendar.ranges.sort(key=lambda entry: (entry.start, entry.name))
    _LOGGER.info(
        "Ferien/Feiertage fuer %s geladen: %s Eintraege bis %s",
        name,
        len(calendar.ranges),
        end,
    )
    return calendar


def read_cache(path: Path | None = None) -> HolidayCalendar | None:
    target = path or CACHE_PATH
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        calendar = HolidayCalendar.from_dict(payload)
    except (OSError, ValueError, TypeError) as err:
        _LOGGER.warning("Ferien-Cache unlesbar: %s", err)
        return None
    if calendar.fetched_at is not None and calendar.fetched_at.tzinfo is None:
        calendar.fetched_at = calendar.fetched_at.replace(tzinfo=TZ)
    return calendar


def write_cache(calendar: HolidayCalendar, path: Path | None = None) -> None:
    target = path or CACHE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(
            json.dumps(calendar.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(target)
    except OSError as err:
        _LOGGER.warning("Ferien-Cache konnte nicht geschrieben werden: %s", err)


def load_holidays(
    bundesland: str,
    path: Path | None = None,
    now: datetime | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> HolidayCalendar:
    """Cache bevorzugen, sonst API; bei Netzfehlern alten Cache nutzen."""
    now = now or datetime.now(TZ)
    name = normalize_bundesland(bundesland)
    cached = read_cache(path)
    if cached is not None and cached.bundesland == name and not cached.is_stale(now):
        _LOGGER.debug("Ferien/Feiertage aus dem Cache (%s)", cached.fetched_at)
        return cached
    try:
        calendar = fetch_holidays(name, now=now, timeout=timeout)
    except HolidayError as err:
        _LOGGER.warning("Ferien/Feiertage konnten nicht geladen werden: %s", err)
        if cached is not None and cached.bundesland == name:
            stamp = cached.fetched_at.strftime("%d.%m.%Y") if cached.fetched_at else "unbekannt"
            cached.warnings.append(
                f"Ferien/Feiertage nicht aktualisierbar - Stand {stamp} wird genutzt"
            )
            return cached
        return HolidayCalendar(
            bundesland=name,
            warnings=[f"Ferien/Feiertage nicht verfuegbar ({err})"],
        )
    write_cache(calendar, path)
    return calendar

