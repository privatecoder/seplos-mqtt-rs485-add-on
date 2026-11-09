# OpenBMS Monitor Feed (add-on) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The add-on publishes a raw, timestamped measurement stream that OpenBMS Monitor can analyse over MQTT. It gets a bus choice independent of the pack count and an adjustable bus quiet (200–2000 ms), and a failed 0x44 no longer drops a valid 0x42.

**Architecture:**
- `SerialBus` keeps the measured raw INFO bytes and the failure reason of the last request.
- `BatteryPack.read()` splits into `read_core()` (0x42, 0x44) and `read_extras()` (everything optional).
- A new module `feed.py` builds the feed messages and the round order. `app.py` publishes them and sets the effective timing while the persistent switch is on.

**Tech Stack:** Python 3.12, paho-mqtt 1.6 API, aiohttp (Ingress), vanilla JS UI, pytest with `tests/fakes.py`/`tests/frames.py`.

**Spec:** `docs/exchange/openbms-feed-v1.md`. Task 1 copies it from `.orch/openbms-feed-v1.md` (draft r2).

## Global Constraints

- Python 3.12, Paho 1.x API; no new dependencies.
- Topic root `<mqtt_topic>/openbms/v1/`, QoS 0. `pack/<DIP>` and `system` are not retained and use `Publisher.publish` (no state suppression, no replay). `meta` is retained.
- Times are UTC epoch milliseconds taken right after the answer arrived.
- `seq` counts globally over `pack/*` and `system` within one `boot`. `boot` is a random hex id per process start.
- `err` values: `busy` | `failed` | `unsupported`. A part that was not attempted is absent.
- `r47.hex` is always the 169-byte ASCII 0x47 INFO; a binary UART1 read is its address byte + the 168 data bytes.
- `bus_quiet_ms`: integer 200–2000, default 1000. While the feed is on: 200 ms on both buses and round pause 0. Stored settings stay untouched.
- Bus: `"uart0"` or `"uart1"`; `pack_count > 1` requires `"uart1"`.
- No extra periodic BMS reads in feed mode; every existing interval stays.
- The repo is public: no real installation data in tests, docs or examples.
- Docs and commit messages in English; translations DE/EN; code comments as the surrounding code does.
- Golden files change only on purpose: `UPDATE_GOLDEN=1 .venv/bin/pytest`, then review the diff.

## Review Focus

1. **Feed on with no parallel sets:** every pack is its own group, in address order. A set that names a pack ≥ `pack_count` is ignored for ordering. → test in Task 5.
2. **Feed switched off mid-round:** the change is adopted at the next round boundary. Bus quiet and round pause return to the stored values, and `meta` is republished with `"on": false`. → test in Task 6.
3. **0x42 fails:** `r44` is absent, not `failed`, and no `pack/<DIP>` message claims a status. → test in Task 4.
4. **Reconnect of the MQTT client:** the retained `meta` is republished with the current `boot`; pack messages are never replayed. → test in Task 6.
5. **Old `monitoring.json` without `bus`/`bus_quiet_ms`:** it loads with `bus` derived from `pack_count` (`>1` → `uart1`, else `uart0`) and 1000 ms. → test in Task 2.

---

### Task 1: Spec into the repo

**Files:**
- Create: `docs/exchange/openbms-feed-v1.md` (copy of `.orch/openbms-feed-v1.md`, status line "v1, implemented by this plan")

- [ ] **Step 1:** Copy the file; remove "draft"/"Claude + Codex" wording and the `.orch` reference from the status line.
- [ ] **Step 2:** Commit: `git add docs/exchange/openbms-feed-v1.md && git commit -m "docs: OpenBMS Monitor feed v1 spec"`

### Task 2: Bus choice and bus quiet setting

**Files:**
- Modify: `src/seplos_mqtt/monitoring/model.py` (`_CONNECTION_KEYS`, `ConnectionSettings`, `from_dict`, `to_dict`)
- Modify: `src/seplos_mqtt/monitoring/migration.py`
- Modify: `src/seplos_mqtt/config.py` (`Config.bus`, `Config.bus_quiet_ms`, `can_bus`, `baudrate`, `from_monitoring`, `from_env`, `__post_init__`)
- Modify: every `multi_pack` use in `src/seplos_mqtt/{pack,maint,health,status,app}.py`
- Modify: `src/seplos_mqtt/web/static/settings.js`, `web/static/i18n/{de,en}.json`
- Test: `tests/test_monitoring.py`, `tests/test_characterization.py`, `tests/test_web.py`

