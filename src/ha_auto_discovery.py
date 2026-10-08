"""
Handle creation and publishing of auto discovery configs for Home Assistant.
"""
import json
import logging
import copy
from typing import Optional, Dict, Any, List

logger = logging.getLogger("SeplosBMS.Discovery")

# Base sensor template
BASE_SENSOR = {
    "name": "",
    "uniq_id": "",  # unique_id
    "obj_id": "",  # object_id (older HA versions)
    "stat_t": "",  # state_topic
    "val_tpl": "",  # value_template
    "avty": [],  # availability
    "dev": {}  # device
}

DEVICE_BASE_CONFIG = {
    "hw": "10C / 10E",  # hw_version
    "sw": "2.x / 16.x",  # sw_version
    "mdl": "BMS V14 / V16",  # model
    "mf": "Seplos"  # manufacturer
}

# Telemetry sensor templates
TELEMETRY_SENSOR_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "Cell Voltage Range Min (Setting)",
        "value_template_key": "cell_voltage_setting_min",
        "device_class": "voltage",
        "unit_of_measurement": "V",
        "suggested_display_precision": 3,
        "icon": "mdi:cog"
    },
    {
        "name": "Cell Voltage Range Max (Setting)",
        "value_template_key": "cell_voltage_setting_max",
        "device_class": "voltage",
        "unit_of_measurement": "V",
        "suggested_display_precision": 3,
        "icon": "mdi:cog"
    },
    {
        "name": "Pack Voltage Range Min (Setting)",
        "value_template_key": "pack_voltage_setting_min",
        "device_class": "voltage",
        "unit_of_measurement": "V",
        "suggested_display_precision": 2,
        "icon": "mdi:cog"
    },
    {
        "name": "Pack Voltage Range Max (Setting)",
        "value_template_key": "pack_voltage_setting_max",
        "device_class": "voltage",
        "unit_of_measurement": "V",
        "suggested_display_precision": 2,
        "icon": "mdi:cog"
    },
    {
        "name": "Average Cell Voltage",
        "value_template_key": "average_cell_voltage",
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "suggested_display_precision": 3,
        "icon": "mdi:chart-line"
    },
    {
        "name": "Lowest Cell No.",
        "value_template_key": "lowest_cell",
        "icon": "mdi:numeric"
    },
    {
        "name": "Lowest Cell Voltage",
        "value_template_key": "lowest_cell_voltage",
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "suggested_display_precision": 3,
        "icon": "mdi:arrow-down-thin"
    },
    {
        "name": "Highest Cell No.",
        "value_template_key": "highest_cell",
        "icon": "mdi:numeric"
    },
    {
        "name": "Highest Cell Voltage",
        "value_template_key": "highest_cell_voltage",
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "suggested_display_precision": 3,
        "icon": "mdi:arrow-up-thin"
    },
    {
        "name": "Delta Cell Voltage",
        "value_template_key": "delta_cell_voltage",
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "suggested_display_precision": 3,
        "icon": "mdi:delta"
    },
    {
        "name": "Delta Cell Temperature",
        "value_template_key": "delta_cell_temperature",
        "device_class": "temperature_delta",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "suggested_display_precision": 1,
        "icon": "mdi:delta"
    },
    {
        "name": "Ambient Temperature",
        "value_template_key": "ambient_temperature",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "suggested_display_precision": 1,
        "icon": "mdi:thermometer"
    },
    {
        "name": "MOSFET Temperature",
        "value_template_key": "components_temperature",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "suggested_display_precision": 1,
        "icon": "mdi:thermometer"
    },
    {
        "name": "Current (0 when idle)",
        "value_template_key": "dis_charge_current",
        "invert_value": True,
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "A",
        "suggested_display_precision": 2,
        "icon": "mdi:current-dc"
    },
    {
        "name": "Power (calculated)",
        "value_template_key": "dis_charge_power",
        "invert_value": True,
        "device_class": "power",
        "state_class": "measurement",
        "unit_of_measurement": "W",
        "suggested_display_precision": 2,
        "icon": "mdi:flash"
    },
    {
        "name": "Pack Voltage",
        "value_template_key": "total_pack_voltage",
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "suggested_display_precision": 2,
        "icon": "mdi:server"
    },
    {
        "name": "Rated Capacity (Setting)",
        "value_template_key": "rated_capacity",
        "unit_of_measurement": "Ah",
        "suggested_display_precision": 2,
        "icon": "mdi:battery"
    },
    {
        "name": "Full Capacity",
        "value_template_key": "full_capacity",
        "unit_of_measurement": "Ah",
        "state_class": "measurement",
        "suggested_display_precision": 2,
        "icon": "mdi:battery"
    },
    {
        "name": "Remaining Capacity",
        "value_template_key": "residual_capacity",
        "state_class": "measurement",
        "unit_of_measurement": "Ah",
        "suggested_display_precision": 2,
        "icon": "mdi:battery-50"
    },
    {
        "name": "State of Charge",
        "value_template_key": "state_of_charge",
        "device_class": "battery",
        "state_class": "measurement",
        "unit_of_measurement": "%",
        "suggested_display_precision": 1,
        "icon": "mdi:battery"
    },
    {
        "name": "Cycle Count",
        "value_template_key": "charging_cycles",
        "unit_of_measurement": "cycles",
        "state_class": "total_increasing",
        "icon": "mdi:counter"
    },
    {
        "name": "Port Voltage (P+/P-)",
        "value_template_key": "port_voltage",
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "suggested_display_precision": 2,
        "icon": "mdi:flash-triangle"
    },
    # Fields the protocol doc calls "Reservation" 1-4 (0x42)
    {
        "name": "Current",
        "value_template_key": "current",
        "invert_value": True,
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "A",
        "suggested_display_precision": 3,
        "icon": "mdi:current-dc"
    },
    {
        "name": "Current Sense Offset",
        "value_template_key": "current_offset",
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "mA",
        "suggested_display_precision": 0,
        "icon": "mdi:tune-variant",
        "entity_category": "diagnostic"
    },
    {
        "name": "Energy Charged (total)",
        "value_template_key": "energy_charged",
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": "kWh",
        "suggested_display_precision": 1,
        "icon": "mdi:battery-arrow-up"
    },
    {
        "name": "Energy Discharged (total)",
        "value_template_key": "energy_discharged",
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": "kWh",
        "suggested_display_precision": 1,
        "icon": "mdi:battery-arrow-down"
    }
]

