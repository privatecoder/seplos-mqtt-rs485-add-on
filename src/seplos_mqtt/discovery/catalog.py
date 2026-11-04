"""
Home Assistant entity catalog. Each Entity names the JSON key the decoders write
(value_json.<group>.<normal|binary>.<key>) plus its HA metadata. Binary payloads of the
telesignalization entities are taken from the decoder's bit table, so they can't drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..decoders import alarms

DIAG = "diagnostic"
ALWAYS = frozenset({"always"})
PACK_BUS = frozenset({"pack_bus"})
PACK_BUS_OR_MAINT_UART1 = frozenset({"pack_bus", "maint_uart1"})


@dataclass(frozen=True)
class Entity:
    key: str
    name: str
    group: str = ""                 # payload group: telemetry, telesignalization, status, info, system
    domain: str = "sensor"          # sensor | binary_sensor
    kind: str = "value"             # value | info | heartbeat | parameters
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    precision: int | None = None
    icon: str | None = None
    category: str | None = None     # entity_category
    invert: bool = False            # sign flips with invert_ha_dis_charge_measurements
    payload_on: str | None = None
    payload_off: str | None = None
    count: int = 0                  # > 0: key_1..key_n, "name 1".."name n"
    # published if any of these conditions holds for the pack: "always", "pack_bus" (RS485-1/2, single
    # pack mode) or a MAINT capability ("maint", "maint_storage", "maint_uart1"); removed otherwise
    when: frozenset = ALWAYS
    attributes: str | None = None   # Jinja expression for json_attr_tpl

    def expand(self) -> list[Entity]:
        if not self.count:
            return [self]
        return [replace(self, key=f"{self.key}_{i}", name=f"{self.name} {i}", count=0) for i in range(1, self.count + 1)]


def _sensor(key, name, group, **kw) -> Entity:
    return Entity(key=key, name=name, group=group, **kw)


def _alarm_binary(key: str, name: str, **kw) -> Entity:
    """Binary telesignalization entity; payloads follow the decoder's bit mode."""
    field = next(f for f in alarms.BIT_FIELDS if f.key == key)
    on, off = (None, None) if field.mode == "on_off" else field.payloads()
    return Entity(key=key, name=name, group="telesignalization", domain="binary_sensor",
                  payload_on=on, payload_off=off, category=DIAG, **kw)


V = dict(device_class="voltage", state_class="measurement", unit="V")
A = dict(device_class="current", state_class="measurement", unit="A")
T = dict(device_class="temperature", state_class="measurement", unit="°C")

T_ = "telemetry"
S_ = "telesignalization"

