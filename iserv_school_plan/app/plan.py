"""Stundenplan-Datenmodell und Auswahl des relevanten Schultags.

Die Zeitzone ist fest auf Europe/Berlin gesetzt (Sommer-/Winterzeit wird von
``zoneinfo`` korrekt behandelt). Das Modul ist frei von HA-/Netzwerk-Abhaengigkeiten
und damit vollstaendig unit-testbar.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
NOON = time(12, 0)
# Freitag und Samstag: laut Wochenendregel wird kein Wecker gestellt.
WEEKEND = (4, 5)
SUNDAY = 6

STATUS_OK = "ok"
STATUS_FERIEN = "ferien"
STATUS_KEIN_UNTERRICHT = "kein_unterricht"
STATUS_WOCHENENDE = "wochenende"
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
class FreeDay:
    """Konfigurierter schulfreier Tag (Ferien, Feiertag, beweglicher Ferientag)."""

    start: date
    end: date
    name: str = ""

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


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
    alarm: bool = False
    reason: str = ""
    day: DayPlan | None = None
    skipped: list[Skip] = field(default_factory=list)
    planned_first_period: int | None = None

    @property
    def first_period(self) -> int | None:
        return self.first_lesson.period if self.first_lesson else None

    @property
    def cancelled_periods(self) -> list[int]:
        return self.day.cancelled_periods if self.day else []


def parse_date(value: str) -> date:
    """Akzeptiert ISO (2026-10-22) und deutsches Format (22.10.2026)."""
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Ungueltiges Datum: {value!r}")


def parse_free_days(spec: str | None) -> list[FreeDay]:
    """Parst ``2026-07-06..2026-08-14:Ferien;2026-10-03:Feiertag``."""
    if not spec:
        return []
    entries: list[FreeDay] = []
    for raw in spec.replace("\n", ";").split(";"):
        chunk = raw.strip()
        if not chunk:
            continue
        name = ""
        if ":" in chunk:
            chunk, name = chunk.split(":", 1)
            name = name.strip()
        if ".." in chunk:
            start_raw, end_raw = chunk.split("..", 1)
            start, end = parse_date(start_raw), parse_date(end_raw)
        else:
            start = end = parse_date(chunk)
        if end < start:
            start, end = end, start
        entries.append(FreeDay(start=start, end=end, name=name))
    return entries


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


def match_free_day(day: date, free_days: list[FreeDay]) -> FreeDay | None:
    for entry in free_days:
        if entry.contains(day):
            return entry
    return None


def _planned_first(day: DayPlan) -> int | None:
    return day.first_planned_period()


def _weekend_result(
    skipped: list[Skip], reason: str, target_date: date | None = None
) -> PlanResult:
    return PlanResult(
        status=STATUS_WOCHENENDE,
        target_date=target_date,
        alarm=False,
        reason=reason,
        skipped=skipped,
    )


def select_plan(
    now: datetime,
    dataset: PlanDataset | None,
    free_days: list[FreeDay] | None = None,
    lookahead_days: int = 21,
    skip_empty_days: bool = True,
) -> PlanResult:
    """Ermittelt den relevanten Schultag und die erste stattfindende Stunde.

    Regeln:
    * Freitag/Samstag: kein Wecker (Wochenendregel), nur Info-Datum.
    * Sonntag: Montag (bzw. naechsten Schultag) vorbereiten.
    * Mo-Do vor 12:00: heutiger Tag; ab 12:00: Folgetag.
    * Schulfreie Tage (Ferien/Feiertage/Wochenende/leere Tage) werden uebersprungen.
    * Unbekannte Daten fuehren nie zu einem Wecker.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)

    today = now.date()
    weekday = today.weekday()
    info_only = weekday in WEEKEND
    free_days = free_days or []

    if weekday in WEEKEND or weekday == SUNDAY:
        start = today + timedelta(days=1)
    elif now.time() < NOON:
        start = today
    else:
        start = today + timedelta(days=1)

    skipped: list[Skip] = []
    last_checked = start

    for offset in range(lookahead_days + 1):
        day = start + timedelta(days=offset)
        last_checked = day

        if day.weekday() >= 5:
            skipped.append(Skip(day, "Wochenende", "weekend"))
            continue

        free = match_free_day(day, free_days)
        if free:
            skipped.append(Skip(day, free.name or "schulfrei", "ferien"))
            continue

        day_plan = dataset.lookup(day) if dataset else None
        if day_plan is None:
            if info_only:
                return _weekend_result(skipped, "Wochenendregel: kein Wecker", day)
            # Keine belastbaren Daten: keinen Wecker stellen und nicht raten.
            skipped.append(Skip(day, "keine Daten", "unknown"))
            break

        if day_plan.is_school_day is False:
            skipped.append(Skip(day, day_plan.holiday_name or "schulfrei", "ferien"))
            continue

        held = day_plan.held_lessons
        if not held:
            if day_plan.lessons:
                skipped.append(Skip(day, "alle Stunden entfallen", "kein_unterricht"))
                continue
            if skip_empty_days:
                skipped.append(Skip(day, "kein Unterricht", "kein_unterricht"))
                continue
            if info_only:
                return PlanResult(
                    status=STATUS_WOCHENENDE,
                    target_date=day,
                    day=day_plan,
                    alarm=False,
                    reason="Wochenendregel: kein Wecker",
                    skipped=skipped,
                    planned_first_period=_planned_first(day_plan),
                )
            return PlanResult(
                status=STATUS_KEIN_UNTERRICHT,
                target_date=day,
                day=day_plan,
                alarm=False,
                reason="Keine stattfindende Stunde",
                skipped=skipped,
                planned_first_period=_planned_first(day_plan),
            )

        timed = sorted(
            (lesson for lesson in held if lesson.start is not None),
            key=lambda lesson: (lesson.start, lesson.period or 99),
        )
        if not timed:
            if info_only:
                return _weekend_result(skipped, "Wochenendregel: kein Wecker", day)
            return PlanResult(
                status=STATUS_ERROR,
                target_date=day,
                day=day_plan,
                alarm=False,
                reason="Startzeit der ersten Stunde unbekannt",
                skipped=skipped,
                planned_first_period=_planned_first(day_plan),
            )

        first = timed[0]
        start_dt = datetime.combine(day, first.start, tzinfo=TZ)  # type: ignore[arg-type]

        return PlanResult(
            status=STATUS_WOCHENENDE if info_only else STATUS_OK,
            target_date=day,
            first_lesson=first,
            start=None if info_only else start_dt,
            alarm=not info_only,
            reason="Wochenendregel: kein Wecker" if info_only else "Erste stattfindende Stunde",
            day=day_plan,
            skipped=skipped,
            planned_first_period=_planned_first(day_plan),
        )

    if info_only:
        return _weekend_result(skipped, "Wochenendregel: kein Wecker", last_checked)

    kinds = {entry.kind for entry in skipped}
    if "unknown" in kinds:
        unknown = next(entry for entry in skipped if entry.kind == "unknown")
        if "ferien" in kinds:
            return PlanResult(
                status=STATUS_FERIEN,
                target_date=unknown.date,
                alarm=False,
                reason=f"Schulfrei bis {unknown.date.isoformat()}, danach keine Daten",
                skipped=skipped,
            )
        return PlanResult(
            status=STATUS_ERROR,
            target_date=unknown.date,
            alarm=False,
            reason=f"Keine Daten fuer {unknown.date.isoformat()}",
            skipped=skipped,
        )
    if "ferien" in kinds:
        return PlanResult(
            status=STATUS_FERIEN,
            target_date=last_checked,
            alarm=False,
            reason="Nur schulfreie Tage im Vorschauzeitraum",
            skipped=skipped,
        )
    return PlanResult(
        status=STATUS_KEIN_UNTERRICHT,
        target_date=last_checked,
        alarm=False,
        reason="Kein Unterricht im Vorschauzeitraum",
        skipped=skipped,
    )