# Extended status from the intra-pack record (CID2 0x5A, RS485-1/2 only), published under "status"
STATUS_SENSOR_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "Current (incl. idle)",
        "value_template_key": "dis_charge_current_unfiltered",
        "invert_value": True,
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "A",
        "suggested_display_precision": 2,
        "icon": "mdi:current-dc"
    },
    {
        "name": "Highest Cell Temperature",
        "value_template_key": "highest_cell_temperature",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "suggested_display_precision": 1,
        "icon": "mdi:thermometer-chevron-up"
    },
    {
        "name": "Lowest Cell Temperature",
        "value_template_key": "lowest_cell_temperature",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "suggested_display_precision": 1,
        "icon": "mdi:thermometer-chevron-down"
    }
]

STATUS_BINARY_SENSOR_TEMPLATES: List[Dict[str, Any]] = [
    {"name": "Balancing Active", "value_template_key": "balancing_active", "device_class": "running", "entity_category": "diagnostic"},
    {"name": "Balancing Not Running (Blocked / Not Needed)", "value_template_key": "balancing_blocked", "entity_category": "diagnostic"},
    {"name": "Balancing Time Limit Reached", "value_template_key": "balancing_time_limit", "device_class": "problem", "entity_category": "diagnostic"},
    {"name": "Balancing Blocked (Temperature)", "value_template_key": "balancing_temperature_blocked", "device_class": "problem", "entity_category": "diagnostic"},
    {"name": "Charge Blocked (Charge MOSFET Off Request)", "value_template_key": "charge_forbidden", "device_class": "problem", "entity_category": "diagnostic"}
]

# System values from the master via Modbus 0x1000 (CAN port RS485 bus only), topic <topic>/system/sensors
SYSTEM_SENSOR_TEMPLATES: List[Dict[str, Any]] = [
    {"name": "System State (as reported to inverter)", "key": "system_state", "icon": "mdi:information-outline"},
    {"name": "System Voltage", "key": "system_voltage", "device_class": "voltage", "state_class": "measurement", "unit": "V", "precision": 1},
    {"name": "System Current", "key": "system_current", "device_class": "current", "state_class": "measurement", "unit": "A", "precision": 1, "invert": True},
    {"name": "System SOC", "key": "system_soc", "device_class": "battery", "state_class": "measurement", "unit": "%"},
    {"name": "Total Capacity", "key": "total_capacity", "unit": "Ah", "icon": "mdi:battery"},
    {"name": "Charge Voltage Limit", "key": "charge_voltage_limit", "device_class": "voltage", "state_class": "measurement", "unit": "V", "precision": 1},
    {"name": "Charge Current Limit", "key": "charge_current_limit", "device_class": "current", "state_class": "measurement", "unit": "A", "precision": 1},
    {"name": "Discharge Current Limit", "key": "discharge_current_limit", "device_class": "current", "state_class": "measurement", "unit": "A", "precision": 1},
    {"name": "Discharge Voltage Limit", "key": "discharge_voltage_limit", "device_class": "voltage", "state_class": "measurement", "unit": "V", "precision": 1},
    {"name": "Highest Cell Voltage", "key": "highest_cell_voltage", "device_class": "voltage", "state_class": "measurement", "unit": "V", "precision": 3},
    {"name": "Highest Cell Voltage Pack", "key": "highest_cell_voltage_pack", "icon": "mdi:numeric"},
    {"name": "Lowest Cell Voltage", "key": "lowest_cell_voltage", "device_class": "voltage", "state_class": "measurement", "unit": "V", "precision": 3},
    {"name": "Lowest Cell Voltage Pack", "key": "lowest_cell_voltage_pack", "icon": "mdi:numeric"},
    {"name": "Highest Cell Temperature", "key": "highest_cell_temperature", "device_class": "temperature", "state_class": "measurement", "unit": "°C", "precision": 1},
    {"name": "Highest Cell Temperature Pack", "key": "highest_cell_temperature_pack", "icon": "mdi:numeric"},
    {"name": "Lowest Cell Temperature", "key": "lowest_cell_temperature", "device_class": "temperature", "state_class": "measurement", "unit": "°C", "precision": 1},
    {"name": "Lowest Cell Temperature Pack", "key": "lowest_cell_temperature_pack", "icon": "mdi:numeric"},
    {"name": "Alarm Word Current/Voltage", "key": "alarm_word_current_voltage", "icon": "mdi:alert-outline", "entity_category": "diagnostic"},
    {"name": "Alarm Word Temperature/Misc", "key": "alarm_word_temperature_misc", "icon": "mdi:alert-outline", "entity_category": "diagnostic"}
]

