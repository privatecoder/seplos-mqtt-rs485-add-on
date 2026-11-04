# Exchange format "seplos-bank-settings" v1 (draft r4)

Purpose: move the bank configuration between the Seplos HA add-on and OpenBMS Monitor in both directions:
parallel sets, cell type per pack, external active balancers and their assignment. Configuration only: no
measurements, no recording, no parameters, no firmware logs. One UTF-8 JSON file, `*.seplos-bank.json`.
All examples are invented; never put data of a real bank into this document.

```json
{
  "format": "seplos-bank-settings",
  "version": 1,
  "created": "2026-01-01T12:00:00+01:00",
  "producer": { "app": "openbms-monitor", "version": "0.0.0" },
  "bank": { "name": "Beispiel", "pack_count": 2 },
  "packs": [
    { "address": 0, "bms_serial": "SEPLOS-0000000001",
      "cell_type": { "id": "example-cell-a", "entry_version": 1, "series": 16, "parallel": 1 }, "balancer": "bal-1" },
    { "address": 1, "bms_serial": null, "cell_type": null, "balancer": null }
  ],
  "parallel_sets": [
    { "id": "set-00-01", "name": "", "packs": [0, 1], "plus": 0, "minus": 1 }
  ],
  "balancers": [
    { "id": "bal-1", "name": "Balancer A", "kind": "active-balancer",
      "values": { "start_mv": 3400, "stop_cell_mv": 3370, "trigger_mv": 5, "stop_mv": null, "when": "always", "max_a": 4 } }
  ],
  "cell_types": [
    { "id": "example-cell-a", "entry_version": 1, "entry": { "...": "full cell database entry" } }
  ]
}
```

## Sections
The file has three importable sections. Whether a section is present decides whether it is offered:

| Section | Carried by | Absent | Present but empty |
|---|---|---|---|
| Parallel sets | `parallel_sets` | not offered, target unchanged | `[]`: all sets removed |
| Cell types | `packs[].cell_type` (+ `cell_types[]`) | `packs` absent or no pack has the key: not offered | every listed pack `null`: cleared |
| Balancers | `balancers` + `packs[].balancer` | `balancers` absent: not offered | `[]`: all balancers ended, all packs "no balancer" |

A section carried by `packs[]` is offered when at least one pack has its key; within an offered section a missing key
on a pack means the same as `null`. A chosen section replaces the target's section completely: a pack listed with
`null` (or without the key) and a pack of the target not listed in `packs[]` both get "none". Exception: a pack whose
value is not importable (unknown cell type, a declined serial confirmation, see Import 3) keeps its previous value,
and the preview says so.

Parallel sets with a declined pack: a set of the file that contains a declined pack is not importable; a set of the
target that contains a declined pack stays as it is; a set of the file that would then put a pack into two sets is not
importable either. The preview lists each of these cases with its reason.

## Field rules
- `format` and `version` are checked first; another `format` or a `version` > 1 is refused. Unknown KEYS anywhere
  are ignored (forward compatibility). Unknown VALUES of defined keys are errors (see Validation).
- `bank.pack_count`: informative. `bank.name`: informative, never overwrites the target's name.
- `packs[].address`: DIP address 0–15 (pack 00 = master). `bms_serial`: string 2 (CID2 0xA4), trimmed, or null.
  The battery serial (0xA2) is not used: it is often a placeholder shared by all packs.
- `packs[].cell_type`: `id` + `entry_version` of the cell database entry (same ids in both apps), `parallel` (cells in
  parallel per series position, 1 = 1P), `series`: informative only, may be null, never imported (the receiver uses
  the measured cell count); the exporter fills it from the measured cell count when known.
- `cell_types[]`: the full entry of every referenced type that is either not in the bundled database OR differs from
  the bundled entry with the same id (a user override). The receiver feeds these entries into its own user-type logic
  (create / replace / "bundled entry changed"). Referenced id with an `entry_version` different from the receiver's and
  no entry carried: the assignment is taken, the preview notes the version difference. Referenced id unknown to the
  receiver and not carried: that pack's assignment is not importable (listed in the preview; the pack keeps its
  previous value). A carried `entry` must satisfy the cell database schema; a violation is a validation error with the
  JSON path inside the entry.
