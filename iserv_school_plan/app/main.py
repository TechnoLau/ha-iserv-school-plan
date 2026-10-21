"""Einstiegspunkt des Add-ons: plant Abrufe, berechnet den Schultag und
veroeffentlicht die Entities per MQTT Discovery."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path

from .config import AddonOptions, load_options
from .iserv import AuthError, FetchError, IservClient, IservOptions
from .mqtt import MqttPublisher, supervisor_mqtt_config
from .plan import TZ, PlanDataset, apply_period_times, select_plan

VERSION = "1.0.0"
DATA_DIR = Path(os.environ.get("ISERV_DATA", "/data"))
CACHE_FILE = DATA_DIR / "cache.json"
DEBUG_DIR = DATA_DIR / "debug"
DEFAULT_MQTT_HOST = "core-mosquitto"
TICK_SECONDS = 30
RETRY_MINUTES = 5
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 5
NIGHT_MAX_AGE = timedelta(hours=6)

_LOGGER = logging.getLogger("iserv_school_plan")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=TZ)


class Addon:
    def __init__(self, options: AddonOptions) -> None:
        self.options = options
        self._apply_supervisor_mqtt(options)
        self.client = IservClient(
            IservOptions(
                base_url=options.iserv_url,
                plan_url=options.plan_url,
                username=options.username,
                password=options.password,
                plan_format=options.plan_format,
                klasse=options.klasse,
                timeout=30,
                verify_ssl=options.verify_ssl,
            )
        )
        self.publisher = MqttPublisher(options, VERSION)
        self.dataset: PlanDataset | None = None
        self.last_error: str | None = None
        self.last_attempt: datetime | None = None
        self._running = True

    # ------------------------------------------------------------------ Cache
    @staticmethod
    def _apply_supervisor_mqtt(options: AddonOptions) -> None:
        """Beim Mosquitto-Add-on die Zugangsdaten vom Supervisor uebernehmen."""
        if options.mqtt_host not in ("", DEFAULT_MQTT_HOST):
            return
        config = supervisor_mqtt_config()
        if not config:
            return
        options.mqtt_host = config["host"]
        options.mqtt_port = config["port"]
        options.mqtt_ssl = config["ssl"]
        if not options.mqtt_username:
            options.mqtt_username = config["username"]
            options.mqtt_password = options.mqtt_password or config["password"]
        _LOGGER.info(
            "MQTT-Zugangsdaten vom Supervisor uebernommen (%s:%s)",
            options.mqtt_host,
            options.mqtt_port,
        )

    def load_cache(self) -> None:
        if not CACHE_FILE.exists():
            return
        try:
            payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            dataset = PlanDataset.from_dict(payload)
            dataset.fetched_at = _aware(dataset.fetched_at)
            if dataset.days:
                self.dataset = dataset
                _LOGGER.info(
                    "Cache geladen: %s Tage, Stand %s",
                    len(dataset.days),
                    dataset.fetched_at,
                )
        except (OSError, ValueError, KeyError) as err:
            _LOGGER.warning("Cache unlesbar: %s", err)

    def save_cache(self) -> None:
        if self.dataset is None:
            return
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            temp = CACHE_FILE.with_suffix(".tmp")
            temp.write_text(
                json.dumps(self.dataset.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(CACHE_FILE)
        except OSError as err:
            _LOGGER.warning("Cache konnte nicht geschrieben werden: %s", err)

    def dump_raw(self, raw: str, content_type: str) -> None:
        if not self.options.debug_dump or not raw:
            return
        extension = "json" if "json" in content_type else "ics" if "calendar" in content_type else "html"
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"plan_raw.{extension}").write_text(raw, encoding="utf-8")
            meta = {
                "content_type": content_type,
                "plan_url": self.options.plan_url,
                "bytes": len(raw),
                "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
            }
            (DEBUG_DIR / "plan_raw.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _LOGGER.info("Rohantwort gespeichert unter %s", DEBUG_DIR)
        except OSError as err:
            _LOGGER.warning("Debug-Dump fehlgeschlagen: %s", err)

    # --------------------------------------------------------------- Abrufen
    def should_refresh(self, now: datetime) -> bool:
        if self.last_error and self.last_attempt:
            if now - self.last_attempt < timedelta(minutes=RETRY_MINUTES):
                return False
        fetched_at = self.dataset.fetched_at if self.dataset else None
        if fetched_at is None:
            return True
        age = now - fetched_at
        night = now.hour >= NIGHT_START_HOUR or now.hour < NIGHT_END_HOUR
        if night:
            return age >= NIGHT_MAX_AGE
        return age >= timedelta(minutes=self.options.refresh_minutes)

    def refresh(self, now: datetime) -> None:
        self.last_attempt = now
        try:
            dataset = self.client.fetch_dataset(default_year=now.year)
        except (AuthError, FetchError, ValueError) as err:
            self.last_error = str(err)
            self.client.invalidate_session()
            _LOGGER.warning("Plan-Abruf fehlgeschlagen: %s", err)
            return
        except Exception as err:  # pragma: no cover - unerwartete Fehler sichtbar machen
            self.last_error = f"Unerwarteter Fehler: {err}"
            _LOGGER.exception("Unerwarteter Fehler beim Plan-Abruf")
            return

        dataset.fetched_at = now
        apply_period_times(dataset, self.options.parsed_period_times)
        self.dataset = dataset
        self.last_error = None
        self.dump_raw(self.client.last_raw, self.client.last_content_type)
        self.save_cache()
        _LOGGER.info(
            "Plan aktualisiert: %s Tage (%s bis %s), Quelle %s",
            len(dataset.days),
            dataset.coverage_start,
            dataset.coverage_end,
            dataset.source,
        )

    # ----------------------------------------------------------------- State
    def build_state(self, now: datetime) -> dict:
        result = select_plan(
            now,
            self.dataset,
            free_days=self.options.parsed_free_days,
            lookahead_days=self.options.lookahead_days,
        )
        dataset = self.dataset
        fetched_at = dataset.fetched_at if dataset else None
        age = int((now - fetched_at).total_seconds() // 60) if fetched_at else None
        lesson = result.first_lesson

        return {
            "start": result.start.isoformat() if result.start else None,
            "period": result.first_period,
            "subject": lesson.subject if lesson else None,
            "room": lesson.room if lesson else None,
            "target_date": result.target_date.isoformat() if result.target_date else None,
            "alarm": result.alarm,
            "status": result.status,
            "reason": result.reason,
            "planned_first_period": result.planned_first_period,
            "cancelled_periods": result.cancelled_periods,
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "data_age_minutes": age,
            "skipped": [
                {"date": skip.date.isoformat(), "reason": skip.reason}
                for skip in result.skipped[:14]
            ],
            "warnings": dataset.warnings if dataset else [],
            "last_error": self.last_error,
            "iserv_url": self.options.iserv_url,
        }

    def tick(self, now: datetime) -> None:
        if self.should_refresh(now):
            self.refresh(now)
        state = self.build_state(now)
        self.publisher.publish_state(state)
        _LOGGER.debug(
            "Status %s, Ziel %s, Beginn %s, Wecker %s",
            state["status"],
            state["target_date"],
            state["start"],
            state["alarm"],
        )
        if self.dataset is None and not self.last_error:
            self.last_error = "Noch keine Plandaten"
            self.publisher.publish_state(self.build_state(now))

    # ------------------------------------------------------------------ Loop
    def _wait(self, seconds: int) -> None:
        """Wartet in kleinen Schritten, damit SIGTERM schnell greift."""
        for _ in range(seconds):
            if not self._running:
                return
            time_module.sleep(1)

    def handle_signal(self, signum, frame) -> None:  # noqa: ARG002 - Signalhandler
        _LOGGER.info("Signal %s empfangen - beende Add-on", signum)
        self._running = False

    def run(self) -> None:
        problems = self.options.validate()
        for problem in problems:
            _LOGGER.error("Konfiguration: %s", problem)
        if problems:
            self.last_error = "; ".join(problems)

        self.load_cache()
        if self.dataset is not None:
            apply_period_times(self.dataset, self.options.parsed_period_times)
        self.publisher.start()
        _LOGGER.info(
            "IServ-Schulplan %s gestartet (Server: %s, Abruf alle %s Minuten)",
            VERSION,
            self.options.iserv_url,
            self.options.refresh_minutes,
        )
        try:
            while self._running:
                now = datetime.now(TZ)
                try:
                    self.tick(now)
                except Exception:  # pragma: no cover - Schleife am Leben halten
                    _LOGGER.exception("Fehler im Durchlauf")
                self._wait(TICK_SECONDS)
        finally:
            self.publisher.stop()
            _LOGGER.info("Add-on beendet")


def main() -> int:
    try:
        options = load_options()
    except (OSError, ValueError) as err:
        print(f"Optionen konnten nicht gelesen werden: {err}", file=sys.stderr)
        return 2

    setup_logging(options.log_level)
    addon = Addon(options)
    signal.signal(signal.SIGTERM, addon.handle_signal)
    signal.signal(signal.SIGINT, addon.handle_signal)
    addon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

