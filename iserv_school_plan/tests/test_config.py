"""Tests fuer die Optionsverarbeitung."""

from __future__ import annotations

import json
from datetime import time

from app.config import AddonOptions, load_options


def test_defaults_are_sane() -> None:
    options = AddonOptions()
    assert options.iserv_url.startswith("https://")
    assert options.bundesland == "Schleswig-Holstein"
    assert options.refresh_minutes == 30
    assert options.debug_dump is False
    assert options.log_level == "info"
    # Nur die IServ-Adresse ist Pflicht - alles andere wird automatisch ermittelt.
    assert any("iserv_url" in item for item in options.validate())


def test_load_options_parses_strings_and_ignores_legacy_keys(tmp_path) -> None:
    path = tmp_path / "options.json"
    path.write_text(
        json.dumps(
            {
                "iserv_url": "schule.example.net/",
                "refresh_minutes": "15",
                "bundesland": "bayern",
                "period_times": "1=07:45-08:30;2=08:35-09:20",
                # Optionen aus 1.0.x - sie duerfen nicht mehr wirken.
                "plan_url": "/iserv/plan",
                "plan_format": "json",
                "klasse": "5a",
                "free_days": "2026-07-06..2026-08-14:Sommerferien",
                "lookahead_days": "10",
                "mqtt_host": "broker.local",
                "mqtt_port": "1884",
                "mqtt_username": "eigen",
                "verify_ssl": "false",
                "log_level": "debug",
                "device_name": "Alter Name",
                "unknown_option": "ignored",
            }
        ),
        encoding="utf-8",
    )
    options = load_options(path)
    assert options.iserv_url == "https://schule.example.net"
    assert options.refresh_minutes == 15
    assert options.bundesland == "Bayern"
    assert options.parsed_period_times[1] == (time(7, 45), time(8, 30))
    # MQTT kommt vom Supervisor, nicht aus den Optionen.
    assert options.mqtt_host == "core-mosquitto"
    assert options.mqtt_port == 1883
    assert options.mqtt_username == ""
    assert options.device_name == "IServ Stundenplan"
    assert options.log_level == "info"
    for removed in ("plan_url", "plan_format", "klasse", "free_days", "lookahead_days", "verify_ssl"):
        assert not hasattr(options, removed), f"{removed} darf es nicht mehr geben"
    assert options.validate() == []


def test_debug_dump_enables_debug_log_level() -> None:
    options = AddonOptions(debug_dump="ja")
    assert options.debug_dump is True
    assert options.log_level == "debug"


def test_limits_are_clamped() -> None:
    options = AddonOptions(refresh_minutes=1, iserv_url="meine-schule.de")
    assert options.refresh_minutes == 5
    assert options.iserv_url == "https://meine-schule.de"
    assert AddonOptions(refresh_minutes=5000).refresh_minutes == 720


def test_bundesland_is_normalised() -> None:
    assert AddonOptions(bundesland="DE-SH").bundesland == "Schleswig-Holstein"
    assert AddonOptions(bundesland="sh").bundesland == "Schleswig-Holstein"
    assert AddonOptions(bundesland="thüringen").bundesland == "Thüringen"
    assert AddonOptions(bundesland="Atlantis").bundesland == "Schleswig-Holstein"
    assert AddonOptions(bundesland="").bundesland == "Schleswig-Holstein"


def test_validation_reports_missing_configuration() -> None:
    options = AddonOptions(username="kind", password="")
    problems = options.validate()
    assert any("iserv_url" in item for item in problems)
    assert any("password" in item for item in problems)

