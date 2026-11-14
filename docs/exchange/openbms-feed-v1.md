# OpenBMS feed v1

Status: v1. Producer: this add-on; consumer: OpenBMS Monitor ("Home Assistant (MQTT)" connection).

## Purpose

When the Home Assistant add-on "Seplos BMS" polls the bank, OpenBMS Monitor can't poll the same RS485 bus at the same time. The feed lets the Monitor read the add-on's measurements over MQTT and run its analysis on them: overview, pair check, balancing line, history, health report and events. The add-on stays the only poller. The feed is meant for long-term use as well.

## Non-goals

- No writes over MQTT. Writing parameters, calibration, restart, BMS history, firmware and diagnostics that ask the BMS itself still need a direct Monitor session. For that session the add-on is stopped.
- No archive in the add-on. The Monitor records only while it receives.
- No change to the HA entities or the discovery output.
- No cell catalogue or external balancer settings over MQTT. Those stay Monitor configuration and travel via `seplos-bank-settings` v1.

## Add-on: the switch

- **"OpenBMS Monitor feed"** (DE „OpenBMS Monitor-Datenstrom“): a persistent on/off switch in the Ingress app, stored in `monitoring.json`.
  - The main loop picks up a change at the next round boundary, without a restart.
  - After a restart the stored choice applies again, with a new `boot` id.
  - Stored connection values are never overwritten.
- **Optional auto-off** after a chosen time (1 h, 4 h, 24 h); off by default. The end time is stored as well.
- **While on:**
  - **Effective round pause 0.** Off restores the stored `update_interval`.
  - **Bus quiet 200 ms** on both buses, since the last received byte, instead of the stored bus quiet (see "Bus quiet setting"). This applies to every request: ASCII, Modbus, binary EMU1101-EXT services and retries.
    - The 200 ms include the processing time after an exchange; do not add a separate 100 ms gap. No pause inside a frame, no parallel requests.
    - Basis: UART1 ends a frame only after 100 character times (≈ 104 ms at 9600 Bd); the Monitor runs on 200 ms (`openbms-transport/src/timing.rs`).
    - The add-on's 1 s came from centralizing earlier scattered `sleep(1)` calls (`17233db`); it was never shown to be a minimum.
    - 200 ms is a well-founded practical value, not a proven minimum for every image or gateway. 0 is not suitable.
  - **Read order:** within each parallel set, first the core reads (0x42, 0x44) of all members back to back; optional reads that are due come after them, never between partners. In a set of more than two packs, the two diagonal end packs (`plus`, `minus`) come first.
  - **No additional periodic reads;** intervals stay as they are (see the inventory).
- **Hint text while on:** "Feed active. The round pause is 0 for now; the bus quiet is 200 ms. Your stored settings are kept. Current round: … s. The add-on keeps no archive." Add "(auto-off at HH:MM)" when that is set.

## Inventory

Feed on speeds up the shared poll and publishes raw data. It justifies **no extra periodic BMS reads**; every HA value stays. Times are pure line/pause arithmetic for a fictitious 12-pack bank (16 cells, 9600 Bd 8N1, first answers valid, no gateway/Python latency). Each time is the cost when that read is due for all twelve packs in that round, unless it says "once".

| Read | Feed off | Feed on | Time off / on (UART1) |
|---|---|---|---:|
| 0x42 | every pack, every round | same; raw bytes + read time | 14.35 / 4.75 s |
| 0x44 | every pack, every round | same | 13.70 / 4.10 s |
| 0x51 | once, limited retries | use cache, don't re-read at feed start | +13.25 / 3.65 s |
| OEM patch probes | initial | don't repeat | variable |
| EXT 64/03 | initial, ≤ 3 tries | same; pass on known identity | +13.00 / 3.40 s |
| EXT A2/A4 | once (UART1) / with device info (UART0) | same, cached | +25.10 / 5.90 s |
| EXT 64/06 | 60 s (energy/SOC capability) | same (HA energy counters) | +14.18 / 4.58 s |
| EXT 64/04 | 600 s (Light) / 60 s with pack-lock flag (Full) | same | +12.73 / 3.13 s |
| EXT 64/02 | 60 s, UART1 address 0 only | same | once +1.05 / 0.25 s |
| EXT 64/05/00 | 600 s with calibration capability | same, after the core reads | +14.30 / 4.70 s |
| Clock 0x4D | 600 s where supported | same (HA clock offset) | +12.63 / 3.03 s |
| Parameters 0x47 / EXT 47 | initial + hourly (old pack-lock fallback 300 s) | publish the cached raw bytes with their original read time; read only when due or no raw cache | binary +14.28 / 4.68 s |
| 0x5A extended | UART0 single pack every round | same | n/a on UART1 |
| FC03 0x1000 ×21 | once per round (multi-pack) | same; raw registers | once 1.06 / 0.26 s |
| FC03 0x0074 ×1 | with each system read while supported | same; raw register | once 1.02 / 0.22 s |

