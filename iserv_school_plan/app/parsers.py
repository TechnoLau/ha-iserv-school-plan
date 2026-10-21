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
# Wochennamen der IServ-Stundenplanansicht (``Montag``, ``Mo``, ``Mo., 19.10.``).
# Dieses Wochengitter hat *keine* Stunde/Zeit-Kopfzeile - es wird deshalb
# getrennt von den Vertretungsplan-Tabellen erkannt.
WEEKDAY_NAMES = (
    "montag",
    "dienstag",
    "mittwoch",
    "donnerstag",
    "freitag",
    "samstag",
    "sonntag",
)
WEEKDAY_ABBR = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}
# Raumangaben in Gitterzellen (``A1``, ``R 204``, ``Raum 12``). Faecher und
# Lehrernamen enthalten keine Ziffern und werden dadurch nicht als Raum gelesen.
ROOM_RE = re.compile(r"^(?:raum\s*)?[A-Za-zÄÖÜäöü]{1,3}[\s\-.]?\d{1,3}[a-z]?$")
CANCEL_STRONG_RE = re.compile(
    r"f(?:ä|ae)llt\s+aus|entf(?:ä|ae)llt|entfall|ausfall|abgesagt", re.IGNORECASE
)

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
    match = re.match(r"^(\d{1,2})(?!\s*:)\s*(?:[.\-]|\s|$)", text)
    if match:
        # Gitterzellen der IServ-Stundenplanansicht: ``1 07:45 - 08:30``.
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


def weekday_index(text: str | None) -> int | None:
    """Wochentag einer Spaltenueberschrift (0 = Montag, 6 = Sonntag)."""
    value = normalize_text(text).lower()
    if not value:
        return None
    for index, name in enumerate(WEEKDAY_NAMES):
        if name in value:
            return index
    match = re.match(r"^(mo|di|mi|do|fr|sa|so)\b", value)
    if match:
        return WEEKDAY_ABBR[match.group(1)]
    return None


def _grid_date(text: str | None, default_year: int | None) -> date | None:
    """Datum einer Spaltenueberschrift - mit oder ohne Jahr.

    IServ schreibt in der Wochenansicht ``Montag, 19.10.`` (ohne Jahr) oder
    ``Montag, 19.10.2026``. Ohne Jahr gilt das Jahr des Abrufs.
    """
    found = find_date(text, default_year)
    if found is not None:
        return found
    match = re.search(r"\b(\d{1,2})\.\s?(\d{1,2})\.", text or "")
    if not match:
        return None
    year = default_year or datetime.now(TZ).year
    return _build_date(int(match.group(1)), int(match.group(2)), year, year)


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


def _int_attr(attrs, name: str) -> int:
    """Liest ``rowspan``/``colspan`` einer Zelle (mindestens 1)."""
    for key, value in attrs or ():
        if key == name and value:
            try:
                return max(1, min(100, int(str(value).strip())))
            except ValueError:
                return 1
    return 1


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
        self._chunks: list[str] = []
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
            self._cell = {
                "text": "",
                "header": tag == "th",
                # Zeilenumbrueche getrennt halten: im Wochengitter steht in einer
                # Zelle je Zeile eine Angabe (Fach, Lehrkraft, Raum).
                "lines": [],
                # Verbundene Zellen (Doppelstunden) verschieben sonst alle
                # folgenden Spalten.
                "rowspan": _int_attr(attrs, "rowspan"),
                "colspan": _int_attr(attrs, "colspan"),
            }
            self._chunks = []
            self._row["cells"].append(self._cell)
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += " "
            self._flush_cell_line()
        elif tag in self.CONTEXT_TAGS and not self._stack:
            self._capture = []

    def _flush_cell_line(self) -> None:
        """Schliesst die aktuelle Zeile einer Tabellenzelle ab."""
        if self._cell is None:
            return
        text = normalize_text("".join(self._chunks))
        if text:
            self._cell["lines"].append(text)
        self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag in ("td", "th"):
            self._flush_cell_line()
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
            self._chunks.append(data)
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


