# Ingress app: setup, parallel sets, cell types, external balancers

Status: agreed design (2026-11-04), merged from two independent drafts (leader and reviewer), with the user's
decisions. Implementation in stages, each reviewed.

## Decisions

- **Scope:** packs connected in parallel only (n packs on one common feeder to the bank busbars). No towers, groups
  or racks. A pack outside a set stays standalone.
- **Stack:** aiohttp from Alpine (3.11 on Alpine 3.22, 3.13 on 3.24; code must work with 3.11), Python 3.12,
  dataclasses + JSON Schema (py3-jsonschema) for validation, vanilla ES modules with DE/EN texts. All five
  architectures stay. Security and storage contracts as in the Deye app: Supervisor peer check (172.30.32.2) before
  any identity header, CSRF for mutations, `If-Match` revisions for persistent edits, persisted-before-visible
  atomic JSON store, relative URLs, `ingress_port: 0` with host networking.
- **Connection ownership:** the app. The existing add-on options are imported once (migration), then edited in the
  app; the add-on options keep MQTT and log level only. Python starts and supervises the socat bridge, so changed
  gateway settings apply without a restart. Polling restarts in-process after a saved change.
- **External balancer observations:** first an MQTT input bridge (no extra permission), later optionally an entity
  picker through `homeassistant_api` (read-only use of the Core websocket/REST).
- **Read-only toward the BMS.** The app runs only known read requests through the existing bus owner via a typed job
  queue; no raw frames, broadcasts, maintenance or writes.

## Model (`/data/monitoring.json`, schema_version + monotonically increasing revision)

- `connection`: transport (tcp | serial), host/port or device, baud (auto | 9600 | 19200), pack_count, maint_firmware,
  cell voltage display range, update interval. Imported once from the add-on options.
- `packs[address]`: expected BMS serial (string 2, 0xA4) for identity checks, cell assignment
  `{cell_id, entry_version, parallel}` (internal parallel cells, independent of set membership). Packs are keyed by
  DIP address; a changed BMS serial marks the assumptions unconfirmed until the user confirms.
- `parallel_sets[]`: `{id (immutable), name, packs (2..n, physical order), wiring: diagonal | common_busbar | unknown,
  plus, minus (diagonal: both required)}`. A pack in at most one set; no nesting; no auto-creation (a "neighbour
  pairs" quick fill only proposes).
- `balancer_profiles[]`: `{id, name, kind: active-balancer, values{start_mv, stop_cell_mv, trigger_mv, stop_mv, when,
  max_a}}`; `balancers[]` (physical units): `{id, profile, pack, observation binding (later)}`. Twelve equal units
  share one profile but are twelve units.
- Cell catalog: bundled, versioned snapshot of the cell database (+ schema); user types validated against the same
  schema; bundled updates never silently change an existing assignment (offer the new entry).
- Separate stores stay untouched: `/data/seplos_state.json` (energy continuity), discovery inventory.

## Samples and alignment

- An immutable `PackSample` per validated 0x42 (receive time), 0x44 status with its own time; analysis consumes every
  valid sample independent of MQTT de-duplication; bounded in-memory window (no long-term recording).
- Set comparisons use cohorts of samples at most 3 s apart, at least 3 cohorts; otherwise "not comparable", never
  "healthy". Later the poll order visits a set's members back-to-back, optional/static reads after the core pass.

## Home Assistant

- One new diagnostic device per parallel set (stable id from the set id). Packs are NOT re-parented (`via_device`
  is communication routing); membership is an attribute. Existing pack/system topics, device and entity IDs unchanged.
- Per set: net current, power at pack ports, capacity-weighted SOC (only with complete valid capacities), port-voltage
  median and range, highest/lowest cell with pack, temperature extrema, coverage/sample age, sharing state, weak member,
  opposing flow, port gap (diagonal pairs). No summed energy counter. Missing members make affected values unknown.
- Per pack (additive): cell type, datasheet checks (ok | near | over | not evaluated, with reason/limit), external
  balancer estimated / observed / mismatch.

## Rules ported from OpenBMS Monitor (with shared test vectors)

- Pairs: load guard 20 A median per pack, opposing currents > 1 A, weak share enter 0.70 / clear 0.75, low total
  0.85 / 0.90, SOC equalizing explanation (3 points), port gap enter 0.10 V / clear 0.08 V, gap trend. n > 2: shares
  against explicit weights, own thresholds, labelled separately.
- Cells: C/P-rate conversion, internal parallel multiplier, conservative derating, near-limit margins (50 mV, 200 mV,
  5 °C, 80 %), parameter vs datasheet comparison where parameters are readable.
- Balancer activity: OpenBMS rule with the hysteresis rules of the exchange format; deliberate difference: after a
  restart the state is unknown until a decisive sample (OpenBMS starts inactive).

## Exchange with OpenBMS Monitor

`docs/exchange/seplos-bank-settings-v1.md` (both directions). Mapping: sets with exactly 2 packs and both taps ↔
OpenBMS pairs; balancer profiles ↔ OpenBMS device definitions (current revision); per-pack balancer = the profile of
the pack's unit. The add-on's own backup export additionally carries every referenced cell entry snapshot.

## Stages

1. Ingress foundation: security, store + option migration, socat/connection ownership, setup with read-only test,
   status per pack, optional pack detection on request.
2. Parallel sets: editor, invariants, HA devices and values, reconciliation.
3. Cell catalog + checks; computed balancer. Plus calculated full energy (kWh): per pack =
   learned full capacity of the pack (0x42) × measured series cell count × nominal cell voltage, where the nominal
   voltage comes from the pack's assigned cell type, else from a manual setting `nominal_cell_voltage` (V, like the
   min/max cell voltage settings, default 3.2). "Total Full Energy" on the system device = sum of the packs; unknown
   while any pack's value is unknown. Works with different cell types per pack.
