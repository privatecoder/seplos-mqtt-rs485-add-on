"""
MQTT: the Paho client (callback API 1.x and 2.x), state publishing with change suppression based
on what was actually handed to the broker, and resync requests (reconnect, HA birth message).
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

import paho.mqtt.client as mqtt

from .config import Config

logger = logging.getLogger("SeplosBMS.mqtt")


class Publisher:
    """
    Publishes state topics. A payload is suppressed only if the same payload was already handed to
    the broker successfully, so a publish that failed (e.g. during a broker outage) is retried with
    the next poll, and replay() resends the latest state of every topic after a reconnect.
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self._latest: dict[str, tuple[str, bool]] = {}
        self._sent: dict[str, str] = {}

    @staticmethod
    def _encode(payload: Any) -> str:
        return payload if isinstance(payload, str) else json.dumps(payload, indent=2)

    def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> bool:
        """Publish unconditionally; True if the client accepted it."""
        try:
            info = self.client.publish(topic, self._encode(payload), qos=qos, retain=retain)
        except Exception as e:
            logger.error("Publishing %s failed: %s", topic, e)
            return False
        rc = getattr(info, "rc", 0)
        if rc != 0:
            logger.debug("Publishing %s failed: rc %s", topic, rc)
            return False
        return True

    def state(self, topic: str, payload: Any, retain: bool = False) -> bool:
        """Publish a state if it differs from the last successfully published one; True if sent."""
        text = self._encode(payload)
        self._latest[topic] = (text, retain)
        if self._sent.get(topic) == text:
            return False
        if self.publish(topic, text, retain=retain):
            self._sent[topic] = text
            return True
        return False

    def replay(self) -> None:
        """Resend the latest state of every topic (after a reconnect or an HA restart)."""
        self._sent.clear()
        for topic, (text, retain) in list(self._latest.items()):
            if self.publish(topic, text, retain=retain):
                self._sent[topic] = text


def _is_success(reason_code: Any) -> bool:
    """Paho 1.x passes an int, 2.x a ReasonCode."""
    failure = getattr(reason_code, "is_failure", None)
    return not failure if failure is not None else reason_code == 0


class MqttConnection:
    """Paho client wrapper; the callbacks only set flags, the main loop does the work."""

    def __init__(self, config: Config, will_topic: str) -> None:
        self.config = config
        self.connected = False
        self.resync = threading.Event()
        self._connects = 0
        self.birth_topic = f"{config.ha_discovery_prefix}/status"
        version = getattr(mqtt, "CallbackAPIVersion", None)
        self.client = mqtt.Client(version.VERSION2) if version is not None else mqtt.Client()
        self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.client.will_set(will_topic, payload="offline", qos=2, retain=False)

    def connect(self) -> None:
        self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()

    # callback signatures differ between Paho 1.x (client, userdata, flags, rc) and
    # 2.x (client, userdata, flags, reason_code, properties)
    def on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if not _is_success(reason_code):
            self.connected = False
            logger.error("Failed to connect to MQTT broker: %s", reason_code)
            return
        self.connected = True
        self._connects += 1
        logger.info("Connected to MQTT broker (%s:%s)", self.config.mqtt_host, self.config.mqtt_port)
        if self.config.enable_ha_discovery:
            client.subscribe(self.birth_topic)
        if self._connects > 1:
            # the broker may have lost non-retained states and its session
            self.resync.set()

    def on_disconnect(self, _client, _userdata, *_args) -> None:
        self.connected = False

    def on_message(self, _client, _userdata, message) -> None:
        if message.topic == self.birth_topic and message.payload == b"online":
            logger.info("Home Assistant started, resending discovery and states")
            self.resync.set()

    def is_connected(self) -> bool:
        return self.connected and self.client.is_connected()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()
