"""Parser fuer die unterstuetzten Plan-Formate (ICS, IServ-HTML, JSON).

Alle Parser liefern ein :class:`plan.PlanDataset`. Sie greifen nur auf die
Standardbibliothek zurueck, damit das Add-on schlank bleibt.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from .plan import DayPlan, Lesson, PlanDataset, TZ

CANCEL_RE = re.compile(
    r"f(?:ä|ae)llt\s+aus|entf(?:ä|ae)llt|entfall|ausfall|abgesagt|gestrichen|"
    r"kein\s+unterricht|nicht\s+statt|^EA$",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{2,4})\b")
TIME_RE = re.compile(r"\b(\d{1,2})\s*[:.]\s*(\d{2})\b")
PERIOD_RE = re.compile(r"^\s*(\d{1,2})\s*(?:[.\-/]\s*\d{1,2}\s*)?\.?\s*$")

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "period": ("stunde", "stunden", "std", "std.", "periode", "period"),
    "time": ("zeit", "uhrzeit", "beginn", "time"),
    "subject": ("fach", "faecher", "fächer", "unterricht", "stundeninhalt", "subject"),
    "room": ("raum", "raeume", "räume", "raum-aenderung", "raumänderung", "location"),
    "note": (
        "text",
        "info",
        "information",
        "bemerkung",
        "hinweis",
        "art",
        "vertretung",
        "lehrer",
        "lehrkraft",
        "kommentar",
        "note",
    ),
    "klasse": ("klasse", "kurs", "class", "gruppe", "lerngruppe"),
    "date": ("datum", "tag", "date"),
}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def extract_period(text: str) -> int | None:
    """Liest die Stundennummer aus Texten wie ``1``, ``1.``, ``Stunde 2``."""
    text = normalize_text(text)
    if not text:
        return None
    match = PERIOD_RE.match(text)
    if match:
        return int(match.group(1))
    match = re.search(r"stunde\s*(\d{1,2})", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d{1,2})\s*\.\s*(?:stunde|std)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def parse_time(value: str | None) -> time | None:
    if not value:
        return None
    match = TIME_RE.search(value)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour < 24 and minute < 60:
            return time(hour=hour, minute=minute)
    compact = re.fullmatch(r"(\d{1,2})(\d{2})", value.strip())
    if compact:
        hour, minute = int(compact.group(1)), int(compact.group(2))
        if hour < 24 and minute < 60:
            return time(hour=hour, minute=minute)
    return None


def parse_time_range(value: str | None) -> tuple[time | None, time | None]:
    """``07:45 - 08:30`` -> (07:45, 08:30)."""
    if not value:
        return None, None
    matches = TIME_RE.findall(value)
    start = parse_time(f"{matches[0][0]}:{matches[0][1]}") if matches else parse_time(value)
    end = parse_time(f"{matches[1][0]}:{matches[1][1]}") if len(matches) > 1 else None
    return start, end


def _build_date(day: int, month: int, year: int, default_year: int | None) -> date | None:
    if year < 100:
        year += 2000
    elif default_year and year < 2000:
        year = default_year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_date(text: str, default_year: int | None = None) -> date | None:
    if not text:
        return None
    match = DATE_RE.search(text)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    return _build_date(day, month, year, default_year)


def find_last_date(text: str, default_year: int | None = None) -> date | None:
    """Letztes Datum im Text - fuer Tabellen mit fortlaufenden Ueberschriften."""
    if not text:
        return None
    matches = DATE_RE.findall(text)
    for day, month, year in reversed(matches):
        parsed = _build_date(int(day), int(month), int(year), default_year)
        if parsed is not None:
            return parsed
    return None


def is_cancelled(*values: str | None) -> bool:
    return any(CANCEL_RE.search(normalize_text(value)) for value in values if value)


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    name_part, sep, value = line.partition(":")
    if not sep:
        return None
    parts = name_part.split(";")
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.upper()] = val.strip().strip('"')
    return parts[0].upper(), params, value


def _parse_ics_datetime(value: str, params: dict[str, str]) -> datetime | date | None:
    value = value.strip()
    if not value:
        return None
    if value.endswith("Z"):
        try:
            return (
                datetime.strptime(value[:-1], "%Y%m%dT%H%M%S")
                .replace(tzinfo=timezone.utc)
                .astimezone(TZ)
            )
        except ValueError:
            return None
    if "T" in value:
        try:
            naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
        except ValueError:
            return None
        tzid = params.get("TZID")
        try:
            tzinfo = ZoneInfo(tzid) if tzid else TZ
        except Exception:  # unbekannte TZID -> Europe/Berlin
            tzinfo = TZ
        return naive.replace(tzinfo=tzinfo).astimezone(TZ)
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except (ValueError, IndexError):
        return None


class _TableCollector(HTMLParser):
    """Sammelt Tabellen inkl. der vorangehenden Datums-Ueberschrift."""

    CONTEXT_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "caption", "title", "legend")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._stack: list[dict] = []
        self._context: list[str] = []
        self._row: dict | None = None
        self._cell: dict | None = None
        self._capture: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "table":
            table = {"context": normalize_text(" ".join(self._context[-20:])), "rows": []}
            self.tables.append(table)
            self._stack.append(table)
        elif tag == "tr" and self._stack:
            self._row = {"cells": []}
            self._stack[-1]["rows"].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "header": tag == "th"}
            self._row["cells"].append(self._cell)
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += " "
        elif tag in self.CONTEXT_TAGS and not self._stack:
            self._capture = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None
        elif tag == "table" and self._stack:
            self._stack.pop()
        elif tag in self.CONTEXT_TAGS and self._capture is not None:
            text = normalize_text(" ".join(self._capture))
            if text:
                self._context.append(text)
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell["text"] += data
        elif self._capture is not None:
            self._capture.append(data)
        elif not self._stack:
            text = normalize_text(data)
            if text:
                self._context.append(text)


def _match_field(text: str) -> str | None:
    value = normalize_text(text).lower().rstrip(":")
    if not value:
        return None
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if value == alias or value.startswith(alias):
                return field
    return None


def _detect_header(rows: list[dict]) -> tuple[int, dict[str, int]] | None:
    best: tuple[int, dict[str, int]] | None = None
    for index, row in enumerate(rows[:3]):
        columns: dict[str, int] = {}
        for position, cell in enumerate(row["cells"]):
            field = _match_field(cell["text"])
            if field and field not in columns:
                columns[field] = position
        if "period" not in columns and "time" not in columns:
            continue
        if len(columns) < 2:
            continue
        if best is None or len(columns) > len(best[1]):
            best = (index, columns)
    return best


def parse_html(
    text: str,
    source: str = "html",
    klasse: str | None = None,
    default_year: int | None = None,
) -> PlanDataset:
    """Parst IServ-Vertretungsplan-Tabellen (Spalten Stunde/Zeit/Fach/Raum/...)."""
    collector = _TableCollector()
    collector.feed(text)
    collector.close()

    dataset = PlanDataset(source=source)
    for table in collector.tables:
        rows = table["rows"]
        detected = _detect_header(rows)
        if not detected:
            if rows:
                dataset.warnings.append("Tabelle ohne erkannte Spaltenueberschrift ignoriert")
            continue
        header_index, columns = detected
        table_date = find_last_date(table["context"], default_year)

        for row in rows[header_index + 1 :]:
            cells = [normalize_text(cell["text"]) for cell in row["cells"]]
            if not any(cells):
                continue

            def value(field: str) -> str:
                index = columns.get(field)
                return cells[index] if index is not None and index < len(cells) else ""

            row_date = find_date(value("date"), default_year) or table_date
            if row_date is None:
                continue

            period = extract_period(value("period"))
            start, end = parse_time_range(value("time"))
            subject, room = value("subject"), value("room")
            note = value("note")
            full_text = " ".join(cells)

            if period is None and start is None and not subject:
                continue
            if klasse:
                row_class = value("klasse")
                if row_class and klasse.lower() not in row_class.lower():
                    continue

            cancelled = is_cancelled(full_text, note)
            dataset.add_lesson(
                Lesson(
                    period=period,
                    start=start,
                    end=end,
                    subject=subject,
                    room=room,
                    cancelled=cancelled,
                    note=note,
                    date=row_date,
                )
            )

    if not dataset.days:
        dataset.warnings.append("HTML enthielt keine auswertbaren Unterrichtszeilen")
        if "<table" not in text.lower():
            dataset.warnings.append(
                "keine HTML-Tabelle in der Antwort - plan_url zeigt vermutlich auf die "
                "JavaScript-Web-App von IServ; bitte die klassische Plantabelle "
                "verwenden (z. B. /iserv/plan)"
            )
    return dataset


def parse_ics(text: str, source: str = "ics") -> PlanDataset:
    """Parst einen iCalendar-Feed. Ganztagstermine werden ignoriert."""
    dataset = PlanDataset(source=source)
    events: list[dict[str, tuple[str, dict[str, str]]]] = []
    current: dict[str, tuple[str, dict[str, str]]] | None = None

    for line in _unfold(text):
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            current = {}
            continue
        if upper.startswith("END:VEVENT"):
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None:
            continue
        prop = _split_property(line)
        if prop:
            name, params, value = prop
            current[name] = (_unescape(value), params)

    for event in events:
        start_value, start_params = event.get("DTSTART", ("", {}))
        if not start_value or "T" not in start_value:
            continue  # ganztags / ohne Uhrzeit
        start_dt = _parse_ics_datetime(start_value, start_params)
        if not isinstance(start_dt, datetime):
            continue
        end_dt = None
        end_value, end_params = event.get("DTEND", ("", {}))
        if end_value and "T" in end_value:
            parsed_end = _parse_ics_datetime(end_value, end_params)
            if isinstance(parsed_end, datetime):
                end_dt = parsed_end
        summary = normalize_text(event.get("SUMMARY", ("", {}))[0])
        description = normalize_text(event.get("DESCRIPTION", ("", {}))[0])
        location = normalize_text(event.get("LOCATION", ("", {}))[0])
        status = (event.get("STATUS", ("", {}))[0] or "").upper()
        cancelled = status == "CANCELLED" or is_cancelled(summary, description)
        dataset.add_lesson(
            Lesson(
                period=extract_period(summary) or extract_period(description),
                start=start_dt.time().replace(tzinfo=None),
                end=end_dt.time().replace(tzinfo=None) if end_dt else None,
                subject=summary,
                room=location,
                cancelled=cancelled,
                note=description,
                date=start_dt.date(),
            )
        )

    if not dataset.days:
        dataset.warnings.append("ICS enthielt keine Termine mit Uhrzeit")
    return dataset


def parse_json(text: str, source: str = "json") -> PlanDataset:
    """Parst das dokumentierte JSON-Schema (``days`` -> ``lessons``)."""
    payload = json.loads(text.lstrip("\ufeff"))
    entries = payload.get("days", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("JSON: 'days' muss eine Liste sein")

    dataset = PlanDataset(source=source)
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("date"):
            continue
        day = date.fromisoformat(str(entry["date"]))
        day_plan = DayPlan(
            date=day,
            is_school_day=entry.get("is_school_day"),
            holiday_name=entry.get("holiday_name"),
        )
        for item in entry.get("lessons") or []:
            period = item.get("period")
            try:
                period = int(period) if period is not None else None
            except (TypeError, ValueError):
                period = None
            start = parse_time(item.get("start"))
            end = parse_time(item.get("end"))
            day_plan.lessons.append(
                Lesson(
                    period=period,
                    start=start,
                    end=end,
                    subject=normalize_text(item.get("subject")),
                    room=normalize_text(item.get("room")),
                    cancelled=bool(item.get("cancelled")),
                    note=normalize_text(item.get("note")),
                    date=day,
                )
            )
        dataset.days[day] = day_plan

    if not dataset.days:
        dataset.warnings.append("JSON enthielt keine Tage")
    return dataset


def detect_format(text: str, content_type: str = "") -> str:
    content_type = (content_type or "").lower()
    if "calendar" in content_type:
        return "ics"
    if "json" in content_type:
        return "json"
    head = text.lstrip("\ufeff \t\r\n")[:200]
    if head.upper().startswith("BEGIN:VCALENDAR"):
        return "ics"
    if head[:1] in ("{", "["):
        return "json"
    return "html"


def parse_payload(
    text: str,
    fmt: str = "auto",
    source: str = "",
    klasse: str | None = None,
    default_year: int | None = None,
    content_type: str = "",
) -> PlanDataset:
    """Dispatcht auf den passenden Parser."""
    if fmt == "auto":
        fmt = detect_format(text, content_type)
    if fmt == "ics":
        return parse_ics(text, source or "ics")
    if fmt == "json":
        return parse_json(text, source or "json")
    if fmt == "html":
        return parse_html(text, source or "html", klasse=klasse, default_year=default_year)
    raise ValueError(f"Unbekanntes Plan-Format: {fmt}")



