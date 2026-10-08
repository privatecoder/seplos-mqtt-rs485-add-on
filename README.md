# Seplos MQTT RS485 Add-on

This Home Assistant add-on reads one or more Seplos V2/V16 BMS packs over RS485 and publishes their data to MQTT.

## What you get

- MQTT telemetry and alarm data per pack (cell voltages and temperatures, current, voltages, capacities, SOC, cycles, protection/alarm states, MOSFET and balancing states)
- Device info per pack: device name, firmware version, derived firmware patch level, active CAN inverter protocol, serial number (single pack mode)
- Single pack mode only: extended status (balancing active/blocked, charge blocked request, current incl. idle) and all BMS parameters/function switches as attributes
- Multiple pack mode only: a "Seplos BMS System" device with the values the master reports to the inverter (charge/discharge voltage and current limits, charge/discharge allowed, system SOC/voltage/current, min/max cell per pack)
- Home Assistant MQTT discovery (optional)
- Per-pack heartbeat and availability
- Simple /health endpoint for the HA supervisor watchdog
- Debug logging of decoded intra-pack (master/slave) traffic

## Who this is for

- You have Seplos BMS V2/V16 (10C/10E) packs
- You have a working MQTT broker
- You can connect to the BMS via RS485 (local serial or RS485-to-Ethernet)

## Hardware requirements

- Seplos BMS V2/V16 pack(s)
- RS485 interface (local serial or RS485-to-Ethernet gateway)
- Cabling for your setup (see wiring below)
- MQTT broker

