"""
Seplos V2 / V16 BMS Data Fetcher
Reads one or more Seplos protocol v2.0 BMS (in parallel) via
(remote) serial connection(s) and publishes their data to MQTT
"""
import sys
import os
import signal
import logging
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
import json
from typing import Optional, Dict, Any, Union, List, Callable, Tuple
import serial
from serial.serialutil import SerialException
import paho.mqtt.client as mqtt
from paho.mqtt import MQTTException
from ha_auto_discovery import AutoDiscoveryConfig

# Type aliases for clarity
ConfigValue = Union[int, float, bool, str, None]
BatteryData = Dict[str, Any]

# State container for shared application state
class AppState:
    """Container for globally shared runtime objects."""
    def __init__(self):
        self.mqtt_client: Optional[mqtt.Client] = None
        self.serial_instance: Optional[serial.Serial] = None
        self.battery_packs: List[Dict[str, Any]] = []

app_state = AppState()

logger: Optional[logging.Logger] = None

# --- Health/Watchdog state ---
# Timestamp of last successful BMS poll (seconds since epoch).
last_bms_update_ts: float = 0.0
# MQTT connection state as seen by callbacks.
mqtt_connected: bool = False


def _serial_is_open() -> bool:
    """Compatibility wrapper across pyserial versions."""
    s = app_state.serial_instance
    if not s:
        return False
    # Newer pyserial: property
    if hasattr(s, "is_open"):
        return bool(getattr(s, "is_open"))
    # Older pyserial: method
    if hasattr(s, "isOpen"):
        try:
            return bool(s.isOpen())
        except Exception:
            return False
    return False


def _mqtt_loop_running() -> bool:
    """Best-effort check whether Paho's network loop thread is alive."""
    c = app_state.mqtt_client
    if not c:
        return False
    t = getattr(c, "_thread", None)
    return bool(t and getattr(t, "is_alive", lambda: False)())


def _compute_max_age_seconds() -> int:
    """Derive an acceptable age for the last BMS poll from the polling cadence."""
    # The main loop sleeps 1s after each pack poll, plus an optional pause after a full cycle.
    packs = len(app_state.battery_packs) or int(getattr(Config, "NUMBER_OF_PACKS", 1) or 1)
    per_pack_delay = 1
    cycle_pause = int(getattr(Config, "MQTT_UPDATE_INTERVAL", 0) or 0)
    expected_cycle = packs * per_pack_delay + max(cycle_pause, 0)
    # Add a little slack for serial hiccups and startup.
    return max(15, int(expected_cycle * 3 + 5))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return

        now = time.time()
        max_age = _compute_max_age_seconds()

        healthy = (
            mqtt_connected
            and _mqtt_loop_running()
            and _serial_is_open()
            and (now - last_bms_update_ts) < max_age
        )

        if healthy:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"unhealthy")

    def log_message(self, format, *args):
        # Keep addon logs clean
        return


def _start_health_server() -> None:
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    server.serve_forever()


def graceful_exit(signum: Optional[int] = None, _frame: Optional[Any] = None) -> None:
    """Handle script exit to disconnect MQTT gracefully and cleanup."""

    try:
        # Close MQTT client if connected
        if app_state.mqtt_client and app_state.mqtt_client.is_connected():
            if logger:
                logger.info("Sending offline status to MQTT")
            app_state.mqtt_client.publish(f"{os.getenv('MQTT_TOPIC', 'seplos')}/availability", "offline", retain=True)
            for pack in app_state.battery_packs:
                app_state.mqtt_client.publish(_pack_availability_topic(pack["address"]), "offline", retain=False)
            if logger:
                logger.info("Disconnecting MQTT client")
            app_state.mqtt_client.disconnect()
            app_state.mqtt_client.loop_stop()

        # Close serial connection if open
        if _serial_is_open():
            if logger:
                logger.info("Closing serial connection")
            app_state.serial_instance.close()
    except Exception as e:
        if logger:
            logger.error("Error during graceful exit: %s", e)

    if signum is not None:
        sys.exit(0)


# Register signal handler for SIGTERM
signal.signal(signal.SIGTERM, graceful_exit)
signal.signal(signal.SIGINT, graceful_exit)


def get_env_value(var_name: str, default: Any = None, return_type: type = str) -> ConfigValue:
    """
    Get configuration value from environment variable with type casting.

    Args:
        var_name: Environment variable name
        default: Default value if not set
        return_type: Target type for casting (int, float, bool, str)

    Returns:
        Casted value or default
    """
    value = os.getenv(var_name, default)

    if value is None or value == "":
        return default

    try:
        if return_type == int:
            return int(value)
        elif return_type == float:
            return float(value)
        elif return_type == bool:
            if isinstance(value, bool):
                return value
            return str(value).lower() in ['true', '1', 'yes', 'on']
        else:
            return str(value)
    except (ValueError, TypeError):
        return default


# Configuration from environment variables with defaults
class Config:
    """Configuration class holding all settings from environment variables."""

    # BMS Configuration
    MIN_CELL_VOLTAGE = get_env_value("MIN_CELL_VOLTAGE", 2.500, float)
    MAX_CELL_VOLTAGE = get_env_value("MAX_CELL_VOLTAGE", 3.650, float)
    NUMBER_OF_PACKS = get_env_value("NUMBER_OF_PACKS", 1, int)

    # Serial Configuration
    SERIAL_INTERFACE = get_env_value("SERIAL_INTERFACE", "/tmp/vcom0", str)

    # MQTT Configuration
    MQTT_HOST = get_env_value("MQTT_HOST", "192.168.1.100", str)
    MQTT_PORT = get_env_value("MQTT_PORT", 1883, int)
    MQTT_USERNAME = get_env_value("MQTT_USERNAME", "seplos-mqtt", str)
    MQTT_PASSWORD = get_env_value("MQTT_PASSWORD", "", str)
    MQTT_TOPIC = get_env_value("MQTT_TOPIC", "seplos", str)
    MQTT_UPDATE_INTERVAL = get_env_value("MQTT_UPDATE_INTERVAL", 0, int)

    # Home Assistant Discovery
    ENABLE_HA_DISCOVERY_CONFIG = get_env_value("ENABLE_HA_DISCOVERY_CONFIG", True, bool)
    HA_DISCOVERY_PREFIX = get_env_value("HA_DISCOVERY_PREFIX", "homeassistant", str)
    INVERT_HA_DIS_CHARGE_MEASUREMENTS = get_env_value("INVERT_HA_DIS_CHARGE_MEASUREMENTS", True, bool)

    # Logging
    LOGGING_LEVEL = get_env_value("LOGGING_LEVEL", "info", str).upper()


# Logging setup
logging.basicConfig(
    format='%(asctime)s %(levelname)s:%(name)s:%(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("SeplosBMS")

# Set log level based on configuration
log_levels = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR
}
logger.setLevel(log_levels.get(Config.LOGGING_LEVEL, logging.INFO))

# Log configuration on startup
logger.info("Starting Seplos BMS Data Fetcher")
logger.debug("Configuration loaded: %s", vars(Config))


def is_can_port_rs485() -> bool:
    """
    The add-on reads multiple packs via the RS485 bus on the CAN port (UART1, 9600 baud),
    a single pack via RS485-1/2 (UART0, 19200 baud), see initialize_serial().
    """
    return Config.NUMBER_OF_PACKS > 1


def _pack_availability_topic(pack_no: int) -> str:
    return f"{Config.MQTT_TOPIC}/pack-{pack_no}/availability"


def _pack_heartbeat_topic(pack_no: int) -> str:
    return f"{Config.MQTT_TOPIC}/pack-{pack_no}/heartbeat"


def _publish_pack_availability(pack_state: Dict[str, Any], now: float) -> None:
    if not app_state.mqtt_client:
        return

    max_age = _compute_max_age_seconds()
    last_success = pack_state.get("last_success_ts", 0.0)
    is_online = last_success > 0 and (now - last_success) <= max_age
    desired = "online" if is_online else "offline"

    if pack_state.get("availability") != desired:
        topic = _pack_availability_topic(pack_state["address"])
        app_state.mqtt_client.publish(topic, desired, retain=False)
        pack_state["availability"] = desired


class Telemetry:
    """Holds numeric states for different sensors."""

    def __init__(self):
        # From pack
        self.voltage_cell: List[Optional[float]] = [None] * 16
        self.cell_temperature: List[Optional[float]] = [None] * 4
        self.ambient_temperature: Optional[float] = None
        self.components_temperature: Optional[float] = None
        self.dis_charge_current: Optional[float] = None
        self.total_pack_voltage: Optional[float] = None
        self.residual_capacity: Optional[float] = None
        self.full_capacity: Optional[float] = None
        self.state_of_charge: Optional[float] = None
        self.rated_capacity: Optional[float] = None
        self.charging_cycles: Optional[int] = None
        self.port_voltage: Optional[float] = None
        self.current_offset: Optional[int] = None
        self.current_idle: Optional[float] = None
        self.energy_charged: Optional[float] = None
        self.energy_discharged: Optional[float] = None
        self.current: Optional[float] = None

        # From user settings
        self.cell_voltage_setting_min: Optional[float] = None
        self.cell_voltage_setting_max: Optional[float] = None

        # Calculated
        self.average_cell_voltage: Optional[float] = None
        self.delta_cell_voltage: Optional[float] = None
        self.lowest_cell: Optional[int] = None
        self.lowest_cell_voltage: Optional[float] = None
        self.highest_cell: Optional[int] = None
        self.highest_cell_voltage: Optional[float] = None
        self.pack_voltage_setting_min: Optional[float] = None
        self.pack_voltage_setting_max: Optional[float] = None
        self.delta_cell_temperature: Optional[float] = None
        self.dis_charge_power: Optional[float] = None


