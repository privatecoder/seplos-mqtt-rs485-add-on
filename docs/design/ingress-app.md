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

Live data: the **OpenBMS Monitor feed** (`docs/exchange/openbms-feed-v1.md`, `feed.py`). A switch in the Overview
(`PUT /api/feed`, kept in the state file, adopted at a cycle boundary) publishes every raw pack and system answer with
its read time; while on, the round pause is 0, the bus quiet 200 ms and parallel sets are read back to back.

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

## Stage 3 in detail (cell types, checks, computed balancer)

One release, sub-stages each reviewed: **3a** catalog + model + API, **3b** checks + energy + HA, **3c** computed
balancer, **3d** UI. Research: `.orch/codex-stage3-cells-proposal.md` (local).

### 3a: catalog, model, API

- **Bundled catalog**: a vendored copy of OpenBMS Monitor's `data/cells/cells.json` + `data/schema/cells.schema.json`
  under `src/seplos_mqtt/cells/data/`, unchanged, plus `MANIFEST.json` (source repo, commit, sha256 of both files)
  and the MIT notice. `scripts/update_cell_catalog.py <path-to-openbms-monitor>` copies both files and rewrites the
  manifest (maintainer tool, no network). A test checks the manifest digests. No download at build or run time. A
  separate upstream repo may replace the source later; only the manifest's source changes then.
- **Validation** in Python without new runtime dependencies, two layers (like OpenBMS `validate.ts`):
  1. *structural*, equivalent to the vendored schema (required keys, types — booleans are not numbers —, enums,
     `additionalProperties: false` everywhere, no `null`, `minimum`/`exclusiveMinimum`, patterns, `date`/`uri`
     formats, the derating point `oneOf`); a dev-only test runs the bundled file plus mutated entries through
     `jsonschema` (format checking on) and requires the same accept/reject result;
  2. *semantic*, tested on its own: finite numbers, positive nominal V/Ah/Wh, charge cutoff > discharge cutoff,
     min < max temperatures, band bounds ordered, SOC bands 0..100, `interpolation: linear` only with a provenance
     entry for it; may reject structurally valid entries.
  Entries are strict: an unknown key inside an entry is an error (save and import); schema fields the form does not
  show are kept on edit. Errors carry a JSON path. Unknown keys outside entries (pack/profile objects, top level)
  survive load/save like `connection`.
