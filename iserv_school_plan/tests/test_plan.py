"""Tests fuer die Schultag-/Weckerlogik (reine Funktionen, ohne HA-Abhaengigkeit)."""

from __future__ import annotations

from datetime import date, datetime, time

from app.plan import (
    STATUS_ERROR,
    STATUS_FERIEN,
    STATUS_OK,
    TZ,
    DayPlan,
    Holiday,
    Lesson,
    PlanDataset,
    apply_period_times,
    hours_until,
    parse_period_times,
    plan_tomorrow,
    select_plan,
)

MONDAY = date(2026, 10, 19)
WEDNESDAY = date(2026, 10, 21)
THURSDAY = date(2026, 10, 22)
FRIDAY = date(2026, 10, 23)
SATURDAY = date(2026, 10, 24)
SUNDAY = date(2026, 10, 25)
NEXT_MONDAY = date(2026, 10, 26)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def dataset_with(*days: DayPlan) -> PlanDataset:
    dataset = PlanDataset()
    for day in days:
        dataset.days[day.date] = day
    return dataset


def day_plan(day: date, *lessons: Lesson, is_school_day: bool | None = True) -> DayPlan:
    return DayPlan(date=day, lessons=list(lessons), is_school_day=is_school_day)


def lesson(period: int, start: time | None = None, cancelled: bool = False) -> Lesson:
    return Lesson(period=period, start=start, subject=f"Fach {period}", cancelled=cancelled)


def holiday(start: date, end: date, name: str = "Ferien", kind: str = "ferien") -> Holiday:
    return Holiday(start=start, end=end, name=name, kind=kind)


def test_before_noon_targets_today() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45)), lesson(2, time(8, 35))),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 9), dataset)
    assert result.status == STATUS_OK
    assert result.target_date == WEDNESDAY
    assert result.first_period == 1
    assert result.start == at(WEDNESDAY, 7, 45)


def test_after_noon_targets_next_day() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45))),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 13), dataset)
    assert result.status == STATUS_OK
    assert result.target_date == THURSDAY
    assert result.start == at(THURSDAY, 7, 45)


def test_exactly_noon_counts_as_after_noon() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45))),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 12), dataset)
    assert result.target_date == THURSDAY


def test_friday_afternoon_targets_monday_with_start() -> None:
    dataset = dataset_with(
        day_plan(FRIDAY, lesson(1, time(7, 45))),
        day_plan(NEXT_MONDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(FRIDAY, 13), dataset)
    # Keine Sonderregel mehr: Freitag ist ein normaler Schultag.
    assert result.status == STATUS_OK
    assert result.target_date == NEXT_MONDAY
    assert result.start == at(NEXT_MONDAY, 7, 45)


def test_friday_morning_targets_friday() -> None:
    dataset = dataset_with(
        day_plan(FRIDAY, lesson(2, time(8, 35))),
        day_plan(NEXT_MONDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(FRIDAY, 7), dataset)
    assert result.target_date == FRIDAY
    assert result.start == at(FRIDAY, 8, 35)


def test_saturday_targets_monday() -> None:
    dataset = dataset_with(day_plan(NEXT_MONDAY, lesson(1, time(7, 45))))
    result = select_plan(at(SATURDAY, 10), dataset)
    assert result.target_date == NEXT_MONDAY
    assert result.start == at(NEXT_MONDAY, 7, 45)


def test_sunday_targets_monday() -> None:
    dataset = dataset_with(day_plan(NEXT_MONDAY, lesson(1, time(7, 45))))
    result = select_plan(at(SUNDAY, 18), dataset)
    assert result.target_date == NEXT_MONDAY
    assert result.start == at(NEXT_MONDAY, 7, 45)


def test_holiday_is_skipped() -> None:
    dataset = dataset_with(day_plan(THURSDAY, lesson(1, time(7, 45))))
    result = select_plan(at(WEDNESDAY, 8), dataset, holidays=[holiday(WEDNESDAY, WEDNESDAY)])
    assert result.target_date == THURSDAY
    assert any(skip.kind == "ferien" and skip.date == WEDNESDAY for skip in result.skipped)


def test_holiday_does_not_override_plan_data() -> None:
    """Liefert IServ Stunden, gelten sie - auch wenn der Kalender Ferien meldet."""
    dataset = dataset_with(day_plan(WEDNESDAY, lesson(2, time(8, 35))))
    result = select_plan(at(WEDNESDAY, 7), dataset, holidays=[holiday(MONDAY, FRIDAY)])
    assert result.target_date == WEDNESDAY
    assert result.start == at(WEDNESDAY, 8, 35)


def test_empty_day_is_skipped() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 8), dataset)
    assert result.target_date == THURSDAY
    assert any(skip.kind == "kein_unterricht" for skip in result.skipped)


def test_school_day_false_is_treated_as_holiday() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, is_school_day=False),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 8), dataset)
    assert result.target_date == THURSDAY
    assert any(skip.kind == "ferien" for skip in result.skipped)