class Telesignalization:
    """Holds warning, protection, lockout, normal, on and off states for different alarms."""

    def __init__(self):
        # 24 byte alarms
        self.cell_voltage_alarm: List[Optional[str]] = [None] * 16
        self.cell_temperature_alarm: List[Optional[str]] = [None] * 4
        self.ambient_temperature_alarm: Optional[str] = None
        self.component_temperature_alarm: Optional[str] = None
        self.dis_charging_current_alarm: Optional[str] = None
        self.pack_voltage_alarm: Optional[str] = None

        # 20 bit alarms (grouped for clarity)
        self.alarms = {
            # Warning 1 - System failures
            'voltage_sensing_failure': None,
            'temperature_sensing_failure': None,
            'current_sensing_failure': None,
            'power_switch_failure': None,
            'cell_voltage_difference_fault': None,
            'charging_switch_failure': None,
            'discharging_switch_failure': None,
            'current_limit_switch_failure': None,

            # Warning 2 - Voltage issues
            'cell_overvoltage': None,
            'cell_voltage_low': None,
            'pack_overvoltage': None,
            'pack_voltage_low': None,

            # Warning 3 - Temperature issues
            'charging_temperature_high': None,
            'charging_temperature_low': None,
            'discharging_temperature_high': None,
            'discharging_temperature_low': None,

            # Warning 4 - Ambient temperature
            'ambient_temperature_high': None,
            'ambient_temperature_low': None,
            'component_temperature_high': None,
            'low_temperature_heating': None,

            # Warning 5 - Current issues
            'charging_overcurrent': None,
            'discharging_overcurrent': None,
            'transient_overcurrent': None,
            'output_short_circuit': None,

            # Warning 6 - Miscellaneous
            'charging_high_voltage_protection': None,
            'intermittent_power_supplement': None,
            'soc_low': None,
            'cell_low_voltage_forbidden_charging': None,
            'output_reverse_polarity_protection': None,
            'output_connection_failure': None,

            # Warning 7 - Charging wait
            'auto_charging_wait': None,
            'manual_charging_wait': None,

            # Warning 8 - System errors
            'eep_storage_failure': None,
            'rtc_clock_failure': None,
            'no_calibration_of_voltage': None,
            'no_calibration_of_current': None,
            'no_calibration_of_null_point': None
        }

        # Initialize warning attributes for backward compatibility
        for key in self.alarms:
            setattr(self, key, None)

        # Switch status
        self.discharge_switch: Optional[str] = None
        self.charge_switch: Optional[str] = None
        self.current_limit_switch: Optional[str] = None
        self.heating_switch: Optional[str] = None

        # Passive balancing status
        self.balancer_cell: List[Optional[str]] = [None] * 16

        # System status
        self.system_status: Optional[str] = None