TELEMETRY: list[Entity] = [
    _sensor("voltage_cell", "Voltage Cell", T_, count=16, precision=3, icon="mdi:battery-outline", **V),
    _sensor("cell_temperature", "Cell Temperature", T_, count=4, precision=1, icon="mdi:thermometer", **T),
    _sensor("cell_voltage_setting_min", "Cell Voltage Range Min (Setting)", T_, device_class="voltage", unit="V", precision=3, icon="mdi:cog"),
    _sensor("cell_voltage_setting_max", "Cell Voltage Range Max (Setting)", T_, device_class="voltage", unit="V", precision=3, icon="mdi:cog"),
    _sensor("pack_voltage_setting_min", "Pack Voltage Range Min (Setting)", T_, device_class="voltage", unit="V", precision=2, icon="mdi:cog"),
    _sensor("pack_voltage_setting_max", "Pack Voltage Range Max (Setting)", T_, device_class="voltage", unit="V", precision=2, icon="mdi:cog"),
    _sensor("average_cell_voltage", "Average Cell Voltage", T_, precision=3, icon="mdi:chart-line", **V),
    _sensor("lowest_cell", "Lowest Cell No.", T_, icon="mdi:numeric"),
    _sensor("lowest_cell_voltage", "Lowest Cell Voltage", T_, precision=3, icon="mdi:arrow-down-thin", **V),
    _sensor("highest_cell", "Highest Cell No.", T_, icon="mdi:numeric"),
    _sensor("highest_cell_voltage", "Highest Cell Voltage", T_, precision=3, icon="mdi:arrow-up-thin", **V),
    _sensor("delta_cell_voltage", "Delta Cell Voltage", T_, precision=3, icon="mdi:delta", **V),
    _sensor("highest_cell_temperature", "Highest Cell Temperature", T_, precision=1, icon="mdi:thermometer-chevron-up", **T),
    _sensor("lowest_cell_temperature", "Lowest Cell Temperature", T_, precision=1, icon="mdi:thermometer-chevron-down", **T),
    _sensor("delta_cell_temperature", "Delta Cell Temperature", T_, device_class="temperature_delta", state_class="measurement",
            unit="°C", precision=1, icon="mdi:delta"),
    _sensor("ambient_temperature", "Ambient Temperature", T_, precision=1, icon="mdi:thermometer", **T),
    _sensor("components_temperature", "MOSFET Temperature", T_, precision=1, icon="mdi:thermometer", **T),
    _sensor("dis_charge_current", "Current (0 when idle)", T_, invert=True, precision=2, icon="mdi:current-dc", **A),
    _sensor("dis_charge_power", "Power (calculated)", T_, invert=True, device_class="power", state_class="measurement", unit="W",
            precision=2, icon="mdi:flash"),
    _sensor("total_pack_voltage", "Pack Voltage", T_, precision=2, icon="mdi:server", **V),
    _sensor("rated_capacity", "Rated Capacity (Setting)", T_, unit="Ah", precision=2, icon="mdi:battery"),
    _sensor("full_capacity", "Full Capacity", T_, unit="Ah", state_class="measurement", precision=2, icon="mdi:battery"),
    _sensor("residual_capacity", "Remaining Capacity", T_, unit="Ah", state_class="measurement", precision=2, icon="mdi:battery-50"),
    _sensor("state_of_charge", "State of Charge", T_, device_class="battery", state_class="measurement", unit="%", precision=1,
            icon="mdi:battery"),
    _sensor("charging_cycles", "Cycle Count", T_, unit="cycles", state_class="total_increasing", icon="mdi:counter"),
    _sensor("port_voltage", "Port Voltage (P+/P-)", T_, precision=2, icon="mdi:flash-triangle", **V),
    # Fields the protocol doc calls "Reservation" 1-4 (0x42)
    _sensor("current", "Current", T_, invert=True, precision=3, icon="mdi:current-dc", **A),
    _sensor("current_offset", "Current Sense Offset", T_, device_class="current", state_class="measurement", unit="mA", precision=0,
            icon="mdi:tune-variant", category=DIAG),
    _sensor("energy_charged", "Energy Charged (total)", T_, device_class="energy", state_class="total_increasing", unit="kWh",
            precision=1, icon="mdi:battery-arrow-up"),
    _sensor("energy_discharged", "Energy Discharged (total)", T_, device_class="energy", state_class="total_increasing", unit="kWh",
            precision=1, icon="mdi:battery-arrow-down"),
]

# refreshed on every successful poll, also when the data didn't change
HEARTBEAT = Entity(key="last_publish", name="Last Successful Poll", kind="heartbeat", device_class="timestamp", icon="mdi:update",
                   category=DIAG)

# Static device info (CID2 0x51 / 0xA2), published under "info"
INFO: list[Entity] = [
    Entity("device_name", "Device Name", group="info", kind="info", icon="mdi:information-outline", category=DIAG),
    Entity("firmware_version", "Firmware Version", group="info", kind="info", icon="mdi:chip", category=DIAG),
    Entity("firmware_patch", "Firmware Patch (derived)", group="info", kind="info", icon="mdi:chip", category=DIAG),
    Entity("can_protocol", "CAN Protocol", group="info", kind="info", icon="mdi:swap-horizontal", category=DIAG),
    Entity("serial_number", "Battery Serial Number", group="info", kind="info", icon="mdi:barcode", category=DIAG, when=PACK_BUS_OR_MAINT_UART1),
    Entity("bms_serial_number", "BMS Serial Number", group="info", kind="info", icon="mdi:barcode", category=DIAG,
           when=PACK_BUS_OR_MAINT_UART1),
]
FIRMWARE_ENTITY = INFO[1]

