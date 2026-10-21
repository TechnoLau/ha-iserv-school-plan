"""Tests fuer die Add-on-Logik (Statusaufbau, Cache, Abrufintervalle)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, time

import pytest

import app.main as main_module
from app.config import AddonOptions
from app.iserv import FetchError
from app.main import Addon
from app.plan import TZ, DayPlan, Lesson, PlanDataset


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
        plan_url="/iserv/plan",
        period_times="1=07:45-08:30;2=08:35-09:20;3=09:50-10:35",
        **overrides,
    )
    return Addon(options)


def test_build_state_morning_uses_today() -> None:
    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)

    state = addon.build_state(now)

    assert state["status"] == "ok"
    assert state["target_date"] == "2026-10-19"
    assert state["start"] == "2026-10-19T07:45:00+02:00"
    assert state["alarm"] is True
    assert state["period"] == 1
    assert state["subject"] == "Mathematik"
    assert state["room"] == "A1"
    assert state["cancelled_periods"] == [2]
    assert state["data_age_minutes"] == 0
    assert state["last_error"] is None
    json.dumps(state)


def test_build_state_after_noon_uses_next_day() -> None:
    now = datetime(2026, 10, 19, 13, 0, tzinfo=TZ)
    addon = make_addon()
    addon.dataset = make_dataset(now)

    state = addon.build_state(now)

    assert state["target_date"] == "2026-10-20"
    assert state["start"] == "2026-10-20T09:50:00+02:00"
    assert state["period"] == 3
    assert state["subject"] == "Physik"


def test_build_state_without_data_has_no_alarm() -> None:
    now = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    addon = make_addon()
    addon.last_error = "Noch keine Plandaten"

    state = addon.build_state(now)

    assert state["start"] is None
    assert state["alarm"] is False
    assert state["status"] == "error"
    assert state["last_error"] == "Noch keine Plandaten"


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
    assert state["start"] == "2026-10-20T09:50:00+02:00"
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
            "port": 1883,
            "username": "homeassistant",
            "password": "geheim",
            "ssl": False,
        },
    )
    options = AddonOptions()
    Addon._apply_supervisor_mqtt(options)
    assert options.mqtt_host == "core-mosquitto"
    assert options.mqtt_username == "homeassistant"
    assert options.mqtt_password == "geheim"


def test_explicit_mqtt_host_is_not_overridden(monkeypatch) -> None:
    calls: list[int] = []

    def fake_config():
        calls.append(1)
        return {"host": "core-mosquitto", "port": 1883, "username": "x", "password": "y", "ssl": False}

    monkeypatch.setattr(main_module, "supervisor_mqtt_config", fake_config)
    options = AddonOptions(mqtt_host="broker.local", mqtt_username="eigen")
    Addon._apply_supervisor_mqtt(options)
    assert options.mqtt_host == "broker.local"
    assert options.mqtt_username == "eigen"
    assert calls == []


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


def test_refresh_mqtt_credentials_not_done_for_custom_login(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "supervisor_mqtt_config",
        lambda: {
            "host": "core-mosquitto",
            "port": 1883,
            "username": "addons",
            "password": "y",
            "ssl": False,
        },
    )
    addon = make_addon(mqtt_username="eigen", mqtt_password="geheim")

    assert addon.publisher.options.mqtt_username == "eigen"
    assert addon._refresh_mqtt_credentials() is None