SYSTEM_BINARY_SENSOR_TEMPLATES: List[Dict[str, Any]] = [
    {"name": "Charge Allowed", "key": "charge_allowed", "device_class": "power"},
    {"name": "Discharge Allowed", "key": "discharge_allowed", "device_class": "power"},
    {"name": "Pack Communication Fault", "key": "pack_communication_fault", "device_class": "problem"}
]

# Static device info sensors (CID2 0x51 / 0xA2), published under "info"
INFO_SENSOR_TEMPLATES: List[Dict[str, Any]] = [
    {"name": "Device Name", "value_template_key": "device_name", "icon": "mdi:information-outline"},
    {"name": "Firmware Version", "value_template_key": "firmware_version", "icon": "mdi:chip"},
    {"name": "Firmware Patch (derived)", "value_template_key": "firmware_patch", "icon": "mdi:chip"},
    {"name": "CAN Protocol", "value_template_key": "can_protocol", "icon": "mdi:swap-horizontal"},
    {"name": "Serial Number", "value_template_key": "serial_number", "icon": "mdi:barcode"}
]

# Telesignalization sensor templates
TELESIGNALIZATION_SENSOR_TEMPLATES: List[Dict[str, Any]] = [
    ## Info data sensors

    # Individual cell voltage warnings via create_similar_sensor_config

    {
        "name": "Alarm Cell Voltage",
        "value_template_key": "any_cell_voltage_alarm",
        "icon": "mdi:flash-alert"
    },

    # Individual cell temperature warnings via create_similar_sensor_config

    {
        "name": "Alarm Cell Temperature",
        "value_template_key": "any_cell_temperature_alarm",
        "icon": "mdi:thermometer-alert"
    },
    {
        "name": "Alarm Ambient Temp.",
        "value_template_key": "ambient_temperature_alarm",
        "icon": "mdi:thermometer-alert"
    },
    {
        "name": "Alarm MOSFET Temp.",
        "value_template_key": "component_temperature_alarm",
        "icon": "mdi:thermometer-alert"
    },
    {
        "name": "Alarm Dis-/Charge Current",
        "value_template_key": "dis_charging_current_alarm",
        "icon": "mdi:flash-alert"
    },
    {
        "name": "Alarm Pack Voltage",
        "value_template_key": "pack_voltage_alarm",
        "icon": "mdi:flash-alert"
    },
    {
        "name": "System Status",
        "value_template_key": "system_status",
        "icon": "mdi:information-outline"
    },

    ## Diagnostic data sensors

    # Warning 2
    {
        "name": "Cell Overvoltage",
        "value_template_key": "cell_overvoltage",
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Cell Undervoltage",
        "value_template_key": "cell_voltage_low",
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic"
    },

    {
        "name": "Pack Overvoltage",
        "value_template_key": "pack_overvoltage",
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Pack Undervoltage",
        "value_template_key": "pack_voltage_low",
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic"
    },

    # Warning 3
    {
        "name": "Charging Temp. High",
        "value_template_key": "charging_temperature_high",
        "icon": "mdi:thermometer-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Charging Temp. Low",
        "value_template_key": "charging_temperature_low",
        "icon": "mdi:snowflake-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Discharging Temp. High",
        "value_template_key": "discharging_temperature_high",
        "icon": "mdi:thermometer-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Discharging Temp. Low",
        "value_template_key": "discharging_temperature_low",
        "icon": "mdi:snowflake-alert",
        "entity_category": "diagnostic"
    },

    # Warning 4
    {
        "name": "Ambient Temp. High",
        "value_template_key": "ambient_temperature_high",
        "icon": "mdi:thermometer-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Ambient Temp. Low",
        "value_template_key": "ambient_temperature_low",
        "icon": "mdi:snowflake-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "MOSFET Temp. High",
        "value_template_key": "component_temperature_high",
        "icon": "mdi:thermometer-alert",
        "entity_category": "diagnostic"
    },

    # Warning 5
    {
        "name": "Charging Overcurrent",
        "value_template_key": "charging_overcurrent",
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Discharging Overcurrent",
        "value_template_key": "discharging_overcurrent",
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Transient Overcurrent",
        "value_template_key": "transient_overcurrent",
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic"
    },
    {
        "name": "Output Short Circuit",
        "value_template_key": "output_short_circuit",
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic"
    },

    # Warning 6
    {
        "name": "Remaining Capacity Low",
        "value_template_key": "soc_low",
        "icon": "mdi:battery-alert-variant-outline",
        "entity_category": "diagnostic"
    }
]