Tested gateway: [Waveshare 2-CH RS485 to ETH](https://www.waveshare.com/2-ch-rs485-to-eth-b.htm)

## Wiring overview

The BMS has three sockets: CAN, RS485-1 and RS485-2. Besides CAN, the CAN socket carries a second RS485 bus (9600 baud).

Two common setups:
1. Single pack: connect directly to RS485-1 or RS485-2 of the pack (19200 baud).
2. Multiple packs: daisy chain all packs via the RS485 pins of their CAN sockets with splitters (9600 baud, see below). On RS485-1/2 the master is the bus master itself (it polls its slaves) and can't be read reliably, on the CAN socket bus every pack incl. the master answers under its DIP address.

Note: the multiple pack wiring only works if your inverter is connected via CAN. The RS485 pins of the master's CAN socket are then free for this bus.

### Wiring the RS485 device to MULTIPLE battery packs

Carefully check the wiring sample (the pink lines). There are two ways to reach the same result:
1. One custom cable with multiple plugs (recommended).
2. One or more splitters (easier if you do not want to crimp) – ([this one works for me](https://www.amazon.de/gp/product/B00D3KIQXC)).

If you use splitters, the first (**and only the first!**) splitter (connected to the master CAN port) must be modified:
- One outlet keeps only the three RS485 pins.
- The other outlet keeps only the two CAN pins.

Example for four packs:
- Connect the modified splitter to the CAN port of the master (not the RS485 ports).
  - The outlet with only CAN pins goes to your inverter.
  - The outlet with only RS485 pins goes to the first slave.
- Connect the second splitter to the CAN port of the first slave.
  - One outlet receives the cable from the master.
  - The other outlet goes to the second slave.
- Connect the third splitter to the CAN port of the second slave.
  - One outlet receives the cable from the first slave.
  - The other outlet goes to the third slave.
- Connect the fourth splitter to the CAN port of the third slave.
  - One outlet receives the cable from the second slave.
  - The other outlet goes to your RS485 device.

#### Wiring sample

<img alt="wiring sample" src="https://github.com/user-attachments/assets/dc72fa68-df39-41e8-8033-4776d622d618" width="500">

#### Seplos pin assignment (CAN port)

- 1/8 => RS485-B
- 2/7 => RS485-A
- 4 => CAN-H
- 5 => CAN-L
- 3/6 => GND

<img width="400" alt="seplos pin assignment" src="https://github.com/user-attachments/assets/af477cbd-9cba-422f-9e0f-880e4e17fc45" />

#### Waveshare pin assignment (RS485 port)

- orange => RS485-A
- orange-white => RS485-B
- green-white => GND

<img alt="waveshare gateway pinout" src="https://github.com/user-attachments/assets/442e0fee-5ec7-495b-81d7-013c56f1f304" width="100">

### Wiring the RS485 device to ONE SINGLE battery pack

Connect to RS485-1 or RS485-2. When using a regular patch cable (T568B), cut one connector and crimp the following wires for the RS485 device:
- orange (pin 2) => RS485-A
- orange-white (pin 1) => RS485-B
- green-white (pin 3) => GND

<img alt="waveshare gateway pinout" src="https://github.com/user-attachments/assets/442e0fee-5ec7-495b-81d7-013c56f1f304" width="100">

## Installation and configuration

1. Configure an MQTT broker in Home Assistant.
2. Configure your RS485 device.
   - Waveshare example: IP Mode Static, Port 4196, Work Mode TCP Server, Transfer Protocol None.
   - Baud rate: 9600 for multiple packs (RS485 bus on the CAN socket), 19200 for a single pack (RS485-1/2).
     The add-on derives the baud rate from the number of packs. Firmware 16.04.06 (`..._9600.ehex`) used 9600 baud on RS485-1/2 as well, which the add-on doesn't support for a single pack.
3. Install the add-on.
4. Open the add-on configuration and set:
   - RS485 connection (remote IP/port or local serial interface)
   - Number of packs
   - MQTT topic prefix (default: seplos)
   - Optional: HA discovery, update interval, logging level
5. Save and start the add-on.
6. Check the logs for successful startup.

Example log output:

```text
[13:03:09] INFO: Starting Seplos MQTT RS485 Add-on...
[13:03:09] INFO: MQTT service configured: core-mosquitto:1883
[13:03:09] INFO: Configuring remote RS485 connection to 192.168.100.58:4196
[13:03:11] INFO: Starting Seplos BMS data fetcher...
2025-11-15 13:03:11 INFO:SeplosBMS:Starting Seplos BMS Data Fetcher
2025-11-15 13:03:11 INFO:SeplosBMS:Connected to MQTT broker (core-mosquitto:1883)
2025-11-15 13:03:11 INFO:SeplosBMS:Initializing serial interface /tmp/vcom0 at 9600 baud
2025-11-15 13:03:11 INFO:SeplosBMS:Initialized 2 battery pack(s)
2025-11-15 13:03:11 INFO:SeplosBMS:Sending Home Assistant Auto-Discovery configurations
2025-11-15 13:03:11 INFO:SeplosBMS:Auto-Discovery configurations sent
2025-11-15 13:03:11 INFO:SeplosBMS:Pack0:Requesting data...
2025-11-15 13:03:11 INFO:SeplosBMS:Pack0:Telemetry received
2025-11-15 13:03:12 INFO:SeplosBMS:Pack0:Telesignalization received
2025-11-15 13:03:13 INFO:SeplosBMS:Pack0:DeviceInfo received
2025-11-15 13:03:16 INFO:SeplosBMS:Pack0:Publishing updated data to MQTT
2025-11-15 13:03:13 INFO:SeplosBMS:Pack1:Requesting data...
2025-11-15 13:03:14 INFO:SeplosBMS:Pack1:Telemetry received
2025-11-15 13:03:15 INFO:SeplosBMS:Pack1:Telesignalization received
2025-11-15 13:03:15 INFO:SeplosBMS:Pack1:Publishing updated data to MQTT
2025-11-15 13:03:17 INFO:SeplosBMS:System:Publishing updated data to MQTT
2025-11-15 13:03:17 INFO:SeplosBMS:Waiting 60 seconds before next cycle
```

## MQTT topics (default prefix: seplos)

- seplos/pack-{n}/sensors: JSON with the groups `telemetry`, `telesignalization`, `info` and (single pack mode only) `status` and `parameters`
- seplos/pack-{n}/heartbeat: last_publish and publish_counter (JSON)
- seplos/pack-{n}/availability: per-pack online/offline
- seplos/system/sensors: system values of the master as reported to the inverter (multiple pack mode only)
- seplos/system/availability: system online/offline (multiple pack mode only)
- seplos/availability: global online/offline

Home Assistant discovery uses the prefix defined by ha_discovery_prefix (default: homeassistant).

## Sensor reference

Home Assistant names describe what a value actually does (verified against the BMS firmware). The third column maps it to the name used in the Seplos protocol documentation ("SEPLOS BMS Communication Protocol V2.0"), so raw values can be traced back. "–" means the value isn't part of the documented protocol.

| Home Assistant name | MQTT key (JSON path) | Seplos protocol doc name / source |
|---|---|---|
| Voltage Cell 1-16 | `telemetry.normal.voltage_cell_N` | "Voltage of cell 1..M" (0x42) |
| Cell Temperature 1-4 | `telemetry.normal.cell_temperature_N` | "Cell temperature 1..4" (0x42) |
| Cell Voltage Range Min (Setting) | `telemetry.normal.cell_voltage_setting_min` | – (add-on setting) |
| Cell Voltage Range Max (Setting) | `telemetry.normal.cell_voltage_setting_max` | – (add-on setting) |
| Pack Voltage Range Min (Setting) | `telemetry.normal.pack_voltage_setting_min` | – (add-on setting × cells) |
| Pack Voltage Range Max (Setting) | `telemetry.normal.pack_voltage_setting_max` | – (add-on setting × cells) |
| Average Cell Voltage | `telemetry.normal.average_cell_voltage` | – (calculated from 0x42) |
| Lowest Cell No. | `telemetry.normal.lowest_cell` | – (calculated from 0x42) |
| Lowest Cell Voltage | `telemetry.normal.lowest_cell_voltage` | – (calculated from 0x42) |
| Highest Cell No. | `telemetry.normal.highest_cell` | – (calculated from 0x42) |
| Highest Cell Voltage | `telemetry.normal.highest_cell_voltage` | – (calculated from 0x42) |
| Delta Cell Voltage | `telemetry.normal.delta_cell_voltage` | – (calculated from 0x42) |
| Delta Cell Temperature | `telemetry.normal.delta_cell_temperature` | – (calculated from 0x42) |
| Ambient Temperature | `telemetry.normal.ambient_temperature` | "Environment temperature" (0x42) |
| MOSFET Temperature | `telemetry.normal.components_temperature` | "Power temperature" (0x42), MOSFET/power stage |
| Current (0 when idle) | `telemetry.normal.dis_charge_current` | "Charge/discharge current" (0x42), 0 when idle |
| Power (calculated) | `telemetry.normal.dis_charge_power` | – (calculated: current × pack voltage) |
| Pack Voltage | `telemetry.normal.total_pack_voltage` | "Total battery voltage" (0x42) |
| Rated Capacity (Setting) | `telemetry.normal.rated_capacity` | "Rated capacity" (0x42), setting P58 |
| Full Capacity | `telemetry.normal.full_capacity` | "Battery capacity" (0x42), learned full capacity |
| Remaining Capacity | `telemetry.normal.residual_capacity` | "Residual capacity" (0x42) |
| State of Charge | `telemetry.normal.state_of_charge` | "SOC" (0x42) |
| Cycle Count | `telemetry.normal.charging_cycles` | "Number of cycles" (0x42) |
| Port Voltage (P+/P-) | `telemetry.normal.port_voltage` | "Port voltage" (0x42) |
| Reserved 1 (Current Ch. B raw) | `telemetry.normal.reserved_1` | "Reservation" 1 (0x42) |
| Reserved 2 (Current Ch. A raw, idle) | `telemetry.normal.reserved_2` | "Reservation" 2 (0x42) |
| Reserved 3 (Cumulative raw) | `telemetry.normal.reserved_3` | "Reservation" 3 (0x42) |
| Reserved 4 (Cumulative raw) | `telemetry.normal.reserved_4` | "Reservation" 4 (0x42) |
| Voltage Alarm Cell 1-16 | `telesignalization.normal.cell_voltage_alarm_N` | "Cell 1..M alarm" (0x44) |
| Cell Temperature Alarm 1-4 | `telesignalization.normal.cell_temperature_alarm_N` | "Cell temperature alarm 1..4" (0x44) |
| Balancing Cell 1-16 | `telesignalization.binary.balancer_cell_N` | "Equilibrium state 1/2" (cells 1-8 / 9-16) (0x44) |
| Alarm Cell Voltage | `telesignalization.normal.any_cell_voltage_alarm` | – (any of the cell alarms, 0x44) |
| Alarm Cell Temperature | `telesignalization.normal.any_cell_temperature_alarm` | – (any of the cell temperature alarms, 0x44) |
| Alarm Ambient Temp. | `telesignalization.normal.ambient_temperature_alarm` | "Environment temperature alarm" (0x44) |
| Alarm MOSFET Temp. | `telesignalization.normal.component_temperature_alarm` | "Power temperature alarm" (0x44) |
| Alarm Dis-/Charge Current | `telesignalization.normal.dis_charging_current_alarm` | "Charge/discharge current alarm" (0x44) |
| Alarm Pack Voltage | `telesignalization.normal.pack_voltage_alarm` | "Total battery voltage alarm" (0x44) |
| System Status | `telesignalization.normal.system_status` | "System state" (0x44) |
| Cell Overvoltage | `telesignalization.normal.cell_overvoltage` | Alarm event 2 bit 0/1 "Monomer high voltage alarm" / "Monomer overvoltage protection" |
| Cell Undervoltage | `telesignalization.normal.cell_voltage_low` | Alarm event 2 bit 2/3 "Monomer low voltage alarm / under voltage protection" |
| Pack Overvoltage | `telesignalization.normal.pack_overvoltage` | Alarm event 2 bit 4/5 "High voltage alarm / Overvoltage protection for total voltage" |
| Pack Undervoltage | `telesignalization.normal.pack_voltage_low` | Alarm event 2 bit 6/7 "Low voltage alarm / Under voltage protection for total voltage" |
| Charging Temp. High | `telesignalization.normal.charging_temperature_high` | Alarm event 3 bit 0/1 "Charge high temperature alarm / over temperature protection" |
| Charging Temp. Low | `telesignalization.normal.charging_temperature_low` | Alarm event 3 bit 2/3 "Charge low temperature alarm / under temperature protection" |
| Discharging Temp. High | `telesignalization.normal.discharging_temperature_high` | Alarm event 3 bit 4/5 "Discharge high temperature alarm / over temperature protection" |
| Discharging Temp. Low | `telesignalization.normal.discharging_temperature_low` | Alarm event 3 bit 6/7 "Discharge low temperature alarm / under temperature protection" |
| Ambient Temp. High | `telesignalization.normal.ambient_temperature_high` | Alarm event 4 bit 0/1 "Environment high temperature alarm / over temperature protection" |
| Ambient Temp. Low | `telesignalization.normal.ambient_temperature_low` | Alarm event 4 bit 2/3 "Environment low temperature alarm / under temperature protection" |
| MOSFET Temp. High | `telesignalization.normal.component_temperature_high` | Alarm event 4 bit 5/4 "Power high temperature alarm" / "Power over temperature protection" |
| Charging Overcurrent | `telesignalization.normal.charging_overcurrent` | Alarm event 5 bit 0/1 "Charge over current alarm / protection" |
| Discharging Overcurrent | `telesignalization.normal.discharging_overcurrent` | Alarm event 5 bit 2/3 "Discharge over current alarm / protection" |
| Transient Overcurrent | `telesignalization.normal.transient_overcurrent` | Alarm event 5 bit 4/6 "Transient over current protection / lockout" |
| Output Short Circuit | `telesignalization.normal.output_short_circuit` | Alarm event 5 bit 5/7 "Output short circuit protection / lockout" |
| Remaining Capacity Low | `telesignalization.normal.soc_low` | Alarm event 6 bit 2/3 "Residual capacity alarm / protection" |
| Voltage Sensing Failure | `telesignalization.binary.voltage_sensing_failure` | Alarm event 1 bit 0 "Voltage sensor fault" (0x44) |
| Temp. Sensing Failure | `telesignalization.binary.temperature_sensing_failure` | Alarm event 1 bit 1 "Temperature sensor fault" (0x44) |
| Current Sensing Failure | `telesignalization.binary.current_sensing_failure` | Alarm event 1 bit 2 "Current sensor fault" (0x44) |
| Power Button Failure | `telesignalization.binary.power_switch_failure` | Alarm event 1 bit 3 "Key switch fault" (0x44) |
| Cell Voltage Difference Fault | `telesignalization.binary.cell_voltage_difference_fault` | Alarm event 1 bit 4 "Cell voltage dropout fault", delta >= P60 (0x44) |
| Charge MOSFET Failure | `telesignalization.binary.charging_switch_failure` | Alarm event 1 bit 5 "Charge switch fault" (0x44) |
| Discharge MOSFET Failure | `telesignalization.binary.discharging_switch_failure` | Alarm event 1 bit 6 "Discharge switch fault" (0x44) |
| Current Limiter Failure | `telesignalization.binary.current_limit_switch_failure` | Alarm event 1 bit 7 "Current limit switch fault" (0x44) |
| Cell Heating Requested | `telesignalization.binary.low_temperature_heating` | Alarm event 4 bit 6 "Cell low temperature heating" = heating requested (0x44) |
| Charger Overvoltage Protection | `telesignalization.binary.charging_high_voltage_protection` | Alarm event 6 bit 0 "Charge high voltage protection" (charger > P18) (0x44) |
| Charging Paused (Intermittent Charge) | `telesignalization.binary.intermittent_power_supplement` | Alarm event 6 bit 1 "Intermittent recharge waiting" (0x44) |
| Charging Blocked (Cell < Min.) | `telesignalization.binary.cell_low_voltage_forbidden_charging` | Alarm event 6 bit 4 "Cell low voltage charging prohibition" (cell < P9) (0x44) |
| Output Reverse Polarity Protection | `telesignalization.binary.output_reverse_polarity_protection` | Alarm event 6 bit 5 "Output reverse polarity protection" (0x44) |
| Output Connection Failure | `telesignalization.binary.output_connection_failure` | Alarm event 6 bit 6 "Output connection fault" (0x44) |
| Charge Activation Waiting (Auto) | `telesignalization.binary.auto_charging_wait` | Alarm event 7 bit 4 "Automatic charging waiting" (0x44) |
| Charge Activation Waiting (Manual) | `telesignalization.binary.manual_charging_wait` | Alarm event 7 bit 5 "Manual charging waiting" (0x44) |
| EEPROM Failure | `telesignalization.binary.eep_storage_failure` | Alarm event 8 bit 0 "EEP storage fault" (0x44) |
| RTC Clock Failure | `telesignalization.binary.rtc_clock_failure` | Alarm event 8 bit 1 "RTC error" (0x44) |
| No Calibration Of Voltage | `telesignalization.binary.no_calibration_of_voltage` | Alarm event 8 bit 2 "Voltage calibration not performed" (0x44) |
| No Calibration Of Current | `telesignalization.binary.no_calibration_of_current` | Alarm event 8 bit 3 "Current calibration not performed" (0x44) |
| Current Zero Point Not Calibrated | `telesignalization.binary.no_calibration_of_null_point` | Alarm event 8 bit 4 "Zero calibration not performed" (0x44) |
| Discharge MOSFET | `telesignalization.binary.discharge_switch` | On-off state bit 0 "Discharge switch state" (0x44) |
| Charge MOSFET | `telesignalization.binary.charge_switch` | On-off state bit 1 "Charge switch state" (0x44) |
| Current Limiter | `telesignalization.binary.current_limit_switch` | On-off state bit 2 "Current limit switch state" (0x44) |
| Heater | `telesignalization.binary.heating_switch` | On-off state bit 3 "Heating switch state" (0x44) |
| Device Name | `info.normal.device_name` | device name (0x51 bytes 0-9) |
| Firmware Version | `info.normal.firmware_version` | software version (0x51 bytes 10-11) |
| Firmware Patch (derived) | `info.normal.firmware_patch` | – (derived from features, see firmware analysis) |
| CAN Protocol | `info.normal.can_protocol` | manufacturer name field = active CAN protocol (0x51 bytes 12-31) |
| Serial Number | `info.normal.serial_number` | – undocumented CID2 0xA2 (RS485-1/2 only) |
| Current (incl. idle) | `status.normal.dis_charge_current_unfiltered` | intra-pack record current (undocumented CID2 0x5A) |
| Highest Cell Temperature | `status.normal.highest_cell_temperature` | intra-pack record (0x5A) |
| Lowest Cell Temperature | `status.normal.lowest_cell_temperature` | intra-pack record (0x5A) |
| Balancing Active | `status.binary.balancing_active` | Alarm event 7 bit 0 "Inside bit" (0x5A) |
| Balancing Not Running (Blocked / Not Needed) | `status.binary.balancing_blocked` | Alarm event 7 bit 1 "Inside bit" (0x5A) |
| Balancing Time Limit Reached | `status.binary.balancing_time_limit` | Alarm event 7 bit 2 "Inside bit" (0x5A) |
| Balancing Blocked (Temperature) | `status.binary.balancing_temperature_blocked` | Alarm event 7 bit 3 "Inside bit" (0x5A) |
| Charge Blocked (Charge MOSFET Off Request) | `status.binary.charge_forbidden` | Alarm event 8 bit 6 "Inside bit" (0x5A) |
| Parameters | `parameters` (all as attributes) | parameters 0-86 and function switches (CID2 0x47, "Acquisition of teleregulation information") |
| System State (as reported to inverter) (System) | `system: normal.system_state` | – undocumented Modbus RTU FC03 0x1000 (master) |
| System Voltage (System) | `system: normal.system_voltage` | – undocumented Modbus RTU FC03 0x1000 (master) |
| System Current (System) | `system: normal.system_current` | – undocumented Modbus RTU FC03 0x1000 (master) |
| System SOC (System) | `system: normal.system_soc` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Total Capacity (System) | `system: normal.total_capacity` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Charge Voltage Limit (System) | `system: normal.charge_voltage_limit` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Charge Current Limit (System) | `system: normal.charge_current_limit` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Discharge Current Limit (System) | `system: normal.discharge_current_limit` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Discharge Voltage Limit (System) | `system: normal.discharge_voltage_limit` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Highest Cell Voltage (System) | `system: normal.highest_cell_voltage` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Highest Cell Voltage Pack (System) | `system: normal.highest_cell_voltage_pack` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Lowest Cell Voltage (System) | `system: normal.lowest_cell_voltage` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Lowest Cell Voltage Pack (System) | `system: normal.lowest_cell_voltage_pack` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Highest Cell Temperature (System) | `system: normal.highest_cell_temperature` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Highest Cell Temperature Pack (System) | `system: normal.highest_cell_temperature_pack` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Lowest Cell Temperature (System) | `system: normal.lowest_cell_temperature` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Lowest Cell Temperature Pack (System) | `system: normal.lowest_cell_temperature_pack` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Alarm Word Current/Voltage (System) | `system: normal.alarm_word_current_voltage` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Alarm Word Temperature/Misc (System) | `system: normal.alarm_word_temperature_misc` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Charge Allowed (System) | `system: binary.charge_allowed` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Discharge Allowed (System) | `system: binary.discharge_allowed` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Pack Communication Fault (System) | `system: binary.pack_communication_fault` | – undocumented Modbus RTU FC03 0x1000 (master) |

Notes:
- `Current (0 when idle)` is reported as 0 by the BMS as long as it is neither charging nor discharging; `Current (incl. idle)` (single pack mode) shows the real current.
- Alarm/protection pairs report the more severe state (Protection/Lockout before Alarm).
- The doc's "State of health" (always 100 %), "Disconnection state" (always 0) and system state bit 2 "Floating charge" (masked out, actually an internal "charge path active" flag) are not published.

## Home Assistant setup

### Auto-discovery

- If Enable HA Auto-Discovery is on, the add-on publishes MQTT discovery configs and devices appear automatically.

### Lovelace example

The provided `ha-lovelace/lovalace.yaml` uses custom cards such as `mushroom-template-card`, `entity-progress-card`, `button-card`, `bar-card`, `card_mod`, and `browser_mod` (installable via HACS).

<img width="400" alt="lovelace sample multiple packs card" src="https://github.com/user-attachments/assets/814f7540-57a1-40db-9ac1-5d1a3e9a19a3" />
<img width="400" alt="lovelace sample pack info" src="https://github.com/user-attachments/assets/829acb11-d50c-43a0-b818-96fbe28907aa" />
<img width="400" alt="lovelace sample pack errors and warnings" src="https://github.com/user-attachments/assets/419ed442-98ec-4985-98a6-6b4f5b4b9841" />
