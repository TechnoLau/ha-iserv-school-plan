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
from .holidays import HolidayCalendar, load_holidays
from .iserv import IservClient, IservError, IservOptions
from .mqtt import MqttPublisher, supervisor_mqtt_config
from .plan import (
    TZ,
    PlanDataset,
    apply_period_times,
    hours_until,
    plan_tomorrow,
    select_plan,
)

VERSION = "1.1.0"
DATA_DIR = Path(os.environ.get("ISERV_DATA", "/data"))
CACHE_FILE = DATA_DIR / "cache.json"
DEBUG_DIR = DATA_DIR / "debug"
TICK_SECONDS = 30
RETRY_MINUTES = 5
# Nach mehreren Fehlversuchen waechst die Wartezeit (5, 10, 20, 40, 60 Minuten).
RETRY_MAX_MINUTES = 60
# Ferien/Feiertage werden alle sieben Tage aktualisiert; nach einem Netzfehler
# fruehestens nach sechs Stunden erneut versuchen.
LOOKAHEAD_DAYS = 21
HOLIDAY_RETRY = timedelta(hours=6)
# Wortlaut, mit dem IServ einen ungueltigen Filter ableitet. Ein erneuter Abruf
# im Minutentakt bringt dann nichts - es gilt das normale Intervall.
FILTER_ERROR_MARKERS = ("erforderliche eigenschaft", "fehler beim verarbeiten")
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 5
NIGHT_MAX_AGE = timedelta(hours=6)
CONNECTING = "verbinden"
CONNECTED = "verbunden"
CONNECTION_FAILED = "verbindung fehlgeschlagen"

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
        self._mqtt_from_supervisor = self._apply_supervisor_mqtt(options)
        self.client = IservClient(
            IservOptions(
                base_url=options.iserv_url,
                username=options.username,
                password=options.password,
                timeout=30,
            )
        )
        self.publisher = MqttPublisher(
            options, VERSION, credentials_refresher=self._refresh_mqtt_credentials
        )
        self.dataset: PlanDataset | None = None
        self.holidays = HolidayCalendar(bundesland=options.bundesland)
        self._holiday_attempt: datetime | None = None
        self.last_error: str | None = None
        self.last_attempt: datetime | None = None
        # Fehlversuche in Folge - bestimmt die Wartezeit bis zum naechsten Versuch.
        self.failure_count = 0
        # True, wenn IServ den Filter abgelehnt hat (HTTP 400): dann aendert
        # schnelles Wiederholen nichts.
        self.filter_contract_error = False
        self._running = True

    # ------------------------------------------------------------------ Cache
    @staticmethod
    def _apply_supervisor_mqtt(options: AddonOptions) -> bool:
        """MQTT-Zugang immer vom Supervisor beziehen (Mosquitto-Add-on).

        Rueckgabe ``True``, wenn die Zugangsdaten vom Supervisor stammen - dann
        werden sie bei einem Auth-Fehler erneut abgefragt. Ein eigener Broker
        laesst sich ueber die Add-on-Optionen nicht mehr konfigurieren.
        """
        config = supervisor_mqtt_config()
        if not config:
            _LOGGER.warning(
                "MQTT-Dienst des Supervisors nicht erreichbar - versuche %s:%s",
                options.mqtt_host,
                options.mqtt_port,
            )
            return False
        options.mqtt_host = config["host"]
        options.mqtt_port = config["port"]
        options.mqtt_ssl = config["ssl"]
        options.mqtt_username = config["username"]
        options.mqtt_password = config["password"]
        _LOGGER.info(
            "MQTT-Zugangsdaten vom Supervisor uebernommen (%s:%s, TLS %s, Benutzer %s)",
            options.mqtt_host,
            options.mqtt_port,
            "an" if options.mqtt_ssl else "aus",
            options.mqtt_username or "-",
        )
        return True

    def _refresh_mqtt_credentials(self) -> dict | None:
        """Nach einem MQTT-Auth-Fehler die Supervisor-Zugangsdaten erneuern."""
        if not self._mqtt_from_supervisor:
            return None
        _LOGGER.info("Frage MQTT-Zugangsdaten erneut beim Supervisor ab")
        return supervisor_mqtt_config()

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

    # ------------------------------------------------------ Ferien/Feiertage
    def refresh_holidays(self, now: datetime, force: bool = False) -> None:
        """Ferien/Feiertage laden, wenn der Stand aelter als sieben Tage ist."""
        if not force and not self.holidays.is_stale(now):
            return
        if (
            not force
            and self._holiday_attempt is not None
            and now - self._holiday_attempt < HOLIDAY_RETRY
        ):
            return
        self._holiday_attempt = now
        calendar = load_holidays(self.options.bundesland, now=now)
        # Warnungen des bisherigen Stands nicht verlieren.
        for warning in self.holidays.warnings:
            if warning not in calendar.warnings:
                calendar.warnings.append(warning)
        self.holidays = calendar
        if calendar.warnings:
            for warning in calendar.warnings:
                _LOGGER.warning("%s", warning)

    def dump_raw(self, raw: str, content_type: str) -> None:
        if not self.options.debug_dump or not raw:
            return
        extension = "json" if "json" in content_type else "ics" if "calendar" in content_type else "html"
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"plan_raw.{extension}").write_text(raw, encoding="utf-8")
            meta = {
                "content_type": content_type,
                "resolved_url": getattr(self.client, "last_url", "") or "",
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
    def _retry_delay(self) -> timedelta:
        """Wartezeit nach einem Fehlversuch.

        Ein abgelehnter Filter (HTTP 400) bleibt bei jeder Anfrage gleich - dort
        gilt das normale Aktualisierungsintervall. Sonst verdoppelt sich die
        Wartezeit mit jedem Fehlversuch bis maximal eine Stunde, damit ein
        dauerhaft fehlschlagender Abruf IServ nicht im Minutentakt belastet.
        """
        if self.filter_contract_error:
            return timedelta(minutes=max(RETRY_MINUTES, self.options.refresh_minutes))
        minutes = RETRY_MINUTES * (2 ** max(0, self.failure_count - 1))
        return timedelta(minutes=min(minutes, RETRY_MAX_MINUTES))

    def should_refresh(self, now: datetime) -> bool:
        if self.last_error and self.last_attempt:
            if now - self.last_attempt < self._retry_delay():
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
        except (IservError, ValueError) as err:
            self.failure_count += 1
            self.last_error = str(err)
            self.filter_contract_error = any(
                marker in self.last_error.lower() for marker in FILTER_ERROR_MARKERS
            )
            self.client.invalidate_session()
            _LOGGER.warning("Plan-Abruf fehlgeschlagen: %s", err)
            if self.filter_contract_error:
                _LOGGER.warning(
                    "IServ hat den Filter der Stundenplan-Schnittstelle abgelehnt - "
                    "naechster Versuch in %d Minuten",
                    max(RETRY_MINUTES, self.options.refresh_minutes),
                )
            # Auch Fehlantworten sichern - bei der neuen IServ-Web-App ist genau
            # dieses HTML die Grundlage fuer die Parser-Anpassung.
            self.dump_raw(self.client.last_raw, self.client.last_content_type)
            return
        except Exception as err:  # pragma: no cover - unerwartete Fehler sichtbar machen
            self.failure_count += 1
            self.last_error = f"Unerwarteter Fehler: {err}"
            _LOGGER.exception("Unerwarteter Fehler beim Plan-Abruf")
            return

        self.failure_count = 0
        self.filter_contract_error = False
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
    def connection_state(self) -> str:
        """Zustand der Anbindung: ``verbinden`` -> ``verbunden``/``fehlgeschlagen``."""
        if self.last_error:
            return CONNECTION_FAILED
        if self.dataset is None:
            return CONNECTING
        return CONNECTED

    def build_state(self, now: datetime) -> dict:
        result = select_plan(
            now,
            self.dataset,
            holidays=self.holidays.ranges,
            lookahead_days=LOOKAHEAD_DAYS,
        )
        tomorrow = plan_tomorrow(now, self.dataset, self.holidays.ranges)
        dataset = self.dataset
        fetched_at = dataset.fetched_at if dataset else None
        age = int((now - fetched_at).total_seconds() // 60) if fetched_at else None
        lesson = result.first_lesson
        start = result.start.isoformat() if result.start else None
        warnings = list(dataset.warnings if dataset else [])
        warnings.extend(self.holidays.warnings)

        return {
            "connection": self.connection_state(),
            "mqtt_connected": self.publisher.connected,
            "status": result.status,
            "start": start,
            "start_iso": start,
            "period": result.first_period,
            "hours_until": hours_until(now, result.start),
            "subject": lesson.subject if lesson else None,
            "room": lesson.room if lesson else None,
            "target_date": result.target_date.isoformat() if result.target_date else None,
            "reason": result.reason,
            "planned_first_period": result.planned_first_period,
            "cancelled_periods": result.cancelled_periods,
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "data_age_minutes": age,
            "skipped": [
                {"date": skip.date.isoformat(), "reason": skip.reason}
                for skip in result.skipped[:14]
            ],
            "warnings": warnings,
            "last_error": self.last_error,
            "iserv_url": self.options.iserv_url,
            "bundesland": self.options.bundesland,
            "holidays_updated": (
                self.holidays.fetched_at.isoformat() if self.holidays.fetched_at else None
            ),
            "wecker_morgen": tomorrow.school,
            "wecker_morgen_date": tomorrow.date.isoformat(),
            "wecker_morgen_start": (
                tomorrow.start.strftime("%H:%M") if tomorrow.start else None
            ),
            "wecker_morgen_period": tomorrow.period,
            "wecker_morgen_subject": (
                tomorrow.first_lesson.subject if tomorrow.first_lesson else None
            ),
            "wecker_morgen_room": (
                tomorrow.first_lesson.room if tomorrow.first_lesson else None
            ),
            "wecker_morgen_reason": tomorrow.reason,
        }

    def tick(self, now: datetime) -> None:
        self.refresh_holidays(now)
        if self.should_refresh(now):
            self.refresh(now)
        state = self.build_state(now)
        self.publisher.publish_state(state)
        _LOGGER.debug(
            "Verbindung %s, Status %s, Ziel %s, Beginn %s, Wecker morgen %s",
            state["connection"],
            state["status"],
            state["target_date"],
            state["start"],
            state["wecker_morgen"],
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
        # MQTT zuerst starten, damit die Entities sofort erscheinen; die
        # Feriendaten (Netzabruf) folgen direkt danach.
        self.publisher.start()
        self.refresh_holidays(datetime.now(TZ), force=True)
        _LOGGER.info(
            "IServ-Schulplan %s gestartet (Server: %s, Bundesland: %s, Abruf alle %s Minuten)",
            VERSION,
            self.options.iserv_url,
            self.options.bundesland,
            self.options.refresh_minutes,
        )
        _LOGGER.debug("Stundenplan-Quelle: %s", self.client.options.resolved_plan_url)
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

