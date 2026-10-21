"""Tests fuer die Add-on-Logik (Statusaufbau, Cache, Abrufintervalle)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, time

import pytest

import app.main as main_module
from app.config import AddonOptions
from app.holidays import HolidayCalendar
from app.iserv import FetchError
from app.main import Addon
from app.plan import TZ, DayPlan, Holiday, Lesson, PlanDataset


@pytest.fixture(autouse=True)
def no_supervisor(monkeypatch):
    """Ohne Supervisor-Umgebung keine HTTP-Abfrage im Test."""
    monkeypatch.setattr(main_module, "supervisor_mqtt_config", lambda: None)


def make_dataset(now: datetime) -> PlanDataset:
    monday = date(2026, 10, 19)
    tuesday = date(2026, 10, 20)
    dataset = PlanDataset(fetched_at=now, source="/plan")
    dataset.days[monday] = DayPlan(
        date=monday,
        lessons=[
            Lesson(
                period=1,
                start=time(7, 45),
                end=time(8, 30),
                subject="Mathematik",
                room="A1",
                date=monday,
            ),
            Lesson(period=2, start=time(8, 35), end=time(9, 20), subject="Deutsch", cancelled=True,
                   date=monday),
        ],
    )
    dataset.days[tuesday] = DayPlan(
        date=tuesday,
        lessons=[
            Lesson(period=3, start=time(9, 50), end=time(10, 35), subject="Physik", date=tuesday)
        ],
    )
    return dataset


def make_addon(**overrides) -> Addon:
    options = AddonOptions(
        iserv_url="https://schule.example.org",
        period_times="1=07:45-08:30;2=08:35-09:20;3=09:50-10:35",
        **overrides,
    )
    return Addon(options)


def herbstferien(now: datetime) -> HolidayCalendar:
    return HolidayCalendar(
        bundesland="Schleswig-Holstein",
        ranges=[Holiday(date(2026, 10, 12), date(2026, 10, 24), "Herbstferien")],
        fetched_at=now,
    )


def test_build_state_morning_uses_today() -> None:
    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)

    state = addon.build_state(now)

    assert state["connection"] == "verbunden"
    assert state["status"] == "ok"
    assert state["target_date"] == "2026-10-19"
    assert state["start"] == "07:45"
    assert state["start_iso"] == "2026-10-19T07:45:00+02:00"
    assert state["period"] == 1
    assert state["hours_until"] == 0
    assert state["subject"] == "Mathematik"
    assert state["room"] == "A1"
    assert state["cancelled_periods"] == [2]
    assert state["data_age_minutes"] == 0
    assert state["last_error"] is None
    assert state["bundesland"] == "Schleswig-Holstein"
    assert state["holidays_updated"] is None
    json.dumps(state)


def test_wecker_morgen_for_next_school_day() -> None:
    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)

    state = addon.build_state(now)

    assert state["wecker_morgen"] is True
    assert state["wecker_morgen_date"] == "2026-10-20"
    assert state["wecker_morgen_start"] == "09:50"
    assert state["wecker_morgen_period"] == 3
    assert state["wecker_morgen_subject"] == "Physik"
    assert state["wecker_morgen_reason"] == "Unterricht"


def test_wecker_morgen_off_on_weekend() -> None:
    now = datetime(2026, 10, 23, 13, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)

    state = addon.build_state(now)

    assert state["wecker_morgen"] is False
    assert state["wecker_morgen_date"] == "2026-10-24"
    assert state["wecker_morgen_reason"] == "Wochenende"


def test_wecker_morgen_off_on_holiday() -> None:
    now = datetime(2026, 10, 20, 18, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)
    addon.holidays = herbstferien(now)

    state = addon.build_state(now)

    assert state["wecker_morgen"] is False
    assert state["wecker_morgen_date"] == "2026-10-21"
    assert state["wecker_morgen_reason"] == "Herbstferien"
    assert state["wecker_morgen_start"] is None
    assert state["status"] == "ferien"


def test_holidays_bridge_missing_plan_data() -> None:
    """Ohne IServ-Daten fuer Ferientage kommt der Status aus dem Kalender."""
    now = datetime(2026, 10, 21, 8, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)
    addon.holidays = herbstferien(now)

    state = addon.build_state(now)

    assert state["status"] == "ferien"
    assert state["target_date"] == "2026-10-26"
    assert state["start"] is None
    assert state["period"] is None
    assert state["hours_until"] is None
    assert state["bundesland"] == "Schleswig-Holstein"
    assert state["holidays_updated"] == now.isoformat()


def test_build_state_after_noon_uses_next_day() -> None:
    now = datetime(2026, 10, 19, 13, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)

    state = addon.build_state(now)

    assert state["target_date"] == "2026-10-20"
    assert state["start"] == "09:50"
    assert state["start_iso"] == "2026-10-20T09:50:00+02:00"
    assert state["period"] == 3
    assert state["subject"] == "Physik"


def test_start_uses_24h_format_even_in_the_evening() -> None:
    """Der Zustand ist die Uhrzeit im 24-Stunden-Format (kein 12h-Format)."""
    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    monday = date(2026, 10, 19)
    dataset = PlanDataset(fetched_at=now, source="/plan")
    dataset.days[monday] = DayPlan(
        date=monday,
        lessons=[
            Lesson(
                period=1,
                start=time(19, 20),
                end=time(20, 5),
                subject="Musik",
                date=monday,
            )
        ],
    )
    addon.dataset = dataset

    state = addon.build_state(now)

    assert state["start"] == "19:20"
    assert state["start_iso"] == "2026-10-19T19:20:00+02:00"
    assert re.fullmatch(r"\d{2}:\d{2}", state["start"])


def test_hours_until_counts_from_now() -> None:
    now = datetime(2026, 10, 20, 22, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)
    wednesday = date(2026, 10, 21)
    addon.dataset.days[wednesday] = DayPlan(
        date=wednesday,
        lessons=[Lesson(period=1, start=time(7, 45), subject="Englisch", date=wednesday)],
    )

    state = addon.build_state(now)

    assert state["target_date"] == "2026-10-21"
    assert state["hours_until"] == 9


def test_build_state_without_data_reports_failure() -> None:
    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    addon.last_error = "Noch keine Plandaten"

    state = addon.build_state(now)

    assert state["start"] is None
    assert state["status"] == "error"
    assert state["connection"] == "verbindung fehlgeschlagen"
    assert state["wecker_morgen"] is False
    assert state["wecker_morgen_reason"] == "keine Daten"
    assert state["last_error"] == "Noch keine Plandaten"


def test_discovery_templates_use_existing_state_keys() -> None:
    """Jedes ``value_json.<key>`` aus den Vorlagen muss im Status vorhanden sein."""
    from app.mqtt import discovery_messages

    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)
    state = addon.build_state(now)

    for topic, payload in discovery_messages("homeassistant", "IServ Test", "1.1.1"):
        template = payload.get("json_attributes_template", "") + payload.get(
            "value_template", ""
        )
        for key in re.findall(r"value_json\.(\w+)", template):
            assert key in state, f"{topic}: value_json.{key} fehlt im Status"


def test_connection_state_reports_progress() -> None:
    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    assert addon.connection_state() == "verbinden"

    addon.dataset = make_dataset(now)
    assert addon.connection_state() == "verbunden"

    addon.last_error = "Login fehlgeschlagen"
    assert addon.connection_state() == "verbindung fehlgeschlagen"


def test_should_refresh_respects_age_and_night() -> None:
    now = datetime(2026, 10, 19, 10, 0, tzinfo=TZ)
    addon = make_addon(refresh_minutes=30)

    addon.dataset = PlanDataset(fetched_at=now - timedelta(minutes=10))
    assert addon.should_refresh(now) is False

    addon.dataset.fetched_at = now - timedelta(minutes=31)
    assert addon.should_refresh(now) is True

    night = datetime(2026, 10, 19, 23, 0, tzinfo=TZ)
    addon.dataset.fetched_at = night - timedelta(hours=2)
    assert addon.should_refresh(night) is False
    addon.dataset.fetched_at = night - timedelta(hours=7)
    assert addon.should_refresh(night) is True


def test_should_refresh_retries_after_failure() -> None:
    now = datetime(2026, 10, 19, 10, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = PlanDataset(fetched_at=now - timedelta(minutes=45))
    addon.last_error = "HTTP 500"
    addon.last_attempt = now - timedelta(minutes=1)
    assert addon.should_refresh(now) is False

    addon.last_attempt = now - timedelta(minutes=6)
    assert addon.should_refresh(now) is True


def test_should_refresh_waits_after_failed_first_fetch() -> None:
    now = datetime(2026, 10, 19, 10, 0, tzinfo=TZ)
    addon = make_addon()
    addon.last_error = "Login fehlgeschlagen"
    addon.last_attempt = now - timedelta(minutes=1)
    assert addon.should_refresh(now) is False

    addon.last_attempt = now - timedelta(minutes=5)
    assert addon.should_refresh(now) is True


def test_should_refresh_backs_off_after_repeated_failures() -> None:
    """Nach mehreren Fehlversuchen waechst die Wartezeit (hier 20 Minuten)."""
    now = datetime(2026, 10, 19, 10, 0, tzinfo=TZ)
    addon = make_addon(refresh_minutes=30)
    addon.last_error = "HTTP 500"
    addon.failure_count = 3
    addon.last_attempt = now - timedelta(minutes=15)
    assert addon.should_refresh(now) is False

    addon.last_attempt = now - timedelta(minutes=21)
    assert addon.should_refresh(now) is True

    # Nach vielen Fehlversuchen bleibt es bei hoechstens einer Stunde.
    addon.failure_count = 12
    addon.last_attempt = now - timedelta(minutes=59)
    assert addon.should_refresh(now) is False
    addon.last_attempt = now - timedelta(minutes=61)
    assert addon.should_refresh(now) is True


def test_should_refresh_uses_normal_interval_after_filter_error() -> None:
    """Ein abgelehnter Filter (HTTP 400) wird nicht im Minutentakt wiederholt."""
    now = datetime(2026, 10, 19, 10, 0, tzinfo=TZ)
    addon = make_addon(refresh_minutes=30)
    addon.last_error = (
        "Plan-Abruf fehlgeschlagen (HTTP 400): Die erforderliche Eigenschaft classes fehlt."
    )
    addon.failure_count = 1
    addon.filter_contract_error = True
    addon.last_attempt = now - timedelta(minutes=10)
    assert addon.should_refresh(now) is False

    addon.last_attempt = now - timedelta(minutes=31)
    assert addon.should_refresh(now) is True



def test_refresh_marks_filter_error_and_keeps_last_dataset() -> None:
    """Der Filterfehler wird erkannt; der letzte gute Stand bleibt erhalten."""
    addon = make_addon()
    now = datetime(2026, 10, 20, 7, 0, tzinfo=TZ)
    dataset = make_dataset(now)
    addon.dataset = dataset

    class FailingClient:
        last_raw = ""
        last_content_type = ""
        last_url = ""

        def fetch_dataset(self, default_year=None):  # noqa: ARG002 - Testdouble
            raise FetchError(
                "Plan-Abruf fehlgeschlagen (HTTP 400): "
                "Die erforderliche Eigenschaft classes fehlt."
            )

        def invalidate_session(self) -> None:
            return None

    addon.client = FailingClient()
    addon.refresh(now)

    assert addon.filter_contract_error is True
    assert addon.failure_count == 1
    assert addon.dataset is dataset
    assert "Eigenschaft classes" in (addon.last_error or "")
    state = addon.build_state(now)
    assert state["status"] == "ok"
    assert state["connection"] == "verbindung fehlgeschlagen"


def test_refresh_holidays_loads_calendar(monkeypatch) -> None:
    now = datetime(2026, 10, 21, 8, 0, tzinfo=TZ)
    calls: list[str] = []

    def fake_load(bundesland, now=None, **kwargs):  # noqa: ARG001 - Testdouble
        calls.append(bundesland)
        return HolidayCalendar(
            bundesland=bundesland,
            fetched_at=datetime(2026, 10, 21, 8, 0, tzinfo=TZ),
            ranges=[Holiday(date(2026, 10, 12), date(2026, 10, 24), "Herbstferien")],
        )

    monkeypatch.setattr(main_module, "load_holidays", fake_load)
    addon = make_addon(bundesland="Bayern")

    addon.refresh_holidays(now)
    assert calls == ["Bayern"]
    assert addon.holidays.bundesland == "Bayern"
    assert len(addon.holidays.ranges) == 1

    # Innerhalb von sieben Tagen kein weiterer Abruf.
    addon.refresh_holidays(now + timedelta(days=1))
    assert calls == ["Bayern"]

    # Nach Ablauf der sieben Tage wird erneut geladen.
    addon.refresh_holidays(now + timedelta(days=8))
    assert calls == ["Bayern", "Bayern"]


def test_refresh_holidays_waits_after_failure(monkeypatch) -> None:
    now = datetime(2026, 10, 21, 8, 0, tzinfo=TZ)
    calls: list[int] = []

    def fake_load(bundesland, now=None, **kwargs):  # noqa: ARG001 - Testdouble
        calls.append(1)
        return HolidayCalendar(bundesland=bundesland, warnings=["offline"])

    monkeypatch.setattr(main_module, "load_holidays", fake_load)
    addon = make_addon()

    addon.refresh_holidays(now)
    assert len(calls) == 1
    # Nach einem Fehler erst nach sechs Stunden erneut versuchen.
    addon.refresh_holidays(now + timedelta(minutes=30))
    assert len(calls) == 1
    addon.refresh_holidays(now + timedelta(hours=7))
    assert len(calls) == 2
    # Die Warnung bleibt im Status sichtbar.
    assert addon.holidays.warnings



def test_cache_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main_module, "CACHE_FILE", tmp_path / "cache.json")

    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)
    addon.save_cache()

    stored = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    assert stored["fetched_at"] == now.isoformat()

    restored = make_addon()
    restored.load_cache()
    assert restored.dataset is not None
    assert sorted(restored.dataset.days) == [date(2026, 10, 19), date(2026, 10, 20)]
    assert restored.dataset.fetched_at == now
    assert restored.dataset.days[date(2026, 10, 19)].lessons[1].cancelled is True


def test_dump_raw_writes_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "DEBUG_DIR", tmp_path / "debug")
    addon = make_addon(debug_dump=True)
    addon.dump_raw('{"ok": true}', "application/json")

    assert (tmp_path / "debug" / "plan_raw.json").exists()
    meta = json.loads((tmp_path / "debug" / "plan_raw.meta.json").read_text(encoding="utf-8"))
    assert meta["content_type"] == "application/json"
    assert "plan_url" not in meta


def test_failed_refresh_dumps_raw_response(tmp_path, monkeypatch) -> None:
    """Auch fehlgeschlagene Antworten landen im Debug-Dump (IServ-Web-App)."""
    monkeypatch.setattr(main_module, "DEBUG_DIR", tmp_path / "debug")
    addon = make_addon(debug_dump=True)

    class FailingClient:
        last_raw = "<html><title>IServ</title><div id=\"app\"></div></html>"
        last_content_type = "text/html"
        last_url = "https://schule.example.org/iserv/timetable"

        def fetch_dataset(self, default_year=None):  # noqa: ARG002 - Testdouble
            raise FetchError("Plan enthielt keine verwertbaren Daten")

        def invalidate_session(self) -> None:
            return None

    addon.client = FailingClient()
    addon.refresh(datetime(2026, 10, 21, 7, 0, tzinfo=TZ))

    assert addon.last_error is not None
    assert (tmp_path / "debug" / "plan_raw.html").exists()
    meta = json.loads((tmp_path / "debug" / "plan_raw.meta.json").read_text(encoding="utf-8"))
    assert meta["content_type"] == "text/html"


def test_failed_refresh_keeps_last_dataset() -> None:
    """Ein Abruffehler darf keine Stunden als ausgefallen melden."""
    addon = make_addon()
    now = datetime(2026, 10, 20, 7, 0, tzinfo=TZ)
    dataset = make_dataset(now)
    addon.dataset = dataset

    class FailingClient:
        last_raw = ""
        last_content_type = ""
        last_url = ""

        def fetch_dataset(self, default_year=None):  # noqa: ARG002 - Testdouble
            raise FetchError("Plan-Abruf fehlgeschlagen (HTTP 404)")

        def invalidate_session(self) -> None:
            return None

    addon.client = FailingClient()
    addon.refresh(now)

    # Letzter guter Stand bleibt erhalten - nichts wird als "faellt aus" markiert.
    assert addon.dataset is dataset
    state = addon.build_state(now)
    assert state["status"] == "ok"
    assert state["start"] == "09:50"
    assert "404" in (state["last_error"] or "")


def test_invalid_options_are_reported_but_addon_keeps_running() -> None:
    addon = Addon(AddonOptions())
    problems = addon.options.validate()
    assert any("iserv_url" in problem for problem in problems)



def test_supervisor_mqtt_credentials_are_used(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "supervisor_mqtt_config",
        lambda: {
            "host": "core-mosquitto",
            "port": 8883,
            "username": "homeassistant",
            "password": "geheim",
            "ssl": True,
        },
    )
    options = AddonOptions()
    assert Addon._apply_supervisor_mqtt(options) is True
    assert options.mqtt_host == "core-mosquitto"
    assert options.mqtt_port == 8883
    assert options.mqtt_ssl is True
    assert options.mqtt_username == "homeassistant"
    assert options.mqtt_password == "geheim"


def test_without_supervisor_defaults_are_kept(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "supervisor_mqtt_config", lambda: None)
    options = AddonOptions()
    assert Addon._apply_supervisor_mqtt(options) is False
    assert options.mqtt_host == "core-mosquitto"
    assert options.mqtt_port == 1883
    assert options.mqtt_ssl is False
    assert options.mqtt_username == ""


def test_refresh_mqtt_credentials_after_auth_error(monkeypatch) -> None:
    responses = [
        {"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "alt", "ssl": False},
        {"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "neu", "ssl": False},
    ]

    def fake_config():
        return responses.pop(0) if responses else None

    monkeypatch.setattr(main_module, "supervisor_mqtt_config", fake_config)
    addon = make_addon()

    assert addon.options.mqtt_username == "addons"
    assert addon.options.mqtt_password == "alt"
    assert addon._refresh_mqtt_credentials() == {
        "host": "core-mosquitto",
        "port": 1883,
        "username": "addons",
        "password": "neu",
        "ssl": False,
    }


def test_refresh_mqtt_credentials_without_supervisor() -> None:
    """Ohne Supervisor-Zugang gibt es nichts zu erneuern."""
    addon = make_addon()
    assert addon.options.mqtt_username == ""
    assert addon._refresh_mqtt_credentials() is None