The base round is **30.1 s plus the round pause** when off, and **9.3 s** when on. When every periodic extra is due at once on a fully supported 1.6 bank, the round is 99.3 / 29.7 s.

These are lower bounds, not a promise: neither a fixed round time nor three pair matches per 60 s is guaranteed. Spread due reads over rounds where possible, and show the current round time.

**Applies always (bug fix, feed on or off):** a failed 0x44 no longer drops a valid 0x42. The HA alarm state stays unknown then, and the poll does not count as a complete success.

**Long-term load:** with the feed on, HA gets new values about three times as often. Change suppression stays for the HA topics; the HA recorder grows accordingly, which the user can limit with recorder excludes. The Monitor controls its own row rate (recording interval, minute aggregation).

## Topics and payload

Root `<mqtt_topic>/openbms/v1/`, QoS 0. A pack message is published after the extras of its group, so a parameter block read in that poll is part of it; the core read times stay as captured.

- `pack/<DIP>` and `system` use unconditional publish, not state suppression or the state replay. They are not retained.
- `seq` counts globally across both topics within one `boot`.
- Each message carries `sent_at` (source clock, epoch ms).
- A failed part has `{"t":…, "err":"busy|failed|unsupported"}`: `busy` = no quiet gap, nothing sent; `failed` = no valid answer after the retries; `unsupported` = the firmware rejects the read (Modbus exception 01/02, EMU1101-EXT capability missing). A part that was not attempted (for example 0x44 after a failed 0x42) is simply absent.
- Raw bytes are taken at receipt, never recomputed from smoothed or energy-corrected HA values.

**`pack/<DIP>`**

```json
{"v":1,"boot":"…","seq":<n>,"sent_at":<ms>,"addr":<DIP>,
 "r42":{"t":<epoch ms right after the answer>,"hex":"<INFO bytes>"},
 "r44":{"t":…,"hex":"…"},
 "r47":{"t":…,"hex":"<169-byte ASCII 0x47 INFO>"}}
```

`r47` appears only in the poll that read it. It is always the canonical 169-byte ASCII INFO; a binary UART1 read gets its checked address byte prepended to the 168 data bytes. That matches the Monitor's `uart1.rs` / `params.rs`.

**`system`**

```json
{"v":1,"boot":…,"seq":…,"sent_at":…,
 "r1000":{"t":…,"hex":"<42 data bytes, BE>"},
 "r0074":{"t":…,"hex":"<2 bytes>"}}
```

No Modbus header or CRC; the Monitor uses `SystemValues::from_registers`.

**`meta`** (retained)

```json
{"v":1,"on":true,"auto_off":<ms|null>,"boot":"…","sent_at":…,"bus":"uart1|uart0","addon":"<version>",
 "packs":[{"addr":0,"r51":{"t":…,"hex":"…"},"a2":…,"a4":…,"maint":{"t":…,"hex":"…"}|null,"r47":{"t":…,"hex":"…"}},…],
 "sets":[{"id":"…","packs":[…],"wiring":"…","plus":<DIP>,"minus":<DIP>},…]}
```

- `meta` describes the configured feed, not whether the add-on is reachable.
  - Reachability comes from the add-on's existing availability topic and last will; the add-on gets no second client.
  - When the feed is off, `meta` is republished with `"on":false`. It is resent after a restart or reconnect, never mixing an old `boot`'s data in.
