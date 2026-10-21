"""Prueft, ob config.yaml und die Python-Optionen zusammenpassen."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from app.config import USER_OPTION_FIELDS, AddonOptions  # noqa: E402 - erst nach importorskip
from app.holidays import BUNDESLAENDER  # noqa: E402 - erst nach importorskip
from app.main import VERSION  # noqa: E402 - erst nach importorskip

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
DOCKERFILE_PATH = CONFIG_PATH.parent / "Dockerfile"
CHANGELOG_PATH = CONFIG_PATH.parent / "CHANGELOG.md"


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
    # Genau die sieben Optionen aus 1.1.0 - alte Schluessel sind entfernt.
    assert options == set(USER_OPTION_FIELDS)
    assert options <= set(AddonOptions.__dataclass_fields__)


def test_default_options_are_valid() -> None:
    options = AddonOptions(**load_config()["options"])
    assert options.bundesland == "Schleswig-Holstein"
    assert options.refresh_minutes >= 5
    assert options.log_level == "info"


def test_schema_lists_all_bundeslaender() -> None:
    schema = load_config()["schema"]["bundesland"]
    assert schema.startswith("list(") and schema.endswith(")")
    for name in BUNDESLAENDER:
        assert name in schema, f"{name} fehlt in der Bundesland-Auswahl"


def test_dockerfile_builds_with_python_base() -> None:
    """Ohne build.yaml baut der Supervisor mit ghcr.io/home-assistant/base.

    Dort ist pip nicht enthalten (``pip: not found``, Exit-Code 127), deshalb
    muss das Python-Image direkt im Dockerfile stehen. ``build.yaml`` ist beim
    Supervisor ausserdem deprecated.
    """
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "FROM python:3.12-alpine" in dockerfile
    assert "BUILD_FROM" not in dockerfile
    assert "apk add --no-cache tzdata" in dockerfile
    assert "python -m pip install" in dockerfile
    assert not (CONFIG_PATH.parent / "build.yaml").exists()


def test_version_matches_changelog() -> None:
    version = load_config()["version"]
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    assert f"## {version}" in changelog


def test_version_matches_code() -> None:
    assert VERSION == load_config()["version"]
