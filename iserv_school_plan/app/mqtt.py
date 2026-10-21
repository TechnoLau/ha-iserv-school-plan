"""MQTT-Discovery-Bruecke: Das Add-on kann keine HA-Entities direkt anlegen,
deshalb werden Sensoren per MQTT Discovery veroeffentlicht."""

from __future__ import annotations

import json
import logging
import os
import socket
from collections.abc import Callable
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

from .config import AddonOptions

_LOGGER = logging.getLogger(__name__)

STATE_TOPIC = "iserv_school_plan/state"
AVAILABILITY_TOPIC = "iserv_school_plan/availability"
NODE_ID = "iserv_school_plan"
SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"
# CONNACK-Codes fuer "bad user name or password" und "not authorized":
# MQTT 3.1.1 nutzt 4/5, paho-mqtt 2.x liefert die v5-Codes 134/135.
AUTH_ERROR_CODES = (4, 5, 134, 135)
AUTH_ERROR_LOG_LIMIT = 3
MAX_CREDENTIAL_REFRESHES = 3
DEVICE = {
    "identifiers": [NODE_ID],
    "manufacturer": "IServ",
    "model": "Stundenplan (Add-on)",
}

ATTRIBUTES = {
    "status": "{{ {'reason': value_json.reason, 'plan_status': value_json.status, "
    "'bundesland': value_json.bundesland, 'holidays_updated': value_json.holidays_updated, "
    "'fetched_at': value_json.fetched_at, 'age_minutes': value_json.data_age_minutes, "
    "'mqtt_connected': value_json.mqtt_connected, 'iserv_url': value_json.iserv_url, "
    "'last_error': value_json.last_error, 'skipped': value_json.skipped, "
    "'warnings': value_json.warnings} | tojson }}",
    "lesson": "{{ {'start': value_json.start, 'start_iso': value_json.start_iso, "
    "'hours_until': value_json.hours_until, 'target_date': value_json.target_date, "
    "'subject': value_json.subject, 'room': value_json.room, "
    "'planned_first_period': value_json.planned_first_period, "
    "'cancelled_periods': value_json.cancelled_periods} | tojson }}",
    "date": "{{ {'date': value_json.target_date, 'start': value_json.start, "
    "'start_iso': value_json.start_iso, 'hours_until': value_json.hours_until, "
    "'period': value_json.period, 'subject': value_json.subject, "
    "'room': value_json.room, 'planned_first_period': value_json.planned_first_period, "
    "'reason': value_json.reason, 'status': value_json.status, "
    "'cancelled_periods': value_json.cancelled_periods} | tojson }}",
    "morgen": "{{ {'date': value_json.wecker_morgen_date, "
    "'start': value_json.wecker_morgen_start, 'period': value_json.wecker_morgen_period, "
    "'subject': value_json.wecker_morgen_subject, 'room': value_json.wecker_morgen_room, "
    "'reason': value_json.wecker_morgen_reason, 'plan_status': value_json.status} | tojson }}",
}

# Entities aus 1.0.x: leere retained Payloads entfernen sie aus Home Assistant.
REMOVED_DISCOVERY = (
    ("sensor", "iserv_stundenbeginn"),
    ("binary_sensor", "iserv_schulwecker"),
)