# Extended status from the intra-pack record (CID2 0x5A, RS485-1/2 only), published under "status"
STATUS: list[Entity] = [
    _sensor("dis_charge_current_unfiltered", "Current (incl. idle)", "status", invert=True, precision=2, icon="mdi:current-dc",
            when=PACK_BUS, **A),
    Entity("balancing_active", "Balancing Active", "status", "binary_sensor", device_class="running", category=DIAG, when=PACK_BUS),
    Entity("balancing_blocked", "Balancing Not Running (Blocked / Not Needed)", "status", "binary_sensor", category=DIAG,
           when=PACK_BUS),
    Entity("balancing_time_limit", "Balancing Time Limit Reached", "status", "binary_sensor", category=DIAG, when=PACK_BUS),
    Entity("balancing_temperature_blocked", "Balancing Blocked (Temperature)", "status", "binary_sensor", device_class="problem",
           category=DIAG, when=PACK_BUS),
    # normal during recharge waiting with hard stop or before a shutdown, so not a problem class
    Entity("charge_forbidden", "Charge Blocked (Charge MOSFET Off Request)", "status", "binary_sensor", category=DIAG,
           when=PACK_BUS),
    # active limiting (BitGroup6 bit 6): charging detected (normally from +0.6 A) or >= 10 A in the 5 ms path
    Entity("limiter_charging_from_10a", "Current Limiter Active (Charging Detected)", "status", "binary_sensor", category=DIAG,
           when=PACK_BUS),
    Entity("limiter_after_charge_overcurrent_warning", "Current Limiter Active (After Charge Overcurrent Warning)", "status",
           "binary_sensor", category=DIAG, when=PACK_BUS),
    Entity("precharge_running", "Precharge Running", "status", "binary_sensor", device_class="running", category=DIAG,
           when=PACK_BUS),
    Entity("external_voltage_detected", "External Voltage Detected (Charger)", "status", "binary_sensor", device_class="plug",
           category=DIAG, when=PACK_BUS),
    Entity("charge_current_status", "Charge Current Status (>= 3 A)", "status", "binary_sensor", category=DIAG, when=PACK_BUS),
    Entity("discharge_current_status", "Discharge Current Status (>= 3 A)", "status", "binary_sensor", category=DIAG,
           when=PACK_BUS),
    # all parameters (CID2 0x47) as attributes of one sensor, state: time of the last successful read
    Entity("parameters", "Parameters", kind="parameters", device_class="timestamp", icon="mdi:tune-vertical", category=DIAG,
           when=PACK_BUS_OR_MAINT_UART1),
]

