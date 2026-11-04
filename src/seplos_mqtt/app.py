"""Composition and main loop: one serial bus, round-robin pack polling, MQTT publishing."""
from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import serial
from serial.serialutil import SerialException

from . import health, timeutil, topics
from .config import Config, ConfigError
from .decoders import telemetry
from .decoders.identity import device_firmware
from .discovery import Discovery
from .mqtt_client import MqttConnection, Publisher
from .pack import BatteryPack
from .state import StateStore
from .system import SystemReader
from .transport import SerialBus

logger = logging.getLogger("SeplosBMS")


@dataclass
class PackState:
    pack: BatteryPack
    address: int
    # monotonic time of the last successful poll, None before the first one
    last_success: float | None = None
    publish_counter: int = 0
    availability: str = "offline"
    firmware_reported: int = 0          # info_revision of the pack shown in the HA device info


@dataclass
class AppState:
    config: Config
    mqtt: Any = None              # MqttConnection
    publisher: Publisher | None = None
    bus: SerialBus | None = None
    store: StateStore | None = None
    discovery: Discovery | None = None
    health_server: Any = None
    packs: list[PackState] = field(default_factory=list)
    # monotonic time of the last successful BMS poll (startup counts as grace period)
    last_poll: float = 0.0
    next_replay: float = 0.0
    cycle_start: float = 0.0          # monotonic start of the running polling cycle
    last_cycle: float = 0.0           # duration of the last complete cycle
    discovery_retry_at: float = 0.0

    def last_progress(self) -> float:
        """Latest successful core read (0x42/0x44) of any pack, also during a long first poll."""
        return max([self.last_poll, *(p.pack.core_success for p in self.packs if p.pack.core_success)])

    def max_age(self) -> float:
        return health.max_poll_age(self.config.number_of_packs, self.config.mqtt_update_interval, self.config.multi_pack)

    def is_healthy(self) -> bool:
        return health.is_healthy(
            mqtt_connected=self.mqtt is not None and self.mqtt.is_connected(),
            serial_open=self.bus is not None and self.bus.is_open,
            poll_age=time.monotonic() - self.last_progress(),
            max_age=self.max_age(),
        )


def configure_logging(config: Config) -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s:%(name)s:%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
    logger.setLevel(levels.get(config.logging_level, logging.INFO))


def initialize_mqtt(config: Config) -> MqttConnection:
    connection = MqttConnection(config, will_topic=topics.availability(config.mqtt_topic))
    try:
        connection.connect()
    except Exception as e:  # socket/DNS errors and Paho exceptions
        logger.error("MQTT connection to %s:%s failed: %s", config.mqtt_host, config.mqtt_port, e)
        sys.exit(1)
    return connection


def initialize_serial(config: Config) -> Any:
    try:
        logger.info("Initializing serial interface %s at %s baud", config.serial_interface, config.baudrate)
        return serial.Serial(port=config.serial_interface, baudrate=config.baudrate, timeout=0.5, write_timeout=2)
    except SerialException as e:
        logger.error("Serial initialization failed: %s", e)
        sys.exit(1)


def start_health_server(state: AppState) -> Any:
    server = health.start(state.is_healthy)
    if server is not None:
        logger.info("Health endpoint started on http://0.0.0.0:%s/health", health.PORT)
    return server


def publish_discovery(state: AppState) -> None:
    """All discovery configs (startup, reconnect, HA restart)."""
    discovery, config = state.discovery, state.config
    if discovery is None:
        return
    for pack_state in state.packs:
        discovery.publish_pack(pack_state.address, pack_bus=not config.multi_pack)
    if config.multi_pack:
        discovery.publish_system()
    else:
        discovery.remove_system()


# states are refreshed shortly after discovery (HA subscribes after processing the configs)
# and periodically, so late subscribers converge even when the values don't change
REPLAY_DELAY_SECONDS = 5
REPLAY_INTERVAL_SECONDS = 600


