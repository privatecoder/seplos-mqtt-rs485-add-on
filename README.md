# Seplos MQTT RS485 Add-on

This Home Assistant add-on reads one or more Seplos BMS 2.0/10E/V16 (EMU1101) packs over RS485 and publishes their data to MQTT.

## What you get

- MQTT telemetry and alarm data per pack (cell voltages and temperatures, current, voltages, capacities, SOC, cycles, protection/alarm states, MOSFET and balancing states)
- Device info per pack: device name, firmware version, derived firmware patch level, active CAN inverter protocol, battery and BMS serial number (single pack mode)
- Single pack mode only: extended status (balancing, charge blocked request, current limiter reasons, precharge, external voltage/charger detected, current incl. idle) and all BMS parameters and named function switches as attributes
- Multiple pack mode only: a "Seplos BMS System" device with the master's aggregate over all packs via Modbus (charge/discharge voltage and current limits, charge/discharge allowed, force charge requests, system SOC/voltage/current, min/max cell per pack) plus the calculated System Power and Total Full Energy
- Calculated values: power and full energy (kWh, from the option `nominal_cell_voltage`) per pack and for the system
- A settings app in the Home Assistant sidebar: overview of all packs, connection settings, read-only pack detection (incl. MAINT version), parallel sets
- Parallel sets (multiple pack mode): packs on a common feeder grouped into one "Seplos Parallel Set" device with net current, power, capacity-weighted SOC, voltage spread and cell extrema
- Packs with 7 to 16 cells
- Home Assistant MQTT discovery (optional)
- Per-pack heartbeat and availability
- Simple /health endpoint for the HA supervisor watchdog
- Debug logging of decoded intra-pack (master/slave) traffic

## Who this is for

- You have Seplos BMS 2.0/10E/V16 (EMU1101) packs
- You have a working MQTT broker
- You can connect to the BMS via RS485 (local serial or RS485-to-Ethernet)

## Hardware requirements

- Seplos BMS 2.0/10E/V16 (EMU1101) pack(s)
- RS485 interface (local serial or RS485-to-Ethernet gateway)
- Cabling for your setup (see wiring below)
- MQTT broker