- `meta.packs[]` fields `r51`, `a2`, `a4`, `maint` (the raw 64/03 identity; the field keeps its name), `r47` are each `null` or `{"t", "hex"}`. `a2`/`a4` are exactly 30 string bytes (60 hex characters) without frame, CRC or address byte; ASCII A2/A4 INFO and binary EMU1101-EXT A2/A4 DATA share this layout. Nothing is invented: on OEM firmware over UART1 the add-on reads no A2, so it is `null`.
- `meta` caches the per-pack identity and the parameters with their original read times. Changes update the cache and are not new measurements. A Monitor that joins late thus has the parameters and serial numbers at once.
- The sets are proposals for the chosen installation in the Monitor, never silent changes to its local settings.

## Monitor: the third connection type

- **Connection type** "Home Assistant (MQTT)": broker host, port, user, password, optional TLS, base topic.
- **Connected** means fresh measurements arrive. A retained `meta.on=true` alone is not enough. If `on` is false: "Feed is off — turn it on in the Seplos BMS app".
- **Adapter:**
  - each `pack/<DIP>` becomes a `PackUpdate` with `read_at = r42.t`; status events use `r44.t` in the history call too, not the arrival time;
  - `system` becomes a `SystemUpdate`;
  - everything goes through the existing store/recording path;
  - duplicates (`boot`, `seq`) are dropped; when `boot` changes, the gap stays a gap.
- **Clocks:**
  - pair distances come from the source read times;
  - the live window runs in source time (`sent_at` plus monotonic time since receipt), because `livePairs.ts` / `pairs.ts` filter against the local `Date.now()` today;
  - connection freshness comes from monotonic receipt progress;
  - recordings keep the original UTC read times;
  - a clear clock offset is shown, and stored times are never rewritten.
- **Read-only:** every write, maintenance, flash and BMS-history command is disabled for this source in the backend, not only in the UI. Stored parameters, files and archives stay viewable.
- **Parameters:** feed parameter blocks serve the live views of this connection; v1 stores no parameter snapshots from the feed, because the feed can't confirm which board an address holds. The balancing line over MQTT uses only the feed's block; without one its thresholds are unknown.
- **Source state:** the Monitor subscribes the add-on's availability topic too; the current boot comes from `meta`, messages of another boot are dropped, a boot change is handled like a reconnect (bank confirmation, notifications, recording session and live pairs start over). A feed session ends its open events at the latest source status time, not at the Monitor's clock.
- **System:** v1 uses `r1000` only; `r0074` is ignored.
- **Pair check:** unchanged 3-s matching on `r42.t`; with too few matches it shows the existing hint, and the limit is not relaxed.
- **Freshness:** per pack and per part, following the observed round time, not one generous connection timeout.

## Related add-on changes (independent of the feed, same release)

- **Bus choice independent of the pack count:**
  - one pack: RS485-1/2 (UART0, 19200 Bd) or CAN socket (UART1, 9600 Bd);
  - several packs: CAN socket only.

  Before v1 the bus followed `number_of_packs`. A single pack on UART1 behaves like multi-pack mode: no 0x5A, and parameters only via EMU1101-EXT.

- **Bus quiet setting:** the quiet before each request becomes a connection setting `bus_quiet_ms`.
  - Range 200–2000 ms, default 1000 ms (the earlier fixed value).
  - Explanation shown with the field: "Wait time without bus traffic before each request. The packs recognize the end of a frame only after about 100 ms of silence; 200 ms leave a safe margin. Longer values slow each poll round, so Home Assistant stores fewer data points. While the OpenBMS Monitor feed is on, 200 ms apply."
  - The round pause `update_interval` stays as it is; with the feed on it is 0.

- **Bug fix:** a failed 0x44 no longer drops a valid 0x42 (see the inventory).

## Left open

- Add-on set to one pack (`number_of_packs = 1`, RS485-1/2) while that port is part of a chain whose master polls its slaves (0x5A): the add-on today only skips those frames. A truly standalone pack has no such traffic. Reporting "pack bus busy with the master" instead belongs to a separate add-on change, not to the feed.
