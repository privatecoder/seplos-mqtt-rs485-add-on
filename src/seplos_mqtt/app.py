"""Composition and main loop: one serial bus, round-robin pack polling, MQTT publishing."""
from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import paho.mqtt.client as mqtt
import serial
from paho.mqtt import MQTTException
from serial.serialutil import SerialException

from . import health, topics
from .config import Config
from .decoders.identity import device_firmware
from .discovery import Discovery
from .pack import BatteryPack, now_iso
from .state import StateStore
from .system import SystemReader
from .transport import SerialBus

logger = logging.getLogger("SeplosBMS")


@dataclass
class PackState:
    pack: BatteryPack
    address: int
    last_success_ts: float = 0.0
    publish_counter: int = 0
    availability: str = "offline"
    firmware_reported: bool = False


@dataclass
class AppState:
    config: Config
    client: Any = None
    bus: SerialBus | None = None
    store: StateStore | None = None
    packs: list[PackState] = field(default_factory=list)
    # timestamp of the last successful BMS poll (seconds since epoch)
    last_bms_update_ts: float = 0.0
    # MQTT connection state as seen by the callbacks
    mqtt_connected: bool = False

    def max_age(self) -> int:
        return health.max_poll_age(len(self.packs) or self.config.number_of_packs or 1, self.config.mqtt_update_interval)

    def mqtt_loop_running(self) -> bool:
        """Best-effort check whether Paho's network loop thread is alive."""
        thread = getattr(self.client, "_thread", None)
        return bool(thread and getattr(thread, "is_alive", lambda: False)())

    def is_healthy(self, now: float) -> bool:
        return (
            self.mqtt_connected
            and self.mqtt_loop_running()
            and self.bus is not None
            and self.bus.is_open
            and (now - self.last_bms_update_ts) < self.max_age()
        )


def configure_logging(config: Config) -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s:%(name)s:%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
    logger.setLevel(levels.get(config.logging_level, logging.INFO))


def initialize_mqtt(state: AppState) -> Any:
    config = state.config

    def on_connect(_client, _userdata, _flags, reason_code) -> None:
        if reason_code == 0:
            state.mqtt_connected = True
            logger.info("Connected to MQTT broker (%s:%s)", config.mqtt_host, config.mqtt_port)
        else:
            state.mqtt_connected = False
            logger.error("Failed to connect to MQTT broker: %s", reason_code)

    def on_disconnect(_client, _userdata, _reason_code, _properties=None) -> None:
        state.mqtt_connected = False

    client = mqtt.Client()
    client.username_pw_set(config.mqtt_username, config.mqtt_password)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.will_set(topics.availability(config.mqtt_topic), payload="offline", qos=2, retain=False)
    try:
        client.connect(config.mqtt_host, config.mqtt_port, keepalive=60)
        client.loop_start()
        return client
    except MQTTException as e:
        logger.error("MQTT connection failed: %s", e)
        sys.exit(1)


def initialize_serial(config: Config) -> Any:
    try:
        logger.info("Initializing serial interface %s at %s baud", config.serial_interface, config.baudrate)
        return serial.Serial(port=config.serial_interface, baudrate=config.baudrate, timeout=0.5)
    except SerialException as e:
        logger.error("Serial initialization failed: %s", e)
        sys.exit(1)


def start_health_server(state: AppState) -> None:
    threading.Thread(target=health.serve, args=(state.is_healthy,), daemon=True).start()
    logger.info("Health endpoint started on http://0.0.0.0:%s/health", health.PORT)


def publish_pack_availability(state: AppState, pack_state: PackState, now: float) -> None:
    last_success = pack_state.last_success_ts
    desired = "online" if last_success > 0 and (now - last_success) <= state.max_age() else "offline"
    if pack_state.availability != desired:
        state.client.publish(topics.pack_availability(state.config.mqtt_topic, pack_state.address), desired, retain=False)
        pack_state.availability = desired


def publish_system_data(state: AppState, reader: SystemReader) -> None:
    """Read and publish the system values (multi pack mode via the CAN port RS485 bus only)."""
    if not state.config.multi_pack or not state.packs:
        return
    data = reader.read(state.bus)
    base = state.config.mqtt_topic
    state.client.publish(topics.system_availability(base), "online" if data is not None else "offline", retain=False)
    if data is not None and data != reader.last_data:
        reader.last_data = data
        logger.info("System:Publishing updated data to MQTT")
        state.client.publish(topics.system_sensors(base), json.dumps(data, indent=2))