Tested gateway: [Waveshare 2-CH RS485 to ETH](https://www.waveshare.com/2-ch-rs485-to-eth-b.htm)

### Supported hardware

| | |
|---|---|
| Generation | BMS 2.0 |
| OEM platform | EMU1101 |
| Variant | 1101-10E |
| Hardware | V16 |

The firmware (e.g. 16.06.13, shown as "Firmware" in Home Assistant) is not the protocol version (V2.0). In Home Assistant the facts appear on the device page of each pack: model, model ID (`model_id` needs HA 2024.8 or newer), hardware and, where the battery serial number can be read (CID2 0xA2 on RS485-1/2, or MAINT on the CAN port), the serial number exactly as the pack reports it (a placeholder value is shown as is). The BMS serial number (0xA4) is only the diagnostic sensor and the identity check. The System and Parallel Set devices are virtual and show no hardware, firmware or serial.

## Wiring overview

The BMS has three sockets: CAN, RS485-1 and RS485-2. Besides CAN, the CAN socket carries a second RS485 bus (9600 baud).

Two common setups:
1. Single pack: connect directly to RS485-1 or RS485-2 of the pack (19200 baud).
2. Multiple packs: daisy chain all packs via the RS485 pins of their CAN sockets with splitters (9600 baud, see below). On RS485-1/2 the master is the bus master itself: it polls its slaves (19200 baud) and never answers requests there. On the CAN socket bus every pack incl. the master answers under its DIP address.

Note: the multiple pack wiring only works if your inverter is connected via CAN. The RS485 pins of the master's CAN socket are then free for this bus.

Addresses: set the DIP switches of the master to 0 and of the slaves to 1, 2, 3, … The inverter must be connected to the master, only the master sends CAN. The add-on polls the packs under these addresses.

There are two separate RS485 buses:
- **Intra-pack bus** (RS485-1/RS485-2 sockets, orange in the wiring sample): master ↔ slaves, 19200 baud. Leave it as it is.
- **CAN socket bus** (RS485 pins of the CAN sockets, pink in the wiring sample): all packs + your RS485 device, 9600 baud.

### Wiring the RS485 device to MULTIPLE battery packs

Carefully check the wiring sample (the pink lines). There are two ways to reach the same result:
1. One custom cable with multiple plugs (recommended).
2. One or more splitters (easier if you do not want to crimp) – ([this one works for me](https://www.amazon.de/gp/product/B00D3KIQXC)).

The CAN socket carries the RS485 bus twice: pins 1/2/3 and pins 6/7/8 are the same lines (B, A, GND), see the pin assignment below. That is what makes daisy chaining work: one side comes in, the other side goes on.

If you use splitters, the first (**and only the first!**) splitter (connected to the master CAN port) must be modified:
- The outlet to the inverter keeps **only the CAN pins 4 and 5**. Otherwise the inverter's own RS485 lines end up on the 9600 baud bus.
- The other outlet keeps only the RS485 pins (1/2/3 or 6/7/8, both carry the same bus).

The wiring sample below shows three packs with one custom cable; the splitter example uses four packs. Both follow the same scheme.

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

With a regular patch cable (T568B) plugged into the CAN socket (or a splitter outlet) of the last pack, use either pin group – both carry the same bus:

| Signal | pins 1/2/3 | pins 6/7/8 (as in the wiring sample) |
|---|---|---|
| RS485-A | pin 2, orange | pin 7, brown-white |
| RS485-B | pin 1, orange-white | pin 8, brown |
| GND | pin 3, green-white | pin 6, green |

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
     With the option `serial_baudrate: auto` (default) the add-on derives the baud rate from the number of packs. Firmware 16.04.06 (`..._9600.ehex`) uses 9600 baud on RS485-1/2 as well: set `serial_baudrate: "9600"` for a single pack with that firmware.
3. Install the add-on.
4. Open the add-on configuration and set:
   - MQTT topic prefix (default: seplos)
   - Optional: HA discovery, logging level

   The RS485 connection (remote IP/port or serial interface), number of packs and cell voltages are no longer add-on
   options for new installs: set them in the **Seplos BMS** app (see "Settings app" below). Existing installs have
   these values imported once on the first start with this version; the add-on then removes them from its
   configuration automatically.
5. Save and start the add-on.
6. Open **Seplos BMS** in the sidebar and enter the gateway or serial connection in **Settings**. A fresh install
   cannot open any BMS connection yet: the add-on stays up with the app only (the Overview says why) until you
   save a working connection, then it restarts and starts polling.

### Settings app (sidebar)

The add-on adds a **Seplos BMS** entry to the Home Assistant sidebar (Ingress). The connection settings
(remote gateway or serial device, baud rate, number of packs, cell voltage range, update interval, MAINT
firmware) are stored in `/data/monitoring.json`. The corresponding legacy add-on options are only used once,
to create these initial settings on the first start with this version, and are then removed from the add-on
configuration; change them in the Seplos BMS app. Saving a changed connection restarts the add-on's process in place, so all packs,
discovery entities and the gateway bridge are rebuilt from the saved settings. The MQTT, discovery and logging
options stay in the add-on configuration.

### Parallel sets

In the **Parallel sets** tab you group packs that hang on one common feeder to the bank busbars (packs connected in
parallel, e.g. pairs). Per set: a name, the member packs in their physical order, the wiring (diagonal, common
busbar or unknown) and, for diagonal wiring, the packs carrying the plus and minus cable. "Propose neighbour pairs"
fills a draft with (0, 1), (2, 3), … that you check before saving. Saving takes effect at the next poll cycle,
without a restart; nothing is written to the BMS.

Each active set becomes a Home Assistant device "Seplos Parallel Set <name>" (entity IDs
`sensor.seplos_bms_set_<id>_<key>`; the id is assigned once and never reused). The pack devices stay as they are.
After each poll cycle the add-on computes from the members' latest telemetry:

| HA name | MQTT JSON path (`seplos/set-{id}/sensors`) | Meaning |
| --- | --- | --- |
| Current | `normal.current` | sum of the member currents (follows `invert_ha_dis_charge_measurements`) |
| Power | `normal.power` | sum of pack voltage × current per member (W) |
| SOC | `normal.soc` | SOC weighted with each member's learned full capacity |
| Voltage Median / Voltage Range | `normal.voltage_median` / `normal.voltage_range` | median and max − min of the pack voltages |
| Highest / Lowest Cell Voltage (+ Pack) | `normal.highest_cell_voltage[_pack]`, `normal.lowest_cell_voltage[_pack]` | cell extrema and the pack (DIP address) |
| Highest / Lowest Cell Temperature (+ Pack) | `normal.highest_cell_temperature[_pack]`, `normal.lowest_cell_temperature[_pack]` | temperature extrema and the pack |
| Members Reporting | `normal.members_reporting` | members with fresh data; the membership is in its attributes |
| Sample Age | `normal.sample_age` | age of the oldest member sample (s) |

If a member is offline or has no fresh data, all aggregates are unknown (never computed from a part of the set).
A set whose packs are not all configured, or any set in single pack mode, is inactive; deleted or inactive sets
are removed from Home Assistant.

### Cell types and datasheet checks

The **Cells** tab lists the bundled cell database (a pinned copy of the
[OpenBMS Monitor](https://github.com/privatecoder/openbms-monitor) cell database, updated with add-on releases) with
the datasheet limits and their source. Own cell types can be created, copied from a bundled type as a template,
edited, deleted and exchanged as JSON (the OpenBMS cell array format); they are stored in `/data/monitoring.json` and
are part of Home Assistant backups.

In the **Packs** tab each pack gets a cell type and the number of cells in parallel (the series count is measured).
The assignment keeps a copy of the datasheet entry: an add-on update never changes the limits of an existing
assignment, a newer version of the entry is only offered (with the changed fields) and taken over when you accept it.
If the pack reports a different BMS serial than when it was assigned (RS485-1/2, or MAINT on the CAN port), its
checks pause until you confirm the replacement.

With a cell type assigned, the add-on checks per pack:

- **live**: cell voltages against the charge/discharge cut-off, the cell temperatures against the charge or
  discharge range for the current direction, and the charge/discharge current against the datasheet (including
  the temperature derating table, × cells in parallel);
- **parameters** (only where the BMS parameters are readable): the BMS alarm/protection settings against the
  datasheet.

Each result is `ok`, `near`, `over` or `not evaluated` (missing data or values are never shown as ok). Full Energy
uses the nominal voltage of the assigned cell type. Nothing is written to the BMS.

**Balancer profiles** (Packs tab) describe an external active balancer (start/stop thresholds in mV and when it
runs). For a pack with a profile the add-on estimates from the cell voltages whether the balancer is working
(`active` / `inactive` / unknown); it does not talk to the balancer.

Example log output:

```text
[13:03:09] INFO: Starting Seplos MQTT RS485 Add-on...
[13:03:09] INFO: MQTT service configured: core-mosquitto:1883
[13:03:11] INFO: Starting Seplos BMS data fetcher...
2025-11-15 13:03:11 INFO:SeplosBMS.settings:created monitoring.json from the add-on options
2025-11-15 13:03:11 INFO:SeplosBMS:Starting Seplos BMS Data Fetcher
2025-11-15 13:03:11 INFO:SeplosBMS.mqtt:Connected to MQTT broker (core-mosquitto:1883)
2025-11-15 13:03:11 INFO:SeplosBMS:Bridging RS485 gateway 192.168.100.58:4196 to /tmp/vcom0
2025-11-15 13:03:11 INFO:SeplosBMS:Initializing serial interface /tmp/vcom0 at 9600 baud
2025-11-15 13:03:11 INFO:SeplosBMS:Initialized 2 battery pack(s)
2025-11-15 13:03:11 INFO:SeplosBMS:Health endpoint started on http://0.0.0.0:8080/health
2025-11-15 13:03:11 INFO:SeplosBMS:Settings app listening on port 63412
2025-11-15 13:03:11 INFO:SeplosBMS:Sending Home Assistant Auto-Discovery configurations
2025-11-15 13:03:11 INFO:SeplosBMS:Auto-Discovery configurations sent
2025-11-15 13:03:11 INFO:SeplosBMS.pack:Pack0:Requesting data...
2025-11-15 13:03:11 INFO:SeplosBMS.pack:Pack0:Telemetry received
2025-11-15 13:03:12 INFO:SeplosBMS.pack:Pack0:Telesignalization received
2025-11-15 13:03:13 INFO:SeplosBMS.pack:Pack0:DeviceInfo received
2025-11-15 13:03:13 INFO:SeplosBMS:Pack0:Publishing updated data to MQTT
2025-11-15 13:03:13 INFO:SeplosBMS.pack:Pack1:Requesting data...
2025-11-15 13:03:14 INFO:SeplosBMS.pack:Pack1:Telemetry received
2025-11-15 13:03:15 INFO:SeplosBMS.pack:Pack1:Telesignalization received
2025-11-15 13:03:15 INFO:SeplosBMS:Pack1:Publishing updated data to MQTT
2025-11-15 13:03:17 INFO:SeplosBMS:System:Publishing updated data to MQTT
2025-11-15 13:03:17 INFO:SeplosBMS:Waiting 60 seconds before next cycle
```

## MQTT topics (default prefix: seplos)

- seplos/pack-{n}/sensors: JSON with the groups `telemetry`, `telesignalization`, `info` and (single pack mode only) `status` and `parameters`
- seplos/pack-{n}/heartbeat: last_publish (time of the last successful poll, ISO 8601 with UTC offset) and publish_counter (JSON)
- seplos/pack-{n}/availability: per-pack online/offline
- seplos/system/sensors: system aggregate of the master over all packs (multiple pack mode only)
- seplos/system/availability: system online/offline (multiple pack mode only)
- seplos/set-{id}/sensors: values of a parallel set (`normal`) and its membership (`set`)
- seplos/set-{id}/availability: online while the set is active
- seplos/availability: global online/offline

Home Assistant discovery uses the prefix defined by ha_discovery_prefix (default: homeassistant).

## Sensor reference

Home Assistant names describe what a value actually does (verified against the BMS firmware). The third column maps it to the name used in the Seplos protocol documentation ("SEPLOS BMS Communication Protocol V2.0"), so raw values can be traced back. "–" means the value isn't part of the documented protocol.

| Home Assistant name | MQTT key (JSON path) | Seplos protocol doc name / source |
|---|---|---|
| Voltage Cell 1-M | `telemetry.normal.voltage_cell_N` | "Voltage of cell 1..M" (0x42), M = cell count (P65) |
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
| Highest Cell Temperature | `telemetry.normal.highest_cell_temperature` | – (maximum of the 4 cell temperatures, 0x42) |
| Lowest Cell Temperature | `telemetry.normal.lowest_cell_temperature` | – (minimum of the 4 cell temperatures, 0x42) |
| Delta Cell Temperature | `telemetry.normal.delta_cell_temperature` | – (calculated from 0x42) |
| Ambient Temperature | `telemetry.normal.ambient_temperature` | "Environment temperature" (0x42) |
| MOSFET Temperature | `telemetry.normal.components_temperature` | "Power temperature" (0x42), MOSFET/power stage |
| Current (0 when idle) | `telemetry.normal.dis_charge_current` | "Charge/discharge current" (0x42), 0 when idle |
| Power | `telemetry.normal.dis_charge_power` | – (calculated: Current × pack voltage) |
| Pack Voltage | `telemetry.normal.total_pack_voltage` | "Total battery voltage" (0x42) |
| Rated Capacity (Setting) | `telemetry.normal.rated_capacity` | "Rated capacity" (0x42), setting P58 |
| Full Capacity | `telemetry.normal.full_capacity` | "Battery capacity" (0x42), learned full capacity |
| Full Energy | `telemetry.normal.full_energy` | – (calculated: Full Capacity × cell count × nominal voltage of the assigned cell type, else the connection setting `nominal_cell_voltage`; never × cells in parallel, kWh) |
| Remaining Capacity | `telemetry.normal.residual_capacity` | "Residual capacity" (0x42) |
| State of Charge | `telemetry.normal.state_of_charge` | "SOC" (0x42) |
| Cycle Count | `telemetry.normal.charging_cycles` | "Number of cycles" (0x42) |
| Port Voltage (P+/P-) | `telemetry.normal.port_voltage` | "Port voltage" (0x42) |
| Current | `telemetry.normal.current` | "Charge/discharge current" (0x42), while idle "Reservation" 2 (0x42, 1 mA resolution) |
| Current Sense Offset | `telemetry.normal.current_offset` | "Reservation" 1 (0x42): reference/offset channel of the current measurement, mA |
| Energy Charged (total) | `telemetry.normal.energy_charged` | "Reservation" 3 (0x42): charged energy, 0.1 kWh raw |
| Energy Discharged (total) | `telemetry.normal.energy_discharged` | "Reservation" 4 (0x42): discharged energy, 0.1 kWh raw |
| Voltage Alarm Cell 1-M | `telesignalization.normal.cell_voltage_alarm_N` | "Cell 1..M alarm" (0x44) |
| Cell Temperature Alarm 1-4 | `telesignalization.normal.cell_temperature_alarm_N` | "Cell temperature alarm 1..4" (0x44) |
| Balancing Cell 1-M | `telesignalization.binary.balancer_cell_N` | "Equilibrium state 1/2" (cells 1-8 / 9-16) (0x44) |
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
| Severe Fault (Latched Until Restart) | `telesignalization.binary.severe_fault` | Alarm event 4 bit 7: sensor fault, cell ≥ P4 + 50 mV, cell ≤ P6 − 200 mV or overcurrent beyond P54/P55; stays set until the BMS restarts, only reported with function switch BitGroup3 bit 7 (0x44) |
| Charger Overvoltage Protection | `telesignalization.binary.charging_high_voltage_protection` | Alarm event 6 bit 0 "Charge high voltage protection" (charger > P18) (0x44) |
| Intermittent Recharge Waiting | `telesignalization.binary.intermittent_power_supplement` | Alarm event 6 bit 1 "Intermittent recharge waiting" (0x44): the pack no longer counts towards the charge capability (CCL); with the OEM soft stop the charge MOSFET stays on |
| Charging Blocked (Cell < Min.) | `telesignalization.binary.cell_low_voltage_forbidden_charging` | Alarm event 6 bit 4 "Cell low voltage charging prohibition" (cell < P9) (0x44) |
| Output Connection Failure | `telesignalization.binary.output_connection_failure` | Alarm event 6 bit 6 "Output connection fault" (0x44): GPIO input low for ≥ 5 s; parameter P82 is not used by the firmware |
| Charge Activation Window Active (Auto) | `telesignalization.binary.auto_charging_wait` | Alarm event 7 bit 4 "Automatic charging waiting" (0x44): a P72-minute window is running in which undervoltage/empty protections are cleared so a charger can charge |
| Charge Activation Window Active (Manual) | `telesignalization.binary.manual_charging_wait` | Alarm event 7 bit 5 "Manual charging waiting" (0x44), once after power-on |
| EEPROM Failure | `telesignalization.binary.eep_storage_failure` | Alarm event 8 bit 0 "EEP storage fault" (0x44) |
| RTC Clock Failure | `telesignalization.binary.rtc_clock_failure` | Alarm event 8 bit 1 "RTC error" (0x44) |
| No Calibration Of Voltage | `telesignalization.binary.no_calibration_of_voltage` | Alarm event 8 bit 2 "Voltage calibration not performed" (0x44) |
| No Calibration Of Current | `telesignalization.binary.no_calibration_of_current` | Alarm event 8 bit 3 "Current calibration not performed" (0x44) |
| Current Zero Point Not Calibrated | `telesignalization.binary.no_calibration_of_null_point` | Alarm event 8 bit 4 "Zero calibration not performed" (0x44) |
| RTC Time Invalid | `telesignalization.binary.rtc_time_invalid` | Alarm event 8 bit 5: the RTC lost its time (RTC register 2 bit 7) (0x44) |
| Discharge MOSFET | `telesignalization.binary.discharge_switch` | On-off state bit 0 "Discharge switch state" (0x44) |
| Charge MOSFET | `telesignalization.binary.charge_switch` | On-off state bit 1 "Charge switch state" (0x44) |
| Current Limiter | `telesignalization.binary.current_limit_switch` | On-off state bit 2 "Current limit switch state" (0x44) |
| Heater | `telesignalization.binary.heating_switch` | On-off state bit 3 "Heating switch state" (0x44) |
| Device Name | `info.normal.device_name` | name reported by the BMS (0x51 bytes 0-9), e.g. `SP16S020` |
| Firmware Version | `info.normal.firmware_version` | software version (0x51 bytes 10-11) |
| Firmware Patch (derived) | `info.normal.firmware_patch` | – (derived from features, see firmware analysis) |
| CAN Protocol | `info.normal.can_protocol` | manufacturer name field = active CAN protocol (0x51 bytes 12-31) |
| Battery Serial Number | `info.normal.serial_number` | string 1 (CID2 0xA2, RS485-1/2 only) |
| BMS Serial Number | `info.normal.bms_serial_number` | string 2 (CID2 0xA4, RS485-1/2 only) |
| BMS Clock Offset | `clock.normal.clock_offset` (s, BMS minus host local time), attribute `bms_time` | CID2 0x4D (RS485-1/2; on the CAN port only with MAINT), every 10 min. The BMS clock has no time zone; it's compared with the add-on's local time |
| Cell Type | `cells.normal.cell_type` (text: manufacturer, model, variant), attributes `cells.cell_type` (`id`, `entry_version`, `manufacturer`, `model`, `chemistry`, `nominal_v`, `nominal_ah`, `parallel`, `series`, `update_available`) | – (the cell type assigned to the pack in the settings app; unknown without assignment) |
| Datasheet Check (Live) | `cells.normal.datasheet_check_live` (`ok`, `near`, `over`, `not_evaluated`), attributes `cells.live` (`reason`, `checks`: `key`, `result`, `limit`, `unit`, `reason`; the measured values are not part of the attributes, they would rewrite the HA state on every poll, and are in the settings app's `/api/status`) | – (cell voltages, cell temperatures and currents against the datasheet of the assigned cell type; worst result, one `not_evaluated` check makes an otherwise `ok` summary `not_evaluated`) |
| Datasheet Check (Parameters) | `cells.normal.datasheet_check_parameters`, attributes `cells.parameters` (same shape) | – (the BMS protection settings against the datasheet; only where the parameters are readable: RS485-1/2 or MAINT on the CAN port) |
| Datasheet Charge Current Limit | `cells.normal.datasheet_charge_current_limit` (A, disabled by default) | – (advisory: charge current the datasheet allows at the present cell temperatures × cells in parallel; 0 when not allowed) |
| External Balancer | `cells.normal.external_balancer` (MQTT payload `active`, `inactive`, `unknown`; HA enum options `active` and `inactive`, `unknown` shows as HA's own unknown state; only for packs with a balancer profile assigned in the settings app; the settings app's `/api/status` reports `active`, `inactive`, `unknown`) | – (computed, not read from the device: the spread of the cell voltages and the pack current against the manual settings of the profile - start/stop spread, start/stop cell voltage, charging condition - with their hysteresis; `unknown` after a start, a stale sample, an interruption of the readings longer than the pack's freshness allowance, an identity mismatch or a change of the assignment or the profile, until a sample decides) |
| Datasheet Discharge Current Limit | `cells.normal.datasheet_discharge_current_limit` (A, disabled by default) | – (advisory: continuous discharge current inside the discharge temperature range × cells in parallel; 0 outside) |
| Last Successful Poll | heartbeat `last_publish` | – (time of the last successful poll, also when no value changed) |
| Current (incl. idle) | `status.normal.dis_charge_current_unfiltered` | intra-pack record current (undocumented CID2 0x5A) |
| Balancing Active | `status.binary.balancing_active` | Alarm event 7 bit 0 "Inside bit" (0x5A) |
| Balancing Not Running (Blocked / Not Needed) | `status.binary.balancing_blocked` | Alarm event 7 bit 1 "Inside bit" (0x5A) |
| Balancing Time Limit Reached | `status.binary.balancing_time_limit` | Alarm event 7 bit 2 "Inside bit" (0x5A) |
| Balancing Blocked (Temperature) | `status.binary.balancing_temperature_blocked` | Alarm event 7 bit 3 "Inside bit" (0x5A) |
| Charge Blocked (Charge MOSFET Off Request) | `status.binary.charge_forbidden` | Alarm event 8 bit 6 "Inside bit" (0x5A): also normal while recharge waiting with hard stop or before a shutdown |
| Current Limiter Active (Charging Detected) | `status.binary.limiter_charging_from_10a` | Alarm event 7 bit 6 (0x5A): active current limiting (BitGroup6 bit 6) as soon as charging is detected (normally from +0.6 A), immediately from 10 A; stays on until 5 s without charging |
| Current Limiter Active (After Charge Overcurrent Warning) | `status.binary.limiter_after_charge_overcurrent_warning` | Alarm event 7 bit 7 (0x5A): passive current limiting for P71 minutes (BitGroup6 bit 7) |
| Precharge Running | `status.binary.precharge_running` | On-off state bit 4 (0x5A), P57 |
| External Voltage Detected (Charger) | `status.binary.external_voltage_detected` | System state bit 2 "floating charge" (0x5A): a charger/external voltage is detected at the port, no float mode |
| Charge Current Status (>= 3 A) | `status.binary.charge_current_status` | System state bit 7 (0x5A), with hysteresis |
| Discharge Current Status (>= 3 A) | `status.binary.discharge_current_status` | System state bit 6 (0x5A), with hysteresis |
| Parameters | `parameters` (all as attributes), state: `parameters.read_at` | parameters 0-86, function switches (raw `function_switches` and named, e.g. `intermittent_recharge`, `passive_current_limiting`), `disabled_alarm_bits` per alarm event 2-6, `ccl_base_mode` (CID2 0x47, "Acquisition of teleregulation information") |
| System State (Aggregate) (System) | `system: normal.system_state` | – undocumented Modbus RTU FC03 0x1000 (master) |
| System Voltage (System) | `system: normal.system_voltage` | – undocumented Modbus RTU FC03 0x1000 (master) |
| System Current (System) | `system: normal.system_current` | – undocumented Modbus RTU FC03 0x1000 (master) |
| System Power (System) | `system: normal.system_power` | – (calculated: System Voltage × System Current) |
| System SOC (System) | `system: normal.system_soc` | – undocumented Modbus RTU FC03 0x1000 (master) |
| Total Full Capacity (System) | `system: normal.total_capacity` | – undocumented Modbus RTU FC03 0x1000 (master): sum of the learned full capacities |
| Total Full Energy (System) | `system: normal.total_full_energy` | – (calculated: sum of the Full Energy of all configured packs, unknown while one of them is unknown, kWh) |
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
| Force Charge Request I (SOC <= 5 %) (System) | `system: binary.force_charge_1` | – undocumented Modbus RTU FC03 0x0074 bit 5 (master, firmware 16.06.11+) |
| Force Charge Request II (SOC <= 9 %) (System) | `system: binary.force_charge_2` | – undocumented Modbus RTU FC03 0x0074 bit 4 (master, firmware 16.06.11+) |

Notes:
- `Current (0 when idle)` is reported as 0 by the BMS as long as it is neither charging nor discharging. `Current` combines it with the idle current the BMS sends in a "reserved" field (1 mA resolution), so it always shows the real current.
- `Energy Charged/Discharged (total)` are the BMS' own energy counters (current × voltage summed every second while charging/discharging) and can be used in the Home Assistant energy dashboard. From 6500 kWh on the BMS sends a fixed 650.0 kWh instead. The add-on recognizes that fallback from the last valid value, which it keeps in `/data/seplos_state.json` across restarts, and then keeps publishing the last value. Without any stored value a 650.0 is ambiguous and published as unknown until the counter moves. The counters are reset by the BMS command "clear history".
- The extended status (`status`, single pack mode) bridges single failed requests, but becomes unknown when it is older than 60 s (or 3 update intervals), and is unknown until the first successful read after a restart. The force charge flags of the system device become unknown when register 0x0074 doesn't answer.
- The energy fallback detection keeps the last valid value per pack address. After replacing or re-addressing a pack, delete `/data/seplos_state.json` (via the add-on's data folder) or the new pack's first 650.0 kWh might be held at the old pack's value. A bare 650.0 after a higher stored value is always treated as the fallback; values are only written every 5 minutes, so a crash can lose the most recent increase.
- The system device shows the master's aggregate over all packs via Modbus, not a capture of what a specific CAN/RS485 inverter profile sends (profiles add their own rules, e.g. SOC capping). System voltage is the average of the online packs, system current their sum with its own idle dead band (about −0.8 … +1.0 A reads 0) and 0.1 A resolution, Total Full Capacity the sum of the learned full capacities. Pack numbers are 0-based like the pack devices.
- Packs with fewer than 16 cells (7-16 are supported): the per-cell entities above the cell count are removed once the first telemetry is read. With ≤ 8 cells, cell temperatures 3/4 are copies of temperature 2. The Lovelace example ignores unknown/unavailable cells, so it also works for smaller packs.
- Version 2.0.0-beta6 renamed these `parameters` attributes: `overcurrent_recovery_attempts` → `transient_or_short_circuit_lockout_event_count` (P70, lockout after this many transient overcurrent/short circuit events), `charge_current_limit_delay_min` → `charge_current_limit_duration_min` (P71), `charge_activation_delay_min` → `charge_activation_window_min` (P72), `charge_current_limit_mode` → `ccl_base_mode`. The Parameters state is now the time of the last successful read. `connection_fault_impedance_mohm` (P82) is stored but not used by the firmware; its 0.1 mΩ scale (also for P84/P86) is taken from the BatteryMonitor export and not verified in the firmware. The JSON paths of the cell temperature extrema moved from `status.normal.highest_cell_temperature` / `lowest_cell_temperature` to `telemetry.normal.*` (they are calculated from 0x42 now, also in multiple pack mode); the entity IDs are unchanged.
- Alarm/protection pairs report the more severe state (Protection/Lockout before Alarm).
- The doc's "State of health" (always 100 %), "Disconnection state" (always 0) and alarm event 6 bit 5 "reverse polarity" (no firmware code sets it) are not published.

## MAINT custom firmware (optional)

With the option `maint_firmware: true` the add-on also reads the extra data of the MAINT 1.5.4 / 1.6.4 custom firmware (built from OEM firmware with the offline patcher of the Seplos knowledge base). Everything is read-only, nothing is written to the BMS.

- Each pack is identified on its own via ASCII 64/03 (MNT05: MAINT version, reported OEM base, build ID, image flags, read capabilities). Packs that answer with an error (OEM firmware) or stay silent three times keep the normal OEM reads. Mixed banks work; a patched master says nothing about its slaves.
- Full-width energy counters (64/06, live, 0.1 kWh, no 6500 kWh fallback) replace `Energy Charged/Discharged (total)`. If that read fails for more than 10 minutes the counters become unknown instead of falling back to the 0x42 value. The same answer reports the two persisted SOC records (class, CRC acceptance, boot choice) as attributes of `MAINT Firmware`.
- Flash storage diagnostics (64/04, image flag 0x20, every 10 min): `Flash Storage Problem` turns on if a parameter or SOC mirror record is invalid. The raw classification and the 1.6.4 lock bytes are attributes (`null` = unavailable, not "unlocked").
- Multiple pack mode (CAN port, UART1): parameters and function switches (binary 47, hourly) and the battery/BMS serial numbers (binary A2/A4) of every MAINT pack, which the OEM firmware only serves on RS485-1/2.
- The derived firmware patch level is replaced by the OEM base version the MAINT image reports.
- Supported releases are MAINT **1.5.4 and 1.6.4** (identity format MNT05) and newer. **Deprecated:** older builds that answer with MNT03/MNT04 (e.g. MAINT 1.6 build `18daf61d8a93671d`, image flags 0x06) still work for testing, but this support will be removed in one of the next versions; the add-on logs a warning for them. Currently they deliver: identity, maintenance marker, clock (flag 0x02), parameters and serial numbers on the CAN port, and the individual pack lock from BitGroup0 bit 5 of the parameters (read every 5 min, these builds have no 64/04). They use inverse polarity (no flag 0x08): BitGroup0 bit 3 = 0 permits UART1 power/RTC, bit 4 = 0 enables extended off. They have no full-width energy (64/06) and no storage diagnostics (64/04).
- 1.6.4 (image flag 0x04): `Individual Pack Lock Active` from the runtime lock cache (64/04, every 60 s). `null`/unknown if the cache is not valid. It's normally the persistent lock set by a targeted off command (the add-on itself never sends one), but the firmware also locks conservatively after storage failures, and the runtime state alone doesn't prove that both mirrors keep the lock across a reboot: see `lock_recorded_primary/backup` and `lock_cache_consistent` in the `storage` attributes.
- `Maintenance Mode (Slave Polling Paused)` and `Effective Slave Count` (64/02, every 60 s; on the CAN port only the master answers). While the marker is active the master doesn't poll its slaves, so the system values cover the local pack only.
- `Recharge Waiting Stop Mode` (hard stop / soft stop): the configuration from BitGroup0 bits 6/7 and the image default (flag 0x80), not proof that charging is currently stopped. All MAINT meanings of BitGroup0 (UART1 write/maintenance/power permissions, extended off, recorded lock) are attributes (`settings`) of `MAINT Firmware`.
- Active calibration (64/05 page 00, every 10 min): invalid calibration flags and the converted currents A/B in mA as attributes (`calibration`).

| Home Assistant name | MQTT key (JSON path) | Source |
|---|---|---|
| MAINT Firmware | `maint.normal.maint_version`, attributes `maint.attributes` (identity, storage, checkpoint, live snapshot) | ASCII 64/03, 64/04, 64/06 |
| Flash Storage Problem | `maint.binary.storage_problem` | ASCII 64/04 |
| Individual Pack Lock Active | `maint.binary.pack_lock_active` | ASCII 64/04 byte 9 (1.6.4) |
| Maintenance Mode (Slave Polling Paused) | `maint.binary.maintenance_mode` | ASCII 64/02 marker |
| Effective Slave Count | `maint.normal.effective_slave_count` | ASCII 64/02 |
| Recharge Waiting Stop Mode | `maint.normal.recharge_stop_mode` | BitGroup0 bits 6/7 + image flags (0x47) |

The current MAINT images were validated offline only (emulator), not on hardware. Keep the option off unless your packs run MAINT firmware.

## Home Assistant setup

### Auto-discovery

- If Enable HA Auto-Discovery is on, the add-on publishes MQTT discovery configs and devices appear automatically.

### Lovelace example

The provided `ha-lovelace/lovalace.yaml` uses custom cards such as `mushroom-template-card`, `entity-progress-card`, `button-card`, `bar-card`, `card_mod`, and `browser_mod` (installable via HACS).

<img width="400" alt="lovelace sample multiple packs card" src="https://github.com/user-attachments/assets/814f7540-57a1-40db-9ac1-5d1a3e9a19a3" />
<img width="400" alt="lovelace sample pack info" src="https://github.com/user-attachments/assets/829acb11-d50c-43a0-b818-96fbe28907aa" />
<img width="400" alt="lovelace sample pack errors and warnings" src="https://github.com/user-attachments/assets/419ed442-98ec-4985-98a6-6b4f5b4b9841" />
