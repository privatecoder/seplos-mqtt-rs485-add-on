"""Composition and main loop: one serial bus, round-robin pack polling, MQTT publishing."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import serial
from serial.serialutil import SerialException

from . import health, timeutil, topics
from .bridge import BridgeError, SocatBridge
from .config import Config, ConfigError
from .decoders import telemetry
from .decoders.identity import device_firmware
from .discovery import Discovery
from .jobs import JobQueue, step_detect
from .monitoring.migration import load_or_migrate
from .monitoring.model import ConfigValidationError, MonitoringConfig, activity
from .monitoring.store import JsonStore, LoadStatus
from .mqtt_client import MqttConnection, Publisher
from .pack import BatteryPack
from .sets import Member, set_values
from .state import StateStore
from .status import build_status
from .supervisor import clear_legacy_options
from .system import SystemReader
from .transport import SerialBus

logger = logging.getLogger("SeplosBMS")

MONITORING_FILE = "/data/monitoring.json"
# the launcher that run.sh executes; a saved connection change re-executes it in place
LAUNCHER = Path(__file__).resolve().parent.parent / "fetch_bms_data.py"
_execv = os.execv      # module-level name so tests can replace it


class RestartRequested(BaseException):
    """A saved connection change asks for a clean restart (not caught by ``except Exception``)."""


@dataclass
class PackState:
    pack: BatteryPack
    address: int
    # monotonic time of the last successful poll, None before the first one
    last_success: float | None = None
    publish_counter: int = 0
    availability: str = "offline"
    firmware_reported: int = 0          # info_revision of the pack shown in the HA device info
    last_data: dict | None = None       # data of the last successful poll (for the status snapshot)


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
    bridge: Any = None                # SocatBridge (remote gateway) or None
    web: Any = None                   # WebThread or None
    restart_event: threading.Event = field(default_factory=threading.Event)
    restart_reason: str | None = None
    bridge_dead_since: float | None = None   # monotonic time socat was first seen dead
    system_data: dict | None = None   # last system values (multi pack mode)
    status: dict | None = None        # snapshot for the web thread (replaced as a whole, never mutated)
    jobs: Any = None                  # JobQueue: submitted by the web thread, run by the main loop
    parallel_sets: tuple = ()         # the sets the loop works with (changed by the main loop only)
    # latest saved sets, replaced as a whole by the web thread; never cleared, the loop adopts it
    # when it is not the tuple in use (no lost update between reading and clearing)
    pending_sets: tuple | None = None
    set_values: dict[int, dict] = field(default_factory=dict)       # last computed values per active set id
    published_sets: dict[int, Any] = field(default_factory=dict)    # ParallelSet as published to HA, per id
    set_inventory: set[int] = field(default_factory=set)            # ids with state in HA (kept in the state file)
    connection_error: str | None = None   # setup mode: why the BMS connection could not be opened

    def request_sets(self, config: Any) -> None:
        """A saved set change (web thread): picked up at the next cycle boundary, no restart."""
        self.pending_sets = config.parallel_sets

    def adopt_sets(self) -> None:
        pending = self.pending_sets
        if pending is not None and pending is not self.parallel_sets:
            self.parallel_sets = pending
            logger.info("Parallel sets updated: %s set(s)", len(pending))

    def request_restart(self, _config: Any = None, reason: str = "connection") -> None:
        """A clean process restart is wanted ("connection": a saved change, called from the web
        thread; "bridge": socat is gone). The main loop notices it; the first reason is kept."""
        if self.restart_reason is None:
            self.restart_reason = reason
        self.restart_event.set()

    def last_progress(self) -> float:
        """Latest successful core read (0x42/0x44) of any pack, also during a long first poll."""
        return max([self.last_poll, *(p.pack.core_success for p in self.packs if p.pack.core_success)])

    def max_age(self) -> float:
        return health.max_poll_age(self.config.number_of_packs, self.config.mqtt_update_interval, self.config.multi_pack)

    def is_healthy(self) -> bool:
        if self.connection_error is not None:
            return True       # setup mode: nothing to poll, a watchdog restart would not help
        return health.is_healthy(
            mqtt_connected=self.mqtt is not None and self.mqtt.is_connected(),
            serial_open=self.bus is not None and self.bus.is_open,
            poll_age=time.monotonic() - self.last_progress(),
            max_age=self.max_age(),
        )


def setup_logging(level_name: str) -> None:
    """Format and level of every "SeplosBMS.*" logger (they inherit from "SeplosBMS")."""
    logging.basicConfig(format="%(asctime)s %(levelname)s:%(name)s:%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
    logger.setLevel(levels.get(level_name.upper(), logging.INFO))


def configure_logging(config: Config) -> None:
    setup_logging(config.logging_level)


def initialize_mqtt(config: Config) -> MqttConnection:
    connection = MqttConnection(config, will_topic=topics.availability(config.mqtt_topic))
    try:
        connection.connect()
    except Exception as e:  # socket/DNS errors and Paho exceptions
        logger.error("MQTT connection to %s:%s failed: %s", config.mqtt_host, config.mqtt_port, e)
        sys.exit(1)
    return connection


class ConnectionUnavailable(Exception):
    """The serial interface or the gateway bridge cannot be opened (the settings app can fix it)."""


def initialize_serial(config: Config) -> Any:
    try:
        logger.info("Initializing serial interface %s at %s baud", config.serial_interface, config.baudrate)
        return serial.Serial(port=config.serial_interface, baudrate=config.baudrate, timeout=0.5, write_timeout=2)
    except SerialException as e:
        raise ConnectionUnavailable(f"cannot open {config.serial_interface}: {e}") from e


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
    for item in state.published_sets.values():
        discovery.publish_set(item)


# states are refreshed shortly after discovery (HA subscribes after processing the configs)
# and periodically, so late subscribers converge even when the values don't change
REPLAY_DELAY_SECONDS = 5
REPLAY_INTERVAL_SECONDS = 600


def resync(state: AppState) -> None:
    """After a reconnect or an HA restart: discovery, availability and the latest states again."""
    logger.info("Resending discovery configs and states")
    publish_discovery(state)
    reconcile_sets(state)                  # removals that were not accepted before
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


def pack_online(state: AppState, pack_state: PackState, now: float) -> bool:
    return pack_state.last_success is not None and now - pack_state.last_success <= pack_max_age(state, now)


def publish_pack_availability(state: AppState, pack_state: PackState, now: float) -> None:
    """Publish a changed availability; the change only counts as sent once the client accepted it."""
    online = pack_online(state, pack_state, now)
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
        state.system_data = data
    base = state.config.mqtt_topic
    state.publisher.publish(topics.system_availability(base), "online" if data is not None else "offline")
    if data is not None and state.publisher.state(topics.system_sensors(base), data):
        logger.info("System:Publishing updated data to MQTT")


def active_sets(state: AppState) -> list:
    """The adopted sets that can work: multi pack mode and all members configured."""
    flags = activity(state.parallel_sets, state.config.number_of_packs)
    return [item for item, (ok, _reason) in zip(state.parallel_sets, flags, strict=True) if ok]


def set_members(state: AppState, item: Any, now: float) -> list[Member]:
    packs = {p.address: p for p in state.packs}
    members = []
    for address in item.packs:
        pack_state = packs.get(address)
        data = pack_state.last_data if pack_state is not None else None
        normal = data.get("telemetry", {}).get("normal") if isinstance(data, dict) else None
        online = pack_state is not None and pack_online(state, pack_state, now)
        age = now - pack_state.last_success if pack_state is not None and pack_state.last_success is not None else None
        members.append(Member(address, online and isinstance(normal, dict), age, normal))
    return members


def save_set_inventory(state: AppState) -> bool:
    """Write the inventory (also retries a write that failed before); True once it is on disk."""
    if state.store is None:
        return True
    return state.store.set_durable("published_sets", sorted(state.set_inventory))


def remove_set(state: AppState, set_id: int) -> bool:
    """A deleted or inactive set: its HA entities and states go away, availability is retained offline.
    True only if the client accepted every message; otherwise the caller keeps the id and retries."""
    base = state.config.mqtt_topic
    ok = state.discovery.remove_set(set_id) if state.discovery is not None else True
    state.publisher.forget(topics.set_sensors(base, set_id))
    state.publisher.forget(topics.set_availability(base, set_id))
    ok = state.publisher.publish(topics.set_availability(base, set_id), "offline", retain=True) and ok
    state.published_sets.pop(set_id, None)
    state.set_values.pop(set_id, None)
    return ok


def reconcile_sets(state: AppState) -> list:
    """Keep the inventory (ids that may still have retained data on the broker) in step: every id that
    is not an active set is removed again until the client accepted the cleanup (also a set deleted
    while the add-on was stopped, or any set in single pack mode). Returns the sets that may publish:
    the active ones whose id is durably recorded."""
    active = active_sets(state)
    state.set_inventory |= {item.id for item in active}
    recorded = save_set_inventory(state)             # before anything of a new set is published
    wanted = {item.id for item in active}
    for set_id in sorted(state.set_inventory - wanted):
        if remove_set(state, set_id):
            state.set_inventory.discard(set_id)
    save_set_inventory(state)                        # no-op unless a removal succeeded or the last write failed
    return active if recorded else [item for item in active if item.id in state.published_sets]


def publish_sets(state: AppState) -> None:
    """Values and HA devices of the active parallel sets (after a full poll cycle)."""
    base, now = state.config.mqtt_topic, time.monotonic()
    for item in reconcile_sets(state):
        if state.published_sets.get(item.id) != item:
            if state.discovery is not None:
                state.discovery.publish_set(item)
            state.published_sets[item.id] = item
            state.next_replay = now + REPLAY_DELAY_SECONDS
        values = set_values(set_members(state, item, now))
        state.set_values[item.id] = values
        payload = {
            "normal": values,
            "set": {"name": item.name, "packs": list(item.packs), "wiring": item.wiring,
                    "plus": item.plus, "minus": item.minus},
        }
        if state.publisher.state(topics.set_sensors(base, item.id), payload):
            logger.debug("Set %s:Publishing updated data to MQTT", item.id)
        state.publisher.state(topics.set_availability(base, item.id), "online")


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
        pack_state.last_data = data
        if state.publisher.state(topics.pack_sensors(base, address), data):
            logger.info("Pack%s:Publishing updated data to MQTT", address)
        else:
            logger.debug("Pack%s:No changes detected", address)

    for other in state.packs:
        publish_pack_availability(state, other, now)
    state.publisher.publish(topics.availability(base), "online")


def update_status(state: AppState) -> None:
    """Rebuild the snapshot the web thread reads; a defect here must never stop the polling."""
    try:
        state.status = build_status(state, time.monotonic(), timeutil.now_iso())
    except Exception:
        logger.exception("Could not build the status snapshot")


def run_job(state: AppState, job: Any) -> None:
    """One step of a job from the web thread, on the bus in the main loop thread (never during a
    poll). A job spreads over loop iterations; the core polling keeps going in between."""
    jobs = state.jobs
    try:
        if job.kind == "detect":
            if job.scan is None:
                logger.info("Running job %s (%s)", job.id, job.kind)
            if step_detect(state.bus, job, maint=state.config.maint_firmware):
                jobs.complete(job, job.scan.final, result=job.scan.result())
        else:
            jobs.complete(job, "failed", error="unknown_job_kind")
    except Exception:
        logger.exception("Job %s failed", job.id)
        jobs.complete(job, "failed", error="job_failed")


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


def wait_or_restart(state: AppState, seconds: float) -> bool:
    """Interruptible sleep: returns True as soon as a restart was requested."""
    return state.restart_event.wait(seconds)


SETUP_REFRESH_SECONDS = 5         # setup mode: how often the status snapshot is refreshed
BRIDGE_DEAD_RESTART_S = 30       # socat dead this long -> clean process restart
WEB_STOP_DEADLINE_S = 30.0        # how long main() waits for the web server to stop


def check_bridge(state: AppState, now: float) -> None:
    """Watch socat. It is never restarted in place (the open serial descriptor would still point at
    the old pty): after a persistent outage the whole process restarts and rebuilds everything."""
    if state.bridge is None:
        return
    if state.bridge.alive():
        if state.bridge_dead_since is not None:
            logger.info("RS485 gateway bridge (socat) is running again")
        state.bridge_dead_since = None
        return
    if state.bridge_dead_since is None:
        state.bridge_dead_since = now
        logger.warning("RS485 gateway bridge (socat) is not running; restarting in %s s unless it recovers", BRIDGE_DEAD_RESTART_S)
    elif now - state.bridge_dead_since >= BRIDGE_DEAD_RESTART_S:
        state.request_restart(reason="bridge")


def stop_web(web: Any, *, deadline_s: float | None = None, clock: Any = None) -> bool:
    """Stop the web thread and wait until it is really gone (a running save is durable and must
    finish, also its callback). Returns False if it was still running at the deadline."""
    deadline_s = WEB_STOP_DEADLINE_S if deadline_s is None else deadline_s
    clock = clock or time.monotonic
    started = clock()
    while True:
        if web.stop(timeout=5.0):
            return True
        if clock() - started >= deadline_s:
            return False
        logger.warning("Settings app is still shutting down, waiting")


def startup_config(env: Mapping[str, str]) -> tuple[Config, JsonStore | None]:
    """Config for this start: the saved monitoring document (created once from the add-on options).

    A corrupt document does not stop the polling: the add-on options are used and the app shows
    the problem. Invalid options exit with status 1 like before.
    """
    path = Path(env.get("MONITORING_FILE") or MONITORING_FILE)
    store: JsonStore | None = None
    try:
        try:
            mon, store = load_or_migrate(path, env)
        except OSError as e:
            logger.error("Cannot create %s (%s); using the add-on options without the settings app", path, e)
            return Config.from_env(env), None
        if mon is None:
            logger.error(
                "%s is not usable (%s): the settings app shows the problem; polling continues with the add-on options",
                path, store.problem,
            )
            return Config.from_env(env), store
        return Config.from_monitoring(mon, env), store
    except (ConfigError, ConfigValidationError) as e:
        logger.error("Invalid configuration: %s", e)
        sys.exit(1)


def wait_in_setup_mode(state: AppState) -> None:
    """No BMS connection: keep the app up, announce the add-on as offline (retried until the broker
    accepted it, and again after every reconnect) and wait for a saved connection."""
    topic = topics.availability(state.config.mqtt_topic)
    announced = False
    update_status(state)
    while True:
        if state.mqtt.resync.is_set():
            state.mqtt.resync.clear()
            announced = False
        if not announced:
            announced = state.publisher.publish(topic, "offline", retain=True)
        if wait_or_restart(state, SETUP_REFRESH_SECONDS):
            raise RestartRequested
        update_status(state)


def load_sets(store: JsonStore) -> tuple:
    """The stored parallel sets; none when the document is missing or corrupt (the add-on options
    are in use then)."""
    try:
        return MonitoringConfig.from_dict(store.snapshot()[3]).parallel_sets
    except ConfigValidationError:
        return ()


def start_web(state: AppState, store: JsonStore, port: str) -> None:
    """The Ingress app in its own thread; a failure only costs the UI, never the polling."""
    try:
        from .web.server import WebApp, WebSettings, WebThread

        web_app = WebApp(
            WebSettings(port=int(port)), store, on_connection_saved=state.request_restart,
            on_sets_saved=state.request_sets, status_provider=lambda: state.status,
            jobs=state.jobs if state.connection_error is None else None,     # setup mode: nothing runs jobs
        )
        web = WebThread(web_app)
        bound = web.start()
    except Exception as e:
        logger.error("Settings app could not be started: %s", e)
        return
    state.web = web
    logger.info("Settings app listening on port %s", bound)


def main(config: Config | None = None) -> None:
    store: JsonStore | None = None
    standalone = config is None       # started by run.sh (a given config = tests: no bridge, no web)
    if config is None:
        # logging first: the migration and the startup errors are logged before the config exists
        setup_logging(os.environ.get("LOGGING_LEVEL") or "info")
        config, store = startup_config(os.environ)
        token = os.environ.get("SUPERVISOR_TOKEN")
        if token and store is not None and store.status is LoadStatus.VALID:
            # best effort in the background: the Supervisor must never delay the start
            threading.Thread(target=clear_legacy_options, args=(token,), name="legacy-options", daemon=True).start()
    configure_logging(config)
    logger.info("Starting Seplos BMS Data Fetcher")
    logger.debug("Configuration loaded: %s", config.loggable())

    state = AppState(config=config)
    state.jobs = JobQueue()
    signal.signal(signal.SIGTERM, _raise_exit)
    signal.signal(signal.SIGINT, _raise_exit)

    restart = False
    try:
        state.mqtt = initialize_mqtt(config)
        state.publisher = Publisher(state.mqtt.client)
        try:
            if standalone and config.remote_host:
                try:
                    bridge = SocatBridge(config.remote_host, config.remote_port, config.serial_interface)
                    bridge.start()
                except BridgeError as e:
                    raise ConnectionUnavailable(f"gateway bridge failed: {e}") from e
                state.bridge = bridge
            state.bus = SerialBus(initialize_serial(config))
        except ConnectionUnavailable as e:
            state.connection_error = str(e)
            logger.error("No connection to the BMS: %s. Waiting for a saved connection in the settings app", e)
        if state.connection_error is None:
            state.store = StateStore.default(config.state_file)
            state.packs = [
                PackState(BatteryPack(address, state.bus, config, state.store), address)
                for address in range(config.number_of_packs)
            ]
            logger.info("Initialized %s battery pack(s)", config.number_of_packs)
            for pack_state in state.packs:
                state.publisher.publish(topics.pack_availability(config.mqtt_topic, pack_state.address), "offline")

        # initial grace: healthy until the first poll had a chance to succeed
        state.last_poll = time.monotonic()
        state.health_server = start_health_server(state)

        if standalone and store is not None:
            state.parallel_sets = load_sets(store)

        ingress_port = os.environ.get("INGRESS_PORT")
        if standalone and ingress_port and store is not None:
            start_web(state, store, ingress_port)

        if state.connection_error is not None:
            wait_in_setup_mode(state)

        if config.enable_ha_discovery:
            logger.info("Sending Home Assistant Auto-Discovery configurations")
            state.discovery = Discovery(
                state.publisher, config.mqtt_topic, config.ha_discovery_prefix, config.invert_ha_dis_charge_measurements,
                maint_enabled=config.maint_firmware,
            )
            publish_discovery(state)
            logger.info("Auto-Discovery configurations sent")
        state.set_inventory = {i for i in state.store.get("published_sets", []) if isinstance(i, int)}
        reconcile_sets(state)          # sets deleted while the add-on was stopped, or single pack mode

        index = 0
        system_reader = SystemReader()
        state.next_replay = time.monotonic() + REPLAY_DELAY_SECONDS
        state.cycle_start = time.monotonic()
        while True:
            try:
                check_bridge(state, time.monotonic())
                if state.restart_event.is_set():
                    raise RestartRequested
                if state.mqtt.resync.is_set():
                    state.mqtt.resync.clear()
                    resync(state)
                refresh_states(state)
                retry_discovery(state)
                job = state.jobs.current()
                if job is not None:
                    run_job(state, job)       # at most one probe per iteration, same thread as the polling
                    if state.restart_event.is_set():
                        raise RestartRequested    # no further probe and no further poll
                poll_pack(state, state.packs[index])
                update_status(state)
                index += 1
                if index >= len(state.packs):
                    index = 0
                    publish_system_data(state, system_reader)
                    state.adopt_sets()
                    publish_sets(state)
                    now = time.monotonic()
                    state.last_cycle, state.cycle_start = now - state.cycle_start, now
                    update_status(state)
                    if config.mqtt_update_interval > 0:
                        logger.info("Waiting %s seconds before next cycle", config.mqtt_update_interval)
                        if wait_or_restart(state, config.mqtt_update_interval):
                            raise RestartRequested
            except Exception:
                logger.exception("Error in main loop")
                wait_or_restart(state, 10)
    except KeyboardInterrupt:
        logger.info("Shutdown requested via keyboard interrupt")
    except RestartRequested:
        if state.restart_reason == "bridge":
            logger.warning("socat bridge exited, restarting")
        else:
            logger.info("Connection changed, restarting")
        restart = True
    finally:
        # each step on its own: one failure never skips the next
        web_stopped = True
        if state.web is not None:
            # first: no new saves, running ones finish (their callbacks only set the restart event)
            try:
                web_stopped = stop_web(state.web)
            except Exception as e:
                logger.error("Stopping the settings app failed: %s", e)
                web_stopped = False
        # an interrupted job keeps its partial result (the web server no longer accepts new ones)
        state.jobs.abort_active("restarting" if restart else "shutting_down")
        try:
            graceful_exit(state)
        except Exception as e:
            logger.error("Error during graceful exit: %s", e)
        if state.bridge is not None:
            try:
                state.bridge.stop()
            except Exception as e:
                logger.error("Stopping the bridge failed: %s", e)
    if restart:
        if not web_stopped:
            logger.error("web server did not stop, exiting instead of restarting")
            sys.exit(1)
        _execv(sys.executable, [sys.executable, "-u", str(LAUNCHER)])