def graceful_exit(state: AppState) -> None:
    """Send offline states, disconnect MQTT and close the serial port."""
    try:
        client = state.client
        if client and client.is_connected():
            logger.info("Sending offline status to MQTT")
            client.publish(topics.availability(state.config.mqtt_topic), "offline", retain=True)
            for pack_state in state.packs:
                client.publish(topics.pack_availability(state.config.mqtt_topic, pack_state.address), "offline", retain=False)
            logger.info("Disconnecting MQTT client")
            client.disconnect()
            client.loop_stop()
        if state.bus is not None and state.bus.is_open:
            logger.info("Closing serial connection")
            state.bus.close()
        if state.store is not None:
            state.store.flush()
    except Exception as e:
        logger.error("Error during graceful exit: %s", e)


def poll_pack(state: AppState, pack_state: PackState, discovery: Discovery | None) -> None:
    """Poll one pack and publish its data, heartbeat and availability."""
    base = state.config.mqtt_topic
    address = pack_state.address
    data, success = pack_state.pack.read()
    now = time.time()

    if success:
        state.last_bms_update_ts = now
        pack_state.last_success_ts = now
        pack_state.publish_counter += 1
        heartbeat = {"last_publish": now_iso(), "publish_counter": pack_state.publish_counter}
        state.client.publish(topics.pack_heartbeat(base, address), json.dumps(heartbeat), retain=False)
        state.client.publish(topics.pack_availability(base, address), "online", retain=False)
        pack_state.availability = "online"

    if discovery and pack_state.pack.cell_count:
        discovery.update_cell_count(address, pack_state.pack.cell_count)
    # show the real firmware version in the HA device info once it's known
    if discovery and pack_state.pack.info and not pack_state.firmware_reported:
        discovery.update_firmware(address, device_firmware(pack_state.pack.info))
        pack_state.firmware_reported = True

    if data:
        logger.info("Pack%s:Publishing updated data to MQTT", address)
        state.client.publish(topics.pack_sensors(base, address), json.dumps(data, indent=2))
    elif success:
        logger.info("Pack%s:No changes detected", address)

    for other in state.packs:
        publish_pack_availability(state, other, now)
    state.client.publish(topics.availability(base), "online", retain=False)


def main(config: Config | None = None) -> None:
    config = config or Config.from_env()
    configure_logging(config)
    logger.info("Starting Seplos BMS Data Fetcher")
    logger.debug("Configuration loaded: %s", config.loggable())

    state = AppState(config=config)

    def on_signal(_signum, _frame) -> None:
        graceful_exit(state)
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        state.client = initialize_mqtt(state)
        state.bus = SerialBus(initialize_serial(config))

        state.store = StateStore.default(config.state_file)
        state.packs = [
            PackState(BatteryPack(address, state.bus, config, state.store), address) for address in range(config.number_of_packs)
        ]
        logger.info("Initialized %s battery pack(s)", config.number_of_packs)
        for pack_state in state.packs:
            state.client.publish(topics.pack_availability(config.mqtt_topic, pack_state.address), "offline", retain=False)

        # initial grace: healthy until the first successful poll updates the timestamp
        state.last_bms_update_ts = time.time()
        start_health_server(state)

        discovery = None
        if config.enable_ha_discovery:
            logger.info("Sending Home Assistant Auto-Discovery configurations")
            discovery = Discovery(state.client, config.mqtt_topic, config.ha_discovery_prefix, config.invert_ha_dis_charge_measurements)
            for pack_state in state.packs:
                discovery.publish_pack(pack_state.address, pack_bus=not config.multi_pack)
            if config.multi_pack:
                discovery.publish_system()
            else:
                discovery.remove_system()
            logger.info("Auto-Discovery configurations sent")

        index = 0
        system_reader = SystemReader()
        while True:
            try:
                poll_pack(state, state.packs[index], discovery)
                index += 1
                if index >= len(state.packs):
                    index = 0
                    publish_system_data(state, system_reader)
                    if config.mqtt_update_interval > 0:
                        logger.info("Waiting %s seconds before next cycle", config.mqtt_update_interval)
                        time.sleep(config.mqtt_update_interval)
            except Exception as e:
                logger.error("Error in main loop: %s", e)
                time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Shutdown requested via keyboard interrupt")
    except Exception as e:
        logger.error("Fatal error: %s", e)
    finally:
        graceful_exit(state)