def _detect_grid(
    rows: list[dict], default_year: int | None
) -> tuple[int, dict[int, date]] | None:
    """Erkennt ein Wochengitter: Spaltenueberschriften mit Datum je Tag.

    Die IServ-Stundenplanansicht hat keine Stunde/Zeit-Kopfzeile, sondern
    Tage als Spalten (``Mo., 19.10.`` oder ``19.10.``). Ohne Datum in der
    Ueberschrift laesst sich keine Stunde einem Tag zuordnen - dann wird das
    Gitter bewusst nicht ausgewertet. Reine Datumsspalten werden nur akzeptiert,
    wenn die erste Kopfzelle eine Stundenspalte ist (``Stunde``/``Std``).
    """
    best: tuple[int, dict[int, date]] | None = None
    for index, row in enumerate(rows[:4]):
        cells = row["cells"]
        columns: dict[int, date] = {}
        weekday_hits = 0
        for position, cell in enumerate(cells):
            day = _grid_date(cell["text"], default_year)
            if day is None:
                continue
            if weekday_index(cell["text"]) is not None:
                weekday_hits += 1
            columns[position] = day
        if len(columns) < 2:
            continue
        if weekday_hits < 1 and not (
            cells and _match_field(cells[0]["text"]) in ("period", "time")
        ):
            continue
        if best is None or len(columns) > len(best[1]):
            best = (index, columns)
    return best


def _expand_rows(rows: list[dict]) -> list[list[dict | None]]:
    """Setzt ``rowspan``/``colspan`` in ein rechteckiges Raster um.

    Doppelstunden sind in IServ-Zeitplaenen verbundene Zellen. Ohne diese
    Aufloesung waeren alle folgenden Spalten um eine Position verschoben.
    """
    grid: list[list[dict | None]] = []
    spans: dict[int, tuple[dict, int]] = {}
    for row in rows:
        line: list[dict | None] = []
        pending = iter(row["cells"])
        next_cell = next(pending, None)
        column = 0
        while next_cell is not None or column in spans:
            if column in spans:
                cell, remaining = spans[column]
                line.append(cell)
                if remaining <= 1:
                    del spans[column]
                else:
                    spans[column] = (cell, remaining - 1)
                column += 1
                continue
            cell = next_cell
            next_cell = next(pending, None)
            rowspan = cell.get("rowspan") or 1
            colspan = cell.get("colspan") or 1
            for offset in range(colspan):
                line.append(cell)
                if rowspan > 1:
                    spans[column + offset] = (cell, rowspan - 1)
            column += colspan
        grid.append(line)
    return grid


def _has_weekday_header(rows: list[dict]) -> bool:
    """True, wenn eine der ersten Zeilen mehrere Wochentage als Spalten hat."""
    for row in rows[:4]:
        weekdays = [cell for cell in row["cells"] if weekday_index(cell["text"]) is not None]
        if len(weekdays) >= 2:
            return True
    return False


def _has_date_columns(rows: list[dict]) -> bool:
    """True, wenn eine der ersten Zeilen mehrere Datumsspalten hat."""
    for row in rows[:4]:
        dated = [cell for cell in row["cells"] if find_date(cell["text"]) is not None]
        if len(dated) >= 2:
            return True
    return False


def _grid_cell_values(lines: list[str]) -> tuple[str, str, str]:
    """Fach, Raum und Notiz aus den Zeilen einer Gitterzelle.

    IServ schreibt je Zeile eine Angabe (Fach, Lehrkraft, Raum). Die erste
    Zeile ist das Fach, eine Zeile mit Raummuster der Raum, der Rest die Notiz.
    """
    parts = [normalize_text(line) for line in lines or [] if normalize_text(line)]
    if not parts:
        return "", "", ""
    subject, room, notes = parts[0], "", []
    for part in parts[1:]:
        if not room and ROOM_RE.match(part):
            room = part
        else:
            notes.append(part)
    return subject, room, ", ".join(notes)


def _parse_grid(
    rows: list[dict],
    header_index: int,
    columns: dict[int, date],
    dataset: PlanDataset,
) -> None:
    """Liest eine Wochenansicht (Spalten = Tage) in das Dataset."""
    first_day = min(columns)
    for line in _expand_rows(rows[header_index + 1 :]):
        if not line:
            continue
        head = " ".join(
            normalize_text(cell["text"])
            for cell in line[:first_day]
            if cell is not None and normalize_text(cell["text"])
        )
        period = extract_period(head)
        start, end = parse_time_range(head)
        if period is None and start is None:
            continue
        for position, day in columns.items():
            if position >= len(line) or line[position] is None:
                continue
            cell = line[position]
            text = normalize_text(cell["text"])
            if not text:
                continue
            subject, room, note = _grid_cell_values(cell.get("lines") or [text])
            if not subject:
                continue
            cancelled = is_cancelled(note) or bool(
                CANCEL_STRONG_RE.search(f"{subject} {text}")
            )
            dataset.add_lesson(
                Lesson(
                    period=period,
                    start=start,
                    end=end,
                    subject=subject,
                    room=room,
                    cancelled=cancelled,
                    note=note,
                    date=day,
                )
            )