class SeplosBatteryPack:
    """Handles all methods for fetching, validating and parsing BMS data."""

    FRAME_READ_RETRIES = 5
    FRAME_OVERHEAD_LENGTH = 18
    # Give up on the (static) device info after this many polls without success
    INFO_MAX_ATTEMPTS = 3
    PARAMETER_REFRESH_SECONDS = 3600
    STATUS_MAP_24_BYTE_ALARM = {
        0: "OK",
        1: "Alarm (low)",
        2: "Alarm (high)"
    }
    STATUS_MAP_20_BIT_ALARM = {
        "on_off": ("ON", "OFF"),
        "fault_normal": ("Fault", "OK"),
        "warning_normal": ("Warning", "OK"),
        "protection_normal": ("Protection", "OK"),
    }

    def __init__(self, pack_address: int):
        self.pack_address = pack_address
        self.last_status: Optional[BatteryData] = None
        self.info: Optional[Dict[str, Any]] = None
        self.info_attempts = 0
        self.parameters: Optional[Dict[str, Any]] = None
        self.parameters_ts = 0.0
        self.extended_status: Optional[Dict[str, Any]] = None
        self.last_energy: Dict[str, float] = {}
        self.energy_overflow_logged = False
        self.telemetry = Telemetry()
        self.telesignalization = Telesignalization()

    @staticmethod
    def calculate_frame_checksum(frame: bytes) -> int:
        """Calculate frame checksum."""
        # sum of all characters modulo 65536, inverted, plus 1 (two's complement)
        return (-sum(frame)) & 0xFFFF

    @staticmethod
    def is_valid_hex_string(data: bytes) -> bool:
        """Check if given ASCII data is valid hex."""
        try:
            bytes.fromhex(data.decode("ascii"))
            logger.debug("Frame has hex only: OK")
            return True
        except (ValueError, UnicodeDecodeError):
            logger.debug("Frame includes non-hexadecimal characters: %s", data)
            return False

    @staticmethod
    def is_valid_length(data: bytes, expected_length: int) -> bool:
        """Check if given data matches expected length."""
        actual_length = len(data)
        if actual_length != expected_length:
            logger.debug(
                "Frame length mismatch - expected: %s, got: %s",
                expected_length,
                actual_length
            )
            return False
        logger.debug("Frame length OK: %s", expected_length)
        return True

    @staticmethod
    def int_from_1byte_hex_ascii(data: bytes, offset: int, signed: bool = False) -> int:
        """Return (signed) int value from 1 byte ASCII hex data."""
        return int.from_bytes(
            bytes.fromhex(data[offset:offset + 2].decode("ascii")),
            byteorder="big",
            signed=signed
        )

    @staticmethod
    def int_from_2byte_hex_ascii(data: bytes, offset: int, signed: bool = False) -> int:
        """Return (signed) int value from 2 byte ASCII hex data."""
        return int.from_bytes(
            bytes.fromhex(data[offset:offset + 4].decode("ascii")),
            byteorder="big",
            signed=signed
        )

    @staticmethod
    def status_from_24_byte_alarm(data: bytes, offset: int) -> str:
        """Return status string from 24 byte alarm data."""
        alarm_type = bytes.fromhex(data.decode("ascii"))[offset]
        return SeplosBatteryPack.STATUS_MAP_24_BYTE_ALARM.get(alarm_type, "Alarm (other)")

    @staticmethod
    def status_from_20_bit_alarm(
        data: bytes,
        offset: int,
        mode: str,
        first_bit: int,
        second_bit: Optional[int] = None
    ) -> str:
        """Return a status string based on 20-bit alarm data."""

        # Decode hex data into a byte value
        data_byte = bytes.fromhex(data.decode("ascii"))[offset]

        # helper
        def bit_set(bit: int) -> bool:
            return bool(data_byte & (1 << bit))

        # one bit mode
        if mode in SeplosBatteryPack.STATUS_MAP_20_BIT_ALARM:
            active, inactive = SeplosBatteryPack.STATUS_MAP_20_BIT_ALARM[mode]
            return active if bit_set(first_bit) else inactive

        # two bit mode: the more severe state (second_bit) takes precedence, as the
        # alarm bit usually stays set while the protection/lockout is active
        if mode == "protection_alarm_normal":
            if second_bit is not None and bit_set(second_bit):
                return "Protection"
            if bit_set(first_bit):
                return "Alarm"
            return "OK"

        if mode == "lockout_protection_normal":
            if second_bit is not None and bit_set(second_bit):
                return "Lockout"
            if bit_set(first_bit):
                return "Protection"
            return "OK"

        return "unknown"

    def is_valid_frame(self, data: bytes) -> bool:
        """
        Check validity of frame: length, checksum and error flag.
        - Checksum must be valid
        - cid2 must be 00 (no error)
        """
        try:
            # Check frame checksum
            chksum = self.calculate_frame_checksum(data[1:-5])
            expected = self.int_from_2byte_hex_ascii(data, -5)
            if chksum != expected:
                logger.debug(
                    "Frame checksum mismatch - got: %s, expected: %s",
                    chksum,
                    expected
                )
                return False
            logger.debug("Frame checksum OK: %s", chksum)

            # Check frame cid2 flag
            cid2 = data[7:9]
            if cid2 != b"00":
                logger.debug(
                    "Frame error flag (cid2) set - expected b'00', got: %s",
                    cid2
                )
                return False
            logger.debug("Frame error flag OK: %s", cid2)

            return True

        except (UnicodeDecodeError, ValueError) as e:
            logger.debug("Frame validation error: %s", e)
            return False

    @staticmethod
    def get_info_length(info: bytes) -> int:
        """Calculate info length with checksum."""
        lenid = len(info)
        if lenid == 0:
            return 0

        lchksum = (lenid & 0xF) + ((lenid >> 4) & 0xF) + ((lenid >> 8) & 0xF)
        lchksum %= 16
        lchksum ^= 0xF
        lchksum += 1

        return (lchksum << 12) + lenid

    def encode_cmd(self, address: int, cid2: int, info: bytes = b"01") -> bytes:
        """Encode command for battery pack using its address."""
        cid1 = 0x46
        info_length = self.get_info_length(info)
        frame = f"{0x20:02X}{address:02X}{cid1:02X}{cid2:02X}{info_length:04X}".encode()
        frame += info
        checksum = self.calculate_frame_checksum(frame)
        return b"~" + frame + f"{checksum:04X}".encode() + b"\r"

    def get_lowest_cell(self) -> Dict[str, Any]:
        """Get lowest cell number and voltage."""
        valid_cells = [v for v in self.telemetry.voltage_cell if v is not None]
        if not valid_cells:
            return {"lowest_cell": 0, "lowest_cell_voltage": 0}

        lowest_voltage = min(valid_cells)
        lowest_cell = self.telemetry.voltage_cell.index(lowest_voltage)
        return {"lowest_cell": lowest_cell, "lowest_cell_voltage": lowest_voltage}

    def get_highest_cell(self) -> Dict[str, Any]:
        """Get highest cell number and voltage."""
        valid_cells = [v for v in self.telemetry.voltage_cell if v is not None]
        if not valid_cells:
            return {"highest_cell": 0, "highest_cell_voltage": 0}

        highest_voltage = max(valid_cells)
        highest_cell = self.telemetry.voltage_cell.index(highest_voltage)
        return {"highest_cell": highest_cell, "highest_cell_voltage": highest_voltage}

    def decode_telemetry_feedback_frame(self, data: bytes) -> Dict[str, Any]:
        """Decode battery pack telemetry feedback frame."""
        telemetry_feedback = {"normal": {}}
        feedback = telemetry_feedback["normal"]

        # Number of cells
        number_of_cells = self.int_from_1byte_hex_ascii(data, offset=4)

        # Static values from configs

        ## calculate min/max cell and pack voltages
        self.telemetry.cell_voltage_setting_min = Config.MIN_CELL_VOLTAGE
        self.telemetry.cell_voltage_setting_max = Config.MAX_CELL_VOLTAGE
        self.telemetry.pack_voltage_setting_min = Config.MIN_CELL_VOLTAGE * number_of_cells
        self.telemetry.pack_voltage_setting_max = Config.MAX_CELL_VOLTAGE * number_of_cells

        ### Add to telemetry_feedback
        feedback.update({
            "cell_voltage_setting_min": self.telemetry.cell_voltage_setting_min,
            "cell_voltage_setting_max": self.telemetry.cell_voltage_setting_max,
            "pack_voltage_setting_min": self.telemetry.pack_voltage_setting_min,
            "pack_voltage_setting_max": self.telemetry.pack_voltage_setting_max
        })

        # Dynamic values from the BMS

        telemetry_fields = {
            'voltage_cell':             { 'offset': 6,   'scale': 1/1000, 'round': 3, 'amount': number_of_cells },  # doc: "Voltage of cell 1..M" (0x42)
            'cell_temperature':         { 'offset': 72,  'scale': 1/10, 'round': 1,  'bias': -2731, 'amount': 4 },  # doc: "Cell temperature 1..4" (0x42)
            'ambient_temperature':      { 'offset': 88,  'scale': 1/10, 'round': 1,  'bias': -2731 },  # doc: "Environment temperature" (0x42)
            'components_temperature':   { 'offset': 92,  'scale': 1/10, 'round': 1,  'bias': -2731 },  # doc: "Power temperature" (0x42), MOSFET/power stage
            'dis_charge_current':       { 'offset': 96,  'scale': 1/100, 'round': 2, 'signed': True },  # doc: "Charge/discharge current" (0x42), 0 when idle
            'total_pack_voltage':       { 'offset': 100, 'scale': 1/100, 'round': 2 },  # doc: "Total battery voltage" (0x42)
            'residual_capacity':        { 'offset': 104, 'scale': 1/100, 'round': 2 },  # doc: "Residual capacity" (0x42)
            'full_capacity':         { 'offset': 110, 'scale': 1/100, 'round': 1 },  # doc: "Battery capacity" (0x42), learned full capacity
            'state_of_charge':          { 'offset': 114, 'scale': 1/10, 'round': 1 },  # doc: "SOC" (0x42)
            'rated_capacity':           { 'offset': 118, 'scale': 1/100, 'round': 1 },  # doc: "Rated capacity" (0x42), setting P58
            'charging_cycles':          { 'offset': 122 },  # doc: "Number of cycles" (0x42)
            # offset 126: SOH - not evaluated, the firmware always sends 1000 (100.0 %)
            'port_voltage':             { 'offset': 130, 'scale': 1/100, 'round': 2 },  # doc: "Port voltage" (0x42)
            # The four "Reservation" fields of the doc are filled by the firmware:
            # 1: reference/offset channel of the current measurement in mA (used for drift compensation)
            # 2: offset corrected current in mA, only sent while idle (0 while charging/discharging)
            # 3/4: charged/discharged energy in 0.1 kWh (I*U summed every second while charging/
            #      discharging); the firmware sends 6500 once the counter reaches 65000
            'current_offset':           { 'offset': 134, 'signed': True },  # doc: "Reservation" 1 (0x42), mA
            'current_idle':             { 'offset': 138, 'signed': True, 'scale': 1/1000, 'round': 3 },  # doc: "Reservation" 2 (0x42)
            'energy_charged':           { 'offset': 142, 'scale': 1/10, 'round': 1 },  # doc: "Reservation" 3 (0x42), kWh
            'energy_discharged':        { 'offset': 146, 'scale': 1/10, 'round': 1 }  # doc: "Reservation" 4 (0x42), kWh
        }

        ## Fetch values for all telemetry fields
        for attr, cfg in telemetry_fields.items():
            offset = cfg["offset"]
            scale = cfg.get("scale", 1)
            rounding = cfg.get("round", None)
            bias = cfg.get("bias", 0)
            signed = cfg.get("signed", False)
            amount = cfg.get("amount", 1)

            if amount > 1:
                for i in range(amount):
                    raw = self.int_from_2byte_hex_ascii(
                        data,
                        offset + i * 4,
                        signed=signed
                    )
                    value = (raw + bias) * scale
                    if rounding is not None:
                        value = round(value, rounding)
                    getattr(self.telemetry, attr)[i] = value

                    ### Add to telemetry_feedback
                    feedback[f"{attr}_{i + 1}"] = value
            else:
                raw = self.int_from_2byte_hex_ascii(
                    data,
                    offset,
                    signed=signed
                )
                value = (raw + bias) * scale
                if rounding is not None:
                    value = round(value, rounding)
                setattr(self.telemetry, attr, value)

                ### Add to telemetry_feedback
                feedback[attr] = value

        # Current: 0x42 reports 0 while idle, the idle current (1 mA resolution) comes in "Reservation" 2
        current = self.telemetry.dis_charge_current if self.telemetry.dis_charge_current else self.telemetry.current_idle
        self.telemetry.current = current
        feedback["current"] = current
        feedback.pop("current_idle", None)

        # Energy counters: the firmware sends 6500 (650.0 kWh) once a counter reaches 65000 (6500 kWh).
        # Keep the last valid value instead of publishing a false reset.
        for attr in ("energy_charged", "energy_discharged"):
            value = feedback[attr]
            last = self.last_energy.get(attr)
            if value == 650.0 and last is not None and last > 650.0:
                if not self.energy_overflow_logged:
                    logger.warning("Pack%s:Energy counter exceeded 6500 kWh, the BMS no longer reports it", self.pack_address)
                    self.energy_overflow_logged = True
                value = last
            self.last_energy[attr] = value
            setattr(self.telemetry, attr, value)
            feedback[attr] = value

        # Calculated values

        # Get values from previous readings
        total_pack_voltage  = self.telemetry.total_pack_voltage
        cell_voltages       = self.telemetry.voltage_cell
        cell_temps          = self.telemetry.cell_temperature

        ## Dis-/charge power
        # uses the combined current, so the power isn't 0 while idle
        dis_charge_power = round((self.telemetry.current or 0) * total_pack_voltage, 2)
        self.telemetry.dis_charge_power = dis_charge_power

        ## Average cell voltage
        avg_voltage = round(sum(cell_voltages) / len(cell_voltages), 3)
        self.telemetry.average_cell_voltage = avg_voltage

        ## Highest/lowest cell and voltage
        lowest_idx, lowest_voltage = min(
            enumerate(cell_voltages), key=lambda x: x[1]
        )
        highest_idx, highest_voltage = max(
            enumerate(cell_voltages), key=lambda x: x[1]
        )
        self.telemetry.lowest_cell = lowest_idx
        self.telemetry.lowest_cell_voltage = lowest_voltage
        self.telemetry.highest_cell = highest_idx
        self.telemetry.highest_cell_voltage = highest_voltage

        ## Delta cell voltage
        delta_cell_voltage = round(highest_voltage - lowest_voltage, 3)
        self.telemetry.delta_cell_voltage = delta_cell_voltage

        # Delta cell temperature
        delta_cell_temperature = round(
            max(cell_temps) - min(cell_temps), 1
        )
        self.telemetry.delta_cell_temperature = delta_cell_temperature

        ### Add to telemetry_feedback
        feedback.update({
            "dis_charge_power": dis_charge_power,
            "average_cell_voltage": avg_voltage,
            "lowest_cell": lowest_idx + 1,      # 1-based for display
            "lowest_cell_voltage": lowest_voltage,
            "highest_cell": highest_idx + 1,    # 1-based for display
            "highest_cell_voltage": highest_voltage,
            "delta_cell_voltage": delta_cell_voltage,
            "delta_cell_temperature": delta_cell_temperature
        })

        return telemetry_feedback

    def decode_telesignalization_feedback_frame(self, data: bytes) -> Dict[str, Any]:
        """Decode battery pack telesignalization feedback frame."""
        telesignalization_feedback = {"normal": {}, "binary": {}}
        feedback_normal = telesignalization_feedback["normal"]
        feedback_binary = telesignalization_feedback["binary"]

        # Number of cells
        number_of_cells = bytes.fromhex(data.decode("ascii"))[2]

        # 24-Byte alarms

        byte_alarm_fields = {
            'cell_voltage_alarm':           { 'offset': 3,   'amount': number_of_cells },  # doc: "Cell 1..M alarm" (0x44)
            'cell_temperature_alarm':       { 'offset': 20,  'amount': 4 },  # doc: "Cell temperature alarm 1..4" (0x44)
            'ambient_temperature_alarm':    { 'offset': 24 },  # doc: "Environment temperature alarm" (0x44)
            'component_temperature_alarm':  { 'offset': 25 },  # doc: "Power temperature alarm" (0x44)
            'dis_charging_current_alarm':   { 'offset': 26 },  # doc: "Charge/discharge current alarm" (0x44)
            'pack_voltage_alarm':           { 'offset': 27 }  # doc: "Total battery voltage alarm" (0x44)
        }

        ## Fetch values for all byte_alarm fields
        for attr, cfg in byte_alarm_fields.items():
            offset = cfg["offset"]
            amount = cfg.get("amount", 1)

            if amount > 1:
                for i in range(amount):
                    value = self.status_from_24_byte_alarm(
                        data,
                        offset + i
                    )
                    getattr(self.telesignalization, attr)[i] = value

                    ### Add to telesignalization_feedback
                    feedback_normal[f"{attr}_{i + 1}"] = value
            else:
                value = self.status_from_24_byte_alarm(
                    data,
                    offset
                )
                setattr(self.telesignalization, attr, value)

                ### Add to telesignalization_feedback
                feedback_normal[attr] = value

        # Calculated values

        ## General cell voltage alarm if any of the cells has an active alarm
        feedback_normal["any_cell_voltage_alarm"] = (
            "Alarm" if any(cva != "OK" for cva in self.telesignalization.cell_voltage_alarm) else "OK"
        )

        ## General cell temperature alarm if any of the cells has an active alarm
        feedback_normal["any_cell_temperature_alarm"] = (
            "Alarm" if any(cta != "OK" for cta in self.telesignalization.cell_temperature_alarm) else "OK"
        )

         # 20-Bit alarms

        bit_alarm_fields = {
            'alarm_event_1': {
                'offset': 29,
                'sensors': [
                    { 'name': 'voltage_sensing_failure', 'mode': 'fault_normal', 'first_bit': 0 },  # doc: Alarm event 1 bit 0 "Voltage sensor fault"
                    { 'name': 'temperature_sensing_failure', 'mode': 'fault_normal', 'first_bit': 1 },  # doc: Alarm event 1 bit 1 "Temperature sensor fault"
                    { 'name': 'current_sensing_failure', 'mode': 'fault_normal', 'first_bit': 2 },  # doc: Alarm event 1 bit 2 "Current sensor fault"
                    { 'name': 'power_switch_failure', 'mode': 'fault_normal', 'first_bit': 3 },  # doc: Alarm event 1 bit 3 "Key switch fault"
                    { 'name': 'cell_voltage_difference_fault', 'mode': 'fault_normal', 'first_bit': 4 },  # doc: Alarm event 1 bit 4 "Cell voltage dropout fault", delta >= P60
                    { 'name': 'charging_switch_failure', 'mode': 'fault_normal', 'first_bit': 5 },  # doc: Alarm event 1 bit 5 "Charge switch fault"
                    { 'name': 'discharging_switch_failure', 'mode': 'fault_normal', 'first_bit': 6 },  # doc: Alarm event 1 bit 6 "Discharge switch fault"
                    { 'name': 'current_limit_switch_failure', 'mode': 'fault_normal', 'first_bit': 7 }  # doc: Alarm event 1 bit 7 "Current limit switch fault"
                ]
            },
            'alarm_event_2': {
                'offset': 30,
                'sensors': [
                    { 'name': 'cell_overvoltage', 'mode': 'protection_alarm_normal', 'first_bit': 0, 'second_bit': 1 },  # doc: Alarm event 2 bit 0/1 "Monomer high voltage alarm" / "Monomer overvoltage protection"
                    { 'name': 'cell_voltage_low', 'mode': 'protection_alarm_normal', 'first_bit': 2, 'second_bit': 3 },  # doc: Alarm event 2 bit 2/3 "Monomer low voltage alarm / under voltage protection"
                    { 'name': 'pack_overvoltage', 'mode': 'protection_alarm_normal', 'first_bit': 4, 'second_bit': 5 },  # doc: Alarm event 2 bit 4/5 "High voltage alarm / Overvoltage protection for total voltage"
                    { 'name': 'pack_voltage_low', 'mode': 'protection_alarm_normal', 'first_bit': 6, 'second_bit': 7 }  # doc: Alarm event 2 bit 6/7 "Low voltage alarm / Under voltage protection for total voltage"
                ]
            },
            'alarm_event_3': {
                'offset': 31,
                'sensors': [
                    { 'name': 'charging_temperature_high', 'mode': 'protection_alarm_normal', 'first_bit': 0, 'second_bit': 1 },  # doc: Alarm event 3 bit 0/1 "Charge high temperature alarm / over temperature protection"
                    { 'name': 'charging_temperature_low', 'mode': 'protection_alarm_normal', 'first_bit': 2, 'second_bit': 3 },  # doc: Alarm event 3 bit 2/3 "Charge low temperature alarm / under temperature protection"
                    { 'name': 'discharging_temperature_high', 'mode': 'protection_alarm_normal', 'first_bit': 4, 'second_bit': 5 },  # doc: Alarm event 3 bit 4/5 "Discharge high temperature alarm / over temperature protection"
                    { 'name': 'discharging_temperature_low', 'mode': 'protection_alarm_normal', 'first_bit': 6, 'second_bit': 7 }  # doc: Alarm event 3 bit 6/7 "Discharge low temperature alarm / under temperature protection"
                ]
            },
            'alarm_event_4': {
                'offset': 32,
                'sensors': [
                    { 'name': 'ambient_temperature_high', 'mode': 'protection_alarm_normal', 'first_bit': 0, 'second_bit': 1 },  # doc: Alarm event 4 bit 0/1 "Environment high temperature alarm / over temperature protection"
                    { 'name': 'ambient_temperature_low', 'mode': 'protection_alarm_normal', 'first_bit': 2, 'second_bit': 3 },  # doc: Alarm event 4 bit 2/3 "Environment low temperature alarm / under temperature protection"
                    # bit 4 = power (component) over temperature protection, bit 5 = power high temperature alarm
                    { 'name': 'component_temperature_high', 'mode': 'protection_alarm_normal', 'first_bit': 5, 'second_bit': 4 },  # doc: Alarm event 4 bit 5/4 "Power high temperature alarm" / "Power over temperature protection"
                    { 'name': 'low_temperature_heating', 'mode': 'on_off', 'first_bit': 6 }  # doc: Alarm event 4 bit 6 "Cell low temperature heating" = heating requested
                ]
            },
            'alarm_event_5': {
                'offset': 33,
                'sensors': [
                    { 'name': 'charging_overcurrent', 'mode': 'protection_alarm_normal', 'first_bit': 0, 'second_bit': 1 },  # doc: Alarm event 5 bit 0/1 "Charge over current alarm / protection"
                    { 'name': 'discharging_overcurrent', 'mode': 'protection_alarm_normal', 'first_bit': 2, 'second_bit': 3 },  # doc: Alarm event 5 bit 2/3 "Discharge over current alarm / protection"
                    # bit 4/5 = transient overcurrent / short circuit protection, bit 6/7 = their lockouts
                    { 'name': 'transient_overcurrent', 'mode': 'lockout_protection_normal', 'first_bit': 4, 'second_bit': 6 },  # doc: Alarm event 5 bit 4/6 "Transient over current protection / lockout"
                    { 'name': 'output_short_circuit', 'mode': 'lockout_protection_normal', 'first_bit': 5, 'second_bit': 7 }  # doc: Alarm event 5 bit 5/7 "Output short circuit protection / lockout"
                ]
            },
            'alarm_event_6': {
                'offset': 34,
                'sensors': [
                    { 'name': 'charging_high_voltage_protection', 'mode': 'protection_normal', 'first_bit': 0 },  # doc: Alarm event 6 bit 0 "Charge high voltage protection" (charger > P18)
                    { 'name': 'intermittent_power_supplement', 'mode': 'warning_normal', 'first_bit': 1 },  # doc: Alarm event 6 bit 1 "Intermittent recharge waiting"
                    { 'name': 'soc_low', 'mode': 'protection_alarm_normal', 'first_bit': 2, 'second_bit': 3 },  # doc: Alarm event 6 bit 2/3 "Residual capacity alarm / protection"
                    { 'name': 'cell_low_voltage_forbidden_charging', 'mode': 'protection_normal', 'first_bit': 4 },  # doc: Alarm event 6 bit 4 "Cell low voltage charging prohibition" (cell < P9)
                    { 'name': 'output_reverse_polarity_protection', 'mode': 'protection_normal', 'first_bit': 5 },  # doc: Alarm event 6 bit 5 "Output reverse polarity protection"
                    { 'name': 'output_connection_failure', 'mode': 'fault_normal', 'first_bit': 6 }  # doc: Alarm event 6 bit 6 "Output connection fault"
                ]
            },
            'switch_status': {
                'offset': 35,
                'sensors': [
                    { 'name': 'discharge_switch', 'mode': 'on_off', 'first_bit': 0 },  # doc: On-off state bit 0 "Discharge switch state"
                    { 'name': 'charge_switch', 'mode': 'on_off', 'first_bit': 1 },  # doc: On-off state bit 1 "Charge switch state"
                    { 'name': 'current_limit_switch', 'mode': 'on_off', 'first_bit': 2 },  # doc: On-off state bit 2 "Current limit switch state"
                    { 'name': 'heating_switch', 'mode': 'on_off', 'first_bit': 3 }  # doc: On-off state bit 3 "Heating switch state"
                ]
            },
            'balancer_1':  {
                'offset': 36,
                'sensors': [
                    { 'name': 'balancer_cell', 'mode': 'on_off', 'first_bit': 0, 'amount': 8 }  # doc: "Equilibrium state 1/2" (cells 1-8 / 9-16)
                ]
            },
            'balancer_2':  {
                'offset': 37,
                'sensors': [
                    { 'name': 'balancer_cell', 'mode': 'on_off', 'first_bit': 0, 'amount': 8, "start": 8 }  # doc: "Equilibrium state 1/2" (cells 1-8 / 9-16)
                ]
            },
            # offset 38: system status, decoded separately (see decode_system_status)
            # offsets 39/40: cell disconnection 1/2 - not evaluated, the firmware always sends 0x00
            'alarm_event_7': {
                'offset': 41,
                'sensors': [
                    { 'name': 'auto_charging_wait', 'mode': 'warning_normal', 'first_bit': 4 },  # doc: Alarm event 7 bit 4 "Automatic charging waiting"
                    { 'name': 'manual_charging_wait', 'mode': 'warning_normal', 'first_bit': 5 }  # doc: Alarm event 7 bit 5 "Manual charging waiting"
                ]
            },
            'alarm_event_8': {
                'offset': 42,
                'sensors': [
                    { 'name': 'eep_storage_failure', 'mode': 'fault_normal', 'first_bit': 0 },  # doc: Alarm event 8 bit 0 "EEP storage fault"
                    { 'name': 'rtc_clock_failure', 'mode': 'fault_normal', 'first_bit': 1 },  # doc: Alarm event 8 bit 1 "RTC error"
                    { 'name': 'no_calibration_of_voltage', 'mode': 'warning_normal', 'first_bit': 2 },  # doc: Alarm event 8 bit 2 "Voltage calibration not performed"
                    { 'name': 'no_calibration_of_current', 'mode': 'warning_normal', 'first_bit': 3 },  # doc: Alarm event 8 bit 3 "Current calibration not performed"
                    { 'name': 'no_calibration_of_null_point', 'mode': 'warning_normal', 'first_bit': 4 }  # doc: Alarm event 8 bit 4 "Zero calibration not performed"
                ]
            }
        }

        ## Fetch values for all bit_alarm fields
        for group, cfg in bit_alarm_fields.items():
            offset = cfg["offset"]
            sensors = cfg["sensors"]

            for sensor in sensors:
                name       = sensor.get("name")
                mode       = sensor.get("mode")
                first_bit  = sensor.get("first_bit", 0)
                second_bit = sensor.get("second_bit")
                amount     = sensor.get("amount", 1)
                start      = sensor.get("start", 0)

                #### Binary-sensors only
                if amount > 1:
                    arr = getattr(self.telesignalization, name)

                    for i in range(amount):
                        bit = first_bit + i
                        value = self.status_from_20_bit_alarm(
                            data,
                            offset,
                            mode=mode,
                            first_bit=bit
                        )
                        idx = start + i
                        arr[idx] = value

                        ### Add to telesignalization_feedback
                        feedback_binary[f"{name}_{idx + 1}"] = value
                #### Normal- and binary-sensors
                else:
                    value = self.status_from_20_bit_alarm(
                        data,
                        offset,
                        mode=mode,
                        first_bit=first_bit,
                        second_bit=second_bit
                    )
                    setattr(self.telesignalization, name, value)

                    ### Add to telesignalization_feedback
                    if mode in ("protection_alarm_normal", "lockout_protection_normal"):
                        feedback_normal[name] = value
                    else:
                        feedback_binary[name] = value

        ## System status (offset 38)
        system_status = self.decode_system_status(bytes.fromhex(data.decode("ascii"))[38])
        self.telesignalization.system_status = system_status
        feedback_normal["system_status"] = system_status

        return telesignalization_feedback

    @staticmethod
    def decode_system_status(status_byte: int) -> str:
        """
        Decode the system status byte (doc: "System state") with a fixed priority, so the state
        never sticks when no bit is set.
        bit 0 discharging, bit 1 charging, bit 4 standby, bit 5 off (shutdown).
        Bit 2 ("floating charge" per doc, actually an internal "charge path active" flag) is
        masked out by the firmware in 0x44.
        """
        if status_byte & 0x20:
            return "Off"
        if status_byte & 0x02:
            return "Charging"
        if status_byte & 0x01:
            return "Discharging"
        if status_byte & 0x10:
            return "Standby"
        return "Unknown"

    @classmethod
    def decode_intra_pack_frame(cls, info: bytes) -> Dict[str, Any]:
        """
        Decode the 32 byte INFO of an intra-pack answer (CID2 0x5A, slave -> master):
        10 x u16 measurements followed by 10 status bytes.
        """
        raw = bytes.fromhex(info.decode("ascii"))

        def u16(i: int, signed: bool = False) -> int:
            return int.from_bytes(raw[i:i + 2], "big", signed=signed)

        return {
            "highest_cell_voltage": u16(2) / 1000,
            "lowest_cell_voltage": u16(4) / 1000,
            "highest_cell_temperature": round((u16(6) - 2731) / 10, 1),
            "lowest_cell_temperature": round((u16(8) - 2731) / 10, 1),
            "current": u16(10, signed=True) / 100,
            "pack_voltage": u16(12) / 100,
            "residual_capacity": u16(14) / 100,
            "full_capacity": u16(16) / 100,
            "state_of_charge": u16(18) / 10,
            "port_voltage": u16(20) / 100,
            "alarm_events_1_6": raw[22:28].hex(" ").upper(),
            "switch_status": f"{raw[28]:02X}",
            "alarm_event_7": f"{raw[29]:02X}",   # bit0 balancing active, bit1 balancing blocked/not needed
            "alarm_event_8": f"{raw[30]:02X}",
            "system_status": f"{raw[31]:02X}"
        }

    def _read_frame(self, max_skipped: int = 5) -> bytes:
        """Read the next frame, logging and skipping interleaved intra-pack frames."""
        raw_data = app_state.serial_instance.read_until(b'\r')
        for _ in range(max_skipped):
            if not self.log_intra_pack_frame(raw_data):
                break
            raw_data = app_state.serial_instance.read_until(b'\r')
        return raw_data

    @classmethod
    def log_intra_pack_frame(cls, raw_data: bytes) -> bool:
        """
        Detect intra-pack traffic (master polling its slaves with CID2 0x5A on RS485-1/2),
        log it decoded at DEBUG level and return True so it gets skipped by the caller.
        """
        try:
            if len(raw_data) < cls.FRAME_OVERHEAD_LENGTH or raw_data[:1] != b"~" or raw_data[7:9] != b"5A":
                return False
            address = int(raw_data[3:5], 16)
            if logger.isEnabledFor(logging.DEBUG):
                if raw_data[9:13] == b"0000":
                    logger.debug("Intra-pack: master requests pack %s", address)
                elif raw_data[9:13] == b"C040" and len(raw_data) == 82:
                    decoded = cls.decode_intra_pack_frame(raw_data[13:-5])
                    logger.debug("Intra-pack: pack %s answers %s", address, json.dumps(decoded))
                else:
                    logger.debug("Intra-pack: unexpected frame %s", raw_data)
            return True
        except (UnicodeDecodeError, ValueError):
            return False

    @staticmethod
    def decode_device_info_frame(data: bytes) -> Dict[str, Any]:
        """
        Decode device info feedback frame (CID2 0x51, 32 bytes):
        device name (10), firmware version (2), name of the active CAN inverter protocol (20).
        """
        info = bytes.fromhex(data.decode("ascii"))
        return {
            "normal": {
                "device_name": info[0:10].decode("ascii", errors="replace").strip("\x00 "),
                "firmware_version": f"{info[10]}.{info[11]:02d}",
                "can_protocol": info[12:32].decode("ascii", errors="replace").strip("\x00 ")
            }
        }

    @staticmethod
    def decode_serial_number_frame(data: bytes) -> Dict[str, Any]:
        """Decode serial number feedback frame (CID2 0xA2, 30 bytes ASCII)."""
        info = bytes.fromhex(data.decode("ascii"))
        return {
            "normal": {
                "serial_number": info[0:30].decode("ascii", errors="replace").strip("\x00 ")
            }
        }

    def _request_feedback_frame(
        self,
        cid2: int,
        expected_length: int,
        decoder: Callable[[bytes], Dict[str, Any]],
        frame_label: str,
        info: bytes = b"01"
    ) -> Optional[Dict[str, Any]]:
        """Request a feedback frame with retry/validation."""
        if not app_state.serial_instance:
            logger.error("Serial instance not initialized")
            return None

        command = self.encode_cmd(address=self.pack_address, cid2=cid2, info=info)
        logger.debug("Pack%s:%s_command: %s", self.pack_address, frame_label, command)

        # SOI (1) + VER/ADR/CID1/RTN/LENGTH (12) + INFO + CHKSUM (4) + EOI (1)
        expected_frame_length = expected_length + self.FRAME_OVERHEAD_LENGTH

        for attempt in range(self.FRAME_READ_RETRIES):
            app_state.serial_instance.write(command)
            raw_data = self._read_frame()

            if len(raw_data) < expected_frame_length:
                logger.debug(
                    "Pack%s:%s attempt %s: insufficient length",
                    self.pack_address,
                    frame_label,
                    attempt + 1
                )
                continue

            pack_address_data = raw_data[3:5]
            info_frame_data = raw_data[13:-5]

            if (
                self.is_valid_hex_string(pack_address_data) and
                self.int_from_1byte_hex_ascii(pack_address_data, 0) == self.pack_address and
                self.is_valid_length(info_frame_data, expected_length=expected_length) and
                self.is_valid_hex_string(info_frame_data) and
                self.is_valid_frame(raw_data)
            ):
                feedback = decoder(info_frame_data)
                feedback_dump = json.dumps(feedback, indent=2)
                logger.info("Pack%s:%s received", self.pack_address, frame_label)
                logger.debug(
                    "Pack%s:%s: %s",
                    self.pack_address,
                    frame_label,
                    feedback_dump
                )
                return feedback

            logger.debug(
                "Pack%s:%s attempt %s: validation failed",
                self.pack_address,
                frame_label,
                attempt + 1
            )

        logger.error(
            "Pack%s:Failed to read %s after %s retries",
            self.pack_address,
            frame_label.lower(),
            self.FRAME_READ_RETRIES
        )
        return None

    def _fetch_device_info(self) -> None:
        """
        Fetch static device info once: device name, firmware version, active CAN protocol (0x51)
        and serial number (0xA2). Failures don't affect the regular poll; gives up after
        INFO_MAX_ATTEMPTS polls.
        """
        if self.info is not None or self.info_attempts >= self.INFO_MAX_ATTEMPTS:
            return
        self.info_attempts += 1

        requests = [(0x51, 64, self.decode_device_info_frame, "DeviceInfo")]
        # 0xA2 is only served on the RS485-1/2 port (UART0); the RS485 bus on the CAN port
        # (UART1) only knows 0x42/0x44/0x4F/0x51/0x60-0x63
        if not is_can_port_rs485():
            requests.append((0xA2, 60, self.decode_serial_number_frame, "SerialNumber"))

        info: Dict[str, Any] = {"normal": {}}
        for cid2, expected_length, decoder, label in requests:
            # Mandatory delay between each request or there will be corrupt data
            time.sleep(1)
            feedback = self._request_feedback_frame(
                cid2=cid2,
                expected_length=expected_length,
                decoder=decoder,
                frame_label=label,
                info=b""
            )
            if feedback is None:
                if self.info_attempts >= self.INFO_MAX_ATTEMPTS:
                    logger.warning("Pack%s:Giving up on device info", self.pack_address)
                return
            info["normal"].update(feedback["normal"])

        info["normal"]["firmware_patch"] = self._derive_firmware_patch(info["normal"]["firmware_version"])
        self.info = info

    def _derive_firmware_patch(self, firmware_version: str) -> str:
        """
        Derive the firmware patch level. The firmware only reports major.minor (e.g. 16.06),
        so it is derived from features that only exist since a certain patch level.
        """
        if firmware_version == "16.04":
            return "16.04.x"
        if firmware_version != "16.06":
            return f"{firmware_version} (unknown)"
        if is_can_port_rs485():
            return self._derive_patch_can_port()
        return self._derive_patch_pack_bus()

    def _derive_patch_pack_bus(self) -> str:
        """
        RS485-1/2 port (UART0, 19200 baud): CID2 0xAA exists since 16.06.13. Older versions
        answer it with RTN 0x04, 16.06.13+ doesn't answer (0xAA tunnels a request to the device
        at the RS232/binary port; if one is attached, a late reply with 6 bytes may follow,
        which the regular frame validation discards).
        No answer only counts if the pack answers a 0x51 right afterwards.
        """
        silent_rounds = 0
        for _ in range(3):
            time.sleep(1)
            if self._request_rtn(cid2=0xAA) == 0x04:
                return "16.06.03-16.06.12"
            time.sleep(1)
            if self._request_rtn(cid2=0x51) == 0x00:
                silent_rounds += 1
        return "16.06.13+" if silent_rounds == 3 else "16.06 (unknown)"

    def _derive_patch_can_port(self) -> str:
        """
        RS485 bus on the CAN port (UART1, 9600 baud). Only the master pack (address 0) serves
        the Pylontech system commands and Modbus RTU there, so slaves can't be classified.
        - Pylontech 0x60 at ADR 0x02: accepted since 16.06.12 (before only ADR 0x12),
          0x60 itself exists since 16.06.04, 0x61 since 16.04
        - Modbus FC03 0x001F x9: since 16.06.13; Modbus FC04 0x0000 x21: since 16.06.11
        - Modbus FC03 0x9000: 16.06.13 only accepts 26 registers, before only 21 (fallback check)
        """
        if self.pack_address != 0:
            return "16.06 (unknown, only derivable for the master)"

        time.sleep(1)
        if self._request_rtn(cid2=0x60, address=0x02) == 0x00:
            # Modbus answers can get lost on the shared bus, so retry and cross-check
            for _ in range(3):
                time.sleep(1)
                result = self._request_modbus(function_code=0x03, register=0x001F, count=9)
                logger.debug("Pack%s:Patch probe Modbus 0x001F x9: %s", self.pack_address, result)
                if result is True:
                    return "16.06.13+"
                if result is False:
                    return "16.06.12"
                time.sleep(1)
                result = self._request_modbus(function_code=0x03, register=0x9000, count=21)
                logger.debug("Pack%s:Patch probe Modbus 0x9000 x21: %s", self.pack_address, result)
                if result is True:
                    return "16.06.12"
                if result is False:
                    return "16.06.13+"
            return "16.06.12+"

        time.sleep(1)
        if self._request_rtn(cid2=0x60, address=0x12) == 0x00:
            time.sleep(1)
            result = self._request_modbus(function_code=0x04, register=0x0000, count=21)
            if result is True:
                return "16.06.11"
            if result is False:
                return "16.06.04-16.06.05"
            return "16.06.04-16.06.11"

        time.sleep(1)
        if self._request_rtn(cid2=0x61, address=0x12) == 0x00:
            return "16.06.03"
        return "16.06 (unknown)"

    def _request_rtn(self, cid2: int, address: Optional[int] = None) -> Optional[int]:
        """
        Send a command without INFO and return the RTN code of the answer,
        None if there's no valid answer.
        """
        if not app_state.serial_instance:
            return None
        address = self.pack_address if address is None else address
        app_state.serial_instance.flushInput()
        app_state.serial_instance.write(self.encode_cmd(address=address, cid2=cid2, info=b""))
        raw_data = self._read_frame()
        try:
            if len(raw_data) < self.FRAME_OVERHEAD_LENGTH or raw_data[:1] != b"~":
                return None
            if self.int_from_1byte_hex_ascii(raw_data, 3) != address:
                return None
            if self.calculate_frame_checksum(raw_data[1:-5]) != self.int_from_2byte_hex_ascii(raw_data, -5):
                return None
            return self.int_from_1byte_hex_ascii(raw_data, 7)
        except (UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def modbus_crc16(data: bytes) -> int:
        """Modbus CRC16 (poly 0xA001, init 0xFFFF)."""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return crc

    def _read_modbus_registers(
        self, function_code: int, register: int, count: int
    ) -> Tuple[Optional[str], Optional[List[int]]]:
        """
        Read registers via Modbus RTU (slave ID 1, served by the master on the CAN port RS485 bus).
        Returns ("ok", registers), ("exception", None) or (None, None) for no/invalid answer.
        """
        if not app_state.serial_instance:
            return None, None
        request = bytes([0x01, function_code, register >> 8, register & 0xFF, 0x00, count])
        request += self.modbus_crc16(request).to_bytes(2, "little")
        app_state.serial_instance.flushInput()
        app_state.serial_instance.write(request)
        response = app_state.serial_instance.read(5 + 2 * count)
        if len(response) < 5 or response[0] != 0x01:
            return None, None
        if response[1] == function_code | 0x80:
            frame = response[:5]
        elif response[1] == function_code and len(response) == 5 + 2 * count and response[2] == 2 * count:
            frame = response
        else:
            return None, None
        if self.modbus_crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
            return None, None
        if response[1] != function_code:
            return "exception", None
        data = frame[3:-2]
        return "ok", [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]

    def _request_modbus(self, function_code: int, register: int, count: int) -> Optional[bool]:
        """True for a valid response, False for an exception response, None for no/invalid answer."""
        status, _ = self._read_modbus_registers(function_code, register, count)
        if status is None:
            return None
        return status == "ok"

    # ---------------------------------------------------------------------
    # Extended status via the intra-pack record (CID2 0x5A, RS485-1/2 only)
    # ---------------------------------------------------------------------

    def _request_intra_pack_status(self) -> Optional[Dict[str, Any]]:
        """
        Request the pack's own intra-pack record (CID2 0x5A). The answer carries 0x5A instead of
        a RTN code and contains status bits that 0x44 masks out. Only served on RS485-1/2 (UART0).
        """
        if not app_state.serial_instance:
            return None
        command = self.encode_cmd(address=self.pack_address, cid2=0x5A, info=b"")
        for _ in range(self.FRAME_READ_RETRIES):
            app_state.serial_instance.flushInput()
            app_state.serial_instance.write(command)
            raw_data = app_state.serial_instance.read_until(b'\r')
            try:
                if (
                    len(raw_data) == 64 + self.FRAME_OVERHEAD_LENGTH and
                    raw_data[:1] == b"~" and
                    raw_data[7:13] == b"5AC040" and
                    self.int_from_1byte_hex_ascii(raw_data, 3) == self.pack_address and
                    self.calculate_frame_checksum(raw_data[1:-5]) == self.int_from_2byte_hex_ascii(raw_data, -5)
                ):
                    return self.decode_extended_status(raw_data[13:-5])
            except (UnicodeDecodeError, ValueError):
                pass
            time.sleep(0.2)
        logger.debug("Pack%s:No valid intra-pack (0x5A) answer", self.pack_address)
        return None

    @classmethod
    def decode_extended_status(cls, info: bytes) -> Dict[str, Any]:
        """Extract the values of the intra-pack record that aren't available via 0x42/0x44."""
        record = cls.decode_intra_pack_frame(info)
        raw = bytes.fromhex(info.decode("ascii"))
        alarm_event_7 = raw[29]
        alarm_event_8 = raw[30]

        def on_off(value: bool) -> str:
            return "ON" if value else "OFF"

        return {
            "normal": {
                # not zeroed in idle like the current of 0x42
                "dis_charge_current_unfiltered": record["current"],
                "highest_cell_temperature": record["highest_cell_temperature"],
                "lowest_cell_temperature": record["lowest_cell_temperature"],
            },
            "binary": {
                "balancing_active": on_off(alarm_event_7 & 0x01),
                "balancing_blocked": on_off(alarm_event_7 & 0x02),          # blocked or not needed
                "balancing_time_limit": on_off(alarm_event_7 & 0x04),
                "balancing_temperature_blocked": on_off(alarm_event_7 & 0x08),
                "charge_forbidden": on_off(alarm_event_8 & 0x40),           # drives the charge MOSFET
            }
        }

    # ---------------------------------------------------------------------
    # Parameters (CID2 0x47, RS485-1/2 only)
    # ---------------------------------------------------------------------

    # (name, kind) for parameter index 0..86, kind defines the raw unit
    PARAMETER_DEFINITIONS: List[Tuple[str, str]] = [
        ("cell_high_voltage_alarm", "mV"), ("cell_high_voltage_recovery", "mV"),
        ("cell_low_voltage_alarm", "mV"), ("cell_low_voltage_recovery", "mV"),
        ("cell_overvoltage_protection", "mV"), ("cell_overvoltage_recovery", "mV"),
        ("cell_undervoltage_protection", "mV"), ("cell_undervoltage_recovery", "mV"),
        ("balancing_start_voltage", "mV"), ("cell_low_voltage_charging_forbidden", "mV"),
        ("pack_high_voltage_alarm", "10mV"), ("pack_high_voltage_recovery", "10mV"),
        ("pack_low_voltage_alarm", "10mV"), ("pack_low_voltage_recovery", "10mV"),
        ("pack_overvoltage_protection", "10mV"), ("pack_overvoltage_recovery", "10mV"),
        ("pack_undervoltage_protection", "10mV"), ("pack_undervoltage_recovery", "10mV"),
        ("charger_overvoltage_protection", "10mV"), ("charger_overvoltage_recovery", "10mV"),
        ("charging_high_temperature_alarm", "0.1K"), ("charging_high_temperature_recovery", "0.1K"),
        ("charging_low_temperature_alarm", "0.1K"), ("charging_low_temperature_recovery", "0.1K"),
        ("charging_over_temperature_protection", "0.1K"), ("charging_over_temperature_recovery", "0.1K"),
        ("charging_under_temperature_protection", "0.1K"), ("charging_under_temperature_recovery", "0.1K"),
        ("discharging_high_temperature_alarm", "0.1K"), ("discharging_high_temperature_recovery", "0.1K"),
        ("discharging_low_temperature_alarm", "0.1K"), ("discharging_low_temperature_recovery", "0.1K"),
        ("discharging_over_temperature_protection", "0.1K"), ("discharging_over_temperature_recovery", "0.1K"),
        ("discharging_under_temperature_protection", "0.1K"), ("discharging_under_temperature_recovery", "0.1K"),
        ("cell_heating_on", "0.1K"), ("cell_heating_off", "0.1K"),
        ("ambient_high_temperature_alarm", "0.1K"), ("ambient_high_temperature_recovery", "0.1K"),
        ("ambient_low_temperature_alarm", "0.1K"), ("ambient_low_temperature_recovery", "0.1K"),
        ("ambient_over_temperature_protection", "0.1K"), ("ambient_over_temperature_recovery", "0.1K"),
        ("ambient_under_temperature_protection", "0.1K"), ("ambient_under_temperature_recovery", "0.1K"),
        ("power_high_temperature_alarm", "0.1K"), ("power_high_temperature_recovery", "0.1K"),
        ("power_over_temperature_protection", "0.1K"), ("power_over_temperature_recovery", "0.1K"),
        ("charging_overcurrent_alarm", "10mA"), ("charging_overcurrent_recovery", "10mA"),
        ("discharging_overcurrent_alarm", "10mA"), ("discharging_overcurrent_recovery", "10mA"),
        ("charging_overcurrent_protection", "10mA"), ("discharging_overcurrent_protection", "10mA"),
        ("transient_overcurrent_protection", "10mA"), ("output_soft_start_delay_ms", "raw"),
        ("rated_capacity", "10mAh"), ("remaining_capacity_setting", "10mAh"),
        # byte parameters 60..86
        ("cell_difference_fault", "10mV"), ("cell_difference_fault_recovery", "10mV"),
        ("balancing_start_difference", "mV"), ("balancing_stop_difference", "mV"),
        ("static_balancing_time_h", "raw"), ("cells_in_series", "raw"),
        ("charging_overcurrent_delay_s", "raw"), ("discharging_overcurrent_delay_s", "raw"),
        ("transient_overcurrent_delay_ms", "raw"), ("overcurrent_recovery_delay_s", "raw"),
        ("overcurrent_recovery_attempts", "raw"), ("charge_current_limit_delay_min", "raw"),
        ("charge_activation_delay_min", "raw"), ("charge_activation_interval_h", "raw"),
        ("charge_activation_attempts", "raw"), ("work_record_interval_min", "raw"),
        ("standby_record_interval_min", "raw"), ("standby_shutdown_delay_h", "raw"),
        ("remaining_capacity_alarm_percent", "raw"), ("remaining_capacity_protection_percent", "raw"),
        ("intermittent_charge_capacity_percent", "raw"), ("cycle_cumulative_capacity_percent", "raw"),
        # impedances: unit 0.1 mOhm derived from the BatteryMonitor export (one decimal), not verified in code
        ("connection_fault_impedance_mohm", "0.1"), ("compensation_point_1_position", "raw"),
        ("compensation_point_1_impedance_mohm", "0.1"), ("compensation_point_2_position", "raw"),
        ("compensation_point_2_impedance_mohm", "0.1"),
    ]

    @classmethod
    def decode_parameter_frame(cls, data: bytes) -> Dict[str, Any]:
        """
        Decode the parameter answer (CID2 0x47, 169 bytes): address, 0x3C + 60 x u16,
        0x1B + 27 x u8, 0x08 + 8 function switch bytes, 10 byte device name.
        """
        raw = bytes.fromhex(data.decode("ascii"))
        if raw[1] != 0x3C or raw[122] != 0x1B or raw[150] != 0x08:
            raise ValueError("unexpected parameter frame markers")

        values = [int.from_bytes(raw[2 + 2 * i:4 + 2 * i], "big") for i in range(60)]
        values += list(raw[123:150])

        parameters: Dict[str, Any] = {}
        for (name, kind), value in zip(cls.PARAMETER_DEFINITIONS, values):
            if kind == "mV":
                parameters[f"{name}_v"] = round(value / 1000, 3)
            elif kind == "10mV":
                parameters[f"{name}_v"] = round(value / 100, 2)
            elif kind == "0.1K":
                parameters[f"{name}_c"] = round((value - 2731) / 10, 1)
            elif kind == "10mA":
                signed = value - 0x10000 if value & 0x8000 else value
                parameters[f"{name}_a"] = round(signed / 100, 2)
            elif kind == "10mAh":
                parameters[f"{name}_ah"] = round(value / 100, 2)
            elif kind == "0.1":
                parameters[name] = round(value / 10, 1)
            else:
                parameters[name] = value

        switches = raw[151:159]
        parameters["function_switches"] = switches.hex(" ").upper()
        # switch byte k (k = 0..5) masks alarm event k+1, bytes 6/7 hold functions
        parameters["balancing_enabled"] = bool(switches[6] & 0x01)
        parameters["balancing_in_idle"] = bool(switches[6] & 0x02)
        parameters["balancing_time_limit_enabled"] = bool(switches[6] & 0x04)
        parameters["balancing_temperature_block_enabled"] = bool(switches[6] & 0x08)
        parameters["charge_current_limit_mode"] = "warning - 10 A" if switches[7] & 0x80 else "warning / 2"
        disabled = {
            f"alarm_event_{k + 1}": [bit for bit in range(8) if not switches[k] & (1 << bit)]
            for k in range(6)
        }
        parameters["disabled_alarm_bits"] = {k: v for k, v in disabled.items() if v}
        parameters["device_name"] = raw[159:169].decode("ascii", errors="replace").strip("\x00 ")
        return parameters

    def _fetch_parameters(self) -> None:
        """Fetch the parameters (CID2 0x47) at startup and then every PARAMETER_REFRESH_SECONDS."""
        if time.time() - self.parameters_ts < self.PARAMETER_REFRESH_SECONDS:
            return
        self.parameters_ts = time.time()
        time.sleep(1)
        feedback = self._request_feedback_frame(
            cid2=0x47,
            expected_length=338,
            decoder=lambda data: {"normal": self.decode_parameter_frame(data)},
            frame_label="Parameters",
            info=b""
        )
        if feedback is not None:
            self.parameters = feedback["normal"]
        else:
            # retry after a minute instead of waiting for the next regular refresh
            self.parameters_ts = time.time() - self.PARAMETER_REFRESH_SECONDS + 60

    def read_serial_data(self) -> Tuple[Optional[BatteryData], bool]:
        """Read data for battery pack from serial interface."""
        logger.info("Pack%s:Requesting data...", self.pack_address)

        if not app_state.serial_instance:
            logger.error("Serial instance not initialized")
            return None

        battery_pack_data = {
            "telemetry": {},
            "telesignalization": {}
        }

        try:
            # Flush serial buffers
            app_state.serial_instance.flushOutput()
            app_state.serial_instance.flushInput()

            # Request telemetry data
            telemetry_feedback = self._request_feedback_frame(
                cid2=0x42,
                expected_length=150,
                decoder=self.decode_telemetry_feedback_frame,
                frame_label="Telemetry"
            )
            if telemetry_feedback is None:
                return None, False
            battery_pack_data["telemetry"] = telemetry_feedback

            # Mandatory delay between each request or there will be corrupt data
            time.sleep(1)

            # Request telesignalization data
            telesignalization_feedback = self._request_feedback_frame(
                cid2=0x44,
                expected_length=98,
                decoder=self.decode_telesignalization_feedback_frame,
                frame_label="Telesignalization"
            )
            if telesignalization_feedback is None:
                return None, False
            battery_pack_data["telesignalization"] = telesignalization_feedback

            # Static device info (fetched once)
            self._fetch_device_info()
            if self.info:
                battery_pack_data["info"] = self.info

            # RS485-1/2 only (single pack mode): extended status (0x5A) and parameters (0x47)
            if not is_can_port_rs485():
                time.sleep(1)
                extended_status = self._request_intra_pack_status()
                if extended_status is not None:
                    self.extended_status = extended_status
                # keep the last valid status so the HA templates don't break on a single failed request
                if self.extended_status is not None:
                    battery_pack_data["status"] = self.extended_status
                self._fetch_parameters()
                if self.parameters:
                    battery_pack_data["parameters"] = self.parameters

            # Check if data has changed
            if self.last_status is None or self.last_status != battery_pack_data:
                self.last_status = battery_pack_data
                return battery_pack_data, True

            return None, True
        except Exception as e:
            logger.error("Pack%s:Error reading serial data: %s", self.pack_address, e)
            return None, False


class SystemData:
    """
    System values as the inverter sees them, read from the master pack via Modbus RTU
    FC03 0x1000 x21 (RS485 bus on the CAN port, firmware 16.06.04+): aggregated over all
    packs incl. the charge/discharge limits the BMS sends to the inverter.
    """

    MAX_FAILURES = 3
    SYSTEM_STATES = {0x01: "Discharging", 0x02: "Charging", 0x10: "Idle", 0x20: "Off"}

    def __init__(self):
        self.unsupported = False
        self.exceptions = 0
        self.last_data: Optional[Dict[str, Any]] = None

    @staticmethod
    def _signed(value: int) -> int:
        return value - 0x10000 if value & 0x8000 else value

    @staticmethod
    def _pack(value: int) -> Optional[int]:
        """Pack numbers are 1-based in the firmware, the add-on counts from 0 (master)."""
        return value - 1 if value > 0 else None

    @classmethod
    def decode(cls, regs: List[int]) -> Dict[str, Any]:
        """Decode Modbus FC03 registers 0x1000..0x1014."""
        s = cls._signed
        return {
            "normal": {
                "system_state": cls.SYSTEM_STATES.get(regs[0], f"0x{regs[0]:02X}"),
                "system_voltage": round(regs[4] / 10, 1),
                "system_current": round(s(regs[5]) / 10, 1),
                "system_soc": regs[6],
                "highest_cell_temperature": round(s(regs[8]) / 10, 1),
                "highest_cell_temperature_pack": cls._pack(regs[9]),
                "lowest_cell_temperature": round(s(regs[10]) / 10, 1),
                "lowest_cell_temperature_pack": cls._pack(regs[11]),
                "highest_cell_voltage": round(regs[12] / 1000, 3),
                "highest_cell_voltage_pack": cls._pack(regs[13]),
                "lowest_cell_voltage": round(regs[14] / 1000, 3),
                "lowest_cell_voltage_pack": cls._pack(regs[15]),
                "charge_voltage_limit": round(regs[16] / 10, 1),
                "charge_current_limit": round(regs[17] / 10, 1),
                "discharge_current_limit": round(regs[18] / 10, 1),
                "discharge_voltage_limit": round(regs[19] / 10, 1),
                "total_capacity": regs[20],
                # high byte: alarm event 5 (current), low byte: alarm event 2 (voltage), OR of all packs
                "alarm_word_current_voltage": f"0x{regs[2]:04X}",
                # low byte: alarm event 3 (cell temperature), high byte: ambient/power temperature,
                # heating, remaining capacity alarm, cell difference fault, pack communication fault
                "alarm_word_temperature_misc": f"0x{regs[3]:04X}",
            },
            "binary": {
                "charge_allowed": "ON" if regs[1] & 0x02 else "OFF",
                "discharge_allowed": "ON" if regs[1] & 0x01 else "OFF",
                "pack_communication_fault": "ON" if regs[3] & 0x8000 else "OFF",
            }
        }

    def read(self, master: "SeplosBatteryPack") -> Optional[Dict[str, Any]]:
        """Read the system values; disables itself if the firmware doesn't know the block."""
        if self.unsupported:
            return None
        status, regs = master._read_modbus_registers(0x03, 0x1000, 21)
        if status == "exception":
            self.exceptions += 1
            if self.exceptions >= self.MAX_FAILURES:
                self.unsupported = True
                logger.warning("System:Modbus block 0x1000 not supported (firmware < 16.06.04), disabled")
            return None
        if status != "ok" or regs is None:
            logger.debug("System:No valid Modbus answer")
            return None
        self.exceptions = 0
        return self.decode(regs)


def _publish_system_data(system: SystemData) -> None:
    """Read and publish the system values (multi pack mode via the CAN port RS485 bus only)."""
    if not is_can_port_rs485() or not app_state.battery_packs or not app_state.mqtt_client:
        return
    time.sleep(1)
    data = system.read(app_state.battery_packs[0]["pack_instance"])
    availability = "online" if data is not None else "offline"
    app_state.mqtt_client.publish(f"{Config.MQTT_TOPIC}/system/availability", availability, retain=False)
    if data is not None and data != system.last_data:
        system.last_data = data
        logger.info("System:Publishing updated data to MQTT")
        app_state.mqtt_client.publish(f"{Config.MQTT_TOPIC}/system/sensors", json.dumps(data, indent=2))


def on_mqtt_connect(
    _client: mqtt.Client,
    _userdata: Any,
    _flags: Any,
    reason_code: int
) -> None:
    """Handle MQTT connection."""
    global mqtt_connected
    if reason_code == 0:
        mqtt_connected = True
        logger.info(
            "Connected to MQTT broker (%s:%s)",
            Config.MQTT_HOST,
            Config.MQTT_PORT
        )
    else:
        mqtt_connected = False
        logger.error("Failed to connect to MQTT broker: %s", reason_code)


def on_mqtt_disconnect(
    _client: mqtt.Client,
    _userdata: Any,
    _reason_code: int,
    _properties: Any = None,
) -> None:
    """Handle MQTT disconnect."""
    global mqtt_connected
    mqtt_connected = False


def initialize_mqtt() -> mqtt.Client:
    """Initialize and connect MQTT client."""
    client = mqtt.Client()
    client.username_pw_set(Config.MQTT_USERNAME, Config.MQTT_PASSWORD)
    client.on_connect = on_mqtt_connect
    client.on_disconnect = on_mqtt_disconnect
    client.will_set(f"{Config.MQTT_TOPIC}/availability", payload="offline", qos=2, retain=False)

    try:
        client.connect(Config.MQTT_HOST, Config.MQTT_PORT, keepalive=60)
        client.loop_start()
        return client
    except MQTTException as e:
        logger.error("MQTT connection failed: %s", e)
        sys.exit(1)


def initialize_serial() -> serial.Serial:
    """Initialize serial connection."""
    try:
        # RS485 bus on the CAN port (UART1): 9600 baud, RS485-1/2 (UART0): 19200 baud
        baudrate = 9600 if is_can_port_rs485() else 19200
        logger.info(
            "Initializing serial interface %s at %s baud",
            Config.SERIAL_INTERFACE,
            baudrate
        )
        return serial.Serial(
            port=Config.SERIAL_INTERFACE,
            baudrate=baudrate,
            timeout=0.5
        )
    except SerialException as e:
        logger.error("Serial initialization failed: %s", e)
        sys.exit(1)


def main():
    """Main application loop."""
    global last_bms_update_ts
    try:
        # Initialize MQTT
        app_state.mqtt_client = initialize_mqtt()

        # Initialize Serial
        app_state.serial_instance = initialize_serial()

        # Initialize battery packs
        app_state.battery_packs.clear()
        for i in range(Config.NUMBER_OF_PACKS):
            pack_instance = SeplosBatteryPack(pack_address=i)
            app_state.battery_packs.append({
                "pack_instance": pack_instance,
                "address": i,
                "last_success_ts": 0.0,
                "publish_counter": 0,
                "availability": "offline",
            })
        logger.info("Initialized %s battery pack(s)", Config.NUMBER_OF_PACKS)
        for pack in app_state.battery_packs:
            app_state.mqtt_client.publish(_pack_availability_topic(pack["address"]), "offline", retain=False)

        # Initial grace: consider startup healthy until the first successful poll updates the timestamp.
        last_bms_update_ts = time.time()

        # Start minimal HTTP health endpoint for HA Supervisor watchdog
        health_thread = threading.Thread(target=_start_health_server, daemon=True)
        health_thread.start()
        logger.info("Health endpoint started on http://0.0.0.0:8080/health")

        # Send Home Assistant Auto-Discovery configurations on startup
        if Config.ENABLE_HA_DISCOVERY_CONFIG:
            logger.info("Sending Home Assistant Auto-Discovery configurations")
            auto_discovery = AutoDiscoveryConfig(
                mqtt_topic=Config.MQTT_TOPIC,
                discovery_prefix=Config.HA_DISCOVERY_PREFIX,
                invert_ha_dis_charge_measurements=Config.INVERT_HA_DIS_CHARGE_MEASUREMENTS,
                mqtt_client=app_state.mqtt_client
            )
            for pack in app_state.battery_packs:
                auto_discovery.create_autodiscovery_sensors(
                    pack_no=pack['address'],
                    pack_bus=not is_can_port_rs485()
                )
            if is_can_port_rs485():
                auto_discovery.create_system_sensors()
            else:
                auto_discovery.remove_system_sensors()
            logger.info("Auto-Discovery configurations sent")

        # Main loop
        pack_index = 0
        system_data = SystemData()
        while True:
            try:
                current_pack = app_state.battery_packs[pack_index]
                pack_instance = current_pack["pack_instance"]
                pack_address = current_pack["address"]

                # Fetch battery pack data
                pack_data, poll_success = pack_instance.read_serial_data()
                now = time.time()

                if poll_success:
                    last_bms_update_ts = now
                    current_pack["last_success_ts"] = now
                    current_pack["publish_counter"] += 1
                    heartbeat_payload = {
                        "last_publish": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "publish_counter": current_pack["publish_counter"],
                    }
                    app_state.mqtt_client.publish(
                        _pack_heartbeat_topic(pack_address),
                        json.dumps(heartbeat_payload),
                        retain=False,
                    )
                    app_state.mqtt_client.publish(
                        _pack_availability_topic(pack_address),
                        "online",
                        retain=False,
                    )
                    current_pack["availability"] = "online"

                if pack_data:
                    # Publish updated data to MQTT
                    logger.info("Pack%s:Publishing updated data to MQTT", pack_address)
                    topic = f"{Config.MQTT_TOPIC}/pack-{pack_address}/sensors"
                    payload = {**pack_data}
                    app_state.mqtt_client.publish(topic, json.dumps(payload, indent=2))
                elif poll_success:
                    logger.info("Pack%s:No changes detected", pack_address)

                for pack_state in app_state.battery_packs:
                    _publish_pack_availability(pack_state, now)

                # Publish availability
                app_state.mqtt_client.publish(f"{Config.MQTT_TOPIC}/availability", "online", retain=False)

                # Mandatory delay between each request or there will be corrupt data
                time.sleep(1)

                # Move to next pack
                pack_index += 1
                if pack_index >= len(app_state.battery_packs):
                    pack_index = 0
                    # System values (multi pack mode via the CAN port RS485 bus only)
                    _publish_system_data(system_data)
                    if Config.MQTT_UPDATE_INTERVAL > 0:
                        logger.info(
                            "Waiting %s seconds before next cycle",
                            Config.MQTT_UPDATE_INTERVAL
                        )
                        time.sleep(Config.MQTT_UPDATE_INTERVAL)

            except Exception as e:
                logger.error("Error in main loop: %s", e)
                time.sleep(10)

    except KeyboardInterrupt:
        logger.info("Shutdown requested via keyboard interrupt")
    except Exception as e:
        logger.error("Fatal error: %s", e)
    finally:
        graceful_exit()


if __name__ == "__main__":
    main()
