# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant add-on that polls Seplos V2/V16 BMS packs (protocol "SEPLOS BMS Communication Protocol V2.0") over RS485 and publishes the data to MQTT, optionally with HA MQTT discovery. Pure Python 3 with only `paho-mqtt` and `pyserial` (installed via Alpine `apk` in the `Dockerfile`, no requirements file). All HA base images ship paho-mqtt 1.6.1 and Python 3.12 (armv7/armhf/i386, Alpine 3.22) to 3.14 (amd64/aarch64), so the code must run on Python 3.12 with the Paho 1.x API.

## Development commands

The add-on is built by the HA Supervisor from `Dockerfile` + `build.json` (HA base images per arch).

- Test env: `uv venv -p python3.12 .venv && uv pip install -p .venv pytest "paho-mqtt<2" pyserial ruff`
- Tests (no hardware, no broker): `.venv/bin/pytest`; one test: `.venv/bin/pytest tests/test_characterization.py::test_single_pack_poll`
- Lint: `.venv/bin/ruff check src tests`
- Golden files (`tests/golden/*.json`) pin the public output (decoder JSON, every discovery publish, main-loop topics). A test fails on any difference; regenerate deliberately with `UPDATE_GOLDEN=1 .venv/bin/pytest` and review the diff.
- Import smoke test in a real base image: `docker build --platform linux/arm/v7 --build-arg BUILD_FROM=ghcr.io/home-assistant/armv7-base:latest -t seplos-test .` then `docker run --rm --entrypoint sh seplos-test -c 'cd /usr/src/app && python3 -c "import seplos_mqtt.app"'`
- Run locally (needs a reachable BMS on a serial device and an MQTT broker): configure via the env vars `run.sh` exports (`SERIAL_INTERFACE`, `NUMBER_OF_PACKS`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TOPIC`, `LOGGING_LEVEL`, …), then `cd src && python3 -u fetch_bms_data.py`. For a TCP RS485 gateway, create the pty first like `run.sh` does: `socat pty,link=/tmp/vcom0,raw tcp:<ip>:4196,retry,interval=.2,forever &`.

## Tests

`tests/frames.py` builds synthetic wire frames in protocol order from the knowledge base and deliberately does not import production offsets. `tests/fakes.py` has a fake bus (`FakeBms` answers per `(address, cid2)` / Modbus `(fc, register, count)` and validates every outbound ASCII request) and a fake MQTT client. `tests/api.py` is the only file that knows the production module layout; tests call it, so refactorings change `api.py`, not the tests.

## Releases

Each release bumps `version` in `config.yaml` (currently `2.0.0-betaN`) and is committed as `vX.Y.Z[-betaN]: <summary>` with a bullet-point body explaining the *why*. The commit messages and the docs are in English. The translations and some code comments are in German.

## Architecture

Startup chain: `run.sh` (bashio) reads the add-on options and the MQTT service credentials, exports them as env vars, optionally starts `socat` to bridge a remote RS485-to-Ethernet gateway to a local pty, then `exec`s `src/fetch_bms_data.py`, a thin launcher for `seplos_mqtt.app.main()`. The `Dockerfile` copies all of `src/`.

**Adding a config option** means touching `config.yaml` (`options` + `schema`), `run.sh` (export), `Config.from_env()` in `src/seplos_mqtt/config.py`, and both `translations/en.yaml` and `translations/de.yaml`.

### Two operating modes, decided by `NUMBER_OF_PACKS`

`Config.multi_pack` (`number_of_packs > 1`) is the central switch:

| | Single pack (`== 1`) | Multiple packs (`> 1`) |
|---|---|---|
| Physical port | RS485-1/2 (UART0) | RS485 pins of the CAN socket (UART1) |
| Baud | 19200 | 9600 |
| Extra data | extended status (CID2 0x5A), parameters (0x47), serial number (0xA2) | system values via Modbus RTU FC03 0x1000 x21 from the master (`SystemReader`) |
| HA discovery | `pack_bus=True`, status/parameter entities, system device removed | `pack_bus_only` entities removed, "Seplos BMS System" device created |

The UART1 bus only answers 0x42/0x44/0x4F/0x51/0x60-0x63 plus Modbus. Don't add requests there without checking that the firmware supports them. On RS485-1/2 the master itself polls its slaves with CID2 0x5A, so `SerialBus.read_frame()` skips (and debug-logs) those interleaved intra-pack frames.

### `src/seplos_mqtt/`

- `framing/ascii.py`, `framing/modbus.py`: encode requests, validate answers (no I/O).
- `transport.py`: `SerialBus`, the single owner of the serial port. Every request (ASCII, Modbus, later MAINT binary frames and commands) goes through it.
- `decoders/*`: pure functions, binary INFO bytes in, JSON-ready dicts out (`telemetry` 0x42, `alarms` 0x44 incl. the `BIT_FIELDS` table, `intrapack` 0x5A, `identity` 0x51/0xA2, `parameters` 0x47, `system` Modbus 0x1000).
- `pack.py`: `BatteryPack` (one per DIP address 0..n-1) schedules the requests of one poll and caches static data. `read()` returns `(data_or_None_if_unchanged, poll_success)`, so data is only published when it changed. Device info is fetched once; the firmware reports only major.minor, so the **patch level is derived by probing features** (`_derive_patch_pack_bus` / `_derive_patch_can_port`). On the CAN port bus only the master can be classified.
- **A pause between consecutive BMS requests is mandatory** (`request_gap()`, 1 s). Without it the responses get corrupted, so keep it when adding requests.
- `app.py`: composition and main loop. Polls the packs round-robin, publishes data/heartbeat/availability, after each full cycle the system data, then sleeps `MQTT_UPDATE_INTERVAL`. `health.py` serves `/health` on :8080 for the Supervisor watchdog.
- `topics.py`: MQTT state topics and HA identifiers. These are public interfaces (automations, dashboards, retained discovery); keep them stable.

### Discovery (`src/seplos_mqtt/discovery/`)

- `catalog.py` is data: `Entity` entries keyed by the JSON key the decoder writes (`value_json.<group>.<normal|binary>.<key>`). Binary payloads of telesignalization entities come from the decoder's `alarms.BIT_FIELDS`, so they can't drift apart.
- `Discovery` renders and publishes retained configs to `<prefix>/<sensor|binary_sensor>/seplos-mqtt-pack-<n>/<key>/config` (system: `seplos-mqtt-system`) with short-form keys. `default_entity_id` keeps entity IDs stable (`seplos_bms_pack_<n>_<key>`). The full device block travels with the first config per device; `update_firmware()` republishes one config once the real firmware is known.
- **When renaming or dropping an entity key, add the old `(domain, key)` to `OBSOLETE`** so the retained discovery config gets cleared for existing users. Mode-specific entities (`pack_bus_only`, system) are cleared automatically.

### Docs and the Lovelace card

The README has a sensor reference table mapping each HA name ↔ MQTT JSON path ↔ Seplos protocol doc field. Keep it in sync when adding, renaming or reinterpreting values. `ha-lovelace/lovalace.yaml` is a sample dashboard that references entity IDs, so key renames affect it too.
