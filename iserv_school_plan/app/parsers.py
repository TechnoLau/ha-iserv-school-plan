"""Parser fuer die unterstuetzten Plan-Formate (ICS, IServ-HTML, JSON).

Alle Parser liefern ein :class:`plan.PlanDataset`. Sie greifen nur auf die
Standardbibliothek zurueck, damit das Add-on schlank bleibt.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
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


# Wochenanker: Die IServ-Stundenplanansicht beschriftet ihre beiden Gitter je
# nach Version nur mit Wochentagen ("Montag" ... "Freitag"). Ohne einen Anker
# auf der Seite laesst sich keine Stunde einem Datum zuordnen - deshalb wird
# vor dem Verwerfen eines Gitters nach einem Zeitraum, einer Kalenderwoche oder
# dem Filter-JSON der Ansicht gesucht.
_WEEK_RANGE_RE = re.compile(
    r"(\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{2,4})?)\s*(?:-|–|—|bis)\s*"
    r"(\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{2,4})?)"
)
_KW_RE = re.compile(r"\b(?:kw|kalenderwoche)\s*\.?\s*(\d{1,2})\b", re.IGNORECASE)
_KW_YEAR_RE = re.compile(
    r"\b(?:kw|kalenderwoche)\s*\.?\s*\d{1,2}\s*/\s*(\d{4})\b", re.IGNORECASE
)
_WEEK_START_RE = re.compile(
    r"[\"']?(?:startdate|weekstart|wochenstart|monday)[\"']?\s*[:=]\s*[\"']([^\"']{6,24})[\"']",
    re.IGNORECASE,
)


def _anchor_date(value: str | None, default_year: int | None = None) -> date | None:
    """Datum aus ``19.10.``, ``19.10.2026`` oder ``2026-10-19``."""
    found = _grid_date(value, default_year)
    if found is not None:
        return found
    iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", value or "")
    if iso:
        return _build_date(
            int(iso.group(3)), int(iso.group(2)), int(iso.group(1)), default_year
        )
    return None


def _kw_monday(text: str, default_year: int | None = None) -> date | None:
    """Montag einer Kalenderwoche (``KW 43`` bzw. ``KW 43/2026``)."""
    match = _KW_RE.search(text or "")
    if not match:
        return None
    week = int(match.group(1))
    year_match = _KW_YEAR_RE.search(text or "")
    year = int(year_match.group(1)) if year_match else (default_year or datetime.now(TZ).year)
    if not 1 <= week <= 53:
        return None
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError:
        return None


def _text_week_anchor(text: str, default_year: int | None = None) -> date | None:
    """Montag aus einer Ueberschrift wie ``19.10. - 25.10.2026`` oder ``KW 43``.

    Ein einzelnes Datum im Text gilt bewusst *nicht* als Anker: Kopfzeilen
    enthalten oft ein Datum der letzten Aktualisierung, das nichts ueber die
    abgebildete Woche aussagt.
    """
    match = _WEEK_RANGE_RE.search(text or "")
    if match:
        start = _anchor_date(match.group(1), default_year)
        end = _anchor_date(match.group(2), default_year)
        if start is not None and end is not None and 0 <= (end - start).days <= 8:
            return start - timedelta(days=start.weekday())
    return _kw_monday(text, default_year)


def _page_week_anchor(text: str, default_year: int | None = None) -> date | None:
    """Wochenanker aus dem Filter-JSON der Seite (``"startDate": "19.10.2026"``).

    Das Portal-JavaScript legt den geladenen Zeitraum als JSON in die Seite;
    er beschreibt die erste Wochentabelle.
    """
    for match in _WEEK_START_RE.finditer(text or ""):
        found = _anchor_date(match.group(1), default_year)
        if found is not None:
            return found - timedelta(days=found.weekday())
    return None



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
    rows: list[dict],
    default_year: int | None,
    anchor: date | None = None,
) -> tuple[int, dict[int, date]] | None:
    """Erkennt ein Wochengitter: Tage als Spalten, je Spalte ein Datum.

    Die IServ-Stundenplanansicht hat keine Stunde/Zeit-Kopfzeile, sondern
    Tage als Spalten (``Mo., 19.10.`` oder ``19.10.``). Beschriftet eine
    Installation die Spalten nur mit Wochentagen (``Montag`` ... ``Freitag``),
    kommen die Daten aus dem Wochenanker der Seite (Filter-JSON, Zeitraum in
    der Ueberschrift, ``KW 43``). Ohne Datum *und* ohne Anker laesst sich keine
    Stunde einem Tag zuordnen - dann wird das Gitter bewusst nicht ausgewertet.
    Reine Datumsspalten werden nur akzeptiert, wenn die erste Kopfzelle eine
    Stundenspalte ist (``Stunde``/``Std``).
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
            if anchor is None or columns:
                # Ohne Anker bleibt das Gitter unbrauchbar; einzelne Datums-
                # spalten neben Wochentagen sind mehrdeutig.
                continue
            seen_days: set[int] = set()
            for position, cell in enumerate(cells):
                weekday = weekday_index(cell["text"])
                if weekday is None or weekday in seen_days:
                    continue
                seen_days.add(weekday)
                columns[position] = anchor + timedelta(days=weekday)
            if len(columns) < 2:
                continue
        elif weekday_hits < 1 and not (
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


def _row_head(rows: list[dict], limit: int = 6) -> str:
    """Erste nicht-leere Zellen einer Zeile - fuer Log und Fehlermeldung."""
    for row in rows[:4]:
        cells = [normalize_text(cell["text"]) for cell in row["cells"]]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            return " | ".join(cells[:limit])[:80]
    return ""


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
    page_anchor = _page_week_anchor(text)
    grid_number = 0
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
            if not dated:
                # Der Anker der Seite beschreibt nur das erste Gitter - genau wie
                # beim Auswerten in :func:`parse_html`.
                anchor = _text_week_anchor(table.get("context", ""))
                if anchor is None and grid_number == 0:
                    anchor = page_anchor
                grid_number += 1
                description += (
                    f", Wochenanker {anchor.strftime('%d.%m.%Y')}"
                    if anchor
                    else ", kein Wochenanker"
                )
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
        page_anchor = _page_week_anchor(text, default_year)
        grid_number = 0
        for table in collector.tables:
            rows = table["rows"]
            # Anker je Tabelle: Zeitraum/KW in der Ueberschrift. Nur das erste
            # Gitter darf den Anker der Seite (Filter-JSON) uebernehmen - er
            # beschreibt die zuerst geladene Woche.
            anchor = _text_week_anchor(table.get("context", ""), default_year)
            if anchor is None and grid_number == 0:
                anchor = page_anchor
            grid = _detect_grid(rows, default_year, anchor)
            if grid is not None:
                header_index, grid_columns = grid
                grid_number += 1
                _parse_grid(rows, header_index, grid_columns, dataset)
                if dataset.days:
                    break
                grid_warnings.append("Wochengitter ohne verwertbare Zellen erkannt")
            elif _has_weekday_header(rows):
                if anchor is None:
                    grid_warnings.append(
                        "Wochengitter ohne Datum und ohne Wochenanker ignoriert "
                        f"({len(rows)} Zeilen, Kopf: {_row_head(rows)!r})"
                    )
                else:
                    grid_warnings.append("Wochengitter ohne verwertbare Zellen erkannt")
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
    # Wochentag statt Datum. ``dow`` zaehlt bei IServ ab 1 (Montag), ``weekday``
    # in der DieSchulApp-Schnittstelle ab 0 - beide brauchen den Wochenanker
    # aus dem Filter (``meta.filter.startDate``).
    "dow": ("dow",),
    "weekday": ("weekday", "wochentag", "weekdayindex", "dayofweek", "tagindex"),
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
        "substitutionsubject",
        "subjectsubstitution",
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
        "substitutionroom",
        "roomsubstitution",
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
    # Vertretung/Ausfall steht in einem eigenen Objekt (``change``).
    "change": ("change", "changes", "aenderung", "änderung"),
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
        if value < 100_000_000:
            # Kleine Zahlen sind Wochentage oder Stundennummern, keine
            # Zeitstempel - sonst entstuende ein Datum im Jahr 1970.
            return None
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


_PORTAL_DEEP_KEYS: dict[str, tuple[str, ...]] = {
    "period": ("period", "number", "stunde", "lesson", "slot"),
    "start": ("starttime", "start", "beginn", "begin", "from"),
    "end": ("endtime", "end", "ende", "to"),
}


def _portal_deep(node: dict, field: str, depth: int = 2):
    """Feld auch in Unterobjekten suchen (``timeTableSlot``, ``courseSubject``).

    Die DieSchulApp-Schnittstelle legt Stunde und Uhrzeit in ``timeTableSlot``
    ab, das Fach in ``courseSubject`` - beides Geschwister des Eintrags.
    """
    keys = _PORTAL_DEEP_KEYS.get(field, (field,))
    # Fach und Raum stehen in eigenen Objekten (``subject: {name: ...}``).
    allow_objects = field in ("subject", "room")
    for key, value in node.items():
        if not isinstance(key, str) or not isinstance(value, (dict, list)):
            continue
        if isinstance(value, dict):
            index = {
                _portal_key(name): item
                for name, item in value.items()
                if isinstance(name, str)
            }
            for name in keys:
                if name in index and (
                    allow_objects or not isinstance(index[name], (dict, list))
                ):
                    return index[name]
            if depth > 1:
                nested = _portal_deep(value, field, depth - 1)
                if nested is not None:
                    return nested
        elif depth > 1:
            for item in value:
                if isinstance(item, dict):
                    nested = _portal_deep(item, field, depth - 1)
                    if nested is not None:
                        return nested
    return None


def _portal_weekday(found: dict, week_start: date | None) -> date | None:
    """Datum aus einer Wochentagsangabe - nur mit bekanntem Wochenanker.

    ``weekday`` zaehlt ab 0 (Montag, DieSchulApp), ``dow`` ab 1 (IServ). Eine
    Zahl in ``date``/``tag`` gilt ebenfalls als Wochentag ab 1 - sie wurde von
    :func:`_portal_date` als Zeitstempel verworfen.
    """
    if week_start is None:
        return None
    index: int | None = None
    text = _portal_value(found.get("weekday"))
    if text:
        index = int(text.strip()) if text.strip().isdigit() else weekday_index(text)
    if index is None:
        text = _portal_value(found.get("dow")) or _portal_value(found.get("date"))
        if text.strip().isdigit():
            index = int(text.strip()) - 1
        elif text:
            index = weekday_index(text)
    if index is None or not 0 <= index <= 6:
        return None
    return week_start + timedelta(days=index)


def _portal_marks_absent(change: dict) -> bool:
    """True bei Ausfall: ``change_types`` enthaelt die IServ-Kennung ``"0"``."""
    fields = _portal_lookup(change)
    if CANCEL_STRONG_RE.search(_portal_value(fields.get("type"))):
        return True
    for key, value in change.items():
        if not isinstance(key, str) or _portal_key(key) not in ("changetypes", "types"):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = _portal_value(item).strip()
            if text == "0" or CANCEL_STRONG_RE.search(text):
                return True
    return False


def _portal_lesson(
    node: dict,
    day: date | None,
    default_year: int | None,
    context_period: int | None = None,
    week_start: date | None = None,
) -> Lesson | None:
    """Ein Eintrag als Unterrichtsstunde - oder ``None``, wenn es keine ist."""
    found = _portal_lookup(node)
    if day is None:
        # Wochentag statt Datum (``dow``/``weekday``) - nur mit Wochenanker.
        day = _portal_weekday(found, week_start)
    if day is None:
        return None
    subject = _portal_value(found.get("subject"))
    room = _portal_value(found.get("room"))
    if not subject:
        # z. B. DieSchulApp: ``courseSubject.subject.name``.
        subject = _portal_value(_portal_deep(node, "subject"))
    if not room:
        room = _portal_value(_portal_deep(node, "room"))
    note_parts = [_portal_value(found.get("teacher")), _portal_value(found.get("note"))]
    cancelled = (
        found.get("cancelled") is True
        or _portal_value(found.get("cancelled")).lower() in ("true", "1", "ja", "yes")
        or bool(CANCEL_STRONG_RE.search(_portal_value(found.get("type"))))
    )
    # Vertretung/Ausfall steht in einem eigenen Objekt (``change``): dort finden
    # sich Ersatzfach/-raum und die Kennung fuer Ausfall.
    change = found.get("change")
    for entry in change if isinstance(change, list) else [change]:
        if not isinstance(entry, dict):
            continue
        change_fields = _portal_lookup(entry)
        if _portal_marks_absent(entry):
            cancelled = True
        subject = _portal_value(change_fields.get("subject")) or subject
        room = _portal_value(change_fields.get("room")) or room
        replaced = _portal_value(change_fields.get("note"))
        if replaced:
            note_parts.append(replaced)
    # Fach muss Text enthalten ("1" oder "" ist kein Fach).
    if not re.search(r"[^\W\d_]", subject):
        return None
    period = extract_period(_portal_value(found.get("period")))
    if period is None:
        # z. B. DieSchulApp: ``timeTableSlot.number`` neben ``courseSubject``.
        period = extract_period(_portal_value(_portal_deep(node, "period")))
    if period is None:
        # Gitterform: ``"2026-10-19": {"3": {"fach": ...}}`` - die Stunde steht
        # im Schluessel des Zweigs.
        period = context_period
    start = _portal_time(found.get("start") or _portal_deep(node, "start"))
    if period is None and start is None:
        return None
    note = ", ".join(part for part in note_parts if part)
    if CANCEL_STRONG_RE.search(note):
        cancelled = True
    return Lesson(
        period=period,
        start=start,
        end=_portal_time(found.get("end") or _portal_deep(node, "end")),
        subject=subject,
        room=room,
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
    week_start: date | None = None,
) -> None:
    """Sucht rekursiv nach Stunden; Datum kann am Eintrag oder am Zweig haengen."""
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node:
            _walk_portal(
                item,
                context_day,
                default_year,
                lessons,
                seen,
                context_period,
                depth + 1,
                week_start,
            )
        return
    if not isinstance(node, dict):
        return
    own_day = _portal_date(_portal_lookup(node).get("date"), default_year) or context_day
    lesson = _portal_lesson(node, own_day, default_year, context_period, week_start)
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
            week_start,
        )


