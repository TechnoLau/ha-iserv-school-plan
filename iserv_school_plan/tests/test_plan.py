"""Tests fuer die Schultag-/Weckerlogik (reine Funktionen, keine HA-Abhaengigkeit)."""

from __future__ import annotations

from datetime import date, datetime, time

from app.plan import (
    STATUS_ERROR,
    STATUS_FERIEN,
    STATUS_OK,
    STATUS_WOCHENENDE,
    TZ,
    DayPlan,
    FreeDay,
    Lesson,
    PlanDataset,
    apply_period_times,
    parse_free_days,
    parse_period_times,
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


def test_before_noon_targets_today() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45)), lesson(2, time(8, 35))),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 9), dataset)
    assert result.status == STATUS_OK
    assert result.alarm is True
    assert result.target_date == WEDNESDAY
    assert result.first_period == 1
    assert result.start == at(WEDNESDAY, 7, 45)


def test_after_noon_targets_next_day() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45))),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 13), dataset)
    assert result.target_date == THURSDAY
    assert result.start == at(THURSDAY, 7, 45)
    assert result.alarm is True


def test_exactly_noon_counts_as_after_noon() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45))),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 12), dataset)
    assert result.target_date == THURSDAY


def test_friday_never_sets_alarm() -> None:
    dataset = dataset_with(
        day_plan(FRIDAY, lesson(1, time(7, 45))),
        day_plan(NEXT_MONDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(FRIDAY, 9), dataset)
    assert result.alarm is False
    assert result.status == STATUS_WOCHENENDE
    assert result.start is None
    # Info: naechster Schultag ist Montag
    assert result.target_date == NEXT_MONDAY
    assert result.first_period == 1


def test_saturday_reports_monday_without_alarm() -> None:
    dataset = dataset_with(day_plan(NEXT_MONDAY, lesson(1, time(7, 45))))
    result = select_plan(at(SATURDAY, 10), dataset)
    assert result.alarm is False
    assert result.target_date == NEXT_MONDAY
    assert result.start is None


def test_sunday_targets_monday_with_alarm() -> None:
    dataset = dataset_with(day_plan(NEXT_MONDAY, lesson(1, time(7, 45))))
    result = select_plan(at(SUNDAY, 18), dataset)
    assert result.alarm is True
    assert result.target_date == NEXT_MONDAY
    assert result.start == at(NEXT_MONDAY, 7, 45)


def test_free_day_is_skipped() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45))),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    free_days = [FreeDay(start=WEDNESDAY, end=WEDNESDAY, name="Ferien")]
    result = select_plan(at(WEDNESDAY, 8), dataset, free_days=free_days)
    assert result.target_date == THURSDAY
    assert result.alarm is True
    assert any(skip.kind == "ferien" and skip.date == WEDNESDAY for skip in result.skipped)


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


def test_unknown_dataset_never_sets_alarm() -> None:
    result = select_plan(at(WEDNESDAY, 8), None)
    assert result.alarm is False
    assert result.status == STATUS_ERROR
    assert result.start is None


def test_missing_start_time_never_sets_alarm() -> None:
    dataset = dataset_with(day_plan(WEDNESDAY, lesson(1)))
    result = select_plan(at(WEDNESDAY, 8), dataset)
    assert result.alarm is False
    assert result.status == STATUS_ERROR
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
    assert result.first_period == 2
    assert result.start == at(WEDNESDAY, 8, 35)
    assert result.cancelled_periods == [1]


def test_all_lessons_cancelled_means_no_alarm() -> None:
    dataset = dataset_with(
        day_plan(WEDNESDAY, lesson(1, time(7, 45), cancelled=True)),
        day_plan(THURSDAY, lesson(1, time(7, 45))),
    )
    result = select_plan(at(WEDNESDAY, 7), dataset)
    assert result.target_date == THURSDAY
    assert result.alarm is True


def test_period_times_fill_missing_start() -> None:
    dataset = dataset_with(day_plan(WEDNESDAY, lesson(1)))
    apply_period_times(dataset, parse_period_times("1=07:45-08:30;2=08:35-09:20"))
    result = select_plan(at(WEDNESDAY, 7), dataset)
    assert result.status == STATUS_OK
    assert result.start == at(WEDNESDAY, 7, 45)


def test_all_free_days_produce_ferien_status() -> None:
    dataset = dataset_with(day_plan(WEDNESDAY, is_school_day=False))
    free_days = [FreeDay(start=WEDNESDAY, end=THURSDAY, name="Herbstferien")]
    result = select_plan(at(WEDNESDAY, 8), dataset, free_days=free_days)
    assert result.alarm is False
    assert result.status == STATUS_FERIEN


def test_weekend_without_plan_data_reports_last_checked_day() -> None:
    result = select_plan(at(FRIDAY, 9), dataset_with(), lookahead_days=1)
    assert result.status == STATUS_WOCHENENDE
    assert result.alarm is False
    assert result.start is None
    assert result.target_date == SUNDAY


def test_parse_free_days_and_period_times() -> None:
    days = parse_free_days("2026-10-12..2026-10-24:Herbstferien;2026-10-03:Tag der Einheit")
    assert len(days) == 2
    assert days[0].start == date(2026, 10, 12)
    assert days[0].end == date(2026, 10, 24)
    assert days[0].name == "Herbstferien"
    assert days[1].contains(date(2026, 10, 3))

    times = parse_period_times("1=07:45-08:30;2=08:35")
    assert times[1] == (time(7, 45), time(8, 30))
    assert times[2] == (time(8, 35), None)


def test_dataset_json_roundtrip() -> None:
    dataset = dataset_with(day_plan(WEDNESDAY, lesson(1, time(7, 45))))
    dataset.fetched_at = at(WEDNESDAY, 6)
    restored = PlanDataset.from_dict(dataset.to_dict())
    assert restored.days[WEDNESDAY].lessons[0].start == time(7, 45)
    assert restored.fetched_at == dataset.fetched_at