TELESIGNALIZATION: list[Entity] = [
    _sensor("cell_voltage_alarm", "Voltage Alarm Cell", S_, count=16, icon="mdi:flash-alert", category=DIAG),
    _sensor("cell_temperature_alarm", "Cell Temperature Alarm", S_, count=4, icon="mdi:thermometer-alert", category=DIAG),
    Entity("balancer_cell", "Balancing Cell", S_, "binary_sensor", count=16, device_class="running", category=DIAG),
    _sensor("any_cell_voltage_alarm", "Alarm Cell Voltage", S_, icon="mdi:flash-alert"),
    _sensor("any_cell_temperature_alarm", "Alarm Cell Temperature", S_, icon="mdi:thermometer-alert"),
    _sensor("ambient_temperature_alarm", "Alarm Ambient Temp.", S_, icon="mdi:thermometer-alert"),
    _sensor("component_temperature_alarm", "Alarm MOSFET Temp.", S_, icon="mdi:thermometer-alert"),
    _sensor("dis_charging_current_alarm", "Alarm Dis-/Charge Current", S_, icon="mdi:flash-alert"),
    _sensor("pack_voltage_alarm", "Alarm Pack Voltage", S_, icon="mdi:flash-alert"),
    _sensor("system_status", "System Status", S_, icon="mdi:information-outline"),
    # alarm event 2..6 multi-state values
    _sensor("cell_overvoltage", "Cell Overvoltage", S_, icon="mdi:flash-alert", category=DIAG),
    _sensor("cell_voltage_low", "Cell Undervoltage", S_, icon="mdi:flash-alert", category=DIAG),
    _sensor("pack_overvoltage", "Pack Overvoltage", S_, icon="mdi:flash-alert", category=DIAG),
    _sensor("pack_voltage_low", "Pack Undervoltage", S_, icon="mdi:flash-alert", category=DIAG),
    _sensor("charging_temperature_high", "Charging Temp. High", S_, icon="mdi:thermometer-alert", category=DIAG),
    _sensor("charging_temperature_low", "Charging Temp. Low", S_, icon="mdi:snowflake-alert", category=DIAG),
    _sensor("discharging_temperature_high", "Discharging Temp. High", S_, icon="mdi:thermometer-alert", category=DIAG),
    _sensor("discharging_temperature_low", "Discharging Temp. Low", S_, icon="mdi:snowflake-alert", category=DIAG),
    _sensor("ambient_temperature_high", "Ambient Temp. High", S_, icon="mdi:thermometer-alert", category=DIAG),
    _sensor("ambient_temperature_low", "Ambient Temp. Low", S_, icon="mdi:snowflake-alert", category=DIAG),
    _sensor("component_temperature_high", "MOSFET Temp. High", S_, icon="mdi:thermometer-alert", category=DIAG),
    _sensor("charging_overcurrent", "Charging Overcurrent", S_, icon="mdi:flash-alert", category=DIAG),
    _sensor("discharging_overcurrent", "Discharging Overcurrent", S_, icon="mdi:flash-alert", category=DIAG),
    _sensor("transient_overcurrent", "Transient Overcurrent", S_, icon="mdi:flash-alert", category=DIAG),
    _sensor("output_short_circuit", "Output Short Circuit", S_, icon="mdi:flash-alert", category=DIAG),
    _sensor("soc_low", "Remaining Capacity Low", S_, icon="mdi:battery-alert-variant-outline", category=DIAG),
    # alarm event 1
    _alarm_binary("voltage_sensing_failure", "Voltage Sensing Failure", device_class="problem"),
    _alarm_binary("temperature_sensing_failure", "Temp. Sensing Failure", device_class="problem"),
    _alarm_binary("current_sensing_failure", "Current Sensing Failure", device_class="problem"),
    _alarm_binary("power_switch_failure", "Power Button Failure", device_class="problem"),
    _alarm_binary("cell_voltage_difference_fault", "Cell Voltage Difference Fault", device_class="problem"),
    _alarm_binary("charging_switch_failure", "Charge MOSFET Failure", device_class="problem"),
    _alarm_binary("discharging_switch_failure", "Discharge MOSFET Failure", device_class="problem"),
    _alarm_binary("current_limit_switch_failure", "Current Limiter Failure", device_class="problem"),
    # alarm event 4
    _alarm_binary("low_temperature_heating", "Cell Heating Requested", device_class="heat"),
    _alarm_binary("severe_fault", "Severe Fault (Latched Until Restart)", device_class="problem"),
    # alarm event 6
    _alarm_binary("charging_high_voltage_protection", "Charger Overvoltage Protection", device_class="problem"),
    # excludes the pack from the normal charge capability; the charge MOSFET stays on (soft stop)
    _alarm_binary("intermittent_power_supplement", "Intermittent Recharge Waiting"),
    _alarm_binary("cell_low_voltage_forbidden_charging", "Charging Blocked (Cell < Min.)", device_class="problem"),
    _alarm_binary("output_connection_failure", "Output Connection Failure", device_class="problem"),
    # alarm event 7: a charge activation window is running (keys kept for existing installations)
    _alarm_binary("auto_charging_wait", "Charge Activation Window Active (Auto)"),
    _alarm_binary("manual_charging_wait", "Charge Activation Window Active (Manual)"),
    # alarm event 8
    _alarm_binary("eep_storage_failure", "EEPROM Failure", device_class="problem"),
    _alarm_binary("rtc_clock_failure", "RTC Clock Failure", device_class="problem"),
    _alarm_binary("no_calibration_of_voltage", "No Calibration Of Voltage", device_class="problem"),
    _alarm_binary("no_calibration_of_current", "No Calibration Of Current", device_class="problem"),
    _alarm_binary("no_calibration_of_null_point", "Current Zero Point Not Calibrated", device_class="problem"),
    _alarm_binary("rtc_time_invalid", "RTC Time Invalid", device_class="problem"),
    # on-off state
    _alarm_binary("discharge_switch", "Discharge MOSFET", device_class="power"),
    _alarm_binary("charge_switch", "Charge MOSFET", device_class="power"),
    _alarm_binary("current_limit_switch", "Current Limiter", device_class="power"),
    _alarm_binary("heating_switch", "Heater", device_class="power"),
]

