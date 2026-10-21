"""Optionen des Home-Assistant-Add-ons (aus /data/options.json)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .holidays import DEFAULT_BUNDESLAND, normalize_bundesland
from .plan import parse_period_times

OPTIONS_PATH = Path(os.environ.get("ISERV_OPTIONS", "/data/options.json"))
DEFAULT_URL = "https://schule.example.org"
DEFAULT_MQTT_HOST = "core-mosquitto"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEVICE_NAME = "IServ Stundenplan"

# Nur diese Schluessel duerfen aus /data/options.json kommen. Alles andere
# (alte Optionen wie plan_url, free_days, mqtt_*, verify_ssl, log_level)
# wird bewusst ignoriert - MQTT und TLS kommen vom Supervisor.
USER_OPTION_FIELDS = (
    "iserv_url",
    "username",
    "password",
    "bundesland",
    "period_times",
    "refresh_minutes",
    "debug_dump",
)


@dataclass
class AddonOptions:
    iserv_url: str = DEFAULT_URL
    username: str = ""
    password: str = ""
    bundesland: str = DEFAULT_BUNDESLAND
    period_times: str = ""
    refresh_minutes: int = 30
    debug_dump: bool = False
    # Laufzeitwerte: werden zur Laufzeit vom Supervisor gefuellt.
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = 1883
    mqtt_ssl: bool = False
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX
    device_name: str = DEVICE_NAME
    parsed_period_times: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.iserv_url = _normalize_url(self.iserv_url)
        self.bundesland = normalize_bundesland(self.bundesland)
        self.refresh_minutes = max(5, min(int(self.refresh_minutes or 30), 720))
        self.debug_dump = _as_bool(self.debug_dump)
        self.parsed_period_times = parse_period_times(self.period_times)

    @property
    def log_level(self) -> str:
        """Log-Level automatisch: Debug-Dump braucht Details, sonst info."""
        return "debug" if self.debug_dump else "info"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if DEFAULT_URL in self.iserv_url:
            problems.append("iserv_url muss auf die eigene IServ-Adresse gesetzt werden")
        if self.username and not self.password:
            problems.append("username ohne password gesetzt")
        return problems


def _normalize_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        return DEFAULT_URL
    if "://" not in value:
        value = "https://" + value
    return value


def _as_bool(value, default: bool = False) -> bool:
    """Tolerant fuer Strings wie ``"false"`` (z. B. aus YAML oder alten Optionen)."""
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        return text not in ("false", "0", "nein", "no", "off")
    if value is None:
        return default
    return bool(value)


def load_options(path: Path | None = None) -> AddonOptions:
    target = path or OPTIONS_PATH
    raw: object = {}
    if target.exists():
        raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw = {}
    data: dict = {}
    for key in USER_OPTION_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "refresh_minutes":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        elif key == "debug_dump":
            value = _as_bool(value)
        data[key] = value
    return AddonOptions(**data)