**Interfaces:**
- Produces:
  - `ConnectionSettings.bus: str` (`"uart0"`|`"uart1"`), `ConnectionSettings.bus_quiet_ms: int`;
  - `Config.bus: str`, `Config.bus_quiet_ms: int`;
  - `Config.can_bus -> bool` (`bus == "uart1"`), which replaces `multi_pack` wherever the *bus* is meant;
  - `Config.multi_pack` stays and means only `number_of_packs > 1`;
  - `Config.baudrate`: 9600 if `can_bus` else 19200 (unless set).

Rule for each `multi_pack` use:
- Which requests are sent (0x5A, 0x47 ASCII, 0xA2/A4 ASCII, MAINT UART1 paths, Modbus system read), discovery `pack_bus`/system device, baud, health timing → `can_bus`.
- Parallel sets and anything that needs more than one pack → `multi_pack`.

- [ ] **Step 1: Write failing tests**

```python
def test_bus_defaults_from_pack_count():          # old documents
    assert ConnectionSettings.from_dict({"transport": "serial", "device": "/dev/x", "pack_count": 3}).bus == "uart1"
    s = ConnectionSettings.from_dict({"transport": "serial", "device": "/dev/x", "pack_count": 1})
    assert (s.bus, s.bus_quiet_ms) == ("uart0", 1000)

def test_several_packs_need_uart1():
    with pytest.raises(ConfigValidationError) as e:
        ConnectionSettings.from_dict({"transport": "serial", "device": "/dev/x", "pack_count": 2, "bus": "uart0"})
    assert ("connection.bus", "value") in e.value.problems

@pytest.mark.parametrize("ms,ok", [(199, False), (200, True), (2000, True), (2001, False), (1.5, False)])
def test_bus_quiet_range(ms, ok): ...

def test_single_pack_on_uart1_polls_like_the_can_socket():
    # api.mode(1, BUS="uart1"): 9600 Bd, no 0x5A request, no ASCII 0x47; FakeBms sees only 0x42/0x44 (+ Modbus)
    # with MAINT_FIRMWARE=True the MAINT UART1 paths run, and discovery has no pack_bus entities but the system device

def test_health_age_follows_bus_quiet():
    assert health.max_poll_age(12, 0, True, 2.0) > health.max_poll_age(12, 0, True, 1.0)
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_monitoring.py tests/test_characterization.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
  - Problem keys follow the existing style (`required`/`type`/`value`/`range`).
  - Extend `tests/api.py` `_FIELDS` with `"BUS": "bus"` and `"BUS_QUIET_MS": "bus_quiet_ms"`; its discovery helper (`tests/api.py:181–184`) decides `pack_bus` from the bus, not the pack count.
  - `health.max_poll_age(number_of_packs, update_interval, can_bus, quiet_s)`: per-pack seconds scale with the quiet (2 requests per pack: `2 * quiet_s` plus the line time the constants hold today).
  - Settings UI: a bus select ("RS485-1/2 (UART0)" / "CAN socket (UART1)"), disabled to UART1 when packs > 1. Plus a numeric field `bus_quiet_ms` in ms with this help text:
    - EN: "Wait time without bus traffic before each request. The packs recognize the end of a frame only after about 100 ms of silence; 200 ms leave a safe margin. Longer values slow each poll round, so Home Assistant stores fewer data points. While the OpenBMS Monitor feed is on, 200 ms apply."
    - DE: „Wartezeit ohne Busverkehr vor jeder Anfrage. Die Packs erkennen das Ende eines Frames erst nach etwa 100 ms Stille; 200 ms lassen sicheren Abstand. Längere Werte verlangsamen jede Abfragerunde, Home Assistant speichert dann weniger Datenpunkte. Solange der OpenBMS-Monitor-Datenstrom aktiv ist, gelten 200 ms.“
  - `main()` builds `SerialBus(port, gap=config.bus_quiet_ms / 1000)`.
- [ ] **Step 4:** Run `.venv/bin/pytest -q && .venv/bin/ruff check src tests`. Expected: all pass. Golden diffs only where the bus decision moved; review them.
- [ ] **Step 5:** Commit `"Bus choice independent of the pack count, adjustable bus quiet (200-2000 ms)"`.

### Task 3: Raw capture and failure reason in `SerialBus`

**Files:**
- Modify: `src/seplos_mqtt/transport.py`, `src/seplos_mqtt/timeutil.py`
- Test: `tests/test_characterization.py` (or new `tests/test_feed.py`)

**Interfaces:**
- Produces:
  - `timeutil.now_ms() -> int` (UTC epoch ms);
  - `SerialBus.last_failure: str | None`, set by `request`, `request_binary`, `read_modbus`, `read_modbus_single_wide`:
    - `"busy"` on `BusBusy`;
    - `"failed"` when there is no valid answer after the retries;
    - `"unsupported"` on a Modbus exception 01/02;
    - `None` on success;
  - `SerialBus.last_at: int | None` = `now_ms()` right after the last valid answer frame;
  - `SerialBus.set_gap(seconds: float) -> None`, which takes effect at the next request.
  - `request_binary` and `query` also set `last_at`, so identity reads (0x51, A2/A4 ASCII and binary, MAINT 64/03) get their read time.

- [ ] **Step 1: Write failing tests**
  - `test_last_failure_busy`: noise keeps the bus busy; `request` raises `BusBusy`; `last_failure == "busy"`.
  - `test_last_failure_failed`: no answer; `request` returns `None`; `last_failure == "failed"`.
  - `test_last_at_after_answer`: with `now_ms` patched to 1234, a valid answer gives `last_at == 1234`.
  - `test_set_gap`: after `set_gap(0.2)`, the fake clock shows the second request waited 0.2 s, not 1.0 s.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `BusBusy` still propagates as today; only the attribute is new.
- [ ] **Step 4:** Run `.venv/bin/pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `"SerialBus: answer time, failure reason, adjustable gap"`.