def resync(state: AppState) -> None:
    """After a reconnect or an HA restart: discovery, availability and the latest states again."""
    logger.info("Resending discovery configs and states")
    publish_discovery(state)
    now = time.monotonic()
    for pack_state in state.packs:
        pack_state.availability = "unknown"          # current (freshness-derived) value goes out again
        publish_pack_availability(state, pack_state, now)
    state.publisher.publish(topics.availability(state.config.mqtt_topic), "online")
    state.publisher.replay()
    state.next_replay = now + REPLAY_DELAY_SECONDS


DISCOVERY_RETRY_SECONDS = 60


def retry_discovery(state: AppState) -> None:
    """A discovery config/tombstone wasn't accepted by the client: reconcile everything again (bounded)."""
    discovery = state.discovery
    if discovery is None or not discovery.failed:
        return
    now = time.monotonic()
    if now >= state.discovery_retry_at:
        discovery.failed = False
        state.discovery_retry_at = now + DISCOVERY_RETRY_SECONDS
        resync(state)


def refresh_states(state: AppState) -> None:
    """Delayed/periodic replay of the latest states (non-retained)."""
    now = time.monotonic()
    if now >= state.next_replay:
        state.publisher.replay()
        state.next_replay = now + REPLAY_INTERVAL_SECONDS


def pack_max_age(state: AppState, now: float) -> float:
    """
    Per-pack freshness allowance: the nominal budget, but at least three observed full cycles and the
    running cycle (optional/startup reads of the other packs on the shared bus can make a cycle long).
    """
    running = now - state.cycle_start if state.cycle_start else 0.0
    return max(state.max_age(), 3 * state.last_cycle + 10, running + state.max_age())


def publish_pack_availability(state: AppState, pack_state: PackState, now: float) -> None:
    """Publish a changed availability; the change only counts as sent once the client accepted it."""
    online = pack_state.last_success is not None and now - pack_state.last_success <= pack_max_age(state, now)
    desired = "online" if online else "offline"
    if pack_state.availability != desired:
        if state.publisher.publish(topics.pack_availability(state.config.mqtt_topic, pack_state.address), desired):
            pack_state.availability = desired


def publish_system_data(state: AppState, reader: SystemReader) -> None:
    """Read and publish the system values (multi pack mode via the CAN port RS485 bus only)."""
    if not state.config.multi_pack or not state.packs:
        return
    data = reader.read(state.bus)
    if data is not None:
        # total full capacity (Ah, master) x series cells x configured nominal cell voltage
        cells = next((p.pack.cell_count for p in state.packs if p.pack.cell_count), None)
        data["normal"]["total_full_energy"] = telemetry.full_energy(
            data["normal"].get("total_capacity"), cells, state.config.nominal_cell_voltage)
    base = state.config.mqtt_topic
    state.publisher.publish(topics.system_availability(base), "online" if data is not None else "offline")
    if data is not None and state.publisher.state(topics.system_sensors(base), data):
        logger.info("System:Publishing updated data to MQTT")


def poll_pack(state: AppState, pack_state: PackState) -> None:
    """Poll one pack and publish its data, heartbeat and availability."""
    base, address, discovery = state.config.mqtt_topic, pack_state.address, state.discovery
    data, success = pack_state.pack.read()
    now = time.monotonic()

    if success:
        state.last_poll = now
        pack_state.last_success = now
        pack_state.publish_counter += 1
        heartbeat = {"last_publish": timeutil.now_iso(), "publish_counter": pack_state.publish_counter}
        state.publisher.publish(topics.pack_heartbeat(base, address), heartbeat)
        if state.publisher.publish(topics.pack_availability(base, address), "online"):
            pack_state.availability = "online"

    if discovery and pack_state.pack.capabilities_resolved:
        if discovery.set_capabilities(address, pack_state.pack.capabilities, pack_bus=not state.config.multi_pack):
            state.next_replay = now + REPLAY_DELAY_SECONDS
    if discovery and pack_state.pack.cell_count:
        discovery.update_cell_count(address, pack_state.pack.cell_count)
    # show the real firmware version in the HA device info once it's known
    if discovery and pack_state.pack.info and pack_state.firmware_reported != pack_state.pack.info_revision:
        discovery.update_firmware(address, device_firmware(pack_state.pack.info))
        pack_state.firmware_reported = pack_state.pack.info_revision

    if data is not None:
        if state.publisher.state(topics.pack_sensors(base, address), data):
            logger.info("Pack%s:Publishing updated data to MQTT", address)
        else:
            logger.debug("Pack%s:No changes detected", address)

    for other in state.packs:
        publish_pack_availability(state, other, now)
    state.publisher.publish(topics.availability(base), "online")