# MAINT 1.5.4 / 1.6.4 custom firmware (option maint_firmware), published under "maint"
MAINT_ATTRIBUTES = "{{ value_json.get('maint', {}).get('attributes', {}) | tojson }}"
MAINT: list[Entity] = [
    Entity("maint_version", "MAINT Firmware", "maint", icon="mdi:chip", category=DIAG, when=frozenset({"maint"}),
           attributes=MAINT_ATTRIBUTES),
    Entity("storage_problem", "Flash Storage Problem", "maint", "binary_sensor", device_class="problem", category=DIAG,
           when=frozenset({"maint_storage"})),
    # 1.6.4: runtime state of the persistent individual pack lock (64/04 cache), no HA "lock" class (ON would mean unlocked)
    Entity("pack_lock_active", "Individual Pack Lock Active", "maint", "binary_sensor", icon="mdi:lock", category=DIAG,
           when=frozenset({"maint_lock"})),
    # 64/02: the master stopped polling its slaves; the system values then cover the local pack only
    Entity("maintenance_mode", "Maintenance Mode (Slave Polling Paused)", "maint", "binary_sensor", icon="mdi:wrench",
           category=DIAG, when=frozenset({"maint_marker"})),
    Entity("effective_slave_count", "Effective Slave Count", "maint", icon="mdi:numeric", category=DIAG,
           when=frozenset({"maint_marker"})),
    # configuration (Group0 bits 6/7 + image default), not proof that charging is currently stopped
    Entity("recharge_stop_mode", "Recharge Waiting Stop Mode", "maint", icon="mdi:battery-sync", category=DIAG,
           when=frozenset({"maint_hard_stop"})),
]

# BMS real time clock (0x4D): OEM on RS485-1/2, with MAINT also on the CAN port
CLOCK: list[Entity] = [
    Entity("clock_offset", "BMS Clock Offset", "clock", device_class="duration", state_class="measurement", unit="s",
           icon="mdi:clock-alert-outline", category=DIAG, when=frozenset({"pack_bus", "maint_rtc"}),
           attributes="{{ value_json.get('clock', {}).get('normal', {}) | tojson }}"),
]

PACK_ENTITIES: list[Entity] = [*TELEMETRY, HEARTBEAT, *INFO, *STATUS, *TELESIGNALIZATION, *MAINT, *CLOCK]

