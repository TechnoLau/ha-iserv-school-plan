"""Tests fuer die Optionsverarbeitung."""

from __future__ import annotations

import json
from datetime import date, time

from app.config import AddonOptions, load_options


def test_defaults_are_sane() -> None:
    options = AddonOptions()
    assert options.iserv_url.startswith("https://")
    assert options.plan_format == "auto"
    assert options.lookahead_days == 21
    assert options.refresh_minutes == 30
    # Nur die IServ-Adresse ist Pflicht - die Plantabelle wird automatisch gesucht.
    assert options.plan_url == ""
    assert any("iserv_url" in item for item in options.validate())
    assert not any("plan_url" in item for item in options.validate())


def test_load_options_parses_strings(tmp_path) -> None:
    path = tmp_path / "options.json"
    path.write_text(
        json.dumps(
            {
                "iserv_url": "schule.example.net/",
                "plan_url": "/iserv/plan",
                "lookahead_days": "10",
                "refresh_minutes": "15",
                "mqtt_port": "1884",
                "verify_ssl": "false",
                "free_days": "2026-07-06..2026-08-14:Sommerferien;2026-10-03:Feiertag",
                "period_times": "1=07:45-08:30;2=08:35-09:20",
                "unknown_option": "ignored",
            }
        ),
        encoding="utf-8",
    )
    options = load_options(path)
    assert options.iserv_url == "https://schule.example.net"
    assert options.lookahead_days == 10
    assert options.refresh_minutes == 15
    assert options.mqtt_port == 1884
    assert options.verify_ssl is False
    assert options.parsed_free_days[0].name == "Sommerferien"
    assert options.parsed_free_days[0].contains(date(2026, 7, 6))
    assert options.parsed_period_times[1] == (time(7, 45), time(8, 30))
    assert options.validate() == []


def test_limits_are_clamped_and_format_validated() -> None:
    options = AddonOptions(
        plan_format="xml",
        lookahead_days=500,
        refresh_minutes=1,
        iserv_url="meine-schule.de",
    )
    assert options.plan_format == "auto"
    assert options.lookahead_days == 60
    assert options.refresh_minutes == 5
    assert options.iserv_url == "https://meine-schule.de"


def test_mqtt_settings_are_normalised() -> None:
    options = AddonOptions(
        mqtt_ssl="false",
        mqtt_port="8883",
        mqtt_discovery_prefix="/homeassistant/",
        verify_ssl="",
        debug_dump="ja",
    )
    assert options.mqtt_ssl is False
    assert options.mqtt_port == 8883
    assert options.mqtt_discovery_prefix == "homeassistant"
    assert options.verify_ssl is True
    assert options.debug_dump is True


def test_validation_reports_missing_configuration() -> None:
    options = AddonOptions(username="kind", password="")
    problems = options.validate()
    assert any("iserv_url" in item for item in problems)
    assert not any("plan_url" in item for item in problems)
    assert any("password" in item for item in problems)