### Task 4: Split the pack poll, and keep 0x42 when 0x44 fails

**Files:**
- Modify: `src/seplos_mqtt/pack.py` (`read`, new `read_core`, `read_extras`, `raw`), `src/seplos_mqtt/maint.py` (keep the binary 47 block)
- Test: `tests/test_characterization.py`

**Interfaces:**
- Produces:
  - `BatteryPack.read_core() -> tuple[BatteryData | None, bool]`: only 0x42 and 0x44.
    - 0x42 fails: `(None, False)`.
    - 0x44 fails: `(data_without_"telesignalization", False)`.
  - `BatteryPack.read_extras(data: BatteryData) -> None`: everything else `read()` did, in the same order, adding to `data`. That covers MAINT detect/poll/merge, device info, clock, 0x5A, parameters and `alarm_summary`.
    - `alarm_summary` is computed only when `telesignalization` is present; otherwise the HA alarm state is unknown.
  - `BatteryPack.read() -> tuple[BatteryData | None, bool]`: `read_core()` then, whenever `data is not None` (also with `success=False`), `read_extras()`. The behaviour is unchanged except for the bug fix. Only heartbeat and complete-success bookkeeping depend on `success`.
  - `BatteryPack.raw_ids: dict[str, dict | None]`, the identity reads kept raw with their read time, filled where `pack.py`/`maint.py` read them today:
    - `"r51"`: `{"t", "hex"}` of the 0x51 INFO;
    - `"a2"`, `"a4"`: `{"t", "hex"}` with exactly 60 hex characters, the 30 string bytes without frame, CRC or address byte (ASCII A2/A4 INFO and binary MAINT A2/A4 DATA have the same layout);
    - `"maint"`: `{"t", "hex"}` of the 64/03 answer;
    - `None` while not read. No A2 is invented: on OEM firmware on UART1 the add-on reads no A2 today, so it stays `None`.
  - `BatteryPack.raw: dict[str, dict]`, rebuilt by every `read_core()`:
    - `"r42"` / `"r44"`: `{"t": int, "hex": str}` or `{"t": int, "err": str}`;
    - `"r44"` is absent when 0x42 failed.
  - `BatteryPack.raw47: dict | None`: `{"t": int, "hex": str}` of the last parameter read.
    - UART0: the ASCII 0x47 INFO as received.
    - UART1: `bytes([address]) + maint_binary_168`. `t` is the read time of that block, not the publish time.

- [ ] **Step 1: Write failing tests**
  - `test_status_failure_keeps_telemetry`:
    - `FakeBms` answers 0x42 and is silent on 0x44;
    - `poll_pack` publishes `pack-0/sensors` with telemetry values, without `telesignalization`, and without `alarm_summary` values;
    - no heartbeat is published (poll not successful).
  - `test_raw_parts`:
    - 0x42 ok and 0x44 ok: `raw["r42"]["hex"]` equals the INFO hex built by `frames.py`, and `raw["r42"]["t"]` equals the patched `now_ms`;
    - 0x42 silent: `raw == {"r42": {"t": …, "err": "failed"}}`.
  - `test_raw47_uart1_is_canonical`: the MAINT binary 47 answer from `frames.py` gives `raw47["hex"]` of length 338, starting with the address byte.
  - `test_status_failure_keeps_maint_energy_source`: MAINT energy source already chosen (state file), then a 0x44 timeout. The published telemetry carries the MAINT energy totals (or unknown), never the narrow 0x42 values.
  - `test_raw_ids_a2_is_30_bytes`: ASCII A2 on UART0 and binary A2 on UART1 both give 60 hex characters.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The HA output of a fully successful poll stays byte-identical, and the golden files do not change.
