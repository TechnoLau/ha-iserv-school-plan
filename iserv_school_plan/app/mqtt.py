"""MQTT-Discovery-Bruecke: Das Add-on kann keine HA-Entities direkt anlegen,
deshalb werden Sensoren per MQTT Discovery veroeffentlicht."""

from __future__ import annotations

import json
import logging
import os
import socket
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

from .config import AddonOptions

_LOGGER = logging.getLogger(__name__)

STATE_TOPIC = "iserv_school_plan/state"
AVAILABILITY_TOPIC = "iserv_school_plan/availability"
NODE_ID = "iserv_school_plan"
SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"
DEVICE = {
    "identifiers": [NODE_ID],
    "manufacturer": "IServ",
    "model": "Stundenplan (Add-on)",
}

ATTRIBUTES = {
    "start": "{{ {'period': value_json.period, 'subject': value_json.subject, "
    "'room': value_json.room, 'reason': value_json.reason} | tojson }}",
    "period": "{{ {'subject': value_json.subject, 'room': value_json.room, "
    "'planned_first_period': value_json.planned_first_period, "
    "'cancelled_periods': value_json.cancelled_periods} | tojson }}",
    "date": "{{ {'reason': value_json.reason, 'status': value_json.status} | tojson }}",
    "alarm": "{{ {'reason': value_json.reason, 'start': value_json.start, "
    "'target_date': value_json.target_date} | tojson }}",
    "status": "{{ {'reason': value_json.reason, 'fetched_at': value_json.fetched_at, "
    "'age_minutes': value_json.data_age_minutes, 'skipped': value_json.skipped, "
    "'warnings': value_json.warnings, 'last_error': value_json.last_error} | tojson }}",
}


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
                # (z. B. sensor.stundenbeginn). object_id ist der alte Schluessel
                # fuer aeltere Versionen; unbekannte Schluessel entfernt das
                # Discovery-Schema (extra=REMOVE_EXTRA), daher sind beide sicher.
                "default_entity_id": f"{component}.{object_id}",
                "object_id": object_id,
                **payload,
            },
        )

    return [
        config(
            "sensor",
            "iserv_stundenbeginn",
            {
                "name": "Stundenbeginn",
                "device_class": "timestamp",
                "icon": "mdi:clock-start",
                "value_template": "{{ value_json.start if value_json.start else 'unknown' }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["start"],
            },
            object_id="stundenbeginn",
        ),
        config(
            "sensor",
            "iserv_erste_stunde",
            {
                "name": "Erste Unterrichtsstunde",
                "icon": "mdi:book-open-variant",
                "value_template": "{{ value_json.period if value_json.period is not none "
                "else 'unknown' }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["period"],
            },
            object_id="erste_unterrichtsstunde",
        ),
        config(
            "sensor",
            "iserv_naechster_schultag",
            {
                "name": "Naechster Schultag",
                "device_class": "date",
                "icon": "mdi:calendar-arrow-right",
                "value_template": "{{ value_json.target_date if value_json.target_date "
                "else 'unknown' }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["date"],
            },
            object_id="naechster_schultag",
        ),
        config(
            "binary_sensor",
            "iserv_schulwecker",
            {
                "name": "Schulwecker aktiv",
                "icon": "mdi:alarm",
                "payload_on": "ON",
                "payload_off": "OFF",
                "value_template": "{{ 'ON' if value_json.alarm else 'OFF' }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["alarm"],
            },
            object_id="schulwecker_aktiv",
        ),
        config(
            "sensor",
            "iserv_status",
            {
                "name": "Stundenplan Status",
                "icon": "mdi:information-outline",
                "value_template": "{{ value_json.status }}",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": ATTRIBUTES["status"],
            },
            object_id="stundenplan_status",
        ),
    ]


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

    def __init__(self, options: AddonOptions, version: str) -> None:
        self.options = options
        self.version = version
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
            "Verbinde MQTT %s:%s (Discovery-Prefix %s)",
            self.options.mqtt_host,
            self.options.mqtt_port,
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

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if not self._rc_ok(reason_code):
            _LOGGER.error("MQTT-Verbindung abgelehnt: %s", reason_code)
            return
        _LOGGER.info("MQTT verbunden")
        client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        for topic, payload in discovery_messages(
            self.options.mqtt_discovery_prefix, self.options.device_name, self.version
        ):
            client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
        if self._last_state is not None:
            client.publish(
                STATE_TOPIC, json.dumps(self._last_state, ensure_ascii=False), qos=1, retain=True
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