def _portal_week_anchor(payload, default_year: int | None = None) -> date | None:
    """Erster Tag der gelieferten Woche (``meta.filter.startDate`` o. ae.).

    Die Schnittstelle liefert zu Stunden ohne Datum nur den Wochentag
    (``dow``/``weekday``). Der Filter der Anfrage - und damit der Wochenanfang -
    steht in der Antwort unter ``meta.filter``.
    """
    anchors: list[date] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and not isinstance(value, (dict, list)):
                    if _portal_key(key) in (
                        "startdate",
                        "weekstart",
                        "wochenstart",
                        "monday",
                    ):
                        found = _portal_date(value, default_year)
                        if found is not None:
                            anchors.append(found - timedelta(days=found.weekday()))
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload)
    return anchors[0] if anchors else None


def _portal_payload_root(payload):
    """Wurzel der Stunden - bevorzugt ``data.timetable``.

    Die Antwort enthaelt neben ``data.timetable`` auch ``plain-timetable``
    (dieselben Stunden ohne Vertretungen) und ``plain-changes`` (nur die
    Aenderungen). Gelesen wird genau eine Stundenliste, sonst stehen alle
    Stunden doppelt im Plan.
    """
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("timetable", "plain-timetable"):
                value = data.get(key)
                if isinstance(value, (list, dict)) and value:
                    return value
        plain = payload.get("plain-timetable")
        if isinstance(plain, (list, dict)) and plain:
            return plain
    return payload