def discovery_messages(prefix: str, device_name: str, version: str) -> list[tuple[str, dict]]:
    """Alle Entity-Konfigurationen fuer MQTT Discovery."""
    device = {**DEVICE, "name": device_name, "sw_version": version}

    def config(
        component: str, unique_id: str, payload: dict, object_id: str | None = None
    ) -> tuple[str, dict]:
        object_id = object_id or unique_id
        return (
            f"{prefix}/{component}/{unique_id}/config",
            {
                "state_topic": STATE_TOPIC,
                "availability_topic": AVAILABILITY_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
                "unique_id": unique_id,
                # Seit HA 2026.4 bestimmt default_entity_id die Entity-ID
                # (z. B. sensor.stundenplan_status). object_id ist der alte
                # Schluessel fuer aeltere Versionen; unbekannte Schluessel
                # entfernt das Discovery-Schema (extra=REMOVE_EXTRA).
                "default_entity_id": f"{component}.{object_id}",
                "object_id": object_id,
                **payload,
            },
        )

    return [
        config(
            "sensor",
            "iserv_status",
            {
                "name": "Stundenplan Status",
                "icon": "mdi:lan-connect",
                "value_template": "{{ value_json.connection }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["status"],
            },
            object_id="stundenplan_status",
        ),
        config(
            "sensor",
            "iserv_erste_stunde",
            {
                "name": "Erste Unterrichtsstunde",
                "icon": "mdi:clock-start",
                "value_template": "{{ value_json.period if value_json.period is not none "
                "else 'unknown' }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["lesson"],
            },
            object_id="erste_unterrichtsstunde",
        ),
        config(
            "sensor",
            "iserv_naechster_schultag",
            {
                "name": "Naechster Schultag",
                "icon": "mdi:calendar-clock",
                # Kein device_class "date": der Zustand ist die Uhrzeit der
                # ersten Stunde (24-Stunden-Format, z. B. 07:45); das Datum
                # steht im Attribut "date".
                "value_template": "{{ value_json.start if value_json.start "
                "else 'unknown' }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["date"],
            },
            object_id="naechster_schultag",
        ),
        config(
            "binary_sensor",
            "iserv_wecker_morgen",
            {
                "name": "Wecker morgen",
                "icon": "mdi:alarm",
                "payload_on": "ON",
                "payload_off": "OFF",
                "value_template": "{{ 'ON' if value_json.wecker_morgen else 'OFF' }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["morgen"],
            },
            object_id="wecker_morgen",
        ),
    ]


def removed_discovery_topics(prefix: str) -> list[str]:
    """Discovery-Topics der Entities aus 1.0.x (werden mit leerem Payload geloescht)."""
    return [f"{prefix}/{component}/{unique_id}/config" for component, unique_id in REMOVED_DISCOVERY]