Y = "system"
# System values from the master via Modbus 0x1000 (CAN port RS485 bus only), topic <topic>/system/sensors
SYSTEM: list[Entity] = [
    # aggregate of the master over all packs via Modbus; CAN/RS485 inverter profiles add their own rules
    Entity("system_state", "System State (Aggregate)", Y, icon="mdi:information-outline"),
    Entity("system_voltage", "System Voltage", Y, precision=1, **V),
    Entity("system_current", "System Current", Y, precision=1, invert=True, **A),
    Entity("system_soc", "System SOC", Y, device_class="battery", state_class="measurement", unit="%"),
    Entity("total_capacity", "Total Full Capacity", Y, unit="Ah", state_class="measurement", icon="mdi:battery"),
    Entity("charge_voltage_limit", "Charge Voltage Limit", Y, precision=1, **V),
    Entity("charge_current_limit", "Charge Current Limit", Y, precision=1, **A),
    Entity("discharge_current_limit", "Discharge Current Limit", Y, precision=1, **A),
    Entity("discharge_voltage_limit", "Discharge Voltage Limit", Y, precision=1, **V),
    Entity("highest_cell_voltage", "Highest Cell Voltage", Y, precision=3, **V),
    Entity("highest_cell_voltage_pack", "Highest Cell Voltage Pack", Y, icon="mdi:numeric"),
    Entity("lowest_cell_voltage", "Lowest Cell Voltage", Y, precision=3, **V),
    Entity("lowest_cell_voltage_pack", "Lowest Cell Voltage Pack", Y, icon="mdi:numeric"),
    Entity("highest_cell_temperature", "Highest Cell Temperature", Y, precision=1, **T),
    Entity("highest_cell_temperature_pack", "Highest Cell Temperature Pack", Y, icon="mdi:numeric"),
    Entity("lowest_cell_temperature", "Lowest Cell Temperature", Y, precision=1, **T),
    Entity("lowest_cell_temperature_pack", "Lowest Cell Temperature Pack", Y, icon="mdi:numeric"),
    Entity("alarm_word_current_voltage", "Alarm Word Current/Voltage", Y, icon="mdi:alert-outline", category=DIAG),
    Entity("alarm_word_temperature_misc", "Alarm Word Temperature/Misc", Y, icon="mdi:alert-outline", category=DIAG),
    Entity("charge_allowed", "Charge Allowed", Y, "binary_sensor", device_class="power"),
    Entity("discharge_allowed", "Discharge Allowed", Y, "binary_sensor", device_class="power"),
    Entity("pack_communication_fault", "Pack Communication Fault", Y, "binary_sensor", device_class="problem"),
    # FC03 0x0074 (16.06.11+): the master requests forced charging at low SOC
    Entity("force_charge_1", "Force Charge Request I (SOC <= 5 %)", Y, "binary_sensor", category=DIAG),
    Entity("force_charge_2", "Force Charge Request II (SOC <= 9 %)", Y, "binary_sensor", category=DIAG),
]

# Retained discovery configs of earlier versions that must be cleared: (domain, key)
OBSOLETE: list[tuple[str, str]] = [
    # state of health: the firmware always sends 100 %
    ("sensor", "state_of_health"),
    # renamed: min/max_cell_voltage -> cell_voltage_setting_min/max, min/max_pack_voltage -> pack_voltage_setting_min/max,
    # battery_capacity -> full_capacity, cell_voltage_difference_sensing_failure -> cell_voltage_difference_fault
    *[("sensor", k) for k in ("min_cell_voltage", "max_cell_voltage", "min_pack_voltage", "max_pack_voltage", "battery_capacity")],
    ("binary_sensor", "cell_voltage_difference_sensing_failure"),
    # "Reservation" fields, renamed to current_offset, current, energy_charged, energy_discharged
    *[("sensor", f"reserved_{i}") for i in range(1, 5)],
    # cell disconnection: the firmware always sends 0
    *[("binary_sensor", f"disconnection_cell_{i}") for i in range(1, 17)],
    # event 6 bit 5 "reverse polarity": no firmware code sets it
    ("binary_sensor", "output_reverse_polarity_protection"),
]

# per-cell entities, removed above the actual cell count
CELL_ENTITIES: list[tuple[str, str]] = [
    ("sensor", "voltage_cell"),
    ("sensor", "cell_voltage_alarm"),
    ("binary_sensor", "balancer_cell"),
]
