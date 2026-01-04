# Seplos MQTT RS485 Add-on

This is a Home Assistant Add-on that reads data from one or multiple V2 / V16 Seplos BMS (also called 10C and 10E) via a remote or local RS485 connection and publishes its/their stats to MQTT.

Sensor auto discovery can be disabled (optional)

## Hardware requirements

- A remote or local RS485 device ([the Waveshare 2-CH RS485 to ETH gateway has been tested](https://www.waveshare.com/2-ch-rs485-to-eth-b.htm))
- **For MULTIPLE packs**: A self-crimped cable with multiple plugs or one or more splitters ([this one works for me](https://www.amazon.de/gp/product/B00D3KIQXC)) – one of which always needs to be modified (more information [below](#wiring-the-rs485-device-to-multiple-battery-packs))
- **For a SINGLE pack**: A regular patch-cable from one of the two RS485-ports of the pack to the terminals of your RS485 device
- One or multiple [Seplos BMS V2 / V16](https://www.seplos.com/bms-2.0.html)
- A configured and running MQTT broker

## Wiring the RS485 device to MULTIPLE battery packs

Carefully check the provided wiring scheme [below](#wiring-sample) (the PINK lines). There are two ways to approach the same result:
1. A single, self-crimped cable with multiple plugs, crimped as shown in the picture below (recommended).
2. Two or more splitters, one of which need to be modified (easier if you don't like crimping) – if you have two packs, you need two splitters, three packs require three splitters and so on.

When using splitters, the **first** splitter (the one that connects to the Master's CAN-port) needs to be modified like so:
- On one of the two outlets cut all but the three pins of the RS485-part (check the PINK lines in the [image below](#wiring-sample))
- On the other outlet cut all but the two pins of the CAN-part (check the PINK lines in the [image below](#wiring-sample))

Example for four packs:

- Connect the first, **modified** splitter to the **CAN-Port (NOT one of the RS485!) of the Master**
  - The outlet with only the two CAN-pins connect to your inverter using a regular patch-cable.
  - The outlet with only the three RS485-pins connect to the first slave using a regular patch-cable.
- Connect the second, **unmodified** splitter to the **CAN-Port (NOT one of the RS485!) of the first Slave**
  - Use one outlet to connect the regular patch-cable, coming from the Master
  - Use the other outlet to connect a regular patch-cable to the second Slave
- Connect the third, **unmodified** splitter to the **CAN-Port (NOT one of the RS485!) of the second Slave**
  - Use one outlet to connect the regular patch-cable, coming from the first Slave
  - Use the other outlet to connect a regular patch-cable to the third Slave
- Connect the fourth, **unmodified** splitter to the **CAN-Port (NOT one of the RS485!) of the third Slave**
  - Use one outlet to connect the regular patch-cable, coming from the second Slave
  - Use the other outlet to connect a regular patch-cable to your RS485-device (the one that is used to read the data, i.e. the Waveshare 2-CH RS485 to ETH gateway or similar)

### Wiring sample:

<img alt="wiring sample" src="https://github.com/user-attachments/assets/dc72fa68-df39-41e8-8033-4776d622d618" width="500">

### Seplos pin assignment (CAN-port)

- `1/8` => `RS485-B`
- `2/7` => `RS485-A`
- `4` => `CAN-H`
- `5` => `CAN-L`
- `3/6` => `GND`

<img width="400" alt="seplos pin assignment" src="https://github.com/user-attachments/assets/af477cbd-9cba-422f-9e0f-880e4e17fc45" />

### Waveshare pin assignment (RS485-port)

- `orange` => `RS485-A`
- `orange-white` => `RS485-B`
- `green-white` => `GND`

<img alt="waveshare gateway pinout" src="https://github.com/user-attachments/assets/442e0fee-5ec7-495b-81d7-013c56f1f304" width="100">

## Wiring the RS485 device to ONE SINGLE battery pack

When using a regular patch-cable, cut one of its connectors and take the `orange`, `orange-white` and `green-white` wires to crimp a terminal onto each of them. Then connect it to the waveshare device like so:

- `orange` => `RS485-A`
- `orange-white` => `RS485-B`
- `green-white` => `PE`

<img alt="waveshare gateway pinout" src="https://github.com/user-attachments/assets/442e0fee-5ec7-495b-81d7-013c56f1f304" width="100">

## Installation and configuration

1. Configure and setup an MQTT broker in Home Assistant
2. Configure your RS485 device. For the Waveshare 2-CH RS485 to ETH gateway this would most importantly be `IP Mode: Static` (must be a reachable IP within your network), `Port: 4196` (default), `Work Mode: TCP Server`, `Transfer Protocol: None`, `Baud Rate: 9600` (for **MULTIPLE** packs) **or** `Baud Rate: 19200` (for a **SINGLE** pack).
3. Install this Add-on
4. Go to the configurations-tab and enter all the required details, such as the IP address and port when using a remote RS485 device, or the locally available serial-port, number of battery packs etc.
5. Press save
6. Run the Add-on and check the logs. The should look like:

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
2025-11-15 13:03:12 INFO:SeplosBMS:Pack0:Publishing updated data to MQTT
2025-11-15 13:03:13 INFO:SeplosBMS:Pack1:Requesting data...
2025-11-15 13:03:14 INFO:SeplosBMS:Pack1:Telemetry received
2025-11-15 13:03:15 INFO:SeplosBMS:Pack1:Telesignalization received
2025-11-15 13:03:15 INFO:SeplosBMS:Pack1:Publishing updated data to MQTT
2025-11-15 13:03:16 INFO:SeplosBMS:Waiting 60 seconds before next cycle
```

## Configuring Home Assistant

### Add Device(s)

- If `Enable HA Auto-Discovery` is active, it triggers the publishing of auto discovery sensor configs in Home Assistant. The devices should be added automatically.

### Add Sensors to lovelace

- The provided `ha-lovelace/lovelace.yaml` is using custom add-ons like `mushroom-template-card`, `entity-progress-card`, `button-card`, `bar-card`, `card_mod` and `browser_mod` (can be installed via HACS) and allows for a first start (value-based colors are based on EVE LF280K datasheets and measurements and personal preferences).

<img width="400" alt="lovelace sample multiple packs card" src="https://github.com/user-attachments/assets/814f7540-57a1-40db-9ac1-5d1a3e9a19a3" />
<img width="400" alt="lovelace sample pack info" src="https://github.com/user-attachments/assets/829acb11-d50c-43a0-b818-96fbe28907aa" />
<img width="400" alt="lovelace sample pack errors and warnings" src="https://github.com/user-attachments/assets/419ed442-98ec-4985-98a6-6b4f5b4b9841" />