4. Poll order by sets, aligned cohorts, sharing/gap verdicts.
5. Observed balancer values: MQTT bridge, then optional HA entity picker.
6. Import/export with OpenBMS (can move earlier once 2 and 3 exist).

## Stage 2 in detail (parallel sets)

Sub-stages, each reviewed: **2a** model + API + live apply, **2b** values + HA devices + status, **2c** UI.

### 2a: model and API

- `parallel_sets` (top level of `/data/monitoring.json`, optional, default `[]`) and `next_set_id` (int ≥ 1,
  default 1). A set: `{id: int ≥ 1, name: str 1..40 (trimmed, unique case-insensitively), packs: [int] (2..16
  distinct DIP addresses 0..15, physical order), wiring: "diagonal" | "common_busbar" | "unknown", plus: int|null,
  minus: int|null}`. `plus`/`minus` must be members; `diagonal` requires both and `plus != minus`. A pack is in at
  most one set. Unknown keys of a set survive load/save (like `connection`).
- Ids are assigned by the server (`next_set_id`, then incremented) and never reused, so HA entity IDs of a deleted
  set never come back for a different set.
- A set whose packs are not all below `connection.pack_count` stays stored but is **inactive** (reason
  `pack_not_configured`); saving the connection with a lower pack count is allowed. Sets need multi pack mode
  (`pack_count > 1`); in single pack mode all sets are inactive (reason `single_pack`).
- `GET /api/sets` → `{revision, pack_count, sets: [... + {active, reason}]}`. `PUT /api/sets` (CSRF + If-Match) with
  `{sets: [...]}`: the whole list; entries without `id` are new; an `id` that is not stored → `invalid_fields`
  (`value`); `active`/`reason` in the body are ignored. 422 `invalid_fields` with paths like `sets[1].plus`.
- A saved set change applies **without a restart**: the web thread hands the validated set list to the main loop
  (callback like `on_connection_saved`), which picks it up at the next cycle boundary.

### 2b: values and Home Assistant

- After each full poll cycle (next to the system data), per **active** set from the members' latest valid 0x42
  data: `current` (sum), `power` (sum of pack voltage × current, W), `soc` (capacity-weighted with the learned full
  capacity; unknown unless every member has SOC and a capacity > 0), `voltage_median`, `voltage_range` (max − min
  pack voltage), `highest_cell_voltage` + `_pack`, `lowest_cell_voltage` + `_pack`, `highest_cell_temperature` +
  `_pack`, `lowest_cell_temperature` + `_pack`, `members_reporting` (count), `sample_age` (s, oldest member
  sample). A member counts as reporting when it is online and its data is not older than the pack availability
  limit; if any member is missing, every aggregate is unknown (null), only `members_reporting`/`sample_age` stay.
- MQTT: `<base>/set-<id>/sensors` (JSON `{"normal": {...}, "set": {name, packs, wiring, plus, minus}}`),
  `<base>/set-<id>/availability` (online while the set is active). HA device `Seplos Parallel Set <name>`, ids
  `seplos_bms_set_<id>`, node `seplos-mqtt-set-<id>`, entity IDs `seplos_bms_set_<id>_<key>`, `via_device` the
  master; packs are not re-parented; membership as JSON attributes. Current/power follow the `invert` option.
- Deleted or inactive sets: retained discovery configs and availability are cleared. The published set ids are kept
  in `/data/seplos_state.json`, so a set deleted while the add-on was stopped is cleaned up at the next start.
- `/api/status` gains `sets: [{id, name, active, reason, values...}]`.

### 2c: UI

- New tab "Sets": list of sets (name, members in order, wiring, taps, active/inactive with reason), editor
  (name, pack chips in physical order with move up/down, wiring, plus/minus selects limited to members), delete.
  "Propose neighbour pairs" fills a draft with (0,1), (2,3), … for packs not yet in a set; nothing is saved
  until the user saves. Same revision/CSRF/read-only rules as the settings tab.
- Overview: one card per set with the 2b values; inactive sets show the reason.