def graceful_exit(state: AppState) -> None:
    """Send offline states, then release every resource independently of the others."""
    base = state.config.mqtt_topic

    def step(name: str, action) -> None:
        try:
            action()
        except Exception as e:
            logger.error("Error during graceful exit (%s): %s", name, e)

    if state.mqtt is not None and state.mqtt.is_connected() and state.publisher is not None:
        logger.info("Sending offline status to MQTT")
        step("offline", lambda: state.publisher.publish(topics.availability(base), "offline", retain=True))
        for pack_state in state.packs:
            step("offline", lambda p=pack_state: state.publisher.publish(topics.pack_availability(base, p.address), "offline"))
        logger.info("Disconnecting MQTT client")
        step("mqtt", state.mqtt.stop)
    if state.bus is not None and state.bus.is_open:
        logger.info("Closing serial connection")
        step("serial", state.bus.close)
    if state.store is not None:
        step("state", state.store.flush)
    if state.health_server is not None:
        step("health", state.health_server.shutdown)


def _raise_exit(_signum, _frame) -> None:
    """SIGTERM/SIGINT: leave the main loop; cleanup runs once in main()'s finally."""
    raise SystemExit(0)


def main(config: Config | None = None) -> None:
    try:
        config = config or Config.from_env()
    except ConfigError as e:
        logging.basicConfig()
        logger.error("Invalid configuration: %s", e)
        sys.exit(1)
    configure_logging(config)
    logger.info("Starting Seplos BMS Data Fetcher")
    logger.debug("Configuration loaded: %s", config.loggable())

    state = AppState(config=config)
    signal.signal(signal.SIGTERM, _raise_exit)
    signal.signal(signal.SIGINT, _raise_exit)

    try:
        state.mqtt = initialize_mqtt(config)
        state.publisher = Publisher(state.mqtt.client)
        state.bus = SerialBus(initialize_serial(config))
        state.store = StateStore.default(config.state_file)
        state.packs = [
            PackState(BatteryPack(address, state.bus, config, state.store), address) for address in range(config.number_of_packs)
        ]
        logger.info("Initialized %s battery pack(s)", config.number_of_packs)
        for pack_state in state.packs:
            state.publisher.publish(topics.pack_availability(config.mqtt_topic, pack_state.address), "offline")

        # initial grace: healthy until the first poll had a chance to succeed
        state.last_poll = time.monotonic()
        state.health_server = start_health_server(state)

        if config.enable_ha_discovery:
            logger.info("Sending Home Assistant Auto-Discovery configurations")
            state.discovery = Discovery(
                state.publisher, config.mqtt_topic, config.ha_discovery_prefix, config.invert_ha_dis_charge_measurements,
                maint_enabled=config.maint_firmware,
            )
            publish_discovery(state)
            logger.info("Auto-Discovery configurations sent")

        index = 0
        system_reader = SystemReader()
        state.next_replay = time.monotonic() + REPLAY_DELAY_SECONDS
        state.cycle_start = time.monotonic()
        while True:
            try:
                if state.mqtt.resync.is_set():
                    state.mqtt.resync.clear()
                    resync(state)
                refresh_states(state)
                retry_discovery(state)
                poll_pack(state, state.packs[index])
                index += 1
                if index >= len(state.packs):
                    index = 0
                    publish_system_data(state, system_reader)
                    now = time.monotonic()
                    state.last_cycle, state.cycle_start = now - state.cycle_start, now
                    if config.mqtt_update_interval > 0:
                        logger.info("Waiting %s seconds before next cycle", config.mqtt_update_interval)
                        time.sleep(config.mqtt_update_interval)
            except Exception:
                logger.exception("Error in main loop")
                time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Shutdown requested via keyboard interrupt")
    finally:
        graceful_exit(state)
