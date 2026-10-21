"""Optionen des Home-Assistant-Add-ons (aus /data/options.json)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .plan import FreeDay, parse_free_days, parse_period_times

OPTIONS_PATH = Path(os.environ.get("ISERV_OPTIONS", "/data/options.json"))
DEFAULT_URL = "https://schule.example.org"


@dataclass
class AddonOptions:
    iserv_url: str = DEFAULT_URL
    plan_url: str = ""
    username: str = ""
    password: str = ""
    plan_format: str = "auto"
    klasse: str = ""
    free_days: str = ""
    period_times: str = ""
    lookahead_days: int = 21
    refresh_minutes: int = 30
    mqtt_host: str = "core-mosquitto"
    mqtt_port: int = 1883
    mqtt_ssl: bool = False
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_discovery_prefix: str = "homeassistant"
    verify_ssl: bool = True
    debug_dump: bool = False
    log_level: str = "info"
    device_name: str = "IServ Stundenplan"
    parsed_free_days: list[FreeDay] = field(default_factory=list)
    parsed_period_times: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.iserv_url = _normalize_url(self.iserv_url)
        fmt = (self.plan_format or "auto").lower()
        self.plan_format = fmt if fmt in ("auto", "html", "json", "ics") else "auto"
        self.lookahead_days = max(1, min(int(self.lookahead_days or 21), 60))
        self.refresh_minutes = max(5, min(int(self.refresh_minutes or 30), 720))
        self.mqtt_port = int(self.mqtt_port or 1883)
        self.mqtt_ssl = _as_bool(self.mqtt_ssl)
        self.verify_ssl = _as_bool(self.verify_ssl, default=True)
        self.debug_dump = _as_bool(self.debug_dump)
        self.mqtt_discovery_prefix = (self.mqtt_discovery_prefix or "homeassistant").strip("/") or "homeassistant"
        self.parsed_free_days = parse_free_days(self.free_days)
        self.parsed_period_times = parse_period_times(self.period_times)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if DEFAULT_URL in self.iserv_url:
            problems.append("iserv_url muss auf die eigene IServ-Adresse gesetzt werden")
        if not self.plan_url:
            problems.append("plan_url fehlt (Pfad oder URL zum Stundenplan/Export)")
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


def _coerce(raw: dict) -> dict:
    """Supervisor liefert Zahlen/Bools je nach Schema korrekt; Strings defensiv casten."""
    result = dict(raw)
    for key in ("lookahead_days", "refresh_minutes", "mqtt_port"):
        if key in result:
            try:
                result[key] = int(result[key])
            except (TypeError, ValueError):
                result.pop(key)
    for key in ("verify_ssl", "debug_dump", "mqtt_ssl"):
        if key in result:
            result[key] = _as_bool(result[key], default=key == "verify_ssl")
    return result


def load_options(path: Path | None = None) -> AddonOptions:
    target = path or OPTIONS_PATH
    raw: dict = {}
    if target.exists():
        raw = json.loads(target.read_text(encoding="utf-8"))
    known = {field_name for field_name in AddonOptions.__dataclass_fields__}
    data = {key: value for key, value in _coerce(raw).items() if key in known}
    return AddonOptions(**data)