- **Model** (`/data/monitoring.json`, optional keys, defaults empty):
  - `cell_types: [entry]`: own types, full schema entries. A new own id must not collide with a bundled id ("copy
    as template" proposes `<id>-custom`, `-custom-2`, …). **Upgrade collision** (a later bundle adds an id that is
    already an own type): loading never fails for it; the own type stays usable under its id, is listed as
    `conflict: bundled_id` with a "rename" action, and the bundled entry with that id is hidden until resolved.
    Rename is one server operation (`POST /api/cells/rename {from, to}`, one store write): it changes the own entry's
    id, every pack reference to it (also addresses ≥ `pack_count`) and the `id` inside those snapshots; all other
    snapshot content (version, limits) stays. Tested with catalog A → catalog B.
  - `packs: {"<address>": {cell: {id, entry_version, parallel, entry} | null, balancer_profile: id | null,
    bms_serial: str | null, serial_mismatch: bool}}`. `entry` is the server-made snapshot of the assigned entry (a
    catalog update never changes an assignment's limits); on load, `entry.id`/`entry_version` must equal the
    reference, else that assignment is treated as invalid (checks not evaluated, shown for re-assignment).
  - **Identity guard**: `bms_serial` = BMS serial (0xA4 string 2, trimmed; never the 0xA2 battery serial) read
    when the assignment was saved or confirmed. Readable only on RS485-1/2 (single pack) or with EMU1101-EXT UART1; else
    null and the pack is shown "identity not verifiable" (checks still run). When a readable serial differs from the
    stored one, `serial_mismatch` is set: checks of that pack are not evaluated, its balancer state goes unknown,
    and the user confirms (`POST /api/packs/confirm-identity`, stores the new serial) or reassigns.
  - `balancer_profiles: [{id, name, values: {start_mv, stop_cell_mv, trigger_mv, stop_mv, when, max_a}}]` with the
    value rules of `docs/exchange/seplos-bank-settings-v1.md` (`balancers[]`); ids server-assigned (`next_profile_id`,
    never reused), name 1..40 unique. Deleting a profile that any stored pack references (including addresses ≥
    `pack_count`) is refused (`in_use`, addresses); an unknown profile id in a pack is `invalid_fields`.
  - Removed packs (address ≥ `pack_count`) keep their stored assignment.
- `parallel` is an integer ≥ 1 as in the exchange contract; the add-on additionally caps it at 10 000, only as a guard
  against absurd input. Numbers in a cell entry must be finite and at most 1e9 in magnitude (semantic `range`).
- **Update offer**: when the bundled entry with an assigned id has a higher `entry_version` than the snapshot,
  `GET /api/packs` marks the pack `update_available` with the changed fields; only an explicit
  `POST /api/packs/accept-update` (addresses) replaces the snapshot.
- **Own types**: editing increments `entry_version` and sets `updated`; assigned snapshots stay until the user
  accepts the new version (same offer as above). Deleting a type that is assigned is refused (`in_use`, addresses).
- **API** (CSRF + If-Match + revision like `/api/sets`): `GET /api/cells` (bundled + own, `source: bundled|own`,
  manifest version), `PUT /api/cells` (own list), `GET /api/packs` (stored assignments with their snapshots, update
  offers, identity state, series count read-only from the last 0x42), `PUT /api/packs` with **references only**:
  `{packs: {"<address>": {cell: {id, parallel} | null, balancer_profile: id | null}}}` — a listed address is
  replaced, an unlisted one stays unchanged, `null` clears; the server resolves `id` against own + bundled types
  and makes the snapshot only when the cell **id** changes (or the pack had none); the same id keeps the stored
  snapshot even when `parallel` or the profile changes — only accept-update replaces it (tested for bundled and
  own types); bulk = the client lists every address. `GET/PUT /api/balancer-profiles` (whole list, like sets). Saves apply
  without restart (handed to the main loop at the cycle boundary, like sets). Import/export of own types: the
  OpenBMS cell array as JSON (client side download/upload through `PUT /api/cells`). The full bank exchange stays
  stage 6.
- **Parallel suggestion**: when the type is chosen, the UI proposes `round(rated_capacity / nominal_ah)` if it is
  within 2 %; never applied automatically. Series count is never stored or entered (measured).

### 3b: checks, energy, Home Assistant

- Inputs per pack: the latest valid 0x42 sample (cell voltages, the four cell probes, current with charging
  positive before the HA inversion, SOC, rated capacity) with its receive time, independent of MQTT change
  suppression and of a failed 0x44; parameters from 0x47 (single pack) or EMU1101-EXT binary 47 with their read time.
- **Live checks** (OpenBMS rules and margins, shared test vectors):
  - cell voltage vs `charge_cutoff_v` / `discharge_cutoff_v`: over beyond, near within 50 mV / 200 mV (equality =
    near);
  - cell probes vs the charge or discharge temperature range for the current direction: over outside while
    current flows that way, near within 5 °C or "would not be allowed now" at rest;
  - direction thresholds: charging = current ≥ +0.2 A, discharging = current < −0.5 A, otherwise at rest;
  - permitted charge current: with a derating table, the table value at the measured probe temperatures (every
    probe evaluated, the lowest result wins; lower neighbour, worst SOC row when SOC unknown, zero outside the
    table, linear only when the entry says so), capped by `max_continuous_charge_a` when present; without a table
    (11 of 31 bundled entries), `max_continuous_charge_a` inside the charge temperature range and zero outside;
    missing basis → not evaluated; × `parallel`. Charge check: limit 0 → over at ≥ 0.2 A; limit > 0 → over above
    limit + 0.5 A, near above 80 % of the limit;
  - discharge current check (static, like OpenBMS): discharge component `max(0, −current)` vs
    `max_continuous_discharge_a` × `parallel`; over above the limit (no tolerance), near above 80 %; a temperature
    outside the discharge range is reported by the temperature check (flow thresholds above), not by this check;
  - permitted discharge budget (advisory, HA sensor only): `max_continuous_discharge_a` × `parallel` inside the
    discharge temperature range, zero outside, unknown without basis;
  - rates: C × `nominal_ah`, P × `nominal_energy_wh` (else Ah × V) / `nominal_v`; never the learned capacity.
  - Boundary values (equality, zero-rate bands, shared band bounds, +charging vs discharge check, −0.1 A and −0.5 A
    at rest/edge, real discharge) are in the shared test vectors.
- **Parameter checks** (only where parameters are readable): cell/pack voltage alarm and protection settings,
  charge/discharge temperature settings and current settings vs the datasheet; P65 ≠ measured series count is a
  configuration warning; rated capacity vs `nominal_ah` × `parallel` beyond 2 % is near (informational).
- Each check: `ok | near | over | not_evaluated` with reason, value, limit, unit. Summary per group (live,
  parameters) = worst result; any `not_evaluated` check makes an otherwise ok summary `not_evaluated`, `over`
  always wins. No assignment, missing fields, stale sample (pack availability limit) or stale parameters (two
  missed refreshes) → not_evaluated.
- **Full energy**: pack `full_energy` = learned full capacity (0x42) × measured series cells × nominal V of the
  assigned entry, else `connection.nominal_cell_voltage` (default 3.2 V); never × parallel. System
  `total_full_energy` = sum of all configured packs' `full_energy`, unknown if any is unknown (replaces master
  Ah × one series count × one voltage). Recomputed after an assignment change without restart.
