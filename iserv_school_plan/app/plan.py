"""Stundenplan-Datenmodell und Auswahl des relevanten Schultags.

Die Zeitzone ist fest auf Europe/Berlin gesetzt (Sommer-/Winterzeit wird von
``zoneinfo`` korrekt behandelt). Das Modul ist frei von HA-/Netzwerk-Abhaengigkeiten
und damit vollstaendig unit-testbar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
NOON = time(12, 0)
# Der Tag wechselt um Mitternacht: "Schul start" und "Schul ende" zeigen immer
# den laufenden Tag. Ist heute kein Schultag (Wochenende, Ferien, keine Stunden),
# gilt der naechste - genau wie beim Wecker.
# Ohne Plandaten (keine Verbindung, kein Cache) gelten die ueblichen Zeiten,
# damit Automationen nicht auf "unknown" laufen.
FALLBACK_START = time(8, 10)
FALLBACK_END = time(13, 20)
# Trennzeichen zwischen mehreren Faechern in einer Zelle ("Deutsch / Franz").
SUBJECT_SPLIT_RE = re.compile(r"[/,;+&|]")

STATUS_OK = "ok"
STATUS_FERIEN = "ferien"
STATUS_KEIN_UNTERRICHT = "kein_unterricht"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class Lesson:
    """Eine einzelne Unterrichtsstunde an einem Tag."""

    period: int | None = None
    start: time | None = None
    end: time | None = None
    subject: str = ""
    room: str = ""
    cancelled: bool = False
    note: str = ""
    date: date | None = None

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "subject": self.subject,
            "room": self.room,
            "cancelled": self.cancelled,
            "note": self.note,
            "date": self.date.isoformat() if self.date else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lesson":
        def _time(value: str | None) -> time | None:
            return time.fromisoformat(value) if value else None

        return cls(
            period=data.get("period"),
            start=_time(data.get("start")),
            end=_time(data.get("end")),
            subject=data.get("subject") or "",
            room=data.get("room") or "",
            cancelled=bool(data.get("cancelled")),
            note=data.get("note") or "",
            date=date.fromisoformat(data["date"]) if data.get("date") else None,
        )


@dataclass
class DayPlan:
    """Alle bekannten Stunden eines Kalendertags."""

    date: date
    lessons: list[Lesson] = field(default_factory=list)
    # None = unbekannt, True/False = von der Quelle geliefert
    is_school_day: bool | None = None
    holiday_name: str | None = None

    @property
    def held_lessons(self) -> list[Lesson]:
        """Stunden, die tatsaechlich stattfinden (nicht als Ausfall markiert)."""
        return [lesson for lesson in self.lessons if not lesson.cancelled]

    @property
    def cancelled_periods(self) -> list[int]:
        return sorted(
            {lesson.period for lesson in self.lessons if lesson.cancelled and lesson.period}
        )

    def first_planned_period(self) -> int | None:
        periods = [lesson.period for lesson in self.lessons if lesson.period]
        return min(periods) if periods else None

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "is_school_day": self.is_school_day,
            "holiday_name": self.holiday_name,
            "lessons": [lesson.to_dict() for lesson in self.lessons],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DayPlan":
        day = date.fromisoformat(data["date"])
        lessons = []
        for item in data.get("lessons", []):
            lesson = Lesson.from_dict(item)
            if lesson.date is None:
                lesson = Lesson(**{**lesson.__dict__, "date": day})
            lessons.append(lesson)
        return cls(
            date=day,
            lessons=lessons,
            is_school_day=data.get("is_school_day"),
            holiday_name=data.get("holiday_name"),
        )


@dataclass
class PlanDataset:
    """Ergebnis eines Abrufs (ggf. aus dem Cache geladen)."""

    days: dict[date, DayPlan] = field(default_factory=dict)
    fetched_at: datetime | None = None
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def add_lesson(self, lesson: Lesson, is_school_day: bool | None = True) -> None:
        day = lesson.date
        if day is None:
            raise ValueError("Lesson ohne Datum kann nicht einsortiert werden")
        bucket = self.days.setdefault(day, DayPlan(date=day))
        if is_school_day is not None:
            bucket.is_school_day = is_school_day
        bucket.lessons.append(lesson)

    def lookup(self, day: date) -> DayPlan | None:
        """Tag aus den Daten; innerhalb der Abdeckung auch ohne Eintraege."""
        if day in self.days:
            return self.days[day]
        if self.coverage_start and self.coverage_end:
            if self.coverage_start <= day <= self.coverage_end:
                return DayPlan(date=day)
        return None

    @property
    def coverage_start(self) -> date | None:
        return min(self.days) if self.days else None

    @property
    def coverage_end(self) -> date | None:
        return max(self.days) if self.days else None

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "source": self.source,
            "warnings": self.warnings,
            "days": [self.days[key].to_dict() for key in sorted(self.days)],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanDataset":
        dataset = cls(
            source=data.get("source") or "",
            warnings=list(data.get("warnings") or []),
        )
        if data.get("fetched_at"):
            dataset.fetched_at = datetime.fromisoformat(data["fetched_at"])
        for item in data.get("days", []):
            day = DayPlan.from_dict(item)
            dataset.days[day.date] = day
        return dataset


@dataclass(frozen=True)
class Holiday:
    """Ferien- oder Feiertagszeitraum (aus dem Holiday-Kalender des Add-ons)."""

    start: date
    end: date
    name: str = ""
    kind: str = "ferien"  # ferien | feiertag

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "name": self.name,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Holiday":
        return cls(
            start=date.fromisoformat(data["start"]),
            end=date.fromisoformat(data["end"]),
            name=data.get("name") or "",
            kind=data.get("kind") or "ferien",
        )


@dataclass
class Skip:
    date: date
    reason: str
    kind: str  # weekend | ferien | kein_unterricht


@dataclass
class PlanResult:
    status: str
    target_date: date | None = None
    first_lesson: Lesson | None = None
    start: datetime | None = None
    reason: str = ""
    day: DayPlan | None = None
    skipped: list[Skip] = field(default_factory=list)
    planned_first_period: int | None = None
    # Stunden des Tages, die nach dem Fachfilter uebrig bleiben.
    held: list[Lesson] = field(default_factory=list)

    @property
    def first_period(self) -> int | None:
        return self.first_lesson.period if self.first_lesson else None

    @property
    def cancelled_periods(self) -> list[int]:
        return self.day.cancelled_periods if self.day else []


@dataclass
class TomorrowPlan:
    """Antwort auf die Frage: ist morgen (Kalendertag) Schule?"""

    date: date
    school: bool = False
    first_lesson: Lesson | None = None
    start: datetime | None = None
    reason: str = ""

    @property
    def period(self) -> int | None:
        return self.first_lesson.period if self.first_lesson else None


@dataclass
class SchoolTimes:
    """Beginn und Ende des Schultags, den die Sensoren gerade zeigen.

    Beide Werte gehoeren zum selben Tag: dem laufenden Schultag. Um Mitternacht
    wechselt der Tag; ist heute kein Schultag, gilt der naechste.
    """

    start: datetime | None = None
    start_date: date | None = None
    start_period: int | None = None
    end: datetime | None = None
    end_date: date | None = None
    end_period: int | None = None
    end_reason: str = ""
    # "plan" = aus IServ/Stundenzeiten, "fallback" = Standardzeiten ohne Daten
    start_source: str = "plan"
    end_source: str = "plan"


def parse_period_times(spec: str | None) -> dict[int, tuple[time, time | None]]:
    """Parst ``1=07:45-08:30;2=08:35-09:20`` (Endzeit optional)."""
    mapping: dict[int, tuple[time, time | None]] = {}
    if not spec:
        return mapping
    for raw in spec.replace("\n", ";").split(";"):
        chunk = raw.strip()
        if not chunk or "=" not in chunk:
            continue
        period_raw, times = chunk.split("=", 1)
        try:
            period = int(period_raw.strip().rstrip("."))
        except ValueError:
            continue
        start_raw, _, end_raw = times.partition("-")
        try:
            start = time.fromisoformat(start_raw.strip())
        except ValueError:
            continue
        end: time | None = None
        if end_raw.strip():
            try:
                end = time.fromisoformat(end_raw.strip())
            except ValueError:
                end = None
        mapping[period] = (start, end)
    return mapping


def apply_period_times(
    dataset: PlanDataset, mapping: dict[int, tuple[time, time | None]]
) -> PlanDataset:
    """Fuellt fehlende Uhrzeiten anhand der konfigurierten Stundenzeiten."""
    if not mapping:
        return dataset
    for day in dataset.days.values():
        updated: list[Lesson] = []
        for lesson in day.lessons:
            if lesson.period is not None and lesson.period in mapping:
                start, end = mapping[lesson.period]
                lesson = replace(
                    lesson,
                    start=lesson.start if lesson.start is not None else start,
                    end=lesson.end if lesson.end is not None else end,
                )
            updated.append(lesson)
        day.lessons = updated
    return dataset


def find_holiday(day: date, holidays: list[Holiday] | None) -> Holiday | None:
    """Erster Ferien-/Feiertagseintrag, der den Tag abdeckt."""
    for entry in holidays or []:
        if entry.contains(day):
            return entry
    return None


def _planned_first(day: DayPlan) -> int | None:
    return day.first_planned_period()


def _first_timed(lessons: list[Lesson]) -> Lesson | None:
    orders = sorted(
        (lesson for lesson in lessons if lesson.start is not None),
        key=lambda lesson: (lesson.start, lesson.period or 99),
    )
    return orders[0] if orders else None


def _start_datetime(day: date, lesson: Lesson | None) -> datetime | None:
    if lesson is None or lesson.start is None:
        return None
    return datetime.combine(day, lesson.start, tzinfo=TZ)


def _local(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ)
    return now.astimezone(TZ)


def select_plan(
    now: datetime,
    dataset: PlanDataset | None,
    holidays: list[Holiday] | None = None,
    lookahead_days: int = 21,
    cutover: time | None = NOON,
    subjects: frozenset[str] | None = None,
    enabled: frozenset[str] | None = None,
) -> PlanResult:
    """Ermittelt den naechsten Schultag mit stattfindender Stunde.

    Regeln:
    * Vor ``cutover`` (Standard 12:00): heutiger Tag; danach und am Wochenende:
      Folgetag. ``cutover=None`` heisst: der heutige Tag zaehlt immer - das
      nutzen "Schul start" und "Schul ende", die um Mitternacht umspringen.
    * Wochenende, Ferien/Feiertage und Tage ohne stattfindende Stunde werden
      uebersprungen; der Ferien-/Feiertagskalender greift nur fuer Tage, fuer
      die IServ gar keine Daten liefert (Daten haben immer Vorrang).
    * Fehlende Daten fuehren nie zu einer geratenen Startzeit.
    * ``subjects`` beschraenkt die Zaehlung auf die eigenen Faecher: Eine Stunde
      mit mehreren Faechern zaehlt, wenn eines davon gewaehlt ist.
    """
    now = _local(now)
    today = now.date()
    if today.weekday() >= 5 or (cutover is not None and now.time() >= cutover):
        start = today + timedelta(days=1)
    else:
        start = today

    skipped: list[Skip] = []
    last_checked = start

    for offset in range(lookahead_days + 1):
        day = start + timedelta(days=offset)
        last_checked = day

        if day.weekday() >= 5:
            skipped.append(Skip(day, "Wochenende", "weekend"))
            continue

        day_plan = dataset.lookup(day) if dataset else None
        held = day_plan.held_lessons if day_plan else []
        if subjects or enabled is not None:
            held = [
                lesson
                for lesson in held
                if lesson_matches_subjects(lesson, subjects, enabled)
            ]
        holiday = find_holiday(day, holidays)

        if held:
            first = _first_timed(held)
            if first is None:
                return PlanResult(
                    status=STATUS_ERROR,
                    target_date=day,
                    day=day_plan,
                    reason="Startzeit der ersten Stunde unbekannt",
                    skipped=skipped,
                    planned_first_period=_planned_first(day_plan),
                    held=held,
                )
            return PlanResult(
                status=STATUS_OK,
                target_date=day,
                first_lesson=first,
                start=_start_datetime(day, first),
                reason="Erste stattfindende Stunde",
                day=day_plan,
                skipped=skipped,
                planned_first_period=_planned_first(day_plan),
                held=held,
            )

        if day_plan is not None and day_plan.is_school_day is False:
            skipped.append(
                Skip(
                    day,
                    day_plan.holiday_name or (holiday.name if holiday else "schulfrei"),
                    "ferien",
                )
            )
            continue
        if day_plan is not None and day_plan.lessons:
            skipped.append(Skip(day, "alle Stunden entfallen", "kein_unterricht"))
            continue
        if holiday is not None:
            # Kein Plan-Eintrag fuer diesen Tag: Ferien/Feiertag gilt.
            skipped.append(Skip(day, holiday.name or "schulfrei", "ferien"))
            continue
        if day_plan is None:
            skipped.append(Skip(day, "keine Daten", "unknown"))
            break
        skipped.append(Skip(day, "kein Unterricht", "kein_unterricht"))

    kinds = {entry.kind for entry in skipped}
    if "unknown" in kinds:
        unknown = next(entry for entry in skipped if entry.kind == "unknown")
        if "ferien" in kinds:
            return PlanResult(
                status=STATUS_FERIEN,
                target_date=unknown.date,
                reason=f"Schulfrei bis {unknown.date.isoformat()}, danach keine Daten",
                skipped=skipped,
            )
        return PlanResult(
            status=STATUS_ERROR,
            target_date=unknown.date,
            reason=f"Keine Daten fuer {unknown.date.isoformat()}",
            skipped=skipped,
        )
    if "ferien" in kinds:
        return PlanResult(
            status=STATUS_FERIEN,
            target_date=last_checked,
            reason="Nur schulfreie Tage im Vorschauzeitraum",
            skipped=skipped,
        )
    return PlanResult(
        status=STATUS_KEIN_UNTERRICHT,
        target_date=last_checked,
        reason="Kein Unterricht im Vorschauzeitraum",
        skipped=skipped,
    )


def plan_tomorrow(
    now: datetime,
    dataset: PlanDataset | None,
    holidays: list[Holiday] | None = None,
    subjects: frozenset[str] | None = None,
    enabled: frozenset[str] | None = None,
) -> TomorrowPlan:
    """Prueft den morgigen Kalendertag ("Wecker morgen" ja/nein)."""
    tomorrow = _local(now).date() + timedelta(days=1)
    if tomorrow.weekday() >= 5:
        return TomorrowPlan(tomorrow, reason="Wochenende")

    day_plan = dataset.lookup(tomorrow) if dataset else None
    held = day_plan.held_lessons if day_plan else []
    if subjects or enabled is not None:
        held = [
            lesson for lesson in held if lesson_matches_subjects(lesson, subjects, enabled)
        ]
    if held:
        first = _first_timed(held)
        if first is None:
            return TomorrowPlan(
                tomorrow,
                school=True,
                first_lesson=held[0],
                reason="Startzeit der ersten Stunde unbekannt",
            )
        return TomorrowPlan(
            tomorrow,
            school=True,
            first_lesson=first,
            start=_start_datetime(tomorrow, first),
            reason="Unterricht",
        )

    holiday = find_holiday(tomorrow, holidays)
    if day_plan is not None and day_plan.is_school_day is False:
        return TomorrowPlan(
            tomorrow, reason=day_plan.holiday_name or (holiday.name if holiday else "schulfrei")
        )
    if day_plan is not None and day_plan.lessons:
        return TomorrowPlan(tomorrow, reason="alle Stunden entfallen")
    if holiday is not None:
        return TomorrowPlan(tomorrow, reason=holiday.name or "schulfrei")
    if day_plan is None:
        return TomorrowPlan(tomorrow, reason="keine Daten")
    return TomorrowPlan(tomorrow, reason="kein Unterricht")


UMLAUTE = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"))


def normalize_subject(value: str | None) -> str:
    """Vergleichsform eines Fachnamens.

    Kleinschreibung, einfache Leerzeichen und aufgeloeste Umlaute - damit
    "Französisch", "franzoesisch" und "FRANZOESISCH" gleich behandelt werden.
    """
    text = re.sub(r"\s+", " ", (value or "").strip().lower())
    for umlaut, ersatz in UMLAUTE:
        text = text.replace(umlaut, ersatz)
    return text


def split_subject_names(value: str | None) -> list[str]:
    """Einzelfaecher einer Angabe in Originalschreibweise (fuer die Anzeige)."""
    return [part.strip() for part in SUBJECT_SPLIT_RE.split(value or "") if part.strip()]


def split_subjects(value: str | None) -> list[str]:
    """Einzelfaecher einer Angabe - "Deutsch / Franz" ergibt zwei Eintraege."""
    return [normalize_subject(part) for part in split_subject_names(value)]


def is_ambiguous_subject(lesson: Lesson) -> bool:
    """True, wenn in der Zelle mehrere Faecher stehen ("Deutsch / Franz")."""
    return len(split_subject_names(lesson.subject)) > 1


def text_has_subject(value: str | None, wanted: frozenset[str]) -> bool:
    """Steckt eines der gewaehlten Faecher in diesem Text?

    Geprueft werden die ganze Angabe, ihre Teile ("Deutsch / Franz") und -
    fuer Faelle wie "Vertretung Franz" - der Text selbst.
    """
    if not value:
        return False
    text = normalize_subject(value)
    if text in wanted:
        return True
    if any(part in wanted for part in split_subjects(value)):
        return True
    return any(name in text for name in wanted)


def lesson_matches_subjects(
    lesson: Lesson,
    wanted: frozenset[str] | None,
    enabled: frozenset[str] | None = None,
) -> bool:
    """True, wenn die Stunde zu den gewaehlten Faechern gehoert.

    * ``wanted`` (Option ``faecher``): beschraenkt auf diese Faecher. IServ
      schreibt das zweite Fach oft in eine eigene Zeile - die landet als Notiz
      in der Stunde, deshalb wird sie mitgeprueft.
    * ``enabled`` (Schalter): gilt nur fuer mehrdeutige Zellen ("Franz /
      Nautik") - dort muss eines der eingeschalteten Faecher vorkommen. Ist
      ``enabled`` ``None``, gibt es noch keine Auswahl und alles zaehlt.
    """
    if wanted and not (
        text_has_subject(lesson.subject, wanted) or text_has_subject(lesson.note, wanted)
    ):
        return False
    if enabled is not None and is_ambiguous_subject(lesson):
        return any(part in enabled for part in split_subjects(lesson.subject))
    return True


def ambiguous_subjects(dataset: PlanDataset | None) -> list[str]:
    """Faecher aus Zellen mit mehreren Faechern, in Originalschreibweise.

    Nur fuer diese Zellen ist unklar, welches Fach gilt - dafuer gibt es einen
    Schalter. Einfachere Zellen ("Mathe") zaehlen immer.
    """
    seen: dict[str, str] = {}
    for day in dataset.days.values() if dataset else []:
        for lesson in day.lessons:
            if not is_ambiguous_subject(lesson):
                continue
            for part in split_subject_names(lesson.subject):
                seen.setdefault(normalize_subject(part), part)
    return [seen[key] for key in sorted(seen)]


def subject_tokens(subjects: list[str]) -> set[str]:
    """Alle Vergleichsformen einer Fachliste (auch die Teile mehrerer Faecher)."""
    tokens: set[str] = set()
    for value in subjects:
        tokens.add(normalize_subject(value))
        tokens.update(split_subjects(value))
    tokens.discard("")
    return tokens


def dataset_subjects(dataset: PlanDataset | None) -> list[str]:
    """Alle Facherangaben im Plan, sortiert - Grundlage fuer die Auswahl."""
    found: set[str] = set()
    for day in dataset.days.values() if dataset else []:
        for lesson in day.lessons:
            text = (lesson.subject or "").strip()
            if text:
                found.add(text)
    return sorted(found, key=str.lower)


def _last_timed(lessons: list[Lesson]) -> Lesson | None:
    """Letzte Stunde mit bekannter Endzeit - Ausfaelle sind schon aussortiert."""
    timed = [lesson for lesson in lessons if lesson.end is not None]
    if not timed:
        return None
    return max(timed, key=lambda lesson: (lesson.end, lesson.period or 0))


def school_times(
    now: datetime,
    dataset: PlanDataset | None,
    holidays: list[Holiday] | None = None,
    lookahead_days: int = 21,
    offline: bool = False,
    subjects: frozenset[str] | None = None,
    enabled: frozenset[str] | None = None,
) -> SchoolTimes:
    """Beginn und Ende fuer die Sensoren "Schul start" und "Schul ende".

    Beide Werte gelten fuer den laufenden Tag; um Mitternacht springen sie auf
    den neuen Tag um. Ist heute kein Schultag, gilt der naechste. Als Ende
    zaehlt die letzte Stunde, die tatsaechlich stattfindet - entfallene Stunden
    am Tagesende verschieben das Ende nach vorne.

    ``offline=True`` (Status "verbindung fehlgeschlagen") liefert die
    Standardzeiten ``FALLBACK_START``/``FALLBACK_END``. Nur dann: Solange die
    Verbindung steht, bleibt der Wert leer, wenn fuer den Tag kein Plan
    vorliegt - geraten wird nicht.
    """
    now = _local(now)
    if offline:
        today = now.date()
        return SchoolTimes(
            start=datetime.combine(today, FALLBACK_START, tzinfo=TZ),
            start_date=today,
            end=datetime.combine(today, FALLBACK_END, tzinfo=TZ),
            end_date=today,
            end_reason="Verbindung fehlgeschlagen - Standardzeiten",
            start_source="fallback",
            end_source="fallback",
        )

    start_plan = select_plan(
        now,
        dataset,
        holidays,
        lookahead_days,
        cutover=None,
        subjects=subjects,
        enabled=enabled,
    )
    end_plan = select_plan(
        now,
        dataset,
        holidays,
        lookahead_days,
        cutover=None,
        subjects=subjects,
        enabled=enabled,
    )

    # Nur die eigenen Faecher zaehlen fuer das Ende (Fachfilter aus select_plan).
    last = _last_timed(end_plan.held) if end_plan.held else None
    end: datetime | None = None
    if last is not None and last.end is not None and end_plan.target_date is not None:
        end = datetime.combine(end_plan.target_date, last.end, tzinfo=TZ)

    return SchoolTimes(
        start=start_plan.start,
        start_date=start_plan.target_date,
        start_period=start_plan.first_period,
        end=end,
        end_date=end_plan.target_date,
        end_period=last.period if last else None,
        end_reason=end_plan.reason,
    )


def hours_until(now: datetime, start: datetime | None) -> int | None:
    """Ganze Stunden bis zum Beginn (0, wenn er schon begonnen hat)."""
    if start is None:
        return None
    seconds = int((start - _local(now)).total_seconds())
    return 0 if seconds < 0 else seconds // 3600
