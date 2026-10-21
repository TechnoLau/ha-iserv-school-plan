"""Prueft, ob config.yaml und die Python-Optionen zusammenpassen."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from app.config import AddonOptions  # noqa: E402 - erst nach importorskip

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_metadata_is_complete() -> None:
    config = load_config()
    for key in ("name", "version", "slug", "description", "arch", "options", "schema"):
        assert config.get(key), f"{key} fehlt in config.yaml"
    assert config["slug"] == "iserv_school_plan"


def test_options_match_schema_and_dataclass() -> None:
    config = load_config()
    options = set(config["options"])
    assert options == set(config["schema"])
    assert options <= set(AddonOptions.__dataclass_fields__)


def test_default_options_are_valid() -> None:
    options = AddonOptions(**load_config()["options"])
    assert options.plan_format == "auto"
    assert options.refresh_minutes >= 5
    assert options.mqtt_discovery_prefix == "homeassistant"