def parse_portal_json(
    text: str, source: str = "", default_year: int | None = None
) -> PlanDataset:
    """Parst die JSON-Schnittstelle der IServ-Stundenplanansicht.

    Die Struktur unterscheidet sich je nach IServ-Version (Liste von Stunden,
    nach Tagen gruppiert, Feldnamen deutsch oder englisch). Erkannt wird eine
    Stunde an ``Datum`` + ``Fach`` + (``Stunde`` oder ``Beginn``) - alles andere
    (Konfiguration, Zeitraster, Rechte) wird ignoriert.

    Stunden ohne Datum tragen oft nur den Wochentag (``dow``/``weekday``); der
    Wochenanfang kommt aus ``meta.filter.startDate`` der Antwort. ``data``
    enthaelt neben ``timetable`` auch ``plain-timetable`` - gelesen wird nur
    eine der beiden Listen, sonst stehen alle Stunden doppelt im Plan.
    """
    dataset = PlanDataset(source=source or "iserv-json")
    lessons: list[Lesson] = []
    try:
        payload = json.loads(text.lstrip("\ufeff"))
    except ValueError as err:
        dataset.warnings.append(f"IServ-JSON nicht lesbar: {err}")
        return dataset
    week_start = _portal_week_anchor(payload, default_year)
    _walk_portal(
        _portal_payload_root(payload),
        None,
        default_year,
        lessons,
        set(),
        week_start=week_start,
    )
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