TELESIGNALIZATION_BINARY_SENSOR_TEMPLATES: List[Dict[str, Any]] = [
    ## Warning group sensors

    # Warning 1
    {
        "name": "Voltage Sensing Failure",
        "value_template_key": "voltage_sensing_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "Temp. Sensing Failure",
        "value_template_key": "temperature_sensing_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "Current Sensing Failure",
        "value_template_key": "current_sensing_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "Power Button Failure",
        "value_template_key": "power_switch_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "Cell Voltage Difference Fault",
        "value_template_key": "cell_voltage_difference_fault",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "Charge MOSFET Failure",
        "value_template_key": "charging_switch_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "Discharge MOSFET Failure",
        "value_template_key": "discharging_switch_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "Current Limiter Failure",
        "value_template_key": "current_limit_switch_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },

    # Warning 4
    {
        "name": "Cell Heating Requested",
        "value_template_key": "low_temperature_heating",
        "entity_category": "diagnostic",
        "device_class": "heat"
    },

    # Warning 6
    {
        "name": "Charger Overvoltage Protection",
        "value_template_key": "charging_high_voltage_protection",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Protection",
        "payload_off": "OK"
    },
    {
        "name": "Charging Paused (Intermittent Charge)",
        "value_template_key": "intermittent_power_supplement",
        "entity_category": "diagnostic",
        "payload_on": "Warning",
        "payload_off": "OK"
    },
    {
        "name": "Charging Blocked (Cell < Min.)",
        "value_template_key": "cell_low_voltage_forbidden_charging",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Protection",
        "payload_off": "OK"
    },
    {
        "name": "Output Reverse Polarity Protection",
        "value_template_key": "output_reverse_polarity_protection",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Protection",
        "payload_off": "OK"
    },
    {
        "name": "Output Connection Failure",
        "value_template_key": "output_connection_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },

    # Warning 7
    {
        "name": "Charge Activation Waiting (Auto)",
        "value_template_key": "auto_charging_wait",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Warning",
        "payload_off": "OK"
    },
    {
        "name": "Charge Activation Waiting (Manual)",
        "value_template_key": "manual_charging_wait",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Warning",
        "payload_off": "OK"
    },

    # Warning 8
    {
        "name": "EEPROM Failure",
        "value_template_key": "eep_storage_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "RTC Clock Failure",
        "value_template_key": "rtc_clock_failure",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Fault",
        "payload_off": "OK"
    },
    {
        "name": "No Calibration Of Voltage",
        "value_template_key": "no_calibration_of_voltage",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Warning",
        "payload_off": "OK"
    },
    {
        "name": "No Calibration Of Current",
        "value_template_key": "no_calibration_of_current",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Warning",
        "payload_off": "OK"
    },
    {
        "name": "Current Zero Point Not Calibrated",
        "value_template_key": "no_calibration_of_null_point",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "Warning",
        "payload_off": "OK"
    },

    # Switch status
    {
        "name": "Discharge MOSFET",
        "value_template_key": "discharge_switch",
        "entity_category": "diagnostic",
        "device_class": "power"
    },
    {
        "name": "Charge MOSFET",
        "value_template_key": "charge_switch",
        "entity_category": "diagnostic",
        "device_class": "power"
    },
    {
        "name": "Current Limiter",
        "value_template_key": "current_limit_switch",
        "entity_category": "diagnostic",
        "device_class": "power"
    },
    {
        "name": "Heater",
        "value_template_key": "heating_switch",
        "entity_category": "diagnostic",
        "device_class": "power"
    },

    # Balancer status via create_similar_sensor_config

    # Disconnection status via create_similar_sensor_config
]


class AutoDiscoveryConfig:
    """Handle Home Assistant auto-discovery configuration creation and publishing."""

    def __init__(self, mqtt_topic: str, discovery_prefix: str, invert_ha_dis_charge_measurements: bool, mqtt_client) -> None:
        """
        Initialize AutoDiscoveryConfig.

        Args:
            mqtt_topic: MQTT topic where sensor data gets published
            discovery_prefix: Discovery prefix for Home Assistant (defaults to 'homeassistant')
            invert_ha_dis_charge_measurements: Inverts dis-/charge values for power and current
            mqtt_client: MQTT client instance for publishing
        """
        self.mqtt_topic = mqtt_topic
        self.discovery_prefix = discovery_prefix
        self.invert_ha_dis_charge_measurements = invert_ha_dis_charge_measurements
        self.mqtt_client = mqtt_client
        self._device_info_published = set()

    # -------------------------------------------------------------------------
    # Interne Hilfsfunktionen zur Vereinheitlichung
    # -------------------------------------------------------------------------

    def _add_device_info(self, entity: Dict[str, Any], pack_no: int) -> None:
        """Setze passende device-Infos für das gegebene Pack."""
        if pack_no not in self._device_info_published:
            entity["dev"] = {**DEVICE_BASE_CONFIG}
            entity["dev"]["name"] = f"Seplos BMS Pack-{pack_no} ({'Master' if pack_no == 0 else 'Slave'})"
            entity["dev"]["ids"] = f"seplos_bms_pack_{pack_no}"
            if pack_no > 0:
                entity["dev"]["via_device"] = "seplos_bms_pack_0"
            self._device_info_published.add(pack_no)
        else:
            entity["dev"] = {"ids": f"seplos_bms_pack_{pack_no}"}
            if pack_no > 0:
                entity["dev"]["via_device"] = "seplos_bms_pack_0"

    def _build_availability(self, pack_no: int) -> List[Dict[str, str]]:
        return [
            {"t": f"{self.mqtt_topic}/availability"},
            {"t": f"{self.mqtt_topic}/pack-{pack_no}/availability"},
        ]

    def _build_base_entity(
        self,
        pack_no: int,
        name: str,
        value_template: str,
        uniq_obj_id: str,
        state_topic: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Erzeuge Grundstruktur eines Sensors/Binary Sensors basierend auf BASE_SENSOR."""
        entity = copy.deepcopy(BASE_SENSOR)

        # Device-Infos
        self._add_device_info(entity, pack_no)

        # Required fields
        entity["name"] = name
        entity["avty"] = self._build_availability(pack_no)
        entity["avty_mode"] = "all"
        entity["stat_t"] = state_topic or f"{self.mqtt_topic}/pack-{pack_no}/sensors"
        entity["val_tpl"] = value_template
        entity["uniq_id"] = uniq_obj_id
        entity["obj_id"] = uniq_obj_id

        return entity

    def _apply_optional_fields(self, entity: Dict[str, Any], optional_fields: Dict[str, Any]) -> None:
        """Füge optionale Felder hinzu, wenn sie nicht None sind."""
        for key, value in optional_fields.items():
            if value is not None:
                entity[key] = value

    def _publish_config(
        self,
        entity_type: str,
        pack_no: int,
        name: str,
        value_template_key: str,
        config: Dict[str, Any],
    ) -> None:
        """Generische Publish-Funktion für Sensoren und Binary-Sensoren."""
        discovery_topic = f"{self.discovery_prefix}/{entity_type}/seplos-mqtt-pack-{pack_no}/{value_template_key}/config"
        self._set_default_entity_id(entity_type, config)

        try:
            self.mqtt_client.publish(
                discovery_topic,
                json.dumps(config),
                retain=True,
                qos=1
            )
            logger.debug(
                "Published discovery config for pack %s, %s: %s",
                pack_no,
                entity_type,
                name,
            )
        except Exception as e:
            logger.error("Failed to publish discovery config: %s", e)

    # -------------------------------------------------------------------------
    # Build-Funktionen
    # -------------------------------------------------------------------------

    def _build_binary_sensor_config(
        self,
        pack_no: int,
        name: str,
        value_template_group: str,
        value_template_key: str,
        icon: Optional[str] = None,
        entity_category: Optional[str] = None,
        device_class: Optional[str] = None,
        payload_on: Optional[str] = None,
        payload_off: Optional[str] = None,
        options: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Build binary sensor configuration dictionary.
        """
        value_template = f"{{{{ value_json.{value_template_group}.binary.{value_template_key} }}}}"
        binary_sensor = self._build_base_entity(
            pack_no=pack_no,
            name=name,
            value_template=value_template,
            uniq_obj_id = f"seplos_bms_pack_{pack_no}_{value_template_key}",
        )

        optional_fields = {
            "ic": icon,
            "ent_cat": entity_category,
            "dev_cla": device_class,
            "pl_on": payload_on,
            "pl_off": payload_off,
            "ops": options
        }
        self._apply_optional_fields(binary_sensor, optional_fields)

        return binary_sensor

    def _build_sensor_config(
        self,
        pack_no: int,
        name: str,
        value_template_group: str,
        value_template_key: str,
        invert_value: Optional[bool] = False,
        unit_of_measurement: Optional[str] = None,
        suggested_display_precision: Optional[int] = None,
        icon: Optional[str] = None,
        device_class: Optional[str] = None,
        state_class: Optional[str] = None,
        entity_category: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Build sensor configuration dictionary.
        """

        value_template_expr = f"value_json.{value_template_group}.normal.{value_template_key}"

        sensor = self._build_base_entity(
            pack_no=pack_no,
            name=name,
            value_template=f"{{{{ ({value_template_expr} | float) * -1 }}}}" if invert_value and self.invert_ha_dis_charge_measurements else f"{{{{ {value_template_expr} }}}}",
            uniq_obj_id=f"seplos_bms_pack_{pack_no}_{value_template_key}",
        )

        optional_fields = {
            "stat_cla": state_class,
            "unit_of_meas": unit_of_measurement,
            "sug_dsp_prc": suggested_display_precision,
            "ic": icon,
            "ent_cat": entity_category,
            "dev_cla": device_class
        }
        self._apply_optional_fields(sensor, optional_fields)

        return sensor

    # -------------------------------------------------------------------------
    # Publish-Funktionen (API unverändert)
    # -------------------------------------------------------------------------

    def _publish_binary_sensor_config(
        self,
        pack_no: int,
        binary_sensor_name: str,
        value_template_key: str,
        binary_sensor_config: Dict[str, Any]
    ) -> None:
        """
        Publish binary sensor configuration to MQTT.
        """
        self._publish_config(
            entity_type="binary_sensor",
            pack_no=pack_no,
            name=binary_sensor_name,
            value_template_key=value_template_key,
            config=binary_sensor_config,
        )

    def _publish_sensor_config(
        self,
        pack_no: int,
        sensor_name: str,
        value_template_key: str,
        sensor_config: Dict[str, Any]
    ) -> None:
        """
        Publish sensor configuration to MQTT.
        """
        self._publish_config(
            entity_type="sensor",
            pack_no=pack_no,
            name=sensor_name,
            value_template_key=value_template_key,
            config=sensor_config,
        )

    # -------------------------------------------------------------------------
    # Öffentliche Erzeugungs-Funktionen
    # -------------------------------------------------------------------------

    def create_binary_sensor_config(
        self,
        pack_no: int,
        name: str,
        value_template_group: str,
        value_template_key: str,
        icon: Optional[str] = None,
        device_class: Optional[str] = None,
        entity_category: Optional[str] = None,
        payload_on: Optional[str] = None,
        payload_off: Optional[str] = None,
        options: Optional[List[str]] = None
    ) -> None:
        """
        Create and publish unique binary sensor configuration.
        """

        logger.debug(
            "Creating auto-discovery binary sensors for pack %s",
            pack_no,
        )

        binary_sensor_config = self._build_binary_sensor_config(
            pack_no=pack_no,
            value_template_group=value_template_group,
            name=name,
            value_template_key=value_template_key,
            icon=icon,
            device_class=device_class,
            entity_category=entity_category,
            payload_on=payload_on,
            payload_off=payload_off,
            options=options
        )

        self._publish_binary_sensor_config(pack_no, name, value_template_key, binary_sensor_config)

        logger.debug(
            "Auto-discovery binary sensors published for pack %s",
            pack_no,
        )

    def create_sensor_config(
        self,
        pack_no: int,
        value_template_group: str,
        name: str,
        value_template_key: str,
        invert_value: Optional[bool] = False,
        unit_of_measurement: Optional[str] = None,
        suggested_display_precision: Optional[int] = None,
        icon: Optional[str] = None,
        device_class: Optional[str] = None,
        state_class: Optional[str] = None,
        entity_category: Optional[str] = None
    ) -> None:
        """
        Create and publish unique sensor configuration.
        """

        logger.debug(
            "Creating auto-discovery sensors for pack %s",
            pack_no,
        )


        sensor_config = self._build_sensor_config(
            pack_no=pack_no,
            value_template_group=value_template_group,
            name=name,
            value_template_key=value_template_key,
            invert_value=invert_value,
            unit_of_measurement=unit_of_measurement,
            suggested_display_precision=suggested_display_precision,
            icon=icon,
            device_class=device_class,
            state_class=state_class,
            entity_category=entity_category
        )

        self._publish_sensor_config(pack_no, name, value_template_key, sensor_config)

        logger.debug(
            "Auto-discovery sensors published for pack %s",
            pack_no,
        )

    def create_heartbeat_sensor_config(self, pack_no: int) -> None:
        """Create and publish a heartbeat sensor for the pack."""
        name = "Last Publish"
        value_template_key = "last_publish"
        state_topic = f"{self.mqtt_topic}/pack-{pack_no}/heartbeat"
        value_template = "{{ value_json.last_publish }}"

        sensor = self._build_base_entity(
            pack_no=pack_no,
            name=name,
            value_template=value_template,
            uniq_obj_id=f"seplos_bms_pack_{pack_no}_{value_template_key}",
            state_topic=state_topic,
        )

        self._apply_optional_fields(
            sensor,
            {
                "ic": "mdi:update",
                "ent_cat": "diagnostic",
            },
        )

        self._publish_sensor_config(pack_no, name, value_template_key, sensor)

    def create_info_sensor_config(self, pack_no: int, name: str, value_template_key: str, icon: Optional[str] = None) -> None:
        """Create and publish a static info sensor (tolerates a missing 'info' block in the payload)."""
        value_template = (
            f"{{{{ value_json.get('info', {{}}).get('normal', {{}}).get('{value_template_key}', 'unknown') }}}}"
        )
        sensor = self._build_base_entity(
            pack_no=pack_no,
            name=name,
            value_template=value_template,
            uniq_obj_id=f"seplos_bms_pack_{pack_no}_{value_template_key}",
        )
        self._apply_optional_fields(sensor, {"ic": icon, "ent_cat": "diagnostic"})
        self._publish_sensor_config(pack_no, name, value_template_key, sensor)

    def create_parameter_sensor_config(self, pack_no: int) -> None:
        """
        Create one diagnostic sensor carrying all parameters (CID2 0x47) as attributes.
        State: number of parameters, attributes: name -> value.
        """
        name = "Parameters"
        value_template_key = "parameters"
        sensor = self._build_base_entity(
            pack_no=pack_no,
            name=name,
            value_template="{{ value_json.get('parameters', {}) | length }}",
            uniq_obj_id=f"seplos_bms_pack_{pack_no}_{value_template_key}",
        )
        self._apply_optional_fields(sensor, {
            "ic": "mdi:tune-vertical",
            "ent_cat": "diagnostic",
            "json_attr_t": f"{self.mqtt_topic}/pack-{pack_no}/sensors",
            "json_attr_tpl": "{{ value_json.get('parameters', {}) | tojson }}",
        })
        self._publish_sensor_config(pack_no, name, value_template_key, sensor)

    def _remove_configs(self, entity_type: str, pack_no: int, keys: List[str]) -> None:
        """Publish empty retained discovery configs to remove entities."""
        for key in keys:
            topic = f"{self.discovery_prefix}/{entity_type}/seplos-mqtt-pack-{pack_no}/{key}/config"
            try:
                self.mqtt_client.publish(topic, "", retain=True, qos=1)
            except Exception as e:
                logger.error("Failed to remove discovery config %s: %s", topic, e)

    def _system_entity(self, entity_type: str, config: Dict[str, Any], value_template: str) -> Dict[str, Any]:
        """Build an entity of the system device (values of the master as seen by the inverter)."""
        key = config["key"]
        entity = copy.deepcopy(BASE_SENSOR)
        entity["name"] = config["name"]
        entity["uniq_id"] = f"seplos_bms_system_{key}"
        entity["obj_id"] = f"seplos_bms_system_{key}"
        entity["stat_t"] = f"{self.mqtt_topic}/system/sensors"
        entity["val_tpl"] = value_template
        entity["avty"] = [
            {"t": f"{self.mqtt_topic}/availability"},
            {"t": f"{self.mqtt_topic}/system/availability"},
        ]
        entity["avty_mode"] = "all"
        if "system" not in self._device_info_published:
            entity["dev"] = {**DEVICE_BASE_CONFIG, "name": "Seplos BMS System", "ids": "seplos_bms_system", "via_device": "seplos_bms_pack_0"}
            self._device_info_published.add("system")
        else:
            entity["dev"] = {"ids": "seplos_bms_system"}
        return entity

    def create_system_sensors(self) -> None:
        """Create the sensors of the system device (Modbus 0x1000 of the master)."""
        self._device_info_published.discard("system")
        for config in SYSTEM_SENSOR_TEMPLATES:
            expr = f"value_json.normal.{config['key']}"
            if config.get("invert") and self.invert_ha_dis_charge_measurements:
                template = f"{{{{ ({expr} | float) * -1 }}}}"
            else:
                template = f"{{{{ {expr} }}}}"
            entity = self._system_entity("sensor", config, template)
            self._apply_optional_fields(entity, {
                "dev_cla": config.get("device_class"),
                "stat_cla": config.get("state_class"),
                "unit_of_meas": config.get("unit"),
                "sug_dsp_prc": config.get("precision"),
                "ic": config.get("icon"),
                "ent_cat": config.get("entity_category"),
            })
            self._publish_raw_config("sensor", config["key"], entity)
        for config in SYSTEM_BINARY_SENSOR_TEMPLATES:
            entity = self._system_entity("binary_sensor", config, f"{{{{ value_json.binary.{config['key']} }}}}")
            self._apply_optional_fields(entity, {"dev_cla": config.get("device_class")})
            self._publish_raw_config("binary_sensor", config["key"], entity)

    def remove_system_sensors(self) -> None:
        """Remove the system device sensors (when not running on the CAN port RS485 bus)."""
        for entity_type, templates in (("sensor", SYSTEM_SENSOR_TEMPLATES), ("binary_sensor", SYSTEM_BINARY_SENSOR_TEMPLATES)):
            for config in templates:
                self._publish_raw_config(entity_type, config["key"], None)

    @staticmethod
    def _set_default_entity_id(entity_type: str, config: Optional[Dict[str, Any]]) -> None:
        """Newer HA versions ignore object_id and derive the entity ID from the device and entity
        name; default_entity_id keeps the entity IDs stable (seplos_bms_pack_<n>_<key>)."""
        if config and config.get("obj_id"):
            config["default_entity_id"] = f"{entity_type}.{config['obj_id']}"

    def _publish_raw_config(self, entity_type: str, key: str, config: Optional[Dict[str, Any]]) -> None:
        topic = f"{self.discovery_prefix}/{entity_type}/seplos-mqtt-system/{key}/config"
        self._set_default_entity_id(entity_type, config)
        try:
            self.mqtt_client.publish(topic, json.dumps(config) if config is not None else "", retain=True, qos=1)
        except Exception as e:
            logger.error("Failed to publish discovery config %s: %s", topic, e)

    def remove_obsolete_configs(self, pack_no: int) -> None:
        """
        Remove retained discovery configs of entities that no longer exist:
        state of health (firmware always sends 100 %), cell disconnection (always 0) and renamed keys.
        """
        topics = [f"{self.discovery_prefix}/sensor/seplos-mqtt-pack-{pack_no}/state_of_health/config"]
        # renamed keys (old -> new): min/max_cell_voltage -> cell_voltage_setting_min/max,
        # min/max_pack_voltage -> pack_voltage_setting_min/max, battery_capacity -> full_capacity,
        # cell_voltage_difference_sensing_failure -> cell_voltage_difference_fault
        topics += [
            f"{self.discovery_prefix}/sensor/seplos-mqtt-pack-{pack_no}/{key}/config"
            for key in ("min_cell_voltage", "max_cell_voltage", "min_pack_voltage", "max_pack_voltage", "battery_capacity")
        ]
        topics.append(
            f"{self.discovery_prefix}/binary_sensor/seplos-mqtt-pack-{pack_no}/cell_voltage_difference_sensing_failure/config"
        )
        # "Reservation" fields, renamed to current_offset, current, energy_charged, energy_discharged
        topics += [
            f"{self.discovery_prefix}/sensor/seplos-mqtt-pack-{pack_no}/reserved_{i}/config"
            for i in range(1, 5)
        ]
        topics += [
            f"{self.discovery_prefix}/binary_sensor/seplos-mqtt-pack-{pack_no}/disconnection_cell_{i}/config"
            for i in range(1, 17)
        ]
        for topic in topics:
            try:
                self.mqtt_client.publish(topic, "", retain=True, qos=1)
            except Exception as e:
                logger.error("Failed to remove obsolete discovery config %s: %s", topic, e)

    def create_similar_binary_sensor_config(
        self,
        num_sensors: int,
        pack_no: int,
        value_template_group: str,
        base_value_template_key: str,
        base_name: str,
        entity_category: Optional[str] = None,
        device_class: Optional[str] = None,
        icon: Optional[str] = None,
        payload_on: Optional[str] = None,
        payload_off: Optional[str] = None,
        options: Optional[List[str]] = None
    ) -> None:
        """
        Create multiple similar binary sensor configurations.
        """
        for i in range(1, num_sensors + 1):
            name = f"{base_name} {i}"
            value_template_key = f"{base_value_template_key}_{i}"

            self.create_binary_sensor_config(
                pack_no=pack_no,
                name=name,
                value_template_group=value_template_group,
                value_template_key=value_template_key,
                entity_category=entity_category,
                device_class=device_class,
                icon=icon,
                payload_on=payload_on,
                payload_off=payload_off,
                options=options
            )

    def create_similar_sensor_config(
        self,
        num_sensors: int,
        pack_no: int,
        value_template_group: str,
        base_value_template_key: str,
        base_name: str,
        entity_category: Optional[str] = None,
        device_class: Optional[str] = None,
        state_class: Optional[str] = None,
        unit_of_measurement: Optional[str] = None,
        suggested_display_precision: Optional[int] = None,
        icon: Optional[str] = None
    ) -> None:
        """
        Create multiple similar sensor configurations.
        """
        for i in range(1, num_sensors + 1):
            name = f"{base_name} {i}"
            value_template_key = f"{base_value_template_key}_{i}"

            self.create_sensor_config(
                pack_no=pack_no,
                name=name,
                value_template_group=value_template_group,
                value_template_key=value_template_key,
                entity_category=entity_category,
                device_class=device_class,
                state_class=state_class,
                unit_of_measurement=unit_of_measurement,
                suggested_display_precision=suggested_display_precision,
                icon=icon
            )

    def create_autodiscovery_sensors(self, pack_no: int, pack_bus: bool = True) -> None:
        """
        Create all Home Assistant auto-discovery sensors for a pack.

        Args:
            pack_no: Pack number to create sensors for
            pack_bus: True if connected to RS485-1/2 (extended status and parameters available)
        """
        # Clear device info flag for this pack to ensure it's included in first sensor
        self._device_info_published.discard(pack_no)

        ## Telemetry sensors

        # Create cell voltage sensors
        self.create_similar_sensor_config(
            num_sensors=16,
            pack_no=pack_no,
            value_template_group="telemetry",
            base_value_template_key="voltage_cell",
            base_name="Voltage Cell",
            device_class="voltage",
            state_class="measurement",
            unit_of_measurement="V",
            suggested_display_precision=3,
            icon="mdi:battery-outline"
        )

        # Create cell temperature sensors
        self.create_similar_sensor_config(
            num_sensors=4,
            pack_no=pack_no,
            value_template_group="telemetry",
            base_value_template_key="cell_temperature",
            base_name="Cell Temperature",
            device_class="temperature",
            state_class="measurement",
            unit_of_measurement="°C",
            suggested_display_precision=1,
            icon="mdi:thermometer"
        )

        # Create telemetry sensors
        for config in TELEMETRY_SENSOR_TEMPLATES:
            self.create_sensor_config(
                pack_no=pack_no,
                value_template_group="telemetry",
                **config
            )

        # Create heartbeat sensor
        self.create_heartbeat_sensor_config(pack_no=pack_no)

        # Create static info sensors
        for config in INFO_SENSOR_TEMPLATES:
            self.create_info_sensor_config(pack_no=pack_no, **config)

        # Extended status (0x5A) and parameters (0x47): only served on RS485-1/2
        if pack_bus:
            for config in STATUS_SENSOR_TEMPLATES:
                self.create_sensor_config(pack_no=pack_no, value_template_group="status", **config)
            for config in STATUS_BINARY_SENSOR_TEMPLATES:
                self.create_binary_sensor_config(pack_no=pack_no, value_template_group="status", **config)
            self.create_parameter_sensor_config(pack_no=pack_no)
        else:
            self._remove_configs("sensor", pack_no, [c["value_template_key"] for c in STATUS_SENSOR_TEMPLATES] + ["parameters"])
            self._remove_configs("binary_sensor", pack_no, [c["value_template_key"] for c in STATUS_BINARY_SENSOR_TEMPLATES])

        ## Telesignalization sensors

        # Create Cell voltage warning sensors
        self.create_similar_sensor_config(
            num_sensors=16,
            pack_no=pack_no,
            value_template_group="telesignalization",
            base_value_template_key="cell_voltage_alarm",
            base_name="Voltage Alarm Cell",
            icon="mdi:flash-alert",
            entity_category="diagnostic"
        )

        # Create Cell Temperature warning sensors
        self.create_similar_sensor_config(
            num_sensors=4,
            pack_no=pack_no,
            value_template_group="telesignalization",
            base_value_template_key="cell_temperature_alarm",
            base_name="Cell Temperature Alarm",
            icon="mdi:thermometer-alert",
            entity_category="diagnostic"
        )

        # Create Balancer sensors
        self.create_similar_binary_sensor_config(
            num_sensors=16,
            pack_no=pack_no,
            value_template_group="telesignalization",
            base_value_template_key="balancer_cell",
            base_name="Balancing Cell",
            entity_category="diagnostic",
            device_class="running"
        )

        # Remove entities of former versions whose values the firmware never fills
        self.remove_obsolete_configs(pack_no=pack_no)

        # Create telesignalization sensors
        for config in TELESIGNALIZATION_SENSOR_TEMPLATES:
            self.create_sensor_config(
                pack_no=pack_no,
                value_template_group="telesignalization",
                **config
            )

        # Create telesignalization binary sensors
        for config in TELESIGNALIZATION_BINARY_SENSOR_TEMPLATES:
            self.create_binary_sensor_config(
                pack_no=pack_no,
                value_template_group="telesignalization",
                **config
            )