- `packs[].balancer`: id of an entry in `balancers[]` or null.
- `parallel_sets[]`: packs connected in parallel on one busbar connection. `packs`: ≥ 2 distinct addresses; a pack in
  at most one set. `plus`/`minus`: the pack carrying the main + / − cable (diagonal wiring), each one of `packs`
  or null (unknown). `id`: stable string, unique in the file; `name` may be empty. A receiver that cannot represent a
  set (OpenBMS: only pairs of exactly 2 packs with both `plus` and `minus` set) lists it as "not importable" with the
  reason. A receiver that keeps no set ids/names generates `set-<aa>-<bb>` (addresses, two digits, ascending) and an
  empty name on export.
- `balancers[]`: `id` stable and unique in the file, `kind` = `active-balancer`. `values` (mV, A): `start_mv` highest
  cell from which it works or null; `stop_cell_mv` highest cell below which it stops or null; `trigger_mv` spread from
  which it starts (required, 1–5000); `stop_mv` spread below which it stops or null (= trigger); `when` = `always` |
  `charging` | `not-discharging`; `max_a` informative or null. Only the current values travel.
  Hysteresis rules (both apps compute identically): `stop_mv` null or ≥ `trigger_mv` means no spread hysteresis
  (stop = trigger); `stop_cell_mv` is ignored when `start_mv` is null, and `stop_cell_mv` null or ≥ `start_mv` means no
  cell-voltage hysteresis (stop cell = start).
- Ids (`parallel_sets[].id`, `balancers[].id`, `cell_types[].id`, `packs[].balancer`): printable ASCII only
  (0x21–0x7E), 1–64 characters; anything else is a validation error. This keeps the export order identical across
  languages.
- Not part of the format: towers/groups, wiring/inverter port, notifications, diagnosis thresholds, connection
  endpoints, recordings.

## Validation (errors; nothing is written, all errors listed with their JSON path)
Wrong `format`; `version` not an integer ≥ 1 or > 1; `address` outside 0–15 or duplicated in `packs[]`; duplicate ids
in `parallel_sets[]`, `balancers[]` or `cell_types[]`; a pack in two sets; a set with < 2 packs, duplicate packs, or
`plus`/`minus` not in its `packs`; `plus` = `minus`; unknown `kind` or `when`; `trigger_mv` missing or outside 1–5000;
an id that is not printable ASCII or longer than 64 characters; other mV values outside 1–5000; negative `max_a`; `parallel` not an integer ≥ 1; `packs[].balancer` referencing an id
not in `balancers[]` (when `balancers` is present); a `cell_types[].entry` violating the cell database schema; wrong
JSON types. New values of `kind` or `when` (or any other enumerated value) require `version: 2`; a v1 reader refuses
the whole file otherwise.

## Import behaviour (both apps)
1. Parse and validate (above); on any error nothing is written.
2. Preview per offered section: what will be set, what is not importable (with reason), notes (cell type version
   differences; in OpenBMS: custom cell types are global, not per installation).
3. Bank match per address via `bms_serial` against the target's known BMS serials: a differing serial needs an
   explicit confirmation per affected pack; if it is declined, that pack keeps its previous values in every section.
   An unknown serial, a differing `pack_count`, or addresses the target has not seen are notes only; the assignments
   are taken anyway.
4. The user picks sections. Apply all chosen sections all-or-nothing (one transaction / one store write). Custom cell
   types: NEW types (id unknown to the receiver) may be added before the transaction, because that only adds.
   Replacing an existing custom or overridden type happens only if the user explicitly chose it in the preview, and
   only after the sections were written successfully (in OpenBMS such a type is global, not per installation).
5. Balancers keep the ids of the file: an id the target already has gets a new revision with the file's values
   (time = import time); an id of a balancer the target has deleted (ended with `to`) is revived: `to` is cleared and
   it gets a new revision at import time; a target balancer not in the file is ended at import time, unless a pack that
   keeps its previous values (declined serial) is still assigned to it; assignments get a new revision at import time
   ("no balancer" for packs without one). Earlier history stays computable.
6. Export writes exactly this format, in a fixed order: `packs` by address, `parallel_sets`, `balancers` and
   `cell_types` by `id` (plain string order); a set's `packs` ascending.

## Round trip
- Same app: export → import → export gives identical content except `created`, `producer` and `series` (which
  depends on whether the cell count was measured at export time).
- Across apps: identical in meaning: same sets (as sets of addresses with the same plus/minus), same cell type
  assignments (id, entry_version, parallel), same balancers (ids, values) and assignments. Set ids/names and `series`
  may differ when one side doesn't store them.
