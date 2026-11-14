# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant add-on that polls Seplos BMS 2.0 (EMU1101, hardware V16) packs (protocol "SEPLOS BMS Communication Protocol V2.0") over RS485 and publishes the data to MQTT, optionally with HA MQTT discovery. Pure Python 3 with only `paho-mqtt` and `pyserial` (installed via Alpine `apk` in the `Dockerfile`, no requirements file). All HA base images ship paho-mqtt 1.6.1 and Python 3.12 (armv7/armhf/i386, Alpine 3.22) to 3.14 (amd64/aarch64), so the code must run on Python 3.12 with the Paho 1.x API.

## Development commands

The add-on is built by the HA Supervisor from `Dockerfile` + `build.json` (HA base images per arch, pinned: Alpine 3.24 for amd64/aarch64, 3.22 for armv7/armhf/i386, the last ones published for those architectures).

- Test env: `uv venv -p python3.12 .venv && uv pip install -p .venv pytest "paho-mqtt<2" pyserial ruff`
- Tests (no hardware, no broker): `.venv/bin/pytest`; one test: `.venv/bin/pytest tests/test_characterization.py::test_single_pack_poll`
- Lint: `.venv/bin/ruff check src tests`
- Golden files (`tests/golden/*.json`) pin the public output (decoder JSON, every discovery publish, main-loop topics). A test fails on any difference; regenerate deliberately with `UPDATE_GOLDEN=1 .venv/bin/pytest` and review the diff.
- Import smoke test in a real base image: `docker build --platform linux/arm/v7 --build-arg BUILD_FROM=ghcr.io/home-assistant/armv7-base:3.22 -t seplos-test .` then `docker run --rm --entrypoint sh seplos-test -c 'cd /usr/src/app && python3 -c "import seplos_mqtt.app"'`
- Run locally (needs a reachable BMS on a serial device and an MQTT broker): configure via the env vars `run.sh` exports (`SERIAL_INTERFACE`, `NUMBER_OF_PACKS`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TOPIC`, `LOGGING_LEVEL`, …), then `cd src && python3 -u fetch_bms_data.py`. For a TCP RS485 gateway, create the pty first like `run.sh` does: `socat pty,link=/tmp/vcom0,raw tcp:<ip>:4196,retry,interval=.2,forever &`.

## Tests

`tests/frames.py` builds synthetic wire frames in protocol order from the knowledge base and deliberately does not import production offsets. `tests/fakes.py` has a fake bus (`FakeBms` answers per `(address, cid2)` / Modbus `(fc, register, count)` and validates every outbound ASCII request) and a fake MQTT client. `tests/api.py` is the only file that knows the production module layout; tests call it, so refactorings change `api.py`, not the tests.

## Releases

Each release bumps `version` in `config.yaml` (currently `2.0.0-betaN`) and is committed as `vX.Y.Z[-betaN]: <summary>` with a bullet-point body explaining the *why*. The commit messages and the docs are in English. The translations and some code comments are in German.

## Architecture

Startup chain: `run.sh` (bashio) reads the add-on options and the MQTT service credentials, exports them as env vars (plus `INGRESS_PORT` and `MONITORING_FILE`), then `exec`s `src/fetch_bms_data.py`, a thin launcher for `seplos_mqtt.app.main()`. `main()` calls `startup_config()`: the connection settings come from `/data/monitoring.json` (`monitoring/`), created once from the legacy add-on options (they stay in `config.yaml`, deprecated, only as migration input); a corrupt file falls back to the options so polling continues. Python starts the `socat` bridge for a remote gateway (`bridge.py`, supervised in the main loop), and the Ingress web server (`web/server.py`, aiohttp) runs in its own thread. A saved connection change calls `AppState.request_restart`; the main loop leaves at the next pack boundary, runs `graceful_exit` and re-executes the launcher with `os.execv`. The `Dockerfile` copies all of `src/`.

**Adding a config option**: connection settings live in `monitoring/model.py` (`ConnectionSettings`), `monitoring/migration.py` (legacy env), `Config.from_monitoring()` and the app UI; they need no `config.yaml` entry. Options that stay in the add-on configuration (MQTT, discovery, logging) mean touching `config.yaml` (`options` + `schema`), `run.sh` (export), `Config.from_env()` and `Config.from_monitoring()` in `src/seplos_mqtt/config.py`, and both `translations/en.yaml` and `translations/de.yaml`.

### Two buses, decided by `Config.can_bus`

`Config.can_bus` (`bus == "uart1"` or `number_of_packs > 1`) decides what is read; several packs are always on UART1, a
single pack on either (connection setting `bus`). `Config.multi_pack` (`number_of_packs > 1`) only matters for
parallel sets and the "mode" label.

| | RS485-1/2 (`bus: uart0`, one pack) | CAN socket (`bus: uart1`) |
|---|---|---|
| Physical port | RS485-1/2 (UART0) | RS485 pins of the CAN socket (UART1) |
| Baud | 19200 | 9600 |
| Extra data | extended status (CID2 0x5A), parameters (0x47), serial number (0xA2) | system values via Modbus RTU FC03 0x1000 x21 from the master (`SystemReader`) |
| HA discovery | `pack_bus=True`, status/parameter entities, system device removed | `pack_bus_only` entities removed, "Seplos BMS System" device created |

The UART1 bus only answers 0x42/0x44/0x4F/0x51/0x60-0x63 plus Modbus. Don't add requests there without checking that the firmware supports them. On RS485-1/2 the master itself polls its slaves with CID2 0x5A, so `SerialBus.read_frame()` skips (and debug-logs) those interleaved intra-pack frames.

### `src/seplos_mqtt/`

- `framing/ascii.py`, `framing/modbus.py`: encode requests, validate answers (no I/O).
- `transport.py`: `SerialBus`, the single owner of the serial port. Every request (ASCII, Modbus, EMU1101-EXT binary frames, later commands) runs as one locked transaction and waits for the bus quiet (connection setting `bus_quiet_ms`, 200–2000 ms, default 1000; 200 ms while the OpenBMS Monitor feed is on) of continuous silence (bytes arriving meanwhile are discarded and restart the timer; `BusBusy` if it can't be established), retries included. Never `sleep` between requests elsewhere. A later command path must go through the same owner (the lock is not reentrant; don't wrap public bus methods in it).
- `decoders/*`: pure functions, binary INFO bytes in, JSON-ready dicts out (`telemetry` 0x42, `alarms` 0x44 incl. the `BIT_FIELDS` table, `intrapack` 0x5A, `identity` 0x51/0xA2, `parameters` 0x47, `system` Modbus 0x1000).
- `pack.py`: `BatteryPack` (one per DIP address 0..n-1) schedules the requests of one poll and caches static data. `read()` returns `(data_or_None_if_failed, poll_success)`; change suppression happens in the publisher. Device info is fetched once; the firmware reports only major.minor, so the **patch level is derived by probing features** (`_derive_patch_pack_bus` / `_derive_patch_can_port`). On the CAN port bus only the master can be classified.
- **A pause between consecutive BMS requests is mandatory**; `SerialBus` enforces it (`gap`, `set_gap`). Without it the responses get corrupted; the packs end a frame after about 100 ms of silence, so 200 ms is the floor.
- `energy.py` keeps the 0x42 energy counters continuous across the 6500 kWh fallback, persisted via `state.py` (`/data/seplos_state.json`).
- `feed.py`: the OpenBMS Monitor feed (`docs/exchange/openbms-feed-v1.md`): raw answers with read times
  (`BatteryPack.raw`/`raw_ids`/`raw47`, `SystemReader.raw`), the retained meta and the round order that keeps a
  parallel set's packs back to back. `BatteryPack.read()` = `read_core()` (0x42/0x44) + `read_extras()`; a failed
  0x44 keeps the 0x42 (no complete success, status unknown). While the feed is on: round pause 0, bus quiet 200 ms.
- `app.py`: composition and main loop. Polls the packs round-robin, publishes data/heartbeat/availability, after each full cycle the system data, then sleeps `MQTT_UPDATE_INTERVAL`. SIGTERM raises `SystemExit`; cleanup runs once in `main()`'s `finally`. `health.py` serves `/health` on :8080 for the Supervisor watchdog.
- `mqtt_client.py`: `MqttConnection` supports the Paho 1.x and 2.x callback APIs (callbacks only set flags; a reconnect or the HA birth message `<prefix>/status` = `online` requests a resync in the main loop). `Publisher.state()` suppresses a payload only if the same payload was already accepted by the client, and `replay()` resends all latest states (5 s after discovery changes, then every 10 min). Availability changes count as sent only after the client accepted them.
- `maint.py` (option `maint_firmware`): the EMU1101-EXT patched firmware (Light 1.5.x, Full 1.6.x; identity 64/03 `EXT` format 05, 21 bytes; display names like "EMU1101-EXT Full 1.6.5"). Supported: Light from 1.5.4, Full from 1.6.4; any other identity (older versions, other formats/signatures) is "unsupported" and gets only the OEM reads. Authority for flags/capabilities: KB `mod/docs/firmware/uart-write-capabilities.md` and `openbms-monitor.md`. TEMP: the one first MAINT 1.6 build (`MNT` format 3, 18 bytes, build `18daf61d8a93671d`) stays accepted, with its inverse BitGroup0 bits 3/4 (no flag 0x08) and the lock from BitGroup0; every piece is marked `TEMP(maint-1.6)` so it can be removed in one sweep. Persisted keys (`maint_firmware`, state file keys), the `maint` JSON group/feed field, topics and unique IDs keep the old name. `MaintSupport.detect()` identifies a board via ASCII 64/03 only; every read is gated by that board's image flags/capability mask, never fails the regular poll and never writes. `framing/binary.py` handles the UART1 binary frames (`55 … AA`, CRC-16/XMODEM, read by LEN). Discovery entities carry `when` conditions (`pack_bus`, `maint`, `maint_storage`, `maint_uart1`); `Discovery.set_capabilities()` republishes a pack when its capabilities are known (EMU1101-EXT present or confirmed absent); while detection is pending, EMU1101-EXT-dependent configs are neither created nor deleted. Once EMU1101-EXT supplied the energy totals (`pack<n>.energy_source` in the state file), they become unknown instead of falling back to 0x42 until the identity is known again.
- The accepted EMU1101-EXT builds and the capability matrix stay in the code (no remote list). **Before working on anything that touches EMU1101-EXT** (identity decoding, flag/capability gating, discovery conditions, wording), fetch the current state from the public firmware repo `privatecoder/seplos-emu1101-docs` (branch `main`), e.g. `gh api repos/privatecoder/seplos-emu1101-docs/contents/<path> -H 'Accept: application/vnd.github.raw'`: the matrix `mod/docs/firmware/uart-write-capabilities.md` and the builds `mod/firmware/releases/ext-light.json`, `ext-full.json`, `references/ext-*.json` (`firmware_name`, `build_id`, `image_flags`, `remote_capabilities`, `info_hex`). Compare them with `decoders/maint.py` / `maint.py` and with the OpenBMS Monitor (`crates/openbms-flash/src/known.rs`). On any difference (new or withdrawn build, other flag or capability, a read moved between buses), **report it to the user and ask how to proceed**; do not adopt it silently. If the repo cannot be reached, say so.
- Parallel sets (`monitoring/model.py` `ParallelSet`, `sets.py`, `/api/sets`): saved sets reach the main loop via `AppState.request_sets` (no restart) and are adopted at the cycle boundary; `sets.py` computes the aggregates (all unknown when a member is missing). The published set ids live in `seplos_state.json` (`published_sets`) and leave it only after their HA cleanup was accepted.
- Cell types (`cells/`): `data/` is a vendored, byte-identical copy of the OpenBMS Monitor cell database + schema (`MANIFEST.json` digests, a test checks them; update only with `scripts/update_cell_catalog.py <openbms-monitor>`). `validate.py` has a structural layer equivalent to the schema (differentially tested against `jsonschema`, dev only) and a separate semantic layer. Pack assignments store a server-made snapshot of the entry; only an explicit accept-update replaces it. `checks.py` (pure, vectors in `tests/vectors/cell_checks.json` cross-checked against OpenBMS, deviations marked) and `evaluate.py` produce the `cells` group; `balancer.py` is the computed external-balancer state machine (unknown start/reset).
- `topics.py`: MQTT state topics and HA identifiers. These are public interfaces (automations, dashboards, retained discovery); keep them stable.

### Discovery (`src/seplos_mqtt/discovery/`)

- `catalog.py` is data: `Entity` entries keyed by the JSON key the decoder writes (`value_json.<group>.<normal|binary>.<key>`). Binary payloads of telesignalization entities come from the decoder's `alarms.BIT_FIELDS`, so they can't drift apart.
- `Discovery` renders and publishes retained configs to `<prefix>/<sensor|binary_sensor>/seplos-mqtt-pack-<n>/<key>/config` (system: `seplos-mqtt-system`) with short-form keys. `default_entity_id` keeps entity IDs stable (`seplos_bms_pack_<n>_<key>`). The full device block travels with the first config per device; `update_firmware()` republishes one config once the real firmware is known.
- **When renaming or dropping an entity key, add the old `(domain, key)` to `OBSOLETE`** so the retained discovery config gets cleared for existing users. Entities whose `when` conditions don't hold for a pack, and the system device in single pack mode, are cleared automatically.

### Docs and the Lovelace card

The README has a sensor reference table mapping each HA name ↔ MQTT JSON path ↔ Seplos protocol doc field. Keep it in sync when adding, renaming or reinterpreting values. `ha-lovelace/lovalace.yaml` is a sample dashboard (4 packs, built-in HA cards only, `SET_*` placeholders) that references entity IDs, so key renames affect it too. It is generated, not hand-edited: `tools/dashboard/build_dashboard.py` (local helper in the git-ignored `tools/`, see `tools/README.md`) writes it together with a personal 12-pack variant and validates every entity against the discovery catalog. Besides entity IDs the dashboard depends on:

- attributes: `limits` on `cell_type` (datasheet min/max) and the flat BMS settings dict on `parameters` (e.g. `*_temperature_alarm_c` / `*_protection_c`, `remaining_capacity_*_percent`, `balancing_start_difference_v`, `cell_difference_fault_v`, `pack_*_voltage_*_v`); renaming those keys silently drops the threshold colouring (it falls back to fixed bands);
- state values: `bms_alarm` ok/alarm/protection/fault, check results ok/near/over/not_evaluated, balancer active/inactive.

Colour semantics (keep consistent): ok green, near/alarm amber, protection orange, over/fault red, unknown grey; temperatures blue <0 °C, cyan <10, green, then amber/red at the BMS alarm/protection settings (without settings: amber ≥35, orange ≥45 °C), red for cells only outside the datasheet ranges. Built-in tile `color` is static, so dynamic colour = several card variants with template `visibility`, exactly one visible per entity (unknown included).