- [ ] **Step 4:** Run `.venv/bin/pytest -q`. Expected: PASS, golden files unchanged.
- [ ] **Step 5:** Commit `"A failed 0x44 no longer drops 0x42; keep raw answers"`.

### Task 5: `feed.py` — messages and round order

**Files:**
- Create: `src/seplos_mqtt/feed.py`
- Modify: `src/seplos_mqtt/topics.py`, `src/seplos_mqtt/system.py` (raw registers + read time)
- Test: `tests/test_feed.py`

**Interfaces:**
- Consumes: `BatteryPack.raw`, `raw47` (Task 4); `SerialBus.last_at`, `last_failure` (Task 3); `ParallelSet` (`monitoring/model.py`).
- Produces:
  - `topics.feed_pack(base, addr)`, `topics.feed_system(base)`, `topics.feed_meta(base)` → `<base>/openbms/v1/pack/<addr>`, `…/system`, `…/meta`.
  - `class Feed`, holding `boot: str` (`secrets.token_hex(8)`), the `seq` counter, `on: bool` and `auto_off_ms: int | None`:
    - `next_seq() -> int`;
    - `pack_message(addr, raw, raw47_if_new) -> dict`;
    - `system_message(r1000, r0074) -> dict`;
    - `meta_message(config, packs_cache, sets) -> dict`.
  - `SystemReader.raw: dict`: `{"r1000": {"t", "hex"|"err"}, "r0074": {...}}`, built by each read. `hex` is the register data bytes, big endian, without header or CRC.
  - `round_order(pack_count: int, sets: Sequence[ParallelSet]) -> list[list[int]]`. Groups in the order of each group's lowest address; packs without a set are one group each.
    - Inside a set: `plus`, `minus` (when known), then the rest in physical order.
    - A pack ≥ `pack_count` is dropped, and a set left with fewer than 2 packs falls apart into single groups.

- [ ] **Step 1: Write failing tests**

```python
def test_round_order_sets_first_ends():
    s = ParallelSet(id=1, name="A", packs=(2, 3, 4), wiring="diagonal", plus=4, minus=2)
    assert round_order(6, [s]) == [[0], [1], [4, 2, 3], [5]]

def test_round_order_no_sets():
    assert round_order(3, []) == [[0], [1], [2]]

def test_round_order_ignores_missing_pack():
    assert round_order(2, [ParallelSet(id=1, name="A", packs=(1, 5))]) == [[0], [1]]

def test_pack_message_shape():
    f = Feed(boot="ab", on=True)
    m = f.pack_message(0, {"r42": {"t": 1, "hex": "00"}}, None)
    assert m == {"v": 1, "boot": "ab", "seq": 1, "sent_at": ANY_INT, "addr": 0, "r42": {"t": 1, "hex": "00"}}

def test_seq_is_global():   # pack, system, pack -> 1, 2, 3

def test_meta_contains_sets_and_cache():
    # keys: v, on, auto_off, boot, sent_at, bus, addon, packs[{addr, r51, a2, a4, maint, r47}], sets[{id, packs, wiring, plus, minus}]
```
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_feed.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement.
  - `meta.packs[]` = `{"addr", "r51", "a2", "a4", "maint", "r47"}` straight from `BatteryPack.raw_ids` and `raw47` (Task 4); each is `null` or `{"t", "hex"}`.
  - `addon`: the `Dockerfile` gets `ARG BUILD_VERSION` and `ENV ADDON_VERSION=${BUILD_VERSION}` (the HA builder passes `BUILD_VERSION`); `feed.py` reads `os.environ.get("ADDON_VERSION", "unknown")`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `"Feed messages and round order"`.

### Task 6: Feed switch, timing and loop

