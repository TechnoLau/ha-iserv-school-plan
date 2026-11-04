"""Tests fuer die MQTT-Discovery-Nachrichten."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from app.config import AddonOptions
from app.mqtt import (
    AUTH_ERROR_LOG_LIMIT,
    AVAILABILITY_TOPIC,
    MAX_CREDENTIAL_REFRESHES,
    STATE_TOPIC,
    MqttPublisher,
    discovery_messages,
    removed_discovery_topics,
    subject_switch,
)


def test_discovery_topics_and_payloads() -> None:
    messages = discovery_messages("homeassistant", "IServ Test", "1.1.1")
    topics = {topic: payload for topic, payload in messages}

    assert set(topics) == {
        "homeassistant/sensor/iserv_status/config",
        "homeassistant/sensor/iserv_erste_stunde/config",
        "homeassistant/sensor/iserv_schul_start/config",
        "homeassistant/sensor/iserv_schul_ende/config",
        "homeassistant/binary_sensor/iserv_wecker_morgen/config",
    }

    status = topics["homeassistant/sensor/iserv_status/config"]
    assert status["state_topic"] == STATE_TOPIC
    assert status["availability_topic"] == AVAILABILITY_TOPIC
    assert status["device"]["name"] == "IServ Test"
    assert status["device"]["sw_version"] == "1.1.1"
    assert "value_json.connection" in status["value_template"]

    first = topics["homeassistant/sensor/iserv_erste_stunde/config"]
    assert "value_json.period" in first["value_template"]
    assert "hours_until" in first["json_attributes_template"]

    start = topics["homeassistant/sensor/iserv_schul_start/config"]
    # Der Zustand ist die Uhrzeit (24h, z. B. 08:10); ein Datum waere mit
    # device_class "date" erwartet worden und ist deshalb entfernt.
    assert "device_class" not in start
    assert start["name"] == "Schul start"
    assert "value_json.schul_start" in start["value_template"]
    assert "schul_start_date" in start["json_attributes_template"]

    end = topics["homeassistant/sensor/iserv_schul_ende/config"]
    assert end["name"] == "Schul ende"
    assert "device_class" not in end
    assert "value_json.schul_ende" in end["value_template"]
    assert "schul_ende_period" in end["json_attributes_template"]

    morning = topics["homeassistant/binary_sensor/iserv_wecker_morgen/config"]
    assert morning["payload_on"] == "ON"
    assert morning["payload_off"] == "OFF"
    assert "value_json.wecker_morgen" in morning["value_template"]
    assert "wecker_morgen_start" in morning["json_attributes_template"]

    unique_ids = [payload["unique_id"] for payload in topics.values()]
    assert len(unique_ids) == len(set(unique_ids))


def test_discovery_entity_ids_match_documented_entity_ids() -> None:
    topics = dict(discovery_messages("homeassistant", "IServ Test", "1.1.1"))
    expected = {
        "homeassistant/sensor/iserv_status/config": "sensor.stundenplan_status",
        "homeassistant/sensor/iserv_erste_stunde/config": "sensor.erste_unterrichtsstunde",
        "homeassistant/sensor/iserv_schul_start/config": "sensor.schul_start",
        "homeassistant/sensor/iserv_schul_ende/config": "sensor.schul_ende",
        "homeassistant/binary_sensor/iserv_wecker_morgen/config": "binary_sensor.wecker_morgen",
    }
    for topic, entity_id in expected.items():
        payload = topics[topic]
        domain, _, object_id = entity_id.partition(".")
        # Ab HA 2026.4 bestimmt default_entity_id die Entity-ID.
        assert payload["default_entity_id"] == entity_id
        # Aeltere HA-Versionen kennen nur object_id (ohne Domain).
        assert payload["object_id"] == object_id
        # Die Domain muss zum Discovery-Component passen.
        assert topic.split("/")[1] == domain


def test_removed_entities_are_deleted_with_empty_payload() -> None:
    topics = removed_discovery_topics("homeassistant")
    assert topics == [
        "homeassistant/sensor/iserv_stundenbeginn/config",
        "homeassistant/binary_sensor/iserv_schulwecker/config",
        # 1.2.0: ersetzt durch sensor.schul_start / sensor.schul_ende
        "homeassistant/sensor/iserv_naechster_schultag/config",
    ]

    publisher = MqttPublisher(AddonOptions(), "1.1.1")
    published: list[tuple] = []

    class FakeClient:
        def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload, qos, retain))

        def subscribe(self, topic):
            return (0, 1)

    publisher._on_connect(FakeClient(), None, {}, 0)

    removed = {topic for topic in topics}
    for topic, payload, qos, retain in published:
        if topic in removed:
            assert payload == ""
            assert retain is True
            assert qos == 1
    assert removed <= {topic for topic, *_ in published}



def test_discovery_payloads_are_json_serialisable() -> None:
    for _topic, payload in discovery_messages("ha", "Gerät", "0.1"):
        assert json.loads(json.dumps(payload))


def test_publisher_stop_marks_offline() -> None:
    publisher = MqttPublisher(AddonOptions(), "1.0.0")
    published: list[tuple] = []
    calls: list[str] = []

    class FakeInfo:
        def wait_for_publish(self, timeout=None):
            calls.append(f"wait:{timeout}")

    class FakeClient:
        def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload, qos, retain))
            return FakeInfo()

        def disconnect(self):
            calls.append("disconnect")

        def loop_stop(self):
            calls.append("loop_stop")

    publisher._client = FakeClient()
    publisher.stop()

    assert published == [(AVAILABILITY_TOPIC, "offline", 1, True)]
    assert calls == ["wait:2", "disconnect", "loop_stop"]


def test_publisher_deduplicates_state() -> None:
    publisher = MqttPublisher(AddonOptions(), "1.0.0")
    sent: list[dict] = []

    class FakeClient:
        def is_connected(self) -> bool:
            return True

        def publish(self, topic, payload, qos=0, retain=False):
            sent.append(json.loads(payload))
            return None

    publisher._client = FakeClient()
    state = {"start": None, "alarm": False}
    publisher.publish_state(state)
    publisher.publish_state(dict(state))
    assert len(sent) == 1


def test_auth_failure_refreshes_credentials_from_supervisor() -> None:
    options = AddonOptions(mqtt_username="alt", mqtt_password="alt")
    publisher = MqttPublisher(
        options,
        "1.0.3",
        credentials_refresher=lambda: {"username": "addons", "password": "neu"},
    )

    class FakeClient:
        credentials = None

        def username_pw_set(self, username=None, password=None):
            FakeClient.credentials = (username, password)

    publisher._on_connect(FakeClient(), None, {}, 5)

    assert options.mqtt_username == "addons"
    assert options.mqtt_password == "neu"
    assert FakeClient.credentials == ("addons", "neu")


def test_auth_failure_with_paho2_reason_code_refreshes() -> None:
    """paho-mqtt 2.x meldet 'Not authorized' als Code 135, nicht als 5."""

    class ReasonCode:
        value = 135

        def __str__(self) -> str:
            return "Not authorized"

    options = AddonOptions(mqtt_username="alt", mqtt_password="alt")
    publisher = MqttPublisher(
        options,
        "1.0.3",
        credentials_refresher=lambda: {"username": "addons", "password": "neu"},
    )

    class FakeClient:
        credentials = None

        def username_pw_set(self, username=None, password=None):
            FakeClient.credentials = (username, password)

    publisher._on_connect(FakeClient(), None, {}, ReasonCode())

    assert FakeClient.credentials == ("addons", "neu")


def test_auth_failure_without_refresher_keeps_configured_credentials() -> None:
    options = AddonOptions(mqtt_username="fest", mqtt_password="geheim")
    publisher = MqttPublisher(options, "1.0.3")

    publisher._on_connect(object(), None, {}, 5)

    assert options.mqtt_username == "fest"
    assert options.mqtt_password == "geheim"


def test_auth_failure_ignores_unchanged_supervisor_credentials() -> None:
    options = AddonOptions(mqtt_username="addons", mqtt_password="gleich")
    publisher = MqttPublisher(
        options,
        "1.0.3",
        credentials_refresher=lambda: {"username": "addons", "password": "gleich"},
    )

    class FakeClient:
        called = False

        def username_pw_set(self, username=None, password=None):
            FakeClient.called = True

    publisher._on_connect(FakeClient(), None, {}, 5)

    assert FakeClient.called is False
    assert options.mqtt_password == "gleich"


def test_credential_refresh_is_limited() -> None:
    calls: list[int] = []
    publisher = MqttPublisher(
        AddonOptions(),
        "1.0.3",
        credentials_refresher=lambda: calls.append(1) or {"username": "u", "password": "p"},
    )

    class FakeClient:
        def username_pw_set(self, username=None, password=None):
            return None

    for _ in range(MAX_CREDENTIAL_REFRESHES + 4):
        publisher._handle_connect_failure(FakeClient(), 5)

    assert len(calls) == MAX_CREDENTIAL_REFRESHES


def test_auth_error_logging_is_limited_and_reset_after_success(caplog) -> None:
    publisher = MqttPublisher(AddonOptions(), "1.0.3")
    caplog.set_level(logging.DEBUG, logger="app.mqtt")

    for _ in range(6):
        publisher._handle_connect_failure(object(), 5)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "abgelehnt" in r.getMessage()]
    assert len(errors) == AUTH_ERROR_LOG_LIMIT
    warnings = [r for r in caplog.records if "schlaegt weiterhin fehl" in r.getMessage()]
    assert len(warnings) == 1

    published: list[tuple] = []

    class FakeClient:
        def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload, qos, retain))

        def subscribe(self, topic):
            return (0, 1)

    publisher._on_connect(FakeClient(), None, {}, 0)
    assert publisher._auth_errors == 0
    assert publisher._credential_refreshes == 0
    assert published[0] == (AVAILABILITY_TOPIC, "online", 0, True)


def test_subject_switch_discovery_payload() -> None:
    topic, payload = subject_switch("homeassistant", "IServ Test", "1.4.0", "Französisch")
    assert topic == "homeassistant/switch/iserv_fach_franzoesisch/config"
    assert payload["name"] == "Fach Französisch"
    assert payload["state_topic"] == "iserv_school_plan/fach/franzoesisch/state"
    assert payload["command_topic"] == "iserv_school_plan/fach/franzoesisch/set"
    assert payload["default_entity_id"] == "switch.fach_franzoesisch"
    assert payload["device"]["name"] == "IServ Test"


def test_publish_subjects_creates_and_removes_switches() -> None:
    publisher = MqttPublisher(AddonOptions(), "1.4.0")
    published: list[tuple] = []

    class FakeClient:
        def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload, qos, retain))

    publisher._client = FakeClient()
    publisher.publish_subjects(["Franz", "Nautik"], {"Franz": False})
    sent = {topic: payload for topic, payload, *_ in published}
    assert "homeassistant/switch/iserv_fach_franz/config" in sent
    assert "homeassistant/switch/iserv_fach_nautik/config" in sent
    assert sent["iserv_school_plan/fach/franz/state"] == "OFF"
    assert sent["iserv_school_plan/fach/nautik/state"] == "ON"

    published.clear()
    publisher.publish_subjects(["Franz"], {"Franz": True})
    removed = [topic for topic, payload, *_ in published if payload == ""]
    assert "homeassistant/switch/iserv_fach_nautik/config" in removed
    assert "iserv_school_plan/fach/nautik/state" in removed


def test_subject_command_calls_handler() -> None:
    publisher = MqttPublisher(AddonOptions(), "1.4.0")
    calls: list[tuple[str, bool]] = []
    published: list[tuple] = []

    class FakeClient:
        def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload, qos, retain))

    client = FakeClient()
    publisher._client = client
    publisher.set_subject_handler(lambda name, on: calls.append((name, on)))
    publisher.publish_subjects(["Franz"], {})

    message = SimpleNamespace(topic="iserv_school_plan/fach/franz/set", payload=b" OFF ")
    publisher._on_message(client, None, message)
    assert calls == [("Franz", False)]
    assert ("iserv_school_plan/fach/franz/state", "OFF", 1, True) in published

    # Unbekanntes Fach und ungueltiger Payload werden ignoriert.
    publisher._on_message(client, None, SimpleNamespace(
        topic="iserv_school_plan/fach/sport/set", payload=b"ON"
    ))
    publisher._on_message(client, None, SimpleNamespace(
        topic="iserv_school_plan/fach/franz/set", payload=b"quatsch"
    ))
    assert calls == [("Franz", False)]