- **HA** (existing pack device, no new devices, existing IDs unchanged), all diagnostic:
  - `cell_type` (text; attributes id, entry_version, manufacturer, model, chemistry, nominal_v, nominal_ah,
    parallel, series, update_available);
  - `datasheet_check_live`, `datasheet_check_parameters` (`device_class: enum`, options ok/near/over/
    not_evaluated; attribute `checks` with key, result, value, limit, unit, reason; no timestamps in attributes);
  - `datasheet_charge_current_limit`, `datasheet_discharge_current_limit` (A, measurement, disabled by default,
    advisory): the permitted currents above at the current temperatures (0 when not allowed, unknown without basis
    or while not evaluated);
  - published in the pack's state JSON under a new group `cells`; README sensor table updated.
- `/api/status` per pack: assignment summary, both summaries, the checks.

### 3c: computed balancer

- Per pack with a `balancer_profile`: state `active | inactive | unknown` from the latest fresh sample: spread
  (mV) = highest − lowest cell; start when spread ≥ `trigger_mv` and (no `start_mv` or highest cell ≥ `start_mv`)
  and the `when` condition holds (`charging`: current > +1 A, `not-discharging`: ≥ −1 A); stop when spread <
  `stop_mv` (null or ≥ trigger → trigger), or highest cell < `stop_cell_mv` (rules of the exchange format), or the
  condition fails; otherwise keep the state. Starts `unknown`, back to `unknown` after a restart, a stale sample,
  an identity mismatch, assigning/unassigning/reassigning a profile, or any edit of the assigned profile's values
  (same id). No cell catalog data involved.
- HA: `external_balancer` (enum active/inactive/unknown, diagnostic) on the pack, only for packs with a profile;
  unassigning clears its discovery config (retried until the broker accepted it, like removed sets).

### 3d: UI

- Tab "Cells": list (search manufacturer/model/Ah, bundled/own badge), detail (limits, derating table, source
  link), own types: create, copy as template, edit (common fields in the form, everything else preserved),
  delete, import/export JSON.
- Tab "Packs": per pack address, BMS serial, measured series, rated capacity, cell type + parallel, balancer
  profile, check summaries, update offer with diff; "apply to all packs" with the list of affected packs.
- Balancer profiles editor (in the Packs tab): name and the values with their rules.
- Overview pack card: cell type and the live check summary; balancer state when a profile is assigned.