def table_summary(text: str, limit: int = 3) -> str:
    """Kurzfassung der Tabellenstruktur fuer Log und Fehlermeldung.

    Ohne diese Angaben steht im Log nur "keine Spaltenueberschrift" - damit ist
    nicht erkennbar, ob die Seite ein Wochengitter, eine JavaScript-Huelle oder
    eine ganz andere Tabelle liefert.
    """
    collector = _TableCollector()
    try:
        collector.feed(text)
        collector.close()
    except Exception:  # pragma: no cover - defektes HTML darf nicht stoeren
        return "Tabellen nicht lesbar"
    if not collector.tables:
        return "keine Tabelle"
    parts: list[str] = []
    for number, table in enumerate(collector.tables[:limit], start=1):
        rows = table["rows"]
        width = max((len(row["cells"]) for row in rows), default=0)
        head = ""
        weekday_header = False
        dated = False
        date_columns = False
        for row in rows[:3]:
            cells = [normalize_text(cell["text"]) for cell in row["cells"]]
            cells = [cell for cell in cells if cell]
            if len(cells) >= 2 and not head:
                head = " | ".join(cells[:6])[:80]
            if len([cell for cell in cells if find_date(cell) is not None]) >= 2:
                date_columns = True
            weekdays = [cell for cell in cells if weekday_index(cell) is not None]
            if len(weekdays) >= 2:
                weekday_header = True
                dated = dated or any(_grid_date(cell, None) is not None for cell in weekdays)
        description = f"{number}) {len(rows)} Zeilen x {width} Spalten"
        if head:
            description += f", Kopf: {head!r}"
        if weekday_header:
            description += ", Wochengitter " + ("mit Datum" if dated else "ohne Datum")
        elif date_columns:
            description += ", Datumsspalten"
        parts.append(description)
    return f"{len(collector.tables)} Tabellen: " + "; ".join(parts)


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
    # Hinweise erst am Ende uebernehmen: nach einem erfolgreichen Gitter-Lauf
    # sollen keine "ignoriert"-Meldungen im Status stehen.
    skipped_tables = 0
    for table in collector.tables:
        rows = table["rows"]
        detected = _detect_header(rows)
        if not detected:
            if rows:
                skipped_tables += 1
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
        # Zweiter Versuch: IServ-Stundenplanansicht (Spalten = Wochentage).
        grid_warnings: list[str] = []
        for table in collector.tables:
            rows = table["rows"]
            grid = _detect_grid(rows, default_year)
            if grid is not None:
                header_index, grid_columns = grid
                _parse_grid(rows, header_index, grid_columns, dataset)
                if dataset.days:
                    break
                grid_warnings.append("Wochengitter ohne verwertbare Zellen erkannt")
            elif _has_weekday_header(rows):
                grid_warnings.append(
                    "Wochengitter ohne Datum in der Spaltenueberschrift ignoriert"
                )
            elif _has_date_columns(rows):
                grid_warnings.append(
                    "Tabelle mit Datumsspalten, aber ohne Stundenspalte ignoriert"
                )
        if not dataset.days:
            if skipped_tables:
                dataset.warnings.append(
                    "Tabelle ohne erkannte Spaltenueberschrift ignoriert"
                )
            dataset.warnings.extend(grid_warnings)

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