**Files:**
- Modify: `src/seplos_mqtt/monitoring/model.py` (top-level `feed: {"on": bool, "auto_off_hours": int | null}`)
- Modify: `src/seplos_mqtt/app.py` (`AppState.feed`, `request_feed`, `adopt_feed`, main loop by groups, `poll_group`, `publish_system_data`, `resync`)
- Modify: `src/seplos_mqtt/state.py` (feed `until_ms` in `seplos_state.json`)
- Modify: `src/seplos_mqtt/web/server.py` (`GET/PUT /api/feed`), `src/seplos_mqtt/status.py` (round time, feed state)
- Modify: `web/static/overview.js` or `settings.js` (switch, auto-off select: off/1 h/4 h/24 h), `web/static/i18n/{de,en}.json`
- Test: `tests/test_feed.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces:
  - `AppState.request_feed(on: bool, auto_off_hours: int | None)` → adopted at the next round boundary by `adopt_feed() -> bool` (changed).
  - Effective timing:
    - `bus.set_gap(0.2 if feed.on else config.bus_quiet_ms / 1000)`;
    - round pause `0 if feed.on else config.mqtt_update_interval`.
  - `poll_group(state, members: list[int])`:
    - `read_core()` for each member, then `read_extras()` for each member whose `data is not None` (also with `success=False`), then the existing publish/availability path per member;
    - with the feed on, `meta` is republished (retained) after the group when any member's `raw_ids` or `raw47` changed;
    - with the feed on, one `pack/<addr>` message per member **after** the group's extras, built from the captured raw parts. The core read times stay as captured; a parameter block read in this poll's extras is included as `r47`.
  - `/api/feed`: GET `{"on", "auto_off_hours", "until_ms", "round_s"}`, PUT `{"on", "auto_off_hours"}`.

- [ ] **Step 1: Write failing tests**
  - `test_feed_on_publishes_pack_and_system`: feed on, 2 packs, one round. The fake MQTT sees `seplos/openbms/v1/pack/0`, `…/pack/1`, `…/system` (retain False) and `…/meta` (retain True); `seq` is 1, 2, 3.
  - `test_feed_on_sets_gap_and_no_round_pause`: `mqtt_update_interval=30`, feed on: no wait after the round and `bus.gap == 0.2`. After switching off: the gap is back to the stored value and the round pause is 30.
  - `test_core_reads_of_a_set_back_to_back`: set (0, 1) and a due clock read on pack 0. The FakeBms request log is `0x42@0, 0x44@0, 0x42@1, 0x44@1, 0x4D@0`.
  - `test_group_keeps_maint_energy_on_status_timeout`: the Task 4 MAINT-energy case run through `poll_group` (set of 2).
  - `test_meta_republished_after_first_identity_reads`: first round reads 0x51/A2/parameters; a subscriber that joins afterwards (retained only) gets them from `meta`.
  - `test_due_parameters_are_in_the_same_pack_message`: parameters due on pack 0; its `pack/0` message carries `r42`, `r44` and `r47`, and `r42.t` is earlier than `r47.t`.
  - `test_feed_off_republishes_meta_off`: `meta` is republished with `"on": False`; no further `pack/*`.
  - `test_auto_off`: `until_ms` passes (patched `now_ms`); the next round boundary switches off, and the stored state says off.
  - `test_reconnect_republishes_meta_not_packs`: after `resync`, `meta` is republished and no `pack/*` replay happens.
  - `test_put_feed_validates`: `auto_off_hours` ∈ {null, 1, 4, 24}, else 400.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - UI hint text while on:
    - EN: "Feed active. The round pause is 0 for now; the bus quiet is 200 ms. Your stored settings are kept. Current round: {s} s. The add-on keeps no archive." plus " Auto-off at {time}." when set.
    - DE: „Datenstrom aktiv. Die Rundenpause ist vorübergehend 0, die Busruhe 200 ms. Deine gespeicherten Einstellungen bleiben erhalten. Aktuelle Runde: {s} s. Das Add-on legt kein Archiv an.“ plus „ Automatisch aus um {time}.“
  - Switch label: EN "OpenBMS Monitor feed", DE „OpenBMS-Monitor-Datenstrom“.
- [ ] **Step 4:** Run `.venv/bin/pytest -q && .venv/bin/ruff check src tests`. Expected: PASS; golden files unchanged when the feed is off.
- [ ] **Step 5:** Commit `"OpenBMS Monitor feed switch"`.

### Task 7: Docs

**Files:**
- Modify: `README.md` (feed section, bus choice, bus quiet), `docs/design/ingress-app.md`, `CLAUDE.md` (two-modes table: bus now independent; `feed.py` in the module list)

- [ ] **Step 1:** Write the sections. Link `docs/exchange/openbms-feed-v1.md`.
- [ ] **Step 2:** Commit `"Docs: OpenBMS Monitor feed, bus choice, bus quiet"`. Release bump (`config.yaml` version) only on the user's go.