def supervisor_mqtt_config() -> dict | None:
    """Broker-Zugangsdaten beim Supervisor abfragen (Add-on mit ``mqtt:want``).

    Liefert ``None``, wenn das Add-on nicht unter dem Supervisor laeuft oder der
    MQTT-Dienst nicht eingerichtet ist.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    request = Request(
        SUPERVISOR_MQTT_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - feste Supervisor-URL
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as err:
        _LOGGER.debug("Supervisor-MQTT-Dienst nicht abrufbar: %s", err)
        return None

    data = payload.get("data") or {}
    host, port = data.get("host"), data.get("port")
    if not host or not port:
        return None
    return {
        "host": str(host),
        "port": int(port),
        "username": data.get("username") or "",
        "password": data.get("password") or "",
        "ssl": bool(data.get("ssl")),
    }


class MqttPublisher:
    """Verbindet sich mit dem Mosquitto-Broker und publiziert Discovery + State."""

    def __init__(
        self,
        options: AddonOptions,
        version: str,
        credentials_refresher: Callable[[], dict | None] | None = None,
    ) -> None:
        self.options = options
        self.version = version
        self._credentials_refresher = credentials_refresher
        self._auth_errors = 0
        self._credential_refreshes = 0
        self._client = self._build_client()
        self._last_state: dict | None = None

    def _build_client(self):
        client_id = f"{NODE_ID}-{socket.gethostname()[:12]}"
        try:
            from paho.mqtt.enums import CallbackAPIVersion  # paho-mqtt >= 2.0
        except ImportError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=client_id)
        else:
            client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2, client_id=client_id
            )

        if self.options.mqtt_ssl:
            client.tls_set()
        if self.options.mqtt_username:
            client.username_pw_set(self.options.mqtt_username, self.options.mqtt_password or None)
        client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        return client

    @property
    def connected(self) -> bool:
        return bool(getattr(self._client, "is_connected", lambda: False)())

    def start(self) -> None:
        _LOGGER.info(
            "Verbinde MQTT %s:%s (Benutzer %s, Discovery-Prefix %s)",
            self.options.mqtt_host,
            self.options.mqtt_port,
            self.options.mqtt_username or "ohne",
            self.options.mqtt_discovery_prefix,
        )
        self._client.connect_async(self.options.mqtt_host, self.options.mqtt_port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            info = self._client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
            info.wait_for_publish(timeout=2)
        except Exception:  # pragma: no cover - Broker evtl. nicht erreichbar
            _LOGGER.debug("Offline-Status konnte nicht gesendet werden", exc_info=True)
        try:
            self._client.disconnect()
            self._client.loop_stop()
        except Exception:  # pragma: no cover - Aufraeumen darf nicht werfen
            _LOGGER.debug("MQTT-Stop fehlgeschlagen", exc_info=True)

    @staticmethod
    def _rc_ok(reason_code) -> bool:
        value = getattr(reason_code, "value", reason_code)
        return value == 0

    @staticmethod
    def _rc_value(reason_code):
        value = getattr(reason_code, "value", reason_code)
        return value if isinstance(value, int) else None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if not self._rc_ok(reason_code):
            self._handle_connect_failure(client, reason_code)
            return
        self._auth_errors = 0
        self._credential_refreshes = 0
        _LOGGER.info("MQTT verbunden")
        client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        for topic in removed_discovery_topics(self.options.mqtt_discovery_prefix):
            client.publish(topic, "", qos=1, retain=True)
        for topic, payload in discovery_messages(
            self.options.mqtt_discovery_prefix, self.options.device_name, self.version
        ):
            client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
        if self._last_state is not None:
            client.publish(
                STATE_TOPIC, json.dumps(self._last_state, ensure_ascii=False), qos=1, retain=True
            )

    def _handle_connect_failure(self, client, reason_code) -> None:
        """Fehlgeschlagene Anmeldung erklaeren und veraltete Daten erneuern."""
        self._auth_errors += 1
        code = self._rc_value(reason_code)
        if code in AUTH_ERROR_CODES:
            if self._auth_errors <= AUTH_ERROR_LOG_LIMIT:
                _LOGGER.error(
                    "MQTT-Verbindung abgelehnt: %s (Benutzer %r). Pruefe die "
                    "Zugangsdaten: Mosquitto 7 akzeptiert Home-Assistant-Benutzer "
                    "und die Logins im Mosquitto-Add-on (homeassistant/addons sind "
                    "reserviert); alternativ mqtt_username/mqtt_password setzen.",
                    reason_code,
                    self.options.mqtt_username,
                )
            else:
                _LOGGER.debug("MQTT-Verbindung weiterhin abgelehnt: %s", reason_code)
            self._refresh_credentials(client)
            if self._auth_errors == AUTH_ERROR_LOG_LIMIT + 1:
                _LOGGER.warning(
                    "MQTT-Anmeldung schlaegt weiterhin fehl - bitte mqtt_username/"
                    "mqtt_password in den Add-on-Optionen pruefen oder das "
                    "Mosquitto-Add-on neu starten"
                )
            return

        if self._auth_errors <= AUTH_ERROR_LOG_LIMIT:
            _LOGGER.error("MQTT-Verbindung abgelehnt: %s", reason_code)
        else:
            _LOGGER.debug("MQTT-Verbindung weiterhin abgelehnt: %s", reason_code)

    def _refresh_credentials(self, client) -> None:
        """Nach einem Auth-Fehler die Zugangsdaten neu beim Supervisor holen.

        Die Dienst-Zugangsdaten (Mosquitto 7: Benutzer ``addons``) schreibt das
        Broker-Add-on bei jedem Start neu in seine Passwortdatei und meldet sie
        dem Supervisor. Dieses Add-on liest sie aber nur beim eigenen Start -
        nach einem Neustart des Brokers kann es deshalb mit veralteten Daten
        dauerhaft auf "Not authorized" laufen. Hier werden sie nachgeladen; der
        naechste automatische Reconnect von paho nutzt sie unmittelbar.
        """
        if self._credentials_refresher is None:
            return
        if self._credential_refreshes >= MAX_CREDENTIAL_REFRESHES:
            return
        self._credential_refreshes += 1
        config = self._credentials_refresher()
        if not config:
            return
        username = config.get("username") or ""
        password = config.get("password") or ""
        if (username, password) == (self.options.mqtt_username, self.options.mqtt_password):
            _LOGGER.debug("Supervisor liefert unveraenderte MQTT-Zugangsdaten")
            return
        self.options.mqtt_username = username
        self.options.mqtt_password = password
        if hasattr(client, "username_pw_set"):
            client.username_pw_set(username or None, password or None)
        _LOGGER.info(
            "Neue MQTT-Zugangsdaten vom Supervisor uebernommen (Benutzer %r) - "
            "naechster Reconnect nutzt sie",
            username,
        )

    def _on_disconnect(self, client, userdata, *args) -> None:
        _LOGGER.warning("MQTT-Verbindung getrennt - Reconnect laeuft")

    def publish_state(self, state: dict) -> None:
        if state == self._last_state:
            return
        self._last_state = state
        if not self.connected:
            _LOGGER.debug("MQTT nicht verbunden - State wird beim Reconnect gesendet")
            return
        self._client.publish(
            STATE_TOPIC, json.dumps(state, ensure_ascii=False), qos=1, retain=True
        )