# --- IServ-Portalschnittstelle (``/iserv/timetable/data?...``) ---------------
# Die neue IServ-Oberflaeche rendert den Stundenplan im Browser und laedt die
# Stunden als JSON nach. Die Feldnamen unterscheiden sich je nach Installation,
# deshalb wird ueber Aliase gesucht statt ueber ein festes Schema.
PORTAL_FIELDS: dict[str, tuple[str, ...]] = {
    "date": ("date", "datum", "tag", "day", "startdate", "datefrom"),
    "start": (
        "start",
        "beginn",
        "begin",
        "starttime",
        "timefrom",
        "uhrzeit",
        "time",
        "zeit",
    ),
    "end": ("end", "ende", "endtime", "timeto"),
    "period": ("period", "stunde", "stunden", "lesson", "hour", "stundennummer", "periode"),
    "subject": (
        "subject",
        "subjects",
        "fach",
        "faecher",
        "fachname",
        "subjectname",
        "unterricht",
        "lessonname",
        "title",
    ),
    "room": (
        "room",
        "rooms",
        "raum",
        "raeume",
        "raumname",
        "roomname",
        "location",
        "ort",
    ),
    "teacher": (
        "teacher",
        "teachers",
        "lehrer",
        "lehrkraft",
        "lehrkraefte",
        "teachername",
        "lehrername",
    ),
    "note": ("note", "notes", "hinweis", "info", "bemerkung", "comment", "description"),
    "cancelled": (
        "cancelled",
        "canceled",
        "iscancelled",
        "iscanceled",
        "is_cancelled",
        "ausfall",
        "entfall",
        "istausfall",
        "cancel",
    ),
    "type": ("type", "art", "typ", "kind", "status"),
}
# Werte sind oft Objekte (``{"name": "Mathematik"}``) statt Strings.
PORTAL_NAME_KEYS = (
    "name",
    "label",
    "title",
    "text",
    "value",
    "short",
    "long",
    "display",
    "kuerzel",
    "abbreviation",
)


def _portal_key(value: str) -> str:
    return re.sub(r"[\s_\-]", "", value).lower()


_PORTAL_ALIASES = {
    field: tuple(_portal_key(alias) for alias in aliases)
    for field, aliases in PORTAL_FIELDS.items()
}


def _portal_value(value, depth: int = 2) -> str:
    """Text aus einem beliebigen JSON-Wert (auch ``{"name": ...}``)."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    if depth > 0 and isinstance(value, dict):
        for key in PORTAL_NAME_KEYS:
            if key in value:
                found = _portal_value(value[key], depth - 1)
                if found:
                    return found
        return ""
    if depth > 0 and isinstance(value, list):
        for item in value:
            found = _portal_value(item, depth - 1)
            if found:
                return found
    return ""


def _portal_lookup(node: dict) -> dict[str, object]:
    """Ordnet die Schluessel eines Eintrags den bekannten Feldern zu."""
    index: dict[str, object] = {}
    for key, value in node.items():
        if isinstance(key, str):
            index.setdefault(_portal_key(key), value)
    found: dict[str, object] = {}
    for field, aliases in _PORTAL_ALIASES.items():
        for alias in aliases:
            if alias in index:
                found[field] = index[alias]
                break
    return found


def _portal_date(value, default_year: int | None = None) -> date | None:
    """Datum aus ``2026-10-19``, ``19.10.2026``, ``19.10.`` oder Zeitstempel."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, TZ).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = _portal_value(value)
    if not text:
        return None
    found = find_date(text, default_year)
    if found is not None:
        return found
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        return _build_date(int(iso.group(3)), int(iso.group(2)), int(iso.group(1)), None)
    short = re.match(r"^(\d{1,2})\.\s?(\d{1,2})\.?$", text)
    if short:
        year = default_year or datetime.now(TZ).year
        return _build_date(int(short.group(1)), int(short.group(2)), year, year)
    return None


def _portal_time(value) -> time | None:
    text = _portal_value(value)
    if not text:
        return None
    iso = re.search(r"T(\d{1,2}):(\d{2})", text)
    if iso:
        text = f"{iso.group(1)}:{iso.group(2)}"
    return parse_time(text)