def test_unknown_dataset_has_no_start() -> None:
    result = select_plan(at(WEDNESDAY, 8), None)
    assert result.status == STATUS_ERROR
    assert result.start is None
    assert result.first_period is None


def test_missing_start_time_is_error() -> None:
    dataset = dataset_with(day_plan(WEDNESDAY, lesson(1)))
    result = select_plan(at(WEDNESDAY, 8), dataset)
    assert result.status == STATUS_ERROR
    assert result.start is None
    assert result.planned_first_period == 1


def test_cancelled_first_lesson_uses_next_held_lesson() -> None:
    dataset = dataset_with(
        day_plan(
            WEDNESDAY,
            lesson(1, time(7, 45), cancelled=True),
            lesson(2, time(8, 35)),
        )
    )
    result = select_plan(at(WEDNESDAY, 7), dataset)
    assert result.status == STATUS_OK
    assert result.first_period == 2
    assert result.start == at(WEDNESDAY, 8, 35)
    assert result.cancelled_periods == [1]


def test_all_lessons_cancelled_moves_to_next_day() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45), cancelled=True)),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 7), dataset)
    assert result.target_date == THURSDAY
    assert result.start == at(THURSDAY, 7, 45)


def test_period_times_fill_missing_start() -> None:
    dataset = dataset_with(day_plan(WEDNESDAY, lesson(1)))
    apply_period_times(dataset, parse_period_times("1=07:45-08:30;2=08:35-09:20"))
    result = select_plan(at(WEDNESDAY, 7), dataset)
    assert result.status == STATUS_OK
    assert result.start == at(WEDNESDAY, 7, 45)


def test_all_holidays_produce_ferien_status() -> None:
    result = select_plan(
        at(WEDNESDAY, 8),
        dataset_with(),
        holidays=[holiday(WEDNESDAY, THURSDAY, "Herbstferien")],
        lookahead_days=2,
    )
    assert result.status == STATUS_FERIEN
    assert result.start is None


def test_missing_data_reports_error() -> None:
    result = select_plan(at(FRIDAY, 9), dataset_with(), lookahead_days=1)
    assert result.status == STATUS_ERROR
    assert result.target_date == FRIDAY
    assert result.start is None


def test_parse_period_times() -> None:
    times = parse_period_times("1=07:45-08:30;2=08:35")
    assert times[1] == (time(7, 45), time(8, 30))
    assert times[2] == (time(8, 35), None)


def test_dataset_json_roundtrip() -> None:
    dataset = dataset_with(day_plan(WEDNESDAY, lesson(1, time(7, 45))))
    dataset.fetched_at = at(WEDNESDAY, 6)
    restored = PlanDataset.from_dict(dataset.to_dict())
    assert restored.days[WEDNESDAY].lessons[0].start == time(7, 45)
    assert restored.fetched_at == dataset.fetched_at


def test_plan_tomorrow_requires_lesson() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45))),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    tomorrow = plan_tomorrow(at(WEDNESDAY, 13), dataset)
    assert tomorrow.school is True
    assert tomorrow.date == THURSDAY
    assert tomorrow.start == at(THURSDAY, 7, 45)
    assert tomorrow.period == 1
    assert tomorrow.reason == "Unterricht"


def test_plan_tomorrow_weekend() -> None:
    tomorrow = plan_tomorrow(at(FRIDAY, 9), dataset_with())
    assert tomorrow.school is False
    assert tomorrow.date == SATURDAY
    assert tomorrow.reason == "Wochenende"


def test_plan_tomorrow_holiday() -> None:
    tomorrow = plan_tomorrow(
        at(WEDNESDAY, 13),
        dataset_with(),
        holidays=[holiday(THURSDAY, THURSDAY, "Tag der Deutschen Einheit", "feiertag")],
    )
    assert tomorrow.school is False
    assert tomorrow.reason == "Tag der Deutschen Einheit"


def test_plan_tomorrow_without_data_is_off() -> None:
    tomorrow = plan_tomorrow(at(WEDNESDAY, 13), dataset_with())
    assert tomorrow.school is False
    assert tomorrow.reason == "keine Daten"


def test_plan_tomorrow_off_when_all_lessons_cancelled() -> None:
    dataset = dataset_with(
        day_plan(THURSDAY, lesson(1, time(7, 45), cancelled=True)),
        day_plan(FRIDAY, lesson(1, time(7, 45))),
    )
    tomorrow = plan_tomorrow(at(WEDNESDAY, 13), dataset)
    assert tomorrow.school is False
    assert tomorrow.date == THURSDAY
    assert tomorrow.reason == "alle Stunden entfallen"


def test_hours_until() -> None:
    assert hours_until(at(MONDAY, 22), at(MONDAY, 7, 45)) == 0
    assert hours_until(at(SUNDAY, 22), at(NEXT_MONDAY, 7, 45)) == 9
    assert hours_until(at(WEDNESDAY, 7, 30), at(WEDNESDAY, 7, 45)) == 0
    assert hours_until(at(WEDNESDAY, 8), at(WEDNESDAY, 7, 45)) == 0
    assert hours_until(at(WEDNESDAY, 8), None) is None