def _portal_period_key(key) -> int | None:
    """Stundennummer aus einem Zweig-Schluessel (``"3": {...}``)."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key if 1 <= key <= 20 else None
    if isinstance(key, str) and key.strip().isdigit():
        value = int(key.strip())
        return value if 1 <= value <= 20 else None
    return None


def _portal_lesson(
    node: dict,
    day: date | None,
    default_year: int | None,
    context_period: int | None = None,
) -> Lesson | None:
    """Ein Eintrag als Unterrichtsstunde - oder ``None``, wenn es keine ist."""
    if day is None:
        return None
    found = _portal_lookup(node)
    subject = _portal_value(found.get("subject"))
    # Fach muss Text enthalten ("1" oder "" ist kein Fach).
    if not re.search(r"[^\W\d_]", subject):
        return None
    period = extract_period(_portal_value(found.get("period")))
    if period is None:
        # Gitterform: ``"2026-10-19": {"3": {"fach": ...}}`` - die Stunde steht
        # im Schluessel des Zweigs.
        period = context_period
    start = _portal_time(found.get("start"))
    if period is None and start is None:
        return None
    kind = _portal_value(found.get("type"))
    note_parts = [_portal_value(found.get("teacher")), _portal_value(found.get("note"))]
    note = ", ".join(part for part in note_parts if part)
    cancelled_value = found.get("cancelled")
    cancelled = (
        cancelled_value is True
        or _portal_value(cancelled_value).lower() in ("true", "1", "ja", "yes")
        or bool(CANCEL_STRONG_RE.search(kind))
        or bool(CANCEL_STRONG_RE.search(note))
    )
    return Lesson(
        period=period,
        start=start,
        end=_portal_time(found.get("end")),
        subject=subject,
        room=_portal_value(found.get("room")),
        cancelled=cancelled,
        note=note,
        date=day,
    )


def _walk_portal(
    node,
    context_day: date | None,
    default_year: int | None,
    lessons: list[Lesson],
    seen: set[tuple],
    context_period: int | None = None,
    depth: int = 0,
) -> None:
    """Sucht rekursiv nach Stunden; Datum kann am Eintrag oder am Zweig haengen."""
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node:
            _walk_portal(
                item, context_day, default_year, lessons, seen, context_period, depth + 1
            )
        return
    if not isinstance(node, dict):
        return
    own_day = _portal_date(_portal_lookup(node).get("date"), default_year) or context_day
    lesson = _portal_lesson(node, own_day, default_year, context_period)
    if lesson is not None:
        signature = (
            lesson.date,
            lesson.period,
            lesson.start,
            lesson.end,
            lesson.subject,
            lesson.room,
            lesson.cancelled,
        )
        if signature not in seen:
            seen.add(signature)
            lessons.append(lesson)
        return
    for key, value in node.items():
        if not isinstance(value, (dict, list)):
            continue
        child_day = _portal_date(key, default_year) if isinstance(key, str) else None
        child_period = _portal_period_key(key)
        _walk_portal(
            value,
            child_day or own_day,
            default_year,
            lessons,
            seen,
            child_period if child_period is not None else context_period,
            depth + 1,
        )


def parse_portal_json(
    text: str, source: str = "", default_year: int | None = None
) -> PlanDataset:
    """Parst die JSON-Schnittstelle der IServ-Stundenplanansicht.

    Die Struktur unterscheidet sich je nach IServ-Version (Liste von Stunden,
    nach Tagen gruppiert, Feldnamen deutsch oder englisch). Erkannt wird eine
    Stunde an ``Datum`` + ``Fach`` + (``Stunde`` oder ``Beginn``) - alles andere
    (Konfiguration, Zeitraster, Rechte) wird ignoriert.
    """
    dataset = PlanDataset(source=source or "iserv-json")
    lessons: list[Lesson] = []
    try:
        payload = json.loads(text.lstrip("\ufeff"))
    except ValueError as err:
        dataset.warnings.append(f"IServ-JSON nicht lesbar: {err}")
        return dataset
    _walk_portal(payload, None, default_year, lessons, set())
    for lesson in lessons:
        dataset.add_lesson(lesson, is_school_day=True)
    if not dataset.days:
        dataset.warnings.append(
            "IServ-JSON ohne Stunden - Struktur: " + json_summary(text)
        )
    return dataset


def _json_sample(value) -> str:
    """Wertangabe fuer die Struktur-Kurzbeschreibung (keine Namen)."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        if re.fullmatch(r"[\d.:\-/T+ ]{1,25}", value):
            return repr(value)
        return f"<str {len(value)}>"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    return type(value).__name__


def _json_describe(node, depth: int = 0) -> str:
    if depth > 2:
        return "..."
    if isinstance(node, dict):
        parts = []
        for key, value in list(node.items())[:8]:
            if isinstance(value, (dict, list)):
                parts.append(f"{key}: {_json_describe(value, depth + 1)}")
            else:
                parts.append(f"{key}: {_json_sample(value)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(node, list):
        return "[]" if not node else f"[{len(node)}x {_json_describe(node[0], depth + 1)}]"
    return _json_sample(node)


def json_summary(text: str, limit: int = 400) -> str:
    """Kurzfassung einer JSON-Antwort fuer Log und Fehlermeldung.

    Nur Schluessel und Typen (Datums-/Zeitwerte im Klartext), damit im Log
    erkennbar ist, wie die Schnittstelle aufgebaut ist - ohne Namen oder
    Schuelerdaten zu protokollieren.
    """
    try:
        payload = json.loads(text.lstrip("\ufeff"))
    except ValueError as err:
        return f"kein gueltiges JSON ({err})"
    summary = _json_describe(payload)
    if len(summary) > limit:
        summary = summary[:limit] + "..."
    return summary


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



